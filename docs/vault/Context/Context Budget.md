---
title: Context Budget
tags: [context, priorities]
---

# Context Budget

File: `taktis/core/context.py` (class `ContextBudget`, lines 54–108)

Tasks can easily overflow the model's context window. `ContextBudget` packs context sections by **priority** until a character budget is hit, then trims or summarizes the rest.

## Priority levels (`context.py:54–59`)

| Level | Value | Meaning |
|---|---|---|
| `P0_MUST` | 0 | Critical — always included in full |
| `P1_HIGH` | 1 | High — upstream results, immediate deps |
| `P2_MEDIUM` | 2 | Cross-phase shared files (via manifest) |
| `P3_LOW` | 3 | DB state summary, prior reviews |
| `P4_TRIM` | 4 | Trim-first — prior `RESULT_*.md` files |

## Usage

```python
budget = ContextBudget(max_chars=150_000)
budget.add(ContextBudget.P0_MUST, "project", text, path="PROJECT.md")
budget.add(ContextBudget.P1_HIGH, "upstream", upstream_text)
...
assembled, manifest = budget.assemble()
```

- `add()` (`65–72`) appends a section with priority, tag, content, source path, optional summary.
- `assemble()` (`74–108`) sorts by priority and greedily packs into the budget. Manifest records per-section inclusion mode: `full`, `summary`, `truncated`, `omitted`.

## Default budget

150,000 characters. Configurable per-project via `planning_options.context_budget_chars` ([[Config]] / [[Models]]).

## Who assembles for who

- **Ordinary phases** — [[WaveScheduler]] calls `async_get_phase_context()` which builds a budget with:
  - **P0** — `PROJECT.md`, `PLAN.md`
  - **P2** — `REQUIREMENTS.md`, `ROADMAP.md`
  - **P3** — DB state summary, `phases/*/REVIEW.md`, research
  - **P4** — prior `RESULT_*.md` files (summary first; full if budget permits)
- **Designer phases** — [[GraphExecutor]] builds a simpler budget at `create_task` time:
  - **P0** — `PROJECT.md` (`graph_executor.py:1125–1128`)
  - **P1** — upstream results (`1131–1154`)
  - **P2** — cross-phase shared files via manifest (`1166–1172`)
  - **P3** — DB state summary (`1175–1176`)

## Context manifest integration

`_build_budgeted_context()` reads `.taktis/context_manifest.json` (`context.py:599`) to discover files that prior phases opted-in via `file_writer` nodes. This is the cross-phase context channel — see [[Context Chain]].

## Related

[[Context Chain]] · [[WaveScheduler]] · [[GraphExecutor]]
