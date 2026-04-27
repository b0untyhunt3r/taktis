"""Tests for repository CRUD functions against a real (in-memory) SQLite DB."""

import json

import pytest
import pytest_asyncio

from taktis import repository as repo


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_project(db_conn):
    project = await repo.create_project(db_conn, name="proj1", description="A test")
    assert project is not None
    assert project["name"] == "proj1"
    assert project["description"] == "A test"
    assert project["id"] is not None

    fetched = await repo.get_project_by_id(db_conn, project["id"])
    assert fetched is not None
    assert fetched["name"] == "proj1"

    by_name = await repo.get_project_by_name(db_conn, "proj1")
    assert by_name is not None
    assert by_name["id"] == project["id"]


@pytest.mark.asyncio
async def test_list_projects_empty(db_conn):
    projects = await repo.list_projects(db_conn)
    assert projects == []


@pytest.mark.asyncio
async def test_list_projects_multiple(db_conn):
    await repo.create_project(db_conn, name="alpha")
    await repo.create_project(db_conn, name="beta")
    projects = await repo.list_projects(db_conn)
    assert len(projects) == 2
    names = {p["name"] for p in projects}
    assert names == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_update_project(db_conn):
    project = await repo.create_project(db_conn, name="upd")
    updated = await repo.update_project(db_conn, project["id"], description="new desc")
    assert updated["description"] == "new desc"
    assert updated["name"] == "upd"


@pytest.mark.asyncio
async def test_delete_project(db_conn):
    await repo.create_project(db_conn, name="delme")
    deleted = await repo.delete_project(db_conn, "delme")
    assert deleted is True
    assert await repo.get_project_by_name(db_conn, "delme") is None

    # Deleting non-existent returns False
    assert await repo.delete_project(db_conn, "nope") is False


# ---------------------------------------------------------------------------
# ProjectState CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_project_state(db_conn):
    project = await repo.create_project(db_conn, name="ps-proj")
    state = await repo.create_project_state(
        db_conn, project["id"], status="active", decisions=["d1"]
    )
    assert state is not None
    assert state["project_id"] == project["id"]
    assert state["status"] == "active"

    fetched = await repo.get_project_state(db_conn, project["id"])
    assert fetched is not None
    assert fetched["status"] == "active"


@pytest.mark.asyncio
async def test_update_project_state(db_conn):
    project = await repo.create_project(db_conn, name="ps-upd")
    await repo.create_project_state(db_conn, project["id"])
    await repo.update_project_state(db_conn, project["id"], status="running")

    state = await repo.get_project_state(db_conn, project["id"])
    assert state["status"] == "running"


# ---------------------------------------------------------------------------
# Phase CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_phase(db_conn):
    project = await repo.create_project(db_conn, name="ph-proj")
    phase = await repo.create_phase(
        db_conn, project_id=project["id"], name="Design", phase_number=1, goal="Design it"
    )
    assert phase is not None
    assert phase["name"] == "Design"
    assert phase["phase_number"] == 1

    fetched = await repo.get_phase(db_conn, project["id"], 1)
    assert fetched is not None
    assert fetched["id"] == phase["id"]


@pytest.mark.asyncio
async def test_list_phases_ordered(db_conn):
    project = await repo.create_project(db_conn, name="ph-ord")
    await repo.create_phase(db_conn, project_id=project["id"], name="Second", phase_number=2)
    await repo.create_phase(db_conn, project_id=project["id"], name="First", phase_number=1)

    phases = await repo.list_phases(db_conn, project["id"])
    assert len(phases) == 2
    assert phases[0]["name"] == "First"
    assert phases[1]["name"] == "Second"


@pytest.mark.asyncio
async def test_get_max_phase_number(db_conn):
    project = await repo.create_project(db_conn, name="ph-max")
    assert await repo.get_max_phase_number(db_conn, project["id"]) == 0

    await repo.create_phase(db_conn, project_id=project["id"], name="P1", phase_number=1)
    await repo.create_phase(db_conn, project_id=project["id"], name="P2", phase_number=5)
    assert await repo.get_max_phase_number(db_conn, project["id"]) == 5


@pytest.mark.asyncio
async def test_delete_phase(db_conn):
    project = await repo.create_project(db_conn, name="ph-del")
    await repo.create_phase(db_conn, project_id=project["id"], name="Gone", phase_number=1)
    deleted = await repo.delete_phase(db_conn, project["id"], 1)
    assert deleted is True
    assert await repo.get_phase(db_conn, project["id"], 1) is None

    assert await repo.delete_phase(db_conn, project["id"], 99) is False


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_task(db_conn):
    project = await repo.create_project(db_conn, name="t-proj")
    task = await repo.create_task(
        db_conn, project_id=project["id"], name="build", prompt="Do the build"
    )
    assert task is not None
    assert task["name"] == "build"
    assert task["status"] == "pending"
    assert task["wave"] == 1

    fetched = await repo.get_task(db_conn, task["id"])
    assert fetched is not None
    assert fetched["name"] == "build"


@pytest.mark.asyncio
async def test_list_tasks_filtered_by_phase(db_conn):
    project = await repo.create_project(db_conn, name="t-filt")
    phase_a = await repo.create_phase(
        db_conn, project_id=project["id"], name="A", phase_number=1
    )
    phase_b = await repo.create_phase(
        db_conn, project_id=project["id"], name="B", phase_number=2
    )

    await repo.create_task(
        db_conn, project_id=project["id"], phase_id=phase_a["id"], name="ta1"
    )
    await repo.create_task(
        db_conn, project_id=project["id"], phase_id=phase_a["id"], name="ta2"
    )
    await repo.create_task(
        db_conn, project_id=project["id"], phase_id=phase_b["id"], name="tb1"
    )

    tasks_a = await repo.list_tasks(db_conn, project["id"], phase_id=phase_a["id"])
    assert len(tasks_a) == 2

    tasks_b = await repo.list_tasks(db_conn, project["id"], phase_id=phase_b["id"])
    assert len(tasks_b) == 1

    all_tasks = await repo.list_tasks(db_conn, project["id"])
    assert len(all_tasks) == 3


@pytest.mark.asyncio
async def test_update_task(db_conn):
    project = await repo.create_project(db_conn, name="t-upd")
    task = await repo.create_task(db_conn, project_id=project["id"], name="upd-task")
    await repo.update_task(db_conn, task["id"], status="running", cost_usd=1.23)

    fetched = await repo.get_task(db_conn, task["id"])
    assert fetched["status"] == "running"
    assert fetched["cost_usd"] == pytest.approx(1.23)


# ---------------------------------------------------------------------------
# Expert CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_expert(db_conn):
    expert = await repo.create_expert(
        db_conn, name="architect", description="Design expert", category="architecture"
    )
    assert expert is not None
    assert expert["name"] == "architect"

    by_name = await repo.get_expert_by_name(db_conn, "architect")
    assert by_name is not None
    assert by_name["id"] == expert["id"]

    by_id = await repo.get_expert_by_id(db_conn, expert["id"])
    assert by_id is not None
    assert by_id["name"] == "architect"


@pytest.mark.asyncio
async def test_list_experts(db_conn):
    await repo.create_expert(db_conn, name="exp-a")
    await repo.create_expert(db_conn, name="exp-b")
    experts = await repo.list_experts(db_conn)
    assert len(experts) >= 2
    names = {e["name"] for e in experts}
    assert "exp-a" in names
    assert "exp-b" in names


@pytest.mark.asyncio
async def test_delete_expert(db_conn):
    await repo.create_expert(db_conn, name="exp-del")
    deleted = await repo.delete_expert(db_conn, "exp-del")
    assert deleted is True
    assert await repo.get_expert_by_name(db_conn, "exp-del") is None
    assert await repo.delete_expert(db_conn, "nope") is False


# ---------------------------------------------------------------------------
# TaskOutput
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_output(db_conn):
    project = await repo.create_project(db_conn, name="to-proj")
    task = await repo.create_task(db_conn, project_id=project["id"], name="to-task")

    await repo.create_task_output(
        db_conn, task_id=task["id"], event_type="stdout", content={"text": "hello"}
    )
    outputs = await repo.get_task_outputs(db_conn, task["id"])
    assert len(outputs) == 1
    assert outputs[0]["event_type"] == "stdout"


@pytest.mark.asyncio
async def test_get_task_outputs_with_tail(db_conn):
    project = await repo.create_project(db_conn, name="to-tail")
    task = await repo.create_task(db_conn, project_id=project["id"], name="tail-task")

    for i in range(5):
        await repo.create_task_output(
            db_conn,
            task_id=task["id"],
            event_type="line",
            content={"n": i},
            timestamp=f"2025-01-01T00:00:0{i}+00:00",
        )

    tail_2 = await repo.get_task_outputs(db_conn, task["id"], tail=2)
    assert len(tail_2) == 2
    # Should be the last 2 in ascending order
    contents = [json.loads(o["content"]) if isinstance(o["content"], str) else o["content"]
                 for o in tail_2]
    assert contents[0]["n"] == 3
    assert contents[1]["n"] == 4


# ---------------------------------------------------------------------------
# Status / count helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_projects(db_conn):
    assert await repo.count_projects(db_conn) == 0
    await repo.create_project(db_conn, name="cnt1")
    await repo.create_project(db_conn, name="cnt2")
    assert await repo.count_projects(db_conn) == 2


@pytest.mark.asyncio
async def test_get_task_counts_by_status(db_conn):
    project = await repo.create_project(db_conn, name="cnt-proj")
    await repo.create_task(db_conn, project_id=project["id"], name="t1", status="pending")
    await repo.create_task(db_conn, project_id=project["id"], name="t2", status="pending")
    await repo.create_task(db_conn, project_id=project["id"], name="t3", status="running")

    counts = await repo.get_task_counts_by_status(db_conn)
    assert counts.get("pending") == 2
    assert counts.get("running") == 1


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_fields_round_trip(db_conn):
    """Verify JSON fields serialize/deserialize correctly through the repo layer."""
    env_vars = {"FOO": "bar", "NUM": "42"}
    project = await repo.create_project(
        db_conn, name="json-rt", default_env_vars=env_vars
    )
    fetched = await repo.get_project_by_id(db_conn, project["id"])
    # default_env_vars is stored as JSON text; the raw row returns a string
    raw_env = fetched["default_env_vars"]
    if isinstance(raw_env, str):
        parsed = json.loads(raw_env)
    else:
        parsed = raw_env
    assert parsed == env_vars

    # Phase success_criteria
    phase = await repo.create_phase(
        db_conn,
        project_id=project["id"],
        name="J",
        phase_number=1,
        success_criteria=["a", "b"],
    )
    fetched_phase = await repo.get_phase(db_conn, project["id"], 1)
    raw_criteria = fetched_phase["success_criteria"]
    if isinstance(raw_criteria, str):
        parsed_criteria = json.loads(raw_criteria)
    else:
        parsed_criteria = raw_criteria
    assert parsed_criteria == ["a", "b"]

    # Task depends_on
    task = await repo.create_task(
        db_conn,
        project_id=project["id"],
        name="dep-task",
        depends_on=["id1", "id2"],
    )
    fetched_task = await repo.get_task(db_conn, task["id"])
    raw_deps = fetched_task["depends_on"]
    if isinstance(raw_deps, str):
        parsed_deps = json.loads(raw_deps)
    else:
        parsed_deps = raw_deps
    assert parsed_deps == ["id1", "id2"]
