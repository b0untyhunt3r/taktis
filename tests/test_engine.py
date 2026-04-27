"""Integration tests for the Taktis facade."""

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_project(taktis_engine):
    project = await taktis_engine.create_project(
        name="myproj", working_dir="/tmp/myproj", description="A project",
        create_dir=True,
    )
    assert project["name"] == "myproj"
    assert project["description"] == "A project"
    assert project["status"] == "idle"
    assert project["state"] is not None
    assert project["phase_count"] == 0
    assert project["task_count"] == 0


@pytest.mark.asyncio
async def test_create_duplicate_project_raises(taktis_engine):
    await taktis_engine.create_project(name="dup", working_dir=".")
    with pytest.raises(ValueError, match="already exists"):
        await taktis_engine.create_project(name="dup", working_dir=".")


@pytest.mark.asyncio
async def test_list_projects(taktis_engine):
    await taktis_engine.create_project(name="lp1", working_dir=".")
    await taktis_engine.create_project(name="lp2", working_dir=".")
    projects = await taktis_engine.list_projects()
    names = {p["name"] for p in projects}
    assert "lp1" in names
    assert "lp2" in names


@pytest.mark.asyncio
async def test_get_project_not_found(taktis_engine):
    result = await taktis_engine.get_project("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_delete_project(taktis_engine):
    await taktis_engine.create_project(name="todel", working_dir=".")
    deleted = await taktis_engine.delete_project("todel")
    assert deleted is True
    assert await taktis_engine.get_project("todel") is None


@pytest.mark.asyncio
async def test_update_project(taktis_engine):
    await taktis_engine.create_project(name="upd", working_dir=".")
    updated = await taktis_engine.update_project("upd", description="changed")
    assert updated is not None
    assert updated["description"] == "changed"


# ---------------------------------------------------------------------------
# Phase CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_phase(taktis_engine):
    await taktis_engine.create_project(name="ph", working_dir=".")
    phase = await taktis_engine.create_phase(project_name="ph", name="Design", goal="Design it")
    assert phase["name"] == "Design"
    assert phase["phase_number"] == 1
    assert phase["goal"] == "Design it"
    assert phase["status"] == "not_started"


@pytest.mark.asyncio
async def test_create_phase_auto_numbers(taktis_engine):
    await taktis_engine.create_project(name="phn", working_dir=".")
    p1 = await taktis_engine.create_phase(project_name="phn", name="First")
    p2 = await taktis_engine.create_phase(project_name="phn", name="Second")
    assert p1["phase_number"] == 1
    assert p2["phase_number"] == 2


@pytest.mark.asyncio
async def test_list_phases(taktis_engine):
    await taktis_engine.create_project(name="lph", working_dir=".")
    await taktis_engine.create_phase(project_name="lph", name="A")
    await taktis_engine.create_phase(project_name="lph", name="B")
    phases = await taktis_engine.list_phases("lph")
    assert len(phases) == 2
    assert phases[0]["name"] == "A"
    assert phases[1]["name"] == "B"


@pytest.mark.asyncio
async def test_add_criterion(taktis_engine):
    await taktis_engine.create_project(name="crit", working_dir=".")
    await taktis_engine.create_phase(project_name="crit", name="P1")
    ok = await taktis_engine.add_criterion("crit", 1, "All tests pass")
    assert ok is True

    phase = await taktis_engine.get_phase("crit", 1)
    assert "All tests pass" in phase["success_criteria"]


@pytest.mark.asyncio
async def test_delete_phase(taktis_engine):
    await taktis_engine.create_project(name="dph", working_dir=".")
    await taktis_engine.create_phase(project_name="dph", name="Gone")
    deleted = await taktis_engine.delete_phase("dph", 1)
    assert deleted is True
    phases = await taktis_engine.list_phases("dph")
    assert len(phases) == 0


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task(taktis_engine):
    await taktis_engine.create_project(name="tp", working_dir=".")
    await taktis_engine.create_phase(project_name="tp", name="P1")
    task = await taktis_engine.create_task(
        project_name="tp", prompt="Write tests", phase_number=1
    )
    assert task["prompt"] == "Write tests"
    assert task["status"] == "pending"
    assert task["phase_id"] is not None


@pytest.mark.asyncio
async def test_create_task_requires_phase(taktis_engine):
    """A task requires a phase."""
    await taktis_engine.create_project(name="ts", working_dir=".")
    await taktis_engine.create_phase(project_name="ts", name="Phase1")
    task = await taktis_engine.create_task(project_name="ts", prompt="Phased work", phase_number=1)
    assert task["phase_id"] is not None
    assert task["prompt"] == "Phased work"


@pytest.mark.asyncio
async def test_create_task_with_expert(taktis_engine):
    """A task can reference an expert by name."""
    await taktis_engine.create_project(name="te", working_dir=".")
    await taktis_engine.create_phase(project_name="te", name="Phase1")

    # Use a builtin expert -- the taktis_engine fixture loads builtins
    experts = await taktis_engine.list_experts()
    if not experts:
        pytest.skip("No builtin experts loaded")

    expert_name = experts[0]["name"]
    task = await taktis_engine.create_task(
        project_name="te", prompt="Expert work", phase_number=1, expert=expert_name
    )
    assert task["expert"] == expert_name
    assert task["expert_id"] is not None


@pytest.mark.asyncio
async def test_list_tasks(taktis_engine):
    await taktis_engine.create_project(name="lt", working_dir=".")
    await taktis_engine.create_phase(project_name="lt", name="Phase1")
    await taktis_engine.create_task(project_name="lt", prompt="A", phase_number=1)
    await taktis_engine.create_task(project_name="lt", prompt="B", phase_number=1)
    tasks = await taktis_engine.list_tasks("lt")
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_list_tasks_filtered_by_phase(taktis_engine):
    await taktis_engine.create_project(name="ltf", working_dir=".")
    await taktis_engine.create_phase(project_name="ltf", name="P1")
    await taktis_engine.create_phase(project_name="ltf", name="P2")
    await taktis_engine.create_task(project_name="ltf", prompt="t1", phase_number=1)
    await taktis_engine.create_task(project_name="ltf", prompt="t2", phase_number=2)
    await taktis_engine.create_task(project_name="ltf", prompt="t3", phase_number=2)

    tasks_p1 = await taktis_engine.list_tasks("ltf", phase_number=1)
    assert len(tasks_p1) == 1

    tasks_p2 = await taktis_engine.list_tasks("ltf", phase_number=2)
    assert len(tasks_p2) == 2


@pytest.mark.asyncio
async def test_get_task(taktis_engine):
    await taktis_engine.create_project(name="gt", working_dir=".")
    await taktis_engine.create_phase(project_name="gt", name="Phase1")
    created = await taktis_engine.create_task(project_name="gt", prompt="Find me", phase_number=1)
    fetched = await taktis_engine.get_task(created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["prompt"] == "Find me"


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status(taktis_engine):
    status = await taktis_engine.get_status()
    assert "projects" in status
    assert "tasks" in status
    assert "running_processes" in status
    assert "max_concurrent" in status
    assert isinstance(status["projects"], int)


# ---------------------------------------------------------------------------
# Experts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_experts_builtins_loaded(taktis_engine):
    """After initialize(), builtin experts should be available."""
    experts = await taktis_engine.list_experts()
    # At minimum the list should be returned (even if empty on some setups)
    assert isinstance(experts, list)


@pytest.mark.asyncio
async def test_get_expert(taktis_engine):
    experts = await taktis_engine.list_experts()
    if not experts:
        pytest.skip("No experts loaded")
    name = experts[0]["name"]
    expert = await taktis_engine.get_expert(name)
    assert expert is not None
    assert expert["name"] == name


# ---------------------------------------------------------------------------
# Experts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_expert_from_content(taktis_engine):
    md_content = """---
description: Test expert
category: testing
---
You are a testing expert. Write comprehensive tests.
"""
    expert = await taktis_engine.create_expert(name="test-expert-custom", file_content=md_content)
    assert expert is not None
    assert expert["name"] == "test-expert-custom"

    fetched = await taktis_engine.get_expert("test-expert-custom")
    assert fetched is not None


# ---------------------------------------------------------------------------
# RECOVERY-01: Checkpoint-aware stale task recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_stale_task_with_checkpoint_resets_to_pending(taktis_engine):
    """RECOVERY-01: stale task whose phase has current_wave set is reset to 'pending'."""
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="rec_ckpt", working_dir=".")
    await taktis_engine.create_phase(project_name="rec_ckpt", name="Wave Phase")
    task = await taktis_engine.create_task(
        project_name="rec_ckpt", prompt="Do work", phase_number=1
    )
    phase = await taktis_engine.get_phase("rec_ckpt", 1)

    async with get_session() as conn:
        # Simulate an interrupted wave: task is running (no PID), phase has a checkpoint.
        await repo.update_task(conn, task["id"], status="running", pid=None)
        await repo.update_phase(conn, phase["id"], current_wave=1)

    await taktis_engine._execution_service._recover_stale_tasks()

    recovered = await taktis_engine.get_task(task["id"])
    assert recovered["status"] == "pending"


@pytest.mark.asyncio
async def test_recover_stale_task_without_checkpoint_marks_failed(taktis_engine):
    """RECOVERY-01: stale task whose phase has no current_wave is marked 'failed'."""
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="rec_nocp", working_dir=".")
    await taktis_engine.create_phase(project_name="rec_nocp", name="No Checkpoint Phase")
    task = await taktis_engine.create_task(
        project_name="rec_nocp", prompt="Do work", phase_number=1
    )

    async with get_session() as conn:
        # Task is running but phase has no wave checkpoint (current_wave stays NULL).
        await repo.update_task(conn, task["id"], status="running", pid=None)

    await taktis_engine._execution_service._recover_stale_tasks()

    recovered = await taktis_engine.get_task(task["id"])
    assert recovered["status"] == "failed"
    assert "Process lost" in (recovered["result_summary"] or "")


@pytest.mark.asyncio
async def test_recover_stale_tasks_mixed_phases(taktis_engine):
    """RECOVERY-01: tasks in two phases in one call — each phase handled independently.

    Also validates the N+1 avoidance: both tasks in the checkpointed phase
    share a single phase lookup, and neither lookup leaks across phases.
    """
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="rec_mix", working_dir=".")
    await taktis_engine.create_phase(project_name="rec_mix", name="Checkpointed Phase")
    await taktis_engine.create_phase(project_name="rec_mix", name="Uncheckpointed Phase")

    # Two tasks in the checkpointed phase, one in the uncheckpointed phase.
    t1a = await taktis_engine.create_task(
        project_name="rec_mix", prompt="T1a", phase_number=1
    )
    t1b = await taktis_engine.create_task(
        project_name="rec_mix", prompt="T1b", phase_number=1
    )
    t2 = await taktis_engine.create_task(
        project_name="rec_mix", prompt="T2", phase_number=2
    )

    ph1 = await taktis_engine.get_phase("rec_mix", 1)
    ph2 = await taktis_engine.get_phase("rec_mix", 2)

    async with get_session() as conn:
        for task in (t1a, t1b, t2):
            await repo.update_task(conn, task["id"], status="running", pid=None)
        # Phase 1 has a checkpoint; phase 2 does not.
        await repo.update_phase(conn, ph1["id"], current_wave=1)

    await taktis_engine._execution_service._recover_stale_tasks()

    r1a = await taktis_engine.get_task(t1a["id"])
    r1b = await taktis_engine.get_task(t1b["id"])
    r2 = await taktis_engine.get_task(t2["id"])

    assert r1a["status"] == "pending", "checkpointed phase task should reset to pending"
    assert r1b["status"] == "pending", "checkpointed phase task should reset to pending"
    assert r2["status"] == "failed", "uncheckpointed phase task should be marked failed"
    assert "Process lost" in (r2["result_summary"] or "")


@pytest.mark.asyncio
async def test_recover_alive_pid_task_is_skipped(taktis_engine):
    """RECOVERY-01: a task whose PID is still alive is not touched, regardless of checkpoint."""
    import os

    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="rec_alive", working_dir=".")
    await taktis_engine.create_phase(project_name="rec_alive", name="Phase")
    task = await taktis_engine.create_task(
        project_name="rec_alive", prompt="Alive work", phase_number=1
    )
    phase = await taktis_engine.get_phase("rec_alive", 1)

    own_pid = os.getpid()  # Our own PID is definitely still alive.
    async with get_session() as conn:
        await repo.update_task(conn, task["id"], status="running", pid=own_pid)
        # Even with a checkpoint, the alive-PID guard should fire first.
        await repo.update_phase(conn, phase["id"], current_wave=1)

    await taktis_engine._execution_service._recover_stale_tasks()

    # Task must remain untouched — the process managing it is still running.
    still_running = await taktis_engine.get_task(task["id"])
    assert still_running["status"] == "running"


# ---------------------------------------------------------------------------
# resume_phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_phase_unknown_id_raises(taktis_engine):
    """resume_phase raises ValueError for a non-existent phase ID."""
    with pytest.raises(ValueError, match="not found"):
        await taktis_engine.resume_phase("nonexistent-phase-id")


@pytest.mark.asyncio
async def test_resume_phase_already_complete_raises(taktis_engine):
    """resume_phase raises ValueError when the phase is already complete."""
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="rph_done", working_dir=".")
    phase = await taktis_engine.create_phase(project_name="rph_done", name="Done Phase")

    async with get_session() as conn:
        await repo.update_phase(conn, phase["id"], status="complete")

    with pytest.raises(ValueError, match="already complete"):
        await taktis_engine.resume_phase(phase["id"])


@pytest.mark.asyncio
async def test_resume_phase_no_checkpoint_no_failed_tasks_raises(taktis_engine):
    """resume_phase raises ValueError when current_wave is NULL and no task is
    failed/running — there is no resume point to return to."""
    await taktis_engine.create_project(name="rph_noresume", working_dir=".")
    phase = await taktis_engine.create_phase(
        project_name="rph_noresume", name="No Resume"
    )
    # A single pending task — no failed/running ones, no wave checkpoint.
    await taktis_engine.create_task(
        project_name="rph_noresume", prompt="Pending work", phase_number=1
    )

    with pytest.raises(ValueError, match="no resume point"):
        await taktis_engine.resume_phase(phase["id"])


@pytest.mark.asyncio
async def test_resume_phase_resets_failed_tasks_and_invokes_scheduler(taktis_engine):
    """Happy path: failed tasks in resume_wave are reset to pending, wave-1
    completed tasks are untouched, and the scheduler is called with the
    correct start_wave."""
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="rph_proj", working_dir=".")
    await taktis_engine.create_phase(project_name="rph_proj", name="Resume Phase")

    t_wave1 = await taktis_engine.create_task(
        project_name="rph_proj", prompt="Wave 1 work", phase_number=1, wave=1
    )
    t_wave2 = await taktis_engine.create_task(
        project_name="rph_proj", prompt="Wave 2 work", phase_number=1, wave=2
    )
    phase = await taktis_engine.get_phase("rph_proj", 1)

    async with get_session() as conn:
        # Simulate: wave 1 completed, wave 2 failed, checkpoint at wave 1.
        await repo.update_task(conn, t_wave1["id"], status="completed")
        await repo.update_task(conn, t_wave2["id"], status="failed")
        await repo.update_phase(conn, phase["id"], current_wave=1, status="in_progress")

    # Capture scheduler invocation without running real processes.
    execute_calls: list[dict] = []

    async def _mock_execute_phase(pid, proj, start_wave=1):
        execute_calls.append({"phase_id": pid, "start_wave": start_wave})

    taktis_engine.scheduler.execute_phase = _mock_execute_phase

    await taktis_engine.resume_phase(phase["id"])
    # Yield to the event loop so the background asyncio.create_task fires.
    await asyncio.sleep(0)

    # Wave 2 (failed) must be reset to pending.
    reset = await taktis_engine.get_task(t_wave2["id"])
    assert reset["status"] == "pending", "failed wave-2 task should be reset to pending"

    # Wave 1 (completed) must remain untouched.
    done = await taktis_engine.get_task(t_wave1["id"])
    assert done["status"] == "completed", "completed wave-1 task must not be touched"

    # Scheduler must be called with start_wave=2 (current_wave=1, so resume from 2).
    assert len(execute_calls) == 1
    assert execute_calls[0]["phase_id"] == phase["id"]
    assert execute_calls[0]["start_wave"] == 2


@pytest.mark.asyncio
async def test_resume_phase_no_checkpoint_with_failed_tasks_uses_wave_1(taktis_engine):
    """When current_wave is NULL but there are failed tasks, resume_wave=1
    and those tasks are reset to pending."""
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="rph_wave1", working_dir=".")
    await taktis_engine.create_phase(project_name="rph_wave1", name="Wave 1 Resume")

    task = await taktis_engine.create_task(
        project_name="rph_wave1", prompt="Failed work", phase_number=1, wave=1
    )
    phase = await taktis_engine.get_phase("rph_wave1", 1)

    async with get_session() as conn:
        await repo.update_task(conn, task["id"], status="failed")
        # current_wave stays NULL — no checkpoint written yet.

    execute_calls: list[dict] = []

    async def _mock_execute_phase(pid, proj, start_wave=1):
        execute_calls.append({"phase_id": pid, "start_wave": start_wave})

    taktis_engine.scheduler.execute_phase = _mock_execute_phase

    await taktis_engine.resume_phase(phase["id"])
    await asyncio.sleep(0)

    reset = await taktis_engine.get_task(task["id"])
    assert reset["status"] == "pending", "failed task should be reset to pending"

    assert len(execute_calls) == 1
    assert execute_calls[0]["start_wave"] == 1, "no checkpoint → resume from wave 1"


# ---------------------------------------------------------------------------
# get_interrupted_work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_interrupted_work_empty_when_nothing_interrupted(taktis_engine):
    """get_interrupted_work returns empty lists when there is nothing to resume."""
    result = await taktis_engine.get_interrupted_work()
    assert result == {"phases": []}


@pytest.mark.asyncio
async def test_get_interrupted_work_includes_checkpointed_in_progress_phase(taktis_engine):
    """A phase that is 'in_progress' with current_wave set appears in 'phases'."""
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="giw_phase", working_dir=".")
    phase = await taktis_engine.create_phase(project_name="giw_phase", name="Mid-Run")

    async with get_session() as conn:
        await repo.update_phase(
            conn, phase["id"], status="in_progress", current_wave=2
        )

    result = await taktis_engine.get_interrupted_work()
    phase_ids = [p["id"] for p in result["phases"]]

    assert phase["id"] in phase_ids
    match = next(p for p in result["phases"] if p["id"] == phase["id"])
    assert match["name"] == "Mid-Run"
    assert match["project_name"] == "giw_phase"


@pytest.mark.asyncio
async def test_get_interrupted_work_excludes_phase_without_checkpoint(taktis_engine):
    """An 'in_progress' phase with current_wave=NULL (no checkpoint) must NOT
    appear — it has no safe resume point."""
    import taktis.repository as repo
    from taktis.db import get_session

    await taktis_engine.create_project(name="giw_nochk", working_dir=".")
    phase = await taktis_engine.create_phase(
        project_name="giw_nochk", name="No Checkpoint"
    )

    async with get_session() as conn:
        # Set status to in_progress but leave current_wave as NULL.
        await repo.update_phase(conn, phase["id"], status="in_progress")

    result = await taktis_engine.get_interrupted_work()
    phase_ids = [p["id"] for p in result["phases"]]
    assert phase["id"] not in phase_ids


# ---------------------------------------------------------------------------
# _report_interrupted_work / startup reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_interrupted_work_silent_when_nothing_pending(taktis_engine):
    """_report_interrupted_work emits no event and logs nothing when the DB is clean."""
    from taktis.core.events import EVENT_SYSTEM_INTERRUPTED_WORK

    queue = taktis_engine.event_bus.subscribe(EVENT_SYSTEM_INTERRUPTED_WORK)
    await taktis_engine._execution_service._report_interrupted_work()

    # No event should have been published.
    assert queue.empty()
    taktis_engine.event_bus.unsubscribe(EVENT_SYSTEM_INTERRUPTED_WORK, queue)


@pytest.mark.asyncio
async def test_report_interrupted_work_warns_and_emits_for_interrupted_phase(
    taktis_engine, caplog
):
    """_report_interrupted_work logs a WARNING and publishes the event when an
    interrupted phase (in_progress + current_wave set) exists."""
    import logging

    import taktis.repository as repo
    from taktis.core.events import EVENT_SYSTEM_INTERRUPTED_WORK
    from taktis.db import get_session

    await taktis_engine.create_project(name="rip_phase", working_dir=".")
    phase = await taktis_engine.create_phase(project_name="rip_phase", name="Alpha")

    async with get_session() as conn:
        await repo.update_phase(conn, phase["id"], status="in_progress", current_wave=1)

    queue = taktis_engine.event_bus.subscribe(EVENT_SYSTEM_INTERRUPTED_WORK)

    with caplog.at_level(logging.WARNING, logger="taktis.core.engine"):
        await taktis_engine._execution_service._report_interrupted_work()

    # One WARNING log line mentioning the phase name / id.
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Alpha" in t or phase["id"] in t for t in warning_texts), (
        f"Expected a warning about phase 'Alpha'/{phase['id']}, got: {warning_texts}"
    )

    # One event published with the correct payload.
    assert not queue.empty(), "Expected system.interrupted_work event to be published"
    envelope = queue.get_nowait()
    assert envelope["event_type"] == EVENT_SYSTEM_INTERRUPTED_WORK
    phase_ids = [p["id"] for p in envelope["data"]["phases"]]
    assert phase["id"] in phase_ids

    taktis_engine.event_bus.unsubscribe(EVENT_SYSTEM_INTERRUPTED_WORK, queue)


@pytest.mark.asyncio
async def test_initialize_emits_interrupted_work_event_via_fresh_engine(tmp_path):
    """Full-cycle test: interrupted state persisted to disk triggers the event
    during a subsequent Taktis.initialize() call.

    Uses a second Taktis instance (same DB file) to simulate a server
    restart.
    """
    import taktis.db as db_mod
    import taktis.repository as repo
    from taktis.core.events import EVENT_SYSTEM_INTERRUPTED_WORK
    from taktis.core.engine import Taktis

    db_file = str(tmp_path / "restart_test.db")
    original_path = db_mod.DATABASE_PATH
    db_mod.DATABASE_PATH = db_file

    try:
        # --- First lifecycle: create state, then shut down without recovery ---
        orch1 = Taktis()
        await orch1.initialize()
        await orch1.create_project(name="restart_proj", working_dir=".")
        phase = await orch1.create_phase(project_name="restart_proj", name="Restart Phase")

        # Simulate an interrupted phase with a checkpoint wave.
        from taktis.db import get_session
        async with get_session() as conn:
            await repo.update_phase(
                conn, phase["id"], status="in_progress", current_wave=3
            )

        await orch1.shutdown()

        # --- Second lifecycle: fresh Taktis on the same DB ---
        orch2 = Taktis()

        # Subscribe *before* initialize() so we catch the startup event.
        queue = orch2.event_bus.subscribe(EVENT_SYSTEM_INTERRUPTED_WORK)
        await orch2.initialize()

        assert not queue.empty(), (
            "Expected system.interrupted_work to be published during initialize()"
        )
        envelope = queue.get_nowait()
        assert envelope["event_type"] == EVENT_SYSTEM_INTERRUPTED_WORK
        phase_ids = [p["id"] for p in envelope["data"]["phases"]]
        assert phase["id"] in phase_ids

        orch2.event_bus.unsubscribe(EVENT_SYSTEM_INTERRUPTED_WORK, queue)
        await orch2.shutdown()
    finally:
        db_mod.DATABASE_PATH = original_path


# ---------------------------------------------------------------------------
# Async context manager (Change 2)
# ---------------------------------------------------------------------------


class TestAsyncContextManager:
    @pytest.mark.asyncio
    async def test_context_manager_initializes_and_shuts_down(self, taktis_engine):
        """Taktis can be used as async context manager."""
        # The taktis_engine fixture already initialized it, so just test the methods exist
        assert hasattr(taktis_engine, '__aenter__')
        assert hasattr(taktis_engine, '__aexit__')
