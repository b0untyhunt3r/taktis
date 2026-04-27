"""In-process async event bus using asyncio.Queue."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed event dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskStartedEvent:
    task_id: str
    project_id: str = ""
    project_name: str = ""
    model: str = ""
    pid: int | None = None
    working_dir: str = ""


@dataclass(frozen=True, slots=True)
class TaskCompletedEvent:
    task_id: str
    exit_code: int = 0
    project_id: str = ""


@dataclass(frozen=True, slots=True)
class TaskFailedEvent:
    task_id: str
    reason: str = ""
    exit_code: int | None = None
    error: str = ""
    stderr: str = ""
    project_id: str = ""


@dataclass(frozen=True, slots=True)
class TaskOutputEvent:
    task_id: str
    event: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskCheckpointEvent:
    task_id: str
    event: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PhaseStartedEvent:
    phase_id: str
    project_id: str = ""
    project_name: str = ""
    phase_name: str = ""


@dataclass(frozen=True, slots=True)
class PhaseCompletedEvent:
    phase_id: str
    project_id: str = ""
    project_name: str = ""


@dataclass(frozen=True, slots=True)
class PhaseFailedEvent:
    phase_id: str
    project_id: str = ""
    project_name: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class WaveStartedEvent:
    phase_id: str
    project_id: str = ""
    project_name: str = ""
    wave: int = 0


@dataclass(frozen=True, slots=True)
class WaveCompletedEvent:
    phase_id: str
    project_id: str = ""
    project_name: str = ""
    wave: int = 0
    statuses: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PipelinePlanReadyEvent:
    project_id: str
    project_name: str = ""


@dataclass(frozen=True, slots=True)
class SystemInterruptedWorkEvent:
    reason: str = ""
    component: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class SystemErrorEvent:
    reason: str = ""
    dropped_event_type: str = ""
    dropped_count: int = 0
    error: str = ""


# Union of all typed events
TypedEvent = Union[
    TaskStartedEvent, TaskCompletedEvent, TaskFailedEvent,
    TaskOutputEvent, TaskCheckpointEvent,
    PhaseStartedEvent, PhaseCompletedEvent, PhaseFailedEvent,
    WaveStartedEvent, WaveCompletedEvent,
    PipelinePlanReadyEvent,
    SystemInterruptedWorkEvent, SystemErrorEvent,
]

# Map typed event class → string event type
_EVENT_TYPE_MAP: dict[type, str] = {}  # populated after constants defined below


# Well-known event types
EVENT_TASK_STARTED = "task.started"
EVENT_TASK_COMPLETED = "task.completed"
EVENT_TASK_FAILED = "task.failed"
EVENT_TASK_OUTPUT = "task.output"
EVENT_TASK_CHECKPOINT = "task.checkpoint"
EVENT_PHASE_STARTED = "phase.started"
EVENT_PHASE_COMPLETED = "phase.completed"
EVENT_PHASE_FAILED = "phase.failed"
EVENT_WAVE_STARTED = "wave.started"
EVENT_WAVE_COMPLETED = "wave.completed"
EVENT_PIPELINE_PLAN_READY = "pipeline.plan_ready"
EVENT_PIPELINE_GATE_WAITING = "pipeline.gate_waiting"
EVENT_SYSTEM_INTERRUPTED_WORK = "system.interrupted_work"
EVENT_SYSTEM_ERROR = "system.error"

ALL_EVENT_TYPES = frozenset(
    {
        EVENT_TASK_STARTED,
        EVENT_TASK_COMPLETED,
        EVENT_TASK_FAILED,
        EVENT_TASK_OUTPUT,
        EVENT_TASK_CHECKPOINT,
        EVENT_PHASE_STARTED,
        EVENT_PHASE_COMPLETED,
        EVENT_PHASE_FAILED,
        EVENT_WAVE_STARTED,
        EVENT_WAVE_COMPLETED,
        EVENT_PIPELINE_PLAN_READY,
        EVENT_PIPELINE_GATE_WAITING,
        EVENT_SYSTEM_INTERRUPTED_WORK,
        EVENT_SYSTEM_ERROR,
    }
)

# Populate typed event → string mapping
_EVENT_TYPE_MAP.update({
    TaskStartedEvent: EVENT_TASK_STARTED,
    TaskCompletedEvent: EVENT_TASK_COMPLETED,
    TaskFailedEvent: EVENT_TASK_FAILED,
    TaskOutputEvent: EVENT_TASK_OUTPUT,
    TaskCheckpointEvent: EVENT_TASK_CHECKPOINT,
    PhaseStartedEvent: EVENT_PHASE_STARTED,
    PhaseCompletedEvent: EVENT_PHASE_COMPLETED,
    PhaseFailedEvent: EVENT_PHASE_FAILED,
    WaveStartedEvent: EVENT_WAVE_STARTED,
    WaveCompletedEvent: EVENT_WAVE_COMPLETED,
    PipelinePlanReadyEvent: EVENT_PIPELINE_PLAN_READY,
    SystemInterruptedWorkEvent: EVENT_SYSTEM_INTERRUPTED_WORK,
    SystemErrorEvent: EVENT_SYSTEM_ERROR,
})


def typed_event_to_dict(event: TypedEvent) -> tuple[str, dict[str, Any]]:
    """Convert a typed event to (event_type_string, data_dict) for publishing."""
    event_type = _EVENT_TYPE_MAP.get(type(event))
    if event_type is None:
        raise TypeError(f"Unknown event type: {type(event).__name__}")
    return event_type, asdict(event)


class EventBus:
    """In-process async event bus using asyncio.Queue.

    Subscribers receive events via individual ``asyncio.Queue`` instances so
    that each consumer can process at its own pace without blocking
    publishers.
    """

    # How long a queue must remain full before it is considered stale and
    # automatically removed.
    STALE_SUBSCRIBER_TIMEOUT = 60.0  # seconds

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        # Track when a subscriber queue first became full.  Cleared when a
        # successful put_nowait succeeds (queue is still active).
        self._full_since: dict[int, float] = {}  # id(queue) → timestamp
        # Counters for diagnostics (surfaced on /admin page)
        self.total_events_published: int = 0
        self.total_events_dropped: int = 0
        self.total_stale_sweeps: int = 0

    def subscribe(self, event_type: str) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe to *event_type* and return a queue that will receive events.

        The caller should ``await queue.get()`` in a loop to consume events.
        When done, call :meth:`unsubscribe` to clean up.
        """
        # 1000-item cap per subscriber prevents a slow consumer from causing
        # unbounded memory growth.  Stale queues that stay full longer than
        # STALE_SUBSCRIBER_TIMEOUT are automatically removed.
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(queue)
        logger.debug("New subscriber for %s (total: %d)", event_type, len(self._subscribers[event_type]))
        return queue

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish *data* to every subscriber of *event_type*.

        The event envelope includes ``event_type``, ``timestamp``, and the
        caller-supplied ``data`` dict.
        """
        envelope: dict[str, Any] = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }

        self.total_events_published += 1

        subscribers = self._subscribers.get(event_type, [])
        if not subscribers:
            logger.debug("No subscribers for %s", event_type)
            return

        dropped_count = 0
        # Iterate a snapshot so _sweep_stale_subscribers() can safely mutate
        # the underlying list without causing RuntimeError or skipped entries.
        for queue in list(subscribers):
            try:
                queue.put_nowait(envelope)
                # Queue accepted the event — it is still active.
                self._full_since.pop(id(queue), None)
            except asyncio.QueueFull:
                logger.warning(
                    "Subscriber queue full for %s – dropping event", event_type
                )
                dropped_count += 1
                self.total_events_dropped += 1
                # Record when this queue first became full.
                if id(queue) not in self._full_since:
                    self._full_since[id(queue)] = time.monotonic()

        # Remove subscribers that have been full longer than the threshold.
        self._sweep_stale_subscribers()

        if dropped_count and event_type != EVENT_SYSTEM_ERROR:
            # Escalate to ERROR and publish a system-level error event so that
            # UI observers are notified of the dropped events.
            # The EVENT_SYSTEM_ERROR guard prevents infinite recursion if a
            # system-error subscriber's queue is itself full.
            logger.error(
                "%d subscriber queue(s) full for event %r – %d event(s) dropped; "
                "publishing %s",
                dropped_count,
                event_type,
                dropped_count,
                EVENT_SYSTEM_ERROR,
            )
            await self.publish(
                EVENT_SYSTEM_ERROR,
                {
                    "reason": "subscriber_queue_full",
                    "dropped_event_type": event_type,
                    "dropped_count": dropped_count,
                },
            )

        logger.debug("Published %s to %d subscriber(s)", event_type, len(subscribers))

    async def publish_typed(self, event: TypedEvent) -> None:
        """Publish a typed event dataclass.

        Converts the typed event to the legacy ``(event_type, data)`` format
        and delegates to :meth:`publish`.  New code should prefer this method.
        """
        event_type, data = typed_event_to_dict(event)
        await self.publish(event_type, data)

    def unsubscribe(self, event_type: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove *queue* from the subscribers of *event_type*."""
        subscribers = self._subscribers.get(event_type, [])
        try:
            subscribers.remove(queue)
            logger.debug(
                "Unsubscribed from %s (remaining: %d)", event_type, len(subscribers)
            )
        except ValueError:
            logger.warning("Queue not found in subscribers for %s", event_type)

        # Clean up stale-tracking entry for this queue.
        self._full_since.pop(id(queue), None)

        # Clean up empty lists
        if event_type in self._subscribers and not self._subscribers[event_type]:
            del self._subscribers[event_type]

    def subscriber_count(self, event_type: str) -> int:
        """Return the number of active subscribers for *event_type*."""
        return len(self._subscribers.get(event_type, []))

    def _sweep_stale_subscribers(self) -> None:
        """Remove subscriber queues that have been full beyond the threshold.

        Called from :meth:`publish` after each event delivery round.  A queue
        is considered stale when it has been continuously full (no successful
        ``put_nowait``) for longer than :attr:`STALE_SUBSCRIBER_TIMEOUT`.
        """
        now = time.monotonic()
        stale_ids: set[int] = set()
        for qid, since in list(self._full_since.items()):
            if now - since > self.STALE_SUBSCRIBER_TIMEOUT:
                stale_ids.add(qid)

        if not stale_ids:
            return

        removed = 0
        for event_type, sub_list in list(self._subscribers.items()):
            before = len(sub_list)
            sub_list[:] = [q for q in sub_list if id(q) not in stale_ids]
            removed += before - len(sub_list)
            if not sub_list:
                del self._subscribers[event_type]

        # Clean up tracking entries for removed queues.
        for qid in stale_ids:
            self._full_since.pop(qid, None)

        if removed:
            self.total_stale_sweeps += removed
            logger.warning(
                "Swept %d stale subscriber queue(s) (full > %.0fs)",
                removed,
                self.STALE_SUBSCRIBER_TIMEOUT,
            )

    def clear(self) -> None:
        """Remove all subscriptions.  Useful for teardown / testing."""
        self._subscribers.clear()
        self._full_since.clear()
        logger.debug("All subscriptions cleared")


def make_done_callback(
    name: str,
    event_bus: EventBus,
    event_type: str = EVENT_SYSTEM_INTERRUPTED_WORK,
    event_data: dict[str, Any] | None = None,
    on_crash: Callable[[BaseException], None] | None = None,
) -> Callable[[asyncio.Task], None]:
    """Factory for standard crash-handling done callbacks (CLAUDE.md Rule 3).

    Creates a sync ``done_callback`` that:

    1. Returns immediately if the task was cancelled (normal shutdown).
    2. Logs at ERROR with ``exc_info`` if the task raised.
    3. Publishes a failure event to the ``EventBus``.
    4. Guards the publish task itself with a secondary ``done_callback``.
    5. Calls ``on_crash(exc)`` if provided (for component-specific cleanup).

    Args:
        name: Human-readable name for the background task (used in logs).
        event_bus: The ``EventBus`` to publish the crash event to.
        event_type: Event type to publish (default: ``EVENT_SYSTEM_INTERRUPTED_WORK``).
        event_data: Extra fields to merge into the event payload.
        on_crash: Optional sync callable ``(exc) -> None`` for side effects
            (e.g., setting ``self._running = False``).

    Returns:
        A sync callable suitable for :py:meth:`asyncio.Task.add_done_callback`.
    """

    def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
        if task.cancelled():
            return

        exc = task.exception()
        if exc is None:
            return

        logger.error(
            "Background task '%s' crashed: %s", name, exc, exc_info=exc,
        )

        if on_crash is not None:
            try:
                on_crash(exc)
            except Exception:
                logger.exception("on_crash callback failed for '%s'", name)

        try:
            loop = asyncio.get_running_loop()
            data: dict[str, Any] = {"reason": f"{name}_crash", "error": str(exc)}
            if event_data:
                data.update(event_data)
            pub = loop.create_task(
                event_bus.publish(event_type, data),
                name=f"{name}-crash-event",
            )

            def _log_pub_err(pt: asyncio.Task) -> None:  # type: ignore[type-arg]
                if not pt.cancelled() and pt.exception():
                    logger.error(
                        "Failed to publish crash event for '%s': %s",
                        name, pt.exception(),
                    )

            pub.add_done_callback(_log_pub_err)
        except RuntimeError:
            logger.error("No event loop to publish crash event for '%s'", name)

    return _on_done
