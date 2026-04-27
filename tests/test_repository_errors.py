"""Tests for repository._execute error-handling — ERR-08.

Covers:
- DatabaseError is raised (and chains the original aiosqlite.Error) when a
  query fails for a generic reason.
- DuplicateError is raised (and is a DatabaseError subclass) on UNIQUE
  constraint violations, carrying a ``constraint`` attribute.
- The ``label`` argument appears in the exception message.
- format_error_for_user() maps both exception types to safe UI strings.
- Real CRUD functions (create_project, create_expert) surface DuplicateError
  on name collision.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from taktis import repository as repo
from taktis.exceptions import (
    DatabaseError,
    DuplicateError,
    format_error_for_user,
)
from taktis.repository import _execute


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aiosqlite_error(message: str) -> aiosqlite.Error:
    """Return an aiosqlite.Error whose str() equals *message*."""
    err = aiosqlite.Error(message)
    return err


def _make_integrity_error(message: str) -> aiosqlite.IntegrityError:
    """Return an aiosqlite.IntegrityError (subclass of aiosqlite.Error)."""
    return aiosqlite.IntegrityError(message)


# ---------------------------------------------------------------------------
# _execute — unit tests with a mocked connection
# ---------------------------------------------------------------------------


class TestExecuteGenericError:
    """_execute maps generic aiosqlite.Error → DatabaseError."""

    @pytest.mark.asyncio
    async def test_raises_database_error(self):
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_aiosqlite_error("disk I/O error")

        with pytest.raises(DatabaseError) as exc_info:
            await _execute(conn, "SELECT 1", label="test_op")

        err = exc_info.value
        assert isinstance(err, DatabaseError)
        # Not a duplicate — must NOT be DuplicateError
        assert not isinstance(err, DuplicateError)

    @pytest.mark.asyncio
    async def test_label_in_message(self):
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_aiosqlite_error("some error")

        with pytest.raises(DatabaseError) as exc_info:
            await _execute(conn, "SELECT 1", label="my_custom_label")

        assert "my_custom_label" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_cause_chained(self):
        original = _make_aiosqlite_error("disk I/O error")
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = original

        with pytest.raises(DatabaseError) as exc_info:
            await _execute(conn, "SELECT 1", label="test_chain")

        err = exc_info.value
        # Both the .cause attribute and __cause__ must point to the original.
        assert err.cause is original
        assert err.__cause__ is original

    @pytest.mark.asyncio
    async def test_returns_cursor_on_success(self):
        mock_cursor = MagicMock()
        conn = MagicMock()
        # Explicitly make execute an AsyncMock so `await conn.execute(...)` works.
        conn.execute = AsyncMock(return_value=mock_cursor)

        result = await _execute(conn, "SELECT 1", label="ok_op")
        assert result is mock_cursor

    @pytest.mark.asyncio
    async def test_empty_params_logged_as_zero(self, caplog):
        """_execute does not crash when params is omitted (defaults to ())."""
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_aiosqlite_error("error")

        import logging
        with caplog.at_level(logging.ERROR, logger="taktis.repository"):
            with pytest.raises(DatabaseError):
                await _execute(conn, "SELECT 1", label="no_params")

        # Param count should appear as 0 in the log record.
        assert any("params count: 0" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_param_values_not_logged(self, caplog):
        """Parameter values (potentially sensitive) must never appear in logs."""
        secret_value = "super-secret-password-12345"
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_aiosqlite_error("error")

        import logging
        with caplog.at_level(logging.ERROR, logger="taktis.repository"):
            with pytest.raises(DatabaseError):
                await _execute(
                    conn, "SELECT 1 WHERE x = ?", (secret_value,), label="secret_test"
                )

        full_log = " ".join(r.message for r in caplog.records)
        assert secret_value not in full_log


class TestExecuteUniqueConstraint:
    """_execute maps UNIQUE constraint failures → DuplicateError."""

    @pytest.mark.asyncio
    async def test_raises_duplicate_error(self):
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_integrity_error(
            "UNIQUE constraint failed: projects.name"
        )

        with pytest.raises(DuplicateError):
            await _execute(conn, "INSERT INTO projects VALUES (?)", ("x",), label="dup_test")

    @pytest.mark.asyncio
    async def test_duplicate_error_is_database_error(self):
        """DuplicateError must be catch-able as DatabaseError."""
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_integrity_error(
            "UNIQUE constraint failed: experts.name"
        )

        with pytest.raises(DatabaseError):
            await _execute(conn, "INSERT", label="dup_hierarchy")

    @pytest.mark.asyncio
    async def test_constraint_attribute_populated(self):
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_integrity_error(
            "UNIQUE constraint failed: projects.name"
        )

        with pytest.raises(DuplicateError) as exc_info:
            await _execute(conn, "INSERT", label="dup_attr")

        err = exc_info.value
        # constraint should be the table.column fragment from the SQLite message
        assert err.constraint == "projects.name"

    @pytest.mark.asyncio
    async def test_constraint_in_message(self):
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_integrity_error(
            "UNIQUE constraint failed: tasks.id"
        )

        with pytest.raises(DuplicateError) as exc_info:
            await _execute(conn, "INSERT", label="dup_msg")

        # Both the constraint fragment and the label must appear in the message.
        msg = str(exc_info.value)
        assert "tasks.id" in msg
        assert "dup_msg" in msg

    @pytest.mark.asyncio
    async def test_cause_chained_on_duplicate(self):
        original = _make_integrity_error("UNIQUE constraint failed: experts.name")
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = original

        with pytest.raises(DuplicateError) as exc_info:
            await _execute(conn, "INSERT", label="dup_chain")

        err = exc_info.value
        assert err.cause is original
        assert err.__cause__ is original

    @pytest.mark.asyncio
    async def test_unique_constraint_without_colon(self):
        """Handles rare edge case where message has no ':' after 'failed'."""
        conn = AsyncMock(spec=aiosqlite.Connection)
        conn.execute.side_effect = _make_integrity_error("UNIQUE constraint failed")

        with pytest.raises(DuplicateError) as exc_info:
            await _execute(conn, "INSERT", label="dup_no_colon")

        # constraint attribute should be None when not parseable
        assert exc_info.value.constraint is None


# ---------------------------------------------------------------------------
# Integration tests — real in-memory SQLite via db_conn fixture
# ---------------------------------------------------------------------------


class TestRealCrudDuplicateError:
    """Verify DuplicateError is raised by real CRUD functions on name collision."""

    @pytest.mark.asyncio
    async def test_create_project_duplicate_name(self, db_conn):
        await repo.create_project(db_conn, name="unique-proj")

        with pytest.raises(DuplicateError) as exc_info:
            await repo.create_project(db_conn, name="unique-proj")

        err = exc_info.value
        # Must carry the constraint detail
        assert err.constraint is not None
        assert "projects" in err.constraint

    @pytest.mark.asyncio
    async def test_create_project_duplicate_is_database_error(self, db_conn):
        await repo.create_project(db_conn, name="dup-hierarchy")

        with pytest.raises(DatabaseError):
            await repo.create_project(db_conn, name="dup-hierarchy")

    @pytest.mark.asyncio
    async def test_create_expert_duplicate_name(self, db_conn):
        await repo.create_expert(db_conn, name="my-expert")

        with pytest.raises(DuplicateError) as exc_info:
            await repo.create_expert(db_conn, name="my-expert")

        err = exc_info.value
        assert err.constraint is not None
        assert "experts" in err.constraint

    @pytest.mark.asyncio
    async def test_unique_after_delete(self, db_conn):
        """Deleting a project allows re-creating with the same name — no error."""
        await repo.create_project(db_conn, name="reuse-me")
        await repo.delete_project(db_conn, "reuse-me")
        # Should not raise
        project = await repo.create_project(db_conn, name="reuse-me")
        assert project["name"] == "reuse-me"


# ---------------------------------------------------------------------------
# format_error_for_user integration
# ---------------------------------------------------------------------------


class TestFormatErrorForUser:
    """format_error_for_user returns safe strings for repo exception types."""

    def test_database_error_safe_message(self):
        err = DatabaseError("SELECT * FROM users", cause=RuntimeError("conn lost"))
        msg = format_error_for_user(err)
        assert "database" in msg.lower()
        # Must not expose the raw message containing the SQL
        assert "SELECT" not in msg

    def test_duplicate_error_safe_message(self):
        err = DuplicateError(
            "Duplicate value in create_project: projects.name already exists",
            constraint="projects.name",
        )
        msg = format_error_for_user(err)
        # The user-facing string should mention name/identifier, not SQL internals
        assert msg  # non-empty
        assert "projects.name" not in msg  # schema detail not leaked

    def test_duplicate_error_is_catchable_as_database_error_in_format(self):
        """DuplicateError should resolve to its own entry, not DatabaseError's."""
        dup = DuplicateError("dup", constraint="tbl.col")
        db_err = DatabaseError("generic")
        dup_msg = format_error_for_user(dup)
        db_msg = format_error_for_user(db_err)
        # Both should return user-safe strings; DuplicateError gets its own msg
        assert dup_msg != db_msg
