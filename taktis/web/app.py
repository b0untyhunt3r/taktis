"""Starlette web application for the Taktis UI."""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route, Mount
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

from taktis import repository as repo
from taktis.core.engine import Taktis
from taktis.db import get_session
from taktis.core.consult import ConsultRegistry
from taktis.core.prompts import CONSULT_TASK_PROMPT, CONSULT_PROJECT_PROMPT
from taktis.exceptions import ConsultError, TaktisError, format_error_for_user
from taktis.core.profiles import get_context_window as _get_context_window
from taktis.core.events import (
    EVENT_TASK_STARTED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_CHECKPOINT,
    EVENT_TASK_OUTPUT,
    EVENT_PHASE_STARTED,
    EVENT_PHASE_COMPLETED,
    EVENT_PIPELINE_GATE_WAITING,
    EVENT_PIPELINE_PLAN_READY,
    make_done_callback,
)

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _dt_filter(value, fmt="%Y.%m.%d %H:%M"):
    """Jinja2 filter: format an ISO datetime string."""
    if not value:
        return "—"
    from datetime import datetime
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return value
    try:
        return value.strftime(fmt)
    except Exception:
        return str(value)


templates.env.filters["dt"] = _dt_filter


def _from_json_filter(value):
    """Jinja2 filter: parse a JSON string into a Python object."""
    if not value:
        return []
    if isinstance(value, (list, dict)):
        return value
    import json as _json
    try:
        return _json.loads(value)
    except (ValueError, TypeError):
        return []


templates.env.filters["from_json"] = _from_json_filter


# Events watched by the global SSE stream
WATCHED_EVENTS = [
    EVENT_TASK_STARTED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_CHECKPOINT,
    EVENT_TASK_OUTPUT,
    EVENT_PHASE_STARTED,
    EVENT_PHASE_COMPLETED,
]

# Shared Taktis engine instance -- set during lifespan
orch: Taktis | None = None

# Shared consult registry -- set during lifespan
_consult_registry: ConsultRegistry | None = None


# ======================================================================
# SSE relay done callback (Rule 3 — all create_task calls need one)
# ======================================================================


def _on_relay_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Log if an SSE relay task crashes unexpectedly."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("SSE relay task %s crashed: %s", task.get_name(), exc, exc_info=exc)


# ======================================================================
# Lifespan
# ======================================================================


def _asyncio_exception_handler(loop: Any, context: dict) -> None:
    """Custom asyncio exception handler to filter SDK cancel-scope noise.

    When we break out of sdk_query() after receiving the result, the SDK's
    async generator cleanup tries to exit an anyio cancel scope from a
    different task, producing a noisy RuntimeError.  We log a single line
    instead of the full traceback.
    """
    exc = context.get("exception")
    if exc and "cancel scope in a different task" in str(exc):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.debug(
            "%s SDK stream cleanup: cancel scope cross-task exit (harmless)",
            now,
        )
        return
    # Default handler for everything else
    loop.default_exception_handler(context)


@asynccontextmanager
async def _lifespan(app: Starlette):
    global orch, _consult_registry
    asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)
    orch = Taktis()
    await orch.initialize()
    from taktis.core.profiles import refresh_from_api as _refresh_profiles
    await _refresh_profiles()
    _consult_registry = ConsultRegistry()
    sweep_task = asyncio.create_task(
        _consult_registry.run_sweep_loop(), name="consult-sweep"
    )

    def _on_sweep_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("consult sweep task crashed: %s", exc, exc_info=exc)

    sweep_task.add_done_callback(_on_sweep_done)
    logger.info("Taktis web UI started")
    yield
    sweep_task.cancel()
    with suppress(asyncio.CancelledError):
        await sweep_task
    await orch.shutdown()
    orch = None
    _consult_registry = None
    logger.info("Taktis web UI stopped")


def _orch() -> Taktis:
    """Return the shared Taktis engine or raise."""
    if orch is None:
        raise TaktisError("Taktis not initialized")
    return orch


# ======================================================================
# Page routes (full HTML)
# ======================================================================


async def page_dashboard(request: Request) -> HTMLResponse:
    """GET / -- dashboard with global status."""
    o = _orch()
    status = await o.get_status()
    projects = await o.list_projects()
    active_tasks = await o.get_active_tasks_all()
    interrupted = await o.get_interrupted_work()
    recent_events = await o.get_recent_task_transitions(limit=8)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "status": status,
            "projects": projects,
            "active_tasks": active_tasks,
            "interrupted": interrupted,
            "recent_events": recent_events,
            "active_page": "dashboard",
        },
    )


async def page_projects(request: Request) -> HTMLResponse:
    """GET /projects -- project list."""
    from taktis.core.env_vars import enrich_template
    o = _orch()
    experts = await o.list_experts()
    async with get_session() as conn:
        projects = await repo.list_projects_summary(conn)
        pipeline_templates = await repo.list_pipeline_templates(conn)
    for t in pipeline_templates:
        enrich_template(t)
    return templates.TemplateResponse(
        "projects.html",
        {"request": request, "projects": projects, "experts": experts,
         "pipeline_templates": pipeline_templates, "active_page": "projects"},
    )


async def page_project_detail(request: Request) -> HTMLResponse:
    """GET /projects/{name} -- single project with phases and tasks."""
    o = _orch()
    name = request.path_params["name"]
    project = await o.get_project(name)
    if project is None:
        return HTMLResponse("Project not found", status_code=404)
    phases = await o.list_phases(name)
    tasks = await o.list_tasks(name)
    experts = await o.list_experts()

    # Check if there's a pending plan approval for this project
    pending_plan_approval = False
    pending_gate = None
    project_id = project.get("id", "")
    executor = o._active_flow_executors.get(project_id)
    if executor is not None:
        if getattr(executor, "_pending_plan", None) is not None:
            pending_plan_approval = True
        # Check for pending human gates
        for node_id, gate_info in getattr(executor, "_pending_gates", {}).items():
            if not gate_info.get("event", asyncio.Event()).is_set():
                pending_gate = {
                    "node_id": node_id,
                    "message": gate_info.get("message", ""),
                    "phase_id": gate_info.get("phase_id", ""),
                    "node_name": gate_info.get("node_name", "Human Gate"),
                    "upstream_preview": gate_info.get("upstream_preview", ""),
                }
                break

    from taktis.core.profiles import MODEL_CONTEXT_WINDOWS
    return templates.TemplateResponse(
        "project_detail.html",
        {
            "request": request,
            "project": project,
            "phases": phases,
            "tasks": tasks,
            "experts": experts,
            "active_page": "projects",
            "pending_plan_approval": pending_plan_approval,
            "pending_gate": pending_gate,
            "ctx_windows": MODEL_CONTEXT_WINDOWS,
        },
    )


async def page_project_timeline(request: Request) -> HTMLResponse:
    """GET /projects/{name}/timeline -- unified timeline view of all phases and tasks."""
    o = _orch()
    name = request.path_params["name"]
    project = await o.get_project(name)
    if project is None:
        return HTMLResponse("Project not found", status_code=404)
    phases = await o.list_phases(name)
    tasks = await o.list_tasks(name)

    def _parse_dt(val):
        """Parse datetime from string or passthrough datetime objects."""
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        try:
            return datetime.fromisoformat(str(val))
        except (ValueError, TypeError):
            return None

    def _task_duration_secs(t: dict) -> float | None:
        """Compute task duration in seconds, or None."""
        sa = _parse_dt(t.get("started_at"))
        ca = _parse_dt(t.get("completed_at"))
        if sa and ca:
            d = (ca - sa).total_seconds()
            return d if d >= 0 else None
        return None

    # Compute per-task duration and inject into task dict
    for t in tasks:
        dur = _task_duration_secs(t)
        t["duration_secs"] = dur

    # Group tasks by phase_id -> wave for the timeline structure
    from collections import defaultdict
    phase_tasks: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        pid = t.get("phase_id", "")
        phase_tasks[pid].append(t)

    # Build timeline data: list of phases, each with waves, each with tasks
    timeline_phases = []
    for phase in phases:
        phase_id = phase.get("id", "")
        ptasks = phase_tasks.get(phase_id, [])

        # Group tasks by wave
        wave_map: dict[int, list[dict]] = defaultdict(list)
        for t in ptasks:
            wave_map[t.get("wave", 1)].append(t)

        waves = []
        for wave_num in sorted(wave_map.keys()):
            wtasks = wave_map[wave_num]
            wave_cost = sum(t.get("cost_usd", 0) or 0 for t in wtasks)
            # Wave duration = max of individual task durations (parallel)
            durations = [t["duration_secs"] for t in wtasks if t.get("duration_secs")]
            wave_duration = max(durations) if durations else 0
            waves.append({
                "number": wave_num,
                "tasks": wtasks,
                "task_count": len(wtasks),
                "is_parallel": len(wtasks) > 1,
                "total_cost": wave_cost,
                "duration": wave_duration,
            })

        phase_cost = sum(t.get("cost_usd", 0) or 0 for t in ptasks)
        # Build phase entry without embedding the full tasks list from
        # _enrich_phase (we use wave-grouped tasks instead)
        timeline_phases.append({
            "id": phase.get("id"),
            "name": phase.get("name"),
            "description": phase.get("description"),
            "goal": phase.get("goal"),
            "phase_number": phase.get("phase_number"),
            "status": phase.get("status"),
            "waves": waves,
            "total_cost": phase_cost,
            "task_count": len(ptasks),
        })

    return templates.TemplateResponse(
        "project_timeline.html",
        {
            "request": request,
            "project": project,
            "timeline_phases": timeline_phases,
            "active_page": "projects",
        },
    )


async def page_task_detail(request: Request) -> HTMLResponse:
    """GET /tasks/{task_id} -- task detail with output."""
    o = _orch()
    task_id = request.path_params["task_id"]
    task = await o.get_task(task_id)
    if task is None:
        return HTMLResponse("Task not found", status_code=404)
    # Look up project and phase for breadcrumb + discuss/research buttons
    project_name = None
    proj = None
    has_discuss = False
    has_research = False
    async with get_session() as conn:
        if task.get("project_id"):
            proj = await repo.get_project_by_id(conn, task["project_id"])
            if proj:
                project_name = proj["name"]
        if task.get("phase_id") and proj:
            phase = await repo.get_phase_by_id(conn, task["phase_id"])
            if phase:
                from taktis.core.context import _ctx_dir
                phase_dir = _ctx_dir(proj["working_dir"]) / "phases" / str(phase["phase_number"])
                has_discuss = (phase_dir / f"DISCUSS_{task_id}.md").exists()
                has_research = (phase_dir / f"RESEARCH_{task_id}.md").exists()
        # Last activity timestamp for idle indicator (running tasks only)
        last_activity = None
        if task and task["status"] == "running":
            cur = await conn.execute(
                "SELECT MAX(timestamp) as last_activity FROM task_outputs WHERE task_id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
            if row and row[0]:
                last_activity = row[0]
    output = await o.get_task_output(task_id, tail=200)
    pending_approval = await o.get_pending_approval(task_id)

    # Check if project has a pending plan approval (for post-completion banner)
    pending_plan_approval = False
    project_id_for_plan = ""
    if proj:
        project_id_for_plan = proj.get("id", "")
        executor = o._active_flow_executors.get(project_id_for_plan)
        if executor is not None and getattr(executor, "_pending_plan", None) is not None:
            pending_plan_approval = True

    # Read actual context window for the task's model
    context_window = _get_context_window(task.get("model"))

    # Parse context manifest for the inspector panel
    context_manifest = None
    raw_manifest = task.get("context_manifest")
    if raw_manifest:
        try:
            context_manifest = json.loads(raw_manifest)
        except (json.JSONDecodeError, TypeError):
            pass

    return templates.TemplateResponse(
        "task_detail.html",
        {
            "request": request,
            "task": task,
            "output": output,
            "project_name": project_name,
            "active_page": "projects",
            "pending_approval": pending_approval,
            "has_discuss": has_discuss,
            "has_research": has_research,
            "context_window": context_window,
            "context_manifest": context_manifest,
            "last_activity": last_activity,
            "pending_plan_approval": pending_plan_approval,
            "project_id_for_plan": project_id_for_plan,
        },
    )


async def page_experts(request: Request) -> HTMLResponse:
    """GET /experts -- expert list."""
    o = _orch()
    experts = await o.list_experts()
    return templates.TemplateResponse(
        "experts.html",
        {"request": request, "experts": experts, "active_page": "experts"},
    )


async def page_admin(request: Request) -> HTMLResponse:
    """GET /admin -- admin/settings page (read-only diagnostics).

    If ``admin_api_key`` is configured, requires ``Authorization: Bearer <key>``
    or ``?key=<key>`` query parameter.  If no key is configured, access is open.
    """
    import os
    import platform
    import sys

    from taktis.config import settings

    # Optional auth gate — only accepts Authorization: Bearer header
    if settings.admin_api_key:
        auth = request.headers.get("authorization", "")
        expected = settings.admin_api_key
        if not (auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], expected)):
            return HTMLResponse(
                "<h1>401 Unauthorized</h1><p>Admin access requires a valid API key.</p>",
                status_code=401,
            )

    o = _orch()

    # Database file size
    db_path = Path("taktis.db")
    db_size = 0
    try:
        if db_path.exists():
            db_size = os.path.getsize(db_path)
    except OSError:
        pass  # File may be inaccessible; leave at 0

    # Running process count
    running_count = 0
    if o.process_manager is not None:
        running_count = o.process_manager.get_running_count()

    # Event bus subscriber counts
    event_subscribers = sum(
        len(v) for v in o.event_bus._subscribers.values()
    )

    diagnostics = {
        "db_size_mb": round(db_size / (1024 * 1024), 2),
        "running_tasks": running_count,
        "event_subscribers": event_subscribers,
        "active_pipelines": 0,
        "events_published": o.event_bus.total_events_published,
        "events_dropped": o.event_bus.total_events_dropped,
        "stale_sweeps": o.event_bus.total_stale_sweeps,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.platform(),
    }

    config_items = {
        "max_concurrent_tasks": settings.max_concurrent_tasks,
        "default_model": settings.default_model,
        "default_permission_mode": settings.default_permission_mode,
        "log_level": settings.log_level,
        "phase_timeout": settings.phase_timeout,
    }

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "active_page": "admin",
            "config": config_items,
            "diagnostics": diagnostics,
        },
    )


# ======================================================================
# Partial routes (HTML fragments for htmx)
# ======================================================================


async def partial_status_cards(request: Request) -> HTMLResponse:
    """GET /partials/status-cards"""
    o = _orch()
    status = await o.get_status()
    return templates.TemplateResponse(
        "partials/status_cards.html",
        {"request": request, "status": status},
    )


async def partial_active_tasks(request: Request) -> HTMLResponse:
    """GET /partials/active-tasks"""
    o = _orch()
    active_tasks = await o.get_active_tasks_all()
    return templates.TemplateResponse(
        "partials/active_tasks.html",
        {"request": request, "active_tasks": active_tasks},
    )


async def partial_project_list(request: Request) -> HTMLResponse:
    """GET /partials/project-list"""
    async with get_session() as conn:
        projects = await repo.list_projects_summary(conn)
    return templates.TemplateResponse(
        "partials/project_list.html",
        {"request": request, "projects": projects},
    )


def _extract_result_text(content: Any) -> str:
    """Convert tool result content (str, list, dict, None) to display text."""
    if content is None or content == "":
        return "(no output)"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", str(item)))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _process_tool_results(event: dict, blocks: list[dict]) -> None:
    """Extract tool results from a 'user' event and attach to tool_use blocks."""
    # New format: tool_use_results (list of results)
    results_list = event.get("tool_use_results")
    if results_list and isinstance(results_list, list):
        # Match results to tool_use blocks in forward order (API returns
        # results in the same order as the corresponding tool calls)
        for tr in results_list:
            result_text = _extract_result_text(
                tr.get("content") if isinstance(tr, dict) else tr
            )[:5000]
            attached = False
            for b in blocks:
                if b["type"] == "tool_use" and "result" not in b:
                    b["result"] = result_text
                    attached = True
                    break
            if not attached:
                blocks.append({"type": "tool_result", "text": result_text})
        return

    # Legacy format: tool_use_result (single result)
    raw = event.get("tool_use_result")
    if raw is not None:
        result_text = _extract_result_text(raw)[:5000]
        for b in reversed(blocks):
            if b["type"] == "tool_use" and "result" not in b:
                b["result"] = result_text
                return
        blocks.append({"type": "tool_result", "text": result_text})


def _accumulate_output_blocks(events: list[dict]) -> list[dict]:
    """Walk task output events and accumulate into compact renderable blocks.

    For completed turns (before the last assistant/result event), uses the full
    assistant messages to avoid duplicating streaming deltas.  For the current
    in-progress turn (after the last assistant/result), accumulates deltas into
    text/thinking blocks.

    Returns a list of dicts with keys: type, text/name, and optionally in_progress.
    """
    # Find boundary: last assistant/result event index
    last_full_idx = -1
    for i, entry in enumerate(events):
        c = entry.get("content") or {}
        if isinstance(c, dict) and c.get("type") in ("assistant", "result"):
            last_full_idx = i

    # Check if result events should be skipped (duplicated by assistant events)
    has_assistant = any(
        isinstance(e.get("content"), dict) and e["content"].get("type") == "assistant"
        for e in events[: last_full_idx + 1]
    ) if last_full_idx >= 0 else False

    blocks: list[dict] = []
    current_text: list[str] = []
    current_thinking: list[str] = []
    in_thinking = False

    def _flush_text() -> None:
        nonlocal current_text
        if current_text:
            blocks.append({"type": "text", "text": "".join(current_text)})
            current_text = []

    def _flush_thinking() -> None:
        nonlocal current_thinking, in_thinking
        if current_thinking:
            blocks.append({"type": "thinking", "text": "".join(current_thinking)})
            current_thinking = []
        in_thinking = False

    for i, entry in enumerate(events):
        c = entry.get("content") or {}
        if not isinstance(c, dict):
            continue
        etype = c.get("type", "")

        # --- Completed turns region: use full messages, skip deltas ---
        if i <= last_full_idx:
            if etype == "assistant":
                msg = c.get("message", {})
                if isinstance(msg, dict):
                    for block in msg.get("content", []):
                        btype = block.get("type", "")
                        if btype == "text":
                            blocks.append({"type": "text", "text": block.get("text", "")})
                        elif btype == "thinking":
                            blocks.append({"type": "thinking", "text": block.get("thinking", "")})
                        elif btype == "tool_use":
                            blocks.append({"type": "tool_use", "name": block.get("name", "?"), "input": block.get("input", {})})
            elif etype == "result":
                # Always emit a result block for token/cost display
                blk: dict = {"type": "result", "text": ""}
                if not has_assistant:
                    r = c.get("result", "")
                    if isinstance(r, str) and r:
                        blk["text"] = r
                # Attach token/cost data for per-message display
                blk["input_tokens"] = c.get("input_tokens", 0) or 0
                blk["output_tokens"] = c.get("output_tokens", 0) or 0
                blk["cost_usd"] = c.get("cost_usd", 0) or 0
                blocks.append(blk)
            elif c.get("error"):
                blocks.append({"type": "error", "text": c["error"]})
            elif c.get("stderr"):
                blocks.append({"type": "error", "text": c["stderr"]})
            elif etype == "user_message":
                blocks.append({"type": "user_message", "text": c.get("text", "")})
            elif etype == "user":
                _process_tool_results(c, blocks)
            elif etype == "ask_user_question":
                questions = c.get("questions", [])
                q_text = "\n".join(
                    f"**{q.get('question', '?')}**\n"
                    + "\n".join(f"- {o.get('label', '?')}" for o in q.get("options", []))
                    for q in questions
                ) if questions else "(question data unavailable)"
                blocks.append({"type": "ask_question", "text": q_text})
            continue  # skip delta events in completed region

        # --- In-progress turn: accumulate deltas ---
        if c.get("error"):
            _flush_text(); _flush_thinking()
            blocks.append({"type": "error", "text": c["error"]})
            continue
        if c.get("stderr"):
            _flush_text(); _flush_thinking()
            blocks.append({"type": "error", "text": c["stderr"]})
            continue
        if etype == "user_message":
            _flush_text(); _flush_thinking()
            blocks.append({"type": "user_message", "text": c.get("text", "")})
            continue

        if etype == "user":
            _flush_text(); _flush_thinking()
            _process_tool_results(c, blocks)
            continue

        if etype == "ask_user_question":
            _flush_text(); _flush_thinking()
            questions = c.get("questions", [])
            q_text = "\n".join(
                f"**{q.get('question', '?')}**\n"
                + "\n".join(f"- {o.get('label', '?')}" for o in q.get("options", []))
                for q in questions
            ) if questions else "(question data unavailable)"
            blocks.append({"type": "ask_question", "text": q_text})
            continue

        if etype == "content_block_start":
            cb = c.get("content_block", {})
            cb_type = cb.get("type", "")
            if cb_type == "thinking":
                _flush_text()
                in_thinking = True
            elif cb_type == "text":
                _flush_thinking()
                in_thinking = False
            elif cb_type == "tool_use":
                _flush_text(); _flush_thinking()
                blocks.append({"type": "tool_use", "name": cb.get("name", "?"), "input": cb.get("input", {})})
            continue

        if etype == "content_block_delta":
            delta = c.get("delta", {})
            if in_thinking:
                current_thinking.append(delta.get("thinking", ""))
            else:
                current_text.append(delta.get("text", ""))
            continue

        if etype == "content_block_stop":
            if in_thinking:
                _flush_thinking()
            else:
                _flush_text()
            continue

        if etype == "permission_request":
            _flush_text(); _flush_thinking()
            blocks.append({"type": "permission", "tool": c.get("tool_name", "?")})
            continue

    # Flush remaining in-progress text
    if current_thinking:
        blocks.append({"type": "thinking", "text": "".join(current_thinking), "in_progress": True})
    if current_text:
        blocks.append({"type": "text", "text": "".join(current_text), "in_progress": True})

    return blocks


async def partial_task_output(request: Request) -> HTMLResponse:
    """GET /partials/task-output/{task_id}"""
    o = _orch()
    task_id = request.path_params["task_id"]
    task = await o.get_task(task_id)
    if task is None:
        return HTMLResponse('<span class="text-muted">Task not found.</span>')
    is_running = task["status"] == "running"
    # Use accumulated blocks for ALL tasks — merges tool results into
    # their tool call collapses, works for both running and completed.
    all_events = await o.get_task_output(task_id)
    blocks = _accumulate_output_blocks(all_events)
    return templates.TemplateResponse(
        "partials/task_output.html",
        {"request": request, "task": task, "blocks": blocks,
         "is_running": is_running, "active_page": ""},
    )


async def partial_task_status(request: Request) -> HTMLResponse:
    """GET /partials/task-status/{task_id} -- hidden poller that triggers
    page reload when a task leaves 'running' status."""
    o = _orch()
    task_id = request.path_params["task_id"]
    task = await o.get_task(task_id)
    if task is None or task["status"] != "running":
        # Task is no longer running — reload to show updated UI
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return HTMLResponse("")


# ======================================================================
# API routes (form submissions, htmx-compatible)
# ======================================================================


async def api_create_project(request: Request):
    """POST /api/projects -- create project from form data."""
    o = _orch()
    form = await request.form()
    try:
        name = _validate_name(str(form.get("name", "")), "Project name")
        working_dir = str(form.get("working_dir", "")).strip()
        if not working_dir:
            raise ValueError("Working directory is required")
        project = await o.create_project(
            name=name,
            working_dir=working_dir,
            description=str(form.get("description", "")),
            model=str(form.get("model", "")) or None,
            permission_mode=str(form.get("permission_mode", "")) or None,
            create_dir=form.get("create_dir") == "on",
            clean_existing=form.get("clean_existing") == "on",
        )
    except (ValueError, KeyError, TaktisError) as exc:
        msg = _user_message(exc)
        # Special handling: existing .taktis/ detected — show checkbox prompt
        if msg.startswith("EXISTING_PROJECT:"):
            parts = msg.split(":", 3)
            old_name = parts[1] if len(parts) > 2 else ""
            display_msg = parts[2] if len(parts) > 2 else msg
            if request.headers.get("HX-Request"):
                # Inject a hidden input into the form via JS so htmx picks it up
                html = (
                    f'<div class="error" style="color:var(--warning);margin:0.5rem 0;">'
                    f'<strong>Existing project data found{" from &quot;" + _esc(old_name) + "&quot;" if old_name else ""}.</strong><br>'
                    f'<span style="margin-top:0.4rem;display:block;">'
                    f'Click Create Project again to archive the old project and start fresh.</span>'
                    f'</div>'
                    f'<script>'
                    f'(function(){{var f=document.querySelector(\'form[hx-post="/api/projects"]\');'
                    f'if(f&&!f.querySelector(\'input[name="clean_existing"]\')){{var i=document.createElement("input");'
                    f'i.type="hidden";i.name="clean_existing";i.value="on";f.appendChild(i);}}}})();'
                    f'</script>'
                )
                return HTMLResponse(
                    html, status_code=200,
                    headers={"HX-Retarget": "#create-project-error", "HX-Reswap": "innerHTML"},
                )
            return HTMLResponse(f'<div class="error">{_esc(display_msg)}</div>', status_code=400)
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                f'<div class="error" style="color:var(--danger);margin:0.5rem 0;">{_esc(msg)}</div>',
                status_code=200,
                headers={"HX-Retarget": "#create-project-error", "HX-Reswap": "innerHTML"},
            )
        return HTMLResponse(f'<div class="error">{_esc(msg)}</div>', status_code=400)

    description = str(form.get("description", ""))
    redirect_url = f"/projects/{project['name']}"
    pipeline_template_id = str(form.get("pipeline_template", "")).strip()

    # Optional env_vars (JSON string in a hidden field, populated by the
    # new-project form when a pipeline template declares ${VAR} references).
    # Must be applied BEFORE pipeline execution so api_call substitutions see
    # the values on the very first run.
    env_vars_raw = str(form.get("env_vars", "")).strip()
    if env_vars_raw:
        try:
            parsed = json.loads(env_vars_raw)
            if isinstance(parsed, dict):
                clean: dict[str, str] = {}
                for k, v in parsed.items():
                    key = str(k).strip()
                    if not key or not all(c.isalnum() or c == "_" for c in key):
                        continue
                    clean[key] = "" if v is None else str(v)
                if clean:
                    await o.update_project(project["name"], default_env_vars=clean)
        except (ValueError, TypeError):
            logger.warning("api_create_project: invalid env_vars JSON, skipping")

    # Validate: pipeline template requires a description
    if pipeline_template_id and not description.strip():
        msg = "A description is required when a pipeline template is selected."
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                f'<div class="error" style="color:var(--danger);margin:0.5rem 0;">{msg}</div>',
                status_code=200,
                headers={"HX-Retarget": "#create-project-error", "HX-Reswap": "innerHTML"},
            )
        return HTMLResponse(f'<div class="error">{msg}</div>', status_code=400)

    if pipeline_template_id and description.strip():
        # Execute the selected pipeline template via GraphExecutor
        try:
            async with get_session() as conn:
                tmpl = await repo.get_pipeline_template(conn, pipeline_template_id)
            if tmpl:
                import asyncio as _asyncio
                _task = _asyncio.create_task(
                    o.execute_flow(project["name"], tmpl["flow_json"], template_name=tmpl["name"]),
                    name=f"flow-{project['name']}",
                )
                def _on_flow_done(t):
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.error("Flow execution failed for '%s': %s",
                                     project["name"], exc, exc_info=exc)
                _task.add_done_callback(_on_flow_done)
                redirect_url = f"/projects/{project['name']}"
        except (ValueError, KeyError, TaktisError):
            logger.warning(
                "Pipeline execution failed after project creation; "
                "redirecting to project page",
                exc_info=True,
            )

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = redirect_url
        return resp
    return RedirectResponse(redirect_url, status_code=303)


async def api_delete_project(request: Request):
    """DELETE /api/projects/{name}"""
    o = _orch()
    name = request.path_params["name"]
    ok = await o.delete_project(name)
    if not ok:
        return HTMLResponse('<div class="error">Failed to delete project</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = "/projects"
        return resp
    return RedirectResponse("/projects", status_code=303)


async def api_update_project_env_vars(request: Request) -> JSONResponse:
    """PATCH /api/projects/{name}/env-vars — replace project env vars dict.

    API Call nodes substitute ``${VAR}`` against this dict (with os.environ
    as fallback) at runtime.  Suited for non-secret per-project config like
    ``GH_REPO``.  Real secrets should live in the host shell or .env file.
    """
    name = request.path_params["name"]
    o = _orch()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    raw = body.get("env_vars")
    if not isinstance(raw, dict):
        return JSONResponse({"error": "env_vars must be an object"}, status_code=400)
    env_vars = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        # Allow letters, digits, underscores in keys (Posix-ish)
        if not all(c.isalnum() or c == "_" for c in key):
            return JSONResponse(
                {"error": f"Invalid key '{key}': use letters, digits, underscores only"},
                status_code=400,
            )
        env_vars[key] = "" if v is None else str(v)
    updated = await o.update_project(name, default_env_vars=env_vars)
    if updated is None:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    return JSONResponse({"ok": True, "env_vars": env_vars})


async def api_create_phase(request: Request):
    """POST /api/projects/{name}/phases -- create phase."""
    o = _orch()
    project_name = request.path_params["name"]
    form = await request.form()
    try:
        phase_name = _validate_name(str(form.get("name", "")), "Phase name")
        phase = await o.create_phase(
            project_name=project_name,
            name=phase_name,
            goal=str(form.get("goal", "")),
            description=str(form.get("description", "")),
        )
    except (ValueError, KeyError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/projects/{project_name}"
        return resp
    return RedirectResponse(f"/projects/{project_name}", status_code=303)


async def api_create_task(request: Request):
    """POST /api/projects/{name}/tasks -- create task."""
    o = _orch()
    project_name = request.path_params["name"]
    form = await request.form()
    try:
        phase_number = form.get("phase_number")
        if not phase_number:
            raise ValueError("A phase is required for all tasks")
        prompt = str(form.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("Task prompt is required")
        if len(prompt) > 50000:
            raise ValueError("Task prompt must be 50,000 characters or fewer")
        task = await o.create_task(
            project_name=project_name,
            prompt=prompt,
            phase_number=int(phase_number),
            name=str(form.get("name", "")),
            expert=str(form.get("expert", "")) or None,
            wave=int(form.get("wave", 1)),
            interactive=form.get("interactive") == "on",
            model=str(form.get("model", "")) or None,
        )
    except (ValueError, KeyError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/projects/{project_name}"
        return resp
    return RedirectResponse(f"/projects/{project_name}", status_code=303)


async def api_start_task(request: Request):
    """POST /api/tasks/{task_id}/start"""
    o = _orch()
    task_id = request.path_params["task_id"]
    try:
        await o.start_task(task_id)
    except TaktisError as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


async def api_stop_task(request: Request):
    """POST /api/tasks/{task_id}/stop"""
    o = _orch()
    task_id = request.path_params["task_id"]
    try:
        await o.stop_task(task_id)
    except TaktisError as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)



async def api_approve_checkpoint(request: Request):
    """POST /api/tasks/{task_id}/approve"""
    o = _orch()
    task_id = request.path_params["task_id"]
    try:
        await o.approve_checkpoint(task_id)
    except TaktisError as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


async def api_approve_answers(request: Request):
    """POST /api/tasks/{task_id}/approve-answers — approve AskUserQuestion with user's answers."""
    o = _orch()
    task_id = request.path_params["task_id"]
    try:
        body = await request.json()
        answers = body.get("answers", [])
        answer_text = ", ".join(answers) if answers else "No selection"
        # Store user's answer in conversation history
        async with get_session() as conn:
            await repo.create_task_output(
                conn, task_id=task_id, event_type="user_message",
                content={"type": "user_message", "text": f"Selected: {answer_text}"},
            )
        process = o._execution_service._manager.get_process(task_id)
        if process and process.pending_permission_info:
            original_input = dict(process.pending_permission_info.get("input", {}))
            # Build per-question answer dicts matching the SDK's expected format
            questions = original_input.get("questions", [])
            answer_dicts = {}
            for i, q in enumerate(questions):
                qid = q.get("id", str(i))
                if i < len(answers):
                    answer_dicts[qid] = answers[i]
                else:
                    answer_dicts[qid] = ""
            original_input["answers"] = answer_dicts
            await process.approve_tool(updated_input=original_input)
            async with get_session() as conn:
                task = await repo.get_task(conn, task_id)
                if task and task["status"] == "awaiting_input":
                    await repo.update_task(conn, task_id, status="running")
            # Background: poll for ===CONFIRMED=== in the resumed turn's result
            import asyncio
            async def _poll_confirmed(tid: str) -> None:
                for _ in range(30):
                    await asyncio.sleep(1)
                    async with get_session() as conn:
                        t = await repo.get_task(conn, tid)
                        rs = (t.get("result_summary") or "") if t else ""
                        if "===CONFIRMED===" in rs and t.get("status") == "awaiting_input":
                            await repo.update_task(conn, tid, status="completed")
                            logger.info("[%s] AskUserQuestion: ===CONFIRMED=== detected, completed", tid)
                            return
                        if t and t.get("status") not in ("running", "awaiting_input"):
                            return
            _task = asyncio.create_task(
                _poll_confirmed(task_id),
                name=f"poll-confirmed-{task_id}",
            )
            _task.add_done_callback(
                make_done_callback(
                    f"poll-confirmed-{task_id}",
                    _orch().event_bus,
                )
            )
        else:
            return HTMLResponse('<div class="error">No pending question</div>', status_code=400)
    except TaktisError as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


async def api_deny_tool(request: Request):
    """POST /api/tasks/{task_id}/deny"""
    o = _orch()
    task_id = request.path_params["task_id"]
    form = await request.form()
    message = str(form.get("message", "User denied this action"))
    try:
        await o.deny_tool(task_id, message)
    except TaktisError as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


async def api_continue_task(request: Request):
    """POST /api/tasks/{task_id}/continue -- continue chat with follow-up message."""
    o = _orch()
    task_id = request.path_params["task_id"]
    form = await request.form()
    text = str(form.get("text", ""))
    if not text:
        return HTMLResponse('<div class="error">No message provided</div>', status_code=400)
    try:
        await o.continue_task(task_id, text)
    except TaktisError as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


async def api_send_input(request: Request):
    """POST /api/tasks/{task_id}/input"""
    o = _orch()
    task_id = request.path_params["task_id"]
    form = await request.form()
    text = str(form.get("text", ""))
    if not text:
        return HTMLResponse('<div class="error">No input text provided</div>', status_code=400)
    try:
        await o.send_input(task_id, text)
    except TaktisError as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task_id}"
        return resp
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


async def api_run_phase(request: Request):
    """POST /api/phases/{project}/{number}/run"""
    o = _orch()
    project_name = request.path_params["project"]
    try:
        phase_number = int(request.path_params["number"])
        if phase_number < 1:
            raise ValueError("Phase number must be positive")
    except ValueError:
        return HTMLResponse('<div class="error">Invalid phase number</div>', status_code=400)
    try:
        await o.run_phase(project_name, phase_number)
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    # Return 200 — SSE will push task/phase status updates live.
    # No redirect: avoids race where page reloads before tasks start running.
    return HTMLResponse("", status_code=200)


async def api_discuss_task(request: Request):
    """POST /api/tasks/{task_id}/discuss"""
    o = _orch()
    task_id = request.path_params["task_id"]
    try:
        task = await o.discuss_task(task_id)
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if task is None:
        return HTMLResponse('<div class="error">Task not found</div>', status_code=404)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task['id']}"
        return resp
    return RedirectResponse(f"/tasks/{task['id']}", status_code=303)


async def api_research_task(request: Request):
    """POST /api/tasks/{task_id}/research"""
    o = _orch()
    task_id = request.path_params["task_id"]
    try:
        task = await o.research_task(task_id)
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if task is None:
        return HTMLResponse('<div class="error">Task not found</div>', status_code=404)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/tasks/{task['id']}"
        return resp
    return RedirectResponse(f"/tasks/{task['id']}", status_code=303)


async def api_run_project(request: Request):
    """POST /api/projects/{name}/run -- run all phases sequentially."""
    o = _orch()
    project_name = request.path_params["name"]
    try:
        await o.run_project(project_name)
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/projects/{project_name}"
        return resp
    return RedirectResponse(f"/projects/{project_name}", status_code=303)


async def api_interrupted(request: Request):
    """GET /api/interrupted -- return interrupted phases and pipelines as JSON."""
    o = _orch()
    result = await o.get_interrupted_work()
    return JSONResponse(result)


async def api_resume_phase(request: Request):
    """POST /api/phases/{phase_id}/resume -- resume an interrupted phase."""
    o = _orch()
    phase_id = request.path_params["phase_id"]

    # Look up project name for the post-resume redirect.
    project_name: str | None = None
    async with get_session() as conn:
        phase = await repo.get_phase_by_id(conn, phase_id)
        if phase is not None:
            proj = await repo.get_project_by_id(conn, phase["project_id"])
            if proj is not None:
                project_name = proj["name"]

    if phase is None:
        return HTMLResponse('<div class="error">Phase not found</div>', status_code=404)

    try:
        # resume_phase validates synchronously then fires asyncio.create_task internally —
        # the await here returns immediately after the background task is scheduled.
        await o.resume_phase(phase_id)
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)

    redirect_url = f"/projects/{project_name}" if project_name else "/"
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = redirect_url
        return resp
    return RedirectResponse(redirect_url, status_code=303)


async def api_resume_pipeline(request: Request):
    """POST /api/phases/{phase_id}/resume-pipeline -- resume a dead pipeline executor."""
    o = _orch()
    phase_id = request.path_params["phase_id"]

    # Look up phase, project, and pipeline template
    async with get_session() as conn:
        phase = await repo.get_phase_by_id(conn, phase_id)
        if phase is None:
            return HTMLResponse('<div class="error">Phase not found</div>', status_code=404)
        proj = await repo.get_project_by_id(conn, phase["project_id"])
        if proj is None:
            return HTMLResponse('<div class="error">Project not found</div>', status_code=404)

        # Find the pipeline template used:
        # 1. Check context_config for stored template_name (multi-module phases)
        # 2. Fall back to phase name pattern "Flow: <template_name>" (single-module)
        # 3. Fall back to matching phase name directly against template names
        import json as _json
        context_config_raw = phase.get("context_config", "") or ""
        stored_template_name = None
        try:
            cc = _json.loads(context_config_raw) if context_config_raw else {}
            stored_template_name = cc.get("template_name")
        except (_json.JSONDecodeError, TypeError):
            pass

        phase_name = phase.get("name", "")
        # Build candidate names in priority order
        candidate_names = []
        if stored_template_name:
            candidate_names.append(stored_template_name)
        if phase_name.startswith("Flow: "):
            candidate_names.append(phase_name.replace("Flow: ", "", 1))
        if phase_name not in candidate_names:
            candidate_names.append(phase_name)

        tmpl = None
        templates = await repo.list_pipeline_templates(conn)
        for candidate in candidate_names:
            for t in templates:
                if t["name"] == candidate:
                    tmpl = t
                    break
            if tmpl:
                break

    if tmpl is None:
        return HTMLResponse(
            f'<div class="error">Pipeline template not found (tried: {", ".join(candidate_names)})</div>',
            status_code=404,
        )

    project_name = proj["name"]
    import asyncio as _asyncio
    _task = _asyncio.create_task(
        o.resume_flow(project_name, tmpl["flow_json"], phase_id, template_name=tmpl["name"]),
        name=f"resume-{project_name}",
    )
    def _on_done(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Pipeline resume failed for '%s': %s", project_name, exc, exc_info=exc)
    _task.add_done_callback(_on_done)

    redirect_url = f"/projects/{project_name}"
    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = redirect_url
        return resp
    return RedirectResponse(redirect_url, status_code=303)


async def api_approve_pipeline_plan(request: Request):
    """POST /api/pipelines/{project_id}/approve-plan -- approve a pending plan in the graph executor."""
    o = _orch()
    project_id = request.path_params["project_id"]
    ok = o.approve_flow_plan(project_id)
    if not ok:
        return HTMLResponse('<div class="error">No plan awaiting approval</div>', status_code=404)
    if request.headers.get("HX-Request"):
        return HTMLResponse("", status_code=200)
    return HTMLResponse("Plan applied", status_code=200)


async def api_approve_gate(request: Request):
    """POST /api/pipelines/{project_id}/approve-gate/{node_id}"""
    o = _orch()
    project_id = request.path_params["project_id"]
    node_id = request.path_params["node_id"]
    # Debug: log the state so we can diagnose failures
    executor = o._active_flow_executors.get(project_id)
    if executor is None:
        logger.warning("approve_gate: no active executor for project %s (active: %s)",
                       project_id, list(o._active_flow_executors.keys()))
        return HTMLResponse(
            '<div class="toast error">No active pipeline — try Resume Pipeline first</div>',
            status_code=404,
        )
    pending = getattr(executor, "_pending_gates", {})
    logger.info("approve_gate: project=%s node=%s pending_gates=%s",
                project_id, node_id, list(pending.keys()))
    ok = o.approve_gate(project_id, node_id)
    if not ok:
        return HTMLResponse(
            f'<div class="toast error">Gate {node_id} not found in pending gates: {list(pending.keys())}</div>',
            status_code=404,
        )
    if request.headers.get("HX-Request"):
        # Trigger page reload so the pipeline progress is visible
        resp = HTMLResponse(
            '<div class="pipeline-progress"><span class="text-success">Gate approved — pipeline continuing...</span></div>',
            status_code=200,
        )
        resp.headers["HX-Refresh"] = "true"
        return resp
    return HTMLResponse("Gate approved", status_code=200)


async def api_reject_gate(request: Request):
    """POST /api/pipelines/{project_id}/reject-gate/{node_id}"""
    o = _orch()
    project_id = request.path_params["project_id"]
    node_id = request.path_params["node_id"]
    ok = o.reject_gate(project_id, node_id)
    if not ok:
        return HTMLResponse('<div class="error">No gate awaiting approval</div>', status_code=404)
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            '<div class="pipeline-progress"><span class="text-warning">Gate rejected</span></div>',
            status_code=200,
        )
    return HTMLResponse("Gate rejected", status_code=200)


async def api_stop_all(request: Request):
    """POST /api/stop-all"""
    o = _orch()
    form = await request.form()
    project_name = str(form.get("project", "")) or None
    count = await o.stop_all(project_name)
    if request.headers.get("HX-Request"):
        return HTMLResponse(f'<div class="success">Stopped {count} task(s)</div>')
    return RedirectResponse("/", status_code=303)


async def api_factory_reset(request: Request):
    """POST /api/factory-reset -- reset pipeline templates, experts, agent templates to defaults."""
    from taktis.db import factory_reset_pipeline_templates
    o = _orch()
    results = []
    async with get_session() as conn:
        count = await factory_reset_pipeline_templates(conn)
        results.append(f"Pipeline templates: {count} restored")
    # Re-load builtin experts and agent templates (force-update from .md files)
    await o.expert_registry.load_builtins()
    results.append("Experts: builtins reloaded")
    await o.agent_template_registry.load_builtins()
    results.append("Agent templates: builtins reloaded")
    msg = " | ".join(results)
    if request.headers.get("HX-Request"):
        return HTMLResponse(f'<div class="success">{msg}</div>')
    return JSONResponse({"message": msg})


async def api_create_expert(request: Request):
    """POST /api/experts -- create expert from form data."""
    o = _orch()
    form = await request.form()
    try:
        expert = await o.create_expert(
            name=str(form["name"]),
            description=str(form.get("description", "")),
            system_prompt=str(form.get("system_prompt", "")),
            category=str(form.get("category", "")),
        )
    except (ValueError, KeyError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = "/experts"
        return resp
    return RedirectResponse("/experts", status_code=303)


async def api_update_expert(request: Request):
    """POST /api/experts/{name}/edit -- update expert."""
    o = _orch()
    name = request.path_params["name"]
    form = await request.form()
    try:
        updated = await o.update_expert(
            name,
            description=str(form.get("description", "")),
            system_prompt=str(form.get("system_prompt", "")),
            category=str(form.get("category", "")),
        )
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if updated is None:
        return HTMLResponse('<div class="error">Expert not found</div>', status_code=404)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = "/experts"
        return resp
    return RedirectResponse("/experts", status_code=303)


async def api_delete_expert(request: Request):
    """POST /api/experts/{name}/delete -- delete custom expert."""
    o = _orch()
    name = request.path_params["name"]
    try:
        ok = await o.delete_expert(name)
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if not ok:
        return HTMLResponse('<div class="error">Expert not found</div>', status_code=404)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = "/experts"
        return resp
    return RedirectResponse("/experts", status_code=303)


async def page_expert_edit(request: Request) -> HTMLResponse:
    """GET /experts/{name}/edit -- edit form for an expert."""
    o = _orch()
    name = request.path_params["name"]
    expert = await o.get_expert(name)
    if expert is None:
        return HTMLResponse("Expert not found", status_code=404)
    return templates.TemplateResponse(
        "expert_edit.html",
        {"request": request, "expert": expert, "active_page": "experts"},
    )


# ------------------------------------------------------------------
# Agent Templates
# ------------------------------------------------------------------

async def page_agent_templates(request: Request) -> HTMLResponse:
    """GET /agent-templates -- template list."""
    import json as _json
    o = _orch()
    agent_templates = await o.list_agent_templates()
    # Normalize JSON string fields to lists for the template
    for t in agent_templates:
        for key in ("auto_variables", "internal_variables"):
            val = t.get(key)
            if isinstance(val, str):
                try:
                    t[key] = _json.loads(val)
                except (ValueError, TypeError):
                    t[key] = []
            elif val is None:
                t[key] = []
    return templates.TemplateResponse(
        "agent_templates.html",
        {"request": request, "agent_templates": agent_templates, "active_page": "agent_templates"},
    )


async def api_create_agent_template(request: Request):
    """POST /api/agent-templates -- create custom template."""
    o = _orch()
    form = await request.form()
    try:
        auto_vars = [v.strip() for v in str(form.get("auto_variables", "")).split(",") if v.strip()]
        internal_vars = [v.strip() for v in str(form.get("internal_variables", "")).split(",") if v.strip()]
        await o.create_agent_template(
            slug=str(form["slug"]),
            name=str(form["name"]),
            description=str(form.get("description", "")),
            prompt_text=str(form.get("prompt_text", "")),
            auto_variables=auto_vars,
            internal_variables=internal_vars,
        )
    except (ValueError, KeyError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = "/agent-templates"
        return resp
    return RedirectResponse("/agent-templates", status_code=303)


async def api_update_agent_template(request: Request):
    """POST /api/agent-templates/{slug}/edit -- update template."""
    o = _orch()
    slug = request.path_params["slug"]
    form = await request.form()
    try:
        auto_vars = [v.strip() for v in str(form.get("auto_variables", "")).split(",") if v.strip()]
        internal_vars = [v.strip() for v in str(form.get("internal_variables", "")).split(",") if v.strip()]
        updated = await o.update_agent_template(
            slug,
            name=str(form.get("name", "")),
            description=str(form.get("description", "")),
            prompt_text=str(form.get("prompt_text", "")),
            auto_variables=auto_vars,
            internal_variables=internal_vars,
        )
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if updated is None:
        return HTMLResponse('<div class="error">Template not found</div>', status_code=404)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = "/agent-templates"
        return resp
    return RedirectResponse("/agent-templates", status_code=303)


async def api_delete_agent_template(request: Request):
    """POST /api/agent-templates/{slug}/delete -- delete custom template."""
    o = _orch()
    slug = request.path_params["slug"]
    try:
        ok = await o.delete_agent_template(slug)
    except (ValueError, TaktisError) as exc:
        return HTMLResponse(f'<div class="error">{_esc(_user_message(exc))}</div>', status_code=400)
    if not ok:
        return HTMLResponse('<div class="error">Template not found</div>', status_code=404)

    if request.headers.get("HX-Request"):
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = "/agent-templates"
        return resp
    return RedirectResponse("/agent-templates", status_code=303)


async def page_agent_template_edit(request: Request) -> HTMLResponse:
    """GET /agent-templates/{slug}/edit -- edit form."""
    import json as _json
    o = _orch()
    slug = request.path_params["slug"]
    tmpl = await o.get_agent_template(slug)
    if tmpl is None:
        return HTMLResponse("Template not found", status_code=404)
    # Normalize JSON string fields to lists
    for key in ("auto_variables", "internal_variables"):
        val = tmpl.get(key)
        if isinstance(val, str):
            try:
                tmpl[key] = _json.loads(val)
            except (ValueError, TypeError):
                tmpl[key] = []
        elif val is None:
            tmpl[key] = []
    return templates.TemplateResponse(
        "agent_template_edit.html",
        {"request": request, "tmpl": tmpl, "active_page": "agent_templates"},
    )




# ======================================================================
# SSE routes
# ======================================================================


async def event_stream(request: Request):
    """GET /events -- SSE stream of all watched events."""
    o = _orch()
    queues = [(et, o.event_bus.subscribe(et)) for et in WATCHED_EVENTS]
    merged: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=5000)

    async def relay(et: str, q: asyncio.Queue) -> None:
        try:
            while True:
                event = await q.get()
                await merged.put((et, event))
        except asyncio.CancelledError:
            return

    relay_tasks = [asyncio.create_task(relay(et, q)) for et, q in queues]
    for _rt in relay_tasks:
        _rt.add_done_callback(_on_relay_done)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    et, event = await asyncio.wait_for(merged.get(), timeout=15)
                    data = event.get("data", {})
                    ts = event.get("timestamp", "")[:19]
                    task_id = data.get("task_id", "")
                    short_id = task_id[:8] if task_id else ""

                    if et == EVENT_TASK_STARTED:
                        html = f'<div class="event"><span class="event-time">{ts}</span> (*) Task <a href="/tasks/{task_id}">{short_id}</a> started</div>'
                    elif et == EVENT_TASK_COMPLETED:
                        cost = data.get("cost_usd", 0)
                        cost_s = f" ${cost:.4f}" if cost else ""
                        html = f'<div class="event"><span class="event-time">{ts}</span> (+) Task <a href="/tasks/{task_id}">{short_id}</a> completed{cost_s}</div>'
                    elif et == EVENT_TASK_FAILED:
                        html = f'<div class="event"><span class="event-time">{ts}</span> (!) Task <a href="/tasks/{task_id}">{short_id}</a> failed</div>'
                    elif et == EVENT_TASK_CHECKPOINT:
                        html = f'<div class="event"><span class="event-time">{ts}</span> (?) Task <a href="/tasks/{task_id}">{short_id}</a> awaiting input</div>'
                    elif et == EVENT_PHASE_STARTED:
                        html = f'<div class="event"><span class="event-time">{ts}</span> Phase started ({data.get("task_count", "?")} tasks)</div>'
                    elif et == EVENT_PHASE_COMPLETED:
                        html = f'<div class="event"><span class="event-time">{ts}</span> Phase completed</div>'
                    else:
                        continue  # skip task.output noise from the global feed
                    yield f"data: {html}\n\n"
                    # Also emit a named event for dashboard SSE-triggered refresh
                    if et in (EVENT_TASK_STARTED, EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, EVENT_TASK_CHECKPOINT):
                        yield f"event: status-change\ndata: refresh\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            for t in relay_tasks:
                t.cancel()
            for et, q in queues:
                o.event_bus.unsubscribe(et, q)

    return StreamingResponse(generate(), media_type="text/event-stream")


_TOOL_ICONS: dict[str, str] = {
    "read": "&#128196;",      # page
    "edit": "&#9999;&#65039;", # pencil
    "write": "&#128221;",     # memo
    "bash": "&#9654;",        # play
    "grep": "&#128269;",      # magnifying glass
    "glob": "&#128193;",      # folder
    "webfetch": "&#127760;",  # globe
    "websearch": "&#128269;", # search
    "skill": "&#9889;",       # lightning
    "agent": "&#9889;",       # lightning
    "task": "&#9889;",        # lightning
    "toolsearch": "&#9889;",  # lightning
}

# Maps tool name (lowercase) to CSS widget class
_TOOL_WIDGET_CLASS: dict[str, str] = {
    "bash": "tool-bash",
    "read": "tool-read",
    "edit": "tool-edit",
    "write": "tool-write",
    "grep": "tool-search",
    "glob": "tool-search",
    "webfetch": "tool-web",
    "websearch": "tool-web",
    "skill": "tool-agent",
    "agent": "tool-agent",
    "toolsearch": "tool-agent",
    "task": "tool-agent",
}


def _tool_summary(name: str, input_data: dict | None = None) -> str:
    """Extract the key parameter from a tool call for display."""
    if not input_data:
        return ""
    n = name.lower()
    if n in ("read", "edit", "write"):
        return _esc(str(input_data.get("file_path", ""))[:80])
    if n == "bash":
        return _esc(str(input_data.get("command", ""))[:80])
    if n in ("grep", "glob"):
        return _esc(str(input_data.get("pattern", ""))[:60])
    # Default: show first string value
    for v in input_data.values():
        if isinstance(v, str) and v:
            return _esc(v[:60])
    return ""


def _build_diff_html(old_string: str, new_string: str) -> str:
    """Build a unified diff HTML view from old_string and new_string."""
    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    parts: list[str] = []
    parts.append('<div class="diff-view">')
    # Show removed lines
    for line in old_lines:
        parts.append(f'<div class="diff-line diff-line-del">{_esc(line)}</div>')
    # Show added lines
    for line in new_lines:
        parts.append(f'<div class="diff-line diff-line-add">{_esc(line)}</div>')
    parts.append('</div>')
    return "".join(parts)


def _build_tool_widget_html(
    tool_name: str,
    input_data: dict | None = None,
    result_text: str | None = None,
    *,
    open_details: bool = False,
) -> str:
    """Build specialized tool widget HTML for both SSE and history replay."""
    n = tool_name.lower()
    widget_cls = _TOOL_WIDGET_CLASS.get(n, "tool-call")
    icon = _TOOL_ICONS.get(n, "&#9881;&#65039;")
    inp = input_data or {}
    open_attr = " open" if open_details else ""

    # --- Bash ---
    if n == "bash":
        cmd = _esc(str(inp.get("command", ""))[:120])
        body = ""
        if result_text:
            is_err = "error" in result_text[:50].lower() or result_text.startswith("Exit code")
            err_cls = "tw-error" if is_err else "tw-success"
            body = f'<div class="tool-widget-body {err_cls}">{_esc(result_text[:3000])}</div>'
        return (
            f'<details class="tool-widget {widget_cls}"{open_attr}>'
            f'<summary class="tool-widget-header">'
            f'<span class="tw-icon">{icon}</span>'
            f'<span class="tw-name">Bash</span>'
            f'<span class="tw-cmd">{cmd}</span>'
            f'<span class="tool-widget-chevron">&#9654;</span>'
            f'</summary>{body}</details>'
        )

    # --- Read ---
    if n == "read":
        fp = _esc(str(inp.get("file_path", ""))[:100])
        body = ""
        if result_text:
            body = f'<div class="tool-widget-body">{_esc(result_text[:3000])}</div>'
        return (
            f'<details class="tool-widget {widget_cls}"{open_attr}>'
            f'<summary class="tool-widget-header">'
            f'<span class="tw-icon">{icon}</span>'
            f'<span class="tw-name">Read</span>'
            f'<span class="tw-path">{fp}</span>'
            f'<span class="tool-widget-chevron">&#9654;</span>'
            f'</summary>{body}</details>'
        )

    # --- Edit ---
    if n == "edit":
        fp = _esc(str(inp.get("file_path", ""))[:100])
        old_s = str(inp.get("old_string", ""))
        new_s = str(inp.get("new_string", ""))
        diff_html = _build_diff_html(old_s, new_s) if (old_s or new_s) else ""
        body = ""
        if diff_html:
            body = f'<div class="tool-widget-body">{diff_html}</div>'
        elif result_text:
            body = f'<div class="tool-widget-body" style="padding:0.5rem 0.75rem;">{_esc(result_text[:3000])}</div>'
        return (
            f'<details class="tool-widget {widget_cls}"{open_attr}>'
            f'<summary class="tool-widget-header">'
            f'<span class="tw-icon">{icon}</span>'
            f'<span class="tw-name">Edit</span>'
            f'<span class="tw-path">{fp}</span>'
            f'<span class="tool-widget-chevron">&#9654;</span>'
            f'</summary>{body}</details>'
        )

    # --- Write ---
    if n == "write":
        fp = _esc(str(inp.get("file_path", ""))[:100])
        body = ""
        if result_text:
            body = f'<div class="tool-widget-body">{_esc(result_text[:3000])}</div>'
        return (
            f'<details class="tool-widget {widget_cls}"{open_attr}>'
            f'<summary class="tool-widget-header">'
            f'<span class="tw-icon">{icon}</span>'
            f'<span class="tw-name">Write</span>'
            f'<span class="tw-path">{fp}</span>'
            f'<span class="tool-widget-chevron">&#9654;</span>'
            f'</summary>{body}</details>'
        )

    # --- Grep / Glob ---
    if n in ("grep", "glob"):
        label = "Grep" if n == "grep" else "Glob"
        pattern = _esc(str(inp.get("pattern", ""))[:80])
        body = ""
        if result_text:
            body = f'<div class="tool-widget-body">{_esc(result_text[:3000])}</div>'
        return (
            f'<details class="tool-widget {widget_cls}"{open_attr}>'
            f'<summary class="tool-widget-header">'
            f'<span class="tw-icon">{icon}</span>'
            f'<span class="tw-name">{label}</span>'
            f'<span class="tw-pattern">{pattern}</span>'
            f'<span class="tool-widget-chevron">&#9654;</span>'
            f'</summary>{body}</details>'
        )

    # --- WebFetch / WebSearch ---
    if n in ("webfetch", "websearch"):
        label = "WebFetch" if n == "webfetch" else "WebSearch"
        url = _esc(str(inp.get("url", inp.get("query", "")))[:100])
        body = ""
        if result_text:
            body = f'<div class="tool-widget-body">{_esc(result_text[:3000])}</div>'
        return (
            f'<details class="tool-widget {widget_cls}"{open_attr}>'
            f'<summary class="tool-widget-header">'
            f'<span class="tw-icon">{icon}</span>'
            f'<span class="tw-name">{label}</span>'
            f'<span class="tw-url">{url}</span>'
            f'<span class="tool-widget-chevron">&#9654;</span>'
            f'</summary>{body}</details>'
        )

    # --- Skill / Agent / Task ---
    if n in ("skill", "agent", "task", "toolsearch"):
        summary_param = _tool_summary(tool_name, inp)
        body = ""
        if result_text:
            body = f'<div class="tool-widget-body">{_esc(result_text[:3000])}</div>'
        return (
            f'<details class="tool-widget {widget_cls}"{open_attr}>'
            f'<summary class="tool-widget-header">'
            f'<span class="tw-icon">{icon}</span>'
            f'<span class="tw-name">{_esc(tool_name)}</span>'
            f'<span class="tool-widget-param">{summary_param}</span>'
            f'<span class="tool-widget-chevron">&#9654;</span>'
            f'</summary>{body}</details>'
        )

    # --- Fallback: generic tool-call style ---
    summary_param = _tool_summary(tool_name, inp)
    body = ""
    if result_text:
        body = f'<div class="tool-call-body">{_esc(result_text[:3000])}</div>'
    return (
        f'<details class="tool-call"{open_attr}>'
        f'<summary class="tool-call-header">'
        f'<span class="tool-call-icon">{icon}</span>'
        f'<span class="tool-call-name">{_esc(tool_name)}</span>'
        f'<span class="tool-call-param">{summary_param}</span>'
        f'<span class="tool-call-chevron">&#9654;</span>'
        f'</summary>{body}</details>'
    )


def _js_esc(text: str) -> str:
    """Escape a string for safe embedding inside a JS string literal (double-quoted)."""
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("<", "\\x3c")
        .replace(">", "\\x3e")
    )


# Track current block type per task for content_block_stop handling
_current_block_type: dict[str, str] = {}  # task_id -> "text" | "thinking" | "tool_use"


def _sse_html_for_event(et: str, data: dict, task_id: str = "") -> str:
    """Convert a task event into an HTML fragment for SSE live output."""
    event_data = data.get("event", {})
    if not isinstance(event_data, dict):
        return ""

    ctype = event_data.get("type", "")

    # Errors
    if event_data.get("error"):
        text = event_data["error"]
        return (
            f'<div class="msg-card msg-system">'
            f'<div class="msg-icon">&#10007;</div>'
            f'<div class="msg-content" style="color:var(--danger);">{_esc(text)}</div>'
            f'</div>'
        )
    if event_data.get("stderr"):
        text = event_data["stderr"]
        return (
            f'<div class="msg-card msg-system">'
            f'<div class="msg-icon">&#10007;</div>'
            f'<div class="msg-content" style="color:var(--danger);">{_esc(text)}</div>'
            f'</div>'
        )

    # --- Streaming token deltas (typing effect) ---
    if ctype == "content_block_delta":
        delta = event_data.get("delta", {})
        text = delta.get("text", "")
        thinking = delta.get("thinking", "")
        if text:
            js_text = json.dumps(text)
            return f'<script>window.__chatAppend({js_text})</script>'
        if thinking:
            js_thinking = json.dumps(thinking)
            return (
                '<script>'
                f'(function(){{var el=document.getElementById("thinking-active");if(el)el.textContent+={js_thinking}}})()'
                '</script>'
            )
        return ""

    # --- Block boundaries ---
    if ctype == "content_block_start":
        cb = event_data.get("content_block", {})
        cb_type = cb.get("type", "")
        if task_id:
            _current_block_type[task_id] = cb_type
        if cb_type == "tool_use":
            tool_name = cb.get("name", "tool")
            inp = cb.get("input", {})
            # Use specialized widget HTML
            return _build_tool_widget_html(tool_name, inp)
        if cb_type == "thinking":
            return (
                '<details class="thinking-block" open>'
                '<summary><span class="thinking-icon">&#10024;</span>'
                '<span class="thinking-label">Thinking...</span></summary>'
                '<div class="thinking-content" id="thinking-active"></div></details>'
                '<script>(function(){var w=document.getElementById("working-indicator");if(w)w.style.display="none";})()</script>'
            )
        # text block start -- open a message card with markdown render target
        # Also signal progress pill timer start
        return (
            '<div class="msg-card msg-assistant">'
            '<div class="msg-icon">&#9656;</div>'
            '<div class="msg-content"><div class="md-render" id="current-text-block"></div></div>'
            '</div>'
            '<script>(function(){var w=document.getElementById("working-indicator");if(w)w.style.display="none";})()'
            'if(typeof window.__progressStart==="function")window.__progressStart();</script>'
        )

    if ctype == "content_block_stop":
        block_type = _current_block_type.pop(task_id, "text") if task_id else "text"
        if block_type == "thinking":
            return (
                '<script>(function(){'
                'var s=document.querySelector(".thinking-block:last-of-type .thinking-label");'
                'if(s)s.textContent="Thought process";'
                'var i=document.querySelector(".thinking-block:last-of-type .thinking-icon");'
                'if(i)i.innerHTML="&#129504;";'
                'var el=document.getElementById("thinking-active");'
                'if(el)el.removeAttribute("id");'
                '})()</script>'
            )
        if block_type == "text":
            return (
                '<script>window.__chatReset()</script>'
                '<script>document.querySelector(".typing-cursor")?.remove()</script>'
            )
        # tool_use or other — just a spacer
        return '<div style="margin-bottom:0.3rem;"></div>'

    # --- Full assistant message (fallback if partials not available) ---
    if ctype == "assistant":
        msg = event_data.get("message", {})
        if isinstance(msg, dict):
            parts: list[str] = []
            for c in msg.get("content", []):
                if c.get("type") == "text":
                    text = _esc(c["text"])
                    parts.append(
                        f'<div class="msg-card msg-assistant">'
                        f'<div class="msg-icon">&#9656;</div>'
                        f'<div class="msg-content"><div class="md-render">{text}</div></div>'
                        f'</div>'
                    )
                elif c.get("type") == "tool_use":
                    parts.append(_build_tool_widget_html(
                        c.get("name", "tool"), c.get("input", {})
                    ))
            return "".join(parts)
        elif isinstance(event_data.get("content"), str):
            text = _esc(event_data["content"])
            return (
                f'<div class="msg-card msg-assistant">'
                f'<div class="msg-icon">&#9656;</div>'
                f'<div class="msg-content"><div class="md-render">{text}</div></div>'
                f'</div>'
            )
        return ""

    # --- Tool result (type: "user") --- inject as turn separator + result
    if ctype == "user":
        parts: list[str] = []
        # New format: tool_use_results (list)
        results_list = event_data.get("tool_use_results")
        if results_list and isinstance(results_list, list):
            for tr in results_list:
                content = tr.get("content") if isinstance(tr, dict) else tr
                result_text = _extract_result_text(content)
                truncated = _esc(result_text[:3000])
                parts.append(
                    '<details class="tool-call" style="border-left:2px solid var(--success);">'
                    '<summary class="tool-call-header">'
                    '<span class="tool-call-icon">&#9664;</span>'
                    '<span class="tool-call-name" style="color:var(--success);">Tool Result</span>'
                    f'<span class="tool-call-param">{len(result_text)} chars</span>'
                    '<span class="tool-call-chevron">&#9654;</span>'
                    '</summary>'
                    f'<div class="tool-call-body">{truncated}</div>'
                    '</details>'
                )
        # Legacy format: tool_use_result (singular)
        raw = event_data.get("tool_use_result")
        if raw is not None and not parts:
            result_text = _extract_result_text(raw)
            truncated = _esc(result_text[:3000])
            parts.append(
                '<details class="tool-call" style="border-left:2px solid var(--success);">'
                '<summary class="tool-call-header">'
                '<span class="tool-call-icon">&#9664;</span>'
                '<span class="tool-call-name" style="color:var(--success);">Tool Result</span>'
                f'<span class="tool-call-param">{len(result_text)} chars</span>'
                '<span class="tool-call-chevron">&#9654;</span>'
                '</summary>'
                f'<div class="tool-call-body">{truncated}</div>'
                '</details>'
            )
        if parts:
            return "".join(parts)

    # --- AskUserQuestion ---
    if ctype == "ask_user_question":
        questions = event_data.get("questions", [])
        parts_html: list[str] = []
        for q in questions:
            label = _esc(q.get("question", "?"))
            opts = q.get("options", [])
            opts_html = "".join(
                f"<li>{_esc(o.get('label', '?'))}</li>" for o in opts
            )
            parts_html.append(f"<strong>{label}</strong><ul>{opts_html}</ul>")
        body = "".join(parts_html) if parts_html else "(awaiting question data)"
        return (
            '<div class="msg-card msg-assistant">'
            '<div class="msg-icon">&#10067;</div>'
            f'<div class="msg-content">{body}</div>'
            '</div>'
        )

    # --- Permission request ---
    if ctype == "permission_request":
        return (
            '<div class="msg-card msg-system">'
            '<div class="msg-icon">&#128274;</div>'
            '<div class="msg-content" style="color:var(--warning);">Awaiting tool approval...</div>'
            '</div>'
        )

    # --- User message (echoed from API) ---
    if ctype == "user_message":
        text = _esc(event_data.get("text", ""))
        if text:
            return (
                '<div class="turn-separator"><span class="turn-label">---</span></div>'
                f'<div class="msg-card msg-user">'
                f'<div class="msg-icon">U</div>'
                f'<div class="msg-content"><div class="md-render">{text}</div></div>'
                f'</div>'
            )
        return ""

    # --- Result --- includes per-message token/cost + progress pill stop + turn separator
    if ctype == "result" and isinstance(event_data.get("result"), str):
        # Per-message token/cost footer
        inp_tok = event_data.get("input_tokens", 0) or 0
        out_tok = event_data.get("output_tokens", 0) or 0
        cost = event_data.get("cost_usd", 0) or 0
        token_html = ""
        if inp_tok or out_tok:
            cost_str = f"${cost:.4f}" if cost else ""
            token_html = (
                f'<div class="msg-tokens">'
                f'<span>{inp_tok:,} in</span>'
                f'<span>{out_tok:,} out</span>'
                f'{f"<span>{cost_str}</span>" if cost_str else ""}'
                f'</div>'
            )
        # Turn separator after result
        turn_sep = '<div class="turn-separator"><span class="turn-label">---</span></div>'
        progress_stop = '<script>if(typeof window.__progressStop==="function")window.__progressStop();</script>'
        return (
            f'{token_html}'
            f'{progress_stop}'
            f'{turn_sep}'
        )

    # Task completed/failed from manager events
    if et == EVENT_TASK_COMPLETED:
        return (
            '<div class="msg-card msg-system">'
            '<div class="msg-icon">&#10003;</div>'
            '<div class="msg-content" style="color:var(--success);">--- Task completed ---</div>'
            '</div>'
        )
    if et == EVENT_TASK_FAILED:
        reason = data.get("stderr", data.get("reason", ""))
        extra = f": {_esc(reason[:200])}" if reason else ""
        return (
            f'<div class="msg-card msg-system">'
            f'<div class="msg-icon">&#10005;</div>'
            f'<div class="msg-content" style="color:var(--danger);">--- Task failed{extra} ---</div>'
            f'</div>'
        )

    return ""


from taktis.core.views import html_escape as _esc  # noqa: E402


import re as _re

_NAME_PATTERN = _re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9 _.-]{0,79}$')


def _validate_name(value: str, label: str = "Name") -> str:
    """Validate a user-supplied name (project, phase, expert).

    Returns the stripped name or raises ValueError with a user-friendly message.
    """
    value = value.strip()
    if not value:
        raise ValueError(f"{label} is required")
    if len(value) > 80:
        raise ValueError(f"{label} must be 80 characters or fewer")
    if not _NAME_PATTERN.match(value):
        raise ValueError(
            f"{label} must start with a letter or digit and contain only "
            f"letters, digits, spaces, hyphens, underscores, or dots"
        )
    return value


def _js_str(text: str) -> str:
    """Escape a string for embedding in a JS string literal."""
    import json
    return json.dumps(text)


# ======================================================================
# Error helpers
# ======================================================================


def _user_message(exc: Exception) -> str:
    """Return a safe user-facing message for any exception.

    :class:`TaktisError` subclasses are formatted via the curated
    ``format_error_for_user`` lookup so internal details (SQL, file paths,
    tracebacks) are never exposed.  Plain :class:`ValueError` /
    :class:`KeyError` raised from input validation in the routes or the
    Taktis layer are passed through as-is because they already carry
    user-safe text (e.g. "A phase is required for all tasks").
    """
    if isinstance(exc, TaktisError):
        return format_error_for_user(exc)
    return str(exc)


#: Standalone 500 error page — matches the dark theme from ``base.html``
#: (same CSS variables, no external dependencies).
_500_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Server Error \u2014 Taktis</title>
  <style>
    :root {{
      --bg: #1a1a2e; --bg-surface: #16213e; --text: #eee;
      --danger: #e74c3c; --border: #2a3a5c; --radius: 6px;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh;
    }}
    .box {{
      background: var(--bg-surface); border: 1px solid var(--border);
      border-top: 3px solid var(--danger); border-radius: var(--radius);
      padding: 2rem 2.5rem; max-width: 480px; width: 100%; text-align: center;
    }}
    h1 {{ color: var(--danger); margin-bottom: 0.75rem; font-size: 1.4rem; font-weight: 700; }}
    p {{ color: #aaa; font-size: 0.9rem; line-height: 1.6; }}
    a {{ color: #5dade2; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>&#9888; Server Error</h1>
    <p>{message}</p>
    <p style="margin-top:1.25rem;"><a href="/">&#8592; Back to dashboard</a></p>
  </div>
</body>
</html>"""


async def _handle_500(request: Request, exc: Exception) -> HTMLResponse:
    """Global Starlette exception handler for all unhandled 500-level errors.

    Logs the full traceback via ``logger.error`` and returns a user-friendly
    HTML response that is consistent with the existing htmx patterns:

    - **htmx requests** receive an error toast injected into the
      ``#toast-area`` element present in ``base.html`` so the user sees the
      failure inline without a full-page reload.
    - **Full-page requests** receive a standalone styled 500 HTML page that
      matches the dark theme from ``base.html``.

    Internal details (tracebacks, SQL, file paths) are never included in the
    response body — only the curated ``format_error_for_user`` summary.
    """
    logger.error(
        "Unhandled exception in %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    user_msg = format_error_for_user(exc)
    if request.headers.get("HX-Request"):
        fragment = (
            '<div class="toast" style="border-left-color:var(--danger);">'
            f"<strong>&#9888; Server Error</strong><br>{_esc(user_msg)}"
            "</div>"
        )
        return HTMLResponse(
            fragment,
            status_code=200,
            headers={"HX-Retarget": "#toast-area", "HX-Reswap": "afterbegin"},
        )
    return HTMLResponse(_500_PAGE.format(message=_esc(user_msg)), status_code=500)


async def event_stream_task(request: Request):
    """GET /events/task/{task_id} -- SSE stream of HTML fragments for live output."""
    o = _orch()
    task_id = request.path_params["task_id"]

    watched = [EVENT_TASK_OUTPUT, EVENT_TASK_STARTED, EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, EVENT_TASK_CHECKPOINT]
    queues = [(et, o.event_bus.subscribe(et)) for et in watched]
    merged: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=5000)

    async def relay(et: str, q: asyncio.Queue) -> None:
        try:
            while True:
                event = await q.get()
                data = event.get("data", {})
                if data.get("task_id") == task_id:
                    await merged.put((et, data))
        except asyncio.CancelledError:
            return

    relay_tasks = [asyncio.create_task(relay(et, q)) for et, q in queues]
    for _rt in relay_tasks:
        _rt.add_done_callback(_on_relay_done)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    et, data = await asyncio.wait_for(merged.get(), timeout=15)
                    html = _sse_html_for_event(et, data, task_id=task_id)
                    if html:
                        yield f"data: {html}\n\n"
                    # Send named events so client can reload on status change
                    if et in (EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, EVENT_TASK_CHECKPOINT):
                        yield f"event: done\ndata: {et}\n\n"
                    if et == EVENT_TASK_STARTED:
                        yield f"event: started\ndata: {et}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            for t in relay_tasks:
                t.cancel()
            for et, q in queues:
                o.event_bus.unsubscribe(et, q)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ======================================================================
# Project SSE endpoint (JSON-based live status updates)
# ======================================================================

# Events that trigger project page updates (not task.output — too noisy)
PROJECT_SSE_EVENTS = [
    EVENT_TASK_STARTED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_CHECKPOINT,
    EVENT_PHASE_STARTED,
    EVENT_PHASE_COMPLETED,
    EVENT_PIPELINE_PLAN_READY,
    EVENT_PIPELINE_GATE_WAITING,
]


async def _build_project_updates(
    o: "Taktis", et: str, data: dict, project_name: str,
    *,
    _cache: dict[str, tuple[float, Any]] | None = None,
) -> dict[str, str]:
    """Build a dict of element_id → HTML for a project status change event.

    The client JS iterates over the dict and replaces each element by ID.
    Special keys starting with 'new-task-row-' are appended to the phase tbody.

    *_cache* is an optional per-connection dict of ``{key: (expiry, value)}``
    used to avoid redundant DB queries for the same task/project within a
    short window.
    """
    import time as _time
    from taktis.core.context import _ctx_dir

    _CACHE_TTL = 0.5  # seconds — fresh enough for UI updates

    async def _cached_get_task(tid: str) -> dict | None:
        if _cache is not None:
            key = f"task:{tid}"
            entry = _cache.get(key)
            if entry and entry[0] > _time.monotonic():
                return entry[1]
        result = await o.get_task(tid)
        if _cache is not None:
            _cache[f"task:{tid}"] = (_time.monotonic() + _CACHE_TTL, result)
        return result

    async def _cached_get_project(name: str) -> dict | None:
        if _cache is not None:
            key = f"proj:{name}"
            entry = _cache.get(key)
            if entry and entry[0] > _time.monotonic():
                return entry[1]
        result = await o.get_project(name)
        if _cache is not None:
            _cache[f"proj:{name}"] = (_time.monotonic() + _CACHE_TTL, result)
        return result

    updates: dict[str, str] = {}
    task_id = data.get("task_id", "")

    # Task-level updates
    if task_id and et in (EVENT_TASK_STARTED, EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, EVENT_TASK_CHECKPOINT):
        task = await _cached_get_task(task_id)
        if task:
            status = task["status"]
            # Status badge
            updates[f"task-badge-{task_id}"] = (
                f'<span id="task-badge-{task_id}" class="badge badge-{status}">{status}</span>'
            )
            # Cost
            cost = task.get("cost_usd")
            updates[f"task-cost-{task_id}"] = (
                f'<span id="task-cost-{task_id}">${cost:.4f}</span>' if cost
                else f'<span id="task-cost-{task_id}">\u2014</span>'
            )
            # Context (percent used — peak single-turn input, model-aware window)
            peak = task.get("peak_input_tokens") or 0
            ctx_window = _get_context_window(task.get("model"))
            if peak > 0:
                pct = round((peak / ctx_window) * 100)
                ctx_html = f'<span id="task-context-{task_id}">{pct}%</span>'
            else:
                ctx_html = f'<span id="task-context-{task_id}">\u2014</span>'
            updates[f"task-context-{task_id}"] = ctx_html
            # Action buttons
            if status in ("pending", "failed"):
                label = "Retry" if status == "failed" else "Start"
                btn_html = (
                    f'<span id="task-actions-{task_id}">'
                    f'<button class="btn-sm btn-success" hx-post="/api/tasks/{task_id}/start" hx-swap="none">{label}</button>'
                    f'</span>'
                )
            elif status == "completed":
                btn_html = (
                    f'<span id="task-actions-{task_id}">'
                    f'<button class="btn-sm" hx-post="/api/tasks/{task_id}/start" hx-confirm="Re-run this task?" hx-swap="none">Re-run</button>'
                    f'</span>'
                )
            elif status == "running":
                btn_html = (
                    f'<span id="task-actions-{task_id}">'
                    f'<button class="btn-sm btn-danger" hx-post="/api/tasks/{task_id}/stop" hx-swap="none">Stop</button>'
                    f'</span>'
                )
            elif status == "awaiting_input":
                btn_html = (
                    f'<span id="task-actions-{task_id}">'
                    f'<button class="btn-sm" onclick="window.location=\'/tasks/{task_id}\'">Reply</button>'
                    f'</span>'
                )
            else:
                btn_html = f'<span id="task-actions-{task_id}"></span>'
            updates[f"task-actions-{task_id}"] = btn_html

            # New task row — for tasks created programmatically (review, pipeline)
            # Send a full <tr> that the client appends to the phase tbody
            if et == EVENT_TASK_STARTED:
                phase_id = task.get("phase_id")
                if phase_id:
                    async with get_session() as conn:
                        phase = await repo.get_phase_by_id(conn, phase_id)
                    if phase:
                        pn = phase["phase_number"]
                        prompt_short = _esc((task.get("prompt") or "\u2014")[:80])
                        expert_name = _esc(task.get("expert") or "\u2014")
                        created = _dt_filter(task.get("created_at"))
                        row_html = (
                            f'<tr id="task-row-{task_id}">'
                            f'<td><a href="/tasks/{task_id}" class="mono">{task_id[:12]}</a></td>'
                            f'<td><span id="task-badge-{task_id}" class="badge badge-{status}">{status}</span></td>'
                            f'<td>{task.get("wave", "\u2014")}</td>'
                            f'<td>{expert_name}</td>'
                            f'<td><span id="task-cost-{task_id}">\u2014</span></td>'
                            f'<td><span id="task-context-{task_id}">\u2014</span></td>'
                            f'<td>{created}</td>'
                            f'<td><span class="text-truncate">{prompt_short}</span></td>'
                            f'<td><span id="task-actions-{task_id}">'
                            f'<button class="btn-sm btn-danger" hx-post="/api/tasks/{task_id}/stop" hx-swap="none">Stop</button>'
                            f'</span></td>'
                            f'</tr>'
                        )
                        updates[f"new-task-row-{pn}"] = row_html

                        # Update phase task count
                        async with get_session() as conn:
                            all_tasks = await repo.get_tasks_by_phase(conn, phase_id)
                        updates[f"phase-task-count-{pn}"] = str(len(all_tasks))

    # Plan applied — new phases created, tell client to reload
    if data.get("status") == "plan_applied":
        updates["__reload__"] = "true"

    # Phase-level updates
    phase_id = data.get("phase_id")
    if phase_id:
            async with get_session() as conn:
                phase = await repo.get_phase_by_id(conn, phase_id)
                if phase:
                    pn = phase["phase_number"]
                    status = phase["status"] or "pending"
                    # Check for interrupted state (same logic as template)
                    if status == "in_progress" and phase.get("current_wave") is not None:
                        tasks = await repo.get_tasks_by_phase(conn, phase_id)
                        has_running = any(t["status"] in ("running", "awaiting_input") for t in tasks)
                        if not has_running:
                            updates[f"phase-badge-{pn}"] = (
                                f'<span id="phase-badge-{pn}" class="badge badge-interrupted">Interrupted</span>'
                            )
                        else:
                            updates[f"phase-badge-{pn}"] = (
                                f'<span id="phase-badge-{pn}" class="badge badge-{status}">{status}</span>'
                            )
                    else:
                        updates[f"phase-badge-{pn}"] = (
                            f'<span id="phase-badge-{pn}" class="badge badge-{status}">{status}</span>'
                        )

    # Plan ready — render approval button via SSE (both top fallback + inline after phases)
    if et == EVENT_PIPELINE_PLAN_READY:
        project_id = data.get("project_id", "")
        _approval_html = (
            '<div style="padding:1.25rem 1.5rem;margin:0.5rem 0 1rem;'
            'background:rgba(99,102,241,0.12);'
            'border:1px solid rgba(99,102,241,0.35);'
            'border-left:4px solid var(--accent);'
            'border-radius:8px;'
            'display:flex;align-items:center;justify-content:space-between;'
            'flex-wrap:wrap;gap:1rem;">'
            '<div style="flex:1;min-width:200px;">'
            '<strong style="font-size:1.1rem;color:var(--text-primary);">'
            'Pipeline plan is ready for review</strong>'
            '<p style="margin:0.35rem 0 0;color:var(--text-muted);font-size:0.9rem;">'
            'Review the requirements and roadmap above, then apply to start execution.</p>'
            '</div>'
            f'<button class="btn btn-primary" '
            f'style="font-size:1rem;padding:0.6rem 1.5rem;white-space:nowrap;" '
            f'hx-post="/api/pipelines/{project_id}/approve-plan" '
            f'hx-swap="none">'
            f'Apply Plan &amp; Start Execution</button>'
            '</div>'
        )
        # Target the inline approval div (after phases) — inject via JS
        updates["pipeline-progress"] = (
            f'<div id="pipeline-progress">'
            f'<script>'
            f'(function(){{'
            f'var target=document.getElementById("plan-approval-inline");'
            f'if(!target){{'
            f'var phases=document.querySelectorAll(".phase-card");'
            f'if(phases.length){{'
            f'target=document.createElement("div");'
            f'target.id="plan-approval-inline";'
            f'phases[phases.length-1].after(target);'
            f'}}}}'
            f'if(target){{target.innerHTML={json.dumps(_approval_html)};target.style.display="block";}}'
            f'}})();'
            f'</script></div>'
        )

    if et == EVENT_PIPELINE_GATE_WAITING:
        import html as _html
        project_id = data.get("project_id", "")
        node_id = data.get("node_id", "")
        node_name = data.get("node_name", "Human Gate")
        gate_msg = data.get("gate_message", "")
        preview = data.get("upstream_preview", "")
        preview_html = ""
        if preview:
            preview_html = (
                f'<pre class="gate-preview" style="max-height:200px;overflow:auto;'
                f'margin:0.5rem 0;padding:0.5rem;background:var(--bg-tertiary);'
                f'border-radius:4px;font-size:0.85rem;white-space:pre-wrap">'
                f'{_html.escape(preview)}</pre>'
            )
        updates["pipeline-progress"] = (
            '<div id="pipeline-progress" class="pipeline-progress">'
            f'<div class="pipeline-gate-waiting" style="padding:1rem;'
            f'border:1px solid var(--border-color);border-radius:8px;margin:0.5rem 0">'
            f'<strong>{_html.escape(node_name)}</strong>: '
            f'{_html.escape(gate_msg)}'
            f'{preview_html}'
            f'<div style="margin-top:0.5rem;display:flex;gap:0.5rem">'
            f'<button class="btn btn-sm btn-primary" '
            f'hx-post="/api/pipelines/{project_id}/approve-gate/{node_id}" '
            f'hx-target="#pipeline-progress" hx-swap="outerHTML">'
            f'Approve</button>'
            f'<button class="btn btn-sm btn-outline" '
            f'hx-post="/api/pipelines/{project_id}/reject-gate/{node_id}" '
            f'hx-target="#pipeline-progress" hx-swap="outerHTML">'
            f'Reject</button>'
            f'</div></div></div>'
        )

    # Project status badge — update on any event
    project_obj = await _cached_get_project(project_name)
    if project_obj:
        proj_status = project_obj.get("status") or "pending"
        updates["project-status-badge"] = (
            f'<span id="project-status-badge" class="badge badge-{proj_status}">{proj_status}</span>'
        )

    return updates


async def event_stream_project(request: Request):
    """GET /events/project/{name} — SSE stream of project status changes as JSON."""
    project_name = request.path_params["name"]
    o = _orch()

    # Build a set of task IDs belonging to this project for fast filtering
    project = await o.get_project(project_name)
    if project is None:
        return StreamingResponse(iter([]), media_type="text/event-stream")
    project_id = project["id"]

    queues = [(et, o.event_bus.subscribe(et)) for et in PROJECT_SSE_EVENTS]
    merged: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=5000)

    async def relay(et: str, q: asyncio.Queue) -> None:
        try:
            while True:
                event = await q.get()
                await merged.put((et, event))
        except asyncio.CancelledError:
            return

    relay_tasks = [asyncio.create_task(relay(et, q)) for et, q in queues]
    for _rt in relay_tasks:
        _rt.add_done_callback(_on_relay_done)

    async def generate():
        # Per-connection cache to avoid redundant DB lookups on rapid events
        sse_cache: dict[str, tuple[float, Any]] = {}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    et, event = await asyncio.wait_for(merged.get(), timeout=15)
                    data = event.get("data", {})

                    # Filter: only events for this project
                    evt_project_name = data.get("project_name", "")
                    evt_project_id = data.get("project_id", "")

                    if evt_project_name == project_name or evt_project_id == project_id:
                        pass  # matches
                    else:
                        # Fallback: look up project from task_id (manager events lack project_name)
                        task_id = data.get("task_id", "")
                        if task_id:
                            task = await o.get_task(task_id)
                            if not task or task.get("project_id") != project_id:
                                continue
                        else:
                            continue

                    # Build JSON update dict {element_id: html}
                    updates = await _build_project_updates(
                        o, et, data, project_name, _cache=sse_cache,
                    )
                    if updates:
                        yield f"event: status\ndata: {json.dumps(updates)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            for t in relay_tasks:
                t.cancel()
            for et, q in queues:
                o.event_bus.unsubscribe(et, q)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ======================================================================
# Consult routes (advisory chat sidebar)
# ======================================================================


async def _build_consult_system_prompt(
    context_type: str, context_id: str, context_data: dict
) -> str:
    """Build a system prompt for a consult session based on context type.

    Parameters
    ----------
    context_type:
        ``"task"`` or ``"project"``.
    context_id:
        Task ID (for ``"task"`` type) or empty string.
    context_data:
        Dict of additional context (project fields for ``"project"`` type).
    """
    if context_type == "task":
        # Load the task-advisor persona by role.
        persona_text = ""
        try:
            async with get_session() as conn:
                advisor = await repo.get_expert_by_role(conn, "task_advisor")
            if advisor and advisor.get("system_prompt"):
                persona_text = advisor["system_prompt"]
            else:
                persona_text = "You are a task response advisor."
        except Exception as exc:
            logger.warning("Could not load task-advisor persona: %s", exc)
            persona_text = "You are a task response advisor."

        task: dict | None = None
        expert_name: str = "default"
        async with get_session() as conn:
            task = await repo.get_task(conn, context_id)
            if task is not None and task.get("expert_id"):
                expert_row = await repo.get_expert_by_id(conn, task["expert_id"])
                if expert_row is not None:
                    expert_name = expert_row.get("name") or "default"
        if task is None:
            return CONSULT_TASK_PROMPT.format(
                persona=persona_text,
                task_name=context_id,
                expert="unknown",
                status="unknown",
                task_prompt="(task not found)",
                recent_output="",
            )
        # Extract text-only output: use the result_summary field, split on lines,
        # skip segments that look like tool_use JSON blocks, take last 20.
        raw_output = task.get("result_summary") or ""
        lines = raw_output.splitlines()
        text_segments = [
            line
            for line in lines
            if line.strip()
            and not line.strip().startswith("{")
            and not line.strip().startswith("[")
        ]
        recent_output = "\n".join(text_segments[-20:])[:4000]
        return CONSULT_TASK_PROMPT.format(
            persona=persona_text,
            task_name=task.get("name") or context_id,
            expert=expert_name,
            status=task.get("status") or "unknown",
            task_prompt=task.get("prompt") or "",
            recent_output=recent_output,
        )
    else:  # context_type == "project"
        # Load the project-advisor persona by role.
        persona_text = ""
        try:
            async with get_session() as conn:
                advisor = await repo.get_expert_by_role(conn, "project_advisor")
            if advisor and advisor.get("system_prompt"):
                persona_text = advisor["system_prompt"]
            else:
                persona_text = "You are a project setup advisor."
        except Exception as exc:
            logger.warning("Could not load project-advisor persona: %s", exc)
            persona_text = "You are a project setup advisor."
        # If context_id is a project ID, load project data from DB
        if context_id and not context_data.get("name"):
            try:
                async with get_session() as conn:
                    proj = await repo.get_project_by_id(conn, context_id)
                    if proj:
                        context_data = {
                            "name": proj.get("name", ""),
                            "description": proj.get("description", ""),
                            "working_dir": proj.get("working_dir", ""),
                        }
            except Exception:
                pass
        return CONSULT_PROJECT_PROMPT.format(
            persona=persona_text,
            name=context_data.get("name", ""),
            description=context_data.get("description", "(not yet filled in)"),
            working_dir=context_data.get("working_dir", ""),
            auto_plan=context_data.get("auto_plan", False),
            interview_mode=context_data.get("interview_mode", "simple"),
            research=context_data.get("research", False),
            verification=context_data.get("verification", False),
            phase_review=context_data.get("phase_review", False),
        )


async def api_create_consult(request: Request):
    """POST /api/consult -- create a new consult session."""
    registry = _consult_registry
    if registry is None:
        return JSONResponse({"error": "Consult registry not initialized"}, status_code=503)
    try:
        body = await request.json()
        context_type = str(body.get("context_type", "project"))
        context_id = str(body.get("context_id", ""))
        context_data = body.get("context_data", {})
        system_prompt = await _build_consult_system_prompt(context_type, context_id, context_data)
        # Set working_dir so the advisor can read project files
        working_dir = ""
        if context_type == "project" and context_id:
            try:
                async with get_session() as conn:
                    proj = await repo.get_project_by_id(conn, context_id)
                    if proj:
                        working_dir = proj.get("working_dir", "")
            except Exception:
                pass
        elif context_type == "task" and context_id:
            try:
                async with get_session() as conn:
                    task = await repo.get_task(conn, context_id)
                    if task and task.get("project_id"):
                        proj = await repo.get_project_by_id(conn, task["project_id"])
                        if proj:
                            working_dir = proj.get("working_dir", "")
            except Exception:
                pass
        session = registry.create(system_prompt=system_prompt, working_dir=working_dir, model="haiku")
        return JSONResponse({"token": session.token})
    except ConsultError as exc:
        return JSONResponse({"error": format_error_for_user(exc)}, status_code=400)
    except Exception as exc:
        logger.error("api_create_consult failed: %s", exc, exc_info=exc)
        return JSONResponse({"error": format_error_for_user(exc)}, status_code=400)


async def api_send_consult(request: Request):
    """POST /api/consult/{token}/send -- send a message to a consult session."""
    registry = _consult_registry
    if registry is None:
        return JSONResponse({"error": "Consult registry not initialized"}, status_code=503)
    token = request.path_params["token"]
    session = registry.get(token)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        body = await request.json()
        message = str(body.get("message", ""))
        session.send(message)
        return JSONResponse({"ok": True})
    except ConsultError as exc:
        return JSONResponse({"error": format_error_for_user(exc)}, status_code=400)
    except Exception as exc:
        logger.error("api_send_consult failed: %s", exc, exc_info=exc)
        return JSONResponse({"error": "Failed to send message"}, status_code=500)


async def _consult_sse_generator(session, request):
    """Async generator yielding SSE events from a consult session stream."""
    try:
        async for chunk in session.stream_response():
            data = json.dumps({"type": "text", "text": chunk})
            yield f"data: {data}\n\n"
            if await request.is_disconnected():
                break
    finally:
        yield "event: done\ndata: \n\n"


async def sse_consult(request: Request):
    """GET /events/consult/{token} -- SSE stream for a consult session."""
    registry = _consult_registry
    if registry is None:
        return JSONResponse({"error": "Consult registry not initialized"}, status_code=503)
    token = request.path_params["token"]
    session = registry.get(token)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return StreamingResponse(
        content=_consult_sse_generator(session, request),
        media_type="text/event-stream",
    )


async def api_delete_consult(request: Request):
    """DELETE /api/consult/{token} -- stop and remove a consult session."""
    registry = _consult_registry
    if registry is None:
        return JSONResponse({"error": "Consult registry not initialized"}, status_code=503)
    token = request.path_params["token"]
    registry.remove(token)
    return JSONResponse({"ok": True})


# ======================================================================
# App factory
# ======================================================================


# ======================================================================
# CSRF Protection (double-submit cookie)
# ======================================================================

import secrets

_CSRF_COOKIE = "csrf_token"
_CSRF_HEADER = "X-CSRFToken"
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


# ---------------------------------------------------------------------------
# Rate-limiting middleware (in-memory sliding window per IP)
# ---------------------------------------------------------------------------

class ContentSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with bodies exceeding *max_bytes* (default 2 MB).

    Prevents memory exhaustion from oversized POST payloads. Only checks
    requests with a Content-Length header; chunked transfers without
    Content-Length are allowed through (Starlette's form parser has its
    own limits).
    """

    def __init__(self, app, max_bytes: int = 2 * 1024 * 1024) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    return HTMLResponse(
                        '<div class="error">Request body too large.</div>',
                        status_code=413,
                    )
            except (ValueError, TypeError):
                pass  # malformed header — let Starlette handle it
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-IP rate limiting for mutation endpoints + SSE cap.

    - Mutation requests (POST/PUT/DELETE): max *mutation_rpm* per minute.
    - SSE connections (/events*): max *max_sse_per_ip* concurrent per IP.
    """

    def __init__(
        self,
        app,
        mutation_rpm: int = 60,
        max_sse_per_ip: int = 10,
    ) -> None:
        super().__init__(app)
        self._mutation_rpm = mutation_rpm
        self._max_sse_per_ip = max_sse_per_ip
        # IP → list of request timestamps (mutation endpoints only)
        self._mutation_log: dict[str, list[float]] = {}
        # IP → active SSE connection count
        self._sse_connections: dict[str, int] = {}

    def _client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        client = request.client
        return client.host if client else "unknown"

    def _prune_old(self, timestamps: list[float], now: float) -> list[float]:
        cutoff = now - 60.0
        return [t for t in timestamps if t > cutoff]

    async def dispatch(self, request: Request, call_next) -> Response:
        ip = self._client_ip(request)
        path = request.url.path
        method = request.method
        now = time.monotonic()

        # --- SSE connection cap ---
        is_sse = path.startswith("/events")
        if is_sse:
            current = self._sse_connections.get(ip, 0)
            if current >= self._max_sse_per_ip:
                return HTMLResponse(
                    "Too many SSE connections", status_code=429,
                )
            self._sse_connections[ip] = current + 1
            try:
                return await call_next(request)
            finally:
                self._sse_connections[ip] = max(self._sse_connections.get(ip, 1) - 1, 0)

        # --- Mutation rate limit ---
        if method in ("POST", "PUT", "DELETE"):
            timestamps = self._prune_old(self._mutation_log.get(ip, []), now)
            if len(timestamps) >= self._mutation_rpm:
                return HTMLResponse(
                    '<div class="error">Rate limit exceeded. Please wait a moment.</div>',
                    status_code=429,
                )
            timestamps.append(now)
            self._mutation_log[ip] = timestamps

        return await call_next(request)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection.

    On every response, sets a ``csrf_token`` cookie with a random token.
    On POST/PUT/DELETE requests, requires the token to be echoed back
    via the ``X-CSRFToken`` header.  htmx reads it from the cookie and
    sends it automatically when configured with ``hx-headers``.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        method = request.method

        # Validate token on state-changing requests
        if method not in _CSRF_SAFE_METHODS:
            cookie_token = request.cookies.get(_CSRF_COOKIE, "")
            header_token = request.headers.get(_CSRF_HEADER, "")
            if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
                return HTMLResponse(
                    '<div class="error">CSRF validation failed. Please refresh the page and try again.</div>',
                    status_code=403,
                )

        response = await call_next(request)

        # Set or refresh the CSRF cookie on every response
        token = request.cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(32)
        # httponly=False is intentional: the double-submit cookie pattern
        # requires JavaScript to read the cookie and send it as the
        # X-CSRFToken header.  Setting httponly=True would break CSRF.
        response.set_cookie(
            _CSRF_COOKIE, token, path="/", samesite="strict", httponly=False,
        )
        return response


# ---------------------------------------------------------------------------
# Pipeline template routes
# ---------------------------------------------------------------------------


async def page_pipelines(request: Request) -> HTMLResponse:
    """GET /pipelines — pipeline editor page."""
    o = _orch()
    from taktis.core.node_types import list_node_types
    from taktis.core.graph_executor import (
        get_template_variables, get_template_texts, get_template_list,
    )
    async with get_session() as conn:
        pipeline_templates = await repo.list_pipeline_templates(conn)
    node_types = [nt.to_dict() for nt in list_node_types()]
    experts = await o.list_experts()
    projects = await o.list_projects()
    template_variables = await get_template_variables(get_session)
    template_texts = await get_template_texts(get_session)
    agent_templates = await get_template_list(get_session)
    return templates.TemplateResponse(
        "pipelines.html",
        {
            "request": request,
            "pipeline_templates": pipeline_templates,
            "node_types": node_types,
            "experts": experts,
            "projects": projects,
            "template_variables": template_variables,
            "template_texts": template_texts,
            "agent_templates": agent_templates,
            "active_page": "pipelines",
        },
    )


async def api_list_pipeline_templates(request: Request) -> JSONResponse:
    """GET /api/pipeline-templates — list all templates."""
    from taktis.core.env_vars import enrich_template
    async with get_session() as conn:
        templates_list = await repo.list_pipeline_templates(conn)
    for t in templates_list:
        enrich_template(t)
    return JSONResponse(templates_list)


async def api_create_pipeline_template(request: Request) -> JSONResponse:
    """POST /api/pipeline-templates — create a new template."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)

    flow_json = body.get("flow_json", "{}")
    if isinstance(flow_json, dict):
        flow_json = json.dumps(flow_json)

    async with get_session() as conn:
        try:
            tmpl = await repo.create_pipeline_template(conn, {
                "name": name,
                "description": body.get("description", ""),
                "flow_json": flow_json,
                "is_default": body.get("is_default", False),
            })
        except TaktisError as exc:
            return JSONResponse({"error": format_error_for_user(exc)}, status_code=400)
    from taktis.core.env_vars import enrich_template
    return JSONResponse(enrich_template(tmpl), status_code=201)


async def api_update_pipeline_template(request: Request) -> JSONResponse:
    """PUT /api/pipeline-templates/{id} — update a template."""
    template_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    flow_json = body.get("flow_json")
    if isinstance(flow_json, dict):
        flow_json = json.dumps(flow_json)

    kwargs = {}
    if "name" in body:
        kwargs["name"] = body["name"]
    if "description" in body:
        kwargs["description"] = body["description"]
    if flow_json is not None:
        kwargs["flow_json"] = flow_json
    if "is_default" in body:
        kwargs["is_default"] = body["is_default"]

    async with get_session() as conn:
        try:
            tmpl = await repo.update_pipeline_template(conn, template_id, **kwargs)
        except TaktisError as exc:
            return JSONResponse({"error": format_error_for_user(exc)}, status_code=400)
    if tmpl is None:
        return JSONResponse({"error": "Template not found"}, status_code=404)
    from taktis.core.env_vars import enrich_template
    return JSONResponse(enrich_template(tmpl))


async def api_delete_pipeline_template(request: Request) -> JSONResponse:
    """DELETE /api/pipeline-templates/{id} — delete a template."""
    template_id = request.path_params["id"]
    async with get_session() as conn:
        tmpl = await repo.get_pipeline_template(conn, template_id)
        if tmpl is None:
            return JSONResponse({"error": "Template not found"}, status_code=404)
        if tmpl.get("is_default"):
            return JSONResponse({"error": "Cannot delete a default template"}, status_code=400)
        await repo.delete_pipeline_template(conn, template_id)
    return JSONResponse({"ok": True})


async def api_list_node_types(request: Request) -> JSONResponse:
    """GET /api/node-types — return all registered node types."""
    from taktis.core.node_types import list_node_types
    return JSONResponse([nt.to_dict() for nt in list_node_types()])


async def api_execute_flow(request: Request) -> JSONResponse:
    """POST /api/projects/{name}/execute-flow — execute a template against a project."""
    o = _orch()
    project_name = request.path_params["name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    template_id = body.get("template_id")
    flow_json = body.get("flow_json")
    template_name = body.get("template_name", "Custom Flow")

    if template_id:
        async with get_session() as conn:
            tmpl = await repo.get_pipeline_template(conn, template_id)
        if tmpl is None:
            return JSONResponse({"error": "Template not found"}, status_code=404)
        flow_json = tmpl["flow_json"]
        template_name = tmpl["name"]

    if not flow_json:
        return JSONResponse({"error": "No flow_json or template_id provided"}, status_code=400)

    try:
        result = await o.execute_flow(
            project_name, flow_json, template_name=template_name,
        )
    except (ValueError, TaktisError) as exc:
        return JSONResponse({"error": format_error_for_user(exc)}, status_code=400)

    if isinstance(result, list):
        return JSONResponse({"phase_ids": result, "phase_id": result[0] if result else None})
    return JSONResponse({"phase_id": result})


# ======================================================================
# Schedule routes
# ======================================================================

async def page_schedules(request: Request):
    """GET /schedules -- scheduled pipeline runs."""
    o = _orch()
    session_factory = o._execution_service._session_factory
    async with session_factory() as conn:
        schedules = await repo.list_schedules(conn)
        pipeline_templates = await repo.list_pipeline_templates(conn)
    projects = await o.list_projects()
    return templates.TemplateResponse(
        "schedules.html",
        {
            "request": request,
            "schedules": schedules,
            "templates": pipeline_templates,
            "projects": projects,
            "active_page": "schedules",
        },
    )


async def api_create_schedule(request: Request):
    """POST /api/schedules -- create a scheduled pipeline run."""
    o = _orch()
    import uuid
    body = await request.json()

    schedule_id = uuid.uuid4().hex[:8]
    name = body.get("name", "").strip()
    project_name = body.get("project_name", "").strip()
    template_id = body.get("template_id", "").strip()

    if not name or not project_name or not template_id:
        return JSONResponse(
            {"error": "Name, project, and pipeline template are required"},
            status_code=400,
        )

    # Validate: check for interactive nodes
    session_factory = o._execution_service._session_factory
    from taktis.core.cron_scheduler import detect_interactive_nodes

    async with session_factory() as conn:
        pipeline_templates = await repo.list_pipeline_templates(conn)
    template = next((t for t in pipeline_templates if t["id"] == template_id), None)
    if template:
        flow = (
            json.loads(template["flow_json"])
            if isinstance(template["flow_json"], str)
            else template["flow_json"]
        )
        interactive = detect_interactive_nodes(flow)
        if interactive:
            return JSONResponse(
                {
                    "error": (
                        "Pipeline has interactive nodes that can't run headless: "
                        + ", ".join(interactive)
                    )
                },
                status_code=400,
            )

    async with session_factory() as conn:
        await repo.create_schedule(
            conn,
            schedule_id,
            name=name,
            project_name=project_name,
            template_id=template_id,
            frequency=body.get("frequency", "daily"),
            cron_expr=body.get("cron_expr"),
            time_of_day=body.get("time_of_day", "02:00"),
            day_of_week=body.get("day_of_week"),
        )
    return JSONResponse({"id": schedule_id, "status": "created"})


async def api_toggle_schedule(request: Request):
    """POST /api/schedules/{id}/toggle -- enable/disable a schedule."""
    o = _orch()
    schedule_id = request.path_params["id"]
    session_factory = o._execution_service._session_factory
    async with session_factory() as conn:
        schedule = await repo.get_schedule(conn, schedule_id)
        if not schedule:
            return JSONResponse({"error": "Not found"}, status_code=404)
        new_enabled = 0 if schedule["enabled"] else 1
        await repo.update_schedule(conn, schedule_id, enabled=new_enabled)
    if request.headers.get("HX-Request"):
        label = "Enabled" if new_enabled else "Disabled"
        cls = "badge-completed" if new_enabled else "badge-idle"
        return HTMLResponse(f'<span class="badge {cls}">{label}</span>')
    return JSONResponse({"enabled": bool(new_enabled)})


async def api_delete_schedule(request: Request):
    """DELETE /api/schedules/{id} -- delete a schedule."""
    o = _orch()
    schedule_id = request.path_params["id"]
    session_factory = o._execution_service._session_factory
    async with session_factory() as conn:
        await repo.delete_schedule(conn, schedule_id)
    if request.headers.get("HX-Request"):
        return HTMLResponse("")
    return JSONResponse({"status": "deleted"})


async def api_run_schedule_now(request: Request):
    """POST /api/schedules/{id}/run-now -- manually trigger a schedule."""
    o = _orch()
    schedule_id = request.path_params["id"]
    session_factory = o._execution_service._session_factory
    async with session_factory() as conn:
        schedule = await repo.get_schedule(conn, schedule_id)
    if not schedule:
        return JSONResponse({"error": "Not found"}, status_code=404)
    from datetime import datetime as dt, timezone as tz
    await o._cron_scheduler._trigger(schedule, dt.now(tz.utc))
    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="text-success" style="color:var(--success);">Triggered</span>')
    return JSONResponse({"status": "triggered"})


def create_app() -> Starlette:
    """Create and return the Starlette ASGI application."""

    routes = [
        # Page routes
        Route("/", page_dashboard, methods=["GET"]),
        Route("/projects", page_projects, methods=["GET"]),
        Route("/projects/{name}/timeline", page_project_timeline, methods=["GET"]),
        Route("/projects/{name}", page_project_detail, methods=["GET"]),
        Route("/tasks/{task_id}", page_task_detail, methods=["GET"]),
        Route("/experts", page_experts, methods=["GET"]),
        Route("/admin", page_admin, methods=["GET"]),
        # Partial routes (htmx fragments)
        Route("/partials/status-cards", partial_status_cards, methods=["GET"]),
        Route("/partials/active-tasks", partial_active_tasks, methods=["GET"]),
        Route("/partials/project-list", partial_project_list, methods=["GET"]),
        Route("/partials/task-output/{task_id}", partial_task_output, methods=["GET"]),
        Route("/partials/task-status/{task_id}", partial_task_status, methods=["GET"]),
        # API routes (form submissions)
        Route("/api/projects", api_create_project, methods=["POST"]),
        Route("/api/projects/{name}", api_delete_project, methods=["DELETE"]),
        Route("/api/projects/{name}/env-vars", api_update_project_env_vars, methods=["PATCH", "POST"]),
        Route("/api/projects/{name}/phases", api_create_phase, methods=["POST"]),
        Route("/api/projects/{name}/tasks", api_create_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/start", api_start_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/stop", api_stop_task, methods=["POST"]),

        Route("/api/tasks/{task_id}/approve", api_approve_checkpoint, methods=["POST"]),
        Route("/api/tasks/{task_id}/approve-answers", api_approve_answers, methods=["POST"]),
        Route("/api/tasks/{task_id}/deny", api_deny_tool, methods=["POST"]),
        Route("/api/tasks/{task_id}/input", api_send_input, methods=["POST"]),
        Route("/api/tasks/{task_id}/continue", api_continue_task, methods=["POST"]),
        Route("/api/phases/{project}/{number:int}/run", api_run_phase, methods=["POST"]),
        Route("/api/tasks/{task_id}/discuss", api_discuss_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/research", api_research_task, methods=["POST"]),
        Route("/api/projects/{name}/run", api_run_project, methods=["POST"]),
        Route("/api/interrupted", api_interrupted, methods=["GET"]),
        Route("/api/phases/{phase_id}/resume", api_resume_phase, methods=["POST"]),
        Route("/api/phases/{phase_id}/resume-pipeline", api_resume_pipeline, methods=["POST"]),
        Route("/api/pipelines/{project_id}/approve-plan", api_approve_pipeline_plan, methods=["POST"]),
        Route("/api/pipelines/{project_id}/approve-gate/{node_id}", api_approve_gate, methods=["POST"]),
        Route("/api/pipelines/{project_id}/reject-gate/{node_id}", api_reject_gate, methods=["POST"]),
        Route("/api/stop-all", api_stop_all, methods=["POST"]),
        Route("/api/factory-reset", api_factory_reset, methods=["POST"]),
        Route("/api/experts", api_create_expert, methods=["POST"]),
        Route("/api/experts/{name}/edit", api_update_expert, methods=["POST"]),
        Route("/api/experts/{name}/delete", api_delete_expert, methods=["POST"]),
        Route("/experts/{name}/edit", page_expert_edit, methods=["GET"]),
        # Agent template routes
        Route("/agent-templates", page_agent_templates, methods=["GET"]),
        Route("/api/agent-templates", api_create_agent_template, methods=["POST"]),
        Route("/api/agent-templates/{slug}/edit", api_update_agent_template, methods=["POST"]),
        Route("/api/agent-templates/{slug}/delete", api_delete_agent_template, methods=["POST"]),
        Route("/agent-templates/{slug}/edit", page_agent_template_edit, methods=["GET"]),
        # Schedule routes
        Route("/schedules", page_schedules, methods=["GET"]),
        Route("/api/schedules", api_create_schedule, methods=["POST"]),
        Route("/api/schedules/{id}/toggle", api_toggle_schedule, methods=["POST"]),
        Route("/api/schedules/{id}/run-now", api_run_schedule_now, methods=["POST"]),
        Route("/api/schedules/{id}", api_delete_schedule, methods=["DELETE"]),
        # Pipeline template routes
        Route("/pipelines", page_pipelines, methods=["GET"]),
        Route("/api/pipeline-templates", api_list_pipeline_templates, methods=["GET"]),
        Route("/api/pipeline-templates", api_create_pipeline_template, methods=["POST"]),
        Route("/api/pipeline-templates/{id}", api_update_pipeline_template, methods=["PUT"]),
        Route("/api/pipeline-templates/{id}", api_delete_pipeline_template, methods=["DELETE"]),
        Route("/api/node-types", api_list_node_types, methods=["GET"]),
        Route("/api/projects/{name}/execute-flow", api_execute_flow, methods=["POST"]),
        # SSE routes
        Route("/events", event_stream, methods=["GET"]),
        Route("/events/task/{task_id}", event_stream_task, methods=["GET"]),
        Route("/events/project/{name}", event_stream_project, methods=["GET"]),
        # Consult routes (advisory chat sidebar)
        Route("/api/consult", api_create_consult, methods=["POST"]),
        Route("/api/consult/{token}/send", api_send_consult, methods=["POST"]),
        Route("/events/consult/{token}", sse_consult, methods=["GET"]),
        Route("/api/consult/{token}", api_delete_consult, methods=["DELETE"]),
    ]

    # Mount static files if directory exists
    if STATIC_DIR.is_dir():
        routes.append(Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"))

    app = Starlette(
        routes=routes,
        lifespan=_lifespan,
        exception_handlers={Exception: _handle_500},
    )
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(ContentSizeLimitMiddleware)

    return app
