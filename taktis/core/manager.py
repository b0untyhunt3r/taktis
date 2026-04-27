"""Manages all SDKProcess instances with concurrency control."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from taktis.core.events import (
    EVENT_TASK_CHECKPOINT,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_OUTPUT,
    EVENT_TASK_STARTED,
    EventBus,
)
from taktis.core.sdk_process import SDKProcess
from taktis.exceptions import TaskExecutionError

logger = logging.getLogger(__name__)


class ProcessManager:
    """Registry and concurrency gate for :class:`SDKProcess` instances.

    At most *max_concurrent* processes run simultaneously; additional
    :meth:`start_task` calls will wait on the internal semaphore until a
    slot is available.
    """

    def __init__(
        self,
        event_bus: EventBus,
        max_concurrent: int = 15,
        claude_command: str = "claude",
    ) -> None:
        self._processes: dict[str, SDKProcess] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._event_bus = event_bus
        self._claude_command = claude_command  # kept for config compat
        self._output_tasks: dict[str, asyncio.Task[None]] = {}
        self._max_concurrent = max_concurrent
        # Per-task callbacks: task_id -> async callable
        self._on_output: dict[str, Any] = {}
        self._on_complete: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_task(
        self,
        task_id: str,
        prompt: str,
        working_dir: str,
        model: str = "sonnet",
        permission_mode: str = "bypassPermissions",
        system_prompt: str | None = None,
        env_vars: dict[str, str] | None = None,
        interactive: bool = False,
    ) -> SDKProcess:
        """Create and start a new :class:`SDKProcess`.

        Uses the Claude Agent SDK for all tasks:
        - Non-interactive: ``query()`` one-shot with ``permission_mode``
        - Interactive + permission_mode: native ``permission_mode`` (no can_use_tool)
        - Interactive + no permission_mode: ``ClaudeSDKClient`` with ``can_use_tool``
        """
        if task_id in self._processes:
            raise ValueError(f"Task {task_id!r} already exists")

        process = SDKProcess(
            task_id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            model=model,
            permission_mode=permission_mode,
            system_prompt=system_prompt,
            env_vars=env_vars,
            interactive=interactive,
        )

        logger.info("[%s] Acquiring concurrency slot …", task_id)
        await self._semaphore.acquire()

        try:
            await process.start()
        except Exception:
            self._semaphore.release()
            raise

        # Between here and create_task(), no monitor exists to release the
        # semaphore on failure.  Wrap in try/except to prevent leaks.
        try:
            self._processes[task_id] = process
            await self._event_bus.publish(
                EVENT_TASK_STARTED,
                {
                    "task_id": task_id,
                    "pid": process.pid,
                    "model": model,
                    "working_dir": working_dir,
                },
            )
            task = asyncio.create_task(
                self._monitor_output(task_id, process),
                name=f"monitor-{task_id}",
            )
        except Exception:
            # Monitor never started — clean up manually
            self._processes.pop(task_id, None)
            self._semaphore.release()
            try:
                await process.stop()
            except Exception:
                logger.warning("[%s] Cleanup stop failed", task_id)
            raise

        # Monitor IS running and owns the semaphore.  These lines cannot
        # practically fail, so a double-release is not a concern.
        task.add_done_callback(self._make_monitor_done_callback(task_id))
        self._output_tasks[task_id] = task

        return process

    async def stop_task(self, task_id: str) -> None:
        """Stop a running task by *task_id*."""
        process = self._processes.get(task_id)
        if process is None:
            logger.warning("[%s] Task not found", task_id)
            return

        # Cancel the monitor task first so it does not race with stop().
        output_task = self._output_tasks.pop(task_id, None)
        monitor_was_running = False
        if output_task is not None and not output_task.done():
            monitor_was_running = True
            output_task.cancel()
            try:
                await output_task
            except asyncio.CancelledError:
                pass

        await process.stop()

        # Only publish EVENT_TASK_FAILED if we cancelled a running monitor.
        # If the monitor already finished, it published its own terminal
        # event — publishing again would cause duplicate failure counts.
        if monitor_was_running:
            await self._event_bus.publish(
                EVENT_TASK_FAILED,
                {
                    "task_id": task_id,
                    "reason": "stopped",
                    "exit_code": process.exit_code,
                },
            )

        # Semaphore is released by the monitor task's finally block.
        # Do NOT release here — the monitor cancel+await above triggers it.

    async def stop_all(self, project_id: str | None = None) -> None:
        """Stop all tracked tasks."""
        task_ids = list(self._processes.keys())
        logger.info("Stopping %d task(s) (project_id=%s)", len(task_ids), project_id)
        await asyncio.gather(
            *(self.stop_task(tid) for tid in task_ids),
            return_exceptions=True,
        )

    async def send_input(self, task_id: str, text: str) -> None:
        """Forward *text* as a follow-up message to an interactive task."""
        process = self._processes.get(task_id)
        if process is None:
            raise KeyError(f"Task {task_id!r} not found")
        await process.send_input(text)

    async def approve_tool(self, task_id: str, updated_input: dict | None = None) -> None:
        """Approve a pending tool permission request."""
        process = self._processes.get(task_id)
        if process is None:
            raise KeyError(f"Task {task_id!r} not found")
        await process.approve_tool(updated_input)

    async def deny_tool(self, task_id: str, message: str = "User denied this action") -> None:
        """Deny a pending tool permission request."""
        process = self._processes.get(task_id)
        if process is None:
            raise KeyError(f"Task {task_id!r} not found")
        await process.deny_tool(message)

    def get_pending_approval(self, task_id: str) -> dict | None:
        """Get details of a pending permission request, or None."""
        process = self._processes.get(task_id)
        if process is None:
            return None
        return process.pending_permission_info

    def get_process(self, task_id: str) -> SDKProcess | None:
        """Return the :class:`SDKProcess` for *task_id*, or ``None``."""
        return self._processes.get(task_id)

    def get_running_count(self) -> int:
        """Return the number of currently-running processes."""
        return sum(1 for p in self._processes.values() if p.is_running)

    def is_sdk_task(self, task_id: str) -> bool:
        """Return True if the task exists (all tasks use SDK now)."""
        return task_id in self._processes

    def remove_dead_process(self, task_id: str) -> None:
        """Remove a dead (non-running) process entry from the registry.

        Safe to call if *task_id* does not exist.  Does NOT release the
        semaphore — the entry is assumed to already be finished.
        """
        proc = self._processes.get(task_id)
        if proc is not None and not proc.is_running:
            self._processes.pop(task_id, None)
            self._output_tasks.pop(task_id, None)
            self._on_output.pop(task_id, None)
            self._on_complete.pop(task_id, None)
            logger.debug("[%s] Removed dead process entry", task_id)

    def unregister_callbacks(self, task_id: str) -> None:
        """Remove any registered callbacks for *task_id*."""
        self._on_output.pop(task_id, None)
        self._on_complete.pop(task_id, None)

    async def continue_task(
        self,
        task_id: str,
        process: "SDKProcess",
        message: str,
        session_id: str,
        on_output=None,
        on_complete=None,
    ) -> "SDKProcess":
        """Resume an interactive task with a new message.

        Acquires a concurrency slot, starts the continuation, and spawns a
        background monitor — mirrors :meth:`start_task` but for resumptions.
        """
        if on_output is not None:
            self._on_output[task_id] = on_output
        if on_complete is not None:
            self._on_complete[task_id] = on_complete

        await self._semaphore.acquire()
        try:
            await process.start_continuation(message, session_id)
        except Exception:
            self._semaphore.release()
            self._on_output.pop(task_id, None)
            self._on_complete.pop(task_id, None)
            raise

        # Between here and create_task(), no monitor exists to release the
        # semaphore on failure.  Wrap in try/except to prevent leaks.
        try:
            self._processes[task_id] = process
            monitor = asyncio.create_task(
                self._monitor_output(task_id, process),
                name=f"monitor-{task_id}",
            )
        except Exception:
            self._processes.pop(task_id, None)
            self._on_output.pop(task_id, None)
            self._on_complete.pop(task_id, None)
            self._semaphore.release()
            try:
                await process.stop()
            except Exception:
                logger.warning("[%s] Cleanup stop failed", task_id)
            raise

        # Monitor IS running and owns the semaphore.
        monitor.add_done_callback(self._make_monitor_done_callback(task_id))
        self._output_tasks[task_id] = monitor

        return process

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    # ------------------------------------------------------------------
    # Background output monitor
    # ------------------------------------------------------------------

    def register_callbacks(
        self,
        task_id: str,
        on_output=None,
        on_complete=None,
    ) -> None:
        """Register per-task callbacks."""
        if on_output is not None:
            self._on_output[task_id] = on_output
        if on_complete is not None:
            self._on_complete[task_id] = on_complete

    async def _monitor_output(self, task_id: str, process: SDKProcess) -> None:
        """Read structured output from *process* and publish events."""
        try:
            async for event in process.stream_output():
                await self._event_bus.publish(
                    EVENT_TASK_OUTPUT,
                    {"task_id": task_id, "event": event},
                )

                # Read callback dynamically — avoids race where callback
                # is registered after the monitor task starts executing.
                on_output_cb = self._on_output.get(task_id)
                if on_output_cb:
                    try:
                        await on_output_cb(task_id, event)
                    except Exception:
                        logger.exception("[%s] on_output callback error", task_id)

                if event.get("is_checkpoint"):
                    await self._event_bus.publish(
                        EVENT_TASK_CHECKPOINT,
                        {"task_id": task_id, "event": event},
                    )

            # Process finished
            exit_code = process.exit_code or 0

            # Run on_complete callback BEFORE publishing events so the DB
            # is updated when SSE handlers query task status.
            on_complete_cb = self._on_complete.get(task_id)
            if on_complete_cb:
                try:
                    await on_complete_cb(task_id, exit_code, "")
                except Exception:
                    logger.exception("[%s] on_complete callback error", task_id)

            if exit_code == 0:
                await self._event_bus.publish(
                    EVENT_TASK_COMPLETED,
                    {"task_id": task_id, "exit_code": exit_code},
                )
            else:
                await self._event_bus.publish(
                    EVENT_TASK_FAILED,
                    {"task_id": task_id, "exit_code": exit_code, "stderr": ""},
                )

        except asyncio.CancelledError:
            logger.debug("[%s] Monitor cancelled", task_id)
            raise

        except Exception:
            logger.exception("[%s] Unexpected error in output monitor", task_id)
            err_complete_cb = self._on_complete.get(task_id)
            if err_complete_cb:
                try:
                    await err_complete_cb(task_id, -1, "monitor_error")
                except Exception:
                    pass
            await self._event_bus.publish(
                EVENT_TASK_FAILED,
                {"task_id": task_id, "reason": "monitor_error"},
            )

        finally:
            self._semaphore.release()
            self._processes.pop(task_id, None)
            self._output_tasks.pop(task_id, None)
            self._on_output.pop(task_id, None)
            self._on_complete.pop(task_id, None)
            logger.debug("[%s] Monitor finished, semaphore released", task_id)

    def _make_monitor_done_callback(
        self, task_id: str
    ) -> Callable[[asyncio.Task[None]], None]:
        """Return a sync ``done_callback`` for a background monitor task.

        Uses the shared :func:`make_done_callback` factory with
        ``EVENT_TASK_FAILED`` so the event bus knows which task crashed.
        """
        from taktis.core.events import make_done_callback
        return make_done_callback(
            f"monitor-{task_id}",
            self._event_bus,
            event_type=EVENT_TASK_FAILED,
            event_data={"task_id": task_id, "reason": "monitor_crash"},
        )
