"""Project state tracking: metrics, decisions, and blockers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from taktis import repository as repo
from taktis.core.events import (
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_PHASE_COMPLETED,
    EVENT_SYSTEM_INTERRUPTED_WORK,
    EventBus,
)
from taktis.exceptions import TaktisError
from taktis.utils import parse_json_field

logger = logging.getLogger(__name__)

_DEFAULT_METRICS: dict[str, Any] = {
    "tasks_completed": 0,
    "tasks_failed": 0,
    "total_cost_usd": 0.0,
    "total_duration_s": 0.0,
}


class StateTracker:
    """Tracks project state including metrics, decisions, and blockers.

    Subscribes to the :class:`EventBus` so that task/phase lifecycle events
    automatically update project state in the database.
    """

    def __init__(self, db_session_factory, event_bus: EventBus) -> None:
        self._session_factory = db_session_factory
        self._event_bus = event_bus
        self._queues: list[tuple[str, asyncio.Queue]] = []
        self._bg_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start listening to events for automatic state updates."""
        if self._running:
            return

        self._running = True

        # Subscribe to relevant events
        for event_type in (EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, EVENT_PHASE_COMPLETED):
            q = self._event_bus.subscribe(event_type)
            self._queues.append((event_type, q))

        from taktis.core.events import make_done_callback
        self._bg_task = asyncio.create_task(self._process_events(), name="state-tracker")
        self._bg_task.add_done_callback(make_done_callback(
            "state-tracker", self._event_bus,
            event_data={"component": "StateTracker"},
            on_crash=lambda _exc: setattr(self, "_running", False),
        ))
        logger.info("StateTracker started")

    async def stop(self) -> None:
        """Stop the background event-processing loop."""
        self._running = False

        for event_type, q in self._queues:
            self._event_bus.unsubscribe(event_type, q)
        self._queues.clear()

        if self._bg_task is not None:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
            self._bg_task = None

        logger.info("StateTracker stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_project_state(self, project_id: str) -> dict:
        """Return the full project state as a plain dict."""
        async with self._session_factory() as conn:
            state = await repo.get_project_state(conn, project_id)
            if state is None:
                return {
                    "project_id": project_id,
                    "status": "idle",
                    "decisions": [],
                    "blockers": [],
                    "metrics": dict(_DEFAULT_METRICS),
                    "current_phase_id": None,
                    "last_session_at": None,
                    "last_session_description": None,
                }

            # JSON fields come back as strings from aiosqlite
            return {
                "project_id": state["project_id"],
                "status": state["status"],
                "decisions": parse_json_field(state.get("decisions"), []),
                "blockers": parse_json_field(state.get("blockers"), []),
                "metrics": parse_json_field(state.get("metrics"), dict(_DEFAULT_METRICS)),
                "current_phase_id": state.get("current_phase_id"),
                "last_session_at": state.get("last_session_at"),
                "last_session_description": state.get("last_session_description"),
            }

    async def update_status(self, project_id: str, status: str) -> None:
        """Update the project status (e.g. idle, active, paused)."""
        async with self._session_factory() as conn:
            await self._ensure_state(conn, project_id)
            await repo.update_project_state(conn, project_id, status=status)
        logger.info("Project %s status -> %s", project_id, status)

    async def add_decision(self, project_id: str, decision: dict) -> None:
        """Record a project decision.

        *decision* should contain at minimum ``description`` and ``rationale``,
        and optionally ``task_id``.  A ``timestamp`` is added automatically if
        not present.
        """
        decision.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        async with self._session_factory() as conn:
            await self._ensure_state(conn, project_id)
            state = await repo.get_project_state(conn, project_id)
            decisions = parse_json_field(state.get("decisions"), [])
            decisions.append(decision)
            # Cap at 200 to prevent unbounded growth over time
            if len(decisions) > 200:
                decisions = decisions[-200:]
            await repo.update_project_state(conn, project_id, decisions=decisions)
        logger.debug("Decision recorded for project %s", project_id)

    async def add_blocker(self, project_id: str, blocker: dict) -> None:
        """Record a blocker.

        *blocker* should contain ``description`` and optionally ``task_id``.
        ``resolved`` defaults to ``False`` and ``timestamp`` is auto-set.
        """
        blocker.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        blocker.setdefault("resolved", False)
        async with self._session_factory() as conn:
            await self._ensure_state(conn, project_id)
            state = await repo.get_project_state(conn, project_id)
            blockers = parse_json_field(state.get("blockers"), [])
            blockers.append(blocker)
            # Cap at 200 to prevent unbounded growth over time
            if len(blockers) > 200:
                blockers = blockers[-200:]
            await repo.update_project_state(conn, project_id, blockers=blockers)
        logger.debug("Blocker recorded for project %s", project_id)

    async def resolve_blocker(self, project_id: str, blocker_index: int) -> None:
        """Mark a blocker as resolved by its list index."""
        async with self._session_factory() as conn:
            await self._ensure_state(conn, project_id)
            state = await repo.get_project_state(conn, project_id)
            blockers = parse_json_field(state.get("blockers"), [])
            if 0 <= blocker_index < len(blockers):
                blockers[blocker_index] = dict(blockers[blocker_index])
                blockers[blocker_index]["resolved"] = True
                blockers[blocker_index]["resolved_at"] = datetime.now(timezone.utc).isoformat()
                await repo.update_project_state(conn, project_id, blockers=blockers)
                logger.debug("Blocker %d resolved for project %s", blocker_index, project_id)
            else:
                logger.warning(
                    "Blocker index %d out of range for project %s (total: %d)",
                    blocker_index,
                    project_id,
                    len(blockers),
                )

    async def update_metrics(self, project_id: str, metrics_update: dict) -> None:
        """Merge a metrics update into the project metrics.

        Numeric values are *added* to existing values; non-numeric values are
        overwritten.

        Uses ``BEGIN IMMEDIATE`` to serialize the read-modify-write within a
        single transaction, preventing lost-update races when concurrent
        tasks complete simultaneously.
        """
        async with self._session_factory() as conn:
            # BEGIN IMMEDIATE acquires a reserved lock immediately, preventing
            # concurrent writers from reading stale data between our read and
            # write.  The session's autocommit handles COMMIT on context exit.
            await conn.execute("BEGIN IMMEDIATE")
            await self._ensure_state(conn, project_id)
            state = await repo.get_project_state(conn, project_id)
            metrics = parse_json_field(state.get("metrics"), dict(_DEFAULT_METRICS))
            for key, value in metrics_update.items():
                if isinstance(value, (int, float)) and isinstance(metrics.get(key), (int, float)):
                    metrics[key] = metrics[key] + value
                else:
                    metrics[key] = value
            await repo.update_project_state(conn, project_id, metrics=metrics)
            await conn.commit()
        logger.debug("Metrics updated for project %s: %s", project_id, metrics_update)

    async def set_current_phase(self, project_id: str, phase_id: str | None) -> None:
        """Set the current phase for a project."""
        async with self._session_factory() as conn:
            await self._ensure_state(conn, project_id)
            await repo.update_project_state(conn, project_id, current_phase_id=phase_id)

    async def record_session(self, project_id: str, description: str) -> None:
        """Record the latest session timestamp and description."""
        async with self._session_factory() as conn:
            await self._ensure_state(conn, project_id)
            await repo.update_project_state(
                conn,
                project_id,
                last_session_at=datetime.now(timezone.utc),
                last_session_description=description,
            )

    # ------------------------------------------------------------------
    # Background event processing
    # ------------------------------------------------------------------

    async def _process_events(self) -> None:
        """Background loop that processes events and updates state.

        Uses ``asyncio.wait()`` on queue gets instead of polling at a fixed
        interval.  This reduces CPU usage to near-zero during idle periods
        while still responding immediately when events arrive.
        """
        while self._running:
            # Create a get() task for each subscribed queue
            pending: dict[asyncio.Task, tuple[str, asyncio.Queue]] = {}
            for event_type, queue in list(self._queues):
                task = asyncio.create_task(queue.get(), name=f"state-get-{event_type}")
                pending[task] = (event_type, queue)

            if not pending:
                await asyncio.sleep(1.0)
                continue

            try:
                done, _ = await asyncio.wait(
                    pending.keys(),
                    timeout=2.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                for t in pending:
                    t.cancel()
                raise

            # Process completed gets
            for task in done:
                event_type, _queue = pending[task]
                try:
                    envelope = task.result()
                    await self._handle_event(event_type, envelope)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Error processing %s event: %s", event_type,
                        task.exception() or "unknown",
                        exc_info=True,
                    )

            # Cancel any incomplete gets (they'll be re-created next iteration)
            for task in pending:
                if task not in done and not task.done():
                    task.cancel()

            # Drain any additional events that arrived during processing
            for event_type, queue in list(self._queues):
                while not queue.empty():
                    try:
                        envelope = queue.get_nowait()
                        await self._handle_event(event_type, envelope)
                    except asyncio.QueueEmpty:
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("Error draining %s event", event_type, exc_info=True)

    async def _handle_event(self, event_type: str, envelope: dict) -> None:
        """Dispatch a single event to the appropriate handler."""
        data = envelope.get("data", {})
        project_id = data.get("project_id")
        if not project_id:
            return

        try:
            if event_type == EVENT_TASK_COMPLETED:
                cost = data.get("cost_usd", 0.0)
                duration = data.get("duration_s", 0.0)
                await self.update_metrics(
                    project_id,
                    {
                        "tasks_completed": 1,
                        "total_cost_usd": cost,
                        "total_duration_s": duration,
                    },
                )
            elif event_type == EVENT_TASK_FAILED:
                await self.update_metrics(project_id, {"tasks_failed": 1})
            elif event_type == EVENT_PHASE_COMPLETED:
                phase_id = data.get("phase_id")
                logger.info(
                    "Phase %s completed for project %s", phase_id, project_id
                )
        except Exception:
            logger.exception(
                "Failed to handle %s event for project %s", event_type, project_id
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_state(self, conn, project_id: str) -> None:
        """Ensure a ProjectState row exists for the given project.

        Creates a new row with defaults if one does not already exist.
        """
        state = await repo.get_project_state(conn, project_id)
        if state is None:
            await repo.create_project_state(
                conn,
                project_id,
                status="idle",
                decisions=[],
                blockers=[],
                metrics=dict(_DEFAULT_METRICS),
            )
