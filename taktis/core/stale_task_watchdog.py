"""Background watchdog that detects and fails stale running tasks."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from taktis.core.events import EVENT_TASK_FAILED, make_done_callback

if TYPE_CHECKING:
    from taktis.core.manager import ProcessManager

logger = logging.getLogger(__name__)


class StaleTaskWatchdog:
    """Background loop that finds tasks stuck in ``running`` with no recent
    output and marks them as ``failed``.

    Runs every :attr:`CHECK_INTERVAL` seconds.  A task is considered stale when
    it has produced no ``task_outputs`` row for :attr:`STALE_TIMEOUT` seconds
    **and** has no live process in the ProcessManager (or the process itself
    reports ``is_running == False``).

    If the process is still alive in the ProcessManager, the task is not stale
    — the output buffer simply hasn't flushed yet.  This avoids false positives
    when batch-flushed events create gaps in the ``task_outputs`` timestamps.
    """

    STALE_TIMEOUT = 300  # 5 minutes
    CHECK_INTERVAL = 60  # 1 minute

    def __init__(self, event_bus, session_factory, process_manager: ProcessManager | None = None) -> None:
        self._event_bus = event_bus
        self._session_factory = session_factory
        self._process_manager = process_manager
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="stale-task-watchdog")
        self._task.add_done_callback(
            make_done_callback("stale-task-watchdog", self._event_bus)
        )
        logger.info("Stale task watchdog started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Stale task watchdog stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_stale_tasks()
            except Exception:
                logger.exception("Error checking for stale tasks")
            await asyncio.sleep(self.CHECK_INTERVAL)

    async def _check_stale_tasks(self) -> None:
        from taktis import repository as repo

        now = datetime.now(timezone.utc)
        failed_tasks: list[tuple[str, str, int]] = []  # (task_id, project_id, idle_seconds)

        async with self._session_factory() as conn:
            cursor = await conn.execute(
                "SELECT id, project_id, started_at FROM tasks WHERE status = 'running'"
            )
            running_tasks = await cursor.fetchall()

            for task in running_tasks:
                task_id = task["id"]
                project_id = task["project_id"]
                started_at_str = task["started_at"]

                # Query the most recent output timestamp for this task
                cursor = await conn.execute(
                    "SELECT MAX(timestamp) as last_activity FROM task_outputs WHERE task_id = ?",
                    (task_id,),
                )
                row = await cursor.fetchone()
                last_activity_str = row["last_activity"] if row else None

                if last_activity_str is not None:
                    # Has output -- check if it's older than the timeout
                    last_activity = datetime.fromisoformat(last_activity_str)
                    if last_activity.tzinfo is None:
                        last_activity = last_activity.replace(tzinfo=timezone.utc)
                    idle_seconds = (now - last_activity).total_seconds()
                else:
                    # No output at all -- check started_at
                    if not started_at_str:
                        continue
                    started_at = datetime.fromisoformat(started_at_str)
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    idle_seconds = (now - started_at).total_seconds()

                if idle_seconds <= self.STALE_TIMEOUT:
                    continue

                # Check if the process is still alive in the ProcessManager.
                # Output events are batch-flushed (threshold=50), so gaps in
                # task_outputs timestamps don't mean the task is dead — the
                # buffer just hasn't flushed.  Only mark stale if the process
                # is genuinely gone or not tracked.
                if self._process_manager is not None:
                    proc = self._process_manager.get_process(task_id)
                    if proc is not None and proc.is_running:
                        logger.debug(
                            "[%s] No DB output for %ds but process is alive — skipping",
                            task_id, int(idle_seconds),
                        )
                        continue

                logger.warning(
                    "[%s] Stale task detected — no output for %ds and process is dead, marking failed",
                    task_id,
                    int(idle_seconds),
                )

                await repo.update_task(
                    conn,
                    task_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                )
                await repo.create_task_output(
                    conn,
                    task_id=task_id,
                    event_type="error",
                    content={"type": "error", "error": f"Stale task timeout: no output for {int(idle_seconds)}s and process is not running"},
                )

                failed_tasks.append((task_id, project_id, int(idle_seconds)))

        # Publish events after DB transaction commits
        for task_id, project_id, idle_seconds in failed_tasks:
            # Also stop the process if it's somehow still tracked
            if self._process_manager is not None:
                try:
                    await self._process_manager.stop_task(task_id)
                except Exception:
                    pass

            await self._event_bus.publish(EVENT_TASK_FAILED, {
                "task_id": task_id,
                "project_id": project_id,
                "status": "failed",
                "stderr": "Stale task timeout",
            })
