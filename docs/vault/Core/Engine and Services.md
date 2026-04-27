---
title: Engine and Services
tags: [core, facade]
---

# Engine and Services

After the Phase 2 decomposition, `taktis/core/engine.py` is a **thin facade**. The real work lives in two services:

- `taktis/core/project_service.py` — CRUD, enrichment, monitoring
- `taktis/core/execution_service.py` — task execution, recovery

## `Taktis` facade (`engine.py`)

Wires components together in `__init__` (`engine.py:66–93`):

- `ProcessManager` ([[ProcessManager]])
- `WaveScheduler` ([[WaveScheduler]])
- `StateTracker` (`state.py`)
- `ExpertRegistry` ([[Expert System]])
- `EventBus` ([[EventBus]])

Public methods are pass-throughs: project/phase/task CRUD (`214–316`) → `ProjectService`; run/stop/resume (`326–384`) → `ExecutionService`.

## `ProjectService` (`project_service.py`)

### CRUD
- `create_project`, `list_projects`, `get_project`, `update_project`, `delete_project` (`131–316`)
- `create_phase`, `list_phases`, `get_phase`, `add_criterion`, `delete_phase` (`321–402`)
- `create_task`, `list_tasks`, `get_active_tasks_all`, `get_task` (`408–536`)
- `discuss_task`, `research_task` (`542–624`) — spawn interactive/research prep tasks

### Enrichment helpers
- `_enrich_project` (`814`) — adds state, phase/task counts, total cost
- `_enrich_phase` (`853`) — batch-loads expert names, enriches all phase tasks
- `_enrich_task` (`899`) — adds expert name + context window

### Experts & profiles
- `list_experts`, `get_expert`, `create_expert`, `update_expert`, `delete_expert` (`753–807`)

### Monitoring
- `get_status` (`654`) — project/task counts, running process count
- `get_interrupted_work` (`668`) — phases needing resume (see [[Crash Recovery]])
- `get_task_output` (`700`)
- `watch_task` (`720`) — stream via [[EventBus]] subscription

`delete_project` only removes the `.taktis/` subdir — **never** the working directory itself.

## `ExecutionService` (`execution_service.py`)

### Task control
- `start_task` (`204`) — reset fields, mark running, launch via [[WaveScheduler]]
- `stop_task` (`258`)
- `continue_task` (`281`) — resume interactive from `session_id`
- `send_input`, `approve_checkpoint`, `deny_tool`, `decide_checkpoint` (`908–982`)

### Phase / project control
- `run_phase` (`685`)
- `run_project` (`714`) — sequentially runs phases, skips completed, stops on first failure
- `resume_phase` (`774`) — resume from wave checkpoint
- `stop_all` (`865`) — optionally filtered by project

### Recovery (delegates to `crash_recovery.py`)
- `_recover_stale_tasks` (`115`) — see [[Crash Recovery]]
- `_recover_unprocessed_reviews` (`120`) — orphaned [[Phase Review]] tasks
- `_report_interrupted_work` (`127`)

### Background task safety
`_make_task_done_callback` (`102`) wires [[Six Rules]] Rule 3 — every `asyncio.create_task` gets a done-callback that publishes `EVENT_SYSTEM_CRASHED` on unhandled failure.

## Related

[[WaveScheduler]] · [[ProcessManager]] · [[Expert System]] · [[Crash Recovery]] · [[Phase Review]]
