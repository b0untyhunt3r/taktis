---
title: ProcessManager
tags: [core, runtime, concurrency]
---

# ProcessManager

File: `taktis/core/manager.py`

Registry of live [[SDKProcess]] instances + concurrency gate. One instance per Taktis engine (`manager.py:24–30`).

## Concurrency cap

`asyncio.Semaphore(max_concurrent)` at `manager.py:39`. Default max is **15** (`manager.py:35`), configurable via `Settings.max_concurrent_tasks` (see [[Config]]). Every `start_task()` acquires one permit before the task runs; the monitor releases it in `finally` (`manager.py:369`).

## Public methods

| Method | Location | Purpose |
|---|---|---|
| `start_task(task_id, ...)` | `manager.py:52` | Acquire semaphore, spawn SDKProcess, register, publish `EVENT_TASK_STARTED`, start monitor |
| `stop_task(task_id)` | `manager.py:127` | Cancel monitor, call `process.stop()`, optionally publish failure |
| `continue_task(task_id, input)` | `manager.py:231` | Resume interactive task — acquire semaphore, call `start_continuation()`, restart monitor |
| `_monitor_output()` | `manager.py:304` | Background task: read `process.stream_output()`, publish to [[EventBus]], invoke callbacks, release semaphore |

## Event flow

Every output event from [[SDKProcess]] is forwarded to [[EventBus]] by `_monitor_output`. Event types published include `EVENT_TASK_OUTPUT`, `EVENT_TASK_CHECKPOINT`, `EVENT_TASK_COMPLETED`, `EVENT_TASK_FAILED` — see [[EventBus]] for the full list.

## Related

[[SDKProcess]] · [[EventBus]] · [[WaveScheduler]]
