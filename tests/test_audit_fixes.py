"""Tests for the multi-agent audit fixes.

These tests verify the specific bugs found during the 10-agent audit
and ensure the fixes are correct and don't regress.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from taktis.core.events import (
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_OUTPUT,
    EVENT_TASK_STARTED,
    EventBus,
    TaskStartedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    typed_event_to_dict,
)


# ===========================================================================
# EventBus: sweep mutation safety (Bug #2 / Fix #3)
# ===========================================================================

class TestEventBusSweepSafety:
    """Verify that publish() iterates a snapshot so sweep can mutate safely."""

    @pytest.mark.asyncio
    async def test_publish_with_stale_subscriber_sweep(self):
        """Publishing to a full queue triggers sweep without RuntimeError."""
        bus = EventBus()
        bus.STALE_SUBSCRIBER_TIMEOUT = 0.0  # immediate stale

        q = bus.subscribe(EVENT_TASK_STARTED)
        # Fill the queue
        for _ in range(1000):
            q.put_nowait({"data": "filler"})

        # Mark queue as already full so sweep sees it
        import time
        bus._full_since[id(q)] = time.monotonic() - 1.0

        # Sweep directly — verify no crash from list mutation
        bus._sweep_stale_subscribers()
        assert bus.subscriber_count(EVENT_TASK_STARTED) == 0

    @pytest.mark.asyncio
    async def test_concurrent_subscribe_unsubscribe_during_publish(self):
        """Subscribe and unsubscribe don't crash concurrent publish."""
        bus = EventBus()
        errors = []

        async def publisher():
            for i in range(50):
                try:
                    await bus.publish(EVENT_TASK_OUTPUT, {"task_id": f"t{i}"})
                except Exception as e:
                    errors.append(e)
                await asyncio.sleep(0)

        async def subscriber_churn():
            for _ in range(50):
                q = bus.subscribe(EVENT_TASK_OUTPUT)
                await asyncio.sleep(0)
                bus.unsubscribe(EVENT_TASK_OUTPUT, q)

        await asyncio.gather(publisher(), subscriber_churn())
        assert errors == [], f"Errors during concurrent publish/subscribe: {errors}"

    @pytest.mark.asyncio
    async def test_full_since_bounded(self):
        """_full_since dict doesn't grow unbounded with many full queues."""
        import time
        bus = EventBus()
        bus.STALE_SUBSCRIBER_TIMEOUT = 0.0

        # Create and fill many subscribers, mark them as stale
        for _ in range(100):
            q = bus.subscribe(EVENT_TASK_OUTPUT)
            for _ in range(1000):
                q.put_nowait({"data": "fill"})
            bus._full_since[id(q)] = time.monotonic() - 1.0

        # Sweep directly
        bus._sweep_stale_subscribers()

        # All stale queues should be removed
        assert len(bus._full_since) == 0
        assert bus.subscriber_count(EVENT_TASK_OUTPUT) == 0


# ===========================================================================
# EventBus: typed events (Fix #7)
# ===========================================================================

class TestTypedEvents:
    """Verify typed event dataclasses work correctly."""

    def test_typed_event_to_dict(self):
        event = TaskStartedEvent(task_id="abc123", project_id="proj1", model="sonnet")
        event_type, data = typed_event_to_dict(event)
        assert event_type == EVENT_TASK_STARTED
        assert data["task_id"] == "abc123"
        assert data["project_id"] == "proj1"
        assert data["model"] == "sonnet"

    def test_typed_event_completed(self):
        event = TaskCompletedEvent(task_id="abc123", exit_code=0)
        event_type, data = typed_event_to_dict(event)
        assert event_type == EVENT_TASK_COMPLETED
        assert data["exit_code"] == 0

    def test_typed_event_failed(self):
        event = TaskFailedEvent(task_id="abc123", reason="crashed")
        event_type, data = typed_event_to_dict(event)
        assert event_type == EVENT_TASK_FAILED
        assert data["reason"] == "crashed"

    @pytest.mark.asyncio
    async def test_publish_typed(self):
        bus = EventBus()
        q = bus.subscribe(EVENT_TASK_STARTED)
        event = TaskStartedEvent(task_id="abc123", project_id="proj1")
        await bus.publish_typed(event)
        envelope = q.get_nowait()
        assert envelope["data"]["task_id"] == "abc123"
        assert envelope["event_type"] == EVENT_TASK_STARTED

    def test_unknown_event_type_raises(self):
        with pytest.raises(TypeError, match="Unknown event type"):
            typed_event_to_dict("not an event")  # type: ignore


# ===========================================================================
# ProcessManager: encapsulation (Fix #1)
# ===========================================================================

class TestProcessManagerEncapsulation:
    """Verify that no external code directly accesses ProcessManager internals."""

    def test_remove_dead_process_nonexistent(self):
        """remove_dead_process is a no-op for unknown task IDs."""
        bus = EventBus()
        from taktis.core.manager import ProcessManager
        pm = ProcessManager(bus, max_concurrent=5)
        pm.remove_dead_process("nonexistent")  # should not raise

    def test_unregister_callbacks_nonexistent(self):
        """unregister_callbacks is a no-op for unknown task IDs."""
        bus = EventBus()
        from taktis.core.manager import ProcessManager
        pm = ProcessManager(bus, max_concurrent=5)
        pm.unregister_callbacks("nonexistent")  # should not raise

    def test_register_and_unregister_callbacks(self):
        """Callbacks can be registered and unregistered."""
        bus = EventBus()
        from taktis.core.manager import ProcessManager
        pm = ProcessManager(bus, max_concurrent=5)

        async def cb(*args): pass
        pm.register_callbacks("t1", on_output=cb, on_complete=cb)
        assert "t1" in pm._on_output
        assert "t1" in pm._on_complete
        pm.unregister_callbacks("t1")
        assert "t1" not in pm._on_output
        assert "t1" not in pm._on_complete


# ===========================================================================
# Path traversal validation (Fix #5)
# ===========================================================================

class TestPathTraversalValidation:
    """Verify that path traversal attacks are blocked."""

    def test_valid_task_id(self):
        from taktis.core.context import _validate_path_component
        assert _validate_path_component("abc12345", "task_id") == "abc12345"

    def test_task_id_with_path_separator(self):
        from taktis.core.context import _validate_path_component
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_path_component("../../../etc/passwd", "task_id")

    def test_task_id_with_backslash(self):
        from taktis.core.context import _validate_path_component
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_path_component("..\\..\\windows", "task_id")

    def test_task_id_with_null(self):
        from taktis.core.context import _validate_path_component
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_path_component("abc\x00def", "task_id")

    def test_empty_task_id(self):
        from taktis.core.context import _validate_path_component
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_path_component("", "task_id")

    def test_task_id_with_hyphen_underscore(self):
        from taktis.core.context import _validate_path_component
        assert _validate_path_component("abc-123_def", "task_id") == "abc-123_def"


# ===========================================================================
# Shared views (Fix #8)
# ===========================================================================

class TestSharedViews:
    """Verify the shared presentation layer."""

    def test_status_indicator(self):
        from taktis.core.views import status_indicator
        assert status_indicator("running") == "[*]"
        assert status_indicator("completed") == "[+]"
        assert status_indicator("failed") == "[!]"
        assert status_indicator("pending") == "[ ]"
        assert status_indicator("unknown") == "[?]"

    def test_format_duration(self):
        from taktis.core.views import format_duration
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        assert format_duration(now, now + timedelta(seconds=30)) == "30s"
        assert format_duration(now, now + timedelta(minutes=5, seconds=10)) == "5m 10s"
        assert format_duration(now, now + timedelta(hours=2, minutes=30)) == "2h 30m"
        assert format_duration(None, None) == "--"

    def test_format_cost(self):
        from taktis.core.views import format_cost
        assert format_cost(None) == "--"
        assert format_cost(0) == "--"
        assert format_cost(1.2345) == "$1.2345"

    def test_short_id(self):
        from taktis.core.views import short_id
        assert short_id("abcdefghij") == "abcdefgh"
        assert short_id("") == "?"

    def test_html_escape(self):
        from taktis.core.views import html_escape
        assert html_escape("<script>alert('xss')</script>") == "&lt;script&gt;alert('xss')&lt;/script&gt;"

    def test_extract_output_text_error(self):
        from taktis.core.views import extract_output_text
        assert extract_output_text({"error": "boom"}) == "ERROR: boom"

    def test_extract_output_text_delta(self):
        from taktis.core.views import extract_output_text
        assert extract_output_text({
            "type": "content_block_delta",
            "delta": {"text": "hello"}
        }) == "hello"

    def test_extract_output_text_skip_metadata(self):
        from taktis.core.views import extract_output_text
        assert extract_output_text({"type": "ping"}) == ""


# ===========================================================================
# Context file size limit (Fix #14)
# ===========================================================================

class TestContextFileSizeLimit:
    """Verify that large context files are truncated."""

    def test_safe_read_truncates_large_file(self, tmp_path):
        from taktis.core.context import get_phase_context, _MAX_CONTEXT_FILE_SIZE

        ctx_dir = tmp_path / ".taktis"
        ctx_dir.mkdir()

        # Create an oversized PROJECT.md
        large_content = "x" * (_MAX_CONTEXT_FILE_SIZE + 1000)
        (ctx_dir / "PROJECT.md").write_text(large_content, encoding="utf-8")

        result, _ = get_phase_context(str(tmp_path), phase_number=1)
        assert "truncated" in result


# ===========================================================================
# Rate limiting middleware (Fix #4)
# ===========================================================================

class TestRateLimitMiddleware:
    """Verify rate limiting logic."""

    def test_prune_old_timestamps(self):
        import time
        from taktis.web.app import RateLimitMiddleware
        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        now = time.monotonic()
        timestamps = [now - 120, now - 61, now - 30, now - 10, now]
        result = mw._prune_old(timestamps, now)
        assert len(result) == 3  # only last 60 seconds
