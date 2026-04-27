---
title: Taktis Vault — Home
tags: [index, moc]
---

# Taktis — Multi-Agent Pipeline Engine

Taktis is a Python orchestration engine that runs multi-agent workflows on top of the **Claude Agent SDK**. Projects are decomposed into **phases**, each phase into a **wave-scheduled DAG of tasks**, and every task is executed by an [[SDKProcess]] wrapping `query()` or `ClaudeSDKClient`. Pipelines are authored visually as **Drawflow templates** and executed by the [[GraphExecutor]].

Start at [[Architecture Overview]] for the big picture, then drill into whichever layer you need.

## Map of Content

### Architecture
- [[Architecture Overview]] — 10,000ft view of projects → phases → waves → tasks
- [[Crash Recovery]] — startup recovery for orphaned tasks, wave checkpoints
- [[Glossary]] — terms and acronyms
- [[Recipes]] — catalog of scheduled-pipeline recipes worth forking

### Core runtime
- [[WaveScheduler]] — `taktis/core/scheduler.py`
- [[GraphExecutor]] — `taktis/core/graph_executor.py`
- [[SDKProcess]] — `taktis/core/sdk_process.py`
- [[ProcessManager]] — `taktis/core/manager.py`
- [[EventBus]] — `taktis/core/events.py`
- [[Planner]] — `taktis/core/planner.py`
- [[Phase Review]] — `taktis/core/phase_review.py`
- [[Cron Scheduler]] — `taktis/core/cron_scheduler.py`
- [[Engine and Services]] — `engine.py`, `project_service.py`, `execution_service.py`

### Pipeline system
- [[Node Types]] — the 14 pipeline node types
- [[Pipeline Factory]] — `spec_to_drawflow` and schema validation
- [[Agent Templates]] — DB-backed prompt templates
- [[AskUserQuestion Flow]] — radio-button questions mid-task

### Data layer
- [[Database Schema]] — tables created in `init_db`
- [[Repository]] — parameterized CRUD
- [[Models]] — dataclasses + status enums
- [[Config]] — `Settings` + env-var precedence
- [[Migrations]] — idempotent schema upgrades

### Context & review
- [[Context Chain]] — `.taktis/` files and how they flow between tasks
- [[Context Budget]] — P0–P4 priority levels
- [[Phase Review]] — auto-review + fix cycles

### Web layer
- [[Web App]] — Starlette routes and lifespan
- [[SSE Architecture]] — three streams, catch-up, reconnect

### Experts & errors
- [[Expert System]] — 184 experts loaded from `.md`
- [[Exception Hierarchy]] — typed errors
- [[Six Rules]] — error-handling rules from `docs/ERROR_HANDLING.md`

## Conventions used in this vault

- File references use `path:line` so clicking them in an IDE jumps to source.
- `[[Note Name]]` wikilinks point to other notes in this vault.
- Each note has frontmatter tags for Obsidian filtering.
- Facts are cited to specific files/lines; prose is kept thin.
