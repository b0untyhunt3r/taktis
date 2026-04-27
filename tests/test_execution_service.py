"""Direct unit tests for ExecutionService.

Covers the most critical methods:
- _make_task_done_callback (via shared factory)
- start_task (status transitions, validation)
- stop_task (validation, status update)
- continue_task (validation, status checks)
- _handle_pipeline_task_complete (discuss_task/task_researcher routing)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.events import EventBus, EVENT_SYSTEM_INTERRUPTED_WORK
from taktis.core.execution_service import ExecutionService
from taktis.core.manager import ProcessManager
from taktis.core.scheduler import WaveScheduler
from taktis.exceptions import TaskExecutionError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_service(
    *,
    db_rows: dict | None = None,
) -> tuple[ExecutionService, MagicMock]:
    """Build an ExecutionService with mocked dependencies.

    Returns (service, session_factory_mock) so tests can configure DB returns.
    """
    event_bus = EventBus()
    manager = MagicMock(spec=ProcessManager)
    manager._event_bus = event_bus
    scheduler = MagicMock(spec=WaveScheduler)
    project_service = MagicMock()

    # Build an async context manager mock for the session factory
    conn_mock = AsyncMock()
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_ctx)

    svc = ExecutionService(
        process_manager=manager,
        scheduler=scheduler,
        event_bus=event_bus,
        db_session_factory=session_factory,
        project_service=project_service,
    )

    return svc, conn_mock


# ---------------------------------------------------------------------------
# _make_task_done_callback
# ---------------------------------------------------------------------------

class TestMakeTaskDoneCallback:
    """Test the done_callback helper (delegates to shared make_done_callback)."""

    def test_cancelled_task_is_noop(self) -> None:
        svc, _ = _make_service()
        cb = svc._make_task_done_callback("test-task")
        task = MagicMock()
        task.cancelled.return_value = True
        cb(task)  # should not raise

    def test_successful_task_is_noop(self) -> None:
        svc, _ = _make_service()
        cb = svc._make_task_done_callback("test-task")
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None
        cb(task)  # should not raise

    @pytest.mark.asyncio
    async def test_failed_task_publishes_event(self) -> None:
        svc, _ = _make_service()
        queue = svc._event_bus.subscribe(EVENT_SYSTEM_INTERRUPTED_WORK)

        cb = svc._make_task_done_callback("test-task", {"task_id": "abc"})
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("boom")
        cb(task)
        await asyncio.sleep(0)

        assert not queue.empty()
        envelope = queue.get_nowait()
        assert "crash" in envelope["data"]["reason"]
        assert envelope["data"]["task_id"] == "abc"


# ---------------------------------------------------------------------------
# start_task
# ---------------------------------------------------------------------------

class TestStartTask:
    """Tests for ExecutionService.start_task validation paths."""

    @pytest.mark.asyncio
    async def test_start_task_not_found_raises(self) -> None:
        svc, conn = _make_service()

        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_task = AsyncMock(return_value=None)
            with pytest.raises(TaskExecutionError, match="not found"):
                await svc.start_task("nonexistent")

    @pytest.mark.asyncio
    async def test_start_task_wrong_status_raises(self) -> None:
        svc, conn = _make_service()

        task = {"id": "abc", "status": "running", "project_id": "p1"}
        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_task = AsyncMock(return_value=task)
            with pytest.raises(TaskExecutionError, match="cannot start"):
                await svc.start_task("abc")

    @pytest.mark.asyncio
    async def test_start_task_valid_updates_status(self) -> None:
        svc, conn = _make_service()

        task = {
            "id": "abc", "status": "pending", "project_id": "p1",
            "phase_id": "ph1",
        }
        project = {"id": "p1", "name": "test", "working_dir": "/tmp/test"}

        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_task = AsyncMock(return_value=task)
            mock_repo.update_task = AsyncMock()
            mock_repo.delete_task_outputs = AsyncMock()
            mock_repo.get_project_by_id = AsyncMock(return_value=project)
            svc._project_service._enrich_project = AsyncMock(return_value=project)
            svc._scheduler.execute_task = AsyncMock()

            await svc.start_task("abc")

            # Verify status was set to running
            mock_repo.update_task.assert_called_once()
            call_kwargs = mock_repo.update_task.call_args
            assert call_kwargs[1].get("status") == "running" or (
                len(call_kwargs[0]) > 2 and call_kwargs[0][2] == "running"
            ) or "running" in str(call_kwargs)


# ---------------------------------------------------------------------------
# stop_task
# ---------------------------------------------------------------------------

class TestStopTask:
    """Tests for ExecutionService.stop_task validation paths."""

    @pytest.mark.asyncio
    async def test_stop_not_running_raises(self) -> None:
        svc, conn = _make_service()
        svc._manager.get_process.return_value = None

        with pytest.raises(TaskExecutionError, match="not running"):
            await svc.stop_task("abc")

    @pytest.mark.asyncio
    async def test_stop_running_task_cancels(self) -> None:
        svc, conn = _make_service()

        process = MagicMock()
        process.is_running = True
        svc._manager.get_process.return_value = process
        svc._manager.stop_task = AsyncMock()

        task = {"id": "abc", "status": "running", "project_id": "p1"}
        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_task = AsyncMock(return_value=task)
            mock_repo.update_task = AsyncMock()

            await svc.stop_task("abc")

            svc._manager.stop_task.assert_called_once_with("abc")
            mock_repo.update_task.assert_called_once()


# ---------------------------------------------------------------------------
# continue_task
# ---------------------------------------------------------------------------

class TestContinueTask:
    """Tests for ExecutionService.continue_task validation paths."""

    @pytest.mark.asyncio
    async def test_continue_not_found_raises(self) -> None:
        svc, conn = _make_service()

        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_task = AsyncMock(return_value=None)
            with pytest.raises(TaskExecutionError, match="not found"):
                await svc.continue_task("nonexistent", "hello")

    @pytest.mark.asyncio
    async def test_continue_wrong_status_raises(self) -> None:
        svc, conn = _make_service()

        task = {"id": "abc", "status": "pending", "project_id": "p1"}
        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_task = AsyncMock(return_value=task)
            with pytest.raises(TaskExecutionError, match="cannot continue"):
                await svc.continue_task("abc", "hello")

    @pytest.mark.asyncio
    async def test_continue_no_session_raises(self) -> None:
        svc, conn = _make_service()

        task = {
            "id": "abc", "status": "completed", "project_id": "p1",
            "session_id": None,
        }
        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_task = AsyncMock(return_value=task)
            mock_repo.get_task_outputs = AsyncMock(return_value=[])
            with pytest.raises(TaskExecutionError, match="session"):
                await svc.continue_task("abc", "hello")


# ---------------------------------------------------------------------------
# resume_phase validation
# ---------------------------------------------------------------------------

class TestResumePhase:
    """Tests for resume_phase validation."""

    @pytest.mark.asyncio
    async def test_resume_phase_not_found_raises(self) -> None:
        svc, conn = _make_service()

        with patch("taktis.core.execution_service.repo") as mock_repo:
            mock_repo.get_phase_by_id = AsyncMock(return_value=None)
            with pytest.raises(ValueError, match="not found"):
                await svc.resume_phase("nonexistent")


# ---------------------------------------------------------------------------
# _handle_pipeline_task_complete — simplified (only discuss/research)
# ---------------------------------------------------------------------------

class TestHandlePipelineTaskComplete:
    """Tests for simplified _handle_pipeline_task_complete (discuss/research only)."""

    @pytest.mark.asyncio
    async def test_discuss_task_routes_to_task_prep_complete(self) -> None:
        """discuss_task type with a result routes to _handle_task_prep_complete."""
        svc, conn = _make_service()

        with patch.object(svc, "_handle_task_prep_complete", new_callable=AsyncMock) as mock_prep:
            await svc._handle_pipeline_task_complete(
                "p1", "t1", "discuss_task", "some discussion result",
            )

            mock_prep.assert_called_once_with("p1", "t1", "discuss_task", "some discussion result")

    @pytest.mark.asyncio
    async def test_task_researcher_routes_to_task_prep_complete(self) -> None:
        """task_researcher type with a result routes to _handle_task_prep_complete."""
        svc, conn = _make_service()

        with patch.object(svc, "_handle_task_prep_complete", new_callable=AsyncMock) as mock_prep:
            await svc._handle_pipeline_task_complete(
                "p1", "t2", "task_researcher", "research findings",
            )

            mock_prep.assert_called_once_with("p1", "t2", "task_researcher", "research findings")

    @pytest.mark.asyncio
    async def test_discuss_task_with_no_result_is_noop(self) -> None:
        """discuss_task with None result does NOT route to _handle_task_prep_complete."""
        svc, conn = _make_service()

        with patch.object(svc, "_handle_task_prep_complete", new_callable=AsyncMock) as mock_prep:
            await svc._handle_pipeline_task_complete(
                "p1", "t1", "discuss_task", None,
            )
            mock_prep.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrecognised_type_is_noop(self) -> None:
        """Unrecognised task types (e.g. phase_review) are silently ignored."""
        svc, conn = _make_service()

        with patch.object(svc, "_handle_task_prep_complete", new_callable=AsyncMock) as mock_prep:
            await svc._handle_pipeline_task_complete(
                "p1", "t1", "phase_review", "review passed",
            )
            mock_prep.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_researcher_with_empty_result_is_noop(self) -> None:
        """task_researcher with empty string result is not routed (falsy check)."""
        svc, conn = _make_service()

        with patch.object(svc, "_handle_task_prep_complete", new_callable=AsyncMock) as mock_prep:
            await svc._handle_pipeline_task_complete(
                "p1", "t1", "task_researcher", "",
            )
            mock_prep.assert_not_called()
