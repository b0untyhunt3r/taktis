"""Tests for StreamingError handling in SDKProcess async iteration loops.

Covers ERR-09: failures inside the ``sdk_query`` and ``receive_response``
async-for loops must:
  - put a ``{"type": "error", ...}`` event on the message queue,
  - log with ``logger.exception`` so the traceback is captured,
  - wrap the raw exception in :class:`~taktis.exceptions.StreamingError`
    (but never double-wrap an already-typed StreamingError), and
  - ensure the ``finally`` cleanup still runs (``_is_running=False``,
    ``finished_at`` set, ``_eof`` sentinel on queue).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.sdk_process import SDKProcess
from taktis.exceptions import StreamingError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_process(task_id: str = "t-test", interactive: bool = False) -> SDKProcess:
    """Create an SDKProcess without connecting to the real SDK."""
    return SDKProcess(
        task_id=task_id,
        prompt="do something",
        working_dir="/tmp",
        interactive=interactive,
    )


async def _drain_queue(q: "asyncio.Queue[dict[str, Any]]") -> list[dict[str, Any]]:
    """Return every item currently in *q* without blocking."""
    items: list[dict[str, Any]] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# --- async-generator factories used as drop-in replacements for sdk_query /
#     receive_response.  Each accepts *args/**kwargs so they match the real
#     signatures when assigned via patch(new=...).

async def _raising_agen(*args: Any, **kwargs: Any):
    """Raises RuntimeError on the first iteration — simulates a broken stream."""
    raise RuntimeError("stream network error")
    yield  # pragma: no cover — required to make this an async generator function


async def _raising_mid_agen(*args: Any, **kwargs: Any):
    """Yields one item then raises — simulates a mid-stream failure."""
    yield MagicMock()
    raise RuntimeError("mid-stream failure")


async def _empty_agen(*args: Any, **kwargs: Any):
    """Yields nothing — simulates a clean, empty stream."""
    return
    yield  # pragma: no cover


# ---------------------------------------------------------------------------
# _run_oneshot  (async for msg in sdk_query(...))
# ---------------------------------------------------------------------------


class TestRunOneshotStreamingError:
    """SDKProcess._run_oneshot: inner try/except around the sdk_query loop."""

    # ---- error path -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_error_event_on_queue(self) -> None:
        """A streaming failure must deliver an error event before the _eof sentinel."""
        proc = _make_process("t-oneshot-err")

        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        types = [e["type"] for e in events]
        assert "error" in types, f"Expected error event; got types: {types}"

    @pytest.mark.asyncio
    async def test_error_content_mentions_task_id(self) -> None:
        """The error event content must include the task_id for traceability."""
        proc = _make_process("t-oneshot-id")

        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events, "Expected at least one error event"
        assert "t-oneshot-id" in error_events[0]["content"], (
            f"task_id missing from error content: {error_events[0]['content']!r}"
        )

    @pytest.mark.asyncio
    async def test_logs_at_error_level_with_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """logger.exception must be called so the full traceback is captured."""
        proc = _make_process("t-oneshot-log")

        with caplog.at_level(logging.ERROR, logger="taktis.core.sdk_process"), \
             patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        assert any("t-oneshot-log" in r.message for r in error_records), (
            f"task_id not found in error log: {[r.message for r in error_records]}"
        )
        assert any(r.exc_info is not None for r in error_records), (
            "Expected exc_info on at least one ERROR record"
        )

    @pytest.mark.asyncio
    async def test_exit_code_is_1_on_streaming_error(self) -> None:
        """_exit_code must be 1 when the streaming loop fails."""
        proc = _make_process("t-oneshot-exitcode")

        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        assert proc._exit_code == 1

    @pytest.mark.asyncio
    async def test_finally_runs_on_streaming_error(self) -> None:
        """The outer finally must always execute: _is_running=False, finished_at set, _eof on queue."""
        proc = _make_process("t-oneshot-finally")
        proc._is_running = True

        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        assert proc._is_running is False, "finally must set _is_running = False"
        assert proc.finished_at is not None, "finally must set finished_at"

        events = await _drain_queue(proc._message_queue)
        assert events, "Queue must not be empty after run"
        assert events[-1]["type"] == "_eof", (
            f"Last event must be _eof sentinel; got: {events[-1]['type']!r}"
        )

    @pytest.mark.asyncio
    async def test_streaming_error_not_double_wrapped(self) -> None:
        """A StreamingError from the SDK must be used as-is, not re-wrapped."""
        original_exc = StreamingError("already typed by SDK")

        async def _pre_typed(*args: Any, **kwargs: Any):
            raise original_exc
            yield  # pragma: no cover

        proc = _make_process("t-oneshot-passthrough")

        with patch("taktis.core.sdk_process.sdk_query", new=_pre_typed), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events, "Expected error event"
        assert "already typed by SDK" in error_events[0]["content"], (
            f"Original message not preserved: {error_events[0]['content']!r}"
        )

    @pytest.mark.asyncio
    async def test_mid_stream_error_still_delivers_eof(self) -> None:
        """A mid-iteration failure must still close the stream with _eof."""
        proc = _make_process("t-oneshot-midstream")

        with patch("taktis.core.sdk_process.sdk_query", new=_raising_mid_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        assert events, "Queue must have events"
        assert events[-1]["type"] == "_eof"

    # ---- happy path -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_happy_path_no_error_event(self) -> None:
        """A clean run must produce no error event and set exit_code=0."""
        proc = _make_process("t-oneshot-ok")

        with patch("taktis.core.sdk_process.sdk_query", new=_empty_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        assert not any(e["type"] == "error" for e in events), (
            f"Unexpected error events: {[e for e in events if e['type'] == 'error']}"
        )
        assert proc._exit_code == 0


# ---------------------------------------------------------------------------
# _run_continuation  (async for msg in sdk_query(...))
# ---------------------------------------------------------------------------


class TestRunContinuationStreamingError:
    """SDKProcess._run_continuation: inner try/except around the sdk_query loop."""

    @pytest.mark.asyncio
    async def test_error_event_and_eof_on_queue(self) -> None:
        """A streaming failure must put error then _eof on the queue."""
        proc = _make_process("t-cont-err")

        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_continuation("follow-up", "session-abc")

        events = await _drain_queue(proc._message_queue)
        types = [e["type"] for e in events]
        assert "error" in types, f"Expected error event; got: {types}"
        assert types[-1] == "_eof", f"Expected _eof last; got: {types[-1]!r}"

    @pytest.mark.asyncio
    async def test_logs_at_error_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        proc = _make_process("t-cont-log")

        with caplog.at_level(logging.ERROR, logger="taktis.core.sdk_process"), \
             patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_continuation("msg", "sess-xyz")

        assert any(r.levelno == logging.ERROR for r in caplog.records), (
            "Expected at least one ERROR log record"
        )

    @pytest.mark.asyncio
    async def test_finally_runs_on_streaming_error(self) -> None:
        """The outer finally must always execute even when return is used."""
        proc = _make_process("t-cont-finally")
        proc._is_running = True

        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_continuation("msg", "sess-xyz")

        assert proc._is_running is False
        assert proc.finished_at is not None

    @pytest.mark.asyncio
    async def test_happy_path_no_error_event(self) -> None:
        proc = _make_process("t-cont-ok")

        with patch("taktis.core.sdk_process.sdk_query", new=_empty_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_continuation("msg", "sess-ok")

        events = await _drain_queue(proc._message_queue)
        assert not any(e["type"] == "error" for e in events)
        assert proc._exit_code == 0


# ---------------------------------------------------------------------------
# _run_interactive  (async for message in self.client.receive_response())
# ---------------------------------------------------------------------------


class TestRunInteractiveStreamingError:
    """SDKProcess._run_interactive: inner try/except around receive_response loop."""

    @staticmethod
    def _make_client_mock(receive_fn: Any) -> MagicMock:
        """Return a ClaudeSDKClient mock whose receive_response calls *receive_fn*."""
        client = MagicMock()
        client.connect = AsyncMock()
        client.receive_response = receive_fn
        return client

    @pytest.mark.asyncio
    async def test_error_event_and_eof_on_queue(self) -> None:
        proc = _make_process("t-inter-err", interactive=True)
        mock_client = self._make_client_mock(_raising_agen)

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        events = await _drain_queue(proc._message_queue)
        types = [e["type"] for e in events]
        assert "error" in types, f"Expected error event; got: {types}"
        assert types[-1] == "_eof"

    @pytest.mark.asyncio
    async def test_error_content_mentions_task_id(self) -> None:
        proc = _make_process("t-inter-id", interactive=True)
        mock_client = self._make_client_mock(_raising_agen)

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events
        assert "t-inter-id" in error_events[0]["content"], (
            f"task_id missing from error content: {error_events[0]['content']!r}"
        )

    @pytest.mark.asyncio
    async def test_logs_at_error_level_with_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        proc = _make_process("t-inter-log", interactive=True)
        mock_client = self._make_client_mock(_raising_agen)

        with caplog.at_level(logging.ERROR, logger="taktis.core.sdk_process"), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        assert any(r.exc_info is not None for r in error_records), (
            "Expected exc_info on ERROR record"
        )

    @pytest.mark.asyncio
    async def test_exit_code_is_1_on_streaming_error(self) -> None:
        proc = _make_process("t-inter-exitcode", interactive=True)
        mock_client = self._make_client_mock(_raising_agen)

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        assert proc._exit_code == 1

    @pytest.mark.asyncio
    async def test_finally_runs_on_streaming_error(self) -> None:
        proc = _make_process("t-inter-finally", interactive=True)
        proc._is_running = True
        mock_client = self._make_client_mock(_raising_agen)

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        assert proc._is_running is False
        assert proc.finished_at is not None
        events = await _drain_queue(proc._message_queue)
        assert events[-1]["type"] == "_eof"

    @pytest.mark.asyncio
    async def test_streaming_error_not_double_wrapped(self) -> None:
        original_exc = StreamingError("SDK raised this directly")

        async def _pre_typed(*args: Any, **kwargs: Any):
            raise original_exc
            yield  # pragma: no cover

        proc = _make_process("t-inter-passthrough", interactive=True)
        mock_client = self._make_client_mock(_pre_typed)

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events
        assert "SDK raised this directly" in error_events[0]["content"]

    @pytest.mark.asyncio
    async def test_happy_path_no_error_event(self) -> None:
        proc = _make_process("t-inter-ok", interactive=True)
        mock_client = self._make_client_mock(_empty_agen)

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        events = await _drain_queue(proc._message_queue)
        assert not any(e["type"] == "error" for e in events)
        assert proc._exit_code == 0


# ---------------------------------------------------------------------------
# _run_followup  (async for message in self.client.receive_response())
# ---------------------------------------------------------------------------


class TestRunFollowupStreamingError:
    """SDKProcess._run_followup: inner try/except around receive_response loop."""

    @staticmethod
    def _make_connected_process(receive_fn: Any, task_id: str = "t-followup") -> SDKProcess:
        """Return a process with a pre-attached mock client."""
        proc = _make_process(task_id, interactive=True)
        client = MagicMock()
        client.query = AsyncMock()
        client.receive_response = receive_fn
        proc.client = client
        return proc

    @pytest.mark.asyncio
    async def test_error_event_on_queue(self) -> None:
        """A streaming failure must deliver an error event."""
        proc = self._make_connected_process(_raising_agen)
        await proc._run_followup("next prompt")

        events = await _drain_queue(proc._message_queue)
        types = [e["type"] for e in events]
        assert "error" in types, f"Expected error event; got: {types}"

    @pytest.mark.asyncio
    async def test_error_content_mentions_task_id(self) -> None:
        proc = self._make_connected_process(_raising_agen, task_id="t-followup-id")
        await proc._run_followup("next prompt")

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events
        assert "t-followup-id" in error_events[0]["content"], (
            f"task_id missing from error content: {error_events[0]['content']!r}"
        )

    @pytest.mark.asyncio
    async def test_logs_at_error_level_with_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        proc = self._make_connected_process(_raising_agen)

        with caplog.at_level(logging.ERROR, logger="taktis.core.sdk_process"):
            await proc._run_followup("next prompt")

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        assert any(r.exc_info is not None for r in error_records), (
            "Expected exc_info on ERROR record"
        )

    @pytest.mark.asyncio
    async def test_streaming_error_not_double_wrapped(self) -> None:
        """A StreamingError from the SDK must not be re-wrapped."""
        original_exc = StreamingError("sdk already typed this")

        async def _pre_typed(*args: Any, **kwargs: Any):
            raise original_exc
            yield  # pragma: no cover

        proc = self._make_connected_process(_pre_typed)
        await proc._run_followup("next")

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events
        assert "sdk already typed this" in error_events[0]["content"]

    @pytest.mark.asyncio
    async def test_happy_path_no_error_event(self) -> None:
        proc = self._make_connected_process(_empty_agen)
        await proc._run_followup("next prompt")

        events = await _drain_queue(proc._message_queue)
        assert not any(e["type"] == "error" for e in events), (
            f"Unexpected error events: {[e for e in events if e['type'] == 'error']}"
        )

    @pytest.mark.asyncio
    async def test_finally_runs_on_streaming_error(self) -> None:
        """Finally block must set _is_running=False, finished_at, and _eof."""
        proc = self._make_connected_process(_raising_agen)
        proc._is_running = True
        await proc._run_followup("next prompt")

        assert proc._is_running is False
        assert proc.finished_at is not None
        events = await _drain_queue(proc._message_queue)
        assert events[-1]["type"] == "_eof"

    @pytest.mark.asyncio
    async def test_exit_code_set_on_streaming_error(self) -> None:
        """Streaming error must set _exit_code=1."""
        proc = self._make_connected_process(_raising_agen)
        await proc._run_followup("next prompt")
        assert proc._exit_code == 1

    @pytest.mark.asyncio
    async def test_exit_code_zero_on_success(self) -> None:
        """Successful followup must set _exit_code=0."""
        proc = self._make_connected_process(_empty_agen)
        await proc._run_followup("next prompt")
        assert proc._exit_code == 0


# ---------------------------------------------------------------------------
# Queue overflow protection (_safe_enqueue / _enqueue_eof)
# ---------------------------------------------------------------------------


def _make_tiny_queue_process(
    task_id: str = "t-qof",
    maxsize: int = 2,
) -> SDKProcess:
    """Create an SDKProcess with a tiny queue and fast timeouts for overflow testing."""
    proc = SDKProcess(
        task_id=task_id,
        prompt="do something",
        working_dir="/tmp",
    )
    proc._message_queue = asyncio.Queue(maxsize=maxsize)
    # Use short timeouts so tests don't wait 5s/30s
    proc.ENQUEUE_TIMEOUT = 0.2
    proc.EOF_TIMEOUT = 0.2
    return proc


class TestSafeEnqueue:
    """Queue overflow protection tests.

    QOF-01: full queue -> event dropped, no exception raised
    QOF-02: full queue -> error logged with task_id and event type
    QOF-03: non-full queue -> event delivered normally
    QOF-04: _eof uses _enqueue_eof (always delivered even when queue full)
    QOF-05: _enqueue_eof drains queue if stuck
    QOF-06: _safe_enqueue succeeds when space appears within timeout
    """

    @pytest.mark.asyncio
    async def test_qof_01_full_queue_drops_event_no_exception(self) -> None:
        """QOF-01: When queue is full, _safe_enqueue drops the event without raising."""
        proc = _make_tiny_queue_process("t-qof-01", maxsize=2)
        # Fill to capacity
        proc._message_queue.put_nowait({"type": "filler1"})
        proc._message_queue.put_nowait({"type": "filler2"})
        assert proc._message_queue.full()

        # Must not raise -- just drop the event
        await proc._safe_enqueue({"type": "should_be_dropped"})

        # Queue still has exactly the original 2 items
        items = await _drain_queue(proc._message_queue)
        assert len(items) == 2
        assert all(e["type"].startswith("filler") for e in items)

    @pytest.mark.asyncio
    async def test_qof_02_full_queue_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """QOF-02: When queue is full, _safe_enqueue logs an ERROR with task_id and type."""
        proc = _make_tiny_queue_process("t-qof-02", maxsize=1)
        proc._message_queue.put_nowait({"type": "filler"})

        with caplog.at_level(logging.ERROR, logger="taktis.core.sdk_process"):
            await proc._safe_enqueue({"type": "dropped_event"})

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected an ERROR log record"
        msg = error_records[0].message
        assert "t-qof-02" in msg, f"task_id not in log: {msg}"
        assert "dropped_event" in msg, f"event type not in log: {msg}"

    @pytest.mark.asyncio
    async def test_qof_03_normal_enqueue(self) -> None:
        """QOF-03: When queue has space, event is delivered normally."""
        proc = _make_tiny_queue_process("t-qof-03", maxsize=5)

        await proc._safe_enqueue({"type": "normal_event", "data": "hello"})

        items = await _drain_queue(proc._message_queue)
        assert len(items) == 1
        assert items[0]["type"] == "normal_event"
        assert items[0]["data"] == "hello"

    @pytest.mark.asyncio
    async def test_qof_04_enqueue_eof_delivered_when_full(self) -> None:
        """QOF-04: _enqueue_eof delivers _eof even when queue is full (by draining)."""
        proc = _make_tiny_queue_process("t-qof-04", maxsize=2)
        proc._message_queue.put_nowait({"type": "filler1"})
        proc._message_queue.put_nowait({"type": "filler2"})
        assert proc._message_queue.full()

        await proc._enqueue_eof()

        # The queue was drained and _eof was inserted
        items = await _drain_queue(proc._message_queue)
        eof_items = [e for e in items if e.get("type") == "_eof"]
        assert eof_items, "Expected _eof to be present after _enqueue_eof"

    @pytest.mark.asyncio
    async def test_qof_05_enqueue_eof_drains_then_inserts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """QOF-05: _enqueue_eof force-drains the queue when stuck, then inserts _eof."""
        proc = _make_tiny_queue_process("t-qof-05", maxsize=2)
        proc._message_queue.put_nowait({"type": "filler1"})
        proc._message_queue.put_nowait({"type": "filler2"})

        with caplog.at_level(logging.CRITICAL, logger="taktis.core.sdk_process"):
            await proc._enqueue_eof()

        # _eof must be present
        items = await _drain_queue(proc._message_queue)
        assert any(e.get("type") == "_eof" for e in items), (
            f"_eof not found in queue items: {items}"
        )

        # CRITICAL log about draining must have been emitted
        critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert critical_records, "Expected a CRITICAL log about force-draining"
        assert "t-qof-05" in critical_records[0].message

    @pytest.mark.asyncio
    async def test_qof_06_safe_enqueue_succeeds_when_space_appears(self) -> None:
        """QOF-06: _safe_enqueue succeeds if a consumer frees space within the timeout."""
        proc = _make_tiny_queue_process("t-qof-06", maxsize=2)
        # Use a longer timeout so the consumer has time to free a slot
        proc.ENQUEUE_TIMEOUT = 5.0
        proc._message_queue.put_nowait({"type": "filler1"})
        proc._message_queue.put_nowait({"type": "filler2"})
        assert proc._message_queue.full()

        async def _consumer():
            """Free one slot after a short delay."""
            await asyncio.sleep(0.1)
            proc._message_queue.get_nowait()

        consumer_task = asyncio.create_task(_consumer())

        # This should succeed within the 5s timeout because consumer frees a slot
        await proc._safe_enqueue({"type": "delayed_event"})

        await consumer_task

        items = await _drain_queue(proc._message_queue)
        types = [e["type"] for e in items]
        assert "delayed_event" in types, (
            f"Expected delayed_event in queue; got: {types}"
        )


# ---------------------------------------------------------------------------
# API error detection — ResultMessage with is_error or "API Error: ..." text
# must flip the task to exit_code=1 instead of being silently accepted.
# ---------------------------------------------------------------------------


class _FakeResult:
    """Minimal stand-in for the SDK's ResultMessage dataclass.

    We can't use MagicMock here because ``_message_to_event`` distinguishes
    a ResultMessage from a StreamEvent via ``hasattr(message, "result") and
    hasattr(message, "total_cost_usd")``. MagicMock answers yes to every
    hasattr, so a second branch would fire for StreamEvent.
    """

    def __init__(self, result: str, is_error: bool = False, subtype: str = "success") -> None:
        self.result = result
        self.total_cost_usd = 0.0
        self.session_id = "sess-fake"
        self.duration_ms = 1
        self.is_error = is_error
        self.subtype = subtype
        self.usage = {}
        self.num_turns = 1


def _api_error_agen_factory(result_text: str, is_error: bool = True):
    """Return an async-generator that yields a single error-ish ResultMessage."""
    async def _gen(*args: Any, **kwargs: Any):
        yield _FakeResult(result_text, is_error=is_error)
    return _gen


class TestApiErrorResultDetection:
    """A ResultMessage whose payload is a transport-layer API error must mark
    the task as failed (exit_code=1) and put an error event on the queue,
    instead of silently being stored as the task's final output."""

    @pytest.mark.asyncio
    async def test_api_error_500_text_sets_exit_code_1(self) -> None:
        """Result text starting with 'API Error: 500' → exit_code=1."""
        proc = _make_process("t-api-err-500")
        agen = _api_error_agen_factory(
            'API Error: 500 {"type":"error","error":{"type":"api_error"}}',
            is_error=False,
        )
        with patch("taktis.core.sdk_process.sdk_query", new=agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        assert proc._exit_code == 1

    @pytest.mark.asyncio
    async def test_api_error_result_enqueues_error_event(self) -> None:
        """On API error result, an 'error' event must be on the queue."""
        proc = _make_process("t-api-err-evt")
        agen = _api_error_agen_factory("API Error: 529 overloaded", is_error=True)
        with patch("taktis.core.sdk_process.sdk_query", new=agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        assert any(e["type"] == "error" for e in events), (
            f"Expected error event; got: {[e['type'] for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_is_error_flag_alone_triggers_failure(self) -> None:
        """is_error=True without API-Error prefix still marks task failed."""
        proc = _make_process("t-api-err-flag")
        agen = _api_error_agen_factory("some weird payload", is_error=True)
        with patch("taktis.core.sdk_process.sdk_query", new=agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        assert proc._exit_code == 1

    @pytest.mark.asyncio
    async def test_happy_result_stays_exit_code_0(self) -> None:
        """Regression guard: a normal ResultMessage must still succeed."""
        proc = _make_process("t-api-ok")
        agen = _api_error_agen_factory("Here is my answer.", is_error=False)
        with patch("taktis.core.sdk_process.sdk_query", new=agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        assert proc._exit_code == 0

    @pytest.mark.asyncio
    async def test_api_error_in_continuation_sets_exit_code_1(self) -> None:
        """Same check for _run_continuation path."""
        proc = _make_process("t-api-err-cont")
        agen = _api_error_agen_factory("API Error: 500 internal", is_error=True)
        with patch("taktis.core.sdk_process.sdk_query", new=agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_continuation("follow-up", "sess-1")

        assert proc._exit_code == 1

    @pytest.mark.asyncio
    async def test_context_overflow_error_classified(self) -> None:
        """'Prompt is too long' → error_type='context_overflow'."""
        proc = _make_process("t-ctx-overflow")
        agen = _api_error_agen_factory("Prompt is too long", is_error=True)
        with patch("taktis.core.sdk_process.sdk_query", new=agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        assert proc._exit_code == 1
        events = await _drain_queue(proc._message_queue)
        err_events = [e for e in events if e["type"] == "error"]
        assert err_events and err_events[0].get("error_type") == "context_overflow", (
            f"Expected error_type='context_overflow'; got: {err_events}"
        )

    @pytest.mark.asyncio
    async def test_usage_limit_error_classified_and_fails_task(self) -> None:
        """Usage-limit text → failed task + error_type='usage_limit' on event."""
        proc = _make_process("t-usage-limit")
        agen = _api_error_agen_factory(
            "You've hit your limit · resets 11pm (Europe/Bucharest)",
            is_error=True,
        )
        with patch("taktis.core.sdk_process.sdk_query", new=agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        assert proc._exit_code == 1
        events = await _drain_queue(proc._message_queue)
        err_events = [e for e in events if e["type"] == "error"]
        assert err_events and err_events[0].get("error_type") == "usage_limit", (
            f"Expected error_type='usage_limit'; got: {err_events}"
        )
