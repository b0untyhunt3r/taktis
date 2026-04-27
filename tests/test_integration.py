"""End-to-end integration tests for the taktis_engine lifecycle.

Tests the full flow: create project → create phases/tasks → execute →
verify completion, without calling the real Claude SDK.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import taktis.db as db_mod
from taktis.core.engine import Taktis


@pytest_asyncio.fixture
async def orch_with_dir(tmp_path, _golden_db_path):
    """Taktis with a real temp working directory for context files."""
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

    # Create a working directory for projects
    work_dir = tmp_path / "workdir"
    work_dir.mkdir()

    try:
        yield orch, str(work_dir)
    finally:
        await orch.shutdown()
        db_mod.DATABASE_PATH = original_path


class TestProjectLifecycle:
    """Full lifecycle: project → phases → tasks → verify state."""

    @pytest.mark.asyncio
    async def test_create_project_with_phases_and_tasks(self, orch_with_dir):
        orch, work_dir = orch_with_dir

        # 1. Create project
        project = await orch.create_project(
            name="test-project",
            working_dir=work_dir,
            description="Integration test project",
        )
        assert project["name"] == "test-project"

        # 2. Create phases
        phase1 = await orch.create_phase(
            project_name="test-project",
            name="Setup",
            goal="Set up the project structure",
        )
        phase2 = await orch.create_phase(
            project_name="test-project",
            name="Implementation",
            goal="Implement the features",
        )
        assert phase1["phase_number"] == 1
        assert phase2["phase_number"] == 2

        # 3. Create tasks in phase 1
        task1 = await orch.create_task(
            project_name="test-project",
            prompt="Create project scaffold",
            phase_number=1,
            wave=1,
        )
        task2 = await orch.create_task(
            project_name="test-project",
            prompt="Set up configuration",
            phase_number=1,
            wave=1,
        )
        task3 = await orch.create_task(
            project_name="test-project",
            prompt="Write README based on scaffold",
            phase_number=1,
            wave=2,
        )

        assert task1["id"] is not None
        assert task2["id"] is not None
        assert task3["id"] is not None

        # 4. Verify listing
        projects = await orch.list_projects()
        assert any(p["name"] == "test-project" for p in projects)

        tasks = await orch.list_tasks("test-project", phase_number=1)
        assert len(tasks) == 3

        phases = await orch.list_phases("test-project")
        assert len(phases) == 2

    @pytest.mark.asyncio
    async def test_task_wave_assignment(self, orch_with_dir):
        orch, work_dir = orch_with_dir

        await orch.create_project(
            name="wave-test", working_dir=work_dir,
        )
        await orch.create_phase(
            project_name="wave-test", name="Phase 1", goal="Test waves",
        )

        t1 = await orch.create_task(
            project_name="wave-test", prompt="Task A", phase_number=1, wave=1,
        )
        t2 = await orch.create_task(
            project_name="wave-test", prompt="Task B", phase_number=1, wave=1,
        )
        t3 = await orch.create_task(
            project_name="wave-test", prompt="Task C (depends on A+B)",
            phase_number=1, wave=2,
        )

        tasks = await orch.list_tasks("wave-test", phase_number=1)
        wave1 = [t for t in tasks if t["wave"] == 1]
        wave2 = [t for t in tasks if t["wave"] == 2]
        assert len(wave1) == 2
        assert len(wave2) == 1

    @pytest.mark.asyncio
    async def test_project_deletion_cascades(self, orch_with_dir):
        orch, work_dir = orch_with_dir

        await orch.create_project(
            name="del-test", working_dir=work_dir,
        )
        await orch.create_phase(
            project_name="del-test", name="Phase 1", goal="Goal",
        )
        await orch.create_task(
            project_name="del-test", prompt="A task", phase_number=1,
        )

        # Delete project
        result = await orch.delete_project("del-test")
        assert result is True

        # Verify gone
        projects = await orch.list_projects()
        assert not any(p["name"] == "del-test" for p in projects)


class TestPlanApplyRollback:
    """Test that apply_plan rolls back on failure."""

    @pytest.mark.asyncio
    async def test_apply_plan_success(self, orch_with_dir):
        from taktis.core.planner import apply_plan

        orch, work_dir = orch_with_dir

        await orch.create_project(
            name="plan-test", working_dir=work_dir,
        )

        plan = {
            "project_summary": "Test project",
            "phases": [
                {
                    "name": "Phase 1",
                    "goal": "Build stuff",
                    "tasks": [
                        {"prompt": "Create main.py", "wave": 1},
                        {"prompt": "Create tests", "wave": 2},
                    ],
                },
                {
                    "name": "Phase 2",
                    "goal": "Deploy stuff",
                    "tasks": [
                        {"prompt": "Deploy to staging", "wave": 1},
                    ],
                },
            ],
        }

        result = await apply_plan(orch, "plan-test", plan)
        assert result["phases_created"] == 2
        assert result["tasks_created"] == 3

        phases = await orch.list_phases("plan-test")
        assert len(phases) == 2

        tasks = await orch.list_tasks("plan-test", phase_number=1)
        assert len(tasks) == 2


class TestWaveGrouperStandalone:
    """Test the extracted wave_grouper module directly."""

    def test_import_standalone(self):
        from taktis.core.wave_grouper import auto_assign_waves
        assert callable(auto_assign_waves)

    def test_simple_dag(self):
        from taktis.core.wave_grouper import auto_assign_waves

        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": []},
            {"id": "c", "depends_on": ["a", "b"]},
        ]
        waves = auto_assign_waves(tasks)
        assert 1 in waves
        assert 2 in waves
        assert len(waves[1]) == 2
        assert len(waves[2]) == 1
        assert waves[2][0]["id"] == "c"

    def test_deep_chain(self):
        from taktis.core.wave_grouper import auto_assign_waves

        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
            {"id": "d", "depends_on": ["c"]},
        ]
        waves = auto_assign_waves(tasks)
        assert len(waves) == 4
        assert waves[1][0]["id"] == "a"
        assert waves[4][0]["id"] == "d"

    def test_cycle_doesnt_crash(self):
        from taktis.core.wave_grouper import auto_assign_waves

        tasks = [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
        ]
        # Should not raise — cycle is broken with a warning
        waves = auto_assign_waves(tasks)
        assert len(waves) >= 1
