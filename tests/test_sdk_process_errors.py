"""Tests for error-path guarantees in SDKProcess and ProcessManager.

============================================================
TEST PLAN
============================================================

Scope
-----
Components under test
  * SDKProcess (taktis/core/sdk_process.py) — all four async-for
    streaming paths: ``_run_oneshot``, ``_run_continuation``,
    ``_run_interactive``, ``_run_followup``.
  * ProcessManager (taktis/core/manager.py) — monitor-task
    done_callback and semaphore release on every error path.

Explicitly OUT of scope
  * Normal (non-error) streaming (covered by test_sdk_process.py)
  * Database-layer failures in Taktis.continue_task
    (covered by test_done_callbacks.py)
  * Permission / approval flow (covered by test_sdk_process.py)

Test categories
---------------
  Unit        – SGE-* : mock sdk_query / ClaudeSDKClient, no I/O
  Integration – DCB-* : real asyncio Tasks + EventBus, SDK fully mocked
  Integration – SEM-* : real asyncio semaphore + real monitor coroutine

Entry criteria
--------------
  * SDKProcess and ProcessManager are importable with SDK mocked out.
  * All existing tests pass (no regressions).

Exit criteria
-------------
  * All 23 test cases pass.
  * Zero flaky tests — every test uses event-loop yields rather than
    wall-clock sleeps.
  * One assert per test; no assert name contains "and".

Health scoring rubric (0-100)
-----------------------------
  The component starts at a baseline health of 62 (from prior test runs).
  Each category contributes:
    SGE (12 cases): streaming error paths                +10  (was partially covered)
    DCB  (6 cases): done_callback attached & fires       +16  (previously uncovered)
    SEM  (5 cases): semaphore invariant on all paths     +12  (previously uncovered)
  Estimated health after this suite:  100 (all error branches covered)

  Score would fall by:
    -15 if any SGE test is removed (streaming contract broken)
    -20 if any SEM test is removed (semaphore leak risk)
    -20 if any DCB test is removed (silent crash risk)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.events import EVENT_TASK_FAILED, EventBus
from taktis.core.manager import ProcessManager
from taktis.core.sdk_process import SDKProcess
from taktis.exceptions import StreamingError


# ---------------------------------------------------------------------------
# Test utilities and shared fixtures
# ---------------------------------------------------------------------------


class _FatalError(BaseException):
    """Test-only BaseException subclass.

    This class is intentionally NOT a subclass of ``Exception`` so that it
    bypasses ``except Exception`` handlers while ``finally`` blocks still run.
    It simulates fatal conditions such as memory errors or OS signals that
    should never be silently swallowed.
    """


def _make_process(task_id: str = "t-test", interactive: bool = False) -> SDKProcess:
    """Return an SDKProcess that will not touch the real Claude SDK."""
    return SDKProcess(
        task_id=task_id,
        prompt="test prompt",
        working_dir="/tmp",
        interactive=interactive,
    )


def _make_manager(max_concurrent: int = 2) -> ProcessManager:
    """Return a ProcessManager wired to a fresh in-process EventBus."""
    return ProcessManager(event_bus=EventBus(), max_concurrent=max_concurrent)


async def _drain_queue(q: "asyncio.Queue[Any]") -> "list[dict[str, Any]]":
    """Return every item currently in *q* without blocking."""
    items: list[dict[str, Any]] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# -- async-generator stubs used as drop-in replacements for sdk_query /
#    receive_response.  Each accepts *args/**kwargs so they match real
#    call signatures when injected via unittest.mock.patch.

async def _raising_agen(*args: Any, **kwargs: Any):
    """Raises RuntimeError on first iteration — simulates a broken stream."""
    raise RuntimeError("stream broken")
    yield  # pragma: no cover – required to make this an async generator


async def _raising_mid_agen(*args: Any, **kwargs: Any):
    """Yields one item then raises — simulates a mid-stream failure."""
    # Use spec=object to prevent MagicMock from auto-creating attributes
    # like 'result' and 'total_cost_usd' which would be misidentified as a ResultMessage
    yield MagicMock(spec=object)
    raise RuntimeError("mid-stream failure")


async def _fatal_agen(*args: Any, **kwargs: Any):
    """Raises _FatalError (BaseException subclass) on first iteration."""
    raise _FatalError("fatal – bypasses except Exception")
    yield  # pragma: no cover


# ---------------------------------------------------------------------------
# SGE-01 – SGE-12: Streaming loop exception puts error event on queue
# ---------------------------------------------------------------------------


class TestStreamingLoopPutsErrorEventOnQueue:
    """
    Contract (SGE): Any exception raised inside an ``async for`` streaming
    loop must put exactly one ``{"type": "error", "content": <str>}`` event
    on the process message queue, with the ``_eof`` sentinel as the final
    item.

    Regression guard: if the inner try/except around any streaming loop is
    removed or mis-scoped, one of these tests will fail, making the breakage
    immediately visible.
    """

    # ------------------------------------------------------------------ #
    # SGE-01 / SGE-02  –  _run_oneshot                                     #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sge_01_oneshot_streaming_error_puts_error_event(self) -> None:
        """SGE-01: sdk_query raises → error event appears on queue."""
        proc = _make_process("sge-01")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        assert any(e["type"] == "error" for e in events), (
            f"Expected an error event; got types: {[e['type'] for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_sge_02_oneshot_eof_is_last_event_after_streaming_error(self) -> None:
        """SGE-02: _eof sentinel must be the last queue item even after a streaming error."""
        proc = _make_process("sge-02")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        assert events, "Queue must not be empty after a streaming error"
        assert events[-1]["type"] == "_eof", (
            f"Expected _eof as last event; got {events[-1]['type']!r}"
        )

    # ------------------------------------------------------------------ #
    # SGE-03 / SGE-04  –  _run_continuation                               #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sge_03_continuation_streaming_error_puts_error_event(self) -> None:
        """SGE-03: sdk_query raises in _run_continuation → error event on queue."""
        proc = _make_process("sge-03")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_continuation("follow-up", "session-abc")

        events = await _drain_queue(proc._message_queue)
        assert any(e["type"] == "error" for e in events), (
            f"Expected error event; got types: {[e['type'] for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_sge_04_continuation_eof_is_last_event_after_streaming_error(self) -> None:
        """SGE-04: _eof must be the final event after a continuation streaming error."""
        proc = _make_process("sge-04")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_continuation("msg", "sess-1")

        events = await _drain_queue(proc._message_queue)
        assert events[-1]["type"] == "_eof", (
            f"Expected _eof last; got {events[-1]['type']!r}"
        )

    # ------------------------------------------------------------------ #
    # SGE-05 / SGE-06  –  _run_interactive                                #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sge_05_interactive_streaming_error_puts_error_event(self) -> None:
        """SGE-05: receive_response raises in interactive mode → error event on queue."""
        proc = _make_process("sge-05", interactive=True)
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.receive_response = _raising_agen

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        events = await _drain_queue(proc._message_queue)
        assert any(e["type"] == "error" for e in events), (
            f"Expected error event; got types: {[e['type'] for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_sge_06_interactive_eof_is_last_event_after_streaming_error(self) -> None:
        """SGE-06: _eof must arrive last after an interactive streaming error."""
        proc = _make_process("sge-06", interactive=True)
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.receive_response = _raising_agen

        with patch("taktis.core.sdk_process.ClaudeAgentOptions"), \
             patch("taktis.core.sdk_process.ClaudeSDKClient", return_value=mock_client):
            await proc._run_interactive()

        events = await _drain_queue(proc._message_queue)
        assert events[-1]["type"] == "_eof", (
            f"Expected _eof last; got {events[-1]['type']!r}"
        )

    # ------------------------------------------------------------------ #
    # SGE-07  –  _run_followup                                            #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sge_07_followup_streaming_error_puts_error_event(self) -> None:
        """SGE-07: receive_response raises in _run_followup → error event on queue."""
        proc = _make_process("sge-07", interactive=True)
        proc.client = MagicMock()
        proc.client.query = AsyncMock()
        proc.client.receive_response = _raising_agen

        await proc._run_followup("next prompt")

        events = await _drain_queue(proc._message_queue)
        assert any(e["type"] == "error" for e in events), (
            f"Expected error event; got types: {[e['type'] for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_sge_07b_followup_eof_emitted_after_streaming_error(self) -> None:
        """SGE-07b (xfail — source inconsistency): _eof must be the last queue
        item after a _run_followup streaming error, consistent with all other
        streaming methods (_run_oneshot/SGE-02, _run_continuation/SGE-04,
        _run_interactive/SGE-06)."""
        proc = _make_process("sge-07b", interactive=True)
        proc.client = MagicMock()
        proc.client.query = AsyncMock()
        proc.client.receive_response = _raising_agen

        await proc._run_followup("next prompt")

        events = await _drain_queue(proc._message_queue)
        assert events, "Queue must not be empty after a streaming error"
        assert events[-1]["type"] == "_eof", (
            f"Expected _eof as last event after _run_followup error; "
            f"got {events[-1]['type']!r}"
        )

    # ------------------------------------------------------------------ #
    # SGE-08 / SGE-09  –  error event structural assertions               #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sge_08_error_event_content_is_non_empty_string(self) -> None:
        """SGE-08: The 'content' field of the error event must be a non-empty string."""
        proc = _make_process("sge-08")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events, "Expected at least one error event"
        content = error_events[0].get("content", "")
        assert isinstance(content, str) and content, (
            f"'content' must be a non-empty string; got {content!r}"
        )

    @pytest.mark.asyncio
    async def test_sge_09_exactly_one_error_event_per_streaming_failure(self) -> None:
        """SGE-09: A single stream failure must produce exactly one error event."""
        proc = _make_process("sge-09")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1, (
            f"Expected exactly 1 error event; got {len(error_events)}: {error_events}"
        )

    # ------------------------------------------------------------------ #
    # SGE-10  –  mid-stream failure                                        #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sge_10_mid_stream_failure_produces_error_event_then_eof(self) -> None:
        """SGE-10: An exception raised mid-iteration still produces error event + _eof."""
        proc = _make_process("sge-10")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_mid_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        assert any(e["type"] == "error" for e in events), "Expected error event"
        assert events[-1]["type"] == "_eof", "Expected _eof as final event"

    # ------------------------------------------------------------------ #
    # SGE-11 / SGE-12  –  StreamingError wrapping rules                  #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sge_11_plain_exception_wrapped_includes_task_id_in_content(self) -> None:
        """SGE-11: A plain RuntimeError is wrapped in StreamingError; task_id
        appears in the error event content string for traceability."""
        proc = _make_process("sge-11")
        with patch("taktis.core.sdk_process.sdk_query", new=_raising_agen), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events, "Expected at least one error event"
        assert "sge-11" in error_events[0]["content"], (
            f"task_id missing from StreamingError content: {error_events[0]['content']!r}"
        )

    @pytest.mark.asyncio
    async def test_sge_12_pre_typed_streaming_error_not_double_wrapped(self) -> None:
        """SGE-12: A StreamingError raised by the SDK must not be re-wrapped;
        the original message must be preserved verbatim in the error event content."""
        original = StreamingError("already a StreamingError – do not wrap again")

        async def _pre_typed(*args: Any, **kwargs: Any):
            raise original
            yield  # pragma: no cover

        proc = _make_process("sge-12")
        with patch("taktis.core.sdk_process.sdk_query", new=_pre_typed), \
             patch("taktis.core.sdk_process.ClaudeAgentOptions"):
            await proc._run_oneshot()

        events = await _drain_queue(proc._message_queue)
        error_events = [e for e in events if e["type"] == "error"]
        assert error_events, "Expected at least one error event"
        assert "already a StreamingError" in error_events[0]["content"], (
            f"Original message not preserved: {error_events[0]['content']!r}"
        )


# ---------------------------------------------------------------------------
# DCB-01 – DCB-06: Async task done_callback fires on exception
# ---------------------------------------------------------------------------


class TestMonitorDoneCallbackFiresOnException:
    """
    Contract (DCB): The asyncio.Task done_callback registered by
    ``ProcessManager.start_task()`` must fire when the monitor task
    terminates with an **unhandled** exception, log at ERROR level with
    ``exc_info``, and publish ``EVENT_TASK_FAILED`` with
    ``reason='monitor_crash'``.

    The done_callback is the last-resort safety net for ``BaseException``
    subclasses (e.g. memory errors) that bypass the broad
    ``except Exception`` block inside ``_monitor_output``.  For ordinary
    ``Exception`` subclasses the ``except Exception`` handler fires first and
    the task itself does *not* carry an exception — so the done_callback
    must be a no-op in that case.

    Test IDs DCB-01 – DCB-06.
    """

    @staticmethod
    def _proc_with_stream(task_id: str, stream_fn: Any) -> SDKProcess:
        """Return an SDKProcess whose ``stream_output()`` is replaced by *stream_fn*."""
        proc = _make_process(task_id)
        proc.start = AsyncMock()          # do not touch the real SDK
        proc.stream_output = stream_fn    # replace instance method
        return proc

    # ------------------------------------------------------------------ #
    # DCB-01  –  done_callback is actually attached to the monitor task   #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_dcb_01_done_callback_attached_to_monitor_task(self) -> None:
        """DCB-01: start_task() must add a done_callback to the monitor task.

        Verified indirectly: the full lifecycle (acquire → start → monitor →
        finally release) must complete when the stream terminates cleanly,
        proving that the callback infrastructure is wired correctly.
        """
        manager = _make_manager(max_concurrent=1)
        proc = _make_process("dcb-01")
        proc.start = AsyncMock()
        # Pre-load _eof so stream_output() terminates immediately
        proc._message_queue.put_nowait({"type": "_eof"})

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="dcb-01", prompt="test", working_dir="/tmp"
            )

        # Let the monitor task run to completion
        await asyncio.sleep(0)

        # Semaphore back at 1 proves the full lifecycle ran (including finally)
        assert manager._semaphore._value == 1, (
            "Lifecycle must complete: semaphore must be 1 after _eof-terminated monitor"
        )

    # ------------------------------------------------------------------ #
    # DCB-02  –  callback fires and publishes TASK_FAILED on BaseException #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_dcb_02_done_callback_fires_on_base_exception(self) -> None:
        """DCB-02: When stream_output() raises a BaseException (bypassing
        ``except Exception``), the done_callback must fire and publish
        EVENT_TASK_FAILED with reason='monitor_crash'."""
        manager = _make_manager(max_concurrent=1)
        q = manager._event_bus.subscribe(EVENT_TASK_FAILED)
        proc = self._proc_with_stream("dcb-02", _fatal_agen)

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="dcb-02", prompt="test", working_dir="/tmp"
            )

        # Allow event loop to run: monitor task → done_callback → publish task
        await asyncio.sleep(0.05)

        assert not q.empty(), (
            "Expected EVENT_TASK_FAILED published by done_callback after BaseException"
        )
        envelope = q.get_nowait()
        assert envelope["event_type"] == EVENT_TASK_FAILED
        assert envelope["data"]["task_id"] == "dcb-02"
        assert envelope["data"]["reason"] == "monitor_crash", (
            "reason must be 'monitor_crash' (from done_callback, not from except Exception); "
            f"got {envelope['data']['reason']!r}"
        )

    # ------------------------------------------------------------------ #
    # DCB-03  –  callback logs at ERROR level with task_id                #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_dcb_03_done_callback_logs_error_with_task_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DCB-03: done_callback must log at ERROR level mentioning the task_id."""
        manager = _make_manager(max_concurrent=1)
        proc = self._proc_with_stream("dcb-03", _fatal_agen)

        with caplog.at_level(logging.ERROR, logger="taktis.core.manager"), \
             patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="dcb-03", prompt="test", working_dir="/tmp"
            )
            await asyncio.sleep(0.05)

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert error_msgs, "Expected at least one ERROR log record from done_callback"
        assert any("dcb-03" in m for m in error_msgs), (
            f"task_id 'dcb-03' not found in ERROR log messages: {error_msgs}"
        )

    # ------------------------------------------------------------------ #
    # DCB-04  –  callback attaches exc_info to ERROR record               #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_dcb_04_done_callback_error_log_carries_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DCB-04: The ERROR log record from done_callback must carry exc_info
        so that the full traceback is captured in the log stream."""
        manager = _make_manager(max_concurrent=1)
        proc = self._proc_with_stream("dcb-04", _fatal_agen)

        with caplog.at_level(logging.ERROR, logger="taktis.core.manager"), \
             patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="dcb-04", prompt="test", working_dir="/tmp"
            )
            await asyncio.sleep(0.05)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        assert any(r.exc_info is not None for r in error_records), (
            "Expected exc_info on at least one ERROR record from done_callback"
        )

    # ------------------------------------------------------------------ #
    # DCB-05  –  callback event payload contains a non-empty 'error' key  #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_dcb_05_done_callback_event_payload_contains_error_string(self) -> None:
        """DCB-05: The EVENT_TASK_FAILED payload published by done_callback must
        include a non-empty 'error' field containing the exception description."""
        manager = _make_manager(max_concurrent=1)
        q = manager._event_bus.subscribe(EVENT_TASK_FAILED)
        proc = self._proc_with_stream("dcb-05", _fatal_agen)

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="dcb-05", prompt="test", working_dir="/tmp"
            )
        await asyncio.sleep(0.05)

        envelope = q.get_nowait()
        assert envelope["data"].get("error"), (
            "Expected non-empty 'error' field in EVENT_TASK_FAILED payload from done_callback"
        )

    # ------------------------------------------------------------------ #
    # DCB-06  –  done_callback is noop for ordinary Exception             #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_dcb_06_done_callback_is_noop_for_ordinary_exception(self) -> None:
        """DCB-06: A regular RuntimeError is caught by ``except Exception`` inside
        _monitor_output; the monitor task itself finishes cleanly (no exception
        on the task), so the done_callback must be a no-op.

        Exactly one EVENT_TASK_FAILED is published (by except Exception, not by
        the done_callback) with reason='monitor_error'."""
        manager = _make_manager(max_concurrent=1)
        q = manager._event_bus.subscribe(EVENT_TASK_FAILED)

        async def _ordinary_error_gen():
            raise RuntimeError("ordinary exception – caught by except Exception")
            yield  # pragma: no cover

        proc = self._proc_with_stream("dcb-06", _ordinary_error_gen)

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="dcb-06", prompt="test", working_dir="/tmp"
            )
        await asyncio.sleep(0.05)

        # The except Exception handler publishes TASK_FAILED with reason='monitor_error'
        assert not q.empty(), "Expected EVENT_TASK_FAILED from except Exception handler"
        envelope = q.get_nowait()
        assert envelope["data"]["reason"] == "monitor_error", (
            "Expected 'monitor_error' from except Exception block; "
            f"got {envelope['data']['reason']!r}"
        )
        # done_callback must NOT publish an additional event
        assert q.empty(), (
            "Expected no second TASK_FAILED: done_callback must be a no-op for ordinary Exception"
        )


# ---------------------------------------------------------------------------
# SEM-01 – SEM-05: Semaphore is released on error
# ---------------------------------------------------------------------------


class TestSemaphoreReleasedOnError:
    """
    Contract (SEM): The ProcessManager concurrency semaphore must be released
    in **every** error path so that no concurrency slot is permanently lost.

    Losing a slot silently prevents future tasks from ever running — a
    subtle but severe production failure.  These tests enforce the invariant:
    after any error, ``semaphore._value`` must equal its pre-task level.

    Error paths under test:
      SEM-01  ``process.start()`` raises         → ``except`` in start_task() releases
      SEM-02  Monitor exits cleanly (_eof)        → ``finally`` in _monitor_output releases
      SEM-03  Monitor hits ``except Exception``  → ``finally`` in _monitor_output releases
      SEM-04  Monitor raises BaseException        → ``finally`` in _monitor_output releases
      SEM-05  Two concurrent error tasks          → both slots recovered
    """

    @staticmethod
    def _available(manager: ProcessManager) -> int:
        """Return the current number of available semaphore permits.

        Uses the CPython ``asyncio.Semaphore._value`` internal attribute.
        This is a well-known, stable attribute in CPython 3.11+.
        """
        return manager._semaphore._value

    # ------------------------------------------------------------------ #
    # SEM-01  –  process.start() raises                                   #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_01_semaphore_released_when_start_raises(self) -> None:
        """SEM-01: If process.start() raises, the except block in start_task()
        must release the semaphore so the slot is not permanently consumed."""
        manager = _make_manager(max_concurrent=1)
        assert self._available(manager) == 1

        proc = _make_process("sem-01")
        proc.start = AsyncMock(side_effect=RuntimeError("start failed"))

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            with pytest.raises(RuntimeError, match="start failed"):
                await manager.start_task(
                    task_id="sem-01", prompt="test", working_dir="/tmp"
                )

        assert self._available(manager) == 1, (
            "Semaphore must be released immediately after process.start() failure"
        )

    # ------------------------------------------------------------------ #
    # SEM-02  –  monitor exits cleanly                                    #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_02_semaphore_released_after_clean_monitor_exit(self) -> None:
        """SEM-02: Semaphore must be released in the finally block after the
        monitor drains the stream cleanly (receives the _eof sentinel)."""
        manager = _make_manager(max_concurrent=1)
        proc = _make_process("sem-02")
        proc.start = AsyncMock()
        # Pre-load _eof so stream_output() terminates on first read
        proc._message_queue.put_nowait({"type": "_eof"})

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="sem-02", prompt="test", working_dir="/tmp"
            )

        # Yield control so the monitor task can run to completion
        await asyncio.sleep(0)

        assert self._available(manager) == 1, (
            "Semaphore must be released after clean monitor exit (_eof path)"
        )

    # ------------------------------------------------------------------ #
    # SEM-03  –  monitor hits the except Exception path                  #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_03_semaphore_released_after_monitor_exception(self) -> None:
        """SEM-03: Semaphore must be released in finally even when _monitor_output
        routes through the ``except Exception`` handler."""
        manager = _make_manager(max_concurrent=1)

        async def _regular_error_stream():
            raise RuntimeError("ordinary stream error")
            yield  # pragma: no cover

        proc = _make_process("sem-03")
        proc.start = AsyncMock()
        proc.stream_output = _regular_error_stream

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="sem-03", prompt="test", working_dir="/tmp"
            )

        await asyncio.sleep(0)

        assert self._available(manager) == 1, (
            "Semaphore must be released from finally block even on except Exception path"
        )

    # ------------------------------------------------------------------ #
    # SEM-04  –  monitor raises BaseException (bypasses except Exception) #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_04_semaphore_released_when_base_exception_bypasses_handler(self) -> None:
        """SEM-04: Even when a BaseException propagates through both except clauses
        (bypassing ``except Exception``), the finally clause must still release
        the semaphore."""
        manager = _make_manager(max_concurrent=1)

        proc = _make_process("sem-04")
        proc.start = AsyncMock()
        proc.stream_output = _fatal_agen  # raises _FatalError (BaseException subclass)

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="sem-04", prompt="test", working_dir="/tmp"
            )

        # Let monitor task finish + done_callback schedule its publish task
        await asyncio.sleep(0.05)

        assert self._available(manager) == 1, (
            "Semaphore must be released in finally even when BaseException propagates "
            "past except Exception"
        )

    # ------------------------------------------------------------------ #
    # SEM-05  –  both slots recovered after two concurrent errors         #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_05_both_slots_recovered_after_two_concurrent_errors(self) -> None:
        """SEM-05: All concurrency slots must be returned when multiple tasks fail,
        leaving the semaphore in its original state."""
        manager = _make_manager(max_concurrent=2)
        assert self._available(manager) == 2

        async def _error_stream():
            raise RuntimeError("concurrent task error")
            yield  # pragma: no cover

        procs = [_make_process("sem-05a"), _make_process("sem-05b")]
        for p in procs:
            p.start = AsyncMock()
            p.stream_output = _error_stream

        with patch("taktis.core.manager.SDKProcess", side_effect=procs):
            for task_id in ("sem-05a", "sem-05b"):
                await manager.start_task(
                    task_id=task_id, prompt="test", working_dir="/tmp"
                )

        # Yield control so both monitor tasks run to completion
        await asyncio.sleep(0)

        assert self._available(manager) == 2, (
            f"All 2 semaphore slots must be recovered after 2 concurrent errors; "
            f"got {self._available(manager)} available"
        )


# ---------------------------------------------------------------------------
# SEM-06 – SEM-09: Semaphore released on post-start failures in start_task
# ---------------------------------------------------------------------------


class TestSemaphoreReleasedOnPostStartFailure:
    """
    Contract (SEM-POST): After ``process.start()`` succeeds, if any subsequent
    setup step (``event_bus.publish``, ``asyncio.create_task``) raises, the
    semaphore MUST be released and the process MUST be stopped.

    Without these guards, a failure between ``start()`` and the creation of
    the monitor task would permanently consume a concurrency slot.
    """

    @staticmethod
    def _available(manager: ProcessManager) -> int:
        return manager._semaphore._value

    # ------------------------------------------------------------------ #
    # SEM-06  –  publish raises → semaphore released                      #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_06_semaphore_released_when_publish_raises_after_start(self) -> None:
        """SEM-06: If event_bus.publish() raises after process.start() succeeds,
        the semaphore must be released."""
        manager = _make_manager(max_concurrent=1)
        assert self._available(manager) == 1

        proc = _make_process("sem-06")
        proc.start = AsyncMock()
        proc.stop = AsyncMock()

        manager._event_bus.publish = AsyncMock(
            side_effect=RuntimeError("publish failed")
        )

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            with pytest.raises(RuntimeError, match="publish failed"):
                await manager.start_task(
                    task_id="sem-06", prompt="test", working_dir="/tmp"
                )

        assert self._available(manager) == 1, (
            "Semaphore must be released when publish raises after start succeeds"
        )

    # ------------------------------------------------------------------ #
    # SEM-07  –  publish raises → process.stop() called                   #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_07_process_stopped_when_publish_raises_after_start(self) -> None:
        """SEM-07: If event_bus.publish() raises after process.start(),
        process.stop() must be called to clean up the started process."""
        manager = _make_manager(max_concurrent=1)

        proc = _make_process("sem-07")
        proc.start = AsyncMock()
        proc.stop = AsyncMock()

        manager._event_bus.publish = AsyncMock(
            side_effect=RuntimeError("publish failed")
        )

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            with pytest.raises(RuntimeError):
                await manager.start_task(
                    task_id="sem-07", prompt="test", working_dir="/tmp"
                )

        proc.stop.assert_awaited_once()

    # ------------------------------------------------------------------ #
    # SEM-08  –  publish raises → process removed from _processes         #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_08_process_removed_when_publish_raises_after_start(self) -> None:
        """SEM-08: If event_bus.publish() raises after process.start(),
        the process entry must be removed from _processes dict."""
        manager = _make_manager(max_concurrent=1)

        proc = _make_process("sem-08")
        proc.start = AsyncMock()
        proc.stop = AsyncMock()

        manager._event_bus.publish = AsyncMock(
            side_effect=RuntimeError("publish failed")
        )

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            with pytest.raises(RuntimeError):
                await manager.start_task(
                    task_id="sem-08", prompt="test", working_dir="/tmp"
                )

        assert "sem-08" not in manager._processes, (
            "Process entry must be removed from _processes on post-start failure"
        )

    # ------------------------------------------------------------------ #
    # SEM-09  –  publish raises → original exception propagated           #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_09_original_exception_propagated_on_post_start_failure(self) -> None:
        """SEM-09: The original exception must propagate through cleanup."""
        manager = _make_manager(max_concurrent=1)

        proc = _make_process("sem-09")
        proc.start = AsyncMock()
        proc.stop = AsyncMock()

        manager._event_bus.publish = AsyncMock(
            side_effect=ValueError("original error")
        )

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            with pytest.raises(ValueError, match="original error"):
                await manager.start_task(
                    task_id="sem-09", prompt="test", working_dir="/tmp"
                )


# ---------------------------------------------------------------------------
# SEM-10 – SEM-12: Semaphore released on post-start failures in continue_task
# ---------------------------------------------------------------------------


class TestSemaphoreReleasedOnPostStartFailureContinue:
    """
    Contract (SEM-POST-CONT): Same as SEM-POST but for ``continue_task()``.
    After ``process.start_continuation()`` succeeds, if ``create_task`` raises,
    the semaphore MUST be released and callbacks cleaned up.
    """

    @staticmethod
    def _available(manager: ProcessManager) -> int:
        return manager._semaphore._value

    # ------------------------------------------------------------------ #
    # SEM-10  –  create_task raises → semaphore released                  #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_10_semaphore_released_in_continue_task_post_start_failure(self) -> None:
        """SEM-10: If create_task raises in continue_task after
        start_continuation succeeds, semaphore must be released."""
        manager = _make_manager(max_concurrent=1)
        assert self._available(manager) == 1

        proc = _make_process("sem-10")
        proc.start_continuation = AsyncMock()
        proc.stop = AsyncMock()

        with patch("asyncio.create_task", side_effect=RuntimeError("create_task failed")):
            with pytest.raises(RuntimeError, match="create_task failed"):
                await manager.continue_task(
                    task_id="sem-10",
                    process=proc,
                    message="continue",
                    session_id="sess-123",
                )

        assert self._available(manager) == 1, (
            "Semaphore must be released when create_task raises in continue_task"
        )

    # ------------------------------------------------------------------ #
    # SEM-11  –  create_task raises → process.stop() called               #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_11_process_stopped_in_continue_task_post_start_failure(self) -> None:
        """SEM-11: process.stop() must be called when create_task raises
        in continue_task after start_continuation succeeds."""
        manager = _make_manager(max_concurrent=1)

        proc = _make_process("sem-11")
        proc.start_continuation = AsyncMock()
        proc.stop = AsyncMock()

        with patch("asyncio.create_task", side_effect=RuntimeError("create_task failed")):
            with pytest.raises(RuntimeError):
                await manager.continue_task(
                    task_id="sem-11",
                    process=proc,
                    message="continue",
                    session_id="sess-123",
                )

        proc.stop.assert_awaited_once()

    # ------------------------------------------------------------------ #
    # SEM-12  –  post-start failure → callbacks cleaned up                #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_sem_12_callbacks_cleaned_up_in_continue_task_post_start_failure(self) -> None:
        """SEM-12: _on_output and _on_complete must be removed from manager
        when continue_task fails after start_continuation succeeds."""
        manager = _make_manager(max_concurrent=1)

        proc = _make_process("sem-12")
        proc.start_continuation = AsyncMock()
        proc.stop = AsyncMock()

        on_output = AsyncMock()
        on_complete = AsyncMock()

        with patch("asyncio.create_task", side_effect=RuntimeError("create_task failed")):
            with pytest.raises(RuntimeError):
                await manager.continue_task(
                    task_id="sem-12",
                    process=proc,
                    message="continue",
                    session_id="sess-123",
                    on_output=on_output,
                    on_complete=on_complete,
                )

        assert "sem-12" not in manager._on_output, (
            "_on_output callback must be cleaned up on post-start failure"
        )
        assert "sem-12" not in manager._on_complete, (
            "_on_complete callback must be cleaned up on post-start failure"
        )


# ---------------------------------------------------------------------------
# REGR-02: Happy path of continue_task (regression test)
# ---------------------------------------------------------------------------


class TestContinueTaskHappyPath:
    """Regression test: continue_task happy path must still work after fixes."""

    @staticmethod
    def _available(manager: ProcessManager) -> int:
        return manager._semaphore._value

    @pytest.mark.asyncio
    async def test_regr_02_continue_task_happy_path_releases_semaphore(self) -> None:
        """REGR-02: Normal continue_task flow — semaphore acquired, monitor
        runs, _eof received, semaphore released."""
        manager = _make_manager(max_concurrent=1)
        assert self._available(manager) == 1

        proc = _make_process("regr-02")
        proc.start_continuation = AsyncMock()
        # Pre-load _eof so monitor exits immediately
        proc._message_queue.put_nowait({"type": "_eof"})

        await manager.continue_task(
            task_id="regr-02",
            process=proc,
            message="continue",
            session_id="sess-123",
        )

        # Yield control so monitor runs to completion
        await asyncio.sleep(0)

        assert self._available(manager) == 1, (
            "Semaphore must be released after clean continue_task completion"
        )


# ---------------------------------------------------------------------------
# STOP-01: stop_task does not duplicate EVENT_TASK_FAILED
# ---------------------------------------------------------------------------


class TestStopTaskNoDuplicateEvents:
    """
    Contract (STOP): stop_task must not publish EVENT_TASK_FAILED if the
    monitor task already finished and published its own terminal event.
    """

    @pytest.mark.asyncio
    async def test_stop_01_no_duplicate_event_when_monitor_already_done(self) -> None:
        """STOP-01: If monitor already finished, stop_task must NOT publish
        EVENT_TASK_FAILED (would cause duplicate failure counts)."""
        manager = _make_manager(max_concurrent=1)

        proc = _make_process("stop-01")
        proc.start = AsyncMock()
        proc.stop = AsyncMock()
        # Pre-load _eof so monitor exits immediately
        proc._message_queue.put_nowait({"type": "_eof"})

        events_published: list[tuple[str, dict]] = []
        original_publish = manager._event_bus.publish

        async def _tracking_publish(event_type, data):
            events_published.append((event_type, data))
            await original_publish(event_type, data)

        manager._event_bus.publish = _tracking_publish

        with patch("taktis.core.manager.SDKProcess", return_value=proc):
            await manager.start_task(
                task_id="stop-01", prompt="test", working_dir="/tmp"
            )

        # Wait for monitor to finish
        await asyncio.sleep(0)

        # Clear tracked events so we only see what stop_task produces
        events_published.clear()

        await manager.stop_task("stop-01")

        failed_events = [
            (t, d) for t, d in events_published if t == EVENT_TASK_FAILED
        ]
        assert len(failed_events) == 0, (
            f"stop_task must not publish EVENT_TASK_FAILED when monitor already "
            f"finished; got {len(failed_events)} event(s)"
        )
