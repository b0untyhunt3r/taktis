"""Wave-based task scheduler with DAG dependency resolution."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from taktis import repository as repo
from taktis.models import (
    DONE_STATUSES, SKIP_TASK_TYPES, TERMINAL_STATUSES,
    PhaseStatus, TaskStatus, TaskType,
)
from taktis.exceptions import SchedulerError
from taktis.core.events import (
    EVENT_PHASE_COMPLETED,
    EVENT_PHASE_FAILED,
    EVENT_PHASE_STARTED,
    EVENT_SYSTEM_ERROR,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_STARTED,
    EVENT_WAVE_COMPLETED,
    EVENT_WAVE_STARTED,
    EventBus,
)
from taktis.core.manager import ProcessManager
from taktis.core.state import StateTracker

logger = logging.getLogger(__name__)

# Re-export from models for backward compat within this module.
_TERMINAL_STATUSES = TERMINAL_STATUSES
_DONE_STATUSES = DONE_STATUSES


# Typed callback signatures for pipeline integration
OnPlanReady = Callable[[str, dict], Coroutine[Any, Any, None]]
OnPipelineTaskComplete = Callable[[str, str, str, str | None], Coroutine[Any, Any, None]]


def _get_project_budget(project: dict) -> int:
    """Read context_budget_chars from planning_options, default 150K."""
    opts = project.get("planning_options") or ""
    if isinstance(opts, str):
        try:
            opts = json.loads(opts) if opts else {}
        except (json.JSONDecodeError, TypeError):
            opts = {}
    if not isinstance(opts, dict):
        opts = {}
    return int(opts.get("context_budget_chars", 150_000))


class WaveScheduler:
    """Wave-based task scheduler with DAG dependency resolution.

    Tasks within a phase are grouped by *wave* number.  Waves execute
    sequentially; within a wave, all tasks run concurrently.  The scheduler
    publishes lifecycle events for waves and phases so that other components
    (e.g. :class:`StateTracker`) can react.
    """

    def __init__(
        self,
        process_manager: ProcessManager,
        event_bus: EventBus,
        state_tracker: StateTracker,
        db_session_factory,
        *,
        on_task_prep_complete: OnPipelineTaskComplete | None = None,
    ) -> None:
        self._manager = process_manager
        self._event_bus = event_bus
        self._state = state_tracker
        self._session_factory = db_session_factory
        self._on_task_prep_complete = on_task_prep_complete
        # Phase IDs currently being processed by execute_phase().
        # Used as a belt-and-suspenders guard against concurrent invocation
        # (run_project / resume_phase / graph_executor could otherwise race).
        self._executing_phase_ids: set[str] = set()

    def set_task_prep_callback(
        self,
        on_task_prep_complete: OnPipelineTaskComplete | None = None,
    ) -> None:
        """Set callback for discuss/research task completions."""
        if on_task_prep_complete is not None:
            self._on_task_prep_complete = on_task_prep_complete

    # ------------------------------------------------------------------
    # Wave auto-assignment (delegated to wave_grouper module)
    # ------------------------------------------------------------------

    @staticmethod
    def auto_assign_waves(tasks: list[dict]) -> dict[int, list[dict]]:
        """Auto-assign wave numbers based on the dependency DAG.

        Delegates to :func:`taktis.core.wave_grouper.auto_assign_waves`.
        """
        from taktis.core.wave_grouper import auto_assign_waves
        return auto_assign_waves(tasks)

    @staticmethod
    def _matches_retry_pattern(error_events: list[dict], patterns: list[str]) -> bool:
        """Check whether any error event matches a retryable pattern."""
        for e in error_events:
            content = str(e.get("content", ""))
            for pattern in patterns:
                if pattern in content:
                    return True
        return False

    @staticmethod
    def _retry_delay(backoff: str, attempt: int, base: float = 2.0) -> float:
        """Calculate retry delay in seconds."""
        if backoff == "linear":
            return base * (attempt + 1)
        elif backoff == "exponential":
            return base ** (attempt + 1)
        return 0.0  # "none" — immediate retry

    # ------------------------------------------------------------------
    # Phase execution
    # ------------------------------------------------------------------

    async def execute_phase(
        self, phase_id: str, project: dict, start_wave: int = 1
    ) -> None:
        """Execute all tasks in a phase, wave by wave.

        1. Group tasks by wave.
        2. For each wave (ascending order, skipping waves < *start_wave*):
           a. Publish ``wave.started``.
           b. Start all tasks in wave concurrently (already-completed tasks
              are silently skipped inside :meth:`execute_task`).
           c. Wait for all tasks to reach a terminal/checkpoint state.
           d. Publish ``wave.completed``.
           e. If all tasks succeeded, write a wave checkpoint via
              ``repository.update_phase_current_wave`` so a resume can pick up
              from the next wave without re-running completed work.
        3. Publish ``phase.completed`` or ``phase.failed``.

        Parameters
        ----------
        phase_id:
            ID of the phase to execute.
        project:
            Project dict (must contain at least ``"id"``).
        start_wave:
            First wave to execute.  Waves numbered below *start_wave* are
            skipped entirely.  Defaults to ``1`` (execute all waves) so
            existing callers are unaffected.  ``resume_phase()`` passes the
            value ``current_wave + 1`` here so that already-completed waves
            are not re-entered.
        """
        # Concurrency guard — refuse a second execute_phase() on the same
        # phase while the first is still running. Without this, overlapping
        # run_project() / resume_phase() calls each start fresh waves on
        # the same phase because the ``tasks in pending/failed`` filter
        # makes already-running waves invisible to the second caller, who
        # then jumps ahead to the next wave. Result: every wave running
        # in parallel instead of sequentially.
        if phase_id in self._executing_phase_ids:
            logger.warning(
                "Phase %s is already being executed — refusing duplicate run",
                phase_id,
            )
            return
        self._executing_phase_ids.add(phase_id)
        try:
            await self._execute_phase_locked(phase_id, project, start_wave)
        finally:
            # Release the guard on every exit path — normal completion,
            # early-return on missing phase, and any unexpected exception
            # escaping the body.  This is the ONLY release point.
            self._executing_phase_ids.discard(phase_id)

    async def _execute_phase_locked(
        self, phase_id: str, project: dict, start_wave: int = 1
    ) -> None:
        """Actual phase-execution body — caller must already hold the
        concurrency guard in ``_executing_phase_ids``.

        Split out from :meth:`execute_phase` so the guard lives in a single
        well-defined try/finally and the body itself can ``return`` freely
        without leaking the lock.
        """
        project_id = project["id"]

        # Load phase and tasks from DB
        async with self._session_factory() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
            if phase is None:
                logger.error("Phase %s not found", phase_id)
                return

            # Update phase status
            await repo.update_phase(conn, phase_id, status="in_progress")

            all_tasks = await repo.get_tasks_by_phase(conn, phase_id)
            # Exclude preparatory tasks (discuss, research) and already completed tasks
            tasks = [
                t for t in all_tasks
                if t.get("task_type") not in SKIP_TASK_TYPES
                and t["status"] in (TaskStatus.PENDING, TaskStatus.FAILED)
            ]
            task_ids = [t["id"] for t in tasks]

        # Update project state
        await self._state.update_status(project_id, "active")
        await self._state.set_current_phase(project_id, phase_id)

        await self._event_bus.publish(
            EVENT_PHASE_STARTED,
            {"phase_id": phase_id, "project_id": project_id, "project_name": project.get("name", ""), "task_count": len(task_ids)},
        )

        # Group tasks by wave
        waves: dict[int, list[dict]] = defaultdict(list)
        for task in tasks:
            waves[task["wave"]].append(task)

        phase_failed = False
        # Tracks the tasks in the wave currently being processed so the outer
        # except block can mark them failed if an unexpected error interrupts
        # the scheduler mid-wave.
        _active_wave_tasks: list[dict] = []

        try:
            for wave_num in sorted(waves):
                # ----------------------------------------------------------
                # start_wave support: skip waves that were already completed
                # in a previous run.  resume_phase() calls execute_phase()
                # with start_wave = current_wave + 1 so this path is taken
                # for every wave before the resume point.
                # ----------------------------------------------------------
                if wave_num < start_wave:
                    logger.debug(
                        "Phase %s wave %d: skipping (start_wave=%d)",
                        phase_id, wave_num, start_wave,
                    )
                    continue

                wave_tasks = waves[wave_num]
                wave_task_ids = [t["id"] for t in wave_tasks]
                # Keep a reference so the outer except block can mark these
                # tasks failed if an unexpected error interrupts the scheduler.
                _active_wave_tasks = wave_tasks

                logger.info(
                    "Phase %s wave %d: starting %d task(s)", phase_id, wave_num, len(wave_tasks)
                )

                await self._event_bus.publish(
                    EVENT_WAVE_STARTED,
                    {
                        "phase_id": phase_id,
                        "project_id": project_id,
                        "project_name": project.get("name", ""),
                        "wave": wave_num,
                        "task_ids": wave_task_ids,
                    },
                )

                # Start all tasks in this wave concurrently.
                # execute_task() skips any task already in 'completed' status,
                # so partial-wave restarts are safe.
                start_coros = [self.execute_task(t["id"], project) for t in wave_tasks]
                start_results = await asyncio.gather(*start_coros, return_exceptions=True)

                # Inspect gather results: execute_task handles its own failures
                # internally, but if an exception somehow escapes it (e.g. an
                # unexpected asyncio.CancelledError), handle it here so the
                # phase doesn't continue as if all tasks started cleanly.
                for task_item, start_exc in zip(wave_tasks, start_results):
                    if isinstance(start_exc, BaseException):
                        logger.error(
                            "Phase %s wave %d: execute_task for task %s raised "
                            "unexpectedly: %s",
                            phase_id, wave_num, task_item["id"], start_exc,
                            exc_info=start_exc,
                        )
                        try:
                            await self._mark_task_failed(
                                task_item["id"], project_id,
                                reason=f"Unexpected error starting task: {start_exc}",
                            )
                        except Exception:
                            logger.warning(
                                "Phase %s: could not mark task %s as failed "
                                "after execute_task exception",
                                phase_id, task_item["id"],
                            )

                # Wait for all wave tasks to finish
                statuses = await self._wait_for_tasks(wave_task_ids)

                await self._event_bus.publish(
                    EVENT_WAVE_COMPLETED,
                    {
                        "phase_id": phase_id,
                        "project_id": project_id,
                        "project_name": project.get("name", ""),
                        "wave": wave_num,
                        "statuses": statuses,
                    },
                )

                # Check for failures – if any task failed, mark phase as failed
                failed_ids = [tid for tid, s in statuses.items() if s == "failed"]
                if failed_ids:
                    logger.warning(
                        "Phase %s wave %d had %d failure(s): %s",
                        phase_id,
                        wave_num,
                        len(failed_ids),
                        failed_ids,
                    )
                    phase_failed = True

                    # Mark all tasks in subsequent (not-yet-started) waves as
                    # 'failed' so the DB and UI reflect the abort rather than
                    # leaving them stuck in 'pending' indefinitely.  Each call
                    # to _mark_task_failed also publishes EVENT_TASK_FAILED so
                    # every subscriber (SSE stream, state tracker)
                    # is notified without having to poll.
                    for future_wave_num in sorted(waves):
                        if future_wave_num <= wave_num:
                            continue
                        for future_task in waves[future_wave_num]:
                            try:
                                await self._mark_task_failed(
                                    future_task["id"],
                                    project_id,
                                    reason=(
                                        f"Aborted: wave {wave_num} had "
                                        f"{len(failed_ids)} failure(s)"
                                    ),
                                )
                            except Exception:
                                logger.warning(
                                    "Phase %s: could not mark future-wave task "
                                    "%s as failed after wave %d abort",
                                    phase_id, future_task["id"], wave_num,
                                )

                    break

                # --------------------------------------------------------------
                # Wave checkpoint
                #
                # WAVE-INVARIANT (1-indexed waves):
                #   current_wave = N  ⟺  "the last *fully* completed wave is N"
                #   After wave 1 finishes: current_wave = 1
                #   After wave 2 finishes: current_wave = 2
                #   On resume: start from current_wave + 1
                #
                # We only checkpoint when the wave had *no* failures so that
                # current_wave always refers to a wave whose tasks all reached
                # 'completed' status.  A wave with failures is not checkpointed;
                # on resume the scheduler re-enters that wave, and the
                # per-task 'completed' guard in execute_task() protects tasks
                # that already succeeded within the same wave.
                #
                # This write is safe in its own session because _wait_for_tasks()
                # only returns after each task's _on_complete() callback has
                # already committed its own status update.
                # --------------------------------------------------------------
                async with self._session_factory() as conn:
                    await repo.update_phase_current_wave(conn, phase_id, wave_num)

        except Exception as exc:
            sched_exc = SchedulerError(
                f"Unexpected error while executing phase {phase_id}",
                cause=exc,
            )
            logger.exception(
                "Unexpected error executing phase %s: %s", phase_id, sched_exc
            )
            phase_failed = True

            # Best-effort: mark any tasks in the active wave as 'failed' so
            # they are not left in 'running' or 'pending' status indefinitely.
            # Tasks that already reached a terminal status are harmlessly
            # overwritten; the phase-failed event published below ensures the
            # UI reflects the correct overall outcome regardless.
            for wt in _active_wave_tasks:
                try:
                    await self._mark_task_failed(
                        wt["id"], project_id, reason=str(sched_exc)
                    )
                except Exception:
                    logger.warning(
                        "Phase %s: could not mark active-wave task %s as "
                        "failed after scheduler error",
                        phase_id, wt["id"],
                    )

        # Finalize phase
        final_status = "failed" if phase_failed else "complete"
        async with self._session_factory() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
            if phase is not None:
                update_kwargs: dict[str, Any] = {"status": final_status}
                if not phase_failed:
                    update_kwargs["completed_at"] = datetime.now(timezone.utc)
                await repo.update_phase(conn, phase_id, **update_kwargs)

        event_type = EVENT_PHASE_FAILED if phase_failed else EVENT_PHASE_COMPLETED
        await self._event_bus.publish(
            event_type,
            {"phase_id": phase_id, "project_id": project_id, "project_name": project.get("name", ""), "status": final_status},
        )

        # Trigger a WAL checkpoint to prevent unbounded WAL file growth
        try:
            from taktis.db import wal_checkpoint
            await wal_checkpoint()
        except Exception:
            logger.warning("WAL checkpoint failed after phase completion", exc_info=True)

        # Spawn phase review if enabled and phase succeeded
        if not phase_failed and phase is not None:
            try:
                options_str = project.get("planning_options", "")
                opts = json.loads(options_str) if options_str else {}
            except (json.JSONDecodeError, TypeError):
                opts = {}
            # Also check per-phase setting in context_config
            phase_level_review = False
            if phase is not None:
                try:
                    cc = json.loads(phase.get("context_config", "") or "{}")
                    phase_level_review = cc.get("phase_review", False)
                except (json.JSONDecodeError, TypeError):
                    pass
            if opts.get("phase_review_enabled") or phase_level_review:
                try:
                    await self._spawn_phase_review(phase, project)
                except Exception as exc:
                    logger.exception(
                        "Phase review failed for phase %s — phase remains complete",
                        phase.get("name", phase_id),
                    )
                    try:
                        await self._event_bus.publish(
                            EVENT_SYSTEM_ERROR,
                            {
                                "reason": "phase_review_spawn_failed",
                                "phase_id": phase_id,
                                "phase_name": phase.get("name", ""),
                                "project_name": project.get("name", ""),
                                "error": str(exc),
                            },
                        )
                    except Exception:
                        logger.warning("Could not publish phase review failure event")

        await self._state.update_status(project_id, "idle")

    # ------------------------------------------------------------------
    # Phase review (delegated to taktis.core.phase_review)
    # ------------------------------------------------------------------

    async def _spawn_phase_review(self, phase: dict, project: dict) -> None:
        """Delegate to :func:`phase_review.spawn_phase_review`."""
        from taktis.core.phase_review import spawn_phase_review
        await spawn_phase_review(self, phase, project)

    @staticmethod
    def _extract_critical_items(review_text: str) -> list[str]:
        """Delegate to :func:`phase_review.extract_critical_items`."""
        from taktis.core.phase_review import extract_critical_items
        return extract_critical_items(review_text)

    # ------------------------------------------------------------------
    # Single task execution
    # ------------------------------------------------------------------

    async def execute_task(self, task_id: str, project: dict) -> None:
        """Execute a single task.

        Resolves the model (task override → project default → sonnet), updates task status in the
        DB throughout its lifecycle, streams output events, and records the
        final state.

        The method returns immediately (without touching the DB or starting a
        process) when the task is already in ``'completed'`` status.  This
        guard is intentionally placed here rather than in :meth:`execute_phase`
        so that *all* callers — including :meth:`_spawn_phase_review` and any
        future entry points — are protected against re-execution on resume.
        """
        project_id = project["id"]
        working_dir = project.get("working_dir", ".")

        # Load task from DB
        async with self._session_factory() as conn:
            task = await repo.get_task(conn, task_id)
            if task is None:
                logger.error("Task %s not found", task_id)
                return

            # Guard: never re-execute a task that already completed.  This
            # protects partial-wave restarts on resume: if a wave had 3 tasks
            # and only task 2 crashed mid-wave, tasks 1 and 3 (completed) are
            # silently skipped while task 2 (failed/pending) is re-run.
            if task["status"] == "completed":
                logger.debug(
                    "Task %s already completed – skipping re-execution", task_id
                )
                return

            # Load expert info if present
            expert: dict | None = None
            if task.get("expert_id"):
                expert = await repo.get_expert_by_id(conn, task["expert_id"])

            # Resolve model: task override → project default → "sonnet"
            system_prompt: str | None = None
            if expert is not None:
                system_prompt = expert.get("system_prompt")

            model = task.get("model") or project.get("default_model") or "sonnet"

            # Non-interactive tasks use bypassPermissions (SDK query() one-shot).
            # Interactive tasks use can_use_tool callback for the first turn.
            # On continuations (--resume), Claude Code may not preserve
            # stdio-granted permissions, so we pass the project's permission
            # mode as a fallback for the underlying process.
            if task.get("interactive"):
                permission_mode = project.get("default_permission_mode") or None
            else:
                permission_mode = "bypassPermissions"

            env_vars: dict[str, str] = {}
            if project.get("default_env_vars"):
                proj_env = project["default_env_vars"]
                if isinstance(proj_env, str):
                    proj_env = json.loads(proj_env)
                env_vars.update(proj_env)
            task_env = task.get("env_vars")
            if task_env:
                if isinstance(task_env, str):
                    task_env = json.loads(task_env)
                env_vars.update(task_env)

            # Update task status to running
            await repo.update_task(
                conn,
                task_id,
                status="running",
                started_at=datetime.now(timezone.utc),
                model=model,
            )

            # Update parent phase to in_progress if it's still not_started
            if task.get("phase_id"):
                phase_row = await repo.get_phase_by_id(conn, task["phase_id"])
                if phase_row and phase_row.get("status") in (None, "not_started"):
                    await repo.update_phase(conn, task["phase_id"], status="in_progress")

            task_prompt = task.get("prompt") or ""
            task_interactive = task.get("interactive")
            task_system_prompt = task.get("system_prompt") or system_prompt
            task_type = task.get("task_type")

            # Inject project context via .taktis/TASK_CONTEXT.md file.
            # We write context to a file instead of the system_prompt CLI arg
            # because Windows has an ~8K command-line length limit and the
            # injected context can be 30K+.
            phase_number = None
            phase_row = None
            if task.get("phase_id"):
                phase_row = await repo.get_phase_by_id(conn, task["phase_id"])
                if phase_row:
                    phase_number = phase_row.get("phase_number")

            # Detect designer phase
            is_designer_phase = False
            if phase_row and phase_row.get("context_config"):
                try:
                    import json as _json
                    cc = _json.loads(phase_row["context_config"])
                    is_designer_phase = cc.get("designer_phase", False)
                except (json.JSONDecodeError, TypeError):
                    pass

            context_manifest = None
            if is_designer_phase:
                # Graph executor already assembled context at create_task time.
                # System prompt already has the context file pointer. Do nothing.
                context = ""
            else:
                # Non-pipeline phases: scheduler assembles context
                from taktis.core.context import generate_state_summary, async_get_phase_context
                state_summary = await generate_state_summary(conn, project_id)
                budget_chars = _get_project_budget(project)
                context, context_manifest = await async_get_phase_context(
                    working_dir, phase_number, task_id=task_id,
                    state_summary=state_summary,
                    budget_chars=budget_chars,
                )

            # Always tell Claude what the working directory is
            wd_note = (
                f"\n\nYour working directory is: {working_dir}\n"
                "All file paths MUST be relative to this directory or use "
                "this absolute path as prefix. Never write to / or other locations.\n"
                "CRITICAL: Always use the Write tool to create files — NEVER use "
                "Bash heredocs, echo, cat, or python scripts to write files. "
                "Bash file writes silently fail on network paths. The Write tool "
                "is reliable on all path types."
            )
            task_system_prompt = (task_system_prompt or "") + wd_note

            from taktis.core.context import write_task_context_file

            if context:  # Non-empty only for non-designer phases
                context_note = write_task_context_file(working_dir, task_id, context)
                if context_note:
                    task_system_prompt += context_note
                elif context:
                    task_system_prompt = (task_system_prompt or "") + "\n\n" + context

            if context_manifest:
                await repo.update_task(conn, task_id, context_manifest=json.dumps(context_manifest))

        # Track cost/result/status via direct callbacks from ProcessManager.
        # This is reliable because the callbacks run inside the same coroutine
        # that reads stdout — no EventBus race conditions.
        task_state = {
            "cost_usd": 0.0, "result_summary": None, "full_result": None,
            "input_tokens": 0, "output_tokens": 0, "num_turns": 0,
            "_text_chunks": [],  # accumulate text from content_block_delta
            "_error_events": [],  # collect error events for retry detection
        }

        # Batch buffer for output events — flushed periodically or on completion
        _output_buffer: list[dict] = []
        _FLUSH_THRESHOLD = 50

        async def _flush_output_buffer() -> None:
            """Write buffered output events to DB in a single batch."""
            if not _output_buffer:
                return
            batch = _output_buffer.copy()
            _output_buffer.clear()
            async with self._session_factory() as conn:
                await repo.create_task_outputs_batch(conn, batch)

        async def _on_output(tid: str, event: dict) -> None:
            """Called for each output event — buffer for batch DB write."""
            evt_type = event.get("type", "unknown")

            # Buffer the event for batch insert
            _output_buffer.append({
                "task_id": tid,
                "event_type": evt_type,
                "content": event,
            })

            # Flush when buffer reaches threshold
            if len(_output_buffer) >= _FLUSH_THRESHOLD:
                await _flush_output_buffer()

            # Track error events for retry detection in _on_complete
            if evt_type == "error":
                task_state["_error_events"].append(event)
            # Accumulate text from streaming deltas for full result reconstruction
            if evt_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    task_state["_text_chunks"].append(delta.get("text", ""))
            if evt_type == "result":
                task_state["cost_usd"] = event.get("cost_usd", task_state["cost_usd"])
                task_state["full_result"] = event.get("result", task_state["full_result"])
                task_state["result_summary"] = event.get("result", task_state["result_summary"])
                task_state["input_tokens"] += event.get("input_tokens", 0)
                task_state["output_tokens"] += event.get("output_tokens", 0)
                task_state["num_turns"] = event.get("num_turns", 0) or task_state["num_turns"]
                # Save session_id so we can resume the conversation later
                sid = event.get("session_id")
                if sid:
                    async with self._session_factory() as conn:
                        await repo.update_task(conn, tid, session_id=sid)
                # Flush buffer + write tokens to DB immediately
                await _flush_output_buffer()
                async with self._session_factory() as conn:
                    await repo.update_task(
                        conn, tid,
                        input_tokens=task_state["input_tokens"],
                        output_tokens=task_state["output_tokens"],
                        cost_usd=task_state["cost_usd"],
                    )
            if event.get("is_checkpoint"):
                await _flush_output_buffer()
                async with self._session_factory() as conn:
                    await repo.update_task(conn, tid, status="awaiting_input")

        async def _on_complete(tid: str, exit_code: int, stderr_text: str) -> None:
            """Called when process finishes — flush buffered outputs, finalise task in DB."""
            await _flush_output_buffer()
            started_at = process.started_at
            finished_at = process.finished_at or datetime.now(timezone.utc)
            duration_s = (finished_at - started_at).total_seconds() if started_at else 0.0

            # Interactive tasks go to awaiting_input on success (not completed).
            # They only truly "complete" via continue_task → _execute_continuation.
            if exit_code == 0 and task_interactive:
                final_status = "awaiting_input"
            elif exit_code != 0:
                # Parse retry policy — no retries unless explicitly configured
                policy_raw = task.get("retry_policy")
                if policy_raw:
                    try:
                        policy = json.loads(policy_raw)
                    except (json.JSONDecodeError, TypeError):
                        policy = {}
                else:
                    policy = {}

                retry_enabled = policy.get("retry_transient", False)
                max_attempts = policy.get("max_attempts", 0)
                backoff = policy.get("backoff", "none")
                retry_patterns = policy.get("retry_on", [])

                current_retries = task.get("retry_count", 0)
                is_retryable = self._matches_retry_pattern(
                    task_state.get("_error_events", []), retry_patterns,
                )

                # Non-retryable error types: retrying will just fail the same
                # way and burn tokens.  context_overflow = task read too many
                # files for the model's context window; usage_limit = user's
                # subscription quota is exhausted.  Both need a structural
                # fix (fan out the task, or wait for the quota reset), not a
                # naive retry.
                non_retryable_types = {"context_overflow", "usage_limit"}
                hit_non_retryable = any(
                    e.get("error_type") in non_retryable_types
                    for e in task_state.get("_error_events", [])
                )
                if hit_non_retryable:
                    is_retryable = False

                if retry_enabled and is_retryable and current_retries < max_attempts:
                    delay = self._retry_delay(backoff, current_retries)
                    if delay > 0:
                        logger.info("[%s] Backing off %.1fs before retry", tid, delay)
                        await asyncio.sleep(delay)
                    logger.warning(
                        "[%s] Retryable error (attempt %d/%d), requeueing",
                        tid, current_retries + 1, max_attempts,
                    )
                    async with self._session_factory() as conn:
                        await repo.update_task(
                            conn, tid,
                            status="pending",
                            completed_at=None,
                            retry_count=current_retries + 1,
                            result_summary=f"Retrying after transient error (attempt {current_retries + 1})",
                        )
                    return  # Skip normal completion flow
                final_status = "failed"
            else:
                final_status = "completed"
            update_kwargs: dict[str, Any] = {
                "status": final_status,
                "cost_usd": task_state["cost_usd"],
                "input_tokens": task_state["input_tokens"],
                "output_tokens": task_state["output_tokens"],
                "num_turns": task_state["num_turns"],
            }
            if final_status != "awaiting_input":
                update_kwargs["completed_at"] = datetime.now(timezone.utc)
            rs = task_state["result_summary"]
            if rs:
                update_kwargs["result_summary"] = rs[:2000] if len(str(rs)) > 2000 else rs
            elif final_status == "failed" and stderr_text:
                update_kwargs["result_summary"] = f"FAILED (exit {exit_code}): {stderr_text[:2000]}"
            elif final_status == "failed":
                update_kwargs["result_summary"] = f"FAILED with exit code {exit_code}"

            async with self._session_factory() as conn:
                await repo.update_task(conn, tid, **update_kwargs)
                if stderr_text:
                    await repo.create_task_output(
                        conn, task_id=tid, event_type="stderr",
                        content={"stderr": stderr_text[:4096]},
                    )

                # Update parent phase status based on sibling tasks
                # Skip for designer (pipeline) phases — the graph executor
                # manages their lifecycle and creates tasks wave-by-wave.
                phase_id = task.get("phase_id")
                if phase_id:
                    phase_row = await repo.get_phase_by_id(conn, phase_id)
                    is_designer = False
                    if phase_row and phase_row.get("context_config"):
                        try:
                            cc = json.loads(phase_row["context_config"])
                            is_designer = cc.get("designer_phase", False)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    if not is_designer:
                        siblings = await repo.get_tasks_by_phase(conn, phase_id)
                        all_done = all(
                            s["status"] in ("completed", "failed", "cancelled")
                            or s["id"] == tid  # this task just finished
                            for s in siblings
                        )
                        if all_done:
                            any_failed = any(
                                s["status"] == "failed"
                                or (s["id"] == tid and final_status == "failed")
                                for s in siblings
                            )
                            phase_status = "failed" if any_failed else "complete"
                            phase_update: dict[str, Any] = {"status": phase_status}
                            if phase_status == "complete":
                                phase_update["completed_at"] = datetime.now(timezone.utc)
                            await repo.update_phase(conn, phase_id, **phase_update)

            # Clean up per-task context file
            try:
                from taktis.core.context import _ctx_dir
                ctx_file = _ctx_dir(working_dir) / f"TASK_CONTEXT_{tid}.md"
                if ctx_file.exists():
                    ctx_file.unlink()
            except Exception as exc:
                logger.debug("Failed to clean up context file for task %s: %s", tid, exc)

            # Use accumulated text from streaming if more complete than SDK result
            accumulated = "".join(task_state["_text_chunks"])
            sdk_result = task_state["full_result"] or ""
            full_result = accumulated if len(accumulated) > len(sdk_result) else (sdk_result or rs)

            # Write per-task result for inter-wave context sharing.
            # Interactive tasks land here with final_status="awaiting_input" on
            # their first turn and are deliberately skipped — their true
            # completion (on ===CONFIRMED===) is handled by
            # execution_service._execute_continuation, which writes the file then.
            if final_status == "completed" and full_result and phase_number:
                try:
                    from taktis.core.context import async_write_task_result
                    await async_write_task_result(
                        working_dir, phase_number, tid, full_result,
                        task_name=task.get("name", ""), wave=task.get("wave", 0),
                    )
                except Exception:
                    logger.exception("Failed to write context files for task %s", tid)

                try:
                    from taktis.core.context import async_cleanup_task_context_file
                    await async_cleanup_task_context_file(working_dir, tid)
                except Exception:
                    logger.exception("Failed to clean up TASK_CONTEXT for task %s", tid)

                try:
                    from taktis.core.context import apply_supersession_if_marked
                    await apply_supersession_if_marked(
                        working_dir, tid, phase_number, full_result,
                    )
                except Exception:
                    logger.exception("Supersession handling failed for task %s", tid)

            logger.info(
                "[%s] on_complete: status=%s, task_type=%s, has_full_result=%s, has_rs=%s",
                tid, final_status, task_type, bool(full_result), bool(rs),
            )
            # Notify on discuss/research task completions
            if final_status == "completed" and task_type in ("discuss_task", "task_researcher"):
                if self._on_task_prep_complete:
                    try:
                        await self._on_task_prep_complete(
                            project_id, tid, task_type, full_result,
                        )
                    except Exception:
                        logger.exception("Task prep callback failed for task %s", tid)

        await self._event_bus.publish(
            EVENT_TASK_STARTED,
            {"task_id": task_id, "project_id": project_id, "project_name": project.get("name", ""), "model": model},
        )

        # Register callbacks BEFORE starting so nothing is missed
        self._manager.register_callbacks(
            task_id, on_output=_on_output, on_complete=_on_complete,
        )

        # Start the Claude process
        try:
            process = await self._manager.start_task(
                task_id=task_id,
                prompt=task_prompt,
                working_dir=working_dir,
                model=model,
                permission_mode=permission_mode,
                system_prompt=task_system_prompt,
                env_vars=env_vars or None,
                interactive=task_interactive,
            )
        except Exception as exc:
            logger.exception("Failed to start process for task %s", task_id)
            self._manager.unregister_callbacks(task_id)
            await self._mark_task_failed(task_id, project_id, reason=str(exc))
            return



    async def _mark_task_failed(self, task_id: str, project_id: str, reason: str = "") -> None:
        """Mark a task as failed in the DB and publish the failure event."""
        async with self._session_factory() as conn:
            update_kwargs: dict[str, Any] = {
                "status": "failed",
                "completed_at": datetime.now(timezone.utc),
            }
            if reason:
                update_kwargs["result_summary"] = f"FAILED: {reason[:2000]}"
            await repo.update_task(conn, task_id, **update_kwargs)

            # Also store the error as a task output entry
            if reason:
                await repo.create_task_output(
                    conn,
                    task_id=task_id,
                    event_type="error",
                    content={"error": reason},
                )

        await self._event_bus.publish(
            EVENT_TASK_FAILED,
            {"task_id": task_id, "project_id": project_id, "status": "failed", "stderr": reason},
        )

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------

    async def _wait_for_tasks(
        self,
        task_ids: list[str],
        timeout: float | None = None,
    ) -> dict[str, str]:
        """Wait for all given tasks to reach a terminal or checkpoint state.

        Args:
            task_ids: Task IDs to wait for.
            timeout: Maximum seconds to wait.  Defaults to
                ``settings.phase_timeout`` (4 hours).  Tasks still pending
                after this are marked ``failed`` with reason ``timed_out``.

        Returns a dict mapping task_id to its final status string.
        """
        if not task_ids:
            return {}

        if timeout is None:
            from taktis.config import settings
            timeout = float(settings.phase_timeout)

        pending = set(task_ids)
        results: dict[str, str] = {}
        start_time = asyncio.get_event_loop().time()

        # Subscribe to completion and failure events
        q_completed = self._event_bus.subscribe(EVENT_TASK_COMPLETED)
        q_failed = self._event_bus.subscribe(EVENT_TASK_FAILED)

        try:
            # Check current status first – some may already be done
            async with self._session_factory() as conn:
                rows = await repo.get_tasks_by_ids(conn, list(pending))
                for task in rows:
                    if task["status"] in _DONE_STATUSES:
                        results[task["id"]] = task["status"]
                        pending.discard(task["id"])

            # Poll + event-driven hybrid to avoid missed events
            while pending:
                # Timeout check
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout:
                    logger.error(
                        "_wait_for_tasks timed out after %.0fs — "
                        "marking %d task(s) as failed: %s",
                        elapsed,
                        len(pending),
                        list(pending),
                    )
                    async with self._session_factory() as conn:
                        for tid in list(pending):
                            await repo.update_task(
                                conn, tid, status="failed",
                                result_summary="Timed out waiting for completion",
                            )
                            results[tid] = "failed"
                            pending.discard(tid)
                    # Publish failure events so UI is notified
                    for tid in list(results):
                        if results[tid] == "failed":
                            await self._event_bus.publish(
                                EVENT_TASK_FAILED,
                                {"task_id": tid, "status": "failed", "reason": "timed_out"},
                            )
                    break

                # Process any queued events
                #
                # We track which queue each event came from — NEVER fall back
                # to ``data.get("status", "completed")``. ProcessManager
                # publishes EVENT_TASK_FAILED with a payload of
                # ``{"task_id", "exit_code", "stderr"}`` and no ``status`` key,
                # so a naive ``.get("status", "completed")`` would silently
                # record every failed task as completed. That caused the
                # scheduler to advance to the next wave even when the
                # previous wave had failures — a real bug observed in
                # Kaiju Phase 8 when 4cdb3781 hit "Prompt is too long" and
                # wave 2 started anyway.
                for q, default_status in (
                    (q_completed, "completed"),
                    (q_failed, "failed"),
                ):
                    while True:
                        try:
                            envelope = q.get_nowait()
                            data = envelope.get("data", {})
                            tid = data.get("task_id")
                            if tid in pending:
                                # Prefer an explicit status from the payload
                                # when present (preserves e.g. "cancelled"),
                                # otherwise use the queue-appropriate default.
                                status = data.get("status") or default_status
                                results[tid] = status
                                pending.discard(tid)
                        except asyncio.QueueEmpty:
                            break

                if not pending:
                    break

                # Periodic DB poll as safety net
                async with self._session_factory() as conn:
                    rows = await repo.get_tasks_by_ids(conn, list(pending))
                    for task in rows:
                        if task["status"] in _DONE_STATUSES:
                            results[task["id"]] = task["status"]
                            pending.discard(task["id"])

                if pending:
                    await asyncio.sleep(2.0)

        finally:
            self._event_bus.unsubscribe(EVENT_TASK_COMPLETED, q_completed)
            self._event_bus.unsubscribe(EVENT_TASK_FAILED, q_failed)

        return results
