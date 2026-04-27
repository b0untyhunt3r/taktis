---
title: Expert System
tags: [experts, personas]
---

# Expert System

File: `taktis/core/experts.py`
Personas: `taktis/experts/*.md` (184 files, flat directory)

Each task can be assigned an **expert**: a system-prompt persona loaded from a markdown file with YAML frontmatter. The `.md` file owns the persona; task prompts carry only data and rules. See the `feedback_expert_persona_pattern` memory for why this split matters.

## `ExpertRegistry` (`experts.py:63–166`)

- `load_builtins()` — iterates `taktis/experts/*.md`, parses YAML frontmatter via `_parse_expert_md`, upserts into DB. Existing experts are skipped unless content changed (`111–150`).
- `format_expert_options()` (`~258`) — filters out `pipeline_internal=True` experts before returning the UI-facing list.

## Frontmatter fields (`experts.py:98–117`)

| Field | Purpose |
|---|---|
| `name` | Expert identifier (kebab-case filename) |
| `id` | Stable expert UUID |
| `description` | Short description |
| `category` | One of the categories listed below |
| `role` | Optional role key (e.g. `phase_reviewer`, `phase_fixer`) |
| `task_type` | Optional preferred task type |
| `pipeline_internal` | Boolean — hides expert from manual task-creation UI |
| `is_default` | Boolean — default expert for its role |

## Origin

- **22 original Taktis experts** — 13 internal + 9 general-purpose. The general-purpose ones are suffixed `-general` (e.g. `architect-general`, `implementer-general`, `reviewer-general`, `security-reviewer-general`, `docs-writer-general`).
- **162 imported** from [agency-agents](https://github.com/msitarzewski/agency-agents) (MIT).

Renaming original engineers with `-general` allowed the specialized imported agents to coexist.

## Categories (and counts)

| Category | Count | Typical role |
|---|---|---|
| `marketing` | 29 | SEO, copywriting, campaign strategy |
| `specialized` | 28 | Legal, medical, finance, niche domains |
| `engineering` | 26 | Backend/frontend/DevOps/AI-ML |
| `game-dev` | 20 | Mechanics, narrative, level design |
| `internal` | 13 | Pipeline orchestration — always `pipeline_internal: true` |
| `testing` | 9 | QA, security testing, a11y |
| `sales` | 8 | Enablement, CRM, accounts |
| `design` | 8 | UI/UX, visual, brand |
| `paid-media` | 7 | Ads, budget, creative testing |
| `support` | 6 | CS, docs, community |
| `spatial-computing` | 6 | AR/VR |
| `project-management` | 6 | Agile, Waterfall, Kanban |
| `product` | 5 | PM, roadmapping |
| `academic` | 5 | Research, thesis |
| `review` | 3 | Code/arch/tech review |
| `implementation` | 3 | Implementation specialists |
| `devops` | 1 | |
| `architecture` | 1 | |

## `pipeline_internal` flag

Effect: filtered out of `format_expert_options()` (`experts.py:258–259`). These are reserved for [[Node Types]] `agent` templates and internal orchestration. All `internal` category experts have this flag set.

## Special internal experts

- `question-asker` — emits `AskUserQuestion` with radio options. See [[AskUserQuestion Flow]].
- `phase_reviewer` / `phase_fixer` — used by [[Phase Review]] (backed by `reviewer-general` / `implementer-general`).

## Persona pattern

From user memory `feedback_expert_persona_pattern`:

> Expert `.md` files own the persona (system prompt); templates carry only data/rules — never duplicate.

This keeps personas versioned in source, while [[Agent Templates]] carry just the parametric prompt body.

## Related

[[Agent Templates]] · [[Phase Review]] · [[AskUserQuestion Flow]] · [[Node Types]]
