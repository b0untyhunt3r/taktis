"""Phase review system — spawns reviewer tasks and fix/re-review cycles.

Extracted from :mod:`taktis.core.scheduler` to reduce file size
and isolate the review concern.
"""

from __future__ import annotations

import json as _json
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from taktis import repository as repo

if TYPE_CHECKING:
    from taktis.core.scheduler import WaveScheduler

logger = logging.getLogger(__name__)

_MAX_REVIEW_ATTEMPTS = 3


async def spawn_phase_review(
    scheduler: WaveScheduler,
    phase: dict,
    project: dict,
) -> None:
    """Create and execute a reviewer task for a completed phase.

    After the review completes:
    - Writes REVIEW.md to .taktis/phases/{N}/ for next phase context
    - If CRITICALs found, spawns fix task → re-review cycle
    """
    from taktis.core.prompts import PHASE_REVIEW_PROMPT

    phase_id = phase["id"]
    phase_number = phase["phase_number"]
    phase_name = phase.get("name", "")
    phase_goal = phase.get("goal", "")
    project_name = project.get("name", "")
    working_dir = project.get("working_dir", ".")

    prompt = PHASE_REVIEW_PROMPT.format(
        phase_number=phase_number,
        phase_name=phase_name,
        phase_goal=phase_goal or "No specific goal defined.",
        working_dir=working_dir,
    )

    # Resolve reviewer expert by role
    async with scheduler._session_factory() as conn:
        reviewer_expert = await repo.get_expert_by_role(conn, "phase_reviewer")
        if reviewer_expert is None:
            logger.warning("Built-in expert 'reviewer' not found; review task will run without persona")
        expert_id = reviewer_expert["id"] if reviewer_expert else None

        # Create the review task
        task_id = uuid4().hex[:8]
        await repo.create_task(
            conn,
            phase_id=phase_id,
            project_id=project["id"],
            id=task_id,
            name=f"Review Phase {phase_number}",
            prompt=prompt,
            model="opus",
            wave=999,
            task_type="phase_review",
            expert_id=expert_id,
        )

    logger.info(
        "Spawned phase review task %s for phase %d of project '%s'",
        task_id, phase_number, project_name,
    )

    # Execute and wait for completion
    await scheduler.execute_task(task_id, project)
    statuses = await scheduler._wait_for_tasks([task_id])

    # If the review task itself crashed, skip CRITICAL extraction
    if statuses.get(task_id) == "failed":
        logger.warning(
            "Phase %d review task %s failed — skipping review (phase stays complete)",
            phase_number, task_id,
        )
        return

    # Get full review result from DB (result_summary is truncated to 2000 chars)
    review_text = _get_result_text(scheduler, task_id)
    review_text = await review_text

    # Write REVIEW.md for next phase context
    if review_text:
        from taktis.core.context import async_write_phase_review
        await async_write_phase_review(working_dir, phase_number, review_text)

    # Check for CRITICALs — parse the CRITICAL section
    critical_items = extract_critical_items(review_text)

    if critical_items:
        logger.warning(
            "Phase %d review found %d CRITICAL issue(s) — spawning fix task",
            phase_number, len(critical_items),
        )
        await _fix_and_re_review(
            scheduler, phase, project, review_text, critical_items, attempt=1,
        )
    else:
        logger.info("Phase %d review passed (no CRITICALs)", phase_number)


async def _get_result_text(scheduler: WaveScheduler, task_id: str) -> str:
    """Extract the result text from a task's output events."""
    review_text = ""
    async with scheduler._session_factory() as conn:
        outputs = await repo.get_task_outputs(
            conn, task_id, event_types=["result"],
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
    return review_text


async def _fix_and_re_review(
    scheduler: WaveScheduler,
    phase: dict,
    project: dict,
    review_text: str,
    critical_items: list[str],
    attempt: int,
) -> None:
    """Create a fix task for CRITICALs, execute it, then re-review.

    Retries up to _MAX_REVIEW_ATTEMPTS. If CRITICALs persist after max
    attempts, marks the phase as failed.
    """
    phase_id = phase["id"]
    phase_number = phase["phase_number"]
    phase_name = phase.get("name", "")
    working_dir = project.get("working_dir", ".")

    if attempt > _MAX_REVIEW_ATTEMPTS:
        logger.warning(
            "Phase %d still has CRITICALs after %d review attempts — marking as failed",
            phase_number, _MAX_REVIEW_ATTEMPTS,
        )
        async with scheduler._session_factory() as conn:
            await repo.update_phase(conn, phase_id, status="failed")
        return

    # Build fix prompt with the critical issues
    from taktis.core.prompts import PHASE_REVIEW_FIX_PROMPT
    criticals_text = "\n".join(f"- {item}" for item in critical_items)
    phase_goal = phase.get("goal", "")
    fix_prompt = PHASE_REVIEW_FIX_PROMPT.format(
        phase_number=phase_number,
        phase_name=phase_name,
        phase_goal=phase_goal or "No specific goal defined.",
        working_dir=working_dir,
        critical_issues=criticals_text,
        review_text=review_text,
    )

    # Create and run the fix task
    fix_task_id = uuid4().hex[:8]
    async with scheduler._session_factory() as conn:
        impl_expert = await repo.get_expert_by_role(conn, "phase_fixer")
        if impl_expert is None:
            logger.warning("Built-in expert 'implementer' not found; fix task will run without persona")
        expert_id = impl_expert["id"] if impl_expert else None

        await repo.create_task(
            conn,
            phase_id=phase_id,
            project_id=project["id"],
            id=fix_task_id,
            name=f"Fix CRITICALs (attempt {attempt})",
            prompt=fix_prompt,
            wave=999,
            task_type="phase_review_fix",
            expert_id=expert_id,
        )

    logger.info(
        "Spawned fix task %s for phase %d (attempt %d)",
        fix_task_id, phase_number, attempt,
    )

    await scheduler.execute_task(fix_task_id, project)
    fix_statuses = await scheduler._wait_for_tasks([fix_task_id])

    if fix_statuses.get(fix_task_id) == "failed":
        logger.warning(
            "Phase %d fix task %s failed (attempt %d)",
            phase["phase_number"], fix_task_id, attempt,
        )
        # Still attempt re-review — the fix may have partially worked

    # Re-review after fix
    logger.info("Re-reviewing phase %d after fix (attempt %d)", phase_number, attempt)
    await _re_review_phase(scheduler, phase, project, attempt, prior_critical_items=critical_items)


async def _re_review_phase(
    scheduler: WaveScheduler,
    phase: dict,
    project: dict,
    attempt: int,
    prior_critical_items: list[str] | None = None,
) -> None:
    """Run another review cycle after a fix attempt."""
    from taktis.core.prompts import PHASE_REVIEW_PROMPT
    from taktis.core.context import async_write_phase_review

    phase_id = phase["id"]
    phase_number = phase["phase_number"]
    phase_name = phase.get("name", "")
    phase_goal = phase.get("goal", "")
    working_dir = project.get("working_dir", ".")

    base_prompt = PHASE_REVIEW_PROMPT.format(
        phase_number=phase_number,
        phase_name=phase_name,
        phase_goal=phase_goal or "No specific goal defined.",
        working_dir=working_dir,
    )

    # Add prior-fix context so the reviewer knows what was attempted
    if prior_critical_items:
        prior_list = "\n".join(f"- {item}" for item in prior_critical_items)
        prompt = (
            f"This is re-review attempt {attempt + 1} of {_MAX_REVIEW_ATTEMPTS}.\n"
            f"A fix task was run to address these CRITICALs from the prior review:\n"
            f"{prior_list}\n\n"
            f"Focus on:\n"
            f"1. Verify each prior CRITICAL listed above is actually resolved\n"
            f"2. Check for regressions introduced by the fixes\n"
            f"3. Report any NEW issues not in the prior list\n\n"
            f"{base_prompt}"
        )
    else:
        prompt = base_prompt

    # Create and run re-review task
    review_task_id = uuid4().hex[:8]
    async with scheduler._session_factory() as conn:
        reviewer_expert = await repo.get_expert_by_role(conn, "phase_reviewer")
        if reviewer_expert is None:
            logger.warning("Built-in expert 'reviewer' not found; re-review task will run without persona")
        expert_id = reviewer_expert["id"] if reviewer_expert else None

        await repo.create_task(
            conn,
            phase_id=phase_id,
            project_id=project["id"],
            id=review_task_id,
            name=f"Re-review Phase {phase_number} (attempt {attempt + 1})",
            prompt=prompt,
            model="opus",
            wave=999,
            task_type="phase_review",
            expert_id=expert_id,
        )

    await scheduler.execute_task(review_task_id, project)
    statuses = await scheduler._wait_for_tasks([review_task_id])

    if statuses.get(review_task_id) == "failed":
        logger.warning(
            "Phase %d re-review task %s failed (attempt %d) — treating as unresolved",
            phase_number, review_task_id, attempt + 1,
        )
        # Mark phase failed since we can't confirm CRITICALs are resolved
        async with scheduler._session_factory() as conn:
            await repo.update_phase(conn, phase_id, status="failed")
        return

    # Get full review result
    review_text = await _get_result_text(scheduler, review_task_id)

    if review_text:
        await async_write_phase_review(working_dir, phase_number, review_text)

    # Check again
    critical_items = extract_critical_items(review_text)
    if critical_items:
        await _fix_and_re_review(
            scheduler, phase, project, review_text, critical_items, attempt=attempt + 1,
        )
    else:
        # Explicitly ensure phase is complete (guards against races with
        # _on_complete sibling checks or prior failed state)
        async with scheduler._session_factory() as conn:
            await repo.update_phase(conn, phase_id, status="complete")
        logger.info(
            "Phase %d review passed on attempt %d (no CRITICALs)",
            phase_number, attempt + 1,
        )


def extract_critical_items(review_text: str) -> list[str]:
    """Parse the CRITICAL section from a review and return non-empty items.

    Returns an empty list if no CRITICALs found or the section says "None found".
    """
    lines = review_text.split("\n")
    in_critical = False
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        lower_line = line.lower()
        # Start CRITICAL section (case-insensitive)
        if "critical" in lower_line and "must fix" in lower_line:
            in_critical = True
            continue
        # End on any heading or horizontal rule
        if in_critical and (stripped.startswith("#") or stripped.startswith("---")):
            in_critical = False
            continue
        if in_critical and stripped and not stripped.startswith("#"):
            lower = stripped.lower()
            if "none found" in lower or lower == "none" or "n/a" in lower:
                # "None found" means no CRITICALs — stop the section
                in_critical = False
                continue
            items.append(stripped)
    return items
