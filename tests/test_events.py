"""Tests for the EventBus (taktis.core.events)."""

import asyncio
import time
from unittest.mock import patch

import pytest

from taktis.core.events import (
    EVENT_SYSTEM_ERROR,
    ALL_EVENT_TYPES,
    EventBus,
)


@pytest.mark.asyncio
async def test_subscribe_and_publish():
    """subscribe() returns a queue; publish() delivers the envelope to it."""
    bus = EventBus()
    queue = bus.subscribe("test.event")

    await bus.publish("test.event", {"key": "value"})

    envelope = queue.get_nowait()
    assert envelope["event_type"] == "test.event"
    assert envelope["data"] == {"key": "value"}
    assert "timestamp" in envelope


@pytest.mark.asyncio
async def test_multiple_subscribers():
    """Two subscribers for the same event type both receive the event."""
    bus = EventBus()
    q1 = bus.subscribe("multi")
    q2 = bus.subscribe("multi")

    await bus.publish("multi", {"n": 1})

    e1 = q1.get_nowait()
    e2 = q2.get_nowait()
    assert e1["data"] == {"n": 1}
    assert e2["data"] == {"n": 1}


@pytest.mark.asyncio
async def test_unsubscribe():
    """After unsubscribe, the queue no longer receives events."""
    bus = EventBus()
    queue = bus.subscribe("unsub")

    bus.unsubscribe("unsub", queue)

    await bus.publish("unsub", {"x": 1})

    assert queue.empty()
    assert bus.subscriber_count("unsub") == 0


@pytest.mark.asyncio
async def test_publish_no_subscribers():
    """Publishing with no subscribers does not raise."""
    bus = EventBus()
    # Should not raise
    await bus.publish("nobody.listening", {"hello": "world"})


@pytest.mark.asyncio
async def test_event_envelope_format():
    """The envelope contains event_type, timestamp, and data."""
    bus = EventBus()
    queue = bus.subscribe("fmt")

    await bus.publish("fmt", {"payload": 42})

    envelope = queue.get_nowait()
    assert set(envelope.keys()) == {"event_type", "timestamp", "data"}
    assert envelope["event_type"] == "fmt"
    assert envelope["data"]["payload"] == 42
    # timestamp should be an ISO-format string
    assert isinstance(envelope["timestamp"], str)
    assert "T" in envelope["timestamp"]


@pytest.mark.asyncio
async def test_clear():
    """clear() removes all subscriptions."""
    bus = EventBus()
    q1 = bus.subscribe("a")
    q2 = bus.subscribe("b")

    assert bus.subscriber_count("a") == 1
    assert bus.subscriber_count("b") == 1

    bus.clear()

    assert bus.subscriber_count("a") == 0
    assert bus.subscriber_count("b") == 0

    # Publishing after clear should not deliver anything
    await bus.publish("a", {"after": "clear"})
    assert q1.empty()
    assert q2.empty()


# ---------------------------------------------------------------------------
# EVENT_SYSTEM_ERROR constant
# ---------------------------------------------------------------------------


def test_event_system_error_in_all_event_types():
    """EVENT_SYSTEM_ERROR must be present in ALL_EVENT_TYPES."""
    assert EVENT_SYSTEM_ERROR in ALL_EVENT_TYPES
    assert EVENT_SYSTEM_ERROR == "system.error"


# ---------------------------------------------------------------------------
# QueueFull → system-error escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_publishes_system_error():
    """When a bounded subscriber queue is full, a system.error event is published.

    Happy path: another subscriber on system.error receives the escalation.
    """
    bus = EventBus()

    # Create a bounded queue with maxsize=1, fill it so the next put_nowait raises.
    full_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"pre": "filled"})
    bus._subscribers["some.event"] = [full_queue]

    # Subscribe to system.error to observe the escalation publish.
    error_queue = bus.subscribe(EVENT_SYSTEM_ERROR)

    await bus.publish("some.event", {"value": 42})

    # The original event must have been dropped (queue still has only the pre-fill).
    assert full_queue.qsize() == 1
    assert full_queue.get_nowait()["pre"] == "filled"

    # A system.error event must have been published.
    assert not error_queue.empty(), "Expected a system.error event to be published"
    envelope = error_queue.get_nowait()
    assert envelope["event_type"] == EVENT_SYSTEM_ERROR
    assert envelope["data"]["reason"] == "subscriber_queue_full"
    assert envelope["data"]["dropped_event_type"] == "some.event"
    assert envelope["data"]["dropped_count"] == 1


@pytest.mark.asyncio
async def test_queue_full_system_error_no_infinite_recursion():
    """A full system.error subscriber queue must NOT cause recursive publishing.

    The guard ``event_type != EVENT_SYSTEM_ERROR`` must prevent infinite recursion
    when the system.error subscriber's own queue is also full.
    """
    bus = EventBus()

    # A full bounded queue registered for the originating event.
    full_source_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    full_source_queue.put_nowait({"sentinel": True})
    bus._subscribers["trigger.event"] = [full_source_queue]

    # A full bounded queue registered for system.error — simulates a slow consumer.
    full_error_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    full_error_queue.put_nowait({"sentinel": True})
    bus._subscribers[EVENT_SYSTEM_ERROR] = [full_error_queue]

    # Must return without raising RecursionError or any other exception.
    await bus.publish("trigger.event", {"check": "no-recursion"})

    # Both queues remain at their pre-filled state — neither event was delivered.
    assert full_source_queue.qsize() == 1
    assert full_error_queue.qsize() == 1


@pytest.mark.asyncio
async def test_queue_full_partial_delivery():
    """Events reach non-full subscribers even when some queues are full."""
    bus = EventBus()

    full_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"blocker": True})

    normal_queue: asyncio.Queue[dict] = asyncio.Queue()

    bus._subscribers["mixed.event"] = [full_queue, normal_queue]

    await bus.publish("mixed.event", {"n": 99})

    # Normal (unbounded) subscriber received the event.
    assert not normal_queue.empty()
    envelope = normal_queue.get_nowait()
    assert envelope["data"]["n"] == 99

    # Full queue was not modified (still has the pre-filled item only).
    assert full_queue.qsize() == 1


# ---------------------------------------------------------------------------
# Bounded subscriber queues
# ---------------------------------------------------------------------------


def test_subscribe_returns_bounded_queue():
    """subscribe() returns a queue with maxsize=1000."""
    bus = EventBus()
    queue = bus.subscribe("bounded.test")
    assert queue.maxsize == 1000


# ---------------------------------------------------------------------------
# Stale subscriber sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_subscriber_auto_removed():
    """A subscriber queue that stays full beyond the timeout is swept."""
    bus = EventBus()

    full_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"blocker": True})
    bus._subscribers["stale.test"] = [full_queue]

    # First publish — records the queue as full.
    await bus.publish("stale.test", {"n": 1})
    assert id(full_queue) in bus._full_since

    # Fast-forward the recorded timestamp beyond the threshold.
    bus._full_since[id(full_queue)] = time.monotonic() - bus.STALE_SUBSCRIBER_TIMEOUT - 1

    # Next publish triggers the sweep.
    await bus.publish("stale.test", {"n": 2})
    assert bus.subscriber_count("stale.test") == 0
    assert id(full_queue) not in bus._full_since


@pytest.mark.asyncio
async def test_active_subscriber_not_swept():
    """A queue that accepts events is never swept, even if it was full briefly."""
    bus = EventBus()

    # Bounded queue that still has room.
    active_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=5)
    bus._subscribers["active.test"] = [active_queue]

    await bus.publish("active.test", {"n": 1})

    # Queue accepted the event — should NOT be tracked as full.
    assert id(active_queue) not in bus._full_since
    assert bus.subscriber_count("active.test") == 1


@pytest.mark.asyncio
async def test_unsubscribe_clears_full_since():
    """unsubscribe() removes the queue from _full_since tracking."""
    bus = EventBus()

    full_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"blocker": True})
    bus._subscribers["cleanup.test"] = [full_queue]

    await bus.publish("cleanup.test", {"n": 1})
    assert id(full_queue) in bus._full_since

    bus.unsubscribe("cleanup.test", full_queue)
    assert id(full_queue) not in bus._full_since


@pytest.mark.asyncio
async def test_clear_clears_full_since():
    """clear() wipes _full_since tracking."""
    bus = EventBus()

    full_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"blocker": True})
    bus._subscribers["clear.test"] = [full_queue]

    await bus.publish("clear.test", {"n": 1})
    assert bus._full_since  # non-empty

    bus.clear()
    assert not bus._full_since
