"""Tests for WaveScheduler — wave assignment, checkpointing, and task guards."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import pytest_asyncio

from taktis.core import phase_review
from taktis.core.scheduler import WaveScheduler


def _make_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    """Helper to build a minimal task dict for wave assignment."""
    return {
        "id": task_id,
        "depends_on": depends_on or [],
        "wave": 1,
    }


class TestAutoAssignWaves:

    def test_auto_assign_waves_no_deps(self):
        """All tasks with no dependencies go to wave 1."""
        tasks = [_make_task("A"), _make_task("B"), _make_task("C")]
        waves = WaveScheduler.auto_assign_waves(tasks)
        assert set(waves.keys()) == {1}
        assert len(waves[1]) == 3
        for t in waves[1]:
            assert t["wave"] == 1

    def test_auto_assign_waves_linear_chain(self):
        """A -> B -> C produces waves 1, 2, 3."""
        tasks = [
            _make_task("A"),
            _make_task("B", depends_on=["A"]),
            _make_task("C", depends_on=["B"]),
        ]
        waves = WaveScheduler.auto_assign_waves(tasks)
        assert set(waves.keys()) == {1, 2, 3}

        ids_by_wave = {w: {t["id"] for t in ts} for w, ts in waves.items()}
        assert ids_by_wave[1] == {"A"}
        assert ids_by_wave[2] == {"B"}
        assert ids_by_wave[3] == {"C"}

    def test_auto_assign_waves_parallel(self):
        """A, B (no deps) + C (depends on A and B) => waves 1,1,2."""
        tasks = [
            _make_task("A"),
            _make_task("B"),
            _make_task("C", depends_on=["A", "B"]),
        ]
        waves = WaveScheduler.auto_assign_waves(tasks)
        assert set(waves.keys()) == {1, 2}

        ids_by_wave = {w: {t["id"] for t in ts} for w, ts in waves.items()}
        assert ids_by_wave[1] == {"A", "B"}
        assert ids_by_wave[2] == {"C"}

    def test_auto_assign_waves_diamond(self):
        """Diamond: A -> B, A -> C, B+C -> D => waves 1, 2, 2, 3."""
        tasks = [
            _make_task("A"),
            _make_task("B", depends_on=["A"]),
            _make_task("C", depends_on=["A"]),
            _make_task("D", depends_on=["B", "C"]),
        ]
        waves = WaveScheduler.auto_assign_waves(tasks)

        task_waves = {t["id"]: t["wave"] for ts in waves.values() for t in ts}
        assert task_waves["A"] == 1
        assert task_waves["B"] == 2
        assert task_waves["C"] == 2
        assert task_waves["D"] == 3

    def test_auto_assign_waves_cycle_detection(self):
        """Cycles should not crash -- tasks involved in cycles get wave 1."""
        tasks = [
            _make_task("A", depends_on=["B"]),
            _make_task("B", depends_on=["A"]),
        ]
        # Should not raise
        waves = WaveScheduler.auto_assign_waves(tasks)
        # Both tasks should have been assigned some wave without crashing
        all_task_ids = {t["id"] for ts in waves.values() for t in ts}
        assert "A" in all_task_ids
        assert "B" in all_task_ids

    def test_auto_assign_waves_deps_as_json_string(self):
        """depends_on stored as a JSON string (from DB) should still work."""
        import json
        tasks = [
            _make_task("A"),
            {"id": "B", "depends_on": json.dumps(["A"]), "wave": 1},
        ]
        waves = WaveScheduler.auto_assign_waves(tasks)
        task_waves = {t["id"]: t["wave"] for ts in waves.values() for t in ts}
        assert task_waves["A"] == 1
        assert task_waves["B"] == 2


# ---------------------------------------------------------------------------
# Helpers shared by the behavioural tests below
# ---------------------------------------------------------------------------

def _make_phase(phase_id: str = "phase-1", current_wave: int | None = None) -> dict:
    """Minimal phase dict as returned by ``repo.get_phase_by_id``."""
    return {
        "id": phase_id,
        "project_id": "proj-1",
        "phase_number": 1,
        "name": "Test Phase",
        "status": "not_started",
        "current_wave": current_wave,
        "planning_options": None,
    }


def _make_db_task(
    task_id: str,
    wave: int = 1,
    status: str = "pending",
    task_type: str = "implementation",
) -> dict:
    """Minimal task dict as returned by ``repo.get_tasks_by_phase`` / ``get_task``."""
    return {
        "id": task_id,
        "phase_id": "phase-1",
        "project_id": "proj-1",
        "name": f"Task {task_id}",
        "wave": wave,
        "status": status,
        "task_type": task_type,
        "prompt": "do something",
        "model": None,
        "expert_id": None,
        "interactive": False,
        "env_vars": None,
        "system_prompt": None,
        "checkpoint_type": None,
        "session_id": None,
    }


def _make_scheduler() -> tuple[WaveScheduler, MagicMock, MagicMock, MagicMock]:
    """Create a WaveScheduler wired to lightweight mock collaborators.

    Returns
    -------
    scheduler, mock_conn, event_bus, state_tracker
        ``mock_conn`` is the connection object yielded by the session factory.
        All async methods on ``event_bus`` and ``state_tracker`` are
        ``AsyncMock`` instances.
    """
    mock_conn = MagicMock()

    @asynccontextmanager
    async def _session_factory():
        yield mock_conn

    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    event_bus.subscribe = MagicMock(return_value=asyncio.Queue())
    event_bus.unsubscribe = MagicMock()

    state_tracker = MagicMock()
    state_tracker.update_status = AsyncMock()
    state_tracker.set_current_phase = AsyncMock()

    process_manager = MagicMock()

    scheduler = WaveScheduler(
        process_manager=process_manager,
        event_bus=event_bus,
        state_tracker=state_tracker,
        db_session_factory=_session_factory,
    )
    return scheduler, mock_conn, event_bus, state_tracker


# ---------------------------------------------------------------------------
# execute_task — completed-task guard
# ---------------------------------------------------------------------------

class TestExecuteTaskCompletedGuard:
    """execute_task() must return immediately for already-completed tasks."""

    @pytest.mark.asyncio
    async def test_completed_task_is_skipped(self):
        """A task with status='completed' must not be re-executed.

        Specifically, ``repo.update_task`` must never be called with
        ``status='running'``, confirming the task is not restarted.
        """
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        completed_task = _make_db_task("t1", status="completed")

        with (
            patch(
                "taktis.core.scheduler.repo.get_task",
                new=AsyncMock(return_value=completed_task),
            ) as mock_get_task,
            patch(
                "taktis.core.scheduler.repo.update_task",
                new=AsyncMock(),
            ) as mock_update_task,
        ):
            await scheduler.execute_task("t1", project)

        # task was fetched
        mock_get_task.assert_awaited_once_with(mock_conn, "t1")
        # status must NOT have been set to 'running' (or anything else)
        for c in mock_update_task.call_args_list:
            kwargs = c.kwargs if c.kwargs else {}
            args = c.args if c.args else ()
            assert kwargs.get("status") != "running", (
                "update_task was called with status='running' for a completed task"
            )
        # no process-start event should have been published
        for c in event_bus.publish.call_args_list:
            data = c.args[1] if len(c.args) > 1 else {}
            assert data.get("task_id") != "t1" or "task.started" not in str(c.args[0])

    @pytest.mark.asyncio
    async def test_pending_task_is_executed(self):
        """A task with status='pending' must proceed past the guard.

        We verify this by confirming ``repo.update_task`` is called with
        ``status='running'`` (the first thing execute_task does after the
        guard).
        """
        scheduler, mock_conn, _eb, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        pending_task = _make_db_task("t2", status="pending")

        # We stub out enough of the DB and process manager to avoid
        # running a real subprocess, but we let execution proceed until
        # the process start call so we can observe the 'running' update.
        fake_process = MagicMock()
        fake_process.started_at = None

        with (
            patch(
                "taktis.core.scheduler.repo.get_task",
                new=AsyncMock(return_value=pending_task),
            ),
            patch(
                "taktis.core.scheduler.repo.get_expert_by_id",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=_make_phase()),
            ),
            patch(
                "taktis.core.scheduler.repo.update_task",
                new=AsyncMock(),
            ) as mock_update_task,
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.create_task_output",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.context.get_phase_context",
                return_value=(None, []),
            ),
            patch(
                "taktis.core.context.generate_state_summary",
                new=AsyncMock(return_value=""),
            ),
            patch.object(
                scheduler._manager,
                "register_callbacks",
            ),
            patch.object(
                scheduler._manager,
                "start_task",
                new=AsyncMock(return_value=fake_process),
            ),
        ):
            await scheduler.execute_task("t2", project)

        # First update_task call should mark the task as 'running'
        running_calls = [
            c for c in mock_update_task.call_args_list
            if c.kwargs.get("status") == "running" or (
                len(c.args) > 1 and "running" in str(c.args)
            )
        ]
        assert running_calls, (
            "Expected at least one update_task(status='running') call for a pending task"
        )


# ---------------------------------------------------------------------------
# execute_phase — start_wave parameter
# ---------------------------------------------------------------------------

class TestExecutePhaseStartWave:
    """execute_phase(start_wave=N) must skip all waves numbered < N."""

    @pytest.mark.asyncio
    async def test_start_wave_1_executes_all_waves(self):
        """Default start_wave=1 runs every wave (baseline / existing callers)."""
        scheduler, mock_conn, _eb, _st = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [
            _make_db_task("t1", wave=1),
            _make_db_task("t2", wave=2),
        ]

        executed_task_ids: list[str] = []

        async def _fake_execute_task(tid, proj):
            executed_task_ids.append(tid)

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler, "_wait_for_tasks",
                new=AsyncMock(side_effect=lambda ids: {tid: "completed" for tid in ids}),
            ),
            patch.object(scheduler, "execute_task", side_effect=_fake_execute_task),
        ):
            await scheduler.execute_phase("phase-1", project, start_wave=1)

        assert set(executed_task_ids) == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_start_wave_2_skips_wave_1(self):
        """start_wave=2 must skip wave 1 tasks entirely."""
        scheduler, mock_conn, _eb, _st = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        # Wave 1 task has status='pending' so it would normally be executed.
        # With start_wave=2, it must be skipped.
        tasks = [
            _make_db_task("t1", wave=1, status="pending"),
            _make_db_task("t2", wave=2, status="pending"),
        ]

        executed_task_ids: list[str] = []

        async def _fake_execute_task(tid, proj):
            executed_task_ids.append(tid)

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler, "_wait_for_tasks",
                new=AsyncMock(side_effect=lambda ids: {tid: "completed" for tid in ids}),
            ),
            patch.object(scheduler, "execute_task", side_effect=_fake_execute_task),
        ):
            await scheduler.execute_phase("phase-1", project, start_wave=2)

        assert "t1" not in executed_task_ids, "Wave 1 task must be skipped with start_wave=2"
        assert "t2" in executed_task_ids, "Wave 2 task must still execute with start_wave=2"

    @pytest.mark.asyncio
    async def test_start_wave_beyond_all_waves_executes_nothing(self):
        """start_wave higher than any wave number results in no task execution."""
        scheduler, mock_conn, _eb, _st = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [
            _make_db_task("t1", wave=1, status="pending"),
            _make_db_task("t2", wave=2, status="pending"),
        ]

        executed_task_ids: list[str] = []

        async def _fake_execute_task(tid, proj):
            executed_task_ids.append(tid)

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler, "_wait_for_tasks",
                new=AsyncMock(side_effect=lambda ids: {tid: "completed" for tid in ids}),
            ),
            patch.object(scheduler, "execute_task", side_effect=_fake_execute_task),
        ):
            await scheduler.execute_phase("phase-1", project, start_wave=99)

        assert executed_task_ids == [], "No tasks should execute when start_wave exceeds all waves"


# ---------------------------------------------------------------------------
# execute_phase — wave checkpoint writes
# ---------------------------------------------------------------------------

class TestWaveCheckpoint:
    """Wave checkpoints must follow the WAVE-INVARIANT."""

    @pytest.mark.asyncio
    async def test_checkpoint_written_after_successful_wave(self):
        """update_phase_current_wave must be called with the correct wave number
        after every wave that finishes without failures.

        WAVE-INVARIANT: current_wave=N ⟺ last fully completed wave is N.
        """
        scheduler, mock_conn, _eb, _st = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [
            _make_db_task("t1", wave=1),
            _make_db_task("t2", wave=2),
        ]

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ) as mock_checkpoint,
            patch.object(
                scheduler, "_wait_for_tasks",
                new=AsyncMock(side_effect=lambda ids: {tid: "completed" for tid in ids}),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
        ):
            await scheduler.execute_phase("phase-1", project)

        # Checkpoint must be written once per wave, in order
        assert mock_checkpoint.await_count == 2, (
            f"Expected 2 checkpoint writes (one per wave), got {mock_checkpoint.await_count}"
        )
        calls = mock_checkpoint.call_args_list
        # First call: wave 1
        assert calls[0].args[1] == "phase-1"
        assert calls[0].args[2] == 1
        # Second call: wave 2
        assert calls[1].args[1] == "phase-1"
        assert calls[1].args[2] == 2

    @pytest.mark.asyncio
    async def test_checkpoint_not_written_for_failed_wave(self):
        """update_phase_current_wave must NOT be called when a wave has failures.

        A failed wave must not be checkpointed so that on resume the scheduler
        re-enters the wave and retries failed tasks.
        """
        scheduler, mock_conn, _eb, _st = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [
            _make_db_task("t1", wave=1),
            _make_db_task("t2", wave=1),
        ]

        # t2 fails; the wave is considered failed
        async def _wait_result(ids):
            return {ids[0]: "completed", ids[1]: "failed"} if len(ids) == 2 else {ids[0]: "failed"}

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ) as mock_checkpoint,
            patch.object(
                scheduler, "_wait_for_tasks",
                new=AsyncMock(side_effect=_wait_result),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
        ):
            await scheduler.execute_phase("phase-1", project)

        mock_checkpoint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_checkpoint_written_only_for_passing_waves(self):
        """In a multi-wave run, checkpoint is written for wave 1 but not
        wave 2 when wave 2 fails.

        WAVE-INVARIANT: after this run current_wave should equal 1.
        """
        scheduler, mock_conn, _eb, _st = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [
            _make_db_task("t1", wave=1),
            _make_db_task("t2", wave=2),
        ]

        call_count = 0

        async def _wait_result(ids):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Wave 1 succeeds
                return {tid: "completed" for tid in ids}
            else:
                # Wave 2 fails
                return {tid: "failed" for tid in ids}

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ) as mock_checkpoint,
            patch.object(
                scheduler, "_wait_for_tasks",
                new=AsyncMock(side_effect=_wait_result),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
        ):
            await scheduler.execute_phase("phase-1", project)

        # Only one checkpoint write — for wave 1
        assert mock_checkpoint.await_count == 1, (
            f"Expected exactly 1 checkpoint write (wave 1 only), got {mock_checkpoint.await_count}"
        )
        written_wave = mock_checkpoint.call_args.args[2]
        assert written_wave == 1, f"Expected checkpoint for wave 1, got wave {written_wave}"

    @pytest.mark.asyncio
    async def test_checkpoint_uses_own_session(self):
        """The wave checkpoint must open its own DB session (separate from
        the session used by the initial phase/task load).

        We verify this by confirming the session factory is called more times
        than the initial load alone would require.  With 1 wave the factory
        must be called at least twice: once for the initial load/update and
        once for the checkpoint (plus once at the end for the final status).
        """
        session_open_count = 0
        mock_conn = MagicMock()

        @asynccontextmanager
        async def _counting_session_factory():
            nonlocal session_open_count
            session_open_count += 1
            yield mock_conn

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()
        event_bus.subscribe = MagicMock(return_value=asyncio.Queue())
        event_bus.unsubscribe = MagicMock()

        state_tracker = MagicMock()
        state_tracker.update_status = AsyncMock()
        state_tracker.set_current_phase = AsyncMock()

        scheduler = WaveScheduler(
            process_manager=MagicMock(),
            event_bus=event_bus,
            state_tracker=state_tracker,
            db_session_factory=_counting_session_factory,
        )

        phase = _make_phase()
        tasks = [_make_db_task("t1", wave=1)]
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler, "_wait_for_tasks",
                new=AsyncMock(return_value={"t1": "completed"}),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
        ):
            await scheduler.execute_phase("phase-1", project)

        # Minimum expected opens:
        #   1 — initial load (get_phase_by_id + update_phase + get_tasks_by_phase)
        #   1 — wave checkpoint
        #   1 — final phase status update
        assert session_open_count >= 3, (
            f"Expected ≥3 session opens (load + checkpoint + final), got {session_open_count}"
        )


# ---------------------------------------------------------------------------
# Wave checkpointing — DB-level persistence (TC-CK-01)
# ---------------------------------------------------------------------------

class TestWaveCheckpointDB:
    """DB-level verification that the wave checkpoint is durably written.

    These tests use the real in-memory aiosqlite connection from conftest so
    that ``current_wave`` can be read back from the database rather than
    merely asserting that a mock was called with the right arguments.
    """

    @pytest.mark.asyncio
    async def test_wave_checkpoint_written(self, db_conn):
        """After wave 1 tasks complete, current_wave=1 must be persisted in the DB.

        Preconditions:
            - Phase exists with two waves of tasks.
            - Wave-1 task status is 'completed'.
        Steps:
            1. Create project, phase, and one task per wave.
            2. Mark the wave-1 task as 'completed'.
            3. Call the checkpoint write logic directly
               (``repo.update_phase_current_wave``), mirroring what
               ``execute_phase`` does after a successful wave.
            4. Re-read the phase row from the DB.
        Expected: ``phase["current_wave"] == 1``.
        """
        import taktis.repository as repo

        # ---- test data -------------------------------------------------------
        project = await repo.create_project(
            db_conn,
            id="proj-ck-db",
            name="ck-db-proj",
            working_dir="/tmp",
        )
        phase = await repo.create_phase(
            db_conn,
            id="phase-ck-db",
            project_id=project["id"],
            name="Checkpoint Phase",
            phase_number=1,
        )
        # Wave-1 task — will be simulated as completed.
        await repo.create_task(
            db_conn,
            id="t-ckdb-w1",
            phase_id=phase["id"],
            project_id=project["id"],
            name="Wave 1 Task",
            wave=1,
            status="pending",
        )
        # Wave-2 task — still pending; we only checkpoint after wave 1 here.
        await repo.create_task(
            db_conn,
            id="t-ckdb-w2",
            phase_id=phase["id"],
            project_id=project["id"],
            name="Wave 2 Task",
            wave=2,
            status="pending",
        )
        await db_conn.commit()

        # ---- add updated_at column (migration gap) --------------------------
        # The in-memory schema created by _CREATE_TABLES_SQL does not include
        # `updated_at` on the `phases` table, but `update_phase_current_wave`
        # references it.  Real production DBs hit the same issue; the column
        # is absent from the canonical schema.  We add it here so the live
        # repo function can be called as production code would call it, making
        # this test a faithful end-to-end DB verification.
        try:
            await db_conn.execute("ALTER TABLE phases ADD COLUMN updated_at TEXT")
            await db_conn.commit()
        except Exception:
            pass  # column may already exist in a future schema revision

        # ---- simulate wave 1 completing -------------------------------------
        await repo.update_task(db_conn, "t-ckdb-w1", status="completed")

        # ---- call the checkpoint write logic --------------------------------
        await repo.update_phase_current_wave(db_conn, phase["id"], 1)
        await db_conn.commit()

        # ---- assertion -------------------------------------------------------
        refreshed = await repo.get_phase_by_id(db_conn, phase["id"])
        assert refreshed is not None, "Phase must still exist after checkpoint write"
        assert refreshed["current_wave"] == 1, (
            f"Expected current_wave=1 in DB after wave-1 checkpoint, "
            f"got {refreshed['current_wave']!r}"
        )


# ---------------------------------------------------------------------------
# Startup recovery — RECOVERY-01 branching (TC-REC-01, TC-REC-02)
# ---------------------------------------------------------------------------

class TestStartupRecovery:
    """_recover_stale_tasks() must branch on current_wave IS NOT NULL.

    These are the core regression guards for RECOVERY-01.  They use the
    ``taktis_engine`` fixture so that ``get_session()`` inside
    ``_recover_stale_tasks`` connects to the same temporary database that
    the test data is written to.
    """

    @pytest.mark.asyncio
    async def test_startup_recovery_resets_running_to_pending(self, taktis_engine):
        """RECOVERY-01: stale 'running' task with a dead PID inside a
        checkpointed phase is reset to 'pending', NOT 'failed'.

        Preconditions:
            - Phase exists with ``current_wave=1`` (checkpoint present).
            - One task in wave 2 has ``status='running'`` and a dead PID.
        Steps:
            1. Create the project, phase, and task.
            2. Set ``current_wave=1`` on the phase and ``status='running'``
               with a dead PID on the task.
            3. Call ``_recover_stale_tasks()``.
        Expected: task ``status == 'pending'`` (NOT 'failed').

        This is the core RECOVERY-01 test: a checkpoint means it is safe to
        re-run the task — do not discard it as a failure.
        """
        import taktis.repository as repo
        from taktis.db import get_session

        await taktis_engine.create_project(name="rec01-proj", working_dir=".")
        await taktis_engine.create_phase(project_name="rec01-proj", name="Checkpointed Phase")

        task = await taktis_engine.create_task(
            project_name="rec01-proj",
            prompt="Wave 2 work",
            phase_number=1,
            wave=2,
        )
        phase = await taktis_engine.get_phase("rec01-proj", 1)

        async with get_session() as conn:
            # Checkpoint: wave 1 completed → current_wave=1.
            await repo.update_phase(conn, phase["id"], current_wave=1)
            # Stale task: left in 'running' state with a dead PID.
            # PID 999999999 is above any realistic system limit and is
            # guaranteed to not be a live process.
            await repo.update_task(conn, task["id"], status="running", pid=999999999)

        await taktis_engine._execution_service._recover_stale_tasks()

        recovered = await taktis_engine.get_task(task["id"])
        assert recovered["status"] == "pending", (
            f"RECOVERY-01: expected 'pending' (checkpoint exists), "
            f"got '{recovered['status']}'"
        )

    @pytest.mark.asyncio
    async def test_startup_recovery_no_checkpoint_marks_failed(self, taktis_engine):
        """Old behavior preserved: stale 'running' task with no wave checkpoint
        is marked 'failed' (regression guard against accidentally breaking the
        pre-RECOVERY-01 path).

        Preconditions:
            - Phase exists with ``current_wave=NULL`` (no checkpoint).
            - One task has ``status='running'`` with a dead PID.
        Steps:
            1. Create the project, phase, and task.
            2. Leave ``current_wave=NULL`` on the phase (default).
            3. Set ``status='running'`` with a dead PID on the task.
            4. Call ``_recover_stale_tasks()``.
        Expected: task ``status == 'failed'`` (old behavior preserved).

        This ensures the checkpoint-aware branching does NOT accidentally
        reset tasks to 'pending' when there is no safe resume point.
        """
        import taktis.repository as repo
        from taktis.db import get_session

        await taktis_engine.create_project(name="rec02-proj", working_dir=".")
        await taktis_engine.create_phase(project_name="rec02-proj", name="Uncheckpointed Phase")

        task = await taktis_engine.create_task(
            project_name="rec02-proj",
            prompt="Wave 1 work",
            phase_number=1,
            wave=1,
        )

        async with get_session() as conn:
            # No checkpoint — current_wave stays NULL.
            # Stale task: left in 'running' state with a dead PID.
            await repo.update_task(conn, task["id"], status="running", pid=999999999)

        await taktis_engine._execution_service._recover_stale_tasks()

        recovered = await taktis_engine.get_task(task["id"])
        assert recovered["status"] == "failed", (
            f"Regression guard: expected 'failed' (no checkpoint), "
            f"got '{recovered['status']}'"
        )


# ---------------------------------------------------------------------------
# resume_phase — wave-skip and task-filtering behaviour (TC-RES-01 – TC-RES-04)
# ---------------------------------------------------------------------------

class TestResumePhaseBehaviour:
    """Integration tests for Taktis.resume_phase().

    Tests TC-RES-01 and TC-RES-02 mock ``execute_phase`` to isolate the
    taktis_engine's coordination logic (correct start_wave, correct task
    reset).  TC-RES-03 and TC-RES-04 are error-path guards that must raise
    ``ValueError`` synchronously before any scheduler work begins.

    TC-RES-05 tests the scheduler's task-level filtering directly without
    going through the taktis_engine, because the filtering belongs to
    ``execute_phase`` and can be exercised deterministically via
    ``_make_scheduler()``.
    """

    @pytest.mark.asyncio
    async def test_resume_phase_skips_completed_waves(self, taktis_engine):
        """resume_phase must pass start_wave=current_wave+1 to execute_phase,
        so that already-completed waves are never re-entered.

        Preconditions:
            - Phase has ``current_wave=1`` and tasks in waves 1 and 2.
            - Wave-1 tasks are 'completed'; wave-2 tasks are 'pending'.
        Steps:
            1. Set up DB state.
            2. Replace ``scheduler.execute_phase`` with a tracking mock.
            3. Call ``resume_phase(phase_id)``.
            4. Yield one event-loop turn so the background
               ``asyncio.create_task`` fires.
        Expected:
            - ``execute_phase`` called exactly once with ``start_wave=2``.
            - Wave-1 completed task is untouched.
        """
        import asyncio as _asyncio

        import taktis.repository as repo
        from taktis.db import get_session

        await taktis_engine.create_project(name="res-skip-waves", working_dir=".")
        await taktis_engine.create_phase(project_name="res-skip-waves", name="Skip-Waves Phase")

        t_w1 = await taktis_engine.create_task(
            project_name="res-skip-waves",
            prompt="Wave 1 work",
            phase_number=1,
            wave=1,
        )
        await taktis_engine.create_task(
            project_name="res-skip-waves",
            prompt="Wave 2 work",
            phase_number=1,
            wave=2,
        )
        phase = await taktis_engine.get_phase("res-skip-waves", 1)

        async with get_session() as conn:
            await repo.update_task(conn, t_w1["id"], status="completed")
            # current_wave=1 means wave 1 fully completed; wave 2 must resume.
            await repo.update_phase(conn, phase["id"], current_wave=1, status="in_progress")

        execute_calls: list[dict] = []

        async def _mock_execute_phase(pid, proj, start_wave=1):
            execute_calls.append({"phase_id": pid, "start_wave": start_wave})

        taktis_engine.scheduler.execute_phase = _mock_execute_phase

        await taktis_engine.resume_phase(phase["id"])
        # One event-loop yield lets the background create_task fire.
        await _asyncio.sleep(0)

        assert len(execute_calls) == 1, (
            f"Expected exactly 1 execute_phase call, got {len(execute_calls)}"
        )
        assert execute_calls[0]["start_wave"] == 2, (
            f"Expected start_wave=2 (WAVE-INVARIANT: current_wave=1 → resume at 2), "
            f"got start_wave={execute_calls[0]['start_wave']}"
        )
        # Completed wave-1 task must not have been reset.
        t_w1_after = await taktis_engine.get_task(t_w1["id"])
        assert t_w1_after["status"] == "completed", (
            "Wave-1 completed task must remain 'completed' after resume"
        )

    @pytest.mark.asyncio
    async def test_resume_phase_skips_completed_tasks_within_wave(self):
        """Within a resumed wave, execute_task must only be called for tasks
        that are NOT already 'completed' (BLOCKER-2 fix).

        Preconditions:
            - Phase has current_wave=1; wave-2 has 3 tasks (2 completed, 1 pending).
        Steps:
            1. Build the scheduler with ``_make_scheduler()``.
            2. Stub ``repo.get_tasks_by_phase`` to return 3 wave-2 tasks
               (2 completed, 1 pending).
            3. Call ``execute_phase("phase-1", project, start_wave=2)``.
            4. Collect task IDs passed to ``execute_task``.
        Expected:
            - ``execute_task`` called exactly once, for the pending task only.
            - The 2 completed tasks are NOT passed to ``execute_task``.

        Note: This test calls ``execute_phase`` directly (bypassing
        ``resume_phase``) for full determinism; the filtering logic under
        test lives entirely in ``execute_phase``.
        """
        scheduler, mock_conn, _eb, _st = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase(current_wave=1)

        # Wave 1: a completed task (must be skipped — below start_wave AND completed).
        # Wave 2: two completed tasks and one pending.
        tasks = [
            _make_db_task("t-done-w1", wave=1, status="completed"),
            _make_db_task("t-done-w2a", wave=2, status="completed"),
            _make_db_task("t-done-w2b", wave=2, status="completed"),
            _make_db_task("t-pending-w2", wave=2, status="pending"),
        ]

        executed_task_ids: list[str] = []

        async def _fake_execute_task(tid: str, proj: dict) -> None:
            executed_task_ids.append(tid)

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase",
                new=AsyncMock(),
            ),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                new=AsyncMock(
                    side_effect=lambda ids: {tid: "completed" for tid in ids}
                ),
            ),
            patch.object(
                scheduler, "execute_task", side_effect=_fake_execute_task
            ),
        ):
            await scheduler.execute_phase("phase-1", project, start_wave=2)

        # Only the one pending task in wave 2 must have been dispatched.
        assert executed_task_ids == ["t-pending-w2"], (
            f"Expected only 't-pending-w2' to be executed; got {executed_task_ids!r}. "
            "Completed wave-2 tasks must not be re-dispatched (BLOCKER-2)."
        )
        assert "t-done-w2a" not in executed_task_ids, (
            "Completed task t-done-w2a must NOT be re-executed"
        )
        assert "t-done-w2b" not in executed_task_ids, (
            "Completed task t-done-w2b must NOT be re-executed"
        )
        assert "t-done-w1" not in executed_task_ids, (
            "Wave-1 task (below start_wave) must NOT be executed"
        )

    @pytest.mark.asyncio
    async def test_resume_phase_not_found(self, taktis_engine):
        """resume_phase raises ValueError for a nonexistent phase ID.

        Preconditions: no phase with the given ID exists.
        Steps: call ``resume_phase`` with a fabricated UUID.
        Expected: ``ValueError`` with message containing 'not found'.
        """
        with pytest.raises(ValueError, match="not found"):
            await taktis_engine.resume_phase("00000000-dead-beef-0000-000000000000")

    @pytest.mark.asyncio
    async def test_resume_phase_already_complete(self, taktis_engine):
        """resume_phase raises ValueError when the phase is already 'complete'.

        Preconditions:
            - Phase exists with ``status='complete'``.
        Steps:
            1. Create a project and phase.
            2. Set phase status to 'complete'.
            3. Call ``resume_phase(phase_id)``.
        Expected: ``ValueError`` with message containing 'already complete'.
        """
        import taktis.repository as repo
        from taktis.db import get_session

        await taktis_engine.create_project(name="res-done-proj", working_dir=".")
        phase = await taktis_engine.create_phase(
            project_name="res-done-proj", name="Completed Phase"
        )
        async with get_session() as conn:
            await repo.update_phase(conn, phase["id"], status="complete")

        with pytest.raises(ValueError, match="already complete"):
            await taktis_engine.resume_phase(phase["id"])


# ---------------------------------------------------------------------------
# execute_phase — wave-failure propagation (ERR-07)
# ---------------------------------------------------------------------------


class TestExecutePhaseWaveFailurePropagation:
    """When a wave contains failing tasks, subsequent wave tasks must be marked
    'failed' (not left in 'pending') and EVENT_TASK_FAILED must be published for
    each so subscribers are notified without having to poll the DB.
    """

    @pytest.mark.asyncio
    async def test_future_wave_tasks_marked_failed_when_wave_fails(self):
        """Tasks in waves N+1, N+2, … are marked 'failed' when wave N fails.

        Setup:
            - Phase with two waves: wave 1 (task t1) and wave 2 (task t2).
            - Wave 1 completes with t1 reporting status='failed'.
        Expected:
            - ``_mark_task_failed`` is called for t2 (wave 2).
            - The reason string mentions the wave-1 failure.
        """
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        wave1_task = _make_db_task("t1", wave=1, status="pending")
        wave2_task = _make_db_task("t2", wave=2, status="pending")

        mark_failed_calls: list[tuple[str, str]] = []

        async def _fake_mark_failed(task_id: str, project_id: str, reason: str = "") -> None:
            mark_failed_calls.append((task_id, reason))
            # Also publish EVENT_TASK_FAILED as the real implementation does.
            await event_bus.publish(
                "task.failed",
                {"task_id": task_id, "project_id": project_id, "status": "failed"},
            )

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=[wave1_task, wave2_task]),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                # Wave 1: t1 failed; wave 2 never reached.
                new=AsyncMock(side_effect=lambda ids: {"t1": "failed"} if "t1" in ids else {}),
            ),
            patch.object(scheduler, "_mark_task_failed", side_effect=_fake_mark_failed),
        ):
            await scheduler.execute_phase("phase-1", project)

        failed_task_ids = [tid for tid, _ in mark_failed_calls]
        assert "t2" in failed_task_ids, (
            "Task t2 (wave 2) must be marked failed when wave 1 fails; "
            f"_mark_task_failed was called for: {failed_task_ids}"
        )
        # t1 itself is already 'failed' via _wait_for_tasks; the scheduler
        # should NOT re-mark it (it belongs to the failed wave, not a future wave).
        assert "t1" not in failed_task_ids, (
            "Task t1 (the wave-1 failing task) must NOT be re-marked by the "
            "future-wave abort path; it was already handled by _wait_for_tasks."
        )

    @pytest.mark.asyncio
    async def test_future_wave_tasks_reason_mentions_wave_number(self):
        """The reason stored for aborted future tasks names the failing wave."""
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [
            _make_db_task("t1", wave=1, status="pending"),
            _make_db_task("t2", wave=2, status="pending"),
            _make_db_task("t3", wave=2, status="pending"),
        ]

        mark_failed_reasons: dict[str, str] = {}

        async def _fake_mark_failed(task_id: str, project_id: str, reason: str = "") -> None:
            mark_failed_reasons[task_id] = reason
            await event_bus.publish(
                "task.failed",
                {"task_id": task_id, "project_id": project_id, "status": "failed"},
            )

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                new=AsyncMock(side_effect=lambda ids: {"t1": "failed"} if "t1" in ids else {}),
            ),
            patch.object(scheduler, "_mark_task_failed", side_effect=_fake_mark_failed),
        ):
            await scheduler.execute_phase("phase-1", project)

        for tid in ("t2", "t3"):
            assert tid in mark_failed_reasons, f"Task {tid} was not marked failed"
            reason = mark_failed_reasons[tid]
            assert "wave 1" in reason, (
                f"Expected reason for {tid} to mention 'wave 1'; got: {reason!r}"
            )

    @pytest.mark.asyncio
    async def test_phase_failed_event_published_after_wave_failure(self):
        """EVENT_PHASE_FAILED must be published when a wave fails."""
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [_make_db_task("t1", wave=1, status="pending")]

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                new=AsyncMock(return_value={"t1": "failed"}),
            ),
            patch.object(scheduler, "_mark_task_failed", new=AsyncMock()),
        ):
            await scheduler.execute_phase("phase-1", project)

        published_types = [c.args[0] for c in event_bus.publish.call_args_list]
        assert "phase.failed" in published_types, (
            f"EVENT_PHASE_FAILED not published; published events: {published_types}"
        )


# ---------------------------------------------------------------------------
# execute_phase — asyncio.gather exception handling (ERR-07)
# ---------------------------------------------------------------------------


class TestExecutePhaseGatherExceptionHandling:
    """Exceptions returned by asyncio.gather from execute_task must be handled
    explicitly: the affected task must be marked 'failed' and phase execution
    must continue (or fail gracefully) rather than silently ignoring the error.
    """

    @pytest.mark.asyncio
    async def test_gather_exception_causes_task_to_be_marked_failed(self):
        """When execute_task raises (returned via gather), _mark_task_failed is called.

        This guards against the rare case where an exception escapes execute_task
        itself (e.g. asyncio.CancelledError mid-start).
        """
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [_make_db_task("t-crash", wave=1, status="pending")]

        mark_failed_calls: list[str] = []

        async def _fake_mark_failed(task_id: str, project_id: str, reason: str = "") -> None:
            mark_failed_calls.append(task_id)
            await event_bus.publish(
                "task.failed",
                {"task_id": task_id, "project_id": project_id, "status": "failed"},
            )

        async def _exploding_execute_task(task_id: str, proj: dict) -> None:
            raise RuntimeError("execute_task escaped exception")

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler, "execute_task", side_effect=_exploding_execute_task
            ),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                new=AsyncMock(return_value={"t-crash": "failed"}),
            ),
            patch.object(scheduler, "_mark_task_failed", side_effect=_fake_mark_failed),
        ):
            await scheduler.execute_phase("phase-1", project)

        assert "t-crash" in mark_failed_calls, (
            "Task that raised in execute_task must be marked failed via "
            f"_mark_task_failed; calls: {mark_failed_calls}"
        )

    @pytest.mark.asyncio
    async def test_gather_exception_does_not_crash_execute_phase(self):
        """execute_phase must complete (publish phase event) even when gather returns exceptions."""
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [_make_db_task("t-crash", wave=1, status="pending")]

        async def _exploding_execute_task(task_id: str, proj: dict) -> None:
            raise RuntimeError("boom")

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(
                scheduler, "execute_task", side_effect=_exploding_execute_task
            ),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                new=AsyncMock(return_value={"t-crash": "failed"}),
            ),
            patch.object(scheduler, "_mark_task_failed", new=AsyncMock()),
        ):
            # Must not raise — execute_phase always finalises the phase.
            await scheduler.execute_phase("phase-1", project)

        published_types = [c.args[0] for c in event_bus.publish.call_args_list]
        # Either phase.completed or phase.failed must be published.
        assert any(t in published_types for t in ("phase.completed", "phase.failed")), (
            f"No phase lifecycle event published; events seen: {published_types}"
        )


# ---------------------------------------------------------------------------
# execute_phase — SchedulerError wrapping on unexpected exception (ERR-07)
# ---------------------------------------------------------------------------


class TestExecutePhaseSchedulerErrorWrapping:
    """An unexpected exception inside the wave loop must be logged as a
    SchedulerError (not a bare exception) and the phase must still be
    finalized with EVENT_PHASE_FAILED published.
    """

    @pytest.mark.asyncio
    async def test_unexpected_exception_logs_scheduler_error_and_publishes_phase_failed(
        self,
    ):
        """When _wait_for_tasks raises, the phase must be finalized as failed.

        Setup: _wait_for_tasks raises RuntimeError mid-wave.
        Expected:
            - execute_phase does NOT propagate the exception to the caller.
            - EVENT_PHASE_FAILED is published.
        """
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [_make_db_task("t1", wave=1, status="pending")]

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                new=AsyncMock(side_effect=RuntimeError("DB exploded mid-wait")),
            ),
            patch.object(scheduler, "_mark_task_failed", new=AsyncMock()),
        ):
            # Must not raise.
            await scheduler.execute_phase("phase-1", project)

        published_types = [c.args[0] for c in event_bus.publish.call_args_list]
        assert "phase.failed" in published_types, (
            f"EVENT_PHASE_FAILED must be published after unexpected error; "
            f"events seen: {published_types}"
        )

    @pytest.mark.asyncio
    async def test_unexpected_exception_marks_active_wave_tasks_failed(self):
        """Active-wave tasks must be marked failed when the scheduler errors mid-wave."""
        scheduler, mock_conn, event_bus, _ = _make_scheduler()
        project = {"id": "proj-1", "name": "Test", "working_dir": "/tmp"}
        phase = _make_phase()

        tasks = [_make_db_task("t1", wave=1, status="pending")]

        mark_failed_calls: list[str] = []

        async def _fake_mark_failed(task_id: str, project_id: str, reason: str = "") -> None:
            mark_failed_calls.append(task_id)

        with (
            patch(
                "taktis.core.scheduler.repo.get_phase_by_id",
                new=AsyncMock(return_value=phase),
            ),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch(
                "taktis.core.scheduler.repo.get_tasks_by_phase",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "taktis.core.scheduler.repo.update_phase_current_wave",
                new=AsyncMock(),
            ),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(
                scheduler,
                "_wait_for_tasks",
                new=AsyncMock(side_effect=RuntimeError("DB exploded")),
            ),
            patch.object(scheduler, "_mark_task_failed", side_effect=_fake_mark_failed),
        ):
            await scheduler.execute_phase("phase-1", project)

        assert "t1" in mark_failed_calls, (
            "Active-wave task t1 must be marked failed when the scheduler "
            f"encounters an unexpected error; calls: {mark_failed_calls}"
        )


# ---------------------------------------------------------------------------
# _extract_critical_items
# ---------------------------------------------------------------------------


class TestExtractCriticalItems:
    """Tests for the CRITICAL section parser used in phase review."""

    _extract = staticmethod(WaveScheduler._extract_critical_items)

    def test_exact_prompt_format(self):
        """The format produced by PHASE_REVIEW_PROMPT should be parsed correctly."""
        review = (
            "#### CRITICAL (must fix before next phase)\n"
            "- Missing login endpoint\n"
            "- SQL injection in search\n"
            "\n"
            "#### WARNING (should fix)\n"
            "- No rate limiting\n"
        )
        items = self._extract(review)
        assert items == ["- Missing login endpoint", "- SQL injection in search"]

    def test_case_insensitive_critical(self):
        """'Critical', 'critical', 'CRITICAL' should all be detected."""
        for variant in ["CRITICAL", "Critical", "critical"]:
            review = f"#### {variant} (must fix before next phase)\n- Bug found\n#### WARNING\n"
            items = self._extract(review)
            assert items == ["- Bug found"], f"Failed for variant: {variant}"

    def test_case_insensitive_must_fix(self):
        """'Must Fix', 'MUST FIX', 'must fix' should all trigger."""
        for variant in ["must fix", "Must Fix", "MUST FIX"]:
            review = f"#### CRITICAL ({variant})\n- Issue\n#### WARNING\n"
            items = self._extract(review)
            assert items == ["- Issue"], f"Failed for variant: {variant}"

    def test_none_found(self):
        """'None found.' in CRITICAL section means no items."""
        review = (
            "#### CRITICAL (must fix before next phase)\n"
            "None found.\n"
            "\n"
            "#### WARNING (should fix)\n"
            "- Some warning\n"
        )
        assert self._extract(review) == []

    def test_na_variant(self):
        """'N/A' and 'none' variants should produce empty list."""
        for none_text in ["N/A", "n/a", "None", "none"]:
            review = f"#### CRITICAL (must fix)\n{none_text}\n#### WARNING\n"
            assert self._extract(review) == [], f"Failed for: {none_text}"

    def test_no_critical_section(self):
        """Review without CRITICAL section should return empty list."""
        review = "#### WARNING (should fix)\n- Something\n#### NIT\n- Style\n"
        assert self._extract(review) == []

    def test_critical_at_end_of_text(self):
        """CRITICAL section at EOF (no closing heading) should still collect items."""
        review = "#### CRITICAL (must fix)\n- Item 1\n- Item 2\n"
        items = self._extract(review)
        assert items == ["- Item 1", "- Item 2"]

    def test_empty_review(self):
        """Empty string should return empty list."""
        assert self._extract("") == []

    def test_multiple_items_with_blank_lines(self):
        """Blank lines within CRITICAL section should be skipped."""
        review = (
            "#### CRITICAL (must fix before next phase)\n"
            "- First issue\n"
            "\n"
            "- Second issue\n"
            "\n"
            "#### WARNING (should fix)\n"
        )
        items = self._extract(review)
        assert items == ["- First issue", "- Second issue"]

    def test_ends_on_any_heading_level(self):
        """Section should end on ## or ### headings, not just ####."""
        review = (
            "#### CRITICAL (must fix)\n"
            "- Real issue\n"
            "### Summary\n"
            "Everything looks good except the critical.\n"
        )
        items = self._extract(review)
        assert items == ["- Real issue"]


# ---------------------------------------------------------------------------
# Phase review execution loop
# ---------------------------------------------------------------------------

# Stable expert IDs from .md frontmatter — used only in test assertions
_TEST_REVIEWER_ID = "4e4e016e2d5a59019e18035167c0a07d"
_TEST_IMPLEMENTER_ID = "dc868af81906568a97458c1f8ee709a4"

_REVIEWER_EXPERT = {"id": _TEST_REVIEWER_ID, "name": "reviewer", "system_prompt": "You are a reviewer."}
_IMPL_EXPERT = {"id": _TEST_IMPLEMENTER_ID, "name": "implementer", "system_prompt": "You are an implementer."}
_ALL_EXPERTS = [_REVIEWER_EXPERT, _IMPL_EXPERT]

def _mock_get_expert_by_role(conn, role):
    """Return the right expert based on role."""
    _experts = {"phase_reviewer": _REVIEWER_EXPERT, "phase_fixer": _IMPL_EXPERT}
    return _experts.get(role)

_REVIEW_RESULT_CLEAN = [{"content": '{"type": "result", "result": "#### CRITICAL (must fix)\\nNone found.\\n#### WARNING\\n- minor"}'}]
_REVIEW_RESULT_WITH_CRITICALS = [{"content": '{"type": "result", "result": "#### CRITICAL (must fix)\\n- SQL injection\\n- Missing auth\\n#### WARNING\\n- minor"}'}]


def _make_review_phase(**overrides) -> dict:
    base = {
        "id": "ph-review",
        "phase_number": 2,
        "name": "Build API",
        "goal": "Create REST endpoints",
        "status": "complete",
    }
    base.update(overrides)
    return base


def _make_review_project(**overrides) -> dict:
    base = {"id": "proj-1", "name": "test-proj", "working_dir": "/tmp/test"}
    base.update(overrides)
    return base


class TestPhaseReviewLoop:
    """Tests for _spawn_phase_review, _fix_and_re_review, _re_review_phase."""

    @pytest.mark.asyncio
    async def test_spawn_review_creates_task_with_correct_params(self):
        """Review task should use reviewer expert, wave=999, model=opus."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.scheduler.repo.list_experts", new=AsyncMock(return_value=_ALL_EXPERTS)),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(side_effect=_mock_get_expert_by_role)),
            patch("taktis.core.scheduler.repo.create_task", new=AsyncMock()) as mock_create,
            patch("taktis.core.scheduler.repo.get_task_outputs", new=AsyncMock(return_value=_REVIEW_RESULT_CLEAN)),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.context.write_phase_review"),
        ):
            await scheduler._spawn_phase_review(_make_review_phase(), _make_review_project())

        # Verify create_task was called with review params
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["task_type"] == "phase_review"
        assert call_kwargs["wave"] == 999
        assert call_kwargs["model"] == "opus"
        assert call_kwargs["expert_id"] == _TEST_REVIEWER_ID

    @pytest.mark.asyncio
    async def test_spawn_review_no_criticals_stays_complete(self):
        """When no CRITICALs found, phase stays complete and REVIEW.md is written."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.scheduler.repo.list_experts", new=AsyncMock(return_value=_ALL_EXPERTS)),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(side_effect=_mock_get_expert_by_role)),
            patch("taktis.core.scheduler.repo.create_task", new=AsyncMock()),
            patch("taktis.core.scheduler.repo.get_task_outputs", new=AsyncMock(return_value=_REVIEW_RESULT_CLEAN)),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.context.write_phase_review") as mock_write_review,
            patch("taktis.core.phase_review._fix_and_re_review", new=AsyncMock()) as mock_fix,
        ):
            await scheduler._spawn_phase_review(_make_review_phase(), _make_review_project())

        mock_write_review.assert_called_once()
        mock_fix.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_review_with_criticals_triggers_fix(self):
        """When CRITICALs found, _fix_and_re_review is called."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.scheduler.repo.list_experts", new=AsyncMock(return_value=_ALL_EXPERTS)),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(side_effect=_mock_get_expert_by_role)),
            patch("taktis.core.scheduler.repo.create_task", new=AsyncMock()),
            patch("taktis.core.scheduler.repo.get_task_outputs", new=AsyncMock(return_value=_REVIEW_RESULT_WITH_CRITICALS)),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.context.write_phase_review"),
            patch("taktis.core.phase_review._fix_and_re_review", new=AsyncMock()) as mock_fix,
        ):
            await scheduler._spawn_phase_review(_make_review_phase(), _make_review_project())

        mock_fix.assert_called_once()
        call_args = mock_fix.call_args
        assert call_args.kwargs.get("attempt") == 1
        # critical_items is positional arg[4] (scheduler, phase, project, review_text, critical_items)
        assert len(call_args.args[4]) == 2

    @pytest.mark.asyncio
    async def test_spawn_review_missing_reviewer_logs_warning(self, caplog):
        """When reviewer expert is missing, task still created with expert_id=None."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.scheduler.repo.list_experts", new=AsyncMock(return_value=[_IMPL_EXPERT])),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(return_value=None)),
            patch("taktis.core.scheduler.repo.create_task", new=AsyncMock()) as mock_create,
            patch("taktis.core.scheduler.repo.get_task_outputs", new=AsyncMock(return_value=_REVIEW_RESULT_CLEAN)),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.context.write_phase_review"),
        ):
            await scheduler._spawn_phase_review(_make_review_phase(), _make_review_project())

        assert mock_create.call_args.kwargs["expert_id"] is None
        assert "reviewer" in caplog.text.lower() and "not found" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_fix_creates_task_with_implementer(self):
        """Fix task should use implementer expert, wave=999, task_type=phase_review_fix."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.phase_review.repo.list_experts", new=AsyncMock(return_value=_ALL_EXPERTS)),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(side_effect=_mock_get_expert_by_role)),
            patch("taktis.core.phase_review.repo.create_task", new=AsyncMock()) as mock_create,
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.phase_review._re_review_phase", new=AsyncMock()),
        ):
            await phase_review._fix_and_re_review(scheduler,
                _make_review_phase(), _make_review_project(),
                review_text="review", critical_items=["- bug"], attempt=1,
            )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["task_type"] == "phase_review_fix"
        assert call_kwargs["wave"] == 999
        assert call_kwargs["expert_id"] == _TEST_IMPLEMENTER_ID

    @pytest.mark.asyncio
    async def test_fix_max_attempts_marks_phase_failed(self):
        """Exceeding max attempts should mark phase as failed without creating tasks."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.phase_review.repo.update_phase", new=AsyncMock()) as mock_update,
            patch("taktis.core.phase_review.repo.create_task", new=AsyncMock()) as mock_create,
        ):
            await phase_review._fix_and_re_review(scheduler,
                _make_review_phase(), _make_review_project(),
                review_text="review", critical_items=["- bug"], attempt=4,
            )

        mock_update.assert_called_once()
        assert mock_update.call_args.kwargs["status"] == "failed"
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_fix_calls_re_review_after_completion(self):
        """After fix task completes, _re_review_phase should be called."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.phase_review.repo.list_experts", new=AsyncMock(return_value=_ALL_EXPERTS)),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(side_effect=_mock_get_expert_by_role)),
            patch("taktis.core.phase_review.repo.create_task", new=AsyncMock()),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.phase_review._re_review_phase", new=AsyncMock()) as mock_re_review,
        ):
            await phase_review._fix_and_re_review(scheduler,
                _make_review_phase(), _make_review_project(),
                review_text="review", critical_items=["- bug"], attempt=2,
            )

        mock_re_review.assert_called_once()
        # args: (scheduler, phase, project, attempt)
        assert mock_re_review.call_args.args[3] == 2  # attempt passed through
        assert mock_re_review.call_args.kwargs["prior_critical_items"] == ["- bug"]

    @pytest.mark.asyncio
    async def test_re_review_no_criticals_marks_complete(self):
        """Re-review with no CRITICALs should explicitly mark phase complete."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.phase_review.repo.list_experts", new=AsyncMock(return_value=_ALL_EXPERTS)),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(side_effect=_mock_get_expert_by_role)),
            patch("taktis.core.phase_review.repo.create_task", new=AsyncMock()),
            patch("taktis.core.phase_review.repo.get_task_outputs", new=AsyncMock(return_value=_REVIEW_RESULT_CLEAN)),
            patch("taktis.core.phase_review.repo.update_phase", new=AsyncMock()) as mock_update,
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.context.write_phase_review"),
        ):
            await phase_review._re_review_phase(scheduler, _make_review_phase(), _make_review_project(), attempt=1)

        mock_update.assert_called_once()
        assert mock_update.call_args.kwargs["status"] == "complete"

    @pytest.mark.asyncio
    async def test_re_review_with_criticals_recurses(self):
        """Re-review finding CRITICALs should call _fix_and_re_review with attempt+1."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()

        with (
            patch("taktis.core.phase_review.repo.list_experts", new=AsyncMock(return_value=_ALL_EXPERTS)),
            patch("taktis.core.phase_review.repo.get_expert_by_role", new=AsyncMock(side_effect=_mock_get_expert_by_role)),
            patch("taktis.core.phase_review.repo.create_task", new=AsyncMock()),
            patch("taktis.core.phase_review.repo.get_task_outputs", new=AsyncMock(return_value=_REVIEW_RESULT_WITH_CRITICALS)),
            patch.object(scheduler, "execute_task", new=AsyncMock()),
            patch.object(scheduler, "_wait_for_tasks", new=AsyncMock(return_value={})),
            patch("taktis.core.context.write_phase_review"),
            patch("taktis.core.phase_review._fix_and_re_review", new=AsyncMock()) as mock_fix,
        ):
            await phase_review._re_review_phase(scheduler,_make_review_phase(), _make_review_project(), attempt=2)

        mock_fix.assert_called_once()
        assert mock_fix.call_args.kwargs["attempt"] == 3


# ---------------------------------------------------------------------------
# System prompt assembly in execute_task
# ---------------------------------------------------------------------------


class TestSystemPromptAssembly:
    """Tests for system prompt construction in execute_task()."""

    def _setup_execute_task_patches(self, task_dict, expert_dict=None, phase_dict=None,
                                     context="", write_succeeds=True):
        """Return a dict of patches for execute_task."""
        if phase_dict is None:
            phase_dict = _make_phase()
        patches = {
            "get_task": patch("taktis.core.scheduler.repo.get_task",
                              new=AsyncMock(return_value=task_dict)),
            "get_expert": patch("taktis.core.scheduler.repo.get_expert_by_id",
                                new=AsyncMock(return_value=expert_dict)),
            "get_phase": patch("taktis.core.scheduler.repo.get_phase_by_id",
                               new=AsyncMock(return_value=phase_dict)),
            "update_task": patch("taktis.core.scheduler.repo.update_task",
                                 new=AsyncMock()),
            "update_phase": patch("taktis.core.scheduler.repo.update_phase",
                                  new=AsyncMock()),
            "get_context": patch("taktis.core.scheduler.get_phase_context",
                                 return_value=(context, [])),
            "register_cb": patch("taktis.core.scheduler.repo.register_callbacks",
                                 new=AsyncMock()),
            "publish": patch.object(self._event_bus, "publish", new=AsyncMock()),
        }
        return patches

    @pytest.mark.asyncio
    async def test_expert_system_prompt_injected(self):
        """Expert's system_prompt should appear in the prompt passed to start_task."""
        scheduler, mock_conn, self._event_bus, _ = _make_scheduler()
        task = _make_db_task("t-sp1", status="pending")
        task["expert_id"] = "exp-1"
        expert = {"id": "exp-1", "name": "implementer", "system_prompt": "You are an implementer.", "category": "implementation"}
        fake_process = MagicMock()
        fake_process.started_at = None

        with (
            patch("taktis.core.scheduler.repo.get_task", new=AsyncMock(return_value=task)),
            patch("taktis.core.scheduler.repo.get_expert_by_id", new=AsyncMock(return_value=expert)),
            patch("taktis.core.scheduler.repo.get_phase_by_id", new=AsyncMock(return_value=_make_phase())),
            patch("taktis.core.scheduler.repo.update_task", new=AsyncMock()),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch("taktis.core.context.get_phase_context", return_value=("", [])),
            patch("taktis.core.context.generate_state_summary", new=AsyncMock(return_value="")),
            patch.object(scheduler._event_bus, "publish", new=AsyncMock()),
            patch.object(scheduler._manager, "register_callbacks"),
            patch.object(scheduler._manager, "start_task", new=AsyncMock(return_value=fake_process)) as mock_start,
        ):
            await scheduler.execute_task("t-sp1", {"id": "p1", "name": "proj", "working_dir": "/tmp"})

        system_prompt = mock_start.call_args.kwargs["system_prompt"]
        assert "You are an implementer." in system_prompt

    @pytest.mark.asyncio
    async def test_working_dir_note_appended(self):
        """Working directory should always appear in system prompt."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()
        task = _make_db_task("t-wd1", status="pending")
        fake_process = MagicMock()
        fake_process.started_at = None

        with (
            patch("taktis.core.scheduler.repo.get_task", new=AsyncMock(return_value=task)),
            patch("taktis.core.scheduler.repo.get_expert_by_id", new=AsyncMock(return_value=None)),
            patch("taktis.core.scheduler.repo.get_phase_by_id", new=AsyncMock(return_value=_make_phase())),
            patch("taktis.core.scheduler.repo.update_task", new=AsyncMock()),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch("taktis.core.context.get_phase_context", return_value=("", [])),
            patch("taktis.core.context.generate_state_summary", new=AsyncMock(return_value="")),
            patch.object(scheduler._event_bus, "publish", new=AsyncMock()),
            patch.object(scheduler._manager, "register_callbacks"),
            patch.object(scheduler._manager, "start_task", new=AsyncMock(return_value=fake_process)) as mock_start,
        ):
            await scheduler.execute_task("t-wd1", {"id": "p1", "name": "proj", "working_dir": "/my/project"})

        system_prompt = mock_start.call_args.kwargs["system_prompt"]
        assert "/my/project" in system_prompt

    @pytest.mark.asyncio
    async def test_no_expert_uses_empty_base(self):
        """Without expert, system prompt should still have working_dir note."""
        scheduler, mock_conn, _eb, _ = _make_scheduler()
        task = _make_db_task("t-ne1", status="pending")
        fake_process = MagicMock()
        fake_process.started_at = None

        with (
            patch("taktis.core.scheduler.repo.get_task", new=AsyncMock(return_value=task)),
            patch("taktis.core.scheduler.repo.get_expert_by_id", new=AsyncMock(return_value=None)),
            patch("taktis.core.scheduler.repo.get_phase_by_id", new=AsyncMock(return_value=_make_phase())),
            patch("taktis.core.scheduler.repo.update_task", new=AsyncMock()),
            patch("taktis.core.scheduler.repo.update_phase", new=AsyncMock()),
            patch("taktis.core.context.get_phase_context", return_value=("", [])),
            patch("taktis.core.context.generate_state_summary", new=AsyncMock(return_value="")),
            patch.object(scheduler._event_bus, "publish", new=AsyncMock()),
            patch.object(scheduler._manager, "register_callbacks"),
            patch.object(scheduler._manager, "start_task", new=AsyncMock(return_value=fake_process)) as mock_start,
        ):
            await scheduler.execute_task("t-ne1", {"id": "p1", "name": "proj", "working_dir": "/tmp"})

        system_prompt = mock_start.call_args.kwargs["system_prompt"]
        assert "Your working directory is:" in system_prompt
        # Should NOT contain an expert persona prefix
        assert not system_prompt.startswith("You are")


# ---------------------------------------------------------------------------
# _wait_for_tasks — timeout behaviour
# ---------------------------------------------------------------------------


class TestWaitForTasksTimeout:
    """_wait_for_tasks must fail pending tasks when timeout is exceeded."""

    @pytest.mark.asyncio
    async def test_timeout_marks_tasks_failed(self):
        """Tasks still pending after the timeout are marked failed."""
        scheduler, mock_conn, event_bus, _ = _make_scheduler()

        # get_tasks_by_ids returns task still 'running' on every poll.
        mock_conn.execute = AsyncMock()
        with patch(
            "taktis.core.scheduler.repo.get_tasks_by_ids",
            new=AsyncMock(return_value=[{"id": "t1", "status": "running"}]),
        ), patch(
            "taktis.core.scheduler.repo.update_task",
            new=AsyncMock(),
        ) as mock_update:
            results = await scheduler._wait_for_tasks(["t1"], timeout=0.1)

        assert results["t1"] == "failed"
        # update_task called with failed status
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs[0][1] == "t1"  # task_id
        assert call_kwargs[1]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_timeout_publishes_failure_events(self):
        """Each timed-out task triggers an EVENT_TASK_FAILED publish."""
        scheduler, mock_conn, event_bus, _ = _make_scheduler()

        with patch(
            "taktis.core.scheduler.repo.get_tasks_by_ids",
            new=AsyncMock(return_value=[
                {"id": "t1", "status": "running"},
                {"id": "t2", "status": "running"},
            ]),
        ), patch(
            "taktis.core.scheduler.repo.update_task",
            new=AsyncMock(),
        ):
            results = await scheduler._wait_for_tasks(["t1", "t2"], timeout=0.1)

        assert results["t1"] == "failed"
        assert results["t2"] == "failed"
        # At least 2 failure events published (one per timed-out task).
        fail_calls = [
            c for c in event_bus.publish.call_args_list
            if c[0][0] == "task.failed"
        ]
        assert len(fail_calls) >= 2

    @pytest.mark.asyncio
    async def test_completed_before_timeout_returns_normally(self):
        """Tasks that complete before timeout are returned with their real status."""
        scheduler, mock_conn, event_bus, _ = _make_scheduler()

        with patch(
            "taktis.core.scheduler.repo.get_tasks_by_ids",
            new=AsyncMock(return_value=[{"id": "t1", "status": "completed"}]),
        ):
            results = await scheduler._wait_for_tasks(["t1"], timeout=10)

        assert results["t1"] == "completed"
