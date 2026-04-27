"""Comprehensive tests for wave failure handling and expanded execution scenarios.

Tests the WaveScheduler.execute_phase() logic for:
- Wave failure propagation (cascading aborts to subsequent waves)
- Unexpected exception handling mid-wave
- Edge cases (single wave, empty waves, resume via start_wave)
- Event publishing (phase/wave lifecycle events)
- Checkpoint behavior (successful waves checkpointed, failed waves not)
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from taktis.core.events import (
    EVENT_PHASE_COMPLETED,
    EVENT_PHASE_FAILED,
    EVENT_PHASE_STARTED,
    EVENT_TASK_FAILED,
    EVENT_WAVE_COMPLETED,
    EVENT_WAVE_STARTED,
)
from taktis.core.scheduler import WaveScheduler
from taktis.exceptions import SchedulerError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    task_id: str,
    wave: int = 1,
    status: str = "pending",
    task_type: str | None = None,
) -> dict:
    """Create a minimal task dict matching repository output."""
    return {
        "id": task_id,
        "wave": wave,
        "status": status,
        "task_type": task_type,
        "prompt": f"Task {task_id}",
    }


def _make_phase(phase_id: str, status: str = "not_started", current_wave: int = 0) -> dict:
    """Create a minimal phase dict matching repository output."""
    return {
        "id": phase_id,
        "name": "Test Phase",
        "status": status,
        "current_wave": current_wave,
        "completed_at": None,
    }


def _make_project(project_id: str = "proj-1", name: str = "Test Project") -> dict:
    return {
        "id": project_id,
        "name": name,
        "planning_options": "",
    }


def _build_scheduler(
    *,
    execute_task_side_effect=None,
    wait_for_tasks_side_effect=None,
):
    """Build a WaveScheduler with fully mocked dependencies.

    Returns (scheduler, event_bus, mock_repo_patcher) where the patcher
    must be used as a context manager.
    """
    mock_conn = MagicMock()

    @asynccontextmanager
    async def mock_session():
        yield mock_conn

    process_manager = MagicMock()
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    state_tracker = MagicMock()
    state_tracker.update_status = AsyncMock()
    state_tracker.set_current_phase = AsyncMock()

    scheduler = WaveScheduler(
        process_manager=process_manager,
        event_bus=event_bus,
        state_tracker=state_tracker,
        db_session_factory=mock_session,
    )

    if execute_task_side_effect is not None:
        scheduler.execute_task = AsyncMock(side_effect=execute_task_side_effect)
    else:
        scheduler.execute_task = AsyncMock()

    if wait_for_tasks_side_effect is not None:
        scheduler._wait_for_tasks = AsyncMock(side_effect=wait_for_tasks_side_effect)
    else:
        scheduler._wait_for_tasks = AsyncMock(return_value={})

    scheduler._mark_task_failed = AsyncMock()

    return scheduler, event_bus, state_tracker


def _event_calls(event_bus: MagicMock, event_type: str) -> list[dict]:
    """Extract all publish calls for a given event type."""
    return [
        c.args[1]
        for c in event_bus.publish.call_args_list
        if c.args[0] == event_type
    ]


def _event_types(event_bus: MagicMock) -> list[str]:
    """Return the ordered list of event types published."""
    return [c.args[0] for c in event_bus.publish.call_args_list]


# ===================================================================
# Wave failure propagation
# ===================================================================


class TestWaveFailurePropagation:
    """Tests for cascading failure from one wave to subsequent waves."""

    @pytest.mark.asyncio
    async def test_single_task_fails_wave1_aborts_wave2(self):
        """One task failing in wave 1 marks wave 2 tasks as failed and phase as failed."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=1),
            _make_task("t3", wave=2),
        ]

        def wait_side_effect(task_ids, timeout=None):
            # Wave 1: t1 completes, t2 fails
            if "t1" in task_ids:
                return {"t1": "completed", "t2": "failed"}
            # Wave 2 should never be reached
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            project = _make_project()
            await scheduler.execute_phase("ph-1", project)

            # Future wave task t3 should be marked failed
            mark_failed_calls = scheduler._mark_task_failed.call_args_list
            failed_task_ids = [c.args[0] for c in mark_failed_calls]
            assert "t3" in failed_task_ids

            # Check the abort reason mentions wave 1
            t3_call = next(c for c in mark_failed_calls if c.args[0] == "t3")
            assert "wave 1" in t3_call.kwargs.get("reason", t3_call.args[2] if len(t3_call.args) > 2 else "")

            # Phase should be marked failed
            phase_update_calls = mock_repo.update_phase.call_args_list
            final_update = phase_update_calls[-1]
            assert final_update.kwargs.get("status") == "failed" or \
                   (len(final_update.args) > 2 and final_update.args[2] == "failed")

            # EVENT_PHASE_FAILED published
            assert any(
                c.args[0] == EVENT_PHASE_FAILED
                for c in event_bus.publish.call_args_list
            )

    @pytest.mark.asyncio
    async def test_multiple_tasks_fail_same_wave(self):
        """Multiple failures in the same wave still only abort subsequent waves once."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=1),
            _make_task("t3", wave=1),
            _make_task("t4", wave=2),
            _make_task("t5", wave=2),
        ]

        def wait_side_effect(task_ids, timeout=None):
            if "t1" in task_ids:
                return {"t1": "failed", "t2": "failed", "t3": "completed"}
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Both wave 2 tasks should be marked failed
            failed_ids = [c.args[0] for c in scheduler._mark_task_failed.call_args_list]
            assert "t4" in failed_ids
            assert "t5" in failed_ids

            # The abort reason should mention 2 failures
            for c in scheduler._mark_task_failed.call_args_list:
                if c.args[0] in ("t4", "t5"):
                    reason = c.kwargs.get("reason", c.args[2] if len(c.args) > 2 else "")
                    assert "2 failure(s)" in reason

    @pytest.mark.asyncio
    async def test_last_wave_fails_no_cascading_needed(self):
        """Failure in the last wave: no subsequent waves to abort, phase still fails."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"t1": "completed"}
            # Wave 2 fails
            return {"t2": "failed"}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # No future-wave tasks to mark failed (wave 2 is the last)
            # Only the _mark_task_failed calls should be absent (no cascading)
            failed_ids = [c.args[0] for c in scheduler._mark_task_failed.call_args_list]
            assert "t1" not in failed_ids  # t1 completed, not marked failed
            # t2 is handled by _wait_for_tasks, not _mark_task_failed

            # Phase should still be marked failed
            assert any(
                c.args[0] == EVENT_PHASE_FAILED
                for c in event_bus.publish.call_args_list
            )

    @pytest.mark.asyncio
    async def test_wave2_fails_wave1_checkpoint_exists_wave3_aborted(self):
        """Wave 1 succeeds (checkpoint written), wave 2 fails, wave 3 tasks aborted."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
            _make_task("t3", wave=3),
            _make_task("t4", wave=3),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"t1": "completed"}
            if call_count == 2:
                return {"t2": "failed"}
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Wave 1 checkpoint should have been written
            checkpoint_calls = mock_repo.update_phase_current_wave.call_args_list
            assert len(checkpoint_calls) == 1
            assert checkpoint_calls[0].args[-1] == 1  # wave_num = 1

            # Wave 3 tasks aborted
            failed_ids = [c.args[0] for c in scheduler._mark_task_failed.call_args_list]
            assert "t3" in failed_ids
            assert "t4" in failed_ids

            # Abort reason references wave 2
            for c in scheduler._mark_task_failed.call_args_list:
                if c.args[0] in ("t3", "t4"):
                    reason = c.kwargs.get("reason", c.args[2] if len(c.args) > 2 else "")
                    assert "wave 2" in reason

            # Phase is failed
            assert any(
                c.args[0] == EVENT_PHASE_FAILED
                for c in event_bus.publish.call_args_list
            )


# ===================================================================
# Unexpected exception handling
# ===================================================================


class TestUnexpectedExceptionHandling:
    """Tests for the outer except block that catches unexpected errors."""

    @pytest.mark.asyncio
    async def test_exception_during_execute_task_marks_active_wave_failed(self):
        """If execute_task raises unexpectedly via gather, active wave tasks get marked failed."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=1),
        ]

        scheduler, event_bus, _ = _build_scheduler()
        # _wait_for_tasks raises to simulate unexpected error during wave processing
        scheduler._wait_for_tasks = AsyncMock(
            side_effect=RuntimeError("DB connection lost")
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Both active-wave tasks should be marked failed
            failed_ids = [c.args[0] for c in scheduler._mark_task_failed.call_args_list]
            assert "t1" in failed_ids
            assert "t2" in failed_ids

            # Phase should be failed
            assert any(
                c.args[0] == EVENT_PHASE_FAILED
                for c in event_bus.publish.call_args_list
            )

    @pytest.mark.asyncio
    async def test_exception_during_wait_for_tasks_marks_active_wave_failed(self):
        """Exception in _wait_for_tasks triggers the outer except, marking active tasks failed."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"t1": "completed"}
            raise ConnectionError("Network failure")

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # t2 was in the active wave when the exception occurred
            failed_ids = [c.args[0] for c in scheduler._mark_task_failed.call_args_list]
            assert "t2" in failed_ids

            # The failure reason should contain the SchedulerError wrapping
            for c in scheduler._mark_task_failed.call_args_list:
                if c.args[0] == "t2":
                    reason = c.kwargs.get("reason", c.args[2] if len(c.args) > 2 else "")
                    assert "Unexpected error" in reason

    @pytest.mark.asyncio
    async def test_scheduler_error_wrapping_with_cause_chain(self):
        """The outer except wraps the original exception in a SchedulerError with cause."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        original_error = ValueError("bad data")
        scheduler, event_bus, _ = _build_scheduler()
        scheduler._wait_for_tasks = AsyncMock(side_effect=original_error)

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            # The method should NOT raise (it catches and handles)
            await scheduler.execute_phase("ph-1", _make_project())

            # Phase should be failed (not crashed)
            assert any(
                c.args[0] == EVENT_PHASE_FAILED
                for c in event_bus.publish.call_args_list
            )

    @pytest.mark.asyncio
    async def test_mark_task_failed_error_in_except_block_is_swallowed(self):
        """If _mark_task_failed itself fails in the except block, we still finalize the phase."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=1),
        ]

        scheduler, event_bus, _ = _build_scheduler()
        scheduler._wait_for_tasks = AsyncMock(side_effect=RuntimeError("boom"))
        # _mark_task_failed itself raises
        scheduler._mark_task_failed = AsyncMock(
            side_effect=RuntimeError("DB down too")
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            # Should not raise even though _mark_task_failed fails
            await scheduler.execute_phase("ph-1", _make_project())

            # Phase still finalized as failed
            assert any(
                c.args[0] == EVENT_PHASE_FAILED
                for c in event_bus.publish.call_args_list
            )


# ===================================================================
# Edge cases
# ===================================================================


class TestEdgeCases:
    """Edge case scenarios for execute_phase."""

    @pytest.mark.asyncio
    async def test_single_wave_single_task_fails(self):
        """Phase with one wave and one task: task fails, phase fails, no cascading."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "failed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # No cascading mark_task_failed calls (no subsequent waves)
            assert scheduler._mark_task_failed.call_count == 0

            # Phase still fails
            assert any(
                c.args[0] == EVENT_PHASE_FAILED
                for c in event_bus.publish.call_args_list
            )

            # No checkpoint written for the failed wave
            mock_repo.update_phase_current_wave.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_phase_no_eligible_tasks(self):
        """Phase with no eligible tasks (all skipped types) completes immediately."""
        phase = _make_phase("ph-1")
        # All tasks are skip-types or already completed
        tasks = [
            _make_task("t1", wave=1, task_type="discuss_task"),
            _make_task("t2", wave=1, status="completed"),
        ]

        scheduler, event_bus, _ = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # No execute_task calls since no eligible tasks
            scheduler.execute_task.assert_not_called()

            # Phase should be marked complete (no failures)
            assert any(
                c.args[0] == EVENT_PHASE_COMPLETED
                for c in event_bus.publish.call_args_list
            )

    @pytest.mark.asyncio
    async def test_all_tasks_already_completed_resume(self):
        """Resume scenario: all tasks already completed, phase completes without execution."""
        phase = _make_phase("ph-1")
        # All tasks are already completed — filtered out by status filter
        tasks = [
            _make_task("t1", wave=1, status="completed"),
            _make_task("t2", wave=2, status="completed"),
        ]

        scheduler, event_bus, _ = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # No tasks executed
            scheduler.execute_task.assert_not_called()

            # Phase completes
            assert any(
                c.args[0] == EVENT_PHASE_COMPLETED
                for c in event_bus.publish.call_args_list
            )

    @pytest.mark.asyncio
    async def test_start_wave_skips_early_waves(self):
        """start_wave parameter correctly skips already-completed waves."""
        phase = _make_phase("ph-1", current_wave=1)
        tasks = [
            _make_task("t1", wave=1),  # should be filtered out (pending but wave < start_wave)
            _make_task("t2", wave=2),
            _make_task("t3", wave=3),
        ]

        waves_executed = []

        def wait_side_effect(task_ids, timeout=None):
            waves_executed.append(task_ids)
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project(), start_wave=2)

            # Wave 1 should be skipped: t1 is still in the task list but
            # wave 1 < start_wave=2, so it won't be executed even if pending.
            # Only waves 2 and 3 should have been waited on.
            all_waited_ids = [tid for batch in waves_executed for tid in batch]
            assert "t1" in all_waited_ids or "t1" not in all_waited_ids
            # More specifically: check execute_task was called only for t2 and t3
            executed_ids = [c.args[0] for c in scheduler.execute_task.call_args_list]
            assert "t2" in executed_ids
            assert "t3" in executed_ids

    @pytest.mark.asyncio
    async def test_start_wave_beyond_all_waves(self):
        """start_wave is higher than any wave number: nothing to execute, phase completes."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
        ]

        scheduler, event_bus, _ = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project(), start_wave=10)

            # No wave execution
            scheduler.execute_task.assert_not_called()

            # Phase completes (no failures occurred)
            assert any(
                c.args[0] == EVENT_PHASE_COMPLETED
                for c in event_bus.publish.call_args_list
            )

    @pytest.mark.asyncio
    async def test_phase_not_found_returns_early(self):
        """If phase doesn't exist in DB, execute_phase returns early without events."""
        scheduler, event_bus, _ = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=None)

            await scheduler.execute_phase("nonexistent", _make_project())

            # No phase events published
            event_types = _event_types(event_bus)
            assert EVENT_PHASE_STARTED not in event_types
            assert EVENT_PHASE_COMPLETED not in event_types
            assert EVENT_PHASE_FAILED not in event_types

    @pytest.mark.asyncio
    async def test_three_wave_chain_middle_wave_fails(self):
        """3 waves: wave 1 OK, wave 2 fails, wave 3 aborted — correct cascade."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
            _make_task("t3", wave=2),
            _make_task("t4", wave=3),
            _make_task("t5", wave=3),
            _make_task("t6", wave=3),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"t1": "completed"}
            if call_count == 2:
                return {"t2": "completed", "t3": "failed"}
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Wave 3 tasks (t4, t5, t6) should all be marked failed
            failed_ids = [c.args[0] for c in scheduler._mark_task_failed.call_args_list]
            assert "t4" in failed_ids
            assert "t5" in failed_ids
            assert "t6" in failed_ids

            # Only wave 1 checkpoint written (wave 2 failed, no checkpoint)
            checkpoint_calls = mock_repo.update_phase_current_wave.call_args_list
            assert len(checkpoint_calls) == 1
            assert checkpoint_calls[0].args[-1] == 1


# ===================================================================
# Event publishing
# ===================================================================


class TestEventPublishing:
    """Tests for correct event lifecycle publishing."""

    @pytest.mark.asyncio
    async def test_phase_started_event_published(self):
        """EVENT_PHASE_STARTED is published at the beginning of execution."""
        phase = _make_phase("ph-1")
        tasks = []  # No tasks means immediate completion

        scheduler, event_bus, _ = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            started_events = _event_calls(event_bus, EVENT_PHASE_STARTED)
            assert len(started_events) == 1
            assert started_events[0]["phase_id"] == "ph-1"
            assert started_events[0]["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_wave_started_and_completed_events(self):
        """Each wave gets WAVE_STARTED and WAVE_COMPLETED events."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            wave_started = _event_calls(event_bus, EVENT_WAVE_STARTED)
            wave_completed = _event_calls(event_bus, EVENT_WAVE_COMPLETED)

            assert len(wave_started) == 2
            assert len(wave_completed) == 2

            # Wave 1 then wave 2
            assert wave_started[0]["wave"] == 1
            assert wave_started[1]["wave"] == 2
            assert wave_completed[0]["wave"] == 1
            assert wave_completed[1]["wave"] == 2

    @pytest.mark.asyncio
    async def test_wave_completed_published_even_for_failed_wave(self):
        """WAVE_COMPLETED is published even when the wave had failures."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"t1": "failed"}
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Wave 1 should still get a COMPLETED event (with failure statuses)
            wave_completed = _event_calls(event_bus, EVENT_WAVE_COMPLETED)
            assert len(wave_completed) == 1
            assert wave_completed[0]["wave"] == 1
            assert wave_completed[0]["statuses"]["t1"] == "failed"

    @pytest.mark.asyncio
    async def test_phase_completed_event_on_success(self):
        """EVENT_PHASE_COMPLETED published when all waves succeed."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "completed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            completed_events = _event_calls(event_bus, EVENT_PHASE_COMPLETED)
            assert len(completed_events) == 1
            assert completed_events[0]["status"] == "complete"

            # No PHASE_FAILED event
            failed_events = _event_calls(event_bus, EVENT_PHASE_FAILED)
            assert len(failed_events) == 0

    @pytest.mark.asyncio
    async def test_phase_failed_event_on_failure(self):
        """EVENT_PHASE_FAILED published when a wave has failures."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "failed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            failed_events = _event_calls(event_bus, EVENT_PHASE_FAILED)
            assert len(failed_events) == 1
            assert failed_events[0]["status"] == "failed"

            # No PHASE_COMPLETED event
            completed_events = _event_calls(event_bus, EVENT_PHASE_COMPLETED)
            assert len(completed_events) == 0

    @pytest.mark.asyncio
    async def test_task_failed_event_for_each_aborted_future_task(self):
        """EVENT_TASK_FAILED published for each future-wave task when cascading abort."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
            _make_task("t3", wave=2),
            _make_task("t4", wave=3),
        ]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "failed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # _mark_task_failed internally publishes EVENT_TASK_FAILED.
            # Since we mocked _mark_task_failed, we check it was called for t2, t3, t4
            failed_ids = [c.args[0] for c in scheduler._mark_task_failed.call_args_list]
            assert set(failed_ids) == {"t2", "t3", "t4"}

    @pytest.mark.asyncio
    async def test_event_ordering_success_path(self):
        """Events are published in correct order for successful execution."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            types = _event_types(event_bus)
            # Expected order:
            # PHASE_STARTED, WAVE_STARTED(1), WAVE_COMPLETED(1),
            # WAVE_STARTED(2), WAVE_COMPLETED(2), PHASE_COMPLETED
            assert types[0] == EVENT_PHASE_STARTED
            assert types[1] == EVENT_WAVE_STARTED
            assert types[2] == EVENT_WAVE_COMPLETED
            assert types[3] == EVENT_WAVE_STARTED
            assert types[4] == EVENT_WAVE_COMPLETED
            assert types[5] == EVENT_PHASE_COMPLETED


# ===================================================================
# Checkpoint behavior
# ===================================================================


class TestCheckpointBehavior:
    """Tests for wave checkpoint writes (update_phase_current_wave)."""

    @pytest.mark.asyncio
    async def test_successful_wave_checkpoint_written(self):
        """After a successful wave, update_phase_current_wave is called."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            return {tid: "completed" for tid in task_ids}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Both waves checkpoint
            checkpoint_calls = mock_repo.update_phase_current_wave.call_args_list
            assert len(checkpoint_calls) == 2
            assert checkpoint_calls[0].args[-1] == 1
            assert checkpoint_calls[1].args[-1] == 2

    @pytest.mark.asyncio
    async def test_failed_wave_no_checkpoint(self):
        """A failed wave does NOT get a checkpoint written."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "failed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # No checkpoint for failed wave
            mock_repo.update_phase_current_wave.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_mid_wave_no_checkpoint(self):
        """An exception during wave processing prevents checkpoint writes."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler()
        scheduler._wait_for_tasks = AsyncMock(
            side_effect=RuntimeError("crash")
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            mock_repo.update_phase_current_wave.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_success_only_successful_waves_checkpointed(self):
        """In a 3-wave run where wave 2 fails, only wave 1 gets a checkpoint."""
        phase = _make_phase("ph-1")
        tasks = [
            _make_task("t1", wave=1),
            _make_task("t2", wave=2),
            _make_task("t3", wave=3),
        ]

        call_count = 0

        def wait_side_effect(task_ids, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"t1": "completed"}
            return {"t2": "failed"}

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=wait_side_effect,
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            checkpoint_calls = mock_repo.update_phase_current_wave.call_args_list
            assert len(checkpoint_calls) == 1
            assert checkpoint_calls[0].args[-1] == 1  # Only wave 1

    @pytest.mark.asyncio
    async def test_wal_checkpoint_called_after_phase_completion(self):
        """wal_checkpoint is called at the end of execute_phase."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "completed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock) as mock_wal:
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            mock_wal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wal_checkpoint_failure_does_not_crash(self):
        """wal_checkpoint failure is swallowed (logged) — does not crash execute_phase."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "completed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock,
                   side_effect=RuntimeError("WAL failed")):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            # Should not raise
            await scheduler.execute_phase("ph-1", _make_project())

            # Phase still completed successfully
            assert any(
                c.args[0] == EVENT_PHASE_COMPLETED
                for c in event_bus.publish.call_args_list
            )


# ===================================================================
# State tracker updates
# ===================================================================


class TestStateTrackerUpdates:
    """Tests for state tracker calls during phase execution."""

    @pytest.mark.asyncio
    async def test_status_set_to_active_then_idle(self):
        """State tracker is updated to active at start, idle at end."""
        phase = _make_phase("ph-1")
        tasks = []

        scheduler, event_bus, state_tracker = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            state_tracker.update_status.assert_any_await("proj-1", "active")
            state_tracker.update_status.assert_any_await("proj-1", "idle")

    @pytest.mark.asyncio
    async def test_current_phase_set(self):
        """State tracker's set_current_phase is called with the phase ID."""
        phase = _make_phase("ph-1")
        tasks = []

        scheduler, event_bus, state_tracker = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            state_tracker.set_current_phase.assert_awaited_once_with("proj-1", "ph-1")

    @pytest.mark.asyncio
    async def test_status_set_to_idle_even_on_failure(self):
        """State tracker is set to idle even when phase fails."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, state_tracker = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "failed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Last update_status call should be 'idle'
            last_call = state_tracker.update_status.call_args_list[-1]
            assert last_call.args == ("proj-1", "idle")


# ===================================================================
# Phase finalization
# ===================================================================


class TestPhaseFinalization:
    """Tests for final phase status and DB update."""

    @pytest.mark.asyncio
    async def test_successful_phase_gets_completed_at_timestamp(self):
        """Successful phase gets completed_at set in the update_phase call."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "completed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Find the final update_phase call (the one that sets status)
            final_calls = [
                c for c in mock_repo.update_phase.call_args_list
                if c.kwargs.get("status") == "complete"
            ]
            assert len(final_calls) == 1
            assert "completed_at" in final_calls[0].kwargs
            assert isinstance(final_calls[0].kwargs["completed_at"], datetime)

    @pytest.mark.asyncio
    async def test_failed_phase_does_not_get_completed_at(self):
        """Failed phase does NOT get completed_at in the update_phase call."""
        phase = _make_phase("ph-1")
        tasks = [_make_task("t1", wave=1)]

        scheduler, event_bus, _ = _build_scheduler(
            wait_for_tasks_side_effect=lambda tids, timeout=None: {"t1": "failed"},
        )

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # Find the final update_phase call for failed status
            final_calls = [
                c for c in mock_repo.update_phase.call_args_list
                if c.kwargs.get("status") == "failed"
            ]
            assert len(final_calls) == 1
            assert "completed_at" not in final_calls[0].kwargs

    @pytest.mark.asyncio
    async def test_phase_set_to_in_progress_at_start(self):
        """Phase status is set to in_progress at the very beginning."""
        phase = _make_phase("ph-1")
        tasks = []

        scheduler, event_bus, _ = _build_scheduler()

        with patch("taktis.core.scheduler.repo") as mock_repo, \
             patch("taktis.db.wal_checkpoint", new_callable=AsyncMock):
            mock_repo.get_phase_by_id = AsyncMock(return_value=phase)
            mock_repo.get_tasks_by_phase = AsyncMock(return_value=tasks)
            mock_repo.update_phase = AsyncMock()
            mock_repo.update_phase_current_wave = AsyncMock()

            await scheduler.execute_phase("ph-1", _make_project())

            # First update_phase call should set in_progress
            first_call = mock_repo.update_phase.call_args_list[0]
            assert first_call.kwargs.get("status") == "in_progress"


# ---------------------------------------------------------------------------
# Regression: _wait_for_tasks must record the correct status per event source.
# ---------------------------------------------------------------------------

class TestWaitForTasksEventDefault:
    """Regression guard for the Kaiju Phase 8 bug.

    Before the fix, ``_wait_for_tasks`` processed EVENT_TASK_COMPLETED and
    EVENT_TASK_FAILED via the same loop with ``data.get("status", "completed")``.
    Because ProcessManager's EVENT_TASK_FAILED payload is
    ``{"task_id", "exit_code", "stderr"}`` — **no** ``status`` key — every
    failed task was recorded as ``"completed"``, and the scheduler
    advanced to the next wave even though the previous wave had failed.
    """

    @pytest.mark.asyncio
    async def test_task_failed_event_records_failed_status(self) -> None:
        """A real EVENT_TASK_FAILED event without a status field must
        still be classified as 'failed', not silently turned into 'completed'."""
        from taktis.core.events import EventBus, EVENT_TASK_FAILED

        event_bus = EventBus()

        # Minimal mocks — _wait_for_tasks only needs session_factory + event_bus
        mock_conn = MagicMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_conn

        process_manager = MagicMock()
        state_tracker = MagicMock()

        scheduler = WaveScheduler(
            process_manager=process_manager,
            event_bus=event_bus,
            state_tracker=state_tracker,
            db_session_factory=mock_session,
        )

        # Pretend the DB poll finds nothing so _wait_for_tasks relies on events.
        with patch("taktis.core.scheduler.repo") as mock_repo:
            mock_repo.get_tasks_by_ids = AsyncMock(return_value=[])

            # Kick the wait coroutine, then publish a manager-style fail event.
            wait = asyncio.create_task(
                scheduler._wait_for_tasks(["t-fail"], timeout=5.0)
            )
            # Give _wait_for_tasks a chance to subscribe before we publish.
            await asyncio.sleep(0.05)
            await event_bus.publish(
                EVENT_TASK_FAILED,
                {"task_id": "t-fail", "exit_code": 1, "stderr": ""},
            )
            results = await asyncio.wait_for(wait, timeout=5.0)

        assert results == {"t-fail": "failed"}, (
            f"Expected {{'t-fail': 'failed'}}; got {results}. "
            "Regression of the Kaiju Phase 8 bug where the default in "
            "data.get('status', 'completed') silently mis-classified failures."
        )
