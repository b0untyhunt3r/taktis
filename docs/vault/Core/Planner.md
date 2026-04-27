---
title: Planner
tags: [core, runtime, planner]
---

# Planner

File: `taktis/core/planner.py`

Parses structured **plan JSON** produced by roadmapper-style prompts and turns it into DB rows (phases + tasks) via an applier protocol. Also handles wave auto-assignment based on file-write collisions.

## JSON repair — 3-tier fallback

`planner.py:32–130`:

1. **Tier 1** — literal `json.loads` (`123`).
2. **Tier 2** — `_repair_json()` (`32–71`) fixes unescaped `\n`, `\t`, trailing commas (`54–70`).
3. **Tier 3** — `_extract_plan_lenient()` (`74–117`) re-escapes prompt values using a `wave` delimiter heuristic (`82–92`) or truncates oversized prompts (`102–107`).

LLMs emit plausible-but-broken JSON constantly; this ladder recovers most of it.

## `apply_plan()` (`planner.py:273–436`)

Takes an applier (PlanApplier protocol), project name, and plan dict.

1. Validate expert names, auto-fixing mismatches against [[Expert System]] (`299–333`).
2. For each phase spec:
   - `applier.create_phase()` (`366`)
   - `_auto_assign_waves()` on its tasks (`376`)
   - Write `.taktis/phases/{N}/PLAN.md` (`379`)
   - `applier.create_task()` per task (`390`)
3. **Rollback on failure**: delete created phases in reverse order (`400–407`).

## Wave auto-assignment (`planner.py:213–270`)

Detects file-write collisions to parallelize safely:

1. Extract write-file mentions from each task prompt (`229`).
2. Within the same wave, if two tasks write overlapping files, bump one to the next wave (`249`).
3. Tasks without an explicit wave are greedily assigned to the first non-conflicting wave (`255–268`).

This preserves authored waves where possible and auto-resolves collisions only when needed.

## Related

[[WaveScheduler]] · [[Expert System]] · [[Models]]
