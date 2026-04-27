---
title: Context Chain
tags: [context, files]
---

# Context Chain

File: `taktis/core/context.py`

Human-readable context artifacts live in `.taktis/` inside the project's working directory. This is where tasks share information with each other and with you.

## Directory layout

```
<working_dir>/.taktis/
├── PROJECT.md                       # project description (P0)
├── REQUIREMENTS.md                  # from roadmapper (P2)
├── ROADMAP.md                       # from roadmapper (P2)
├── PLAN.md                          # applied plan (P2)
├── context_manifest.json            # filename → priority key
├── phases/
│   └── {N}/
│       ├── PLAN.md                  # per-phase plan
│       └── REVIEW.md                # from phase review (P3)
├── research/                        # research outputs (P3)
├── TASK_CONTEXT_{task_id}.md        # budgeted context for this task
├── RESULT_{task_id}.md              # full task result (written on completion)
└── RESULT_{task_id}.summary.md      # extracted first paragraph
```

`CONTEXT_DIR = ".taktis"` at `context.py:20`.

## Key functions

| Function | Line | Purpose |
|---|---|---|
| `write_task_context_file(task_id, text)` | 796 | Write `TASK_CONTEXT_{task_id}.md` — avoids Windows 8K cmd-line limit |
| `write_task_result(task_id, result)` | 312 | Write `RESULT_{task_id}.md` + `.summary.md` |
| `_extract_summary(result)` | 281 | Pull first meaningful paragraph, skipping LLM preamble |
| `write_phase_review(phase_num, review)` | 464 | Write `.taktis/phases/{N}/REVIEW.md` + invalidate cache |
| `generate_state_summary(...)` | 943 | Build live state string from DB (not from a file) |

## Why `TASK_CONTEXT_{id}.md` and not a system-prompt dump

Windows command lines are capped at ~8192 chars. Rather than stuffing the full context into the system prompt, the scheduler writes it to a file and puts a **pointer** in the system prompt ("read this file for context"). This survives on every OS and scales arbitrarily.

`system_prompt` persists on the `tasks` row for debugging.

## Context manifest

`.taktis/context_manifest.json` maps filename → priority key:

```json
{
  "PROJECT.md": "P0_MUST",
  "REQUIREMENTS.md": "P2_MEDIUM",
  "phases/1/REVIEW.md": "P3_LOW"
}
```

- `update_context_manifest()` (`context.py:527–542`) — adds/updates entries
- `read_context_manifest()` (`context.py:545–555`) — consumed by `_build_budgeted_context()` at `context.py:599`

`file_writer` nodes set their output's priority via the `context_priority` config field (`"none"` means don't track).

## Who writes what

- **Scheduler** ([[WaveScheduler]]) — writes `TASK_CONTEXT_{id}.md` for non-designer phases
- **Graph executor** ([[GraphExecutor]]) — writes `TASK_CONTEXT_{id}.md` for designer phases at `create_task` time
- **Task runtime** — writes `RESULT_{id}.md` + summary on completion
- **Phase review** ([[Phase Review]]) — writes per-phase `REVIEW.md`
- **Planner** ([[Planner]]) — writes per-phase `PLAN.md` and top-level artifacts

## `generate_state_summary()`

Live-built from DB (`context.py:943–967`); there is no STATE.md file. Included as P3 context. See [[Context Budget]].

## Related

[[Context Budget]] · [[WaveScheduler]] · [[GraphExecutor]] · [[Phase Review]]
