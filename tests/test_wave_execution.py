"""Integration tests for concurrent wave execution in WaveScheduler.

Tests the full execute_phase flow with real DB but mocked ProcessManager,
verifying: multi-wave sequencing, failure propagation to subsequent waves,
checkpoint-based resume, and concurrent task completion ordering.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import taktis.db as db_mod
from taktis.core.engine import Taktis
from taktis.core.events import EVENT_PHASE_COMPLETED, EVENT_PHASE_FAILED


@pytest_asyncio.fixture
async def orch_env(tmp_path, _golden_db_path):
    """Taktis + working dir for wave execution tests."""
    import shutil
    from unittest.mock import AsyncMock, patch

    db_file = str(tmp_path / "test.db")
    original_path = db_mod.DATABASE_PATH

    shutil.copy2(_golden_db_path, db_file)
    db_mod.DATABASE_PATH = db_file

    orch = Taktis()
    with patch(
        "taktis.core.experts.ExpertRegistry.load_builtins",
        new_callable=AsyncMock,
    ), patch(
        "taktis.core.agent_templates.AgentTemplateRegistry.load_builtins",
        new_callable=AsyncMock,
    ):
        await orch.initialize()

    work_dir = tmp_path / "workdir"
    work_dir.mkdir()

    try:
        yield orch, str(work_dir)
    finally:
        await orch.shutdown()
        db_mod.DATABASE_PATH = original_path


async def _setup_two_wave_phase(orch, work_dir):
    """Create a project with 1 phase: 2 tasks in wave 1, 1 task in wave 2."""
    await orch.create_project(name="wave-proj", working_dir=work_dir)
    await orch.create_phase(
        project_name="wave-proj", name="Phase 1", goal="Test waves",
    )
    t1 = await orch.create_task(
        project_name="wave-proj", prompt="Task A", phase_number=1, wave=1,
    )
    t2 = await orch.create_task(
        project_name="wave-proj", prompt="Task B", phase_number=1, wave=1,
    )
    t3 = await orch.create_task(
        project_name="wave-proj", prompt="Task C (wave 2)",
        phase_number=1, wave=2,
    )
    return t1, t2, t3


class TestWaveExecution:

    @pytest.mark.asyncio
    async def test_two_wave_success(self, orch_env):
        """Wave 1 tasks complete, then wave 2 runs — phase marked complete."""
        orch, work_dir = orch_env
        t1, t2, t3 = await _setup_two_wave_phase(orch, work_dir)

        completed_events = []
        q = orch.event_bus.subscribe(EVENT_PHASE_COMPLETED)

        # Mock execute_task to immediately mark tasks as completed
        original_execute = orch.scheduler.execute_task

        async def mock_execute(task_id, project):
            from taktis.db import get_session
            from taktis import repository as repo
            async with get_session() as conn:
                await repo.update_task(conn, task_id, status="completed")
            await orch.event_bus.publish(
                "task:completed",
                {"task_id": task_id, "project_id": project["id"], "status": "completed"},
            )

        orch.scheduler.execute_task = mock_execute

        project = await orch.get_project("wave-proj")
        phases = await orch.list_phases("wave-proj")
        phase_id = phases[0]["id"]

        await orch.scheduler.execute_phase(phase_id, project)

        # Verify phase is complete
        from taktis.db import get_session
        from taktis import repository as repo
        async with get_session() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
        assert phase["status"] == "complete"

        # Verify all tasks are completed
        async with get_session() as conn:
            for tid in [t1["id"], t2["id"], t3["id"]]:
                task = await repo.get_task(conn, tid)
                assert task["status"] == "completed", f"Task {tid} should be completed"

    @pytest.mark.asyncio
    async def test_wave1_failure_aborts_wave2(self, orch_env):
        """If a wave 1 task fails, wave 2 tasks should be marked failed."""
        orch, work_dir = orch_env
        t1, t2, t3 = await _setup_two_wave_phase(orch, work_dir)

        async def mock_execute(task_id, project):
            from taktis.db import get_session
            from taktis import repository as repo
            # t1 succeeds, t2 fails
            if task_id == t2["id"]:
                status = "failed"
            else:
                status = "completed"
            async with get_session() as conn:
                await repo.update_task(conn, task_id, status=status)
            evt = "task:completed" if status == "completed" else "task:failed"
            await orch.event_bus.publish(
                evt,
                {"task_id": task_id, "project_id": project["id"], "status": status},
            )

        orch.scheduler.execute_task = mock_execute

        project = await orch.get_project("wave-proj")
        phases = await orch.list_phases("wave-proj")
        phase_id = phases[0]["id"]

        await orch.scheduler.execute_phase(phase_id, project)

        # Phase should be failed
        from taktis.db import get_session
        from taktis import repository as repo
        async with get_session() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
        assert phase["status"] == "failed"

        # Wave 2 task (t3) should be marked failed
        async with get_session() as conn:
            t3_updated = await repo.get_task(conn, t3["id"])
        assert t3_updated["status"] == "failed"
        assert "Aborted" in (t3_updated.get("result_summary") or "")

    @pytest.mark.asyncio
    async def test_wave_checkpoint_written_on_success(self, orch_env):
        """After wave 1 succeeds, current_wave should be checkpointed."""
        orch, work_dir = orch_env
        t1, t2, t3 = await _setup_two_wave_phase(orch, work_dir)

        async def mock_execute(task_id, project):
            from taktis.db import get_session
            from taktis import repository as repo
            async with get_session() as conn:
                await repo.update_task(conn, task_id, status="completed")
            await orch.event_bus.publish(
                "task:completed",
                {"task_id": task_id, "project_id": project["id"], "status": "completed"},
            )

        orch.scheduler.execute_task = mock_execute

        project = await orch.get_project("wave-proj")
        phases = await orch.list_phases("wave-proj")
        phase_id = phases[0]["id"]

        await orch.scheduler.execute_phase(phase_id, project)

        # current_wave should be 2 (last fully completed wave)
        from taktis.db import get_session
        from taktis import repository as repo
        async with get_session() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
        assert phase["current_wave"] == 2

    @pytest.mark.asyncio
    async def test_tasks_complete_in_any_order(self, orch_env):
        """Tasks in the same wave can complete in any order."""
        orch, work_dir = orch_env

        await orch.create_project(name="order-proj", working_dir=work_dir)
        await orch.create_phase(
            project_name="order-proj", name="Phase 1", goal="Order test",
        )
        tasks = []
        for i in range(5):
            t = await orch.create_task(
                project_name="order-proj",
                prompt=f"Task {i}",
                phase_number=1,
                wave=1,
            )
            tasks.append(t)

        completion_order = []

        async def mock_execute(task_id, project):
            from taktis.db import get_session
            from taktis import repository as repo
            # Stagger completions with different delays
            idx = next(i for i, t in enumerate(tasks) if t["id"] == task_id)
            await asyncio.sleep(0.01 * (5 - idx))  # reverse order
            completion_order.append(task_id)
            async with get_session() as conn:
                await repo.update_task(conn, task_id, status="completed")
            await orch.event_bus.publish(
                "task:completed",
                {"task_id": task_id, "project_id": project["id"], "status": "completed"},
            )

        orch.scheduler.execute_task = mock_execute

        project = await orch.get_project("order-proj")
        phases = await orch.list_phases("order-proj")
        phase_id = phases[0]["id"]

        await orch.scheduler.execute_phase(phase_id, project)

        # All 5 tasks should have completed
        assert len(completion_order) == 5

        # Phase should be complete
        from taktis.db import get_session
        from taktis import repository as repo
        async with get_session() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
        assert phase["status"] == "complete"
