"""Execution service — task lifecycle, pipeline orchestration, recovery.

Extracted from :class:`taktis.core.engine.Taktis` to keep
the facade thin.  All heavy execution logic (start/stop/continue tasks,
pipeline management, crash recovery) lives here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from taktis import repository as repo
from taktis.core.events import (
    EVENT_SYSTEM_INTERRUPTED_WORK,
    EVENT_TASK_FAILED,
    EventBus,
)
from taktis.core.manager import ProcessManager
from taktis.core.scheduler import WaveScheduler
from taktis.exceptions import (
    TaskExecutionError,
)

if TYPE_CHECKING:
    from taktis.core.engine import Taktis

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExecutionOrchestrator(Protocol):
    """Minimal interface ExecutionService needs from its host.

    Used by apply_plan() and GraphExecutor. Breaks the circular
    dependency between ExecutionService and Taktis.
    """

    @property
    def event_bus(self): ...

    async def get_project(self, name: str) -> dict | None: ...

    async def create_phase(self, *, project_name: str, name: str, goal: str) -> dict: ...

    async def create_task(
        self, *, project_name: str, prompt: str, phase_number: int,
        wave: int = 1, expert: str | None = None,
        task_type: str = "", interactive: bool = False,
    ) -> dict: ...

    async def start_task(self, task_id: str) -> None: ...

    async def delete_phase(self, project_name: str, phase_number: int) -> bool: ...

    async def list_experts(self) -> list[dict]: ...

    async def publish_event(self, event: str, data: dict) -> None: ...


logger = logging.getLogger(__name__)


class ExecutionService:
    """Owns task execution, pipeline management, and crash recovery.

    Instantiated by the :class:`Taktis` facade and wired to the same
    shared components (event bus, process manager, scheduler, etc.).
    """

    def __init__(
        self,
        process_manager: ProcessManager,
        scheduler: WaveScheduler,
        event_bus: EventBus,
        db_session_factory,
        project_service: Any,
        engine: ExecutionOrchestrator | None = None,
    ) -> None:
        self._manager = process_manager
        self._scheduler = scheduler
        self._event_bus = event_bus
        self._session_factory = db_session_factory
        self._project_service = project_service
        # Reference to the facade so apply_plan / GraphExecutor receive
        # the full interface they expect.
        self._engine: ExecutionOrchestrator | None = engine
        # Concurrency guard: project IDs currently being iterated by
        # run_project(). Prevents the "four overlapping _sequential tasks
        # fanning out across every wave at once" failure mode observed
        # when run_project() is invoked multiple times in rapid succession
        # (e.g. by distinct UI clicks or monitor scripts).
        self._running_project_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Background task done_callback helper (Rule 3)
    # ------------------------------------------------------------------

    def _make_task_done_callback(
        self, name: str, event_data: dict[str, Any] | None = None,
    ):
        """Create a done_callback for a background ``asyncio.Task`` (Rule 3)."""
        from taktis.core.events import make_done_callback
        return make_done_callback(
            name, self._event_bus, event_data=event_data,
        )

    # ------------------------------------------------------------------
    # Crash recovery (delegated to crash_recovery module)
    # ------------------------------------------------------------------

    async def _recover_stale_tasks(self) -> None:
        """Recover tasks stuck in active states — delegates to crash_recovery module."""
        from taktis.core.crash_recovery import recover_stale_tasks
        await recover_stale_tasks(self._session_factory)

    async def _recover_unprocessed_reviews(self) -> None:
        """Re-trigger fix loop for reviews with CRITICALs — delegates to crash_recovery module."""
        from taktis.core.crash_recovery import recover_unprocessed_reviews
        await recover_unprocessed_reviews(
            self._session_factory, self._project_service, self._scheduler,
        )

    async def _report_interrupted_work(self) -> None:
        """Log and broadcast interrupted work — delegates to crash_recovery module."""
        from taktis.core.crash_recovery import report_interrupted_work
        await report_interrupted_work(self._project_service, self._event_bus)

    # ------------------------------------------------------------------
    # Plan / Pipeline handling
    # ------------------------------------------------------------------

    async def _handle_plan_ready(self, project_name: str, plan: dict) -> None:
        """Called by the scheduler when a planner task completes with a parsed plan."""
        from taktis.core.planner import apply_plan
        try:
            result = await apply_plan(self._engine, project_name, plan)
            logger.info("Auto-applied plan for '%s': %s", project_name, result)
        except Exception as exc:
            logger.exception(
                "Failed to auto-apply plan for '%s': %s",
                project_name, exc,
            )

    async def _handle_pipeline_task_complete(
        self, project_id: str, task_id: str, task_type: str, result: str | None,
    ) -> None:
        """Handle per-phase one-off task completions (discuss, research)."""
        if task_type in ("discuss_task", "task_researcher") and result:
            await self._handle_task_prep_complete(project_id, task_id, task_type, result)

    async def _handle_task_prep_complete(
        self, project_id: str, task_id: str, task_type: str, result: str,
    ) -> None:
        """Handle discuss-task and task-researcher completions.

        The target task (the one being discussed/researched) is stored in
        the prep task's ``env_vars["TARGET_TASK_ID"]``.
        """
        from taktis.core import context as ctx_mod

        async with self._session_factory() as conn:
            proj = await repo.get_project_by_id(conn, project_id)
            task = await repo.get_task(conn, task_id)
            if not proj or not task or not task.get("phase_id"):
                return
            phase = await repo.get_phase_by_id(conn, task["phase_id"])

        if not phase:
            return

        # Extract target task_id from env_vars
        env_raw = task.get("env_vars") or "{}"
        try:
            env = json.loads(env_raw) if isinstance(env_raw, str) else (env_raw or {})
        except (json.JSONDecodeError, TypeError):
            env = {}
        target_task_id = env.get("TARGET_TASK_ID")
        if not target_task_id:
            logger.warning("Prep task %s has no TARGET_TASK_ID — cannot write context", task_id)
            return

        phase_number = phase["phase_number"]
        working_dir = proj.get("working_dir", ".")

        if task_type == "discuss_task":
            content = result
            if "===CONTEXT===" in result:
                content = result.split("===CONTEXT===", 1)[1].strip()
            ctx_mod.write_task_discuss(working_dir, phase_number, target_task_id, content)
            logger.info("Wrote DISCUSS_%s.md for phase %d", target_task_id, phase_number)

        elif task_type == "task_researcher":
            ctx_mod.write_task_research(working_dir, phase_number, target_task_id, result)
            logger.info("Wrote RESEARCH_%s.md for phase %d", target_task_id, phase_number)

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    async def start_task(self, task_id: str) -> None:
        """Start a single task.

        Raises :class:`TaskExecutionError` if the task is not found or
        is in a non-startable status.
        """
        async with self._session_factory() as conn:
            task = await repo.get_task(conn, task_id)
            if task is None:
                raise TaskExecutionError(
                    f"Task not found", task_id=task_id,
                )

            if task["status"] not in ("pending", "failed", "completed"):
                raise TaskExecutionError(
                    f"Task is in status '{task['status']}', cannot start",
                    task_id=task_id,
                )

            # Clear any stale process entry from a previous run so the
            # ProcessManager doesn't reject the task as a duplicate.
            old_proc = self._manager.get_process(task_id)
            if old_proc is not None:
                if old_proc.is_running:
                    await self._manager.stop_task(task_id)
                else:
                    self._manager.remove_dead_process(task_id)

            # Reset task fields for a fresh run — set to running immediately
            # so the UI shows the correct status on redirect
            await repo.update_task(
                conn, task_id,
                status="running",
                started_at=datetime.now(timezone.utc),
                completed_at=None,
                pid=None,
                session_id=None,
                cost_usd=0.0,
                result_summary=None,
            )
            # Clear old output from previous runs
            await repo.delete_task_outputs(conn, task_id)

            project = await repo.get_project_by_id(conn, task["project_id"])
            project_dict = await self._project_service._enrich_project(conn, project)

        _task = asyncio.create_task(
            self._scheduler.execute_task(task_id, project_dict),
            name=f"task-{task_id}",
        )
        _task.add_done_callback(
            self._make_task_done_callback(f"task-{task_id}", {"task_id": task_id}),
        )

    async def stop_task(self, task_id: str) -> None:
        """Stop a running task.

        Raises :class:`TaskExecutionError` if no running process exists.
        """
        process = self._manager.get_process(task_id)
        if process is None or not process.is_running:
            raise TaskExecutionError(
                "Task is not running", task_id=task_id,
            )

        await self._manager.stop_task(task_id)

        async with self._session_factory() as conn:
            task = await repo.get_task(conn, task_id)
            if task is not None:
                await repo.update_task(
                    conn,
                    task_id,
                    status="cancelled",
                    completed_at=datetime.now(timezone.utc),
                )

    async def continue_task(self, task_id: str, message: str) -> None:
        """Continue a completed/failed task with a follow-up message.

        Resumes the Claude session using the stored session_id, so Claude
        has full context from the previous conversation.

        Raises :class:`TaskExecutionError` if the task is not found,
        not in a continuable status, or has no session to resume.
        """
        async with self._session_factory() as conn:
            task = await repo.get_task(conn, task_id)
            if task is None:
                raise TaskExecutionError(
                    "Task not found", task_id=task_id,
                )

            if task["status"] not in ("completed", "failed", "awaiting_input"):
                raise TaskExecutionError(
                    f"Task is in status '{task['status']}', cannot continue",
                    task_id=task_id,
                )

            session_id = task.get("session_id")
            if not session_id:
                # Fallback: look up session_id from stored result event
                outputs = await repo.get_task_outputs(conn, task_id, tail=500)
                for o in outputs:
                    content = o.get("content")
                    if isinstance(content, str):
                        import json as _json
                        try:
                            content = _json.loads(content)
                        except (ValueError, TypeError) as parse_exc:
                            logger.warning(
                                "Skipping malformed task output for %s: %s",
                                task_id, parse_exc,
                            )
                            continue
                    if isinstance(content, dict) and content.get("type") == "result":
                        session_id = content.get("session_id")
                        if session_id:
                            # Save it for future use
                            await repo.update_task(conn, task_id, session_id=session_id)
                            break

            if not session_id:
                raise TaskExecutionError(
                    "Task has no session_id, cannot resume", task_id=task_id,
                )

            # Clear stale process entry
            old_proc = self._manager.get_process(task_id)
            if old_proc is not None:
                if old_proc.is_running:
                    await self._manager.stop_task(task_id)
                else:
                    self._manager.remove_dead_process(task_id)

            # Update task status to running
            await repo.update_task(
                conn, task_id,
                status="running",
                completed_at=None,
                started_at=datetime.now(timezone.utc),
            )

            project = await repo.get_project_by_id(conn, task["project_id"])
            project_dict = await self._project_service._enrich_project(conn, project)

        # Publish event so SSE clients update the status
        from taktis.core.events import EVENT_TASK_STARTED
        await self._event_bus.publish(
            EVENT_TASK_STARTED,
            {"task_id": task_id, "project_id": task["project_id"], "project_name": project_dict.get("name", "")},
        )

        # Store user message as output entry
        async with self._session_factory() as conn:
            await repo.create_task_output(
                conn, task_id=task_id,
                event_type="user_message",
                content={"type": "user_message", "text": message},
            )

        # Launch continuation via scheduler
        _cont_task = asyncio.create_task(
            self._execute_continuation(task_id, message, session_id, project_dict),
            name=f"continue-{task_id}",
        )

        def _on_continuation_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
            """Last-resort handler: catch any exception that escaped _execute_continuation.

            _execute_continuation wraps its own body in try/except, so this
            callback should only fire on truly unexpected errors (e.g. an
            unhandled exception in the outer try's except block itself).
            CLAUDE.md Rule 3: all create_task() calls must have a done_callback.
            """
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            logger.error(
                "[%s] _execute_continuation task crashed unexpectedly: %s",
                task_id,
                exc,
                exc_info=exc,
            )
            try:
                loop = asyncio.get_running_loop()
                pub_task = loop.create_task(
                    self._event_bus.publish(
                        EVENT_TASK_FAILED,
                        {
                            "task_id": task_id,
                            "reason": "continuation_crash",
                            "error": str(exc),
                        },
                    ),
                    name=f"continue-crash-event-{task_id}",
                )

                def _log_pub_error(pt: asyncio.Task) -> None:  # type: ignore[type-arg]
                    if pt.cancelled():
                        return
                    pub_exc = pt.exception()
                    if pub_exc is not None:
                        logger.error(
                            "[%s] Failed to publish continuation crash event: %s",
                            task_id,
                            pub_exc,
                            exc_info=pub_exc,
                        )

                pub_task.add_done_callback(_log_pub_error)
            except RuntimeError:
                logger.error(
                    "[%s] No running event loop; cannot publish continuation crash event",
                    task_id,
                )

        _cont_task.add_done_callback(_on_continuation_done)

    async def _execute_continuation(
        self, task_id: str, message: str, session_id: str, project: dict,
    ) -> None:
        """Execute a task continuation with the stored session."""
        from taktis.core.sdk_process import SDKProcess
        from taktis.exceptions import format_error_for_user

        working_dir = project.get("working_dir", ".")

        task_state = {
            "cost_usd": 0.0, "result_summary": None,
            "input_tokens": 0, "output_tokens": 0, "num_turns": 0,
            "peak_input_tokens": 0,
        }

        async def _on_output(tid: str, event: dict) -> None:
            async with self._session_factory() as conn:
                await repo.create_task_output(
                    conn, task_id=tid,
                    event_type=event.get("type", "unknown"),
                    content=event,
                )
            if event.get("type") == "result":
                task_state["cost_usd"] = event.get("cost_usd", task_state["cost_usd"])
                task_state["result_summary"] = event.get("result", task_state["result_summary"])
                turn_input = event.get("input_tokens", 0)
                task_state["input_tokens"] += turn_input
                task_state["output_tokens"] += event.get("output_tokens", 0)
                task_state["num_turns"] = event.get("num_turns", 0) or task_state["num_turns"]
                if turn_input > task_state["peak_input_tokens"]:
                    task_state["peak_input_tokens"] = turn_input

        async def _on_complete(tid: str, exit_code: int, stderr_text: str) -> None:
            final_status = "completed" if exit_code == 0 else "failed"

            # Accumulate cost and tokens from previous turns
            async with self._session_factory() as conn:
                old_task = await repo.get_task(conn, tid)
                old_cost = old_task.get("cost_usd", 0) if old_task else 0
                old_in = old_task.get("input_tokens", 0) if old_task else 0
                old_out = old_task.get("output_tokens", 0) if old_task else 0
                task_type = old_task.get("task_type") if old_task else None
                is_interactive = old_task.get("interactive") if old_task else False

            rs = task_state["result_summary"]
            full_result = rs

            logger.info(
                "[%s] _on_complete: exit=%d, is_interactive=%s, rs=%r, has_confirmed=%s",
                tid, exit_code, is_interactive,
                (rs or "")[:80],
                "===CONFIRMED===" in (rs or ""),
            )

            # Interactive tasks stay in awaiting_input until confirmed.
            # The prompt instructs Claude to output ===CONFIRMED=== after
            # the user approves the plan.
            if (
                final_status == "completed"
                and is_interactive
                and full_result
                and "===CONFIRMED===" not in full_result
            ):
                final_status = "awaiting_input"

            update_kwargs: dict[str, Any] = {
                "status": final_status,
                "cost_usd": (old_cost or 0) + task_state["cost_usd"],
                "input_tokens": (old_in or 0) + task_state["input_tokens"],
                "output_tokens": (old_out or 0) + task_state["output_tokens"],
                "num_turns": task_state["num_turns"] or (old_task.get("num_turns", 0) if old_task else 0),
                "peak_input_tokens": max(
                    task_state["peak_input_tokens"],
                    old_task.get("peak_input_tokens", 0) if old_task else 0,
                ),
            }
            if final_status not in ("awaiting_input",):
                update_kwargs["completed_at"] = datetime.now(timezone.utc)
            if rs:
                update_kwargs["result_summary"] = rs[:2000] if len(str(rs)) > 2000 else rs
            elif final_status == "failed":
                update_kwargs["result_summary"] = f"FAILED with exit code {exit_code}"

            async with self._session_factory() as conn:
                await repo.update_task(conn, tid, **update_kwargs)

            # Interactive tasks reach true completion only here (on ===CONFIRMED===);
            # the scheduler's _on_complete fired earlier with status=awaiting_input
            # and skipped the result-file write. Mirror its behavior now so
            # downstream tasks can pick up this task's final output as context.
            if final_status == "completed" and full_result:
                try:
                    phase_number: int | None = None
                    working_dir_for_ctx = working_dir
                    async with self._session_factory() as conn:
                        task_row_ctx = await repo.get_task(conn, tid)
                        if task_row_ctx and task_row_ctx.get("phase_id"):
                            phase_row_ctx = await repo.get_phase_by_id(
                                conn, task_row_ctx["phase_id"],
                            )
                            if phase_row_ctx:
                                phase_number = phase_row_ctx.get("phase_number")
                    if phase_number is not None:
                        from taktis.core.context import async_write_task_result
                        await async_write_task_result(
                            working_dir_for_ctx, phase_number, tid, full_result,
                            task_name=(task_row_ctx.get("name", "") if task_row_ctx else ""),
                            wave=(task_row_ctx.get("wave", 0) if task_row_ctx else 0),
                        )
                except Exception:
                    logger.exception(
                        "Failed to write context files for interactive task %s", tid,
                    )

                try:
                    from taktis.core.context import async_cleanup_task_context_file
                    await async_cleanup_task_context_file(working_dir_for_ctx, tid)
                except Exception:
                    logger.exception(
                        "Failed to clean up TASK_CONTEXT for interactive task %s", tid,
                    )

            if final_status == "completed" and full_result:
                try:
                    phase_number_for_super: int | None = None
                    async with self._session_factory() as conn:
                        task_row_super = await repo.get_task(conn, tid)
                        if task_row_super and task_row_super.get("phase_id"):
                            phase_row_super = await repo.get_phase_by_id(
                                conn, task_row_super["phase_id"],
                            )
                            if phase_row_super:
                                phase_number_for_super = phase_row_super.get("phase_number")
                    from taktis.core.context import apply_supersession_if_marked
                    await apply_supersession_if_marked(
                        working_dir, tid, phase_number_for_super, full_result,
                    )
                except Exception:
                    logger.exception(
                        "Supersession handling failed for interactive task %s", tid,
                    )

            # Notify pipeline only when truly completed
            if final_status == "completed" and task_type and full_result:
                try:
                    await self._handle_pipeline_task_complete(
                        project["id"], tid, task_type, full_result,
                    )
                except Exception as exc:
                    logger.exception(
                        "Pipeline callback failed in continuation for %s: %s",
                        tid,
                        PipelineError(
                            f"Pipeline callback failed for task '{tid}'",
                            cause=exc,
                        ),
                    )

        # Interactive tasks continue without tool permissions (chat only).
        # Non-interactive tasks (e.g. retried one-shot) use bypassPermissions.
        #
        # Outer try/except: catches DB/SDKProcess setup failures that occur
        # before the inner try block.  Without this, a DatabaseError from
        # get_task() or an instantiation error from SDKProcess() would escape
        # _execute_continuation entirely, leaving the task silently stuck in
        # "running" with no EventBus event published.  (CLAUDE.md Rule 2.)
        try:
            async with self._session_factory() as conn:
                task_row = await repo.get_task(conn, task_id)
            is_task_interactive = task_row.get("interactive") if task_row else False
            if is_task_interactive:
                cont_permission = project.get("default_permission_mode") or None
            else:
                cont_permission = "bypassPermissions"

            process = SDKProcess(
                task_id=task_id,
                prompt=message,
                working_dir=working_dir,
                model=project.get("default_model", "sonnet"),
                permission_mode=cont_permission,
                env_vars={},
                interactive=is_task_interactive,
            )

            try:
                await self._manager.continue_task(
                    task_id=task_id,
                    process=process,
                    message=message,
                    session_id=session_id,
                    on_output=_on_output,
                    on_complete=_on_complete,
                )
            except Exception as exc:
                wrapped = TaskExecutionError(
                    f"Failed to continue task '{task_id}'",
                    task_id=task_id,
                    cause=exc,
                )
                logger.exception("Failed to continue task %s: %s", task_id, wrapped)
                _summary = f"FAILED: {type(exc).__name__}: {exc}"
                async with self._session_factory() as conn:
                    await repo.update_task(
                        conn, task_id,
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        result_summary=_summary[:1000] if len(_summary) > 1000 else _summary,
                    )
                await self._event_bus.publish(
                    EVENT_TASK_FAILED,
                    {
                        "task_id": task_id,
                        "reason": "continuation_start_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )

        except Exception as exc:
            # Catches failures in the setup section (DB query, SDKProcess
            # instantiation) or in the inner except's own cleanup code.
            # The task is still in "running" status at this point — mark it
            # failed so it does not stay stuck, and publish the failure event
            # so the UI is notified.
            wrapped = TaskExecutionError(
                f"Unexpected error in continuation setup for task '{task_id}'",
                task_id=task_id,
                cause=exc,
            )
            logger.error(
                "[%s] %s",
                task_id,
                wrapped,
                exc_info=exc,
            )
            _summary = f"FAILED: {type(exc).__name__}: {exc}"
            try:
                async with self._session_factory() as conn:
                    await repo.update_task(
                        conn, task_id,
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        result_summary=_summary[:1000] if len(_summary) > 1000 else _summary,
                    )
            except Exception as db_exc:
                logger.error(
                    "[%s] Failed to mark task as failed after setup error: %s",
                    task_id,
                    TaskExecutionError(
                        f"DB cleanup failed for task '{task_id}'",
                        task_id=task_id,
                        cause=db_exc,
                    ),
                    exc_info=db_exc,
                )
            await self._event_bus.publish(
                EVENT_TASK_FAILED,
                {
                    "task_id": task_id,
                    "reason": "continuation_setup_failed",
                    "error": format_error_for_user(exc),
                },
            )

    # ------------------------------------------------------------------
    # Phase / project execution
    # ------------------------------------------------------------------

    async def run_phase(self, project_name: str, phase_number: int) -> None:
        """Execute all tasks in a phase wave-by-wave."""
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                raise ValueError(f"Project '{project_name}' not found")

            # Guard: archived projects cannot run
            state = await repo.get_project_state(conn, project["id"])
            if state and state.get("status") == "archived":
                raise ValueError(f"Project '{project_name}' is archived and cannot run tasks")

            phase = await repo.get_phase(conn, project["id"], phase_number)
            if phase is None:
                raise ValueError(
                    f"Phase {phase_number} not found in project '{project_name}'"
                )

            project_dict = await self._project_service._enrich_project(conn, project)
            phase_id = phase["id"]

        _phase_task = asyncio.create_task(
            self._scheduler.execute_phase(phase_id, project_dict),
            name=f"phase-{phase_id}",
        )
        _phase_task.add_done_callback(
            self._make_task_done_callback(f"phase-{phase_id}", {"phase_id": phase_id}),
        )

    async def run_project(self, project_name: str) -> None:
        """Execute all phases sequentially in phase_number order.

        Skips phases that are already complete.  Stops on first failed phase.
        Runs as a background asyncio task so the caller returns immediately.
        """
        async with self._session_factory() as conn:
            project = await repo.get_project_by_name(conn, project_name)
            if project is None:
                raise ValueError(f"Project '{project_name}' not found")

            # Guard: archived projects cannot run
            state = await repo.get_project_state(conn, project["id"])
            if state and state.get("status") == "archived":
                raise ValueError(f"Project '{project_name}' is archived and cannot run tasks")
            project_dict = await self._project_service._enrich_project(conn, project)

            phases_raw = await repo.list_phases(conn, project["id"])

        project_id = project["id"]
        # Concurrency guard — refuse a second run_project() while the first
        # is still iterating. Without this, two overlapping calls each spawn
        # a separate ``_sequential`` task and both iterate the same phase
        # list; the second call's ``execute_phase`` sees wave N's tasks in
        # ``running`` status (filtered out as not-pending), skips wave N,
        # and jumps straight to wave N+1 — resulting in every wave of a
        # phase running in parallel instead of sequentially.
        if project_id in self._running_project_ids:
            raise ValueError(
                f"Project '{project_name}' is already running — "
                "wait for the current execution to finish before retrying"
            )
        self._running_project_ids.add(project_id)

        phases = sorted(phases_raw, key=lambda p: p["phase_number"])

        async def _sequential():
            try:
                for phase in phases:
                    if phase["status"] == "complete":
                        continue
                    # execute_phase is synchronous (awaits all waves internally)
                    await self._scheduler.execute_phase(phase["id"], project_dict)
                    # Re-read phase to check if it failed
                    async with self._session_factory() as conn:
                        updated = await repo.get_phase_by_id(conn, phase["id"])
                    if updated and updated.get("status") == "failed":
                        logger.warning(
                            "Phase %d failed, stopping project execution",
                            phase["phase_number"],
                        )
                        break
            finally:
                self._running_project_ids.discard(project_id)

        _proj_task = asyncio.create_task(_sequential(), name=f"project-{project_name}")
        _proj_task.add_done_callback(
            self._make_task_done_callback(f"project-{project_name}"),
        )

    async def resume_phase(self, phase_id: str) -> None:
        """Resume an interrupted phase from its last wave checkpoint.

        Loads the phase from the DB, determines the correct wave to resume from
        (``current_wave + 1`` per the WAVE-INVARIANT), resets stuck tasks to
        ``'pending'``, and delegates to the scheduler starting at that wave.

        The scheduler's :meth:`WaveScheduler.execute_phase` skips waves below
        ``start_wave`` and also skips tasks already in ``'completed'`` status
        (the task-level guard from Phase 2 Task 1).  This avoids duplicating
        wave-iteration logic here.

        Args:
            phase_id: UUID of the phase to resume.

        Raises:
            ValueError: Phase not found, already complete, or has no valid
                resume point (no checkpoint **and** no failed/running tasks).
        """
        async with self._session_factory() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
            if phase is None:
                raise ValueError(f"Phase '{phase_id}' not found")

            if phase["status"] == "complete":
                raise ValueError(
                    f"Phase '{phase_id}' ('{phase.get('name', '')}') is already complete"
                )

            # WAVE-INVARIANT: current_wave = last *fully* completed wave.
            # Resume from the next wave after the last completed one.
            current_wave: int | None = phase.get("current_wave")
            if current_wave is not None:
                resume_wave = current_wave + 1
            else:
                resume_wave = 1

            # Load all tasks so we can reset interrupted ones and check edge cases.
            all_tasks = await repo.get_tasks_by_phase(conn, phase_id)

            # Edge case: no checkpoint AND no interrupted tasks → nothing to resume.
            # The phase either never started or all tasks are already pending/complete.
            if current_wave is None:
                interrupted = [
                    t for t in all_tasks if t["status"] in ("running", "failed")
                ]
                if not interrupted:
                    raise ValueError(
                        f"Phase '{phase_id}' has no wave checkpoint and no "
                        "failed/running tasks — no resume point exists. "
                        "Use run_phase() to start fresh."
                    )

            # Reset tasks in resume_wave or later that are stuck to 'pending'
            # so the scheduler will re-execute them.
            reset_count = 0
            for task in all_tasks:
                if task["wave"] >= resume_wave and task["status"] in ("running", "failed"):
                    await repo.update_task(conn, task["id"], status="pending")
                    reset_count += 1

            # Reflect the resumption in the phase status.
            await repo.update_phase(conn, phase_id, status="in_progress")

            # Load the full project dict that the scheduler expects.
            project = await repo.get_project_by_id(conn, phase["project_id"])
            if project is None:
                raise ValueError(
                    f"Project for phase '{phase_id}' not found"
                )
            project_dict = await self._project_service._enrich_project(conn, project)

        logger.info(
            "Resuming phase '%s' (id=%s) from wave %d — reset %d task(s) to pending",
            phase.get("name", ""), phase_id, resume_wave, reset_count,
        )

        # Delegate to the scheduler.  execute_phase() skips waves < start_wave
        # and skips individual completed tasks within each wave.
        _resume_task = asyncio.create_task(
            self._scheduler.execute_phase(phase_id, project_dict, start_wave=resume_wave),
            name=f"resume-phase-{phase_id}",
        )
        _resume_task.add_done_callback(
            self._make_task_done_callback(f"resume-phase-{phase_id}", {"phase_id": phase_id}),
        )

    # ------------------------------------------------------------------
    # Stop all
    # ------------------------------------------------------------------

    async def stop_all(self, project_name: str | None = None) -> int:
        """Stop all running tasks, optionally filtered to a project.

        Returns the number of tasks stopped.
        """
        # Count running tasks before stopping
        count = self._manager.get_running_count()

        if project_name is None:
            await self._manager.stop_all()
        else:
            async with self._session_factory() as conn:
                project = await repo.get_project_by_name(conn, project_name)
                if project is None:
                    return 0

                task_ids = await repo.get_task_ids_by_project_and_status(
                    conn, project["id"], "running",
                )

            count = len(task_ids)
            for tid in task_ids:
                await self._manager.stop_task(tid)

        # Update DB status for stopped tasks
        async with self._session_factory() as conn:
            task_ids = await repo.get_task_ids_by_status(conn, "running")
            for tid in task_ids:
                proc = self._manager.get_process(tid)
                if proc is None or not proc.is_running:
                    await repo.update_task(
                        conn,
                        tid,
                        status="cancelled",
                        completed_at=datetime.now(timezone.utc),
                    )

        return count

    # ------------------------------------------------------------------
    # Interactive
    # ------------------------------------------------------------------

    async def send_input(self, task_id: str, text: str) -> None:
        """Send text input to an interactive task.

        Raises :class:`TaskExecutionError` on failure.
        """
        # Store user's reply in conversation history
        async with self._session_factory() as conn:
            await repo.create_task_output(
                conn, task_id=task_id, event_type="user_message",
                content={"type": "user_message", "text": text},
            )
        try:
            await self._manager.send_input(task_id, text)
        except Exception as exc:
            raise TaskExecutionError(
                "Failed to send input", task_id=task_id, cause=exc,
            ) from exc
        # Restore running status if the task was awaiting input
        async with self._session_factory() as conn:
            task = await repo.get_task(conn, task_id)
            if task is not None and task["status"] == "awaiting_input":
                await repo.update_task(conn, task_id, status="running")

    async def approve_checkpoint(self, task_id: str) -> None:
        """Approve a human-verify checkpoint.

        For SDK interactive tasks, approves the pending tool permission.

        Raises :class:`TaskExecutionError` on failure.
        """
        if self._manager.is_sdk_task(task_id):
            # Store approval in conversation history
            async with self._session_factory() as conn:
                await repo.create_task_output(
                    conn, task_id=task_id, event_type="user_message",
                    content={"type": "user_message", "text": "Approved tool use"},
                )
            try:
                await self._manager.approve_tool(task_id)
            except Exception as exc:
                raise TaskExecutionError(
                    "Failed to approve tool", task_id=task_id, cause=exc,
                ) from exc
            async with self._session_factory() as conn:
                task = await repo.get_task(conn, task_id)
                if task is not None and task["status"] == "awaiting_input":
                    await repo.update_task(conn, task_id, status="running")
        else:
            await self.send_input(task_id, "approved")

    async def deny_tool(self, task_id: str, message: str = "User denied this action") -> None:
        """Deny a pending tool permission request (SDK interactive tasks only).

        Raises :class:`TaskExecutionError` on failure.
        """
        # Store denial in conversation history
        async with self._session_factory() as conn:
            await repo.create_task_output(
                conn, task_id=task_id, event_type="user_message",
                content={"type": "user_message", "text": f"Denied: {message}"},
            )
        try:
            await self._manager.deny_tool(task_id, message)
        except Exception as exc:
            raise TaskExecutionError(
                "Failed to deny tool", task_id=task_id, cause=exc,
            ) from exc
        async with self._session_factory() as conn:
            task = await repo.get_task(conn, task_id)
            if task is not None and task["status"] == "awaiting_input":
                await repo.update_task(conn, task_id, status="running")

    async def decide_checkpoint(self, task_id: str, option: str) -> None:
        """Select an option for a decision checkpoint."""
        await self.send_input(task_id, option)

    async def get_pending_approval(self, task_id: str) -> dict | None:
        """Get details of a pending tool approval request, or None."""
        return self._manager.get_pending_approval(task_id)
