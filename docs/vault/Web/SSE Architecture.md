---
title: SSE Architecture
tags: [web, sse, events]
---

# SSE Architecture

Four independent SSE streams deliver live updates to the browser. All subscribe to [[EventBus]] (except the consult stream, which uses a per-token queue); each filters and formats differently.

## `/events` — global feed (`app.py:1687`)

**Watched events** (`WATCHED_EVENTS` at `app.py:86–94`):
`TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_CHECKPOINT`, `PHASE_STARTED`, `PHASE_COMPLETED`.

**Payload**: server-rendered HTML fragments showing task status, timestamps, cost (`1658–1673`).

**Used by**: dashboard event feed.

## `/events/task/{task_id}` — live output (`app.py:2362`)

**Filter**: relay tasks filter by `task_id` before merge (`2312–2320`).

**Events**: `EVENT_TASK_OUTPUT`, `EVENT_TASK_STARTED`, `EVENT_TASK_COMPLETED`, `EVENT_TASK_FAILED`, `EVENT_TASK_CHECKPOINT` (`2308`).

**Payload**: streaming content blocks (text, tool calls, thinking). HTML fragments built by `_sse_html_for_event` (`1923`).

**Closure**: cancelled on disconnect or `done`/`started` events (`2337–2347`).

## `/events/project/{name}` — element replacement (`app.py:2676`)

**Filter**: matches by `project_name` or `project_id`; per-connection 0.5s cache to avoid redundant DB lookups (`2645–2668`).

**Events**: `PROJECT_SSE_EVENTS` = `TASK_*`, `PHASE_*`, `PIPELINE_PLAN_READY`, `PIPELINE_GATE_WAITING` (`2357–2366`).

**Payload**: JSON `{element_id: html}` dict — browser replaces matching elements (`2671–2675`). New task rows use `new-task-row-{phase_number}` events.

**Closure**: cancelled if task/project no longer belongs to project (`2678–2682`).

## `/events/consult/{token}` — advisor chat (`app.py:2928`)

Text chunks from Claude advisor streamed as JSON. Session removed on disconnect or token deletion (`2857–2866`, `2884–2891`).

## Keepalive + disconnect detection

All SSE generators:

- `await request.is_disconnected()` before each event (`1649`, `2329`, `2648`)
- 15-second keepalive comments `: keepalive\n\n` to prevent idle close (`1679`, `2341`, `2677`)
- `asyncio.wait_for(..., timeout=15)` on queue reads — never blocks forever
- Unsubscribe from [[EventBus]] in `finally` blocks (`1681–1684`, `2344–2347`, `2678–2682`)

## Per-page strategy

### Dashboard

- Status cards + active tasks: htmx `hx-trigger="load, sse:status-change from:#toast-area, every 30s"` (`dashboard.html:43, 79`)
- Event feed: htmx SSE extension with `sse-connect="/events"` + `sse-swap="message"` (`98–101`)

### Project detail (`project_detail.html:34–128`)

- Vanilla `EventSource` to `/events/project/{name}` (line 37)
- Parses JSON, replaces elements by ID or appends new task rows (44–75)
- **Catch-up mechanism** (89–127): 4 retries at `[800ms, 1.5s, 2.5s, 4s]` (93). If an `in_progress` phase has no visible tasks after all retries, full-page reload (117–119)
- Closes SSE on `beforeunload` (87)

This catches the race where pipeline-generated phases and tasks appear between page render and first SSE event.

### Task detail (`task_detail.html:552–614`)

- **Running tasks**: SSE connects only after history loads (601–604). Inline `<script>` blocks in events are parsed and executed (573–577) to drive typing effect, thinking updates, progress timer. Smart auto-scroll: only scrolls if user is near bottom (583). Closes on `done` and reloads (593–594).
- **Pending / awaiting_input**: status-only SSE reloads on `started`/`done` (704–710).

## Closure strategy

- **Global cleanup** (`base.html:1193–1198`): `beforeunload` → `htmx.remove()` on all SSE-connected elements.
- **Per template**: `window.addEventListener('beforeunload', () => es.close())` on project detail (87) and task detail (597, 708).
- **Server side**: disconnect check every 15s + finally-block unsubscribe.

## Related

[[Web App]] · [[EventBus]] · [[ProcessManager]]
