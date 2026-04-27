"""Unit tests for ProjectService — the 813-line module with zero prior coverage.

Uses the taktis_engine fixture (real Taktis with temp DB) to test
through the facade, exercising the actual ProjectService methods.
"""

from __future__ import annotations

import pytest
import pytest_asyncio


# ======================================================================
# Project CRUD
# ======================================================================

class TestProjectCRUD:

    @pytest.mark.asyncio
    async def test_create_project(self, taktis_engine, tmp_path):
        wd = str(tmp_path / "proj1")
        project = await taktis_engine.create_project(
            name="proj1", working_dir=wd, description="Test",
            create_dir=True,
        )
        assert project["name"] == "proj1"
        assert project["description"] == "Test"
        assert "id" in project
        assert "state" in project

    @pytest.mark.asyncio
    async def test_create_project_duplicate_raises(self, taktis_engine, tmp_path):
        wd = str(tmp_path / "dup")
        await taktis_engine.create_project(name="dup", working_dir=wd, create_dir=True)
        with pytest.raises(ValueError, match="already exists"):
            await taktis_engine.create_project(name="dup", working_dir=wd)

    @pytest.mark.asyncio
    async def test_create_project_missing_dir_raises(self, taktis_engine):
        with pytest.raises(ValueError, match="does not exist"):
            await taktis_engine.create_project(
                name="nodir", working_dir="/nonexistent/path/xyz",
            )

    @pytest.mark.asyncio
    async def test_create_project_with_create_dir(self, taktis_engine, tmp_path):
        wd = str(tmp_path / "newdir" / "subdir")
        project = await taktis_engine.create_project(
            name="dirproj", working_dir=wd, create_dir=True,
        )
        assert project["name"] == "dirproj"
        import os
        assert os.path.isdir(wd)

    @pytest.mark.asyncio
    async def test_list_projects_empty(self, taktis_engine):
        projects = await taktis_engine.list_projects()
        assert projects == []

    @pytest.mark.asyncio
    async def test_list_projects(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="lp1", working_dir=str(tmp_path / "a"), create_dir=True,
        )
        await taktis_engine.create_project(
            name="lp2", working_dir=str(tmp_path / "b"), create_dir=True,
        )
        projects = await taktis_engine.list_projects()
        names = {p["name"] for p in projects}
        assert "lp1" in names
        assert "lp2" in names

    @pytest.mark.asyncio
    async def test_delete_project(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="delme", working_dir=str(tmp_path / "d"), create_dir=True,
        )
        result = await taktis_engine.delete_project("delme")
        assert result is True
        projects = await taktis_engine.list_projects()
        assert all(p["name"] != "delme" for p in projects)

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, taktis_engine):
        result = await taktis_engine.delete_project("ghost")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_project(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="getme", working_dir=str(tmp_path / "g"), create_dir=True,
        )
        project = await taktis_engine.get_project("getme")
        assert project is not None
        assert project["name"] == "getme"

    @pytest.mark.asyncio
    async def test_get_project_not_found(self, taktis_engine):
        project = await taktis_engine.get_project("nope")
        assert project is None


# ======================================================================
# Phase CRUD
# ======================================================================

class TestPhaseCRUD:

    @pytest.mark.asyncio
    async def test_create_phase(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="pp", working_dir=str(tmp_path / "pp"), create_dir=True,
        )
        phase = await taktis_engine.create_phase(
            project_name="pp", name="Phase 1", goal="Build stuff",
        )
        assert phase["name"] == "Phase 1"
        assert phase["phase_number"] == 1

    @pytest.mark.asyncio
    async def test_create_multiple_phases_sequential_numbers(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="mp", working_dir=str(tmp_path / "mp"), create_dir=True,
        )
        p1 = await taktis_engine.create_phase(project_name="mp", name="P1", goal="G1")
        p2 = await taktis_engine.create_phase(project_name="mp", name="P2", goal="G2")
        assert p1["phase_number"] == 1
        assert p2["phase_number"] == 2

    @pytest.mark.asyncio
    async def test_list_phases(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="lph", working_dir=str(tmp_path / "lph"), create_dir=True,
        )
        await taktis_engine.create_phase(project_name="lph", name="A", goal="G")
        await taktis_engine.create_phase(project_name="lph", name="B", goal="G")
        phases = await taktis_engine.list_phases("lph")
        assert len(phases) == 2
        assert phases[0]["name"] == "A"
        assert phases[1]["name"] == "B"

    @pytest.mark.asyncio
    async def test_create_phase_unknown_project_raises(self, taktis_engine):
        with pytest.raises(ValueError, match="not found"):
            await taktis_engine.create_phase(
                project_name="nope", name="P", goal="G",
            )


# ======================================================================
# Task CRUD
# ======================================================================

class TestTaskCRUD:

    @pytest.mark.asyncio
    async def test_create_task(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="tp", working_dir=str(tmp_path / "tp"), create_dir=True,
        )
        phase = await taktis_engine.create_phase(
            project_name="tp", name="P1", goal="G",
        )
        task = await taktis_engine.create_task(
            project_name="tp", prompt="Do it",
            phase_number=phase["phase_number"], name="T1",
        )
        assert task["name"] == "T1"
        assert task["status"] == "pending"
        assert task["wave"] == 1

    @pytest.mark.asyncio
    async def test_create_task_with_expert(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="te", working_dir=str(tmp_path / "te"), create_dir=True,
        )
        phase = await taktis_engine.create_phase(
            project_name="te", name="P1", goal="G",
        )
        task = await taktis_engine.create_task(
            project_name="te", prompt="Review it",
            phase_number=phase["phase_number"],
            expert="implementer-general",
        )
        assert task.get("expert") == "implementer-general"

    @pytest.mark.asyncio
    async def test_list_tasks(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="lt", working_dir=str(tmp_path / "lt"), create_dir=True,
        )
        phase = await taktis_engine.create_phase(
            project_name="lt", name="P1", goal="G",
        )
        await taktis_engine.create_task(
            project_name="lt", prompt="A",
            phase_number=phase["phase_number"], name="T1",
        )
        await taktis_engine.create_task(
            project_name="lt", prompt="B",
            phase_number=phase["phase_number"], name="T2",
        )
        tasks = await taktis_engine.list_tasks("lt")
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_tasks_filter_by_phase(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="ltf", working_dir=str(tmp_path / "ltf"), create_dir=True,
        )
        p1 = await taktis_engine.create_phase(project_name="ltf", name="P1", goal="G")
        p2 = await taktis_engine.create_phase(project_name="ltf", name="P2", goal="G")
        await taktis_engine.create_task(
            project_name="ltf", prompt="A",
            phase_number=p1["phase_number"], name="T1",
        )
        await taktis_engine.create_task(
            project_name="ltf", prompt="B",
            phase_number=p2["phase_number"], name="T2",
        )
        tasks_p1 = await taktis_engine.list_tasks("ltf", phase_number=p1["phase_number"])
        assert len(tasks_p1) == 1
        assert tasks_p1[0]["name"] == "T1"

    @pytest.mark.asyncio
    async def test_get_task(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="gt", working_dir=str(tmp_path / "gt"), create_dir=True,
        )
        phase = await taktis_engine.create_phase(
            project_name="gt", name="P1", goal="G",
        )
        task = await taktis_engine.create_task(
            project_name="gt", prompt="X",
            phase_number=phase["phase_number"],
        )
        fetched = await taktis_engine.get_task(task["id"])
        assert fetched is not None
        assert fetched["id"] == task["id"]

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, taktis_engine):
        result = await taktis_engine.get_task("nonexistent")
        assert result is None


# ======================================================================
# Enrichment
# ======================================================================

class TestEnrichment:

    @pytest.mark.asyncio
    async def test_project_has_state(self, taktis_engine, tmp_path):
        project = await taktis_engine.create_project(
            name="en", working_dir=str(tmp_path / "en"), create_dir=True,
        )
        assert "state" in project
        assert project["state"]["status"] == "idle"

    @pytest.mark.asyncio
    async def test_task_has_enriched_fields(self, taktis_engine, tmp_path):
        await taktis_engine.create_project(
            name="ef", working_dir=str(tmp_path / "ef"), create_dir=True,
        )
        phase = await taktis_engine.create_phase(
            project_name="ef", name="P1", goal="G",
        )
        task = await taktis_engine.create_task(
            project_name="ef", prompt="X",
            phase_number=phase["phase_number"],
        )
        assert "context_window" in task
        assert "depends_on" in task
        assert isinstance(task["depends_on"], list)

    @pytest.mark.asyncio
    async def test_active_tasks_all_empty(self, taktis_engine):
        tasks = await taktis_engine.get_active_tasks_all()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_get_status(self, taktis_engine):
        status = await taktis_engine.get_status()
        assert "projects" in status
        assert "tasks" in status


# ======================================================================
# Expert operations (through facade)
# ======================================================================

class TestExperts:

    @pytest.mark.asyncio
    async def test_list_experts_has_builtins(self, taktis_engine):
        experts = await taktis_engine.list_experts()
        assert len(experts) > 0
        names = {e["name"] for e in experts}
        assert "implementer-general" in names

    @pytest.mark.asyncio
    async def test_create_custom_expert(self, taktis_engine):
        expert = await taktis_engine.create_expert(
            name="custom-test",
            description="A custom test expert",
            system_prompt="You are custom.",
            category="testing",
        )
        assert expert["name"] == "custom-test"
        assert expert["is_builtin"] == 0

    @pytest.mark.asyncio
    async def test_get_expert(self, taktis_engine):
        expert = await taktis_engine.get_expert("implementer-general")
        assert expert is not None
        assert expert["name"] == "implementer-general"

    @pytest.mark.asyncio
    async def test_get_expert_not_found(self, taktis_engine):
        expert = await taktis_engine.get_expert("nonexistent")
        assert expert is None
