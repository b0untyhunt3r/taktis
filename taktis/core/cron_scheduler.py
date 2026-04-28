"""Background scheduler that triggers pipeline executions on a cron basis."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _cron_field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """Check if ``value`` matches a single cron field bounded to [lo, hi].

    Accepts ``*``, comma lists, ranges (``a-b``), and steps (``*/n`` or
    ``a-b/n``). Returns False on any unparseable token so a malformed
    expression simply never fires (caller validates upfront).
    """
    field = field.strip()
    for token in field.split(","):
        token = token.strip()
        if not token:
            continue
        step = 1
        if "/" in token:
            base, _, step_s = token.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                return False
            if step < 1:
                return False
            token = base or "*"
        if token == "*":
            start, end = lo, hi
        elif "-" in token:
            a, _, b = token.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                return False
        else:
            try:
                start = end = int(token)
            except ValueError:
                return False
        if start > end or end < lo or start > hi:
            continue
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """Return True if ``dt`` (naive or tz-aware, minute precision) matches a
    standard 5-field cron expression: ``minute hour day month weekday``.

    Sunday accepts either ``0`` or ``7`` per common cron convention.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    # Python: Mon=0..Sun=6. Cron: Sun=0..Sat=6 (with 7 also = Sunday).
    weekday_cron = (dt.weekday() + 1) % 7
    dow_ok = _cron_field_matches(dow_f, weekday_cron, 0, 6)
    if not dow_ok and weekday_cron == 0:
        dow_ok = _cron_field_matches(dow_f, 7, 0, 7)
    return (
        _cron_field_matches(minute_f, dt.minute, 0, 59)
        and _cron_field_matches(hour_f, dt.hour, 0, 23)
        and _cron_field_matches(dom_f, dt.day, 1, 31)
        and _cron_field_matches(month_f, dt.month, 1, 12)
        and dow_ok
    )


def validate_cron_expr(expr: str) -> str | None:
    """Return None if ``expr`` is a syntactically usable 5-field cron, else
    a short error message describing what's wrong."""
    if not expr or not expr.strip():
        return "Cron expression is empty"
    parts = expr.strip().split()
    if len(parts) != 5:
        return f"Cron expression must have 5 fields (got {len(parts)})"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    labels = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    # Reject if no value in the field matches any candidate value in [lo, hi].
    for field, (lo, hi), label in zip(parts, bounds, labels):
        if not any(_cron_field_matches(field, v, lo, hi) for v in range(lo, hi + 1)):
            return f"Invalid {label} field: '{field}'"
    return None


def detect_interactive_nodes(flow_json: dict | str) -> list[str]:
    """Return list of node names that are interactive (can't run headless).

    Checks for:
    - Agent nodes with ``interactive: true``
    - ``human_gate`` nodes (always require human input)
    """
    if isinstance(flow_json, str):
        flow_json = json.loads(flow_json)

    interactive: list[str] = []
    drawflow = flow_json.get("drawflow", flow_json)
    for module_name, module_data in drawflow.items():
        if not isinstance(module_data, dict) or "data" not in module_data:
            continue
        for node_id, node in module_data["data"].items():
            data = node.get("data", {})
            node_type = node.get("name", "")
            # Interactive agent nodes
            if node_type == "agent" and data.get("interactive"):
                interactive.append(data.get("name", f"Node {node_id}"))
            # Human gates always need human input
            if node_type == "human_gate":
                interactive.append(data.get("name", f"Human Gate {node_id}"))
    return interactive


class CronScheduler:
    """Background loop that checks schedules every 60 seconds and triggers pipelines."""

    def __init__(self, engine: Any, session_factory: Any) -> None:
        self._orch = engine
        self._session_factory = session_factory
        self._task: asyncio.Task | None = None
        self._running = False
        # Schedule IDs whose pipeline is currently executing (cron-fired or
        # manual Run Now). Lets the schedules page render a "Running" state
        # instead of a Run Now button while a trigger is in flight.
        self._running_schedule_ids: set[str] = set()

    def is_schedule_running(self, schedule_id: str) -> bool:
        """Return True if a trigger for this schedule is currently executing."""
        return schedule_id in self._running_schedule_ids

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="cron-scheduler")
        from taktis.core.events import make_done_callback

        self._task.add_done_callback(
            make_done_callback("cron-scheduler", self._orch.event_bus)
        )
        logger.info("Cron scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Cron scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_schedules()
            except Exception:
                logger.exception("Error checking schedules")
            await asyncio.sleep(60)

    async def _check_schedules(self) -> None:
        from taktis import repository as repo

        async with self._session_factory() as conn:
            schedules = await repo.list_schedules(conn)

        now = datetime.now(timezone.utc)

        for schedule in schedules:
            if not schedule.get("enabled"):
                continue
            if self._should_run(schedule, now):
                await self._trigger(schedule, now)

    def _should_run(self, schedule: dict, now: datetime) -> bool:
        """Check if a schedule should fire based on current time."""
        last_run = schedule.get("last_run_at")
        if last_run:
            try:
                last = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                last = None
        else:
            last = None

        freq = schedule.get("frequency", "daily")
        time_of_day = schedule.get("time_of_day", "00:00")
        day_of_week = (schedule.get("day_of_week") or "monday").lower()

        # Parse target hour:minute
        try:
            parts = time_of_day.split(":")
            target_hour = int(parts[0])
            target_minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            target_hour, target_minute = 0, 0

        if freq == "hourly":
            # Run once per hour at minute 0
            if last and (now - last).total_seconds() < 3300:  # 55 min guard
                return False
            return now.minute < 2  # fire in the first 2 minutes of each hour

        elif freq == "daily":
            if last and (now - last).total_seconds() < 82800:  # 23h guard
                return False
            return now.hour == target_hour and now.minute < 2

        elif freq == "weekly":
            if last and (now - last).total_seconds() < 604800 - 7200:  # ~6.9 days guard
                return False
            days = [
                "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday",
            ]
            target_day = days.index(day_of_week) if day_of_week in days else 0
            return (
                now.weekday() == target_day
                and now.hour == target_hour
                and now.minute < 2
            )

        elif freq == "monthly":
            if last and last.month == now.month and last.year == now.year:
                return False
            return now.day == 1 and now.hour == target_hour and now.minute < 2

        elif freq == "cron":
            expr = schedule.get("cron_expr")
            if not expr:
                return False
            # Avoid firing twice within the same minute when the loop ticks
            # are bunched. The 60s loop interval + 50s guard means at most
            # one fire per matching minute even if a tick arrives slightly
            # late or early.
            if last and (now - last).total_seconds() < 50:
                return False
            try:
                return cron_matches(expr, now)
            except Exception:
                return False

        return False

    async def _trigger(self, schedule: dict, now: datetime) -> None:
        """Execute the pipeline for this schedule."""
        from taktis import repository as repo

        schedule_id = schedule["id"]
        project_name = schedule["project_name"]
        template_id = schedule["template_id"]

        logger.info(
            "Cron trigger: schedule '%s' -> project '%s', template '%s'",
            schedule.get("name"), project_name, template_id,
        )

        self._running_schedule_ids.add(schedule_id)
        try:
            # Load template
            async with self._session_factory() as conn:
                templates = await repo.list_pipeline_templates(conn)

            template = None
            for t in templates:
                if t["id"] == template_id:
                    template = t
                    break

            if template is None:
                logger.error(
                    "Schedule '%s': template '%s' not found",
                    schedule.get("name"), template_id,
                )
                async with self._session_factory() as conn:
                    await repo.update_schedule(
                        conn, schedule_id,
                        last_run_at=now.isoformat(), last_run_ok=0,
                    )
                return

            flow_json = (
                json.loads(template["flow_json"])
                if isinstance(template["flow_json"], str)
                else template["flow_json"]
            )

            await self._orch.execute_flow(
                project_name, flow_json,
                template_name=template.get("name", "Scheduled"),
            )

            async with self._session_factory() as conn:
                await repo.update_schedule(
                    conn, schedule_id,
                    last_run_at=now.isoformat(), last_run_ok=1,
                )

            logger.info("Cron trigger succeeded: schedule '%s'", schedule.get("name"))

        except Exception:
            logger.exception("Cron trigger failed: schedule '%s'", schedule.get("name"))
            try:
                async with self._session_factory() as conn:
                    await repo.update_schedule(
                        conn, schedule_id,
                        last_run_at=now.isoformat(), last_run_ok=0,
                    )
            except Exception:
                pass
        finally:
            self._running_schedule_ids.discard(schedule_id)
