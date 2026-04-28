"""ProjectService -- extracted project/phase/task CRUD and enrichment logic."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from taktis import repository as repo
from taktis.core.events import EVENT_TASK_OUTPUT, EventBus
from taktis.core.experts import ExpertRegistry
from taktis.core.profiles import get_context_window
from taktis.utils import parse_json_field as _parse_json_field

logger = logging.getLogger(__name__)


class ProjectService:
    """CRUD and enrichment logic for projects, phases, tasks, experts, and profiles.

    Extracted from :class:`Taktis` to keep the facade thin.  The
    Taktis wires this service up during initialization and delegates all
    project/phase/task queries here.
    """

    def __init__(
        self,
        db_session_factory,
        expert_registry: ExpertRegistry,
        event_bus: EventBus,
        *,
        start_task: Callable[[str], Coroutine[Any, Any, dict]] | None = None,
        stop_project_tasks: Callable[[list[str]], Coroutine[Any, Any, None]] | None = None,
        get_running_count: Callable[[], int] | None = None,
    ) -> None:
        self._session_factory = db_session_factory
        self._expert_registry = expert_registry
        self._event_bus = event_bus
        self._start_task = start_task
        self._stop_project_tasks = stop_project_tasks
        self._get_running_count = get_running_count

    def set_execution_callbacks(
        self,
        *,
        start_task: Callable[[str], Coroutine[Any, Any, dict]] | None = None,
        stop_project_tasks: Callable[[list[str]], Coroutine[Any, Any, None]] | None = None,
        get_running_count: Callable[[], int] | None = None,
    ) -> None:
        """Set cross-service callbacks (called by Taktis after construction)."""
        if start_task is not None:
            self._start_task = start_task
        if stop_project_tasks is not None:
            self._stop_project_tasks = stop_project_tasks
        if get_running_count is not None:
            self._get_running_count = get_running_count

    # ==================================================================
    # Project CRUD
    # ==================================================================

    async def check_existing_context(self, working_dir: str) -> dict | None:
        """Check if a working directory has an existing .taktis/ folder
        belonging to a non-archived project that has actual work done.

        Returns a dict with old project info if found, None otherwise.
        """
        import os
        wd = os.path.realpath(os.path.abspath(os.path.expanduser(working_dir)))
        ctx_dir = os.path.join(wd, ".taktis")
        if not os.path.isdir(ctx_dir):
            return None

        # Find which non-archived project(s) use this working_dir
        norm = os.path.normpath(wd)
        async with self._session_factory() as conn:
            projects = await repo.list_projects(conn)
            for p in projects:
                p_wd = os.path.normpath(p.get("working_dir", ""))
                if p_wd != norm:
                    continue
                # Check if the project is archived — skip if so
                state = await repo.get_project_state(conn, p["id"])
                if state and state.get("status") == "archived":
                    continue
                # Check if the project has any phases (real work done)
                phases = await repo.list_phases(conn, p["id"])
                if not phases:
                    continue
                return {
                    "project_name": p["name"],
                    "project_id": p["id"],
                    "working_dir": wd,
                }
        # No active project with work found — safe to overwrite
        return None

    async def _archive_old_project(self, project_id: str, working_dir: str) -> None:
        """Archive an old project and clean its .taktis/ folder."""
        import shutil
        from pathlib import Path

        if project_id:
            # Stop any running tasks
            async with self._session_factory() as conn:
                tasks = await repo.list_tasks(conn, project_id)
                running_ids = [
                    t["id"] for t in tasks
                    if t["status"] in ("running", "paused", "awaiting_input")
                ]
            if running_ids and self._stop_project_tasks is not None:
                logger.info("Stopping %d running task(s) before archiving", len(running_ids))
                await self._stop_project_tasks(running_ids)

            # Mark project as archived
            async with self._session_factory() as conn:
                await repo.update_project_state(conn, project_id, status="archived")
            logger.info("Archived project %s", project_id)

        # Remove .taktis/ directory
        ctx_dir = Path(working_dir) / ".taktis"
        if ctx_dir.exists():
            try:
                shutil.rmtree(ctx_dir)
                logger.info("Removed old context directory: %s", ctx_dir)
            except OSError:
                logger.warning("Failed to remove context directory: %s", ctx_dir)

    async def create_project(
        self,
        name: str,
        working_dir: str,
        description: str = "",
        model: str | None = None,
        permission_mode: str | None = None,
        create_dir: bool = False,
        clean_existing: bool = False,
        default_model: str = "sonnet",
        default_permission_mode: str = "default",
    ) -> dict:
        """Create a new project and its associated state record.

        If the working directory does not exist and *create_dir* is ``True``,
        it will be created.  If *create_dir* is ``False`` and the directory
        does not exist, a :class:`ValueError` is raised.

        If the working directory already has an ``.taktis/`` folder and
        *clean_existing* is ``False``, raises :class:`ValueError` with info
        about the old project.  Set *clean_existing* to ``True`` to archive
        the old project and remove its context data.
        """
        import os
        import sys
        wd = os.path.expanduser(working_dir)
        if not os.path.isdir(wd):
            if create_dir:
                os.makedirs(wd, exist_ok=True)
                logger.info("Created working directory: %s", wd)
            else:
                raise ValueError(
                    f"Working directory does not exist: {wd}"
                )
        # Normalise to absolute path and guard against path traversal.
        # Use abspath (not realpath) — on Windows, realpath resolves mapped
        # network drives (e.g. Z:) to their underlying UNC path, which
        # triggers SDK permission prompts even in bypassPermissions mode.
        working_dir = os.path.abspath(wd)
        if ".." in os.path.normpath(wd).split(os.sep):
            raise ValueError(
                "Working directory must not contain '..' path components"
            )

        # Reject system-critical paths
        _blocked = ["/bin", "/sbin", "/usr", "/etc", "/boot", "/proc", "/sys", "/dev"]
        if sys.platform == "win32":
            _blocked += [
                os.path.normpath(os.environ.get("SYSTEMROOT", r"C:\Windows")),
                os.path.normpath(os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\System32"),
            ]
        norm_wd = os.path.normpath(working_dir)
        for blocked in _blocked:
            if os.path.normpath(norm_wd) == os.path.normpath(blocked):
                raise ValueError(
                    f"Working directory cannot be a system-critical path: {working_dir}"
                )

        # Check for duplicate name first
        async with self._session_factory() as conn:
            existing = await repo.get_project_by_name(conn, name)
            if existing is not None:
                raise ValueError(f"Project '{name}' already exists")

        # Check for existing .taktis/ context data from another project
        old = await self.check_existing_context(working_dir)
        if old is not None:
            if not clean_existing:
                old_name = old["project_name"]
                raise ValueError(
                    f"EXISTING_PROJECT:{old_name}:"
                    f"This directory has context data from project '{old_name}'. "
                    f"Check 'Clean old data' to archive it and start fresh."
                )
            # User confirmed — archive old project and clean
            await self._archive_old_project(old.get("project_id"), working_dir)

        async with self._session_factory() as conn:

            project = await repo.create_project(
                conn,
                name=name,
                working_dir=working_dir,
                description=description,
                default_model=model or default_model,
                default_permission_mode=permission_mode or default_permission_mode,
            )

            # Create associated state
            await repo.create_project_state(
                conn,
                project_id=project["id"],
                status="idle",
                decisions=[],
                blockers=[],
                metrics={
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                    "total_cost_usd": 0.0,
                    "total_duration_s": 0.0,
                },
            )

            enriched = await self._enrich_project(conn, project)

        # Initialize .taktis/ context directory
        from taktis.core.context import init_context
        init_context(working_dir, name, description)

        return enriched

    async def list_projects(self) -> list[dict]:
        """List all projects (enrichment is parallelized)."""
        async with self._session_factory() as conn:
            projects = await repo.list_projects(conn)
            if not projects:
                return []
            result = await asyncio.gather(
                *(self._enrich_project(conn, p) for p in projects)
            )
            return list(result)

    async def get_project(self, name: str) -> dict | None:
        """Get a project by name."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, name)
            if project is None:
                return None
            return await self._enrich_project(conn, project)

    async def delete_project(self, name: str) -> bool:
        """Delete a project, its DB records, and .taktis/ context directory."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, name)
            if project is None:
                return False

            working_dir = project.get("working_dir")

            # Stop any running tasks for this project before deleting
            tasks = await repo.list_tasks(conn, project["id"])
            running_ids = [
                t["id"] for t in tasks
                if t["status"] in ("running", "paused", "awaiting_input")
            ]
            if running_ids and self._stop_project_tasks is not None:
                logger.info("Stopping %d running task(s) for project '%s'", len(running_ids), name)
                await self._stop_project_tasks(running_ids)

            ok = await repo.delete_project(conn, name)
            if not ok:
                return False

        # Clean up .taktis/ context directory (NOT the working dir itself)
        if working_dir:
            import shutil
            from pathlib import Path
            ctx_dir = Path(working_dir) / ".taktis"
            if ctx_dir.exists():
                try:
                    shutil.rmtree(ctx_dir)
                    logger.info("Removed context directory: %s", ctx_dir)
                except OSError:
                    logger.warning("Failed to remove context directory: %s", ctx_dir)

        return True

    async def update_project(self, name: str, **kwargs) -> dict | None:
        """Update project fields.  Accepted kwargs: description, working_dir,
        default_model, default_permission_mode, default_env_vars,
        planning_options."""
        allowed = {
            "description", "working_dir", "default_model",
            "default_permission_mode", "default_env_vars",
            "planning_options",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return await self.get_project(name)

        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, name)
            if project is None:
                return None

            updated = await repo.update_project(conn, project["id"], **updates)
            return await self._enrich_project(conn, updated)

    # ==================================================================
    # Phase CRUD
    # ==================================================================

    async def create_phase(
        self,
        project_name: str,
        name: str,
        goal: str = "",
        description: str = "",
        success_criteria: list | None = None,
        context_config: str | None = None,
    ) -> dict:
        """Create a new phase within a project."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                raise ValueError(f"Project '{project_name}' not found")

            max_num = await repo.get_max_phase_number(conn, project["id"])

            phase = await repo.create_phase(
                conn,
                project_id=project["id"],
                name=name,
                goal=goal,
                description=description,
                phase_number=max_num + 1,
                success_criteria=success_criteria or [],
                context_config=context_config,
            )

            return await self._enrich_phase(conn, phase)

    async def list_phases(self, project_name: str) -> list[dict]:
        """List all phases for a project."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                raise ValueError(f"Project '{project_name}' not found")

            phases = await repo.list_phases(conn, project["id"])
            result = []
            for p in phases:
                result.append(await self._enrich_phase(conn, p, include_expert=True))
            return result

    async def get_phase(self, project_name: str, phase_number: int) -> dict | None:
        """Get a specific phase by project name and phase number."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                return None

            phase = await repo.get_phase(conn, project["id"], phase_number)
            if phase is None:
                return None
            return await self._enrich_phase(conn, phase, include_expert=True)

    async def add_criterion(self, project_name: str, phase_number: int, criterion: str) -> bool:
        """Append a success criterion to a phase."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                return False

            phase = await repo.get_phase(conn, project["id"], phase_number)
            if phase is None:
                return False

            criteria = list(_parse_json_field(phase["success_criteria"], []))
            criteria.append(criterion)
            await conn.execute(
                "UPDATE phases SET success_criteria = ? WHERE id = ?",
                (json.dumps(criteria), phase["id"]),
            )
            return True

    async def delete_phase(self, project_name: str, phase_number: int) -> bool:
        """Delete a phase by project name and phase number."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                return False

            return await repo.delete_phase(conn, project["id"], phase_number)

    # ==================================================================
    # Task CRUD
    # ==================================================================

    async def create_task(
        self,
        project_name: str,
        prompt: str,
        phase_number: int,
        name: str = "",
        expert: str | None = None,
        expert_id: str | None = None,
        wave: int = 1,
        interactive: bool = False,
        model: str | None = None,
        task_type: str | None = None,
        retry_policy: str | None = None,
        system_prompt: str = "",
    ) -> dict:
        """Create a new task within a project phase.

        Expert can be specified by ``expert_id`` (preferred) or ``expert`` name.
        """
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                raise ValueError(f"Project '{project_name}' not found")

            phase = await repo.get_phase(conn, project["id"], phase_number)
            if phase is None:
                raise ValueError(
                    f"Phase {phase_number} not found in project '{project_name}'"
                )
            phase_id = phase["id"]

            if not expert_id and expert:
                expert_obj = await repo.get_expert_by_name(conn, expert)
                if expert_obj is None:
                    raise ValueError(f"Expert '{expert}' not found")
                expert_id = expert_obj["id"]

            task_name = name or prompt[:80]

            task = await repo.create_task(
                conn,
                project_id=project["id"],
                phase_id=phase_id,
                name=task_name,
                prompt=prompt,
                wave=wave,
                interactive=interactive,
                model=model,
                expert_id=expert_id,
                task_type=task_type,
                retry_policy=retry_policy,
                system_prompt=system_prompt or None,
            )

            return await self._enrich_task(conn, task)

    async def list_tasks(
        self, project_name: str, phase_number: int | None = None
    ) -> list[dict]:
        """List tasks for a project, optionally filtered by phase."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                raise ValueError(f"Project '{project_name}' not found")

            phase_id: str | None = None
            if phase_number is not None:
                phase = await repo.get_phase(conn, project["id"], phase_number)
                if phase is None:
                    raise ValueError(
                        f"Phase {phase_number} not found in project '{project_name}'"
                    )
                phase_id = phase["id"]

            tasks = await repo.list_tasks(conn, project["id"], phase_id=phase_id)

            # Batch-load expert names to avoid N+1 queries
            expert_names_map: dict[str, str] = {}
            if tasks:
                expert_ids = list({
                    t["expert_id"] for t in tasks if t.get("expert_id")
                })
                if expert_ids:
                    expert_names_map = await repo.get_expert_names_by_ids(
                        conn, expert_ids,
                    )

            result = []
            for t in tasks:
                result.append(
                    await self._enrich_task(
                        conn, t, expert_names_map=expert_names_map,
                    )
                )
            return result

    async def get_recent_task_transitions(self, limit: int = 8) -> list[dict]:
        """Recent task starts/completions/failures with short id and timestamp."""
        async with self._session_factory() as conn:
            return await repo.get_recent_task_transitions(conn, limit=limit)

    async def get_active_tasks_all(self) -> list[dict]:
        """Get running/pending/awaiting_input tasks across all projects (single query)."""
        async with self._session_factory() as conn:
            tasks = await repo.get_active_tasks_all_projects(conn)

            # Batch-load expert names
            expert_names_map: dict[str, str] = {}
            if tasks:
                expert_ids = list({
                    t["expert_id"] for t in tasks if t.get("expert_id")
                })
                if expert_ids:
                    expert_names_map = await repo.get_expert_names_by_ids(
                        conn, expert_ids,
                    )

            result = []
            for t in tasks:
                enriched = await self._enrich_task(
                    conn, t, expert_names_map=expert_names_map,
                )
                # Carry project_name from the batch query
                enriched["project_name"] = t.get("project_name", "")
                result.append(enriched)
            return result

    async def get_task(self, task_id: str) -> dict | None:
        """Get a task by its ID."""
        async with self._session_factory() as conn:
            task = await repo.get_task(conn, task_id)
            if task is None:
                return None
            return await self._enrich_task(conn, task)

    # ==================================================================
    # Discuss / Research
    # ==================================================================

    async def discuss_task(self, target_task_id: str) -> dict | None:
        """Create an interactive discuss task for a specific task.

        The target task_id is stored in ``env_vars["TARGET_TASK_ID"]`` so
        the completion handler can write discuss output to the correct file.
        """
        target, project, phase_number, expert_name = await self._resolve_prep_context(target_task_id)
        if target is None or project is None:
            return None

        from taktis.core.context import get_phase_context
        from taktis.core.prompts import DISCUSS_TASK_PROMPT

        project_context, _ = get_phase_context(
            project["working_dir"], phase_number, task_id=target_task_id,
        )
        prompt = DISCUSS_TASK_PROMPT.format(
            task_name=target.get("name", ""),
            task_expert=expert_name,
            task_wave=target.get("wave", 1),
            task_prompt=target.get("prompt", ""),
            project_context=project_context,
        )
        async with self._session_factory() as conn:
            discusser = await repo.get_expert_by_role(conn, "task_discusser")
        discusser_id = discusser["id"] if discusser else None
        task = await self.create_task(
            project_name=project["name"],
            prompt=prompt,
            phase_number=phase_number,
            name=f"Discuss: {target.get('name', target_task_id)[:50]}",
            expert_id=discusser_id,
            wave=1,
            interactive=True,
            task_type="discuss_task",
        )
        # Store target link in DB so completion handler knows which task this is for
        async with self._session_factory() as conn:
            await repo.update_task(conn, task["id"], env_vars={"TARGET_TASK_ID": target_task_id})
        await self._start_task(task["id"])
        return task

    async def research_task(self, target_task_id: str) -> dict | None:
        """Create a non-interactive research task for a specific task.

        The target task_id is stored in ``env_vars["TARGET_TASK_ID"]`` so
        the completion handler can write research output to the correct file.
        Discuss decisions (if any) are already included in the project context
        via ``get_phase_context(task_id=...)``.
        """
        target, project, phase_number, expert_name = await self._resolve_prep_context(target_task_id)
        if target is None or project is None:
            return None

        from taktis.core.context import get_phase_context
        from taktis.core.prompts import RESEARCH_TASK_PROMPT

        project_context, _ = get_phase_context(
            project["working_dir"], phase_number, task_id=target_task_id,
        )
        prompt = RESEARCH_TASK_PROMPT.format(
            task_name=target.get("name", ""),
            task_expert=expert_name,
            task_prompt=target.get("prompt", ""),
            project_context=project_context,
        )
        async with self._session_factory() as conn:
            researcher = await repo.get_expert_by_role(conn, "task_researcher")
        researcher_id = researcher["id"] if researcher else None
        task = await self.create_task(
            project_name=project["name"],
            prompt=prompt,
            phase_number=phase_number,
            name=f"Research: {target.get('name', target_task_id)[:50]}",
            expert_id=researcher_id,
            wave=1,
            interactive=False,
            task_type="task_researcher",
        )
        async with self._session_factory() as conn:
            await repo.update_task(conn, task["id"], env_vars={"TARGET_TASK_ID": target_task_id})
        await self._start_task(task["id"])
        return task

    async def _resolve_prep_context(
        self, target_task_id: str,
    ) -> tuple[dict | None, dict | None, int | None, str]:
        """Shared preamble for discuss_task/research_task.

        Returns (target_task, project, phase_number, expert_name).
        All None/empty if the task or project cannot be found.
        """
        async with self._session_factory() as conn:
            target = await repo.get_task(conn, target_task_id)
            if not target:
                return None, None, None, ""
            project = await repo.get_project_by_id(conn, target.get("project_id", ""))
            if not project:
                return None, None, None, ""
            phase = await repo.get_phase_by_id(conn, target["phase_id"]) if target.get("phase_id") else None
            phase_number = phase["phase_number"] if phase else None
            expert_name = ""
            if target.get("expert_id"):
                expert = await repo.get_expert_by_id(conn, target["expert_id"])
                if expert:
                    expert_name = expert.get("name", "")
        return target, project, phase_number, expert_name

    # ==================================================================
    # Monitoring
    # ==================================================================

    async def get_status(self) -> dict:
        """Return a global overview: running tasks, projects summary."""
        async with self._session_factory() as conn:
            project_count = await repo.count_projects(conn)
            task_counts = await repo.get_task_counts_by_status(conn)
            running_count = self._get_running_count()

        return {
            "projects": project_count,
            "tasks": task_counts,
            "running_processes": running_count,
            "max_concurrent": 0,  # caller should override if needed
        }

    async def get_interrupted_work(self) -> dict:
        """Return a snapshot of all interrupted work awaiting resumption.

        Queries for two categories:

        * **Interrupted phases** -- phases with ``status='in_progress'`` *and*
          ``current_wave IS NOT NULL``.  These have a valid checkpoint and can
          be resumed via ``resume_phase``.
        * **Interrupted pipelines** -- projects that have at least one
          ``'completed'`` pipeline step **and** at least one non-``'completed'``
          step.  The ``output`` column is excluded from the pipeline query for
          performance (outputs can be large).

        Returns:
            dict with keys:

            * ``'phases'``:    ``[{'id': str, 'name': str, 'project_name': str}, ...]``
        """
        async with self._session_factory() as conn:
            phase_rows = await repo.get_interrupted_phases(conn)

        return {
            "phases": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "project_name": row["project_name"],
                }
                for row in phase_rows
            ],
        }

    async def get_task_output(
        self, task_id: str, tail: int | None = None,
        event_types: list[str] | None = None,
    ) -> list[dict]:
        """Get task output events, optionally only the last *tail* entries."""
        async with self._session_factory() as conn:
            outputs = await repo.get_task_outputs(
                conn, task_id, tail=tail, event_types=event_types,
            )
            return [
                {
                    "id": o.get("id"),
                    "task_id": o["task_id"],
                    "timestamp": o.get("timestamp"),
                    "event_type": o["event_type"],
                    "content": _parse_json_field(o.get("content")),
                }
                for o in outputs
            ]

    async def watch_task(self, task_id: str) -> AsyncIterator[dict]:
        """Yield output events in real-time for a task.

        Subscribes to ``task.output`` events on the event bus and yields each
        matching event.  The caller should break out of the iterator to stop
        watching (e.g. on Ctrl+C).
        """
        queue = self._event_bus.subscribe(EVENT_TASK_OUTPUT)
        try:
            while True:
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Check if task is still running
                    async with self._session_factory() as conn:
                        task = await repo.get_task(conn, task_id)
                        if task is None or task["status"] in ("completed", "failed", "cancelled"):
                            return
                    continue

                data = envelope.get("data", {})
                if data.get("task_id") == task_id:
                    yield {
                        "timestamp": envelope.get("timestamp"),
                        "event": data.get("event", {}),
                    }
        finally:
            self._event_bus.unsubscribe(EVENT_TASK_OUTPUT, queue)

    # ==================================================================
    # Experts
    # ==================================================================

    async def list_experts(self) -> list[dict]:
        """List all registered experts."""
        async with self._session_factory() as conn:
            experts = await repo.list_experts(conn)
            return [self._expert_to_dict(e) for e in experts]

    async def get_expert(self, name: str) -> dict | None:
        """Get an expert by name."""
        async with self._session_factory() as conn:
            expert = await repo.get_expert_by_name(conn, name)
            if expert is None:
                return None
            return self._expert_to_dict(expert)

    async def create_expert(self, name: str, file_content: str = "",
                            description: str = "", system_prompt: str = "",
                            category: str = "") -> dict:
        """Create a custom expert.

        If *file_content* is provided (markdown with YAML frontmatter), the
        metadata is parsed from it.  Otherwise *description*, *system_prompt*,
        and *category* are used directly.
        """
        if file_content:
            from taktis.core.experts import _parse_expert_md
            metadata, body = _parse_expert_md(file_content)
            description = metadata.get("description", description)
            system_prompt = body or system_prompt
            category = metadata.get("category", category)

        expert_dict = await self._expert_registry.create_expert(
            name=name,
            description=description,
            system_prompt=system_prompt,
            category=category,
        )
        return expert_dict

    async def update_expert(self, name: str, **kwargs) -> dict | None:
        """Update a custom expert's fields (description, system_prompt, category)."""
        allowed = {"description", "system_prompt", "category"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return await self.get_expert(name)

        async with self._session_factory() as conn:
            expert = await repo.get_expert_by_name(conn, name)
            if expert is None:
                return None
            updated = await repo.update_expert(conn, name, **updates)
            return self._expert_to_dict(updated)

    async def delete_expert(self, name: str) -> bool:
        """Delete a custom expert. Cannot delete builtins."""
        return await self._expert_registry.delete_expert(name)

    # ==================================================================
    # ==================================================================
    # Dict enrichment helpers
    # ==================================================================

    async def _enrich_project(self, conn, project: dict) -> dict:
        """Build a rich project dict with state, phase_count, and task_count."""
        state = await repo.get_project_state(conn, project["id"])
        phases = await repo.list_phases(conn, project["id"])
        tasks = await repo.list_tasks(conn, project["id"])

        state_dict = None
        if state is not None:
            state_dict = {
                "status": state["status"],
                "current_phase_id": state.get("current_phase_id"),
                "decisions": _parse_json_field(state.get("decisions"), []),
                "blockers": _parse_json_field(state.get("blockers"), []),
                "metrics": _parse_json_field(state.get("metrics"), {}),
            }

        # Compute total cost from actual task costs (sum of cost_usd across tasks)
        total_cost_usd = sum(
            t.get("cost_usd") or 0.0 for t in tasks
        )

        return {
            "id": project["id"],
            "name": project["name"],
            "description": project.get("description"),
            "working_dir": project.get("working_dir"),
            "default_model": project.get("default_model"),
            "default_permission_mode": project.get("default_permission_mode"),
            "default_env_vars": _parse_json_field(project.get("default_env_vars")),
            "planning_options": project.get("planning_options"),
            "created_at": project.get("created_at"),
            "updated_at": project.get("updated_at"),
            "status": state["status"] if state else "idle",
            "phase_count": len(phases),
            "task_count": len(tasks),
            "total_cost_usd": total_cost_usd,
            "state": state_dict,
        }

    async def _enrich_phase(self, conn, phase: dict, include_expert: bool = False) -> dict:
        """Build a rich phase dict with tasks.

        When *include_expert* is True, batch-loads all expert names for the
        phase's tasks in a single query to avoid N+1 lookups.
        """
        tasks = await repo.get_tasks_by_phase(conn, phase["id"])

        # Batch-load expert names to avoid N+1 queries (T2.3)
        expert_names_map: dict[str, str] = {}
        if include_expert and tasks:
            expert_ids = list({
                t["expert_id"] for t in tasks
                if t.get("expert_id")
            })
            if expert_ids:
                expert_names_map = await repo.get_expert_names_by_ids(conn, expert_ids)

        enriched_tasks = []
        for t in tasks:
            enriched_tasks.append(
                await self._enrich_task(
                    conn, t,
                    include_expert=include_expert,
                    expert_names_map=expert_names_map,
                )
            )

        return {
            "id": phase["id"],
            "project_id": phase["project_id"],
            "name": phase["name"],
            "description": phase.get("description"),
            "goal": phase.get("goal"),
            "success_criteria": _parse_json_field(phase.get("success_criteria"), []),
            "phase_number": phase["phase_number"],
            "status": phase["status"],
            "current_wave": phase.get("current_wave"),
            "depends_on_phase_id": phase.get("depends_on_phase_id"),
            "created_at": phase.get("created_at"),
            "completed_at": phase.get("completed_at"),
            "task_count": len(tasks),
            "tasks": enriched_tasks,
            "context_config": phase.get("context_config"),
        }

    async def _enrich_task(
        self,
        conn,
        task: dict,
        include_expert: bool = True,
        expert_names_map: dict[str, str] | None = None,
    ) -> dict:
        """Build a rich task dict with expert name.

        When *expert_names_map* is provided (from batch loading in
        ``_enrich_phase``), the expert name is looked up from the map
        instead of issuing a per-task DB query.
        """
        expert_name = None
        if task.get("expert_id") and include_expert:
            if expert_names_map is not None:
                expert_name = expert_names_map.get(task["expert_id"])
            else:
                # Individual lookup for single-task gets
                expert_row = await repo.get_expert_by_id(conn, task["expert_id"])
                if expert_row is not None:
                    expert_name = expert_row["name"]

        return {
            "id": task["id"],
            "phase_id": task.get("phase_id"),
            "project_id": task["project_id"],
            "name": task["name"],
            "prompt": task.get("prompt"),
            "status": task["status"],
            "wave": task["wave"],
            "depends_on": _parse_json_field(task.get("depends_on"), []),
            "model": task.get("model"),
            "context_window": get_context_window(task.get("model")),
            "permission_mode": task.get("permission_mode"),
            "interactive": bool(task.get("interactive")),
            "task_type": task.get("task_type"),
            "checkpoint_type": task.get("checkpoint_type"),
            "expert": expert_name,
            "expert_id": task.get("expert_id"),
            "session_id": task.get("session_id"),
            "pid": task.get("pid"),
            "cost_usd": task.get("cost_usd"),
            "result_summary": task.get("result_summary"),
            "input_tokens": task.get("input_tokens", 0),
            "output_tokens": task.get("output_tokens", 0),
            "num_turns": task.get("num_turns", 0),
            "started_at": task.get("started_at"),
            "completed_at": task.get("completed_at"),
            "created_at": task.get("created_at"),
        }

    @staticmethod
    def _expert_to_dict(expert: dict) -> dict:
        return {
            "id": expert["id"],
            "name": expert["name"],
            "description": expert.get("description"),
            "system_prompt": expert.get("system_prompt"),
            "category": expert.get("category"),
            "is_builtin": bool(expert.get("is_builtin")),
            "created_at": expert.get("created_at"),
        }
