---
title: WaveScheduler
tags: [core, runtime, scheduler]
---

# WaveScheduler

File: `taktis/core/scheduler.py`

The wave-based task scheduler with DAG dependency resolution. Tasks within a phase are grouped by wave number; **waves run sequentially, tasks within a wave run concurrently** (`scheduler.py:61–68`).

## Public API

- Class `WaveScheduler` (`scheduler.py:61`)
- Constructor (`scheduler.py:70`) takes `process_manager`, `event_bus`, `state_tracker`, `db_session_factory`, optional `on_task_prep_complete`
- `execute_phase(phase_id, project, start_wave=1)` (`scheduler.py:133`) — main entry
- `execute_task(task_id, project)` (`scheduler.py:487`) — single-task execution
- `auto_assign_waves(tasks)` static method (`scheduler.py:102`) — delegates to `wave_grouper`
- `set_task_prep_callback()` (`scheduler.py:89`)

## `execute_phase` algorithm

At `scheduler.py:133–420`:

1. **Concurrency guard** (`170–175`) — refuse duplicate invocation on the same phase.
2. **Load phase + filter tasks** (`198–214`) — exclude `SKIP_TASK_TYPES` (`discuss`, `research`, `phase_review`, `phase_review_fix`) and already-completed tasks.
3. `publish(EVENT_PHASE_STARTED)` (`220–223`).
4. Group tasks by wave (`226–228`).
5. **Loop over waves in sorted order** (`237–354`):
   - Skip waves below `start_wave` (`244–249`) — used for [[Crash Recovery]] resume.
   - `publish(EVENT_WAVE_STARTED)` (`261–270`).
   - `asyncio.gather(*tasks)` to start all tasks concurrently (`275–276`).
   - Catch escaped exceptions (`278–300`).
   - `_wait_for_tasks()` (`303`) — wait until every task hits a terminal or checkpoint state.
   - `publish(EVENT_WAVE_COMPLETED)` (`305–314`).
   - If any task failed, mark remaining future-wave tasks failed and break (`317–354`).
   - Else **write checkpoint**: `update_phase_current_wave()` (`377`). See [[Crash Recovery]].
6. Finalize phase status and publish `EVENT_PHASE_COMPLETED` or `EVENT_PHASE_FAILED` (`406–420`).

## Context injection

For non-designer phases, `execute_task` (`scheduler.py:585–630`) calls `async_get_phase_context()` with the project's configured budget (default 150K chars, `_get_project_budget` at `scheduler.py:48–58`). The assembled text is written to `.taktis/TASK_CONTEXT_{task_id}.md`. See [[Context Chain]], [[Context Budget]].

For **designer phases** (`scheduler.py:586–599`): [[GraphExecutor]] already assembled context at `create_task` time; the scheduler only writes the file pointer. Detection via `context_config.designer_phase`.

## `_on_complete` branching

At `scheduler.py:707–830`:

- Interactive tasks → `awaiting_input` on success, not `completed` (`716–717`). True completion comes via `continue_task` or the `===CONFIRMED===` poller.
- Non-interactive tasks → `completed` or `failed` based on exit code (`773`).
- For **regular phases**, sibling completion is aggregated (`799–830`): when every sibling is terminal, phase is marked `complete` or `failed`.
- For **designer phases**, skip phase-status update entirely (`813`) — [[GraphExecutor]] owns it.

## Retry handling

`_on_complete` (`scheduler.py:707–771`) reads `retry_policy` JSON from the task row:

- Non-retryable error patterns: `context_overflow`, `usage_limit` (`745–751`).
- Retryable + attempts remaining → recalculate backoff via `_retry_delay()` (`121–127`, modes: `linear`, `exponential`, `none`), requeue with `status=pending` and bump `retry_count` (`762–769`).

Errors are collected in `task_state["_error_events"]` from `_on_output` (`674`) and matched via `_matches_retry_pattern()` (`111–118`).

## Phase review integration

After a successful phase, if `planning_options.phase_review` or per-phase `context_config.phase_review` is set (`scheduler.py:432–443`), `_spawn_phase_review()` (`446`) delegates to `taktis/core/phase_review.py`. See [[Phase Review]].

## Related

[[GraphExecutor]] · [[ProcessManager]] · [[EventBus]] · [[Context Chain]] · [[Phase Review]] · [[Crash Recovery]]
