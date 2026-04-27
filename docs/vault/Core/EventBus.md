---
title: EventBus
tags: [core, runtime, events]
---

# EventBus

File: `taktis/core/events.py`

In-process async pub/sub using **per-subscriber `asyncio.Queue`** (`events.py:196–202`). Each consumer gets its own queue and drains at its own pace — no shared bottleneck.

## Event constants (`events.py:136–167`)

**Task**
- `EVENT_TASK_STARTED`
- `EVENT_TASK_COMPLETED`
- `EVENT_TASK_FAILED`
- `EVENT_TASK_OUTPUT`
- `EVENT_TASK_CHECKPOINT`

**Phase / wave**
- `EVENT_PHASE_STARTED`
- `EVENT_PHASE_COMPLETED`
- `EVENT_PHASE_FAILED`
- `EVENT_WAVE_STARTED`
- `EVENT_WAVE_COMPLETED`

**System**
- `EVENT_PIPELINE_PLAN_READY`
- `EVENT_SYSTEM_INTERRUPTED_WORK`
- `EVENT_SYSTEM_ERROR`
- `EVENT_PIPELINE_GATE_WAITING`

## Subscribe

`subscribe(event_type)` (`events.py:218`) returns an `asyncio.Queue` with `maxsize=1000` (`227`). Consumers loop on `await queue.get()`.

## Publish

`publish(event_type, data)` (`events.py:234–296`):

1. Wrap payload in envelope (`240–244`).
2. Iterate subscribers; `put_nowait(envelope)`.
3. On full queue: trigger stale sweep (`272`) and optionally emit secondary `EVENT_SYSTEM_ERROR` (`287–294`).

## Auto stale sweep

Queues full for longer than **60 seconds** are auto-removed (`events.py:329–363`). This prevents a silently-dead SSE listener from wedging publishers.

## Who subscribes

- [[SSE Architecture]] — three streams: `/events`, `/events/task/{id}`, `/events/project/{name}`.
- [[WaveScheduler]] — `_wait_for_tasks()` waits on completion events.
- [[GraphExecutor]] — same wait pattern for task completion.
- `state.py` — updates `StateTracker` aggregates.

## Related

[[ProcessManager]] · [[SSE Architecture]] · [[WaveScheduler]]
