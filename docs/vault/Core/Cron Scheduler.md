---
title: Cron Scheduler
tags: [core, runtime, scheduling]
---

# Cron Scheduler

File: `taktis/core/cron_scheduler.py`
DB table: `schedules` (see [[Database Schema]])
UI: `/schedules`

Background loop that fires pipeline runs on a recurring basis. Polls every 60 seconds and triggers any schedule whose window has come due.

## Supported frequencies

Not arbitrary cron expressions — a curated set of shapes, chosen so the UI can present them as simple dropdowns.

| Frequency | Fires | Re-fire guard | Lines |
|---|---|---|---|
| `hourly` | first 2 minutes of each hour | 55 min since last run | `cron_scheduler.py:117–121` |
| `daily` | at configured `time_of_day`, first 2 minutes | 23 hours | `cron_scheduler.py:123–126` |
| `weekly` | configured `day_of_week` + `time_of_day` | ~6.9 days | `cron_scheduler.py:128–140` |
| `monthly` | day 1 of month at `time_of_day` | same calendar month | `cron_scheduler.py:142–145` |

`time_of_day` is `HH:MM`. `day_of_week` is one of `monday`…`sunday`.

## Per-schedule state

The `schedules` row carries:

- `project_name` + `template_id` — the pair to execute
- `enabled` flag
- `frequency`, `time_of_day`, `day_of_week`
- `last_run_at` + `last_run_ok` — updated by `_trigger` (`cron_scheduler.py:197–201`, `209–212`)
- `name` — display label

Repo CRUD: see [[Repository]] → Schedule (`repository.py:1203–1277`).

## Lifecycle

- `CronScheduler.start()` (`cron_scheduler.py:50`) — spawns the loop task; attaches a [[Six Rules]] Rule 3 done-callback via `make_done_callback`.
- `_loop()` (`cron_scheduler.py:70–76`) — `while running: _check_schedules(); sleep(60)`. Exceptions logged and loop continues.
- `_check_schedules()` (`cron_scheduler.py:78–90`) — list enabled schedules; for each whose `_should_run` returns `True`, call `_trigger`.
- `_trigger()` (`cron_scheduler.py:149–214`) — load template from DB, call `engine.execute_flow(project_name, flow_json, template_name)`, update `last_run_at` + `last_run_ok`.
- `stop()` (`cron_scheduler.py:60–68`) — cancel task, await cleanup.

## Interactive-node detection

`detect_interactive_nodes(flow_json)` (`cron_scheduler.py:14–38`) returns a list of node names that block headless execution:

- Agent nodes with `data.interactive = true`
- All `human_gate` nodes (always require a human)

The `/schedules` UI uses this to warn — or refuse — when you try to schedule a pipeline that can't run unattended. A nightly pipeline with a `human_gate` in the middle would stall waiting for someone at 3 AM.

## Engine wiring

The `Taktis` facade owns the `CronScheduler` instance. It starts during web app lifespan (see [[Web App]]) and stops during shutdown. The trigger calls back into `engine.execute_flow()`, which creates a new project run the same way a UI click would — no special code path for scheduled runs.

## Related

[[Engine and Services]] · [[Database Schema]] · [[Repository]] · [[GraphExecutor]]
