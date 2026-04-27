---
title: Agent Templates
tags: [pipeline, templates]
---

# Agent Templates

File: `taktis/core/agent_templates.py`
Built-in templates: `taktis/agent_templates/*.md`
DB table: `agent_templates` (see [[Database Schema]])

Agent nodes in `template` mode pull a prompt from this registry instead of using a raw string. Templates support variable substitution and retry logic.

## `AgentTemplateRegistry` (`agent_templates.py:46–202`)

| Method | Line | Purpose |
|---|---|---|
| `load_builtins()` | 59 | Parse `taktis/agent_templates/*.md` frontmatter, insert new, sync `is_builtin=1` |
| `get_template(slug)` | 162 | Fetch by slug |
| `list_templates()` | 167 | All templates |
| `create_template(...)` | 172 | Create custom (`is_builtin=0`) |
| `delete_template(slug)` | 194 | Delete custom — raises `ValueError` for builtins (`201`) |

`load_builtins` compares by slug first, then by ID if provided, and **updates content if changed** — edits to built-in `.md` files propagate on startup.

## DB schema (`db.py:171–182`)

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `slug` | TEXT UNIQUE | Key (e.g. `ROADMAPPER`) |
| `name` | TEXT | Display name |
| `description` | TEXT | |
| `prompt_text` | TEXT | Full prompt body |
| `auto_variables` | JSON | Variables supplied automatically by the engine |
| `internal_variables` | JSON | Internal to the template |
| `is_builtin` | INTEGER | `1` = protected |
| `created_at`, `updated_at` | TEXT | |

Builtin protection: `is_builtin=1` → synced from `.md`, cannot be deleted. `is_builtin=0` → user-created, never auto-synced, deletable.

## Built-in templates

Seven files in `taktis/agent_templates/`:

| File | Slug | Purpose |
|---|---|---|
| `deep-interview.md` | `DEEP_INTERVIEW` | 10–15 questions, complete mental model of the project |
| `simple-interview.md` | `SIMPLE_INTERVIEW` | 3–5 questions, produces structured plan |
| `researcher.md` | `RESEARCHER_CONTEXT` | Domain research wrapper |
| `roadmapper.md` | `ROADMAPPER` | Generates requirements + roadmap + executable plan |
| `roadmapper-revision.md` | `ROADMAPPER_REVISION` | Surgical revision based on [[Expert System]] `plan-checker` feedback |
| `synthesizer.md` | `SYNTHESIZER` | Merges parallel research outputs |
| `plan-checker.md` | `PLAN_CHECKER` | Verifies plan coverage and consistency |

## UI

Management at `/agent-templates`. Pipeline editor populates template dropdowns from DB via API.

## Related

[[Node Types]] · [[Expert System]] · [[Pipeline Factory]]
