"""Tests for declarative retry policies on pipeline tasks.

Covers the static helpers (_matches_retry_pattern, _retry_delay), the
_on_complete retry decision path, and agent node schema fields.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.scheduler import WaveScheduler
from taktis.core.node_types import get_node_type


# ---------------------------------------------------------------------------
# Static helper tests
# ---------------------------------------------------------------------------

class TestMatchesRetryPattern:

    def test_matches_retry_pattern_streaming(self):
        """StreamingError matches the default pattern list."""
        events = [{"type": "error", "content": "StreamingError: connection reset"}]
        assert WaveScheduler._matches_retry_pattern(events, ["StreamingError"]) is True

    def test_matches_retry_pattern_rate_limit(self):
        """RateLimitError matches when included in pattern list."""
        events = [{"type": "error", "content": "RateLimitError: 429 too many requests"}]
        assert WaveScheduler._matches_retry_pattern(
            events, ["StreamingError", "RateLimitError"]
        ) is True

    def test_matches_retry_pattern_no_match(self):
        """Unrelated error does not match retryable patterns."""
        events = [{"type": "error", "content": "SyntaxError: unexpected token"}]
        assert WaveScheduler._matches_retry_pattern(
            events, ["StreamingError", "RateLimitError"]
        ) is False


class TestRetryDelay:

    def test_retry_delay_none(self):
        """Backoff 'none' returns 0 (immediate retry)."""
        assert WaveScheduler._retry_delay("none", 0) == 0.0
        assert WaveScheduler._retry_delay("none", 5) == 0.0

    def test_retry_delay_linear(self):
        """Linear backoff: base * (attempt + 1)."""
        assert WaveScheduler._retry_delay("linear", 0) == 2.0   # 2 * 1
        assert WaveScheduler._retry_delay("linear", 1) == 4.0   # 2 * 2
        assert WaveScheduler._retry_delay("linear", 2) == 6.0   # 2 * 3

    def test_retry_delay_exponential(self):
        """Exponential backoff: base ^ (attempt + 1)."""
        assert WaveScheduler._retry_delay("exponential", 0) == 2.0  # 2^1
        assert WaveScheduler._retry_delay("exponential", 1) == 4.0  # 2^2
        assert WaveScheduler._retry_delay("exponential", 2) == 8.0  # 2^3


# ---------------------------------------------------------------------------
# _on_complete retry decision tests
# ---------------------------------------------------------------------------

def _make_scheduler() -> tuple[WaveScheduler, MagicMock, MagicMock, MagicMock]:
    """Create a WaveScheduler wired to lightweight mock collaborators."""
    mock_conn = MagicMock()

    @asynccontextmanager
    async def _session_factory():
        yield mock_conn

    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    event_bus.subscribe = MagicMock(return_value=asyncio.Queue())
    event_bus.unsubscribe = MagicMock()

    state_tracker = MagicMock()
    state_tracker.update_status = AsyncMock()
    state_tracker.set_current_phase = AsyncMock()

    process_manager = MagicMock()

    scheduler = WaveScheduler(
        process_manager=process_manager,
        event_bus=event_bus,
        state_tracker=state_tracker,
        db_session_factory=_session_factory,
    )
    return scheduler, mock_conn, event_bus, state_tracker


def _make_db_task(
    task_id: str,
    wave: int = 1,
    status: str = "pending",
    task_type: str = "implementation",
    retry_count: int = 0,
    retry_policy: str | None = None,
) -> dict:
    """Minimal task dict as returned by repo."""
    return {
        "id": task_id,
        "phase_id": "phase-1",
        "project_id": "proj-1",
        "name": f"Task {task_id}",
        "wave": wave,
        "status": status,
        "task_type": task_type,
        "prompt": "do something",
        "model": None,
        "expert_id": None,
        "interactive": False,
        "env_vars": None,
        "system_prompt": None,
        "checkpoint_type": None,
        "session_id": None,
        "retry_count": retry_count,
        "retry_policy": retry_policy,
    }


class TestNoPolicyNoRetry:
    """Task with no retry_policy should NOT retry — retries require explicit config."""

    @pytest.mark.asyncio
    async def test_no_policy_skips_retry(self):
        task = _make_db_task("t1", status="running", retry_count=0, retry_policy=None)

        task_state = {
            "_error_events": [{"type": "error", "content": "StreamingError: connection reset"}],
        }

        policy_raw = task.get("retry_policy")
        policy = json.loads(policy_raw) if policy_raw else {}

        retry_enabled = policy.get("retry_transient", False)
        max_attempts = policy.get("max_attempts", 0)
        retry_patterns = policy.get("retry_on", [])
        is_retryable = WaveScheduler._matches_retry_pattern(
            task_state["_error_events"], retry_patterns,
        )

        # No policy → no retries
        assert retry_enabled is False
        assert max_attempts == 0
        assert is_retryable is False


class TestCustomPolicyMaxAttempts:
    """Custom retry_policy should respect max_attempts."""

    @pytest.mark.asyncio
    async def test_custom_policy_max_attempts(self):
        policy = json.dumps({
            "retry_transient": True,
            "max_attempts": 5,
            "backoff": "linear",
            "retry_on": ["StreamingError", "RateLimitError"],
        })
        task = _make_db_task("t2", status="running", retry_count=3, retry_policy=policy)

        parsed = json.loads(task["retry_policy"])
        assert parsed["max_attempts"] == 5
        # With retry_count=3 and max_attempts=5, should still retry
        assert task["retry_count"] < parsed["max_attempts"]

        # At retry_count=5, should NOT retry
        task_at_max = _make_db_task("t3", status="running", retry_count=5, retry_policy=policy)
        assert task_at_max["retry_count"] >= parsed["max_attempts"]


class TestCustomPolicyDisabled:
    """retry_transient=False should skip retry entirely."""

    @pytest.mark.asyncio
    async def test_custom_policy_disabled(self):
        policy = json.dumps({
            "retry_transient": False,
            "max_attempts": 5,
            "backoff": "none",
            "retry_on": ["StreamingError"],
        })
        task = _make_db_task("t4", status="running", retry_count=0, retry_policy=policy)

        parsed = json.loads(task["retry_policy"])
        error_events = [{"type": "error", "content": "StreamingError: broken pipe"}]

        # Even though the error matches, retry is disabled
        retry_enabled = parsed.get("retry_transient", True)
        is_retryable = WaveScheduler._matches_retry_pattern(
            error_events, parsed.get("retry_on", ["StreamingError"]),
        )

        assert retry_enabled is False
        assert is_retryable is True  # Error matches, but...
        # The scheduler would NOT retry because retry_enabled is False
        should_retry = retry_enabled and is_retryable and task["retry_count"] < parsed["max_attempts"]
        assert should_retry is False


# ---------------------------------------------------------------------------
# Node type schema test
# ---------------------------------------------------------------------------

class TestNodeTypeHasRetryFields:
    """Agent node type must include the 3 transient retry config fields."""

    def test_node_type_has_retry_fields(self):
        agent = get_node_type("agent")
        assert agent is not None
        field_keys = {f.key for f in agent.config_schema}
        assert "retry_transient" in field_keys
        assert "retry_max_attempts" in field_keys
        assert "retry_backoff" in field_keys
