---
title: Phase Review
tags: [core, runtime, review]
---

# Phase Review

File: `taktis/core/phase_review.py`

Auto-reviews a phase after it completes. If CRITICALs are found, spawns a fix task, then re-reviews — up to **3 attempts total** (`phase_review.py:21`, `_MAX_REVIEW_ATTEMPTS`).

## Trigger

[[WaveScheduler]] calls `spawn_phase_review()` (`phase_review.py:24`) after successful phase completion **if** the project's `planning_options.phase_review` is true or the phase's `context_config.phase_review` is set (`scheduler.py:432–443`).

## Review task

Created with (`phase_review.py:58–71`):

- `task_type = "phase_review"` (`69`)
- `wave = 999` — ensures it runs after all normal phase tasks (`68`)
- Uses the `phase_reviewer` expert role (resolved from DB) — [[Expert System]] supplies `reviewer-general` as the default backing persona.

The prompt (`prompts.py:430`) asks the reviewer to inspect deliverables with `Read`/`Glob`/`Grep`, produce `CRITICAL` / `WARNING` / `NIT` sections, and save `REVIEW.md` under `.taktis/phases/{N}/`.

## CRITICAL extraction + fix loop

`phase_review.py:99–210`:

1. `extract_critical_items()` (`310–336`) parses the review for a "critical must fix" section.
2. If CRITICALs exist, `_fix_and_re_review()` (`107`) creates a fix task:
   - `task_type = "phase_review_fix"` (`189`)
   - Expert role `phase_fixer` (backed by `implementer`-style persona, `176`)
3. After the fix task completes, re-run review. Loop caps at `_MAX_REVIEW_ATTEMPTS = 3` (`151`).
4. If CRITICALs persist after 3 attempts, **phase status set to `failed`** (`157`).

## `REVIEW.md` flow

- Written to `.taktis/phases/{N}/REVIEW.md` after every review cycle (`phase_review.py:97, 291`).
- Invalidates context cache (`context.py:474`).
- Flows forward to the next phase as **P3_LOW** context via `get_phase_context()` (`context.py:623–633`). This is how warnings from earlier phases reach later phases.

## SKIP_TYPES

These task types are in [[WaveScheduler]]'s `SKIP_TASK_TYPES` and won't be re-run on phase re-execution:

- `phase_review`
- `phase_review_fix`
- `discuss_task`
- `task_researcher`

(`models.py:68–71`)

## Related

[[WaveScheduler]] · [[Context Chain]] · [[Expert System]] · [[Models]]
