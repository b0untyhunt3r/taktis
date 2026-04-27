---
title: Migrations
tags: [data, schema, migrations]
---

# Migrations

File: `taktis/db.py` (lines 207–310)

No migration framework. All schema upgrades live inline in `init_db()` using a single idempotent pattern:

```python
# pseudo
cols = await (PRAGMA table_info(<table>))
if "<new_col>" not in cols:
    try:
        await db.execute("ALTER TABLE <table> ADD COLUMN <new_col> ... DEFAULT ...")
    except aiosqlite.OperationalError as e:
        # duplicate column — log and continue
```

Duplicate-column errors are caught and logged, not re-raised. This makes migrations safe to re-run.

## Column additions

| Table | Column | Line | Notes |
|---|---|---|---|
| `projects` | `planning_options` TEXT | 213–219 | JSON blob: phase_review flag, context_budget_chars, etc. |
| `tasks` | `input_tokens` INT | 220–233 | |
| `tasks` | `output_tokens` INT | 220–233 | |
| `tasks` | `num_turns` INT | 220–233 | |
| `tasks` | `retry_count` INT | 220–233 | |
| `tasks` | `peak_input_tokens` INT | 220–233 | |
| `phases` | `current_wave` INT | 235–255 | Wave checkpoint — drives [[Crash Recovery]] |
| `phases` | `updated_at` TEXT | 235–255 | |
| `phases` | `context_config` TEXT | 235–255 | JSON: `designer_phase`, cross-phase context, phase_review flag |
| `experts` | `role` TEXT | 257–273 | |
| `experts` | `task_type` TEXT | 257–273 | |
| `experts` | `pipeline_internal` INT | 257–273 | Hides from UI — see [[Expert System]] |
| `experts` | `is_default` INT | 257–273 | |
| `tasks` | `retry_policy` TEXT | 275–285 | JSON: max_attempts, patterns, backoff |
| `tasks` | `context_manifest` TEXT | 275–285 | JSON: filename → priority |

## Table drops

`db.py:290–302` — removes deprecated tables:

- `context_entries_fts`
- `context_reads`
- `context_entries`

These supported the "pull-context" feature removed in April 2026 (see commit `0dabf51`).

## Template sync migrations

`_seed_pipeline_templates()` (`db.py:359–448`):

- Reads from `taktis/defaults/pipeline_templates/*.json`
- Inserts new entries
- Overwrites `is_default=1` entries if content changed
- Leaves `is_default=0` entries (user-created) untouched

A one-shot migration for the "Project Planner" template (`db.py:287–447`, `321–357`) retrofits `retry_transient`, `retry_max_attempts`, `retry_backoff` onto agent nodes inside its `flow_json`.

## Related

[[Database Schema]] · [[Repository]]
