"""Unified exception hierarchy for Taktis.

Every component raises a subclass of :class:`TaktisError` so that
callers can catch at the granularity they need — a single ``except
TaktisError`` for "anything went wrong", or a specific subclass
for targeted recovery.

Each exception carries:
- ``message`` – a human-readable description of what failed.
- ``cause``   – the original exception (if any) that triggered this one,
                attached via standard ``__cause__`` chaining.
"""

from __future__ import annotations


class TaktisError(Exception):
    """Base class for all Taktis exceptions.

    Parameters
    ----------
    message:
        A concise, human-readable description of the failure.
    cause:
        The original lower-level exception, if any.  Automatically
        set as ``__cause__`` for standard Python exception chaining.
    """

    def __init__(self, message: str = "", *, cause: BaseException | None = None) -> None:
        self.message = message
        self.cause = cause
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        base = self.message or self.__class__.__name__
        if self.cause is not None:
            return f"{base} (caused by {type(self.cause).__name__}: {self.cause})"
        return base


class TaskExecutionError(TaktisError):
    """A task failed during execution (process crash, timeout, bad exit code)."""

    def __init__(
        self,
        message: str = "Task execution failed",
        *,
        task_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.task_id = task_id
        super().__init__(message, cause=cause)

    def __str__(self) -> str:
        prefix = f"[task {self.task_id}] " if self.task_id else ""
        base = f"{prefix}{self.message}"
        if self.cause is not None:
            return f"{base} (caused by {type(self.cause).__name__}: {self.cause})"
        return base


class ContextError(TaktisError):
    """A failure in the context-file subsystem (read/write to .taktis/)."""

    def __init__(
        self,
        message: str = "Context file operation failed",
        *,
        path: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.path = path
        if path and path not in message:
            message = f"{message}: {path}"
        super().__init__(message, cause=cause)


class DatabaseError(TaktisError):
    """A database operation failed (query, migration, connection)."""

    def __init__(
        self,
        message: str = "Database operation failed",
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)


class DuplicateError(DatabaseError):
    """A UNIQUE constraint violation was detected in the database.

    Raised when an INSERT or UPSERT would create a duplicate value in a
    column with a UNIQUE constraint (e.g. project name, expert name).
    The ``constraint`` attribute carries the ``table.column`` string
    extracted from the SQLite error message; it contains schema
    information only — never user-supplied values.
    """

    def __init__(
        self,
        message: str = "A record with that value already exists",
        *,
        constraint: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.constraint = constraint
        super().__init__(message, cause=cause)


class PipelineError(TaktisError):
    """A pipeline step failed or the pipeline entered an inconsistent state."""

    def __init__(
        self,
        message: str = "Pipeline error",
        *,
        step: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.step = step
        if step and step not in message:
            message = f"{message} at step '{step}'"
        super().__init__(message, cause=cause)


class SchedulerError(TaktisError):
    """The wave scheduler encountered a problem (dependency cycle, invalid state)."""

    def __init__(
        self,
        message: str = "Scheduler error",
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)


class StreamingError(TaktisError):
    """An error in the async streaming loop (SDK process output, SSE)."""

    def __init__(
        self,
        message: str = "Streaming error",
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)


class ConsultError(TaktisError):
    """Consult session failure."""

    def __init__(
        self,
        message: str = "Consult session error",
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)


# ---------------------------------------------------------------------------
# Helper: safe user-facing formatting
# ---------------------------------------------------------------------------

#: Maps exception types to short, non-technical summaries.
_USER_MESSAGES: dict[type, str] = {
    TaskExecutionError: "A task failed to complete. Check the task log for details.",
    ContextError: "Could not read or write project context files.",
    DuplicateError: "A record with that name or identifier already exists.",
    DatabaseError: "A database error occurred. The operation was not saved.",
    PipelineError: "A pipeline step failed. Review the pipeline status.",
    SchedulerError: "The scheduler encountered a problem. Check phase configuration.",
    StreamingError: "Lost connection to the task output stream.",
    ConsultError: "The advisor chat session encountered an error.",
}


def format_error_for_user(exc: BaseException) -> str:
    """Return a short, non-technical summary safe to show in any UI.

    For :class:`TaktisError` subclasses the message is drawn from a
    curated lookup table so that internal details (tracebacks, SQL, paths)
    are never leaked.  Unknown exceptions produce a generic fallback.

    Parameters
    ----------
    exc:
        Any exception instance.

    Returns
    -------
    str
        A single-line, end-user-safe description.
    """
    for cls in type(exc).__mro__:
        if cls in _USER_MESSAGES:
            return _USER_MESSAGES[cls]
    if isinstance(exc, TaktisError):
        # Fallback for TaktisError itself or future subclasses not
        # yet added to the lookup table.
        return exc.message or "An internal error occurred."
    return "An unexpected error occurred. Please try again or check the logs."
