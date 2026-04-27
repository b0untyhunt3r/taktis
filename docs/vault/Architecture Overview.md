---
title: Architecture Overview
tags: [architecture, overview]
---

# Architecture Overview

Taktis runs long-form, multi-agent work as a hierarchy:

```
Project ──┬── Phase 1 ──┬── Wave 1 ── Task A ── Task B   (parallel inside wave)
          │             ├── Wave 2 ── Task C             (waves serial)
          │             └── Wave 3 ── Task D
          ├── Phase 2 ── …
          └── Phase N ── …
```

Every task is one Claude Agent SDK invocation. Phases are either **ordinary phases** (scheduled directly by [[WaveScheduler]]) or **designer phases** whose execution is driven by a [[GraphExecutor]] running a Drawflow template.

## Layers

| Layer | Key module | Role |
|---|---|---|
| Entry point | `run.py` | Launch Starlette web server |
| Web UI | [[Web App]], [[SSE Architecture]] | Routes, live updates |
| Facade | [[Engine and Services]] | Thin `Taktis` class over services |
| Services | `project_service.py`, `execution_service.py` | CRUD, run/stop, recovery |
| Runtime | [[WaveScheduler]], [[GraphExecutor]], [[ProcessManager]] | DAG execution, concurrency |
| Agent SDK | [[SDKProcess]] | Wraps `query()` / `ClaudeSDKClient` |
| Events | [[EventBus]] | In-process async pub/sub |
| Data | [[Repository]], [[Models]], [[Database Schema]] | aiosqlite + dataclasses |
| Context | [[Context Chain]], [[Context Budget]] | `.taktis/` files with priority |
| Errors | [[Exception Hierarchy]], [[Six Rules]] | Typed errors + policy |

## Async throughout

Everything is `asyncio` — never blocking calls. [[EventBus]] is per-subscriber `asyncio.Queue` with auto-stale sweep after 60s (`taktis/core/events.py:329`). [[ProcessManager]] caps concurrent tasks with a semaphore (default 15, see [[Config]]).

## Designer vs ordinary phases

An ordinary phase's tasks are explicit rows created by [[Planner]] (see `apply_plan()`). A designer phase is flagged via `context_config.designer_phase = True` and created by [[GraphExecutor]] from a Drawflow template — the scheduler skips phase-status updates for these (`scheduler.py:813`) because the graph executor owns the lifecycle.

## Where data lives

- **SQLite DB** (`taktis.db`): projects, phases, tasks, experts, templates, schedules, event log. See [[Database Schema]].
- **`.taktis/` in working directory**: human-readable context artifacts (`PROJECT.md`, `TASK_CONTEXT_{id}.md`, `RESULT_{id}.md`, per-phase `REVIEW.md`). See [[Context Chain]].
- **Built-in templates**: `taktis/agent_templates/*.md` for [[Agent Templates]], `taktis/defaults/pipeline_templates/*.json` for pipelines, `taktis/experts/*.md` for [[Expert System]].

## Control flow on "run project"

1. User POSTs `/api/projects/{id}/run` → [[Engine and Services]] `run_project()`
2. For each not-done phase, [[WaveScheduler]] `execute_phase()` runs
3. For designer phases, [[GraphExecutor]] `execute_multi()` takes over
4. Each task spins up an [[SDKProcess]] through [[ProcessManager]]
5. Task lifecycle events flow through [[EventBus]] → [[SSE Architecture]] → browser
6. On phase completion, optional [[Phase Review]] spawns reviewer+fixer tasks

## Crash resilience

Wave checkpoints are persisted per phase (`current_wave` column). On startup, [[Crash Recovery]] resets running tasks back to `pending` if their phase has a checkpoint, else marks them `failed`.

## Related

[[WaveScheduler]] · [[GraphExecutor]] · [[SDKProcess]] · [[Crash Recovery]] · [[Context Chain]]
