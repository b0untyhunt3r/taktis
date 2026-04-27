---
title: Crash Recovery
tags: [runtime, resilience]
---

# Crash Recovery

When the server starts, `ExecutionService._recover_stale_tasks()` (`execution_service.py:115`) delegates to `taktis/core/crash_recovery.py` to bring DB state back in sync with the real world.

## What gets recovered

`recover_stale_tasks()` (`crash_recovery.py:20`) targets tasks whose status is one of:

- `running`
- `paused`
- `awaiting_input`

…but whose SDK process is no longer alive (dead PID, no registered [[SDKProcess]]). These are **orphans from a prior crash**.

## Decision: requeue or fail

Recovery looks at the parent phase's `current_wave` column:

- **Checkpoint present** (`current_wave IS NOT NULL`): task reset to `pending` (`crash_recovery.py:71`). Tagged as **RECOVERY-01** in logs. The next run of the phase picks it up from that wave.
- **No checkpoint**: task marked `failed` with `result_summary = "FAILED: Process lost (server restarted)"` (`crash_recovery.py:75–79`). Safer to surface the failure than silently re-run work whose partial effects are unknown.

## How checkpoints get written

[[WaveScheduler]] writes `phase.current_wave` after **every wave completes successfully** (`scheduler.py:377`). This is the atomic unit — a wave either fully completes and the checkpoint advances, or it doesn't. Tasks within a wave that failed will be requeued, but siblings that completed won't be re-run ([[WaveScheduler]] filters already-completed tasks on reentry).

## Interrupted work report

`_report_interrupted_work()` (`execution_service.py:127`) generates a dashboard alert listing phases that have a `current_wave` set but are `in_progress` with no live tasks — those are the ones the user can resume with one click.

## Manual resume

Users can call `resume_phase()` (`execution_service.py:774`), which:

1. Resets any stuck tasks
2. Skips already-completed tasks
3. Restarts [[WaveScheduler]] `execute_phase(phase_id, project, start_wave=<checkpoint + 1>)`

## Edge cases

- **Reviews interrupted mid-cycle**: `_recover_unprocessed_reviews()` (`execution_service.py:120`) handles [[Phase Review]] tasks whose output was never processed into `REVIEW.md`.
- **Running `initialize()` on a live server**: documented footgun in `CLAUDE.md` — it marks everything `running` as failed via stale recovery. Never do this from diagnostic scripts.

## Related

[[WaveScheduler]] · [[Engine and Services]] · [[Phase Review]]
