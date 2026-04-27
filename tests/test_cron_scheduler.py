"""Tests for CronScheduler and detect_interactive_nodes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio

from taktis.core.cron_scheduler import CronScheduler, detect_interactive_nodes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockOrch:
    """Minimal mock of the Taktis for CronScheduler tests."""

    class _FakeEventBus:
        async def publish(self, *a, **kw):
            pass

    event_bus = _FakeEventBus()

    async def execute_flow(self, project_name, flow_json, template_name=""):
        pass


def _make_scheduler() -> CronScheduler:
    """Create a CronScheduler with mock dependencies (no DB / no network)."""
    return CronScheduler(_MockOrch(), session_factory=None)


def _utc(*args) -> datetime:
    """Shortcut: datetime(..., tzinfo=utc)."""
    return datetime(*args, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _should_run tests
# ---------------------------------------------------------------------------

class TestShouldRun:
    """Unit tests for CronScheduler._should_run()."""

    def test_daily_at_target_hour(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "daily",
            "time_of_day": "03:00",
            "day_of_week": None,
            "last_run_at": None,
        }
        # Exactly at the target hour, minute 0
        assert sched._should_run(schedule, _utc(2026, 4, 2, 3, 0, 0)) is True

    def test_daily_at_target_hour_minute_1(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "daily",
            "time_of_day": "03:00",
            "day_of_week": None,
            "last_run_at": None,
        }
        # Within the 2-minute window
        assert sched._should_run(schedule, _utc(2026, 4, 2, 3, 1, 30)) is True

    def test_daily_wrong_hour(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "daily",
            "time_of_day": "03:00",
            "day_of_week": None,
            "last_run_at": None,
        }
        assert sched._should_run(schedule, _utc(2026, 4, 2, 5, 0, 0)) is False

    def test_daily_skips_if_already_ran(self):
        sched = _make_scheduler()
        # last_run_at 2 hours ago -> within 23h guard
        schedule = {
            "frequency": "daily",
            "time_of_day": "03:00",
            "day_of_week": None,
            "last_run_at": _utc(2026, 4, 2, 1, 0, 0).isoformat(),
        }
        assert sched._should_run(schedule, _utc(2026, 4, 2, 3, 0, 0)) is False

    def test_daily_runs_after_23h(self):
        sched = _make_scheduler()
        # last_run_at > 23 hours ago
        schedule = {
            "frequency": "daily",
            "time_of_day": "03:00",
            "day_of_week": None,
            "last_run_at": _utc(2026, 4, 1, 3, 0, 0).isoformat(),
        }
        assert sched._should_run(schedule, _utc(2026, 4, 2, 3, 0, 0)) is True

    def test_weekly_correct_day(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "weekly",
            "time_of_day": "02:00",
            "day_of_week": "wednesday",
            "last_run_at": None,
        }
        # 2026-04-01 is a Wednesday
        assert sched._should_run(schedule, _utc(2026, 4, 1, 2, 0, 0)) is True

    def test_weekly_wrong_day(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "weekly",
            "time_of_day": "02:00",
            "day_of_week": "monday",
            "last_run_at": None,
        }
        # 2026-04-01 is a Wednesday, not Monday
        assert sched._should_run(schedule, _utc(2026, 4, 1, 2, 0, 0)) is False

    def test_hourly_fires_at_minute_0(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "hourly",
            "time_of_day": "00:00",
            "day_of_week": None,
            "last_run_at": None,
        }
        assert sched._should_run(schedule, _utc(2026, 4, 2, 14, 0, 30)) is True

    def test_hourly_skips_at_minute_5(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "hourly",
            "time_of_day": "00:00",
            "day_of_week": None,
            "last_run_at": None,
        }
        assert sched._should_run(schedule, _utc(2026, 4, 2, 14, 5, 0)) is False

    def test_hourly_skips_recent_run(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "hourly",
            "time_of_day": "00:00",
            "day_of_week": None,
            "last_run_at": _utc(2026, 4, 2, 13, 0, 0).isoformat(),
        }
        # Only 30 minutes since last run
        assert sched._should_run(schedule, _utc(2026, 4, 2, 13, 30, 0)) is False

    def test_monthly_first_of_month(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "monthly",
            "time_of_day": "04:00",
            "day_of_week": None,
            "last_run_at": None,
        }
        assert sched._should_run(schedule, _utc(2026, 4, 1, 4, 0, 0)) is True

    def test_monthly_not_first_of_month(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "monthly",
            "time_of_day": "04:00",
            "day_of_week": None,
            "last_run_at": None,
        }
        assert sched._should_run(schedule, _utc(2026, 4, 15, 4, 0, 0)) is False

    def test_monthly_skips_same_month(self):
        sched = _make_scheduler()
        schedule = {
            "frequency": "monthly",
            "time_of_day": "04:00",
            "day_of_week": None,
            "last_run_at": _utc(2026, 4, 1, 4, 0, 0).isoformat(),
        }
        # Same month, already ran
        assert sched._should_run(schedule, _utc(2026, 4, 1, 4, 1, 0)) is False


# ---------------------------------------------------------------------------
# detect_interactive_nodes tests
# ---------------------------------------------------------------------------

class TestDetectInteractiveNodes:

    def test_detect_interactive_agent(self):
        flow = {
            "drawflow": {
                "Home": {
                    "data": {
                        "1": {
                            "name": "agent",
                            "data": {"name": "Interview", "interactive": True},
                        },
                        "2": {
                            "name": "agent",
                            "data": {"name": "Researcher", "interactive": False},
                        },
                    }
                }
            }
        }
        result = detect_interactive_nodes(flow)
        assert result == ["Interview"]

    def test_detect_human_gate(self):
        flow = {
            "drawflow": {
                "Home": {
                    "data": {
                        "1": {
                            "name": "human_gate",
                            "data": {"name": "Review Gate"},
                        },
                    }
                }
            }
        }
        result = detect_interactive_nodes(flow)
        assert result == ["Review Gate"]

    def test_detect_no_interactive(self):
        flow = {
            "drawflow": {
                "Home": {
                    "data": {
                        "1": {
                            "name": "agent",
                            "data": {"name": "Researcher"},
                        },
                        "2": {
                            "name": "output_parser",
                            "data": {"name": "Parser"},
                        },
                    }
                }
            }
        }
        result = detect_interactive_nodes(flow)
        assert result == []

    def test_detect_from_json_string(self):
        """Accepts a JSON string as well as a dict."""
        import json
        flow = {
            "drawflow": {
                "Home": {
                    "data": {
                        "1": {
                            "name": "agent",
                            "data": {"name": "Chat", "interactive": True},
                        },
                    }
                }
            }
        }
        result = detect_interactive_nodes(json.dumps(flow))
        assert result == ["Chat"]

    def test_detect_multi_module(self):
        """Detects interactive nodes across multiple modules."""
        flow = {
            "drawflow": {
                "Phase 1": {
                    "data": {
                        "1": {
                            "name": "agent",
                            "data": {"name": "Worker"},
                        },
                    }
                },
                "Phase 2": {
                    "data": {
                        "5": {
                            "name": "human_gate",
                            "data": {"name": "Approval"},
                        },
                    }
                },
            }
        }
        result = detect_interactive_nodes(flow)
        assert result == ["Approval"]


# ---------------------------------------------------------------------------
# Repository CRUD test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repository_crud(db_conn):
    """Test create, list, get, update, delete for schedules."""
    from taktis import repository as repo

    # Create
    result = await repo.create_schedule(
        db_conn,
        "sched001",
        name="Nightly Build",
        project_name="my-project",
        template_id="tmpl001",
        frequency="daily",
        time_of_day="02:00",
    )
    assert result["id"] == "sched001"
    assert result["name"] == "Nightly Build"
    assert result["enabled"] == 1

    # List
    schedules = await repo.list_schedules(db_conn)
    assert len(schedules) == 1
    assert schedules[0]["name"] == "Nightly Build"

    # Get
    s = await repo.get_schedule(db_conn, "sched001")
    assert s is not None
    assert s["project_name"] == "my-project"

    # Get non-existent
    assert await repo.get_schedule(db_conn, "nope") is None

    # Update
    updated = await repo.update_schedule(db_conn, "sched001", enabled=0)
    assert updated["enabled"] == 0
    assert updated["updated_at"] is not None

    # Update with no kwargs returns current
    same = await repo.update_schedule(db_conn, "sched001")
    assert same["enabled"] == 0

    # Delete
    deleted = await repo.delete_schedule(db_conn, "sched001")
    assert deleted is True

    # Delete non-existent
    deleted2 = await repo.delete_schedule(db_conn, "sched001")
    assert deleted2 is False

    # List empty
    schedules = await repo.list_schedules(db_conn)
    assert len(schedules) == 0
