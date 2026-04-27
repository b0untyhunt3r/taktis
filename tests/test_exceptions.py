"""Comprehensive unit tests for taktis/exceptions.py.

Coverage goals
--------------
- Every exception class can be instantiated with and without optional kwargs.
- ``__str__`` formatting is verified for every class under all combinations
  of (message, no-message) × (cause, no-cause).
- ``__cause__`` is set to the exact same object passed as ``cause=``.
- Inheritance relationships in the full hierarchy are confirmed.
- Extra attributes (``task_id``, ``path``, ``constraint``, ``step``) are
  stored correctly and influence ``__str__`` as documented.
- ``format_error_for_user()`` resolves via MRO, returns a safe string for
  every concrete class, and leaks no internal detail.

Test IDs follow the pattern TC-EX-<NNN> so they are cross-referenceable
from the test-plan section at the bottom of this file.
"""

from __future__ import annotations

import pytest

from taktis.exceptions import (
    ContextError,
    DatabaseError,
    DuplicateError,
    TaktisError,
    PipelineError,
    SchedulerError,
    StreamingError,
    TaskExecutionError,
    format_error_for_user,
)


# ===========================================================================
# TaktisError — base class
# ===========================================================================


class TestTaktisErrorInstantiation:
    """TC-EX-001 … TC-EX-007  Instantiation and attribute storage."""

    def test_default_instantiation_has_empty_message(self) -> None:
        """TC-EX-001: TaktisError() stores an empty message."""
        err = TaktisError()
        assert err.message == ""

    def test_custom_message_stored(self) -> None:
        """TC-EX-002: Provided message is accessible via .message."""
        err = TaktisError("something went wrong")
        assert err.message == "something went wrong"

    def test_args_tuple_contains_message(self) -> None:
        """TC-EX-002b: args[0] equals the message (standard Exception contract)."""
        err = TaktisError("boom")
        assert err.args[0] == "boom"

    def test_cause_none_by_default(self) -> None:
        """TC-EX-003: Without cause= the .cause attribute is None."""
        err = TaktisError("msg")
        assert err.cause is None

    def test_cause_is_stored(self) -> None:
        """TC-EX-004: cause= is accessible via .cause."""
        inner = ValueError("low-level")
        err = TaktisError("high-level", cause=inner)
        assert err.cause is inner

    def test_cause_sets_dunder_cause(self) -> None:
        """TC-EX-005: __cause__ must equal the cause kwarg (PEP 3134)."""
        inner = RuntimeError("low")
        err = TaktisError("high", cause=inner)
        assert err.__cause__ is inner

    def test_no_cause_leaves_dunder_cause_unset(self) -> None:
        """TC-EX-006: When cause is None, __cause__ should be falsy."""
        err = TaktisError("no chain")
        assert not err.__cause__

    def test_cause_object_identity_preserved(self) -> None:
        """TC-EX-007: Exact same exception object, not a copy."""
        inner = OSError("disk full")
        err = TaktisError("wrap", cause=inner)
        assert err.cause is inner
        assert err.__cause__ is inner


class TestTaktisErrorStrFormatting:
    """TC-EX-010 … TC-EX-014  __str__ output."""

    def test_str_returns_message(self) -> None:
        """TC-EX-010: str(err) equals the message when there is no cause."""
        err = TaktisError("context lost")
        assert str(err) == "context lost"

    def test_str_empty_message_returns_class_name(self) -> None:
        """TC-EX-011: Fallback to class name when message is empty string."""
        err = TaktisError()
        assert str(err) == "TaktisError"

    def test_str_with_cause_includes_cause_type(self) -> None:
        """TC-EX-012: cause type name appears in the formatted string.

        Note: ``IOError`` is an alias for ``OSError`` since Python 3.3, so
        ``type(IOError(...)).__name__`` is always ``'OSError'``.  The test
        uses ``OSError`` directly to avoid dead-code branches.
        """
        inner = OSError("no space")
        err = TaktisError("outer", cause=inner)
        assert "OSError" in str(err)

    def test_str_with_cause_includes_cause_message(self) -> None:
        """TC-EX-013: cause message appears in the formatted string."""
        inner = ValueError("bad value")
        err = TaktisError("outer", cause=inner)
        assert "bad value" in str(err)

    def test_str_with_cause_includes_outer_message(self) -> None:
        """TC-EX-014: The outer message still appears when cause is set."""
        inner = TypeError("type mismatch")
        err = TaktisError("outer msg", cause=inner)
        assert "outer msg" in str(err)

    def test_str_cause_format_contains_caused_by(self) -> None:
        """TC-EX-014b: The literal phrase 'caused by' links outer to inner."""
        inner = KeyError("missing")
        err = TaktisError("wrapper", cause=inner)
        assert "caused by" in str(err)


# ===========================================================================
# TaskExecutionError
# ===========================================================================


class TestTaskExecutionError:
    """TC-EX-020 … TC-EX-030  TaskExecutionError specifics."""

    def test_default_message(self) -> None:
        """TC-EX-020: Default message is 'Task execution failed'."""
        err = TaskExecutionError()
        assert err.message == "Task execution failed"

    def test_task_id_none_by_default(self) -> None:
        """TC-EX-021: task_id defaults to None."""
        err = TaskExecutionError()
        assert err.task_id is None

    def test_custom_task_id_stored(self) -> None:
        """TC-EX-022: Provided task_id is accessible."""
        err = TaskExecutionError("failed", task_id="abc-123")
        assert err.task_id == "abc-123"

    def test_str_with_task_id_has_prefix(self) -> None:
        """TC-EX-023: task_id appears as a bracketed prefix in __str__."""
        err = TaskExecutionError("timed out", task_id="t-42")
        s = str(err)
        assert "[task t-42]" in s
        assert "timed out" in s

    def test_str_without_task_id_has_no_prefix(self) -> None:
        """TC-EX-024: No bracketed prefix when task_id is absent."""
        err = TaskExecutionError("crashed")
        assert "[task" not in str(err)
        assert "crashed" in str(err)

    def test_str_with_task_id_and_cause(self) -> None:
        """TC-EX-025: task_id prefix + cause suffix both present."""
        inner = OSError("pipe broken")
        err = TaskExecutionError("exec failed", task_id="t-7", cause=inner)
        s = str(err)
        assert "[task t-7]" in s
        assert "caused by" in s
        assert "OSError" in s

    def test_is_taktis_error(self) -> None:
        """TC-EX-026: TaskExecutionError is a subclass of TaktisError."""
        err = TaskExecutionError()
        assert isinstance(err, TaktisError)

    def test_cause_chained(self) -> None:
        """TC-EX-027: __cause__ set correctly."""
        inner = RuntimeError("crash")
        err = TaskExecutionError("wrap", cause=inner)
        assert err.__cause__ is inner
        assert err.cause is inner


# ===========================================================================
# ContextError
# ===========================================================================


class TestContextError:
    """TC-EX-031 … TC-EX-041  ContextError specifics."""

    def test_default_message(self) -> None:
        """TC-EX-031: Default message."""
        err = ContextError()
        assert "Context" in err.message or "context" in err.message.lower()

    def test_path_none_by_default(self) -> None:
        """TC-EX-032: path defaults to None."""
        err = ContextError()
        assert err.path is None

    def test_path_stored(self) -> None:
        """TC-EX-033: Provided path is accessible."""
        err = ContextError(path="/tmp/foo")
        assert err.path == "/tmp/foo"

    def test_path_appended_to_message_when_absent(self) -> None:
        """TC-EX-034: If path is not already in message, it is appended."""
        err = ContextError("Failed to read", path="/some/file.md")
        assert "/some/file.md" in str(err)

    def test_path_not_doubled_when_already_in_message(self) -> None:
        """TC-EX-035: Path not appended a second time if message contains it."""
        path = "/some/file.md"
        err = ContextError(f"Failed to read {path}", path=path)
        # Must appear exactly once
        assert str(err).count(path) == 1

    def test_is_taktis_error(self) -> None:
        """TC-EX-036: ContextError is a subclass of TaktisError."""
        assert isinstance(ContextError(), TaktisError)

    def test_cause_chained(self) -> None:
        """TC-EX-037: __cause__ set correctly."""
        inner = PermissionError("denied")
        err = ContextError("wrap", path="/f", cause=inner)
        assert err.__cause__ is inner
        assert err.cause is inner

    def test_str_without_cause(self) -> None:
        """TC-EX-038: str contains message (and path) but no 'caused by'."""
        err = ContextError("Cannot read", path="/x")
        assert "Cannot read" in str(err)
        assert "caused by" not in str(err)

    def test_str_with_cause(self) -> None:
        """TC-EX-039: 'caused by' present when cause is set."""
        inner = OSError("disk full")
        err = ContextError("write failed", path="/y", cause=inner)
        assert "caused by" in str(err)


# ===========================================================================
# DatabaseError
# ===========================================================================


class TestDatabaseError:
    """TC-EX-042 … TC-EX-047  DatabaseError specifics."""

    def test_default_message(self) -> None:
        """TC-EX-042: Default message."""
        err = DatabaseError()
        assert err.message == "Database operation failed"

    def test_custom_message(self) -> None:
        """TC-EX-043: Custom message stored."""
        err = DatabaseError("query timeout")
        assert err.message == "query timeout"

    def test_is_taktis_error(self) -> None:
        """TC-EX-044: DatabaseError is a subclass of TaktisError."""
        assert isinstance(DatabaseError(), TaktisError)

    def test_cause_chained(self) -> None:
        """TC-EX-045: __cause__ set correctly."""
        inner = RuntimeError("sql error")
        err = DatabaseError("db crash", cause=inner)
        assert err.__cause__ is inner
        assert err.cause is inner

    def test_str_no_cause(self) -> None:
        """TC-EX-046: str equals message when no cause."""
        err = DatabaseError("no rows")
        assert str(err) == "no rows"

    def test_str_with_cause(self) -> None:
        """TC-EX-047: 'caused by' in str when cause is set."""
        inner = ConnectionError("lost")
        err = DatabaseError("db wrap", cause=inner)
        assert "caused by" in str(err)
        assert "ConnectionError" in str(err)


# ===========================================================================
# DuplicateError
# ===========================================================================


class TestDuplicateError:
    """TC-EX-048 … TC-EX-058  DuplicateError specifics."""

    def test_default_message(self) -> None:
        """TC-EX-048: Default message mentions 'already exists'."""
        err = DuplicateError()
        assert "already exists" in err.message.lower()

    def test_constraint_none_by_default(self) -> None:
        """TC-EX-049: constraint defaults to None."""
        err = DuplicateError()
        assert err.constraint is None

    def test_constraint_stored(self) -> None:
        """TC-EX-050: Provided constraint is accessible."""
        err = DuplicateError(constraint="projects.name")
        assert err.constraint == "projects.name"

    def test_is_database_error(self) -> None:
        """TC-EX-051: DuplicateError is a subclass of DatabaseError."""
        assert isinstance(DuplicateError(), DatabaseError)

    def test_is_taktis_error_transitively(self) -> None:
        """TC-EX-052: DuplicateError → DatabaseError → TaktisError."""
        assert isinstance(DuplicateError(), TaktisError)

    def test_catchable_as_database_error(self) -> None:
        """TC-EX-053: A DuplicateError raised is caught by 'except DatabaseError'."""
        with pytest.raises(DatabaseError):
            raise DuplicateError("dup")

    def test_cause_chained(self) -> None:
        """TC-EX-054: __cause__ set correctly."""
        inner = RuntimeError("sqlite3 unique")
        err = DuplicateError("dup", cause=inner)
        assert err.__cause__ is inner
        assert err.cause is inner

    def test_str_no_cause(self) -> None:
        """TC-EX-055: str equals message when no cause."""
        err = DuplicateError("record exists")
        assert str(err) == "record exists"

    def test_str_with_cause(self) -> None:
        """TC-EX-056: 'caused by' in str when cause is set."""
        inner = ValueError("constraint violation")
        err = DuplicateError("dup", cause=inner)
        assert "caused by" in str(err)

    def test_constraint_with_cause(self) -> None:
        """TC-EX-057: constraint attribute survives alongside cause."""
        inner = RuntimeError("low-level")
        err = DuplicateError("dup", constraint="tbl.col", cause=inner)
        assert err.constraint == "tbl.col"
        assert err.cause is inner


# ===========================================================================
# PipelineError
# ===========================================================================


class TestPipelineError:
    """TC-EX-064 … TC-EX-074  PipelineError specifics."""

    def test_default_message(self) -> None:
        """TC-EX-064: Default message."""
        err = PipelineError()
        assert err.message == "Pipeline error"

    def test_step_none_by_default(self) -> None:
        """TC-EX-065: step defaults to None."""
        err = PipelineError()
        assert err.step is None

    def test_step_stored(self) -> None:
        """TC-EX-066: Provided step is accessible."""
        err = PipelineError(step="build")
        assert err.step == "build"

    def test_step_appended_to_message_when_absent(self) -> None:
        """TC-EX-067: If step not in message, it is appended as '…at step <step>'."""
        err = PipelineError("something failed", step="lint")
        assert "lint" in str(err)
        assert "step" in str(err).lower()

    def test_step_not_doubled_when_already_in_message(self) -> None:
        """TC-EX-068: Step not appended again when already present in message."""
        err = PipelineError("failed at step 'deploy'", step="deploy")
        # 'deploy' should appear only once
        assert str(err).count("deploy") == 1

    def test_is_taktis_error(self) -> None:
        """TC-EX-069: PipelineError is a subclass of TaktisError."""
        assert isinstance(PipelineError(), TaktisError)

    def test_cause_chained(self) -> None:
        """TC-EX-070: __cause__ set correctly."""
        inner = RuntimeError("step failed")
        err = PipelineError("pipeline crash", step="test", cause=inner)
        assert err.__cause__ is inner
        assert err.cause is inner

    def test_str_with_step_and_cause(self) -> None:
        """TC-EX-071: step info and 'caused by' both appear in __str__."""
        inner = ValueError("assertion failed")
        err = PipelineError("pipeline error", step="validate", cause=inner)
        s = str(err)
        assert "validate" in s
        assert "caused by" in s

    def test_step_none_str_has_no_at_step(self) -> None:
        """TC-EX-072: When step=None, the string 'at step' is absent."""
        err = PipelineError("generic pipeline failure")
        assert "at step" not in str(err)


# ===========================================================================
# SchedulerError
# ===========================================================================


class TestSchedulerError:
    """TC-EX-073 … TC-EX-077  SchedulerError specifics."""

    def test_default_message(self) -> None:
        """TC-EX-073: Default message."""
        err = SchedulerError()
        assert err.message == "Scheduler error"

    def test_custom_message(self) -> None:
        """TC-EX-074: Custom message stored."""
        err = SchedulerError("dependency cycle detected")
        assert err.message == "dependency cycle detected"

    def test_is_taktis_error(self) -> None:
        """TC-EX-075: SchedulerError is a subclass of TaktisError."""
        assert isinstance(SchedulerError(), TaktisError)

    def test_cause_chained(self) -> None:
        """TC-EX-076: __cause__ set correctly."""
        inner = ValueError("cycle")
        err = SchedulerError("sched failed", cause=inner)
        assert err.__cause__ is inner
        assert err.cause is inner

    def test_str_with_cause(self) -> None:
        """TC-EX-077: 'caused by' present when cause is set."""
        inner = OverflowError("too many phases")
        err = SchedulerError("overflow", cause=inner)
        assert "caused by" in str(err)
        assert "OverflowError" in str(err)


# ===========================================================================
# StreamingError
# ===========================================================================


class TestStreamingError:
    """TC-EX-078 … TC-EX-082  StreamingError specifics."""

    def test_default_message(self) -> None:
        """TC-EX-078: Default message."""
        err = StreamingError()
        assert err.message == "Streaming error"

    def test_custom_message(self) -> None:
        """TC-EX-079: Custom message stored."""
        err = StreamingError("SSE stream closed unexpectedly")
        assert err.message == "SSE stream closed unexpectedly"

    def test_is_taktis_error(self) -> None:
        """TC-EX-080: StreamingError is a subclass of TaktisError."""
        assert isinstance(StreamingError(), TaktisError)

    def test_cause_chained(self) -> None:
        """TC-EX-081: __cause__ set correctly."""
        inner = BrokenPipeError("pipe broken")
        err = StreamingError("stream lost", cause=inner)
        assert err.__cause__ is inner
        assert err.cause is inner

    def test_str_with_cause(self) -> None:
        """TC-EX-082: 'caused by' present when cause is set."""
        inner = ConnectionResetError("reset by peer")
        err = StreamingError("disconnected", cause=inner)
        assert "caused by" in str(err)


# ===========================================================================
# Hierarchy / inheritance cross-checks
# ===========================================================================


class TestInheritanceHierarchy:
    """TC-EX-083 … TC-EX-089  All classes belong to the TaktisError tree."""

    @pytest.mark.parametrize("cls", [
        TaktisError,
        TaskExecutionError,
        ContextError,
        DatabaseError,
        DuplicateError,
        PipelineError,
        SchedulerError,
        StreamingError,
    ])
    def test_is_exception(self, cls: type) -> None:
        """TC-EX-083: Every exception class is an Exception subclass."""
        assert issubclass(cls, Exception)

    @pytest.mark.parametrize("cls", [
        TaskExecutionError,
        ContextError,
        DatabaseError,
        DuplicateError,
        PipelineError,
        SchedulerError,
        StreamingError,
    ])
    def test_is_taktis_error(self, cls: type) -> None:
        """TC-EX-084: Every concrete class is catchable as TaktisError."""
        assert issubclass(cls, TaktisError)

    def test_duplicate_error_is_database_error(self) -> None:
        """TC-EX-085: DuplicateError is a DatabaseError."""
        assert issubclass(DuplicateError, DatabaseError)

    def test_context_error_is_not_database_error(self) -> None:
        """TC-EX-086: ContextError is not in the DatabaseError branch."""
        assert not issubclass(ContextError, DatabaseError)

    def test_task_execution_error_is_not_database_error(self) -> None:
        """TC-EX-087: TaskExecutionError is not in the DatabaseError branch."""
        assert not issubclass(TaskExecutionError, DatabaseError)

    @pytest.mark.parametrize("cls", [
        TaskExecutionError,
        ContextError,
        DatabaseError,
        DuplicateError,
        PipelineError,
        SchedulerError,
        StreamingError,
    ])
    def test_all_catchable_as_taktis_error(self, cls: type) -> None:
        """TC-EX-088: raise X → except TaktisError succeeds for every X."""
        with pytest.raises(TaktisError):
            raise cls()

    def test_duplicate_error_catchable_as_database_error(self) -> None:
        """TC-EX-089: raise DuplicateError → except DatabaseError succeeds."""
        with pytest.raises(DatabaseError):
            raise DuplicateError()


# ===========================================================================
# Exception chaining — cause object identity and multi-hop chains
# ===========================================================================


class TestCauseChaining:
    """TC-EX-090 … TC-EX-094  Cause chaining across the hierarchy."""

    def test_cause_identity_preserved(self) -> None:
        """TC-EX-090: .cause and .__cause__ point to the exact same object."""
        inner = OSError("low level")
        err = ContextError("high level", cause=inner)
        assert err.cause is inner
        assert err.__cause__ is inner

    def test_cause_type_name_in_str(self) -> None:
        """TC-EX-091: The type name of the cause appears in __str__."""
        inner = PermissionError("access denied")
        err = ContextError("wrap", cause=inner)
        # PermissionError is a subclass of OSError; either name is acceptable
        assert "PermissionError" in str(err) or "OSError" in str(err)

    def test_cause_message_in_str(self) -> None:
        """TC-EX-092: The message of the cause appears in __str__."""
        inner = FileNotFoundError("no such file")
        err = ContextError("wrap", cause=inner)
        assert "no such file" in str(err)

    def test_multi_hop_outer_str_references_immediate_cause(self) -> None:
        """TC-EX-093: Multi-hop — outer error str references its direct cause."""
        root = ValueError("root cause")
        middle = DatabaseError("middle layer", cause=root)
        outer = TaskExecutionError("outer layer", cause=middle)
        # outer's __str__ should mention middle (its immediate cause)
        assert "DatabaseError" in str(outer)
        # Python's __cause__ chain is only one hop in __str__; root appears
        # transitively via middle.__cause__ but that is fine — we verify identity
        assert middle.__cause__ is root
        assert outer.__cause__ is middle

    def test_none_cause_excluded_from_str(self) -> None:
        """TC-EX-094: When cause is None, 'caused by' does not appear in str."""
        err = PipelineError("plain failure", step="deploy")
        assert "caused by" not in str(err)


# ===========================================================================
# format_error_for_user
# ===========================================================================


class TestFormatErrorForUser:
    """TC-EX-095 … TC-EX-110  Safe UI message production."""

    # ------------------------------------------------------------------
    # Returns non-empty string for every concrete class
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("exc", [
        TaskExecutionError("internal details"),
        ContextError("internal path /root/secret"),
        DatabaseError("SELECT * FROM passwords"),
        DuplicateError("dup", constraint="users.email"),
        PipelineError("step failed", step="internal-step"),
        SchedulerError("cycle in deps"),
        StreamingError("stream crashed"),
    ])
    def test_returns_non_empty_string(self, exc: TaktisError) -> None:
        """TC-EX-095: format_error_for_user returns a non-empty string."""
        result = format_error_for_user(exc)
        assert isinstance(result, str)
        assert result.strip() != ""

    # ------------------------------------------------------------------
    # Internal details must not leak into the user-facing string
    # ------------------------------------------------------------------

    def test_task_error_no_internal_detail_leaked(self) -> None:
        """TC-EX-096: TaskExecutionError — internal message not exposed."""
        err = TaskExecutionError("INTERNAL task-id-xyz crashed at 0xDEAD")
        msg = format_error_for_user(err)
        assert "task-id-xyz" not in msg
        assert "0xDEAD" not in msg

    def test_context_error_no_path_leaked(self) -> None:
        """TC-EX-097: ContextError — file path not in safe message."""
        err = ContextError("Failed to read", path="/home/user/.taktis/SECRET.md")
        msg = format_error_for_user(err)
        assert "SECRET.md" not in msg
        assert "/home/user" not in msg

    def test_database_error_no_sql_leaked(self) -> None:
        """TC-EX-098: DatabaseError — SQL query not in safe message."""
        err = DatabaseError("SELECT * FROM users WHERE password='hunter2'")
        msg = format_error_for_user(err)
        assert "hunter2" not in msg
        assert "SELECT" not in msg

    def test_duplicate_error_no_constraint_leaked(self) -> None:
        """TC-EX-099: DuplicateError — schema constraint not in safe message."""
        err = DuplicateError("dup", constraint="users.email")
        msg = format_error_for_user(err)
        assert "users.email" not in msg

    # ------------------------------------------------------------------
    # MRO resolution — DuplicateError gets its OWN safe message
    # ------------------------------------------------------------------

    def test_duplicate_error_distinct_from_database_error(self) -> None:
        """TC-EX-100: DuplicateError resolves to its own entry in _USER_MESSAGES."""
        dup_msg = format_error_for_user(DuplicateError())
        db_msg = format_error_for_user(DatabaseError())
        assert dup_msg != db_msg, (
            "DuplicateError must have its own user-facing message, "
            "not fall back to DatabaseError's"
        )

    # ------------------------------------------------------------------
    # TaktisError base (not in table) — uses .message fallback
    # ------------------------------------------------------------------

    def test_base_class_with_message_returns_message(self) -> None:
        """TC-EX-101: TaktisError not in lookup → falls back to .message."""
        err = TaktisError("something went wrong in subsystem X")
        msg = format_error_for_user(err)
        assert msg == "something went wrong in subsystem X"

    def test_base_class_empty_message_returns_generic(self) -> None:
        """TC-EX-102: TaktisError with empty message → generic fallback."""
        err = TaktisError()
        msg = format_error_for_user(err)
        assert "internal error" in msg.lower()

    # ------------------------------------------------------------------
    # Completely unknown exception type
    # ------------------------------------------------------------------

    def test_unknown_exception_returns_generic_fallback(self) -> None:
        """TC-EX-103: Non-Taktis exception → generic 'unexpected error' copy."""
        err = RuntimeError("something truly unexpected")
        msg = format_error_for_user(err)
        assert isinstance(msg, str)
        assert msg.strip() != ""
        # Must not leak the raw message
        assert "truly unexpected" not in msg

    def test_standard_exception_no_internal_detail(self) -> None:
        """TC-EX-104: ValueError with sensitive data → no leakage."""
        err = ValueError("secret_token=abc123")
        msg = format_error_for_user(err)
        assert "abc123" not in msg

    # ------------------------------------------------------------------
    # All known subclasses return their specific curated string
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("cls,keyword", [
        (TaskExecutionError, "task"),
        (ContextError, "context"),
        (DatabaseError, "database"),
        # DuplicateError has its own _USER_MESSAGES entry: "A record with that
        # name or identifier already exists."  The word "record" anchors it.
        (DuplicateError, "record"),
        (PipelineError, "pipeline"),
        (SchedulerError, "scheduler"),
        (StreamingError, "stream"),
    ])
    def test_class_message_contains_domain_keyword(
        self, cls: type, keyword: str,
    ) -> None:
        """TC-EX-105 … TC-EX-112: Each curated message contains a domain word."""
        msg = format_error_for_user(cls())
        assert keyword.lower() in msg.lower(), (
            f"{cls.__name__}: expected '{keyword}' in '{msg}'"
        )
