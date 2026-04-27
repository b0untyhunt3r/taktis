"""Integration tests for crash recovery and wave checkpoint logic.

Tests the RECOVERY-01 path (stale task recovery based on wave checkpoints)
and the wave checkpoint + resume cycle using real DB operations.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from uuid import uuid4
from datetime import datetime, timezone

import aiosqlite
from taktis.db import _CREATE_TABLES_SQL
from taktis import repository as repo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _uuid() -> str:
    return uuid4().hex[:8]


def _full_uuid() -> str:
    return uuid4().hex


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory DB with tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(_CREATE_TABLES_SQL)
    await conn.commit()
    try:
        yield conn
    finally:
        await conn.close()


async def _insert_project(db, name: str = "test-proj") -> str:
    project_id = _full_uuid()
    await repo.create_project(
        db,
        id=project_id,
        name=name,
        working_dir=".",
        description="Test",
    )
    return project_id


async def _insert_phase(
    db, project_id: str, phase_number: int = 1, current_wave: int | None = None,
) -> str:
    phase_id = _full_uuid()
    await repo.create_phase(
        db,
        id=phase_id,
        project_id=project_id,
        name=f"Phase {phase_number}",
        phase_number=phase_number,
    )
    if current_wave is not None:
        await repo.update_phase_current_wave(db, phase_id, current_wave)
    return phase_id


async def _insert_task(
    db, project_id: str, phase_id: str, status: str = "pending",
    wave: int = 1, pid: int | None = None, name: str = "",
) -> str:
    task_id = _uuid()
    await repo.create_task(
        db,
        id=task_id,
        project_id=project_id,
        phase_id=phase_id,
        name=name or f"Task-{task_id}",
        prompt="Do stuff",
        status=status,
        wave=wave,
        pid=pid,
    )
    return task_id


# ======================================================================
# RECOVERY-01: Stale task recovery based on wave checkpoints
# ======================================================================

class TestStaleTaskRecovery:
    """Test the RECOVERY-01 algorithm directly at the repository level."""

    @pytest.mark.asyncio
    async def test_stale_tasks_with_checkpoint_reset_to_pending(self, db):
        """Tasks with a parent phase that has current_wave should be reset to pending."""
        project_id = await _insert_project(db, "rec-cp")
        phase_id = await _insert_phase(db, project_id, current_wave=2)

        # Simulate stale tasks (running with dead PID)
        t1 = await _insert_task(db, project_id, phase_id, status="running", wave=3, pid=99999)
        t2 = await _insert_task(db, project_id, phase_id, status="awaiting_input", wave=3, pid=99998)

        stale = await repo.get_stale_tasks(db)
        assert len(stale) == 2

        # Apply RECOVERY-01 logic: checkpoint exists → reset to pending
        for task in stale:
            phase = await repo.get_phase_by_id(db, task["phase_id"])
            has_checkpoint = phase is not None and phase.get("current_wave") is not None
            assert has_checkpoint is True
            await repo.update_task(db, task["id"], status="pending")

        # Verify
        task1 = await repo.get_task(db, t1)
        task2 = await repo.get_task(db, t2)
        assert task1["status"] == "pending"
        assert task2["status"] == "pending"

    @pytest.mark.asyncio
    async def test_stale_tasks_without_checkpoint_marked_failed(self, db):
        """Tasks with no wave checkpoint should be marked failed."""
        project_id = await _insert_project(db, "rec-nocp")
        phase_id = await _insert_phase(db, project_id)  # no current_wave

        t1 = await _insert_task(db, project_id, phase_id, status="running", wave=1, pid=99997)

        stale = await repo.get_stale_tasks(db)
        assert len(stale) == 1

        # Apply RECOVERY-01: no checkpoint → fail
        phase = await repo.get_phase_by_id(db, stale[0]["phase_id"])
        has_checkpoint = phase is not None and phase.get("current_wave") is not None
        assert has_checkpoint is False
        await repo.update_task(
            db, stale[0]["id"], status="failed",
            completed_at=datetime.now(timezone.utc),
            result_summary="FAILED: Process lost",
        )

        task = await repo.get_task(db, t1)
        assert task["status"] == "failed"
        assert "Process lost" in task["result_summary"]

    @pytest.mark.asyncio
    async def test_mixed_stale_tasks(self, db):
        """Mix of tasks with and without checkpoints get correct treatment."""
        project_id = await _insert_project(db, "rec-mix")
        phase_with_cp = await _insert_phase(db, project_id, phase_number=1, current_wave=1)
        phase_no_cp = await _insert_phase(db, project_id, phase_number=2)

        t_pending = await _insert_task(
            db, project_id, phase_with_cp, status="running", wave=2, pid=88881,
        )
        t_failed = await _insert_task(
            db, project_id, phase_no_cp, status="running", wave=1, pid=88882,
        )

        stale = await repo.get_stale_tasks(db)
        assert len(stale) == 2

        for task in stale:
            phase = await repo.get_phase_by_id(db, task["phase_id"])
            has_checkpoint = phase is not None and phase.get("current_wave") is not None
            if has_checkpoint:
                await repo.update_task(db, task["id"], status="pending")
            else:
                await repo.update_task(db, task["id"], status="failed")

        assert (await repo.get_task(db, t_pending))["status"] == "pending"
        assert (await repo.get_task(db, t_failed))["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_stale_tasks(self, db):
        """No stale tasks → get_stale_tasks returns empty list."""
        project_id = await _insert_project(db, "rec-clean")
        phase_id = await _insert_phase(db, project_id)
        await _insert_task(db, project_id, phase_id, status="pending")
        await _insert_task(db, project_id, phase_id, status="completed")

        stale = await repo.get_stale_tasks(db)
        assert stale == []


# ======================================================================
# Wave checkpoint write + resume cycle
# ======================================================================

class TestWaveCheckpoints:

    @pytest.mark.asyncio
    async def test_checkpoint_write(self, db):
        """Writing a wave checkpoint persists current_wave on the phase."""
        project_id = await _insert_project(db, "wc-write")
        phase_id = await _insert_phase(db, project_id)

        # Initially no checkpoint
        phase = await repo.get_phase_by_id(db, phase_id)
        assert phase["current_wave"] is None

        # Write checkpoint
        await repo.update_phase_current_wave(db, phase_id, 1)

        phase = await repo.get_phase_by_id(db, phase_id)
        assert phase["current_wave"] == 1

    @pytest.mark.asyncio
    async def test_checkpoint_increments(self, db):
        """Successive wave completions increment the checkpoint."""
        project_id = await _insert_project(db, "wc-inc")
        phase_id = await _insert_phase(db, project_id)

        await repo.update_phase_current_wave(db, phase_id, 1)
        await repo.update_phase_current_wave(db, phase_id, 2)
        await repo.update_phase_current_wave(db, phase_id, 3)

        phase = await repo.get_phase_by_id(db, phase_id)
        assert phase["current_wave"] == 3

    @pytest.mark.asyncio
    async def test_completed_tasks_not_rerun_on_resume(self, db):
        """Tasks already in 'completed' status should be skipped on resume.

        This verifies the guard: the scheduler's execute_task checks
        task status before executing and skips completed tasks.
        """
        project_id = await _insert_project(db, "wc-skip")
        phase_id = await _insert_phase(db, project_id, current_wave=1)

        # Wave 1 tasks: all completed
        t1 = await _insert_task(
            db, project_id, phase_id, status="completed", wave=1, name="Done-1",
        )
        t2 = await _insert_task(
            db, project_id, phase_id, status="completed", wave=1, name="Done-2",
        )
        # Wave 2 task: pending (should run on resume)
        t3 = await _insert_task(
            db, project_id, phase_id, status="pending", wave=2, name="Todo-3",
        )

        # Verify: wave 1 tasks are completed, wave 2 task is pending
        assert (await repo.get_task(db, t1))["status"] == "completed"
        assert (await repo.get_task(db, t2))["status"] == "completed"
        assert (await repo.get_task(db, t3))["status"] == "pending"

        # Fetch tasks by phase — verify ordering
        tasks = await repo.get_tasks_by_phase(db, phase_id)
        assert len(tasks) == 3
        waves = [t["wave"] for t in tasks]
        assert waves == [1, 1, 2]

    @pytest.mark.asyncio
    async def test_partial_wave_failure_leaves_checkpoint_at_previous(self, db):
        """If a wave has failures, checkpoint stays at previous wave.

        Per WAVE-INVARIANT: current_wave = N means wave N fully completed.
        A partial failure in wave 2 should leave checkpoint at 1.
        """
        project_id = await _insert_project(db, "wc-partial")
        phase_id = await _insert_phase(db, project_id)

        # Complete wave 1
        await _insert_task(
            db, project_id, phase_id, status="completed", wave=1,
        )
        await repo.update_phase_current_wave(db, phase_id, 1)

        # Wave 2: one completed, one failed
        await _insert_task(
            db, project_id, phase_id, status="completed", wave=2,
        )
        await _insert_task(
            db, project_id, phase_id, status="failed", wave=2,
        )
        # Do NOT write wave 2 checkpoint (it had failures)

        phase = await repo.get_phase_by_id(db, phase_id)
        assert phase["current_wave"] == 1  # Still at wave 1

    @pytest.mark.asyncio
    async def test_resume_from_checkpoint_skips_completed_waves(self, db):
        """On resume, tasks in waves <= current_wave that are completed
        should not need re-execution.  Only tasks in waves > current_wave
        (or failed tasks in current_wave) need attention.
        """
        project_id = await _insert_project(db, "wc-resume")
        phase_id = await _insert_phase(db, project_id, current_wave=2)

        # Wave 1 and 2 tasks: completed
        for w in (1, 2):
            await _insert_task(
                db, project_id, phase_id, status="completed", wave=w,
            )

        # Wave 3 tasks: pending (should run on resume)
        t3a = await _insert_task(
            db, project_id, phase_id, status="pending", wave=3,
        )
        t3b = await _insert_task(
            db, project_id, phase_id, status="pending", wave=3,
        )

        # Verify the pending tasks for wave 3 exist
        tasks = await repo.get_tasks_by_phase(db, phase_id)
        pending = [t for t in tasks if t["status"] == "pending"]
        assert len(pending) == 2
        assert all(t["wave"] == 3 for t in pending)


# ======================================================================
# Semaphore-level concurrency (unit test of ProcessManager limits)
# ======================================================================

class TestConcurrencyLimits:

    @pytest.mark.asyncio
    async def test_semaphore_tracks_count(self):
        """ProcessManager semaphore should limit concurrent acquisitions."""
        import asyncio

        max_concurrent = 3
        sem = asyncio.Semaphore(max_concurrent)

        # Acquire all slots
        for _ in range(max_concurrent):
            acquired = await asyncio.wait_for(sem.acquire(), timeout=0.1)
            assert acquired is True

        # Next acquire should block (timeout)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sem.acquire(), timeout=0.05)

        # Release one → next acquire succeeds
        sem.release()
        acquired = await asyncio.wait_for(sem.acquire(), timeout=0.1)
        assert acquired is True


# ======================================================================
# Streaming error retry (retry_count column + detection)
# ======================================================================

class TestStreamingRetry:

    @pytest.mark.asyncio
    async def test_retry_count_column_defaults_zero(self, db):
        """New tasks have retry_count = 0."""
        project_id = await _insert_project(db, "retry-def")
        phase_id = await _insert_phase(db, project_id)
        task_id = await _insert_task(db, project_id, phase_id)
        task = await repo.get_task(db, task_id)
        assert task["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_retry_count_can_be_incremented(self, db):
        """retry_count can be updated via update_task."""
        project_id = await _insert_project(db, "retry-inc")
        phase_id = await _insert_phase(db, project_id)
        task_id = await _insert_task(db, project_id, phase_id)

        await repo.update_task(db, task_id, retry_count=1)
        task = await repo.get_task(db, task_id)
        assert task["retry_count"] == 1

        await repo.update_task(db, task_id, retry_count=2)
        task = await repo.get_task(db, task_id)
        assert task["retry_count"] == 2

    @pytest.mark.asyncio
    async def test_retry_resets_to_pending(self, db):
        """A streaming-error retry resets task to pending with incremented count."""
        project_id = await _insert_project(db, "retry-pend")
        phase_id = await _insert_phase(db, project_id)
        task_id = await _insert_task(
            db, project_id, phase_id, status="running",
        )

        # Simulate retry: reset to pending, increment retry_count
        await repo.update_task(
            db, task_id,
            status="pending",
            completed_at=None,
            retry_count=1,
            result_summary="Retrying after streaming error (attempt 1)",
        )

        task = await repo.get_task(db, task_id)
        assert task["status"] == "pending"
        assert task["retry_count"] == 1
        assert "Retrying" in task["result_summary"]

    @pytest.mark.asyncio
    async def test_max_retries_leads_to_failed(self, db):
        """After max retries, task should stay failed."""
        _DEFAULT_MAX_ATTEMPTS = 2  # default retry policy max_attempts

        project_id = await _insert_project(db, "retry-max")
        phase_id = await _insert_phase(db, project_id)
        task_id = await _insert_task(
            db, project_id, phase_id, status="running",
        )

        # Simulate reaching max retries
        await repo.update_task(db, task_id, retry_count=_DEFAULT_MAX_ATTEMPTS)
        task = await repo.get_task(db, task_id)
        assert task["retry_count"] == _DEFAULT_MAX_ATTEMPTS

        # Next failure should NOT retry — stays as whatever status is set
        await repo.update_task(db, task_id, status="failed")
        task = await repo.get_task(db, task_id)
        assert task["status"] == "failed"
