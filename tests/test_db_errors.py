"""Tests for taktis/db.py error-handling paths.

**Pre-test specification restatement**
--------------------------------------
Before a single line of test code was written, the following behaviours were
confirmed from reading the source:

1. ``init_db()`` — ``_PHASES_MIGRATIONS`` block
   * Unconditionally executes ``ALTER TABLE phases ADD COLUMN …`` inside a
     ``try/except``.
   * If the exception message contains ``"duplicate column name"`` (case-
     insensitive) → the error is *swallowed* and logged at DEBUG.  This is the
     expected idempotency path on repeat startups.
   * Any other error → wrapped in ``DatabaseError`` (carrying the original as
     ``__cause__``) and re-raised.
   * The ``planning_options`` / token-column migrations use a ``PRAGMA
     table_info`` guard *without* try/except — tested separately.

2. ``_execute()`` (``repository.py``)
   * Catches only ``aiosqlite.Error`` subclasses.
   * ``"UNIQUE constraint failed"`` in the error message → ``DuplicateError``.
   * Everything else (FK, NOT NULL, PK dup, generic) → ``DatabaseError``.
   * Non-``aiosqlite.Error`` exceptions (``TypeError``, ``RuntimeError``, …)
     bubble through unmodified.

3. ``get_session()``
   * Clean exit → ``commit()`` then ``close()`` in order.
   * Exception in body → ``rollback()`` then ``close()`` in order; original
     exception re-raised.
   * ``close()`` is in a ``finally`` block, so it fires even when ``rollback()``
     itself raises.
   * ``PRAGMA foreign_keys=ON`` is issued on every session.

**Ambiguities resolved** (no assumptions were necessary):
- ``asyncio_mode = "auto"`` is set in pyproject.toml → no marker needed.
- ``aiosqlite.connect`` in ``init_db()`` is used as an async context manager
  (``async with``).
- ``aiosqlite.connect`` in ``get_session()`` is *awaited* directly
  (``db = await aiosqlite.connect(...)``).  Both call patterns are patched
  separately in their respective test classes.

Test plan
---------
Scope (IN):
  * ``taktis/db.py``   — ``init_db()``, ``get_session()``
  * ``taktis/repository.py`` — ``_execute()`` error paths,
    FK/NOT-NULL/PK-dup scenarios not covered by test_repository_errors.py

Scope (OUT):
  * Happy-path CRUD (covered by test_repository.py)
  * UNIQUE constraint on ``projects.name`` / ``experts.name`` (covered by
    test_repository_errors.py)
  * Pipeline, scheduler layers

Entry criteria:
  * All tables created by ``_CREATE_TABLES_SQL`` are available in ``db_conn``
  * ``DATABASE_PATH`` can be redirected via ``monkeypatch``

Exit criteria:
  * All tests green; zero failures permitted
  * No ``sleep``-based waits; no wall-clock dependencies
  * Each test verifies exactly one behaviour

Test categories:
  TC-MIGE-*  init_db() migration: duplicate-column vs. genuine SQL error
  TC-SESS-*  get_session() commit / rollback / close sequences
  TC-CRUD-*  CRUD: FK, NOT NULL, PK-dup constraint vs. system errors
  TC-REG-*   Regression guards (one per bug pattern)
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

import taktis.db as db_mod
from taktis import repository as repo
from taktis.db import get_session, init_db
from taktis.exceptions import DatabaseError, DuplicateError
from taktis.repository import _execute


# ===========================================================================
# Shared fixtures
# ===========================================================================


@pytest_asyncio.fixture
async def file_db(tmp_path, monkeypatch):
    """Redirect DATABASE_PATH to a temp file and yield the path.

    A file DB (not :memory:) is required for ``init_db()`` idempotency tests
    because each ``aiosqlite.connect(':memory:')`` opens a *fresh* database;
    a second call to ``init_db()`` would never see the columns added by the
    first call.
    """
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DATABASE_PATH", db_file)
    yield db_file


# ---------------------------------------------------------------------------
# Mock-builder helpers
# ---------------------------------------------------------------------------


def _session_mock_connection():
    """Return (mock_conn, mock_connect) for patching ``get_session()``'s usage.

    ``get_session()`` does::

        db = await aiosqlite.connect(DATABASE_PATH)

    so ``aiosqlite.connect`` must be an ``AsyncMock`` whose awaited result is
    ``mock_conn``.
    """
    mock_conn = AsyncMock()
    mock_connect = AsyncMock(return_value=mock_conn)
    return mock_conn, mock_connect


def _init_db_connect_mock(genuine_error: Exception):
    """Return a ``mock_connect`` callable for patching ``init_db()``'s usage.

    ``init_db()`` does::

        async with aiosqlite.connect(DATABASE_PATH) as db:

    so the return value must be an *async context manager* (not awaitable).

    All SQL calls succeed (returning an ``AsyncMock`` cursor) *except*
    ``ALTER TABLE phases …`` statements, which raise ``genuine_error``.

    The ``PRAGMA table_info`` responses report that ``planning_options``,
    ``input_tokens``, ``output_tokens``, and ``num_turns`` already exist, so
    the ``if col not in cols`` migration branches are skipped.  Only the
    ``_PHASES_MIGRATIONS`` try/except block is exercised.
    """
    # PRAGMA table_info(projects) → planning_options already present → skip
    proj_cursor = AsyncMock()
    proj_cursor.fetchall.return_value = [
        (0, "planning_options", "TEXT", 0, None, 0),
    ]

    # PRAGMA table_info(tasks) → all token/turn cols present → skip
    task_cursor = AsyncMock()
    task_cursor.fetchall.return_value = [
        (0, "input_tokens",  "INTEGER", 1, "0", 0),
        (1, "output_tokens", "INTEGER", 1, "0", 0),
        (2, "num_turns",     "INTEGER", 1, "0", 0),
    ]

    default_cursor = AsyncMock()
    default_cursor.fetchall.return_value = []
    default_cursor.fetchone.return_value = None

    async def execute_impl(sql, *_args, **_kwargs):
        if "table_info(projects)" in sql:
            return proj_cursor
        if "table_info(tasks)" in sql:
            return task_cursor
        if "ALTER TABLE phases" in sql:
            raise genuine_error
        return default_cursor

    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = execute_impl
    mock_conn.executescript = AsyncMock()
    mock_conn.commit = AsyncMock()

    # Build async context manager
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_connect = MagicMock(return_value=mock_cm)
    return mock_connect, mock_conn


# ===========================================================================
# TC-MIGE: init_db() migration handling
# ===========================================================================


class TestMigrationDuplicateColumnSwallowed:
    """TC-MIGE-01/02/03 — duplicate-column-name errors must never surface."""

    async def test_second_call_does_not_raise(self, file_db):
        """TC-MIGE-01: calling init_db() twice on the same DB succeeds.

        The second call tries to ADD already-existing ``phases`` columns;
        SQLite reports "duplicate column name", which the guard must swallow.
        """
        await init_db()   # creates tables + adds columns
        await init_db()   # second call: duplicate column → must be swallowed

    async def test_third_call_does_not_raise(self, file_db):
        """TC-MIGE-02: idempotency holds across more than two calls."""
        await init_db()
        await init_db()
        await init_db()

    async def test_duplicate_column_logged_at_debug_not_warning(
        self, file_db, caplog
    ):
        """TC-MIGE-03: duplicate-column path logs at DEBUG, never at WARNING/ERROR.

        Precondition: first call succeeds (columns added).
        Action: second call triggers the duplicate-column path.
        Expected: a DEBUG record about the skip; zero WARNING/ERROR records
        related to ``phases`` migration.
        """
        await init_db()  # columns added during first call

        with caplog.at_level(logging.DEBUG, logger="taktis.db"):
            await init_db()

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "already exists" in m or "duplicate" in m.lower()
            for m in debug_msgs
        ), "Expected a DEBUG log about the existing column, found none"

        bad = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "phases" in r.message.lower()
        ]
        assert not bad, f"Unexpected WARNING/ERROR records: {bad}"


class TestMigrationGenuineErrorRaisesDatabaseError:
    """TC-MIGE-04/05/06/07/08 — non-duplicate errors must become DatabaseError."""

    async def test_raises_database_error(self):
        """TC-MIGE-04: non-duplicate ALTER TABLE failure → DatabaseError."""
        genuine = aiosqlite.OperationalError("disk I/O error — not a duplicate column")
        mock_connect, _ = _init_db_connect_mock(genuine)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(DatabaseError):
                await init_db()

    async def test_not_classified_as_duplicate_error(self):
        """TC-MIGE-05: the raised DatabaseError must NOT be a DuplicateError."""
        genuine = aiosqlite.OperationalError("some unexpected SQL failure")
        mock_connect, _ = _init_db_connect_mock(genuine)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(DatabaseError) as exc_info:
                await init_db()

        assert not isinstance(exc_info.value, DuplicateError)

    async def test_original_exception_chained_as_cause(self):
        """TC-MIGE-06: DatabaseError.__cause__ is the original aiosqlite error."""
        genuine = aiosqlite.OperationalError("table is locked")
        mock_connect, _ = _init_db_connect_mock(genuine)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(DatabaseError) as exc_info:
                await init_db()

        err = exc_info.value
        assert err.cause is genuine
        assert err.__cause__ is genuine

    async def test_message_contains_column_name(self):
        """TC-MIGE-07: error message identifies the column that triggered failure."""
        genuine = aiosqlite.OperationalError("random error")
        mock_connect, _ = _init_db_connect_mock(genuine)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(DatabaseError) as exc_info:
                await init_db()

        # The first column in _PHASES_MIGRATIONS is 'current_wave'
        msg = exc_info.value.message
        assert "current_wave" in msg or "updated_at" in msg, (
            f"Column name absent from error message: {msg!r}"
        )

    async def test_message_mentions_phases_table(self):
        """TC-MIGE-08: error message references the 'phases' table."""
        genuine = aiosqlite.OperationalError("random error")
        mock_connect, _ = _init_db_connect_mock(genuine)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(DatabaseError) as exc_info:
                await init_db()

        assert "phases" in exc_info.value.message


class TestMigrationDuplicateColumnBoundary:
    """TC-MIGE-09 — boundary: substring match must fire even on unusual messages."""

    async def test_error_containing_duplicate_column_name_substring_is_swallowed(self):
        """TC-MIGE-09: guard is substring-based (case-insensitive).

        If an OperationalError's text happens to contain "duplicate column
        name" the guard must swallow it, exactly as it would for a real
        SQLite "already exists" response.

        This is a boundary-value test: verifying the guard does not accidentally
        over- or under-match on capitalisation.
        """
        # Use the exact lowercase phrase the guard searches for
        matching_error = aiosqlite.OperationalError(
            "duplicate column name: current_wave"
        )
        mock_connect, _ = _init_db_connect_mock(matching_error)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            # Must NOT raise — the guard should swallow this
            await init_db()

    async def test_mixed_case_duplicate_column_name_is_swallowed(self):
        """TC-MIGE-10: case-insensitive match — 'Duplicate Column Name' is swallowed."""
        mixed_case = aiosqlite.OperationalError(
            "Duplicate Column Name: updated_at"
        )
        mock_connect, _ = _init_db_connect_mock(mixed_case)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            await init_db()  # must not raise


# ===========================================================================
# TC-SESS: get_session() commit / rollback / close sequences
# ===========================================================================

import taktis.db as _db_module


@pytest.fixture(autouse=True, scope="function")
def _reset_pool():
    """Ensure the connection pool is not active for get_session unit tests."""
    saved = _db_module._pool
    _db_module._pool = None
    yield
    _db_module._pool = saved


class TestGetSessionSuccessPath:
    """TC-SESS-01/02/03/04/05 — clean exit: commit then close; no rollback."""

    async def test_commit_called_on_success(self):
        """TC-SESS-01: commit() is awaited exactly once on clean exit."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            async with get_session():
                pass

        mock_conn.commit.assert_awaited_once()

    async def test_rollback_not_called_on_success(self):
        """TC-SESS-02: rollback() must never be called on clean exit."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            async with get_session():
                pass

        mock_conn.rollback.assert_not_awaited()

    async def test_close_called_on_success(self):
        """TC-SESS-03: close() is awaited exactly once on clean exit."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            async with get_session():
                pass

        mock_conn.close.assert_awaited_once()

    async def test_commit_before_close_on_success(self):
        """TC-SESS-04: commit() fires before close() (not just both called)."""
        call_order: list[str] = []
        mock_conn = AsyncMock()
        mock_conn.commit.side_effect = lambda: call_order.append("commit")
        mock_conn.close.side_effect = lambda: call_order.append("close")
        mock_connect = AsyncMock(return_value=mock_conn)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            async with get_session():
                pass

        assert call_order == ["commit", "close"], (
            f"Expected [commit, close]; got {call_order}"
        )

    async def test_yields_the_connection_object(self):
        """TC-SESS-05: the context variable is exactly the connection returned by connect()."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            async with get_session() as conn:
                assert conn is mock_conn

    async def test_foreign_keys_pragma_issued(self):
        """TC-SESS-06: PRAGMA foreign_keys=ON is executed on every session."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            async with get_session():
                pass

        executed_sql = [
            c.args[0] if c.args else ""
            for c in mock_conn.execute.call_args_list
        ]
        assert any("foreign_keys" in s.lower() for s in executed_sql), (
            "PRAGMA foreign_keys=ON was not issued; "
            f"SQL calls observed: {executed_sql}"
        )


class TestGetSessionExceptionPath:
    """TC-SESS-07/08/09/10/11/12 — exception in body: rollback then close; re-raise."""

    async def test_rollback_called_on_exception(self):
        """TC-SESS-07: rollback() is awaited when the body raises."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(RuntimeError):
                async with get_session():
                    raise RuntimeError("body failure")

        mock_conn.rollback.assert_awaited_once()

    async def test_commit_not_called_on_exception(self):
        """TC-SESS-08: commit() must never be called when the body raises."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(RuntimeError):
                async with get_session():
                    raise RuntimeError("boom")

        mock_conn.commit.assert_not_awaited()

    async def test_original_exception_propagates_unchanged(self):
        """TC-SESS-09: the exact original exception object is re-raised (no wrapper)."""
        mock_conn, mock_connect = _session_mock_connection()
        sentinel = ValueError("exact original exception")

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(ValueError) as exc_info:
                async with get_session():
                    raise sentinel

        assert exc_info.value is sentinel

    async def test_rollback_before_close_on_exception(self):
        """TC-SESS-10: rollback() fires before close() when body raises."""
        call_order: list[str] = []
        mock_conn = AsyncMock()
        mock_conn.rollback.side_effect = lambda: call_order.append("rollback")
        mock_conn.close.side_effect = lambda: call_order.append("close")
        mock_connect = AsyncMock(return_value=mock_conn)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(RuntimeError):
                async with get_session():
                    raise RuntimeError("body err")

        assert call_order == ["rollback", "close"], (
            f"Expected [rollback, close]; got {call_order}"
        )

    async def test_close_called_even_when_rollback_raises(self):
        """TC-SESS-11: close() (finally block) fires even if rollback() raises.

        When rollback() itself throws, the new exception propagates.  The
        ``finally`` clause must still call ``close()``.
        """
        mock_conn = AsyncMock()
        mock_conn.rollback.side_effect = aiosqlite.OperationalError(
            "rollback I/O failure"
        )
        mock_connect = AsyncMock(return_value=mock_conn)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(Exception):
                async with get_session():
                    raise RuntimeError("body err")

        mock_conn.close.assert_awaited_once()

    async def test_database_error_from_body_propagates_correctly(self):
        """TC-SESS-12: DatabaseError raised in body propagates after rollback."""
        mock_conn, mock_connect = _session_mock_connection()
        db_err = DatabaseError("intentional db error inside session")

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(DatabaseError) as exc_info:
                async with get_session():
                    raise db_err

        assert exc_info.value is db_err
        mock_conn.rollback.assert_awaited_once()


class TestGetSessionRollbackLogging:
    """TC-SESS-13 — rollback path logs at ERROR (via logger.exception)."""

    async def test_error_level_log_emitted_on_rollback(self, caplog):
        """TC-SESS-13: an ERROR-level log record is emitted when rolling back."""
        mock_conn, mock_connect = _session_mock_connection()

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with caplog.at_level(logging.ERROR, logger="taktis.db"):
                with pytest.raises(RuntimeError):
                    async with get_session():
                        raise RuntimeError("trigger rollback log")

        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "Expected at least one ERROR-level log from get_session() rollback"
        )


# ===========================================================================
# TC-CRUD: constraint violations vs. system errors
# ===========================================================================


class TestForeignKeyViolation:
    """TC-CRUD-FK-01/02 — FK failures → DatabaseError, NOT DuplicateError."""

    async def test_task_output_with_nonexistent_task_id_raises_database_error(
        self, db_conn
    ):
        """TC-CRUD-FK-01: inserting task_output for a ghost task_id is a FK violation.

        SQLite error: "FOREIGN KEY constraint failed" — does NOT contain
        "UNIQUE constraint failed", so must be DatabaseError.
        """
        with pytest.raises(DatabaseError) as exc_info:
            await repo.create_task_output(
                db_conn,
                task_id="nonexistent-task-id-xyz",
                event_type="stdout",
                content={"text": "hello"},
            )

        assert not isinstance(exc_info.value, DuplicateError), (
            "FK violation must raise DatabaseError, not DuplicateError"
        )

    async def test_phase_with_nonexistent_project_id_raises_database_error(
        self, db_conn
    ):
        """TC-CRUD-FK-02: inserting a phase with an unknown project_id is a FK violation."""
        with pytest.raises(DatabaseError) as exc_info:
            await repo.create_phase(
                db_conn,
                project_id="ghost-project-id-000",
                name="Orphan Phase",
                phase_number=1,
            )

        assert not isinstance(exc_info.value, DuplicateError)


class TestNonAiosqliteErrorPassthrough:
    """TC-CRUD-SYS-01/02/03 — non-aiosqlite.Error exceptions pass through _execute."""

    async def test_type_error_propagates_unmodified(self):
        """TC-CRUD-SYS-01: TypeError is NOT caught by _execute."""
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        original = TypeError("unexpected type in SQL binding")
        mock_conn.execute.side_effect = original

        with pytest.raises(TypeError) as exc_info:
            await _execute(mock_conn, "SELECT 1", label="type_err")

        assert exc_info.value is original

    async def test_value_error_propagates_unmodified(self):
        """TC-CRUD-SYS-02: ValueError is NOT caught by _execute."""
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        original = ValueError("bad parameter value")
        mock_conn.execute.side_effect = original

        with pytest.raises(ValueError) as exc_info:
            await _execute(mock_conn, "SELECT 1", label="val_err")

        assert exc_info.value is original

    async def test_runtime_error_not_wrapped_in_database_error(self):
        """TC-CRUD-SYS-03: RuntimeError passes through without being wrapped."""
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        original = RuntimeError("connection pool exhausted")
        mock_conn.execute.side_effect = original

        with pytest.raises(RuntimeError) as exc_info:
            await _execute(mock_conn, "SELECT 1", label="rt_err")

        assert exc_info.value is original
        # Must not be re-wrapped as DatabaseError
        assert not isinstance(exc_info.value, DatabaseError)


class TestPrimaryKeyDuplicate:
    """TC-CRUD-PK-01/02 — duplicate PK values raise DuplicateError.

    SQLite treats the PRIMARY KEY as a UNIQUE constraint, so the error
    message contains "UNIQUE constraint failed".
    """

    async def test_task_duplicate_explicit_id_raises_duplicate_error(self, db_conn):
        """TC-CRUD-PK-01: two tasks with identical explicit IDs → DuplicateError."""
        project = await repo.create_project(db_conn, name="pk-dup-proj")
        task_id = "fixed-task-id-00001"

        await repo.create_task(
            db_conn, id=task_id, project_id=project["id"], name="task-original"
        )

        with pytest.raises(DuplicateError):
            await repo.create_task(
                db_conn, id=task_id, project_id=project["id"], name="task-copy"
            )

    async def test_expert_duplicate_explicit_id_raises_duplicate_error(self, db_conn):
        """TC-CRUD-PK-02: two experts with identical explicit IDs → DuplicateError."""
        expert_id = "fixed-expert-id-11111"

        await repo.create_expert(db_conn, id=expert_id, name="expert-original")

        with pytest.raises(DuplicateError):
            await repo.create_expert(db_conn, id=expert_id, name="expert-copy")


class TestProjectStateDuplicate:
    """TC-CRUD-STATE-01 — project_states.project_id is UNIQUE."""

    async def test_second_state_for_same_project_raises_duplicate_error(
        self, db_conn
    ):
        """TC-CRUD-STATE-01: creating a second ProjectState for an existing project
        violates the UNIQUE constraint on project_states.project_id.
        """
        project = await repo.create_project(db_conn, name="ps-dup-proj")
        await repo.create_project_state(db_conn, project["id"])

        with pytest.raises(DuplicateError) as exc_info:
            await repo.create_project_state(db_conn, project["id"])

        err = exc_info.value
        assert err.constraint is not None
        assert "project_states" in err.constraint


class TestExecuteWithAiosqliteErrorSubclasses:
    """TC-CRUD-ERR-01/02 — aiosqlite.Error subclasses are caught and classified."""

    async def test_integrity_error_non_unique_maps_to_database_error(self):
        """TC-CRUD-ERR-01: aiosqlite.IntegrityError without UNIQUE keyword → DatabaseError."""
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        fk_error = aiosqlite.IntegrityError("FOREIGN KEY constraint failed")
        mock_conn.execute.side_effect = fk_error

        with pytest.raises(DatabaseError) as exc_info:
            await _execute(mock_conn, "INSERT ...", label="fk_classify")

        assert not isinstance(exc_info.value, DuplicateError)
        assert exc_info.value.cause is fk_error

    async def test_operational_error_maps_to_database_error(self):
        """TC-CRUD-ERR-02: aiosqlite.OperationalError maps to DatabaseError."""
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        op_error = aiosqlite.OperationalError("no such table: ghosts")
        mock_conn.execute.side_effect = op_error

        with pytest.raises(DatabaseError) as exc_info:
            await _execute(mock_conn, "SELECT * FROM ghosts", label="op_err")

        assert not isinstance(exc_info.value, DuplicateError)
        assert exc_info.value.cause is op_error


# ===========================================================================
# TC-REG: Regression guards
# ===========================================================================


class TestRegressions:
    """One regression test per bug pattern discovered or at risk.

    Naming convention: ``test_regression_<bug_slug>``
    """

    async def test_regression_session_reraises_original_not_a_new_exception(self):
        """TC-REG-01: get_session() re-raises the exact body exception, not a wrapper.

        Risk: if the rollback logic introduced its own exception (e.g., from
        a logging call), the original exception identity could be lost.
        """
        mock_conn, mock_connect = _session_mock_connection()
        original_err = KeyError("original body error — must be preserved")

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(KeyError) as exc_info:
                async with get_session():
                    raise original_err

        assert exc_info.value is original_err, (
            "get_session() must re-raise the original exception object"
        )

    async def test_regression_init_db_idempotent_after_column_migration(
        self, file_db
    ):
        """TC-REG-02: init_db() must never raise DatabaseError on the second call.

        If the duplicate-column guard were removed or the error-message format
        changed, every startup after the first would fail with DatabaseError.
        """
        await init_db()
        try:
            await init_db()
        except DatabaseError as exc:
            pytest.fail(
                f"init_db() raised DatabaseError on the second call: {exc}"
            )

    async def test_regression_fk_violation_not_misclassified_as_duplicate(
        self, db_conn
    ):
        """TC-REG-03: FK violations must never be surfaced as DuplicateError.

        Risk: if the constraint-detection code over-matched, a legitimate
        "record not found" error would be shown to the user as "duplicate
        record", leading to confusing UI messages.
        """
        with pytest.raises(DatabaseError) as exc_info:
            await repo.create_task_output(
                db_conn,
                task_id="ghost-task-reg-01",
                event_type="result",
            )

        # Strict type check: must be DatabaseError itself, not DuplicateError
        assert type(exc_info.value) is DatabaseError, (
            f"FK violation yielded {type(exc_info.value).__name__}, "
            "expected exactly DatabaseError"
        )

    async def test_regression_session_close_not_skipped_on_rollback_failure(self):
        """TC-REG-04: connection.close() must not be skipped if rollback() raises.

        Risk: if close() were called before rollback() (outside the finally
        block), a rollback failure would leak an open connection.
        """
        mock_conn = AsyncMock()
        mock_conn.rollback.side_effect = aiosqlite.OperationalError(
            "disk full — cannot rollback"
        )
        mock_connect = AsyncMock(return_value=mock_conn)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(Exception):
                async with get_session():
                    raise RuntimeError("trigger rollback")

        mock_conn.close.assert_awaited_once()

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "SOURCE BUG: In get_session(), 'await db.execute(PRAGMA foreign_keys=ON)' "
            "runs at line 228, *before* the try/finally block that calls db.close(). "
            "If the PRAGMA call raises, db.close() is never called, leaking the "
            "connection.  Fix: move the PRAGMA call inside the try block.  Remove "
            "this xfail marker once the source is corrected."
        ),
    )
    async def test_regression_pragma_failure_does_not_leak_connection(self):
        """TC-REG-06 (xfail — source bug): close() must fire even when PRAGMA raises.

        Desired behaviour: any exception raised before ``yield`` must still
        trigger ``db.close()`` so the connection is returned to the OS.

        Current behaviour: PRAGMA failure bypasses the finally block, so
        ``db.close()`` is silently skipped — a file-descriptor / WAL-lock leak.
        """
        mock_conn = AsyncMock()
        # Simulate PRAGMA foreign_keys=ON raising an error (e.g. read-only DB)
        mock_conn.execute.side_effect = aiosqlite.OperationalError(
            "attempt to write a readonly database"
        )
        mock_connect = AsyncMock(return_value=mock_conn)

        with patch("taktis.db.aiosqlite.connect", mock_connect):
            with pytest.raises(Exception):
                async with get_session():
                    pass  # pragma: no cover — PRAGMA raises before yield

        # DESIRED: close() must always fire to release the connection
        mock_conn.close.assert_awaited_once()

    async def test_regression_non_aiosqlite_errors_not_silently_swallowed(self):
        """TC-REG-05: _execute must not accidentally catch TypeError/ValueError.

        Risk: an overly broad except clause (``except Exception`` instead of
        ``except aiosqlite.Error``) would hide programming errors.
        """
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        programming_err = TypeError("wrong number of bind parameters")
        mock_conn.execute.side_effect = programming_err

        with pytest.raises(TypeError) as exc_info:
            await _execute(mock_conn, "SELECT ?", ("a", "b"), label="param_count_err")

        assert exc_info.value is programming_err
