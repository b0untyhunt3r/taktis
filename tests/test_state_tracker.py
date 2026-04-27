"""Comprehensive tests for StateTracker (taktis.core.state).

Covers lifecycle (start/stop), all public API methods, background event
processing, and the _handle_event dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.events import (
    EVENT_PHASE_COMPLETED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EventBus,
)
from taktis.core.state import StateTracker, _DEFAULT_METRICS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state_row(
    project_id: str = "proj1",
    status: str = "active",
    decisions: list | None = None,
    blockers: list | None = None,
    metrics: dict | None = None,
    current_phase_id: str | None = None,
    last_session_at: str | None = None,
    last_session_description: str | None = None,
) -> dict:
    """Build a dict that looks like a DB row from get_project_state.

    JSON fields are serialized as strings (as they come from aiosqlite).
    """
    return {
        "project_id": project_id,
        "status": status,
        "decisions": json.dumps(decisions or []),
        "blockers": json.dumps(blockers or []),
        "metrics": json.dumps(metrics or dict(_DEFAULT_METRICS)),
        "current_phase_id": current_phase_id,
        "last_session_at": last_session_at,
        "last_session_description": last_session_description,
    }


def _mock_conn() -> MagicMock:
    """Return a mock connection with execute and commit methods."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.commit = AsyncMock()
    return conn


def _build_tracker(mock_conn=None):
    """Build a StateTracker with a mock session factory and EventBus.

    Returns (tracker, event_bus, mock_conn).
    """
    if mock_conn is None:
        mock_conn = _mock_conn()

    @asynccontextmanager
    async def session_factory():
        yield mock_conn

    event_bus = EventBus()
    tracker = StateTracker(session_factory, event_bus)
    return tracker, event_bus, mock_conn


# ---------------------------------------------------------------------------
# Lifecycle: start / stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Tests for start() and stop() behaviour."""

    @pytest.mark.asyncio
    async def test_start_subscribes_to_three_events(self):
        tracker, event_bus, _ = _build_tracker()

        await tracker.start()
        try:
            # Should have subscribed to task.completed, task.failed, phase.completed
            assert len(tracker._queues) == 3
            event_types = {et for et, _ in tracker._queues}
            assert event_types == {EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, EVENT_PHASE_COMPLETED}
            assert tracker._running is True
            assert tracker._bg_task is not None
        finally:
            await tracker.stop()

    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        tracker, event_bus, _ = _build_tracker()

        await tracker.start()
        try:
            assert tracker._bg_task is not None
            assert not tracker._bg_task.done()
            assert tracker._bg_task.get_name() == "state-tracker"
        finally:
            await tracker.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        tracker, event_bus, _ = _build_tracker()

        await tracker.start()
        try:
            first_task = tracker._bg_task
            first_queues = list(tracker._queues)

            # Second start should be a no-op
            await tracker.start()

            assert tracker._bg_task is first_task
            assert tracker._queues == first_queues
            assert len(tracker._queues) == 3
        finally:
            await tracker.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_bg_task(self):
        tracker, event_bus, _ = _build_tracker()

        await tracker.start()
        bg_task = tracker._bg_task
        assert bg_task is not None

        await tracker.stop()

        assert tracker._bg_task is None
        assert tracker._running is False
        assert bg_task.cancelled() or bg_task.done()

    @pytest.mark.asyncio
    async def test_stop_unsubscribes(self):
        tracker, event_bus, _ = _build_tracker()

        await tracker.start()
        assert event_bus.subscriber_count(EVENT_TASK_COMPLETED) >= 1

        await tracker.stop()

        assert event_bus.subscriber_count(EVENT_TASK_COMPLETED) == 0
        assert event_bus.subscriber_count(EVENT_TASK_FAILED) == 0
        assert event_bus.subscriber_count(EVENT_PHASE_COMPLETED) == 0
        assert tracker._queues == []

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        """stop() on a fresh tracker should not raise."""
        tracker, _, _ = _build_tracker()
        # Should be safe -- no error
        await tracker.stop()
        assert tracker._running is False
        assert tracker._bg_task is None


# ---------------------------------------------------------------------------
# get_project_state
# ---------------------------------------------------------------------------


class TestGetProjectState:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_returns_default_when_no_row(self, mock_repo):
        mock_repo.get_project_state = AsyncMock(return_value=None)
        tracker, _, _ = _build_tracker()

        state = await tracker.get_project_state("proj1")

        assert state["project_id"] == "proj1"
        assert state["status"] == "idle"
        assert state["decisions"] == []
        assert state["blockers"] == []
        assert state["metrics"] == dict(_DEFAULT_METRICS)
        assert state["current_phase_id"] is None
        assert state["last_session_at"] is None
        assert state["last_session_description"] is None

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_returns_parsed_state(self, mock_repo):
        decisions = [{"description": "Use Python", "rationale": "Good fit"}]
        blockers = [{"description": "Need API key", "resolved": False}]
        metrics = {"tasks_completed": 5, "tasks_failed": 1, "total_cost_usd": 1.5, "total_duration_s": 120.0}

        row = _make_state_row(
            project_id="proj2",
            status="active",
            decisions=decisions,
            blockers=blockers,
            metrics=metrics,
            current_phase_id="phase_abc",
            last_session_at="2026-03-29T10:00:00+00:00",
            last_session_description="Working on tests",
        )
        mock_repo.get_project_state = AsyncMock(return_value=row)
        tracker, _, _ = _build_tracker()

        state = await tracker.get_project_state("proj2")

        assert state["project_id"] == "proj2"
        assert state["status"] == "active"
        assert state["decisions"] == decisions
        assert state["blockers"] == blockers
        assert state["metrics"] == metrics
        assert state["current_phase_id"] == "phase_abc"
        assert state["last_session_at"] == "2026-03-29T10:00:00+00:00"
        assert state["last_session_description"] == "Working on tests"

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_default_metrics_structure(self, mock_repo):
        """Default metrics must contain the four expected keys."""
        mock_repo.get_project_state = AsyncMock(return_value=None)
        tracker, _, _ = _build_tracker()

        state = await tracker.get_project_state("proj_x")
        metrics = state["metrics"]

        assert "tasks_completed" in metrics and metrics["tasks_completed"] == 0
        assert "tasks_failed" in metrics and metrics["tasks_failed"] == 0
        assert "total_cost_usd" in metrics and metrics["total_cost_usd"] == 0.0
        assert "total_duration_s" in metrics and metrics["total_duration_s"] == 0.0


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


class TestUpdateStatus:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_update_status(self, mock_repo):
        mock_repo.get_project_state = AsyncMock(return_value=_make_state_row())
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        await tracker.update_status("proj1", "active")

        mock_repo.update_project_state.assert_awaited_once()
        call_kwargs = mock_repo.update_project_state.call_args
        assert call_kwargs[1]["status"] == "active"


# ---------------------------------------------------------------------------
# add_decision
# ---------------------------------------------------------------------------


class TestAddDecision:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_appends_decision_with_auto_timestamp(self, mock_repo):
        row = _make_state_row(decisions=[])
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        decision = {"description": "Use REST API", "rationale": "Simplicity"}
        await tracker.add_decision("proj1", decision)

        # A timestamp should have been added
        assert "timestamp" in decision

        mock_repo.update_project_state.assert_awaited_once()
        call_kwargs = mock_repo.update_project_state.call_args
        saved_decisions = call_kwargs[1]["decisions"]
        assert len(saved_decisions) == 1
        assert saved_decisions[0]["description"] == "Use REST API"

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_preserves_existing_decisions(self, mock_repo):
        existing = [{"description": "Prior decision", "timestamp": "2026-01-01T00:00:00"}]
        row = _make_state_row(decisions=existing)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        new_decision = {"description": "New decision", "rationale": "Good"}
        await tracker.add_decision("proj1", new_decision)

        call_kwargs = mock_repo.update_project_state.call_args
        saved_decisions = call_kwargs[1]["decisions"]
        assert len(saved_decisions) == 2
        assert saved_decisions[0]["description"] == "Prior decision"
        assert saved_decisions[1]["description"] == "New decision"

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_caps_at_200_decisions(self, mock_repo):
        existing = [{"description": f"d{i}", "timestamp": "t"} for i in range(200)]
        row = _make_state_row(decisions=existing)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        new_decision = {"description": "d200"}
        await tracker.add_decision("proj1", new_decision)

        call_kwargs = mock_repo.update_project_state.call_args
        saved = call_kwargs[1]["decisions"]
        assert len(saved) == 200
        # Oldest should have been dropped (d0), newest should be d200
        assert saved[0]["description"] == "d1"
        assert saved[-1]["description"] == "d200"

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_does_not_override_existing_timestamp(self, mock_repo):
        row = _make_state_row(decisions=[])
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        custom_ts = "2025-12-25T00:00:00+00:00"
        decision = {"description": "Holiday decision", "timestamp": custom_ts}
        await tracker.add_decision("proj1", decision)

        assert decision["timestamp"] == custom_ts


# ---------------------------------------------------------------------------
# add_blocker
# ---------------------------------------------------------------------------


class TestAddBlocker:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_sets_defaults(self, mock_repo):
        row = _make_state_row(blockers=[])
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        blocker = {"description": "Need access"}
        await tracker.add_blocker("proj1", blocker)

        assert blocker["resolved"] is False
        assert "timestamp" in blocker

        call_kwargs = mock_repo.update_project_state.call_args
        saved = call_kwargs[1]["blockers"]
        assert len(saved) == 1
        assert saved[0]["resolved"] is False

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_appends_to_existing_blockers(self, mock_repo):
        existing = [{"description": "Old blocker", "resolved": True, "timestamp": "t"}]
        row = _make_state_row(blockers=existing)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        new_blocker = {"description": "New blocker"}
        await tracker.add_blocker("proj1", new_blocker)

        call_kwargs = mock_repo.update_project_state.call_args
        saved = call_kwargs[1]["blockers"]
        assert len(saved) == 2
        assert saved[0]["description"] == "Old blocker"
        assert saved[1]["description"] == "New blocker"

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_caps_at_200_blockers(self, mock_repo):
        existing = [{"description": f"b{i}", "resolved": False, "timestamp": "t"} for i in range(200)]
        row = _make_state_row(blockers=existing)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        new_blocker = {"description": "b200"}
        await tracker.add_blocker("proj1", new_blocker)

        call_kwargs = mock_repo.update_project_state.call_args
        saved = call_kwargs[1]["blockers"]
        assert len(saved) == 200
        assert saved[0]["description"] == "b1"
        assert saved[-1]["description"] == "b200"


# ---------------------------------------------------------------------------
# resolve_blocker
# ---------------------------------------------------------------------------


class TestResolveBlocker:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_marks_resolved_with_timestamp(self, mock_repo):
        blockers = [
            {"description": "Blocker A", "resolved": False, "timestamp": "t1"},
            {"description": "Blocker B", "resolved": False, "timestamp": "t2"},
        ]
        row = _make_state_row(blockers=blockers)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        await tracker.resolve_blocker("proj1", 0)

        mock_repo.update_project_state.assert_awaited_once()
        call_kwargs = mock_repo.update_project_state.call_args
        saved = call_kwargs[1]["blockers"]
        assert saved[0]["resolved"] is True
        assert "resolved_at" in saved[0]
        # Second blocker should be unchanged
        assert saved[1]["resolved"] is False

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_out_of_bounds_index_logs_warning(self, mock_repo, caplog):
        blockers = [{"description": "Only one", "resolved": False, "timestamp": "t"}]
        row = _make_state_row(blockers=blockers)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        with caplog.at_level(logging.WARNING, logger="taktis.core.state"):
            await tracker.resolve_blocker("proj1", 5)

        assert "out of range" in caplog.text.lower()
        # update_project_state should NOT have been called for the blocker update
        mock_repo.update_project_state.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_negative_index_logs_warning(self, mock_repo, caplog):
        blockers = [{"description": "B1", "resolved": False, "timestamp": "t"}]
        row = _make_state_row(blockers=blockers)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        with caplog.at_level(logging.WARNING, logger="taktis.core.state"):
            await tracker.resolve_blocker("proj1", -1)

        assert "out of range" in caplog.text.lower()
        mock_repo.update_project_state.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_metrics
# ---------------------------------------------------------------------------


class TestUpdateMetrics:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_numeric_values_are_added(self, mock_repo):
        """Numeric values should be added to existing, not replaced."""
        existing_metrics = {
            "tasks_completed": 3,
            "tasks_failed": 1,
            "total_cost_usd": 0.50,
            "total_duration_s": 60.0,
        }
        row = _make_state_row(metrics=existing_metrics)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        conn = _mock_conn()
        tracker, _, _ = _build_tracker(conn)

        await tracker.update_metrics("proj1", {
            "tasks_completed": 1,
            "total_cost_usd": 0.25,
        })

        call_kwargs = mock_repo.update_project_state.call_args
        saved_metrics = call_kwargs[1]["metrics"]
        assert saved_metrics["tasks_completed"] == 4
        assert saved_metrics["total_cost_usd"] == pytest.approx(0.75)
        # Unchanged fields should remain
        assert saved_metrics["tasks_failed"] == 1
        assert saved_metrics["total_duration_s"] == 60.0

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_non_numeric_values_overwrite(self, mock_repo):
        existing_metrics = dict(_DEFAULT_METRICS)
        existing_metrics["custom_label"] = "old_value"
        row = _make_state_row(metrics=existing_metrics)
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        conn = _mock_conn()
        tracker, _, _ = _build_tracker(conn)

        await tracker.update_metrics("proj1", {"custom_label": "new_value"})

        call_kwargs = mock_repo.update_project_state.call_args
        saved_metrics = call_kwargs[1]["metrics"]
        assert saved_metrics["custom_label"] == "new_value"

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_uses_begin_immediate(self, mock_repo):
        """update_metrics must issue BEGIN IMMEDIATE for serialization."""
        row = _make_state_row()
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        conn = _mock_conn()
        tracker, _, _ = _build_tracker(conn)

        await tracker.update_metrics("proj1", {"tasks_completed": 1})

        conn.execute.assert_any_await("BEGIN IMMEDIATE")
        conn.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_new_numeric_key_is_set_directly(self, mock_repo):
        """A new numeric key not present in existing metrics should be set (not added)."""
        row = _make_state_row(metrics=dict(_DEFAULT_METRICS))
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        conn = _mock_conn()
        tracker, _, _ = _build_tracker(conn)

        await tracker.update_metrics("proj1", {"new_counter": 5})

        call_kwargs = mock_repo.update_project_state.call_args
        saved_metrics = call_kwargs[1]["metrics"]
        # new_counter is not in existing metrics, so isinstance check on
        # metrics.get("new_counter") returns None, which is not (int, float),
        # so it should be overwritten (set to 5)
        assert saved_metrics["new_counter"] == 5


# ---------------------------------------------------------------------------
# set_current_phase
# ---------------------------------------------------------------------------


class TestSetCurrentPhase:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_sets_phase(self, mock_repo):
        row = _make_state_row()
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        await tracker.set_current_phase("proj1", "phase_42")

        mock_repo.update_project_state.assert_awaited_once()
        call_kwargs = mock_repo.update_project_state.call_args
        assert call_kwargs[1]["current_phase_id"] == "phase_42"

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_sets_phase_to_none(self, mock_repo):
        row = _make_state_row()
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        await tracker.set_current_phase("proj1", None)

        call_kwargs = mock_repo.update_project_state.call_args
        assert call_kwargs[1]["current_phase_id"] is None


# ---------------------------------------------------------------------------
# record_session
# ---------------------------------------------------------------------------


class TestRecordSession:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_records_session(self, mock_repo):
        row = _make_state_row()
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()

        await tracker.record_session("proj1", "Did some testing")

        mock_repo.update_project_state.assert_awaited_once()
        call_kwargs = mock_repo.update_project_state.call_args
        assert call_kwargs[1]["last_session_description"] == "Did some testing"
        assert call_kwargs[1]["last_session_at"] is not None


# ---------------------------------------------------------------------------
# _ensure_state
# ---------------------------------------------------------------------------


class TestEnsureState:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_creates_state_when_missing(self, mock_repo):
        mock_repo.get_project_state = AsyncMock(return_value=None)
        mock_repo.create_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()
        conn = _mock_conn()

        await tracker._ensure_state(conn, "proj_new")

        mock_repo.create_project_state.assert_awaited_once()
        call_args = mock_repo.create_project_state.call_args
        assert call_args[0] == (conn, "proj_new")
        assert call_args[1]["status"] == "idle"
        assert call_args[1]["decisions"] == []
        assert call_args[1]["blockers"] == []

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_does_not_create_when_exists(self, mock_repo):
        mock_repo.get_project_state = AsyncMock(return_value=_make_state_row())
        mock_repo.create_project_state = AsyncMock()
        tracker, _, _ = _build_tracker()
        conn = _mock_conn()

        await tracker._ensure_state(conn, "proj1")

        mock_repo.create_project_state.assert_not_awaited()


# ---------------------------------------------------------------------------
# _handle_event
# ---------------------------------------------------------------------------


class TestHandleEvent:

    @pytest.mark.asyncio
    async def test_task_completed_updates_metrics(self):
        tracker, _, _ = _build_tracker()
        tracker.update_metrics = AsyncMock()

        envelope = {
            "event_type": EVENT_TASK_COMPLETED,
            "data": {
                "project_id": "proj1",
                "cost_usd": 0.05,
                "duration_s": 30.0,
            },
        }

        await tracker._handle_event(EVENT_TASK_COMPLETED, envelope)

        tracker.update_metrics.assert_awaited_once_with(
            "proj1",
            {
                "tasks_completed": 1,
                "total_cost_usd": 0.05,
                "total_duration_s": 30.0,
            },
        )

    @pytest.mark.asyncio
    async def test_task_completed_defaults_cost_and_duration(self):
        """When cost_usd and duration_s are missing, defaults to 0.0."""
        tracker, _, _ = _build_tracker()
        tracker.update_metrics = AsyncMock()

        envelope = {
            "event_type": EVENT_TASK_COMPLETED,
            "data": {"project_id": "proj1"},
        }

        await tracker._handle_event(EVENT_TASK_COMPLETED, envelope)

        tracker.update_metrics.assert_awaited_once_with(
            "proj1",
            {
                "tasks_completed": 1,
                "total_cost_usd": 0.0,
                "total_duration_s": 0.0,
            },
        )

    @pytest.mark.asyncio
    async def test_task_failed_updates_metrics(self):
        tracker, _, _ = _build_tracker()
        tracker.update_metrics = AsyncMock()

        envelope = {
            "event_type": EVENT_TASK_FAILED,
            "data": {"project_id": "proj1"},
        }

        await tracker._handle_event(EVENT_TASK_FAILED, envelope)

        tracker.update_metrics.assert_awaited_once_with(
            "proj1",
            {"tasks_failed": 1},
        )

    @pytest.mark.asyncio
    async def test_phase_completed_logs_info(self, caplog):
        tracker, _, _ = _build_tracker()

        envelope = {
            "event_type": EVENT_PHASE_COMPLETED,
            "data": {"project_id": "proj1", "phase_id": "phase_abc"},
        }

        with caplog.at_level(logging.INFO, logger="taktis.core.state"):
            await tracker._handle_event(EVENT_PHASE_COMPLETED, envelope)

        assert "phase_abc" in caplog.text
        assert "proj1" in caplog.text

    @pytest.mark.asyncio
    async def test_missing_project_id_returns_early(self):
        """If project_id is missing from the event data, _handle_event is a no-op."""
        tracker, _, _ = _build_tracker()
        tracker.update_metrics = AsyncMock()

        envelope = {
            "event_type": EVENT_TASK_COMPLETED,
            "data": {"cost_usd": 0.10},  # no project_id
        }

        await tracker._handle_event(EVENT_TASK_COMPLETED, envelope)

        tracker.update_metrics.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_data_returns_early(self):
        tracker, _, _ = _build_tracker()
        tracker.update_metrics = AsyncMock()

        envelope = {"event_type": EVENT_TASK_COMPLETED}  # no data key

        await tracker._handle_event(EVENT_TASK_COMPLETED, envelope)

        tracker.update_metrics.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_in_handler_is_logged_not_raised(self, caplog):
        tracker, _, _ = _build_tracker()
        tracker.update_metrics = AsyncMock(side_effect=RuntimeError("DB crash"))

        envelope = {
            "event_type": EVENT_TASK_COMPLETED,
            "data": {"project_id": "proj1"},
        }

        with caplog.at_level(logging.ERROR, logger="taktis.core.state"):
            # Should not raise
            await tracker._handle_event(EVENT_TASK_COMPLETED, envelope)

        assert "failed to handle" in caplog.text.lower()


# ---------------------------------------------------------------------------
# _process_events (integration-style)
# ---------------------------------------------------------------------------


class TestProcessEvents:

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_events_reach_handle_event(self, mock_repo):
        """Publishing events on the EventBus should reach _handle_event."""
        row = _make_state_row()
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()

        tracker, event_bus, _ = _build_tracker()
        tracker._handle_event = AsyncMock()

        await tracker.start()
        try:
            await event_bus.publish(EVENT_TASK_COMPLETED, {
                "project_id": "proj1",
                "cost_usd": 0.10,
                "duration_s": 5.0,
            })

            # Give the background loop time to process the event
            for _ in range(20):
                await asyncio.sleep(0.05)
                if tracker._handle_event.await_count > 0:
                    break

            tracker._handle_event.assert_awaited()
            call_args = tracker._handle_event.call_args
            assert call_args[0][0] == EVENT_TASK_COMPLETED
        finally:
            await tracker.stop()

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_multiple_events_processed(self, mock_repo):
        row = _make_state_row()
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()

        tracker, event_bus, _ = _build_tracker()
        tracker._handle_event = AsyncMock()

        await tracker.start()
        try:
            # Publish three different event types
            await event_bus.publish(EVENT_TASK_COMPLETED, {"project_id": "proj1"})
            await event_bus.publish(EVENT_TASK_FAILED, {"project_id": "proj1"})
            await event_bus.publish(EVENT_PHASE_COMPLETED, {"project_id": "proj1", "phase_id": "p1"})

            # Wait for all events to be processed
            for _ in range(30):
                await asyncio.sleep(0.05)
                if tracker._handle_event.await_count >= 3:
                    break

            assert tracker._handle_event.await_count >= 3

            event_types_seen = {
                call[0][0] for call in tracker._handle_event.call_args_list
            }
            assert EVENT_TASK_COMPLETED in event_types_seen
            assert EVENT_TASK_FAILED in event_types_seen
            assert EVENT_PHASE_COMPLETED in event_types_seen
        finally:
            await tracker.stop()

    @pytest.mark.asyncio
    @patch("taktis.core.state.repo")
    async def test_handler_exception_does_not_kill_loop(self, mock_repo):
        """An exception in _handle_event should be caught, not crash the loop."""
        row = _make_state_row()
        mock_repo.get_project_state = AsyncMock(return_value=row)
        mock_repo.create_project_state = AsyncMock()
        mock_repo.update_project_state = AsyncMock()

        tracker, event_bus, _ = _build_tracker()

        call_count = 0

        async def _failing_then_succeeding(event_type, envelope):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated failure")
            # second call succeeds

        tracker._handle_event = AsyncMock(side_effect=_failing_then_succeeding)

        await tracker.start()
        try:
            # First event will cause an exception in handler
            await event_bus.publish(EVENT_TASK_COMPLETED, {"project_id": "proj1"})

            # Wait a bit for the error to be handled
            await asyncio.sleep(0.3)

            # Second event should still be processed (loop survived)
            await event_bus.publish(EVENT_TASK_FAILED, {"project_id": "proj1"})

            for _ in range(20):
                await asyncio.sleep(0.05)
                if call_count >= 2:
                    break

            assert call_count >= 2, f"Expected at least 2 handler calls, got {call_count}"
            assert tracker._running is True, "Loop should still be running"
        finally:
            await tracker.stop()

    @pytest.mark.asyncio
    async def test_stop_terminates_process_events(self):
        """Calling stop() should cleanly terminate _process_events."""
        tracker, _, _ = _build_tracker()

        await tracker.start()
        assert tracker._running is True

        await tracker.stop()

        assert tracker._running is False
        assert tracker._bg_task is None
