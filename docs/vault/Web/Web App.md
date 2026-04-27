---
title: Web App
tags: [web, starlette]
---

# Web App

File: `taktis/web/app.py`
Framework: **Starlette** (async ASGI)

Entry point is `run.py` which imports `build_app()` and serves it over uvicorn at `http://localhost:8080`.

## Lifespan (`app.py:143–171`)

Async context manager `_lifespan`:

- **Startup** — initialize Taktis orchestrator, refresh model profiles from API, create `ConsultRegistry`, start the sweep loop.
- **Sweep loop** (`151–162`) — background cleanup of stale advisor sessions; done-callback (Rule 3 from [[Six Rules]]) logs crashes.
- **Shutdown** — cancel sweep, suppress `CancelledError`, shutdown orchestrator, null globals.

Exception handler `_asyncio_exception_handler` (`122`) suppresses SDK cancel-scope noise during cleanup.

## Middleware stack (added bottom-up)

1. `ContentSizeLimitMiddleware` (`2918`) — 2 MB default body cap.
2. `RateLimitMiddleware` (`2945`) — per-IP 60 req/min for mutations, 10 concurrent SSE per IP.
3. `CSRFMiddleware` (`3011`) — double-submit cookie; checks `X-CSRFToken` on state-changing requests.

## Route groups

| Group | Route range lines | Examples |
|---|---|---|
| Pages (GET, full HTML) | 3336–3342, 3391 | `/`, `/projects`, `/projects/{name}`, `/tasks/{id}`, `/experts`, `/admin`, `/pipelines`, `/schedules` |
| Partials (GET, htmx fragments) | 3344–3348 | Status cards, active tasks, project list, task output, task status |
| API (POST/PUT/DELETE, JSON) | 3350–3396 | Project/phase/task CRUD, execution, checkpoints, approvals, pipeline gates, expert management, schedules |
| SSE (GET, streaming) | 3399–3406 | See [[SSE Architecture]] |

## Jinja2 filters

Registered at `app.py:66, 82`:

- `dt` — `_dt_filter` (`50`) — format ISO datetime, default `%Y.%m.%d %H:%M`, falls back to `—`
- `from_json` — `_from_json_filter` (`69`) — parse JSON or return `[]`

## JS / CSS stack (from `base.html`)

| Library | Version | Purpose |
|---|---|---|
| htmx | 2.0.4 | Attribute-driven AJAX |
| htmx-ext-sse | 2.2.2 | Declarative `sse-connect` |
| marked | latest | Markdown → HTML |
| DOMPurify | 3.2.4 | XSS sanitization |
| mermaid | latest | Diagrams |
| highlight.js | latest | Syntax highlighting |
| Drawflow | pinned | Pipeline graph editor |
| Geist (font) | — | Sans + mono |

## Theme

Dual CSS custom-property themes in `base.html:44–114`:

- Light: `[data-theme="light"]` — #fafafa bg
- Dark: `[data-theme="dark"]` — #09090b bg

`localStorage.theme` is applied **before** the DOM renders to prevent FOUC. hljs stylesheet is swapped in tandem.

## Templates

Files in `taktis/web/templates/`:

| File | Purpose |
|---|---|
| `base.html` | Shared layout, theme, JS libs, CSS |
| `dashboard.html` | Status cards + event feed (htmx+SSE) |
| `projects.html` | Project list with create/delete |
| `project_detail.html` | Phases/tasks, [[SSE Architecture]] status updates |
| `project_timeline.html` | Gantt-like phase/task viz |
| `task_detail.html` | Full output, tool approvals, input forms, live SSE |
| `task_output` (partial) | Rendered output blocks |
| `task_status` (partial) | Badge, cost, context meter |
| `experts.html` / `expert_edit.html` | [[Expert System]] CRUD |
| `pipelines.html` | Drawflow pipeline editor |
| `admin.html` | Stats, event bus metrics |
| `schedules.html` | Cron-style schedule list |
| `agent_templates.html` | [[Agent Templates]] management |
| `status_cards.html`, `active_tasks.html`, `project_list.html`, `advisor_widget.html` | Partials |

## Related

[[SSE Architecture]] · [[Engine and Services]] · [[Expert System]]
