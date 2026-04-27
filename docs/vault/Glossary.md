---
title: Glossary
tags: [glossary, reference]
---

# Glossary

**Agent SDK** — The `claude-agent-sdk` Python package Taktis builds on. Two entry points: `query()` (one-shot) and `ClaudeSDKClient` (interactive). Wrapped by [[SDKProcess]].

**Agent Template** — A DB-backed prompt template with slug, `auto_variables`, and `internal_variables`. Used by `agent` nodes in `template` mode. See [[Agent Templates]].

**AskUserQuestion** — An SDK tool that Claude calls to present radio-button choices to the user mid-task. See [[AskUserQuestion Flow]].

**Budget (context)** — Character limit (default 150K) for assembled context, partitioned across priority levels P0–P4. See [[Context Budget]].

**Checkpoint (wave)** — `phases.current_wave` column value, updated after each wave completes successfully. Drives resume and [[Crash Recovery]].

**Designer phase** — A phase whose lifecycle is managed by [[GraphExecutor]] rather than [[WaveScheduler]]. Flagged via `context_config.designer_phase = True`.

**Drawflow** — JS graph library used for the visual pipeline editor. Templates are stored as Drawflow-format JSON in the `pipeline_templates` table.

**EventBus** — In-process async pub/sub. Per-subscriber `asyncio.Queue`, auto-stale sweep at 60s. See [[EventBus]].

**Expert** — A persona definition with system prompt loaded from a `.md` file in `taktis/experts/`. See [[Expert System]].

**Fan-out** — Pipeline node that splits an upstream result into items and spawns parallel agent tasks (max 20, 1-based indexing). See [[Node Types]].

**Human gate** — Pipeline node that pauses execution for user approve/reject. See [[Node Types]].

**Internal expert** — Expert with `pipeline_internal: true` in frontmatter. Hidden from manual task-creation UI. All experts in the `internal` category are internal.

**LLM router** — Lightweight LLM classifier node that routes to 2–4 downstream branches. See [[Node Types]].

**Phase review** — Auto-spawned reviewer task (wave 999) after a phase completes, if enabled. CRITICALs → fix task → re-review, max 3 attempts. See [[Phase Review]].

**Pipeline Factory** — Meta-pipeline that interviews the user, designs a new pipeline spec, and persists it as a new template. See [[Pipeline Factory]].

**Pipeline Generator** — Node type that runs `spec_to_drawflow()` on an upstream structured spec and saves the resulting template.

**ProcessManager** — Registry + semaphore for [[SDKProcess]] instances. Default cap: 15 concurrent tasks.

**RESULT_{id}.md** — File in `.taktis/` written after a task completes. Summary variant is read by downstream tasks' context assembly.

**Retry policy** — Per-task JSON: `max_attempts`, `patterns` (regex), `backoff` (`linear|exponential|none`). Enforced by [[WaveScheduler]] `_on_complete`.

**Session ID** — Claude Agent SDK session identifier, persisted on `tasks.session_id` for interactive resumption.

**Source section** — Named block (e.g., `===PLAN===`) produced by `output_parser` and consumed by downstream `file_writer`/`plan_applier`/`pipeline_generator` via ancestor search.

**Task type** — Literal on `tasks.task_type`: `planner`, `interviewer`, `researcher_*`, `synthesizer`, `roadmapper`, `plan_checker`, `discuss`, `task_researcher`, `phase_review`, `phase_review_fix`. See [[Models]].

**Wave** — Integer on `tasks.wave`. Tasks share a wave run concurrently; waves are sequential within a phase.

**===CONFIRMED===** — Sentinel string emitted by interactive tasks (e.g., `discuss_task`) to signal true completion. Detected by `_poll_confirmed()` in `execution_service.py`. See [[AskUserQuestion Flow]].
