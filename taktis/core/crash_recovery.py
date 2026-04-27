"""Crash recovery — stale task detection and interrupted work reporting.

Extracted from :class:`ExecutionService` to reduce its size and isolate
the recovery concern.  Called once during :meth:`Taktis.initialize`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from taktis import repository as repo
from taktis.exceptions import SchedulerError

logger = logging.getLogger(__name__)


async def recover_stale_tasks(session_factory) -> None:
    """Recover tasks stuck in 'running', 'paused', or 'awaiting_input'.

    Tasks whose process is still alive are skipped.  For the rest we apply
    RECOVERY-01: checkpoint-aware recovery based on the parent phase's
    ``current_wave``.
    """
    async with session_factory() as conn:
        stale_candidates = await repo.get_stale_tasks(conn)
        if not stale_candidates:
            return

        stale_tasks: list[dict] = []
        for row in stale_candidates:
            pid = row["pid"]
            if pid:
                try:
                    os.kill(pid, 0)
                    logger.info(
                        "Task %s (PID %d) still alive — skipping recovery",
                        row["id"], pid,
                    )
                    continue
                except (OSError, ProcessLookupError):
                    pass

            stale_tasks.append({"id": row["id"], "phase_id": row["phase_id"]})

        if not stale_tasks:
            return

        # Group by phase_id to avoid N+1 queries
        phase_ids: set[str] = {
            t["phase_id"] for t in stale_tasks if t["phase_id"]
        }
        phases_by_id: dict[str, dict] = {}
        for phase_id in phase_ids:
            phase = await repo.get_phase_by_id(conn, phase_id)
            if phase is not None:
                phases_by_id[phase_id] = phase

        now = datetime.now(timezone.utc)
        recovered_pending = 0
        recovered_failed = 0
        for task in stale_tasks:
            phase_id = task["phase_id"]
            phase = phases_by_id.get(phase_id) if phase_id else None
            has_checkpoint = (
                phase is not None and phase.get("current_wave") is not None
            )
            if has_checkpoint:
                await repo.update_task(conn, task["id"], status="pending")
                recovered_pending += 1
            else:
                await repo.update_task(
                    conn,
                    task["id"],
                    status="failed",
                    completed_at=now,
                    result_summary="FAILED: Process lost (server restarted)",
                )
                recovered_failed += 1

        if recovered_pending:
            logger.info(
                "RECOVERY-01: Reset %d stale task(s) to 'pending' (checkpoint found)",
                recovered_pending,
            )
        if recovered_failed:
            logger.warning(
                "Recovered %d stale task(s) as 'failed' (no checkpoint)",
                recovered_failed,
            )


async def recover_unprocessed_reviews(
    session_factory,
    project_service: Any,
    scheduler: Any,
) -> None:
    """Re-trigger fix loop for reviews with unprocessed CRITICALs.

    Handles two cases:

    1. A completed phase_review has CRITICALs but no fix task was created.
    2. A phase_review or phase_review_fix task is stuck in 'pending'.
    """
    import json as _json
    from taktis.core.scheduler import WaveScheduler

    async with session_factory() as conn:
        review_rows = await repo.get_completed_reviews_on_complete_phases(conn)

    seen_phases: set[str] = set()
    for row in reversed(review_rows):
        task_id = row["task_id"]
        phase_id = row["phase_id"]
        project_id = row["project_id"]

        if phase_id in seen_phases:
            continue
        seen_phases.add(phase_id)

        async with session_factory() as conn:
            phase_tasks = await repo.get_tasks_by_phase(conn, phase_id)

        # Clean up pending review/fix tasks left from interrupted loop
        pending_loop_tasks = [
            t for t in phase_tasks
            if t.get("task_type") in ("phase_review", "phase_review_fix")
            and t["status"] == "pending"
        ]
        if pending_loop_tasks:
            async with session_factory() as conn:
                for t in pending_loop_tasks:
                    await repo.update_task(conn, t["id"], status="failed")
            logger.info(
                "RECOVERY: Cleaned up %d pending review-loop task(s) for phase %s",
                len(pending_loop_tasks), phase_id,
            )

        completed_reviews = [
            t for t in phase_tasks
            if t.get("task_type") == "phase_review"
            and t["status"] == "completed"
        ]
        if not completed_reviews:
            continue
        last_review_id = completed_reviews[-1]["id"]

        fix_count = sum(
            1 for t in phase_tasks
            if t.get("task_type") == "phase_review_fix"
            and t["status"] == "completed"
        )

        review_text = ""
        async with session_factory() as conn:
            outputs = await repo.get_task_outputs(
                conn, last_review_id, event_types=["result"],
            )
        for o in outputs:
            content = o.get("content")
            if isinstance(content, str):
                try:
                    content = _json.loads(content)
                except (ValueError, TypeError):
                    continue
            if isinstance(content, dict) and content.get("type") == "result":
                review_text = content.get("result", "")

        if not review_text:
            continue

        critical_items = WaveScheduler._extract_critical_items(review_text)
        if not critical_items:
            continue

        async with session_factory() as conn:
            phase = await repo.get_phase_by_id(conn, phase_id)
            project = await repo.get_project_by_id(conn, project_id)
            if not phase or not project:
                continue
            project_dict = await project_service._enrich_project(conn, project)
        attempt = fix_count + 1
        logger.warning(
            "RECOVERY: Phase '%s' has unprocessed review CRITICALs (%d items) "
            "— triggering fix loop (attempt %d)",
            phase.get("name", phase_id), len(critical_items), attempt,
        )
        try:
            await scheduler._fix_and_re_review(
                phase, project_dict, review_text, critical_items,
                attempt=attempt,
            )
        except Exception as exc:
            logger.exception(
                "RECOVERY: Fix loop failed for phase '%s': %s",
                phase.get("name", phase_id),
                SchedulerError(
                    f"Fix loop failed for phase '{phase.get('name', phase_id)}'",
                    cause=exc,
                ),
            )


async def report_interrupted_work(project_service: Any, event_bus: Any) -> None:
    """Log and broadcast any work that was interrupted before shutdown."""
    from taktis.core.events import EVENT_SYSTEM_INTERRUPTED_WORK

    interrupted = await project_service.get_interrupted_work()
    phases = interrupted.get("phases", [])
    pipelines = interrupted.get("pipelines", [])

    if not phases and not pipelines:
        return

    for phase in phases:
        logger.warning(
            "Interrupted phase found: '%s' (id=%s, project='%s') — "
            "resume with: resume phase %s",
            phase["name"],
            phase["id"],
            phase["project_name"],
            phase["id"],
        )

    for pipeline in pipelines:
        logger.warning(
            "Interrupted pipeline found: project='%s' (id=%s) — "
            "resume with: resume pipeline %s",
            pipeline["project_name"],
            pipeline["project_id"],
            pipeline["project_id"],
        )

    await event_bus.publish(EVENT_SYSTEM_INTERRUPTED_WORK, interrupted)
