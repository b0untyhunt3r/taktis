---
title: Repository
tags: [data, crud]
---

# Repository

File: `taktis/repository.py`

Raw aiosqlite CRUD. Every query is parameterized; errors are mapped to the typed [[Exception Hierarchy]].

## Entities

| Entity | Lines | Shape |
|---|---|---|
| Project | 126–244 | create, get_by_name, get_by_id, list, list_summary, update, delete |
| ProjectState | 251–310 | create, get, update |
| Phase | 317–433 | create, get (by `project_id + phase_number`), get_by_id, list, update, delete, `update_current_wave` |
| Task | 463–599 | create, get, list (optional phase filter), update, get_by_ids |
| TaskOutput | 606–728 | create, create_batch, delete, `purge_old`, get (tail + event_type filter) |
| Expert | 735–852 | create, get_by_name, get_by_id, get_by_role, get_default, list, update, update_id, delete |
| PipelineTemplate | 980–1064 | CRUD + factory_reset |
| AgentTemplate | 1071–1197 | CRUD + update_id (slug-keyed) |
| Schedule | 1203–1277 | CRUD |

## Error mapping

`_execute()` at `repository.py:53–120`:

- Catches `aiosqlite.Error`
- UNIQUE constraint violations → `DuplicateError` with schema info
- Logs only query preview + param count (never values) — keeps secrets out of logs

## Notable query patterns

- **`list_projects_summary`** (`188–217`) — single query joining `projects` → `project_states` → `phases` → `tasks`, aggregating phase count, task counts by status, total cost
- **`list_tasks`** (`527–550`) — LEFT JOIN tasks → phases, decorates with `phase_name` + `phase_number`
- **`get_active_tasks_all_projects`** (`553–568`) — running/awaiting_input/pending across all projects
- **`purge_old_task_outputs`** (`615–647`) — nested subquery to keep the last N outputs per task per project
- **`get_task_outputs`** (`700–728`) — subquery with LIMIT safeguard; tail parameter or 10k default
- **`get_interrupted_phases`** (`939–956`) — `in_progress` phases with `current_wave` set but no live tasks — feeds [[Crash Recovery]]
- **`get_expert_names_by_ids`** (`959–972`) — bulk `{expert_id: name}` lookup for enrichment

## JSON field handling

`repository.py:37–40, 470–510` — auto-serialize multi-valued fields (`depends_on`, `env_vars`, `success_criteria`, `auto_variables`, etc.) on create/update. [[Models]] handles the inverse on read.

## Related

[[Database Schema]] · [[Models]] · [[Exception Hierarchy]]
