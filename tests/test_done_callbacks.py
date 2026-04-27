"""Tests for asyncio.create_task done_callbacks in ProcessManager, StateTracker,
and the continuation task in continue_task().

Covers ERR-03: background async tasks must surface crashes through logging and
EventBus publication rather than failing silently.

Also covers C-1 (Phase 4 code review): _execute_continuation's outer try/except
must catch DB/SDKProcess setup failures and the done_callback on the continuation
task must fire for any exception that escapes the outer try/except.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.events import (
    EVENT_SYSTEM_INTERRUPTED_WORK,
    EVENT_TASK_FAILED,
    EventBus,
    make_done_callback,
)
from taktis.core.manager import ProcessManager
from taktis.core.state import StateTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _failed_task(exc: Exception) -> MagicMock:
    """Return a mock Task that appears to have failed with *exc*."""
    task = MagicMock(spec=asyncio.Task)
    task.cancelled.return_value = False
    task.exception.return_value = exc
    return task


def _cancelled_task() -> MagicMock:
    """Return a mock Task that appears to have been cancelled."""
    task = MagicMock(spec=asyncio.Task)
    task.cancelled.return_value = True
    return task


def _successful_task() -> MagicMock:
    """Return a mock Task that completed without an exception."""
    task = MagicMock(spec=asyncio.Task)
    task.cancelled.return_value = False
    task.exception.return_value = None
    return task


# ---------------------------------------------------------------------------
# ProcessManager._make_monitor_done_callback
# ---------------------------------------------------------------------------


class TestMonitorDoneCallback:
    """Unit tests for the done_callback factory on ProcessManager."""

    def _make_manager(self) -> ProcessManager:
        return ProcessManager(event_bus=EventBus(), max_concurrent=2)

    # --- happy paths (no-ops) ---

    @pytest.mark.asyncio
    async def test_cancelled_task_is_noop(self) -> None:
        """Cancelled task: callback returns immediately with no side-effects."""
        manager = self._make_manager()
        cb = manager._make_monitor_done_callback("t-cancel")
        cb(_cancelled_task())
        # No subscribers should have received anything.
        assert manager._event_bus.subscriber_count(EVENT_TASK_FAILED) == 0

    @pytest.mark.asyncio
    async def test_successful_task_is_noop(self) -> None:
        """Successfully completed task: callback returns immediately."""
        manager = self._make_manager()
        cb = manager._make_monitor_done_callback("t-ok")
        cb(_successful_task())  # must not raise

    # --- error path ---

    @pytest.mark.asyncio
    async def test_crashed_task_logs_at_error_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """A crashed monitor task must log at ERROR with task_id and exc_info."""
        manager = self._make_manager()
        cb = manager._make_monitor_done_callback("t-crash-log")

        with caplog.at_level(logging.ERROR, logger="taktis.core.manager"):
            cb(_failed_task(RuntimeError("boom")))
            await asyncio.sleep(0)  # let loop.create_task run

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert any("t-crash-log" in m for m in error_msgs), (
            f"Expected task_id in error log; got: {error_msgs}"
        )

    @pytest.mark.asyncio
    async def test_crashed_task_logs_exc_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """The error log record must carry exc_info so the traceback is captured."""
        manager = self._make_manager()
        cb = manager._make_monitor_done_callback("t-crash-exc")
        exc = RuntimeError("test exception")

        with caplog.at_level(logging.ERROR, logger="taktis.core.manager"):
            cb(_failed_task(exc))
            await asyncio.sleep(0)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        # exc_info is stored as a tuple (type, value, traceback) on the record.
        assert any(
            r.exc_info is not None and r.exc_info[1] is exc
            for r in error_records
        ), "Expected exc_info to reference the original exception"

    @pytest.mark.asyncio
    async def test_crashed_task_publishes_task_failed_event(self) -> None:
        """A crashed monitor task must publish EVENT_TASK_FAILED with task_id."""
        manager = self._make_manager()
        queue = manager._event_bus.subscribe(EVENT_TASK_FAILED)
        cb = manager._make_monitor_done_callback("t-crash-event")

        cb(_failed_task(ValueError("process died")))
        await asyncio.sleep(0)  # let the scheduled publish task complete

        assert not queue.empty(), "Expected EVENT_TASK_FAILED to be published"
        envelope = queue.get_nowait()
        assert envelope["event_type"] == EVENT_TASK_FAILED
        assert envelope["data"]["task_id"] == "t-crash-event"
        assert envelope["data"]["reason"] == "monitor_crash"

    @pytest.mark.asyncio
    async def test_crashed_task_event_contains_error_string(self) -> None:
        """The EVENT_TASK_FAILED payload must include a non-empty 'error' field."""
        manager = self._make_manager()
        queue = manager._event_bus.subscribe(EVENT_TASK_FAILED)
        cb = manager._make_monitor_done_callback("t-crash-err-str")

        cb(_failed_task(RuntimeError("something bad")))
        await asyncio.sleep(0)

        envelope = queue.get_nowait()
        assert envelope["data"].get("error"), "Expected non-empty 'error' in event payload"


# ---------------------------------------------------------------------------
# StateTracker done callback via make_done_callback
# ---------------------------------------------------------------------------


class TestStateTrackerBgTaskDone:
    """Unit tests for the done_callback on StateTracker._bg_task.

    The StateTracker uses the shared :func:`make_done_callback` factory
    (events.py) with an ``on_crash`` that sets ``_running = False``.
    """

    def _make_tracker_and_cb(self):
        """Return (tracker, done_callback) for testing."""
        tracker = StateTracker(
            db_session_factory=MagicMock(),
            event_bus=EventBus(),
        )
        cb = make_done_callback(
            "state-tracker", tracker._event_bus,
            event_data={"component": "StateTracker"},
            on_crash=lambda _exc: setattr(tracker, "_running", False),
        )
        return tracker, cb

    # --- happy paths (no-ops) ---

    @pytest.mark.asyncio
    async def test_cancelled_task_is_noop(self) -> None:
        """Cancellation (normal shutdown via stop()) must not alter running flag."""
        tracker, cb = self._make_tracker_and_cb()
        tracker._running = True
        cb(_cancelled_task())
        assert tracker._running is True, "Cancellation must not set _running = False"

    @pytest.mark.asyncio
    async def test_successful_task_is_noop(self) -> None:
        """A cleanly finished task (running flag already False) produces no side-effects."""
        tracker, cb = self._make_tracker_and_cb()
        tracker._running = False
        cb(_successful_task())  # must not raise

    # --- error path ---

    @pytest.mark.asyncio
    async def test_crashed_task_sets_running_false(self) -> None:
        """A crashed background task must set _running = False."""
        tracker, cb = self._make_tracker_and_cb()
        tracker._running = True
        cb(_failed_task(RuntimeError("event loop died")))
        assert tracker._running is False

    @pytest.mark.asyncio
    async def test_crashed_task_logs_at_error_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """A crashed background task must log at ERROR level."""
        tracker, cb = self._make_tracker_and_cb()
        tracker._running = True

        with caplog.at_level(logging.ERROR, logger="taktis.core.events"):
            cb(_failed_task(RuntimeError("loop died")))
            await asyncio.sleep(0)

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert any("state-tracker" in m for m in error_msgs), (
            f"Expected 'state-tracker' in error log; got: {error_msgs}"
        )

    @pytest.mark.asyncio
    async def test_crashed_task_logs_exc_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """The ERROR record must carry exc_info for full traceback capture."""
        tracker, cb = self._make_tracker_and_cb()
        tracker._running = True
        exc = RuntimeError("traceback test")

        with caplog.at_level(logging.ERROR, logger="taktis.core.events"):
            cb(_failed_task(exc))
            await asyncio.sleep(0)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        assert any(
            r.exc_info is not None and r.exc_info[1] is exc
            for r in error_records
        ), "Expected exc_info to reference the original exception"

    @pytest.mark.asyncio
    async def test_crashed_task_publishes_system_interrupted_work(self) -> None:
        """A crashed background task must publish EVENT_SYSTEM_INTERRUPTED_WORK."""
        tracker, cb = self._make_tracker_and_cb()
        tracker._running = True
        queue = tracker._event_bus.subscribe(EVENT_SYSTEM_INTERRUPTED_WORK)

        cb(_failed_task(RuntimeError("loop died")))
        await asyncio.sleep(0)

        assert not queue.empty(), "Expected EVENT_SYSTEM_INTERRUPTED_WORK to be published"
        envelope = queue.get_nowait()
        assert envelope["event_type"] == EVENT_SYSTEM_INTERRUPTED_WORK
        assert envelope["data"]["component"] == "StateTracker"

    @pytest.mark.asyncio
    async def test_crashed_task_event_contains_error_string(self) -> None:
        """EVENT_SYSTEM_INTERRUPTED_WORK payload must include a non-empty 'error' field."""
        tracker, cb = self._make_tracker_and_cb()
        tracker._running = True
        queue = tracker._event_bus.subscribe(EVENT_SYSTEM_INTERRUPTED_WORK)

        cb(_failed_task(RuntimeError("descriptive error")))
        await asyncio.sleep(0)

        envelope = queue.get_nowait()
        assert envelope["data"].get("error"), "Expected non-empty 'error' in event payload"


# ---------------------------------------------------------------------------
# Taktis.continue_task — C-1: outer try/except and done_callback
# ---------------------------------------------------------------------------


def _make_session_ctx(mock_session: MagicMock) -> AsyncMock:
    """Wire mock_session to behave as an async context manager."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.return_value = cm
    return cm


class TestContinuationTaskDoneCallback:
    """Tests for C-1: _execute_continuation outer try/except and done_callback.

    Verifies that crashes occurring *before* the inner try block (DB failures,
    SDKProcess instantiation errors) are handled correctly: the task is marked
    'failed' in the DB and EVENT_TASK_FAILED is published.

    Also verifies that the done_callback on the asyncio.Task created by
    continue_task() fires correctly when _execute_continuation raises an
    unexpected exception that escapes even the outer try/except.
    """

    # ------------------------------------------------------------------
    # Outer try/except: DB failure in setup (get_task raises)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_setup_db_failure_publishes_task_failed(self) -> None:
        """DatabaseError from get_task() must publish EVENT_TASK_FAILED."""
        from taktis.core.events import EVENT_TASK_FAILED
        from taktis.core.execution_service import ExecutionService
        from taktis.exceptions import DatabaseError

        bus = EventBus()
        queue = bus.subscribe(EVENT_TASK_FAILED)

        orch = MagicMock()
        orch._event_bus = bus
        orch._manager = MagicMock()

        _make_session_ctx(orch._session_factory)

        with (
            patch("taktis.core.execution_service.repo") as mock_repo,
        ):
            mock_repo.get_task = AsyncMock(
                side_effect=DatabaseError("Connection refused")
            )
            mock_repo.update_task = AsyncMock()

            await ExecutionService._execute_continuation(
                orch,
                task_id="t-setup-fail",
                message="hello",
                session_id="sess-1",
                project={"working_dir": ".", "default_model": "sonnet", "id": "proj-1"},
            )

        assert not queue.empty(), "Expected EVENT_TASK_FAILED after setup DB failure"
        envelope = queue.get_nowait()
        assert envelope["event_type"] == EVENT_TASK_FAILED
        assert envelope["data"]["task_id"] == "t-setup-fail"
        assert envelope["data"]["reason"] == "continuation_setup_failed"

    @pytest.mark.asyncio
    async def test_setup_db_failure_logs_at_error_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DatabaseError from get_task() must produce an ERROR log with exc_info."""
        from taktis.core.execution_service import ExecutionService
        from taktis.exceptions import DatabaseError

        bus = EventBus()
        orch = MagicMock()
        orch._event_bus = bus
        orch._manager = MagicMock()

        db_exc = DatabaseError("Connection refused")
        _make_session_ctx(orch._session_factory)

        with (
            patch("taktis.core.execution_service.repo") as mock_repo,
            caplog.at_level(logging.ERROR, logger="taktis.core.execution_service"),
        ):
            mock_repo.get_task = AsyncMock(side_effect=db_exc)
            mock_repo.update_task = AsyncMock()

            await ExecutionService._execute_continuation(
                orch,
                task_id="t-setup-log",
                message="hello",
                session_id="sess-1",
                project={"working_dir": ".", "default_model": "sonnet", "id": "proj-1"},
            )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        assert any(
            "t-setup-log" in r.message for r in error_records
        ), f"Expected task_id in error log; got: {[r.message for r in error_records]}"
        assert any(
            r.exc_info is not None for r in error_records
        ), "Expected exc_info to be set on the ERROR record"

    @pytest.mark.asyncio
    async def test_setup_db_failure_marks_task_failed_in_db(self) -> None:
        """DatabaseError from get_task() must call update_task with status='failed'."""
        from taktis.core.execution_service import ExecutionService
        from taktis.exceptions import DatabaseError

        bus = EventBus()
        orch = MagicMock()
        orch._event_bus = bus
        orch._manager = MagicMock()

        update_mock = AsyncMock()

        _make_session_ctx(orch._session_factory)

        with (
            patch("taktis.core.execution_service.repo") as mock_repo,
        ):
            mock_repo.get_task = AsyncMock(side_effect=DatabaseError("db down"))
            mock_repo.update_task = update_mock

            await ExecutionService._execute_continuation(
                orch,
                task_id="t-setup-db-update",
                message="hello",
                session_id="sess-1",
                project={"working_dir": ".", "default_model": "sonnet", "id": "proj-1"},
            )

        assert update_mock.called, "Expected repo.update_task to be called"
        _args, kwargs = update_mock.call_args
        assert kwargs.get("status") == "failed", (
            f"Expected status='failed'; got kwargs={kwargs}"
        )

    # ------------------------------------------------------------------
    # done_callback: last-resort handler fires when task crashes
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_done_callback_fires_on_continuation_crash(self) -> None:
        """If _execute_continuation raises despite its try/except, the done_callback
        must log at ERROR and publish EVENT_TASK_FAILED."""
        from taktis.core.events import EVENT_TASK_FAILED
        from taktis.core.execution_service import ExecutionService

        bus = EventBus()
        queue = bus.subscribe(EVENT_TASK_FAILED)

        orch = MagicMock()
        orch._event_bus = bus

        orch._scheduler = MagicMock()
        # get_process returns None → skip stale-process cleanup branch
        orch._manager = MagicMock()
        orch._manager.get_process = MagicMock(return_value=None)

        # Force _execute_continuation to always raise, bypassing its own try/except
        async def _always_raise(task_id: str, message: str, session_id: str, project: dict) -> None:
            raise RuntimeError("totally unexpected crash")

        orch._execute_continuation = _always_raise

        _make_session_ctx(orch._session_factory)

        with (
            patch("taktis.core.execution_service.repo") as mock_repo,
        ):
            mock_repo.get_task = AsyncMock(return_value={
                "status": "completed",
                "session_id": "sess-abc",
                "project_id": "proj-1",
            })
            mock_repo.get_task_outputs = AsyncMock(return_value=[])
            mock_repo.update_task = AsyncMock()
            mock_repo.create_task_output = AsyncMock()
            mock_repo.get_project_by_id = AsyncMock(return_value={
                "id": "proj-1", "name": "test-proj", "working_dir": ".",
                "default_model": "sonnet",
                "planning_options": None, "experts": [],
            })

            orch._project_service = MagicMock()
            orch._project_service._enrich_project = AsyncMock(return_value={
                "id": "proj-1", "name": "test-proj",
                "working_dir": ".", "default_model": "sonnet",
            })
            await ExecutionService.continue_task(orch, "t-crash", "hello")

            # Let the event loop run the background task and its done_callback
            await asyncio.sleep(0.05)

        assert not queue.empty(), (
            "Expected EVENT_TASK_FAILED published by done_callback after continuation crash"
        )
        envelope = queue.get_nowait()
        assert envelope["event_type"] == EVENT_TASK_FAILED
        assert envelope["data"]["task_id"] == "t-crash"
        assert envelope["data"]["reason"] == "continuation_crash"

    @pytest.mark.asyncio
    async def test_done_callback_logs_error_on_continuation_crash(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """done_callback must log at ERROR level with exc_info when the task crashes."""
        from taktis.core.execution_service import ExecutionService

        bus = EventBus()
        orch = MagicMock()
        orch._event_bus = bus

        orch._scheduler = MagicMock()
        # get_process returns None → skip stale-process cleanup branch
        orch._manager = MagicMock()
        orch._manager.get_process = MagicMock(return_value=None)

        async def _always_raise(task_id: str, message: str, session_id: str, project: dict) -> None:
            raise RuntimeError("done_callback log test crash")

        orch._execute_continuation = _always_raise
        _make_session_ctx(orch._session_factory)

        with (
            patch("taktis.core.execution_service.repo") as mock_repo,
            caplog.at_level(logging.ERROR, logger="taktis.core.execution_service"),
        ):
            mock_repo.get_task = AsyncMock(return_value={
                "status": "completed",
                "session_id": "sess-xyz",
                "project_id": "proj-2",
            })
            mock_repo.get_task_outputs = AsyncMock(return_value=[])
            mock_repo.update_task = AsyncMock()
            mock_repo.create_task_output = AsyncMock()
            mock_repo.get_project_by_id = AsyncMock(return_value={
                "id": "proj-2", "name": "proj2", "working_dir": ".",
                "default_model": "sonnet",
                "planning_options": None, "experts": [],
            })

            orch._project_service = MagicMock()
            orch._project_service._enrich_project = AsyncMock(return_value={
                "id": "proj-2", "name": "proj2",
                "working_dir": ".", "default_model": "sonnet",
            })
            await ExecutionService.continue_task(orch, "t-crash-log", "hi")

            await asyncio.sleep(0.05)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record from done_callback"
        assert any(
            "t-crash-log" in r.message for r in error_records
        ), f"Expected task_id in error log; got: {[r.message for r in error_records]}"
        assert any(
            r.exc_info is not None for r in error_records
        ), "Expected exc_info on the done_callback ERROR record"
