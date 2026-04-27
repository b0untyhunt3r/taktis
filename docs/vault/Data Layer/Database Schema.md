---
title: Database Schema
tags: [data, schema]
---

# Database Schema

File: `taktis/db.py`

**Engine**: `aiosqlite` (async SQLite). WAL mode + foreign keys on.

```sql
PRAGMA journal_mode=WAL;   -- db.py:210
PRAGMA foreign_keys=ON;    -- db.py:211
```

Connection pooling: async queue-based, default 10 connections (`db.py:481–494`).

## Tables created in `init_db()` (`db.py:39–200`)

| # | Table | Lines | Purpose |
|---|---|---|---|
| 1 | `experts` | 40–52 | AI persona definitions (name, prompt, category, flags) — see [[Expert System]] |
| 2 | `projects` | 54–65 | Top-level work units (name, working_dir, default_model, `planning_options`) |
| 3 | `project_states` | 67–78 | 1:1 with projects; idle/running, decisions, blockers, metrics, last_session |
| 4 | `phases` | 80–97 | Phases within a project; status, wave-aware, dependency graph |
| 5 | `tasks` | 99–136 | Executable units; status, wave, cost/tokens, expert, retry policy |
| 6 | `task_outputs` | 138–147 | Event log per task; timestamped with `event_type` |
| 7 | `task_templates` | 149–159 | Reusable task templates scoped to projects |
| 8 | `pipeline_templates` | 161–169 | Named Drawflow pipelines; `is_default`/`is_builtin` flag auto-sync |
| 9 | `agent_templates` | 171–182 | Prompt templates for `agent` nodes — see [[Agent Templates]] |
| 10 | `schedules` | 184–198 | Cron-like scheduling for pipeline runs |

## Migrations (idempotent)

`db.py:207–310`. Pattern: `PRAGMA table_info` → `ALTER TABLE ADD COLUMN` if missing, catch duplicate column errors. See [[Migrations]] for the full list.

## Template seeding

`_seed_pipeline_templates()` at `db.py:359–448`, called from `init_db()`:

- New default templates → inserted
- Existing `is_default=1` → **overwritten** if JSON content differs
- Existing `is_default=0` (user-created) → **never touched**

Same pattern applies to [[Agent Templates]] and [[Expert System]].

## Related

[[Repository]] · [[Models]] · [[Migrations]] · [[Config]]
