---
title: Models
tags: [data, dataclasses]
---

# Models

File: `taktis/models.py`

Plain dataclasses, no ORM. JSON fields auto-deserialize on read via `_model_from_row` (`models.py:115–136`).

## Entities

| Dataclass | Lines | Notes |
|---|---|---|
| `Expert` | 159–176 | Persona (name, prompt, category, builtin/default) |
| `Project` | 183–201 | Top-level container (name, working_dir, default model/permission mode, env vars) |
| `ProjectState` | 209–227 | Execution state — current phase, decisions, blockers, metrics |
| `Phase` | 235–257 | Numbered phase (status, success_criteria, `depends_on_phase_id`, `current_wave`) |
| `Task` | 265–297 | Executable unit (prompt, status, wave, expert, cost/tokens, retry_policy, context_manifest) |
| `TaskOutput` | 305–319 | Event log entry (task_id, timestamp, event_type, JSON content) |
| `TaskTemplate` | 327–345 | Reusable task definition |
| `PipelineTemplate` | 353–369 | Named Drawflow graph (flow_json, is_default) |

## Status enums (`models.py:15–71`)

### `TaskStatus` (`15–23`)

- `pending`
- `running`
- `completed`
- `failed`
- `cancelled`
- `awaiting_input`
- `paused`

Helper sets:
- `TERMINAL_STATUSES = {completed, failed, cancelled}` (`57–59`)
- `DONE_STATUSES = {completed, failed, cancelled, awaiting_input}` (`62–65`) — used in wave-wait logic

### `PhaseStatus` (`26–31`)

- `not_started`
- `in_progress`
- `complete`
- `failed`

### `TaskType` (`35–50`)

- `planner`
- `interviewer`
- `researcher_stack`, `researcher_features`, `researcher_architecture`, `researcher_pitfalls`
- `synthesizer`
- `roadmapper`
- `plan_checker`
- `discuss`
- `task_researcher`
- `phase_review`
- `phase_review_fix`

`SKIP_TASK_TYPES = {discuss, research, phase_review, phase_review_fix}` (`68–71`) — skipped on phase re-run.

## Helpers

- `_utcnow()` (`82`) — tz-aware UTC
- `_full_uuid()` (`74`) — 32 hex chars
- `_short_uuid()` (`78`) — **8 hex chars, used for task IDs**
- `_parse_datetime()` (`101`) — ISO string → datetime
- `_model_from_row()` (`115`) — Row → dataclass, auto JSON + datetime decode
- `_model_to_dict()` (`139`) — dataclass → dict, auto JSON + datetime encode

## Related

[[Database Schema]] · [[Repository]] · [[Config]]
