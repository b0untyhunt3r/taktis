"""Unit tests for ConsultSession and ConsultRegistry.

Covers:
  - ConsultSession field initialization
  - send() appends user message synchronously
  - Background query task cancellation via stop()
  - stream_response() yields SDK text tokens
  - _is_running flag lifecycle
  - Assistant message appended after query completes
  - ConsultRegistry create / get / remove lifecycle
  - sweep_expired() removes TTL-exceeded sessions
  - LRU eviction when registry is at capacity
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from taktis.core.consult import ConsultRegistry, ConsultSession

# ---------------------------------------------------------------------------
# Async-generator stubs for sdk_query
# ---------------------------------------------------------------------------


async def _instant_result(*args: Any, **kwargs: Any):
    """Yields a single ResultMessage-like object with no text output."""

    class _Result:
        result = "done"
        total_cost_usd = 0.0
        session_id = "s-instant"

    yield _Result()


async def _text_then_result(*args: Any, **kwargs: Any):
    """Yields a text_delta StreamEvent followed by a ResultMessage."""

    class _StreamEvent:
        event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "assistant reply"},
        }

    class _Result:
        result = "done"
        total_cost_usd = 0.0
        session_id = "s-textresult"

    yield _StreamEvent()
    yield _Result()


async def _hanging(*args: Any, **kwargs: Any):
    """Hangs forever — used to test cancellation."""
    await asyncio.sleep(9_999)
    yield  # pragma: no cover — never reached; required to make this an async generator


# ---------------------------------------------------------------------------
# TestConsultSession
# ---------------------------------------------------------------------------


class TestConsultSession:
    def test_create_has_correct_fields(self) -> None:
        """Constructor sets token, empty messages, None session_id, _is_running=False."""
        session = ConsultSession(
            token="abcd1234", working_dir="/tmp", system_prompt="test"
        )
        assert session.token == "abcd1234"
        assert session.messages == []
        assert session.session_id is None
        assert session._is_running is False

    @pytest.mark.asyncio
    async def test_send_appends_user_message(self) -> None:
        """send() immediately appends the user turn before the task runs."""
        session = ConsultSession(
            token="tok00001", working_dir="/tmp", system_prompt="test"
        )
        with patch("taktis.core.consult.sdk_query", _instant_result):
            session.send("hello")
            # Check the message is appended synchronously (before task finishes)
            assert len(session.messages) == 1
            assert session.messages[0]["role"] == "user"
            assert session.messages[0]["content"] == "hello"
            # Drain the background task so it doesn't leak into later tests
            assert session._task is not None
            await session._task

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        """stop() cancels the running background query task."""
        session = ConsultSession(
            token="tok00002", working_dir="/tmp", system_prompt="test"
        )
        with patch("taktis.core.consult.sdk_query", _hanging):
            session.send("anything")
            task = session._task
            assert task is not None
            session.stop()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_stream_response_yields_text_chunks(self) -> None:
        """stream_response() yields every text token emitted by the SDK."""
        session = ConsultSession(
            token="tok00003", working_dir="/tmp", system_prompt="test"
        )
        with patch("taktis.core.consult.sdk_query", _text_then_result):
            session.send("hi")
            chunks: list[str] = []
            async for chunk in session.stream_response():
                chunks.append(chunk)

        assert "assistant reply" in chunks

    @pytest.mark.asyncio
    async def test_send_sets_is_running(self) -> None:
        """_is_running is True immediately after send() before the task finishes."""
        session = ConsultSession(
            token="tok00004", working_dir="/tmp", system_prompt="test"
        )
        with patch("taktis.core.consult.sdk_query", _instant_result):
            session.send("test")
            assert session._is_running is True
            assert session._task is not None
            await session._task

    @pytest.mark.asyncio
    async def test_run_query_appends_assistant_message(self) -> None:
        """_run_query appends an assistant turn after streaming completes."""
        session = ConsultSession(
            token="tok00005", working_dir="/tmp", system_prompt="test"
        )
        with patch("taktis.core.consult.sdk_query", _text_then_result):
            session.send("question")
            await session._task

        assert len(session.messages) == 2
        assert session.messages[1]["role"] == "assistant"
        assert session.messages[1]["content"] == "assistant reply"


# ---------------------------------------------------------------------------
# TestConsultRegistry
# ---------------------------------------------------------------------------


class TestConsultRegistry:
    def test_create_returns_session(self) -> None:
        """create() returns a ConsultSession with a 16-char hex token, registered."""
        r = ConsultRegistry()
        s = r.create("prompt", "/tmp")
        assert isinstance(s.token, str)
        assert len(s.token) == 16
        # Verify it's valid hex
        int(s.token, 16)
        assert r.get(s.token) is s

    def test_get_retrieves_session(self) -> None:
        """get() returns the exact same session object that was created."""
        r = ConsultRegistry()
        s = r.create("prompt", "/tmp")
        assert r.get(s.token) is s

    def test_remove_deletes_session(self) -> None:
        """remove() deletes the session so get() returns None afterwards."""
        r = ConsultRegistry()
        s = r.create("prompt", "/tmp")
        token = s.token
        r.remove(token)
        assert r.get(token) is None

    def test_sweep_removes_expired(self) -> None:
        """sweep_expired() removes sessions whose last_active exceeds the TTL."""
        r = ConsultRegistry()
        s = r.create("prompt", "/tmp")
        token = s.token
        # Back-date last_active by 2 000 s (TTL is 1 800 s)
        s.last_active = time.monotonic() - 2_000
        r.sweep_expired()
        assert r.get(token) is None

    def test_max_sessions_evicts_lru(self) -> None:
        """Creating a 6th session silently evicts the least-recently-active one."""
        r = ConsultRegistry()
        sessions = [r.create(f"prompt-{i}", "/tmp") for i in range(5)]
        # Make the first session the oldest
        sessions[0].last_active = time.monotonic() - 1_000
        oldest_token = sessions[0].token

        sixth = r.create("prompt-new", "/tmp")

        assert len(r._sessions) == 5
        assert oldest_token not in r._sessions
        assert sixth.token in r._sessions

    def test_remove_nonexistent_is_noop(self) -> None:
        """remove() on an unknown token must not raise."""
        r = ConsultRegistry()
        r.remove("deadbeef")  # should not raise

    def test_sweep_keeps_fresh_sessions(self) -> None:
        """sweep_expired() does not remove sessions that are still within TTL."""
        r = ConsultRegistry()
        s = r.create("prompt", "/tmp")
        token = s.token
        # last_active is current time — session is fresh
        r.sweep_expired()
        assert r.get(token) is s
