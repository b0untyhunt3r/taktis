"""Repository layer -- async CRUD helpers using raw aiosqlite."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from taktis.exceptions import DatabaseError, DuplicateError
from taktis.models import (
    Expert,
    Phase,
    PipelineTemplate,
    Project,
    ProjectState,
    Task,
    TaskOutput,
    TaskTemplate,
    _full_uuid,
    _short_uuid,
    _utcnow,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return _utcnow().isoformat()


def _json_dump(val) -> Optional[str]:
    if val is None:
        return None
    return json.dumps(val)


def _row_to_dict(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[aiosqlite.Row]) -> list[dict]:
    return [dict(r) for r in rows]


async def _execute(
    conn: aiosqlite.Connection,
    sql: str,
    params: tuple | list = (),
    *,
    label: str,
) -> aiosqlite.Cursor:
    """Execute *sql* on *conn*, mapping :class:`aiosqlite.Error` to typed exceptions.

    Logs the first line of the SQL template and the parameter count at ERROR
    level on failure.  Parameter *values* are intentionally omitted from log
    output to avoid leaking sensitive data.

    Parameters
    ----------
    conn:
        Active aiosqlite connection.
    sql:
        SQL statement to execute.
    params:
        Positional bind parameters.  Only the count is logged, never the values.
    label:
        Short description of the operation (e.g. ``"create_project"``), used
        in log messages and exception messages.

    Raises
    ------
    DuplicateError
        When the underlying error is a UNIQUE constraint violation.
    DatabaseError
        For any other :class:`aiosqlite.Error`.
    """
    try:
        return await conn.execute(sql, params)
    except aiosqlite.Error as exc:
        # Log only the first line of the SQL (avoids multi-line noise) and the
        # parameter count so the failing query is identifiable without exposing
        # any user-supplied values.
        sql_preview = sql.strip().splitlines()[0][:120]
        param_count = len(params) if params else 0
        logger.error(
            "DB error in %s — query: %s — params count: %d — %s: %s",
            label,
            sql_preview,
            param_count,
            type(exc).__name__,
            exc,
        )
        exc_str = str(exc)
        if "UNIQUE constraint failed" in exc_str:
            # Extract the table.column fragment from the SQLite error message
            # (e.g. "UNIQUE constraint failed: projects.name").  This is schema
            # information only — it never contains user-supplied values.
            constraint: str | None = None
            if "UNIQUE constraint failed:" in exc_str:
                constraint = exc_str.split("UNIQUE constraint failed:", 1)[1].strip()
            constraint_detail = f": {constraint}" if constraint else ""
            raise DuplicateError(
                f"Duplicate value in {label}{constraint_detail} — "
                "a record with that unique field already exists",
                constraint=constraint,
                cause=exc,
            ) from exc
        raise DatabaseError(
            f"Database error in {label} ({type(exc).__name__})",
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

async def create_project(conn: aiosqlite.Connection, **kwargs) -> dict:
    kwargs.setdefault("id", _full_uuid())
    now = _now_iso()
    kwargs.setdefault("created_at", now)
    kwargs.setdefault("updated_at", now)
    if "default_env_vars" in kwargs:
        kwargs["default_env_vars"] = _json_dump(kwargs["default_env_vars"])
    await _execute(
        conn,
        """INSERT INTO projects
           (id, name, description, working_dir, default_model,
            default_permission_mode, default_env_vars,
            planning_options, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kwargs["id"],
            kwargs["name"],
            kwargs.get("description"),
            kwargs.get("working_dir"),
            kwargs.get("default_model"),
            kwargs.get("default_permission_mode"),
            kwargs.get("default_env_vars"),
            kwargs.get("planning_options"),
            kwargs["created_at"],
            kwargs["updated_at"],
        ),
        label="create_project",
    )
    return await get_project_by_id(conn, kwargs["id"])


async def get_project_by_name(conn: aiosqlite.Connection, name: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM projects WHERE name = ?",
        (name,),
        label="get_project_by_name",
    )
    row = await cur.fetchone()
    return _row_to_dict(row)


async def get_project_by_id(conn: aiosqlite.Connection, id: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM projects WHERE id = ?",
        (id,),
        label="get_project_by_id",
    )
    row = await cur.fetchone()
    return _row_to_dict(row)


async def list_projects(conn: aiosqlite.Connection) -> list[dict]:
    cur = await _execute(
        conn,
        "SELECT * FROM projects ORDER BY created_at",
        label="list_projects",
    )
    return _rows_to_dicts(await cur.fetchall())


async def list_projects_summary(conn: aiosqlite.Connection) -> list[dict]:
    """Lightweight project list with counts — single query, no per-project enrichment."""
    cur = await _execute(
        conn,
        """
        SELECT
            p.id,
            p.name,
            p.description,
            p.working_dir,
            p.default_model,
            p.created_at,
            p.updated_at,
            COALESCE(ps.status, 'idle') AS project_status,
            COUNT(DISTINCT ph.id) AS phase_count,
            COUNT(DISTINCT t.id) AS task_count,
            SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed_tasks,
            SUM(CASE WHEN t.status = 'running' THEN 1 ELSE 0 END) AS running_tasks,
            SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed_tasks,
            COALESCE(SUM(t.cost_usd), 0.0) AS total_cost
        FROM projects p
        LEFT JOIN project_states ps ON ps.project_id = p.id
        LEFT JOIN phases ph ON ph.project_id = p.id
        LEFT JOIN tasks t ON t.project_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
        """,
        label="list_projects_summary",
    )
    return _rows_to_dicts(await cur.fetchall())


async def update_project(conn: aiosqlite.Connection, id: str, **kwargs) -> dict | None:
    if not kwargs:
        return await get_project_by_id(conn, id)
    kwargs["updated_at"] = _now_iso()
    if "default_env_vars" in kwargs:
        kwargs["default_env_vars"] = _json_dump(kwargs["default_env_vars"])
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [id]
    await _execute(
        conn,
        f"UPDATE projects SET {set_clause} WHERE id = ?",
        values,
        label="update_project",
    )
    return await get_project_by_id(conn, id)


async def delete_project(conn: aiosqlite.Connection, name: str) -> bool:
    cur = await _execute(
        conn,
        "DELETE FROM projects WHERE name = ?",
        (name,),
        label="delete_project",
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# ProjectState CRUD
# ---------------------------------------------------------------------------

async def create_project_state(conn: aiosqlite.Connection, project_id: str, **kwargs) -> dict:
    kwargs.setdefault("id", _full_uuid())
    kwargs["project_id"] = project_id
    kwargs.setdefault("status", "idle")
    for jf in ("decisions", "blockers", "metrics"):
        if jf in kwargs:
            kwargs[jf] = _json_dump(kwargs[jf])
        else:
            kwargs[jf] = _json_dump([] if jf != "metrics" else {})
    if "last_session_at" in kwargs and isinstance(kwargs["last_session_at"], datetime):
        kwargs["last_session_at"] = kwargs["last_session_at"].isoformat()
    await _execute(
        conn,
        """INSERT INTO project_states
           (id, project_id, current_phase_id, status, decisions, blockers,
            metrics, last_session_at, last_session_description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kwargs["id"],
            kwargs["project_id"],
            kwargs.get("current_phase_id"),
            kwargs["status"],
            kwargs["decisions"],
            kwargs["blockers"],
            kwargs["metrics"],
            kwargs.get("last_session_at"),
            kwargs.get("last_session_description"),
        ),
        label="create_project_state",
    )
    return await get_project_state(conn, project_id)


async def get_project_state(conn: aiosqlite.Connection, project_id: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM project_states WHERE project_id = ?",
        (project_id,),
        label="get_project_state",
    )
    row = await cur.fetchone()
    return _row_to_dict(row)


async def update_project_state(conn: aiosqlite.Connection, project_id: str, **kwargs) -> None:
    if not kwargs:
        return
    for jf in ("decisions", "blockers", "metrics"):
        if jf in kwargs:
            kwargs[jf] = _json_dump(kwargs[jf])
    if "last_session_at" in kwargs and isinstance(kwargs["last_session_at"], datetime):
        kwargs["last_session_at"] = kwargs["last_session_at"].isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [project_id]
    await _execute(
        conn,
        f"UPDATE project_states SET {set_clause} WHERE project_id = ?",
        values,
        label="update_project_state",
    )


# ---------------------------------------------------------------------------
# Phase CRUD
# ---------------------------------------------------------------------------

async def create_phase(conn: aiosqlite.Connection, **kwargs) -> dict:
    kwargs.setdefault("id", _full_uuid())
    kwargs.setdefault("status", "not_started")
    kwargs.setdefault("created_at", _now_iso())
    if "success_criteria" in kwargs:
        kwargs["success_criteria"] = _json_dump(kwargs["success_criteria"])
    if "completed_at" in kwargs and isinstance(kwargs["completed_at"], datetime):
        kwargs["completed_at"] = kwargs["completed_at"].isoformat()
    await _execute(
        conn,
        """INSERT INTO phases
           (id, project_id, name, description, goal, success_criteria,
            phase_number, status, depends_on_phase_id, created_at, completed_at,
            context_config)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kwargs["id"],
            kwargs["project_id"],
            kwargs["name"],
            kwargs.get("description"),
            kwargs.get("goal"),
            kwargs.get("success_criteria"),
            kwargs["phase_number"],
            kwargs["status"],
            kwargs.get("depends_on_phase_id"),
            kwargs["created_at"],
            kwargs.get("completed_at"),
            kwargs.get("context_config"),
        ),
        label="create_phase",
    )
    cur = await _execute(
        conn,
        "SELECT * FROM phases WHERE id = ?",
        (kwargs["id"],),
        label="create_phase_refetch",
    )
    return _row_to_dict(await cur.fetchone())


async def get_phase(conn: aiosqlite.Connection, project_id: str, phase_number: int) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM phases WHERE project_id = ? AND phase_number = ?",
        (project_id, phase_number),
        label="get_phase",
    )
    return _row_to_dict(await cur.fetchone())


async def list_phases(conn: aiosqlite.Connection, project_id: str) -> list[dict]:
    cur = await _execute(
        conn,
        "SELECT * FROM phases WHERE project_id = ? ORDER BY phase_number",
        (project_id,),
        label="list_phases",
    )
    return _rows_to_dicts(await cur.fetchall())


async def get_max_phase_number(conn: aiosqlite.Connection, project_id: str) -> int:
    cur = await _execute(
        conn,
        "SELECT COALESCE(MAX(phase_number), 0) FROM phases WHERE project_id = ?",
        (project_id,),
        label="get_max_phase_number",
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def delete_phase(conn: aiosqlite.Connection, project_id: str, phase_number: int) -> bool:
    cur = await _execute(
        conn,
        "DELETE FROM phases WHERE project_id = ? AND phase_number = ?",
        (project_id, phase_number),
        label="delete_phase",
    )
    return cur.rowcount > 0


async def get_phase_by_id(conn: aiosqlite.Connection, phase_id: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM phases WHERE id = ?",
        (phase_id,),
        label="get_phase_by_id",
    )
    return _row_to_dict(await cur.fetchone())


async def update_phase(conn: aiosqlite.Connection, phase_id: str, **kwargs) -> None:
    if not kwargs:
        return
    if "success_criteria" in kwargs:
        kwargs["success_criteria"] = _json_dump(kwargs["success_criteria"])
    if "completed_at" in kwargs and isinstance(kwargs["completed_at"], datetime):
        kwargs["completed_at"] = kwargs["completed_at"].isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [phase_id]
    await _execute(
        conn,
        f"UPDATE phases SET {set_clause} WHERE id = ?",
        values,
        label="update_phase",
    )


async def update_phase_current_wave(
    conn: aiosqlite.Connection, phase_id: str, wave: int
) -> None:
    await _execute(
        conn,
        "UPDATE phases SET current_wave = ?, updated_at = datetime('now') WHERE id = ?",
        (wave, phase_id),
        label="update_phase_current_wave",
    )


async def get_expert_by_id(conn: aiosqlite.Connection, expert_id: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM experts WHERE id = ?",
        (expert_id,),
        label="get_expert_by_id",
    )
    return _row_to_dict(await cur.fetchone())


async def get_tasks_by_ids(conn: aiosqlite.Connection, task_ids: list[str]) -> list[dict]:
    if not task_ids:
        return []
    placeholders = ", ".join("?" for _ in task_ids)
    cur = await _execute(
        conn,
        f"SELECT * FROM tasks WHERE id IN ({placeholders})",
        task_ids,
        label="get_tasks_by_ids",
    )
    return _rows_to_dicts(await cur.fetchall())


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

async def create_task(conn: aiosqlite.Connection, **kwargs) -> dict:
    kwargs.setdefault("id", _short_uuid())
    kwargs.setdefault("status", "pending")
    kwargs.setdefault("wave", 1)
    kwargs.setdefault("interactive", False)
    kwargs.setdefault("cost_usd", 0.0)
    kwargs.setdefault("created_at", _now_iso())
    for jf in ("depends_on", "env_vars"):
        if jf in kwargs:
            kwargs[jf] = _json_dump(kwargs[jf])
    for dtf in ("started_at", "completed_at"):
        if dtf in kwargs and isinstance(kwargs[dtf], datetime):
            kwargs[dtf] = kwargs[dtf].isoformat()
    await _execute(
        conn,
        """INSERT INTO tasks
           (id, phase_id, project_id, name, prompt, status, wave, depends_on,
            model, permission_mode, env_vars, system_prompt, expert_id,
            interactive, checkpoint_type, task_type, session_id, pid, cost_usd,
            result_summary, started_at, completed_at, created_at, retry_policy,
            context_manifest)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kwargs["id"],
            kwargs.get("phase_id"),
            kwargs["project_id"],
            kwargs["name"],
            kwargs.get("prompt"),
            kwargs["status"],
            kwargs["wave"],
            kwargs.get("depends_on"),
            kwargs.get("model"),
            kwargs.get("permission_mode"),
            kwargs.get("env_vars"),
            kwargs.get("system_prompt"),
            kwargs.get("expert_id"),
            int(kwargs["interactive"]),
            kwargs.get("checkpoint_type"),
            kwargs.get("task_type"),
            kwargs.get("session_id"),
            kwargs.get("pid"),
            kwargs["cost_usd"],
            kwargs.get("result_summary"),
            kwargs.get("started_at"),
            kwargs.get("completed_at"),
            kwargs["created_at"],
            kwargs.get("retry_policy"),
            kwargs.get("context_manifest"),
        ),
        label="create_task",
    )
    return await get_task(conn, kwargs["id"])


async def get_task(conn: aiosqlite.Connection, task_id: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM tasks WHERE id = ?",
        (task_id,),
        label="get_task",
    )
    return _row_to_dict(await cur.fetchone())


async def list_tasks(
    conn: aiosqlite.Connection, project_id: str, phase_id: str | None = None
) -> list[dict]:
    if phase_id is not None:
        cur = await _execute(
            conn,
            "SELECT t.*, p.name AS phase_name, p.phase_number"
            " FROM tasks t LEFT JOIN phases p ON t.phase_id = p.id"
            " WHERE t.project_id = ? AND t.phase_id = ?"
            " ORDER BY t.wave, t.created_at",
            (project_id, phase_id),
            label="list_tasks",
        )
    else:
        cur = await _execute(
            conn,
            "SELECT t.*, p.name AS phase_name, p.phase_number"
            " FROM tasks t LEFT JOIN phases p ON t.phase_id = p.id"
            " WHERE t.project_id = ?"
            " ORDER BY t.wave, t.created_at",
            (project_id,),
            label="list_tasks",
        )
    return _rows_to_dicts(await cur.fetchall())


async def get_active_tasks_all_projects(
    conn: aiosqlite.Connection,
) -> list[dict]:
    """Fetch running/pending/awaiting_input tasks across ALL projects in one query."""
    cur = await _execute(
        conn,
        "SELECT t.*, p.name AS phase_name, p.phase_number,"
        " proj.name AS project_name"
        " FROM tasks t"
        " LEFT JOIN phases p ON t.phase_id = p.id"
        " JOIN projects proj ON t.project_id = proj.id"
        " WHERE t.status IN ('running', 'awaiting_input', 'pending')"
        " ORDER BY t.created_at DESC",
        label="get_active_tasks_all_projects",
    )
    return _rows_to_dicts(await cur.fetchall())


async def get_recent_task_transitions(
    conn: aiosqlite.Connection, limit: int = 8,
) -> list[dict]:
    """Recent task starts/completions/failures for the dashboard feed.

    Synthesises pseudo-events from task state because there is no
    persisted event_log — keeps the Recent Events panel populated
    on first paint without waiting for live SSE traffic.
    """
    cur = await _execute(
        conn,
        "SELECT t.id, t.status, t.cost_usd, t.started_at, t.completed_at,"
        " proj.name AS project_name"
        " FROM tasks t"
        " JOIN projects proj ON t.project_id = proj.id"
        " WHERE t.started_at IS NOT NULL OR t.completed_at IS NOT NULL"
        " ORDER BY COALESCE(t.completed_at, t.started_at) DESC"
        " LIMIT ?",
        (limit,),
        label="get_recent_task_transitions",
    )
    return _rows_to_dicts(await cur.fetchall())


async def update_task(conn: aiosqlite.Connection, task_id: str, **kwargs) -> None:
    if not kwargs:
        return
    for jf in ("depends_on", "env_vars"):
        if jf in kwargs:
            kwargs[jf] = _json_dump(kwargs[jf])
    for dtf in ("started_at", "completed_at"):
        if dtf in kwargs and isinstance(kwargs[dtf], datetime):
            kwargs[dtf] = kwargs[dtf].isoformat()
    if "interactive" in kwargs:
        kwargs["interactive"] = int(kwargs["interactive"])
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    await _execute(
        conn,
        f"UPDATE tasks SET {set_clause} WHERE id = ?",
        values,
        label="update_task",
    )


async def get_tasks_by_phase(conn: aiosqlite.Connection, phase_id: str) -> list[dict]:
    cur = await _execute(
        conn,
        "SELECT * FROM tasks WHERE phase_id = ? ORDER BY wave, created_at",
        (phase_id,),
        label="get_tasks_by_phase",
    )
    return _rows_to_dicts(await cur.fetchall())


# ---------------------------------------------------------------------------
# TaskOutput
# ---------------------------------------------------------------------------

async def delete_task_outputs(conn: aiosqlite.Connection, task_id: str) -> None:
    await _execute(
        conn,
        "DELETE FROM task_outputs WHERE task_id = ?",
        (task_id,),
        label="delete_task_outputs",
    )


async def purge_old_task_outputs(
    conn: aiosqlite.Connection,
    project_id: str,
    keep_last_n: int = 1000,
) -> int:
    """Delete old task outputs for completed tasks in a project.

    Keeps the most recent *keep_last_n* outputs per task. Returns total
    rows deleted.  Useful for long-running projects where the
    ``task_outputs`` table grows unboundedly.
    """
    cur = await _execute(
        conn,
        """DELETE FROM task_outputs
           WHERE id IN (
               SELECT o.id FROM task_outputs o
               JOIN tasks t ON o.task_id = t.id
               WHERE t.project_id = ?
                 AND t.status IN ('completed', 'failed', 'cancelled')
                 AND o.id NOT IN (
                     SELECT o2.id FROM task_outputs o2
                     WHERE o2.task_id = o.task_id
                     ORDER BY o2.timestamp DESC, o2.id DESC
                     LIMIT ?
                 )
           )""",
        (project_id, keep_last_n),
        label="purge_old_task_outputs",
    )
    deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if deleted:
        logger.info("Purged %d old task outputs for project %s", deleted, project_id)
    return deleted


async def create_task_output(conn: aiosqlite.Connection, **kwargs) -> None:
    kwargs.setdefault("timestamp", _now_iso())
    if isinstance(kwargs.get("timestamp"), datetime):
        kwargs["timestamp"] = kwargs["timestamp"].isoformat()
    if "content" in kwargs:
        kwargs["content"] = _json_dump(kwargs["content"])
    await _execute(
        conn,
        """INSERT INTO task_outputs (task_id, timestamp, event_type, content)
           VALUES (?, ?, ?, ?)""",
        (
            kwargs["task_id"],
            kwargs["timestamp"],
            kwargs["event_type"],
            kwargs.get("content"),
        ),
        label="create_task_output",
    )


async def create_task_outputs_batch(
    conn: aiosqlite.Connection, rows: list[dict],
) -> None:
    """Insert multiple task_output rows in a single transaction."""
    if not rows:
        return
    prepared: list[tuple] = []
    for kwargs in rows:
        kwargs.setdefault("timestamp", _now_iso())
        if isinstance(kwargs.get("timestamp"), datetime):
            kwargs["timestamp"] = kwargs["timestamp"].isoformat()
        if "content" in kwargs:
            kwargs["content"] = _json_dump(kwargs["content"])
        prepared.append((
            kwargs["task_id"],
            kwargs["timestamp"],
            kwargs["event_type"],
            kwargs.get("content"),
        ))
    await conn.executemany(
        """INSERT INTO task_outputs (task_id, timestamp, event_type, content)
           VALUES (?, ?, ?, ?)""",
        prepared,
    )


# Safety limit to avoid loading millions of rows when no tail is specified.
_DEFAULT_OUTPUT_LIMIT = 10_000


async def get_task_outputs(
    conn: aiosqlite.Connection,
    task_id: str,
    tail: int | None = None,
    event_types: list[str] | None = None,
) -> list[dict]:
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        base_where = f"task_id = ? AND event_type IN ({placeholders})"
        params: tuple = (task_id, *event_types)
    else:
        base_where = "task_id = ?"
        params = (task_id,)

    # Always use the subquery approach with a LIMIT to prevent unbounded reads.
    # When tail is not specified, use _DEFAULT_OUTPUT_LIMIT as a safety cap.
    effective_limit = tail if tail is not None else _DEFAULT_OUTPUT_LIMIT
    cur = await _execute(
        conn,
        f"""SELECT * FROM (
             SELECT * FROM task_outputs
             WHERE {base_where}
             ORDER BY timestamp DESC, id DESC
             LIMIT ?
           ) sub ORDER BY timestamp ASC, id ASC""",
        (*params, effective_limit),
        label="get_task_outputs",
    )
    return _rows_to_dicts(await cur.fetchall())


# ---------------------------------------------------------------------------
# Expert CRUD
# ---------------------------------------------------------------------------

async def create_expert(conn: aiosqlite.Connection, **kwargs) -> dict:
    kwargs.setdefault("id", _full_uuid())
    kwargs.setdefault("is_builtin", False)
    kwargs.setdefault("created_at", _now_iso())
    if isinstance(kwargs.get("created_at"), datetime):
        kwargs["created_at"] = kwargs["created_at"].isoformat()
    await _execute(
        conn,
        """INSERT INTO experts
           (id, name, description, system_prompt, category, is_builtin,
            role, task_type, pipeline_internal, is_default, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kwargs["id"],
            kwargs["name"],
            kwargs.get("description"),
            kwargs.get("system_prompt"),
            kwargs.get("category"),
            int(kwargs["is_builtin"]),
            kwargs.get("role"),
            kwargs.get("task_type"),
            int(kwargs.get("pipeline_internal", False)),
            int(kwargs.get("is_default", False)),
            kwargs["created_at"],
        ),
        label="create_expert",
    )
    cur = await _execute(
        conn,
        "SELECT * FROM experts WHERE id = ?",
        (kwargs["id"],),
        label="create_expert_refetch",
    )
    return _row_to_dict(await cur.fetchone())


async def get_expert_by_name(conn: aiosqlite.Connection, name: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM experts WHERE name = ?",
        (name,),
        label="get_expert_by_name",
    )
    return _row_to_dict(await cur.fetchone())


async def list_experts(conn: aiosqlite.Connection) -> list[dict]:
    cur = await _execute(
        conn,
        "SELECT * FROM experts ORDER BY name",
        label="list_experts",
    )
    return _rows_to_dicts(await cur.fetchall())


async def update_expert(conn: aiosqlite.Connection, lookup_name: str, **kwargs) -> dict | None:
    if not kwargs:
        return await get_expert_by_name(conn, lookup_name)
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [lookup_name]
    await _execute(
        conn,
        f"UPDATE experts SET {set_clause} WHERE name = ?",
        values,
        label="update_expert",
    )
    # If name was changed, look up by the new name
    new_name = kwargs.get("name", lookup_name)
    return await get_expert_by_name(conn, new_name)


async def get_expert_by_role(conn: aiosqlite.Connection, role: str) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM experts WHERE role = ? LIMIT 1",
        (role,),
        label="get_expert_by_role",
    )
    return _row_to_dict(await cur.fetchone())


async def get_default_expert(conn: aiosqlite.Connection) -> dict | None:
    cur = await _execute(
        conn,
        "SELECT * FROM experts WHERE is_default = 1 LIMIT 1",
        (),
        label="get_default_expert",
    )
    return _row_to_dict(await cur.fetchone())


async def update_expert_id(conn: aiosqlite.Connection, old_id: str, new_id: str) -> None:
    """Migrate an expert's ID (e.g. from random UUID to stable UUID).

    Also updates any tasks that reference the old expert_id.
    """
    await _execute(
        conn,
        "UPDATE tasks SET expert_id = ? WHERE expert_id = ?",
        (new_id, old_id),
        label="update_expert_id_tasks",
    )
    await _execute(
        conn,
        "UPDATE experts SET id = ? WHERE id = ?",
        (new_id, old_id),
        label="update_expert_id",
    )


async def delete_expert(conn: aiosqlite.Connection, name: str) -> bool:
    cur = await _execute(
        conn,
        "DELETE FROM experts WHERE name = ?",
        (name,),
        label="delete_expert",
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Status / count helpers
# ---------------------------------------------------------------------------

async def get_task_counts_by_status(conn: aiosqlite.Connection) -> dict[str, int]:
    cur = await _execute(
        conn,
        "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status",
        label="get_task_counts_by_status",
    )
    rows = await cur.fetchall()
    return {row["status"]: row["cnt"] for row in rows}


async def count_projects(conn: aiosqlite.Connection) -> int:
    cur = await _execute(
        conn,
        "SELECT COUNT(*) FROM projects",
        label="count_projects",
    )
    row = await cur.fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Extracted queries (T2.2 — formerly raw SQL in engine.py)
# ---------------------------------------------------------------------------


async def get_stale_tasks(conn: aiosqlite.Connection) -> list[dict]:
    """Return tasks in running/paused/awaiting_input status (for crash recovery)."""
    cur = await _execute(
        conn,
        "SELECT id, pid, phase_id FROM tasks"
        " WHERE status IN ('running', 'paused', 'awaiting_input')",
        label="get_stale_tasks",
    )
    return _rows_to_dicts(await cur.fetchall())


async def get_completed_reviews_on_complete_phases(
    conn: aiosqlite.Connection,
) -> list[dict]:
    """Return completed phase_review tasks whose phase is still 'complete'."""
    cur = await _execute(
        conn,
        """SELECT t.id AS task_id, t.phase_id, t.project_id
           FROM tasks t
           JOIN phases ph ON t.phase_id = ph.id
           WHERE t.task_type = 'phase_review'
             AND t.status = 'completed'
             AND ph.status = 'complete'
        """,
        label="get_completed_reviews_on_complete_phases",
    )
    return _rows_to_dicts(await cur.fetchall())


async def get_task_ids_by_project_and_status(
    conn: aiosqlite.Connection, project_id: str, status: str
) -> list[str]:
    """Return task IDs matching a project and status."""
    cur = await _execute(
        conn,
        "SELECT id FROM tasks WHERE project_id = ? AND status = ?",
        (project_id, status),
        label="get_task_ids_by_project_and_status",
    )
    return [row["id"] for row in await cur.fetchall()]


async def get_task_ids_by_status(
    conn: aiosqlite.Connection, status: str
) -> list[str]:
    """Return all task IDs matching a given status."""
    cur = await _execute(
        conn,
        "SELECT id FROM tasks WHERE status = ?",
        (status,),
        label="get_task_ids_by_status",
    )
    return [row["id"] for row in await cur.fetchall()]


async def get_interrupted_phases(conn: aiosqlite.Connection) -> list[dict]:
    """Return phases that are in_progress with a checkpoint but no running tasks."""
    cur = await _execute(
        conn,
        """SELECT ph.id, ph.name, p.name AS project_name
           FROM phases ph
           JOIN projects p ON ph.project_id = p.id
           WHERE ph.status = 'in_progress'
             AND ph.current_wave IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM tasks t
                 WHERE t.phase_id = ph.id
                   AND t.status IN ('running', 'awaiting_input')
             )
           ORDER BY ph.created_at""",
        label="get_interrupted_phases",
    )
    return _rows_to_dicts(await cur.fetchall())


async def get_expert_names_by_ids(
    conn: aiosqlite.Connection, ids: list[str]
) -> dict[str, str]:
    """Return ``{expert_id: name}`` for the given IDs in a single query (T2.3)."""
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    cur = await _execute(
        conn,
        f"SELECT id, name FROM experts WHERE id IN ({placeholders})",
        ids,
        label="get_expert_names_by_ids",
    )
    return {row["id"]: row["name"] for row in await cur.fetchall()}


# ---------------------------------------------------------------------------
# PipelineTemplate CRUD
# ---------------------------------------------------------------------------


async def create_pipeline_template(
    conn: aiosqlite.Connection, data: dict
) -> dict:
    """Insert a new pipeline template and return it."""
    template_id = data.get("id") or _full_uuid()
    await _execute(
        conn,
        """INSERT INTO pipeline_templates (id, name, description, flow_json, is_default, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (
            template_id,
            data["name"],
            data.get("description"),
            data.get("flow_json", "{}"),
            1 if data.get("is_default") else 0,
        ),
        label="create_pipeline_template",
    )
    return await get_pipeline_template(conn, template_id)


async def get_pipeline_template(
    conn: aiosqlite.Connection, template_id: str
) -> Optional[dict]:
    """Return a single pipeline template by ID, or None."""
    cur = await _execute(
        conn,
        "SELECT * FROM pipeline_templates WHERE id = ?",
        (template_id,),
        label="get_pipeline_template",
    )
    return _row_to_dict(await cur.fetchone())


async def list_pipeline_templates(
    conn: aiosqlite.Connection,
) -> list[dict]:
    """Return all pipeline templates ordered by name."""
    cur = await _execute(
        conn,
        "SELECT * FROM pipeline_templates ORDER BY name",
        label="list_pipeline_templates",
    )
    return _rows_to_dicts(await cur.fetchall())


async def update_pipeline_template(
    conn: aiosqlite.Connection, template_id: str, **kwargs
) -> Optional[dict]:
    """Update fields on an existing pipeline template. Returns updated row or None."""
    allowed = {"name", "description", "flow_json", "is_default"}
    sets = []
    params = []
    for key, val in kwargs.items():
        if key not in allowed:
            continue
        if key == "is_default":
            val = 1 if val else 0
        sets.append(f"{key} = ?")
        params.append(val)
    if not sets:
        return await get_pipeline_template(conn, template_id)
    sets.append("updated_at = datetime('now')")
    params.append(template_id)
    await _execute(
        conn,
        f"UPDATE pipeline_templates SET {', '.join(sets)} WHERE id = ?",
        params,
        label="update_pipeline_template",
    )
    return await get_pipeline_template(conn, template_id)


async def delete_pipeline_template(
    conn: aiosqlite.Connection, template_id: str
) -> bool:
    """Delete a pipeline template. Returns True if a row was deleted."""
    cur = await _execute(
        conn,
        "DELETE FROM pipeline_templates WHERE id = ?",
        (template_id,),
        label="delete_pipeline_template",
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Agent template CRUD
# ---------------------------------------------------------------------------


async def create_agent_template(conn: aiosqlite.Connection, **kwargs) -> dict:
    """Create an agent template.

    Defaults: id=_full_uuid(), created_at=_now_iso(), is_builtin=False.
    Lists in auto_variables / internal_variables are JSON-encoded automatically.
    """
    kwargs.setdefault("id", _full_uuid())
    kwargs.setdefault("is_builtin", False)
    kwargs.setdefault("created_at", _now_iso())
    if isinstance(kwargs.get("created_at"), datetime):
        kwargs["created_at"] = kwargs["created_at"].isoformat()
    # JSON-encode list fields
    for field in ("auto_variables", "internal_variables"):
        if isinstance(kwargs.get(field), list):
            kwargs[field] = json.dumps(kwargs[field])
    await _execute(
        conn,
        """INSERT INTO agent_templates
           (id, slug, name, description, prompt_text,
            auto_variables, internal_variables, is_builtin, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kwargs["id"],
            kwargs["slug"],
            kwargs["name"],
            kwargs.get("description"),
            kwargs["prompt_text"],
            kwargs.get("auto_variables"),
            kwargs.get("internal_variables"),
            int(kwargs["is_builtin"]),
            kwargs["created_at"],
        ),
        label="create_agent_template",
    )
    cur = await _execute(
        conn,
        "SELECT * FROM agent_templates WHERE id = ?",
        (kwargs["id"],),
        label="create_agent_template_refetch",
    )
    return _row_to_dict(await cur.fetchone())


async def get_agent_template_by_slug(
    conn: aiosqlite.Connection, slug: str
) -> dict | None:
    """Fetch a single agent template by its unique slug."""
    cur = await _execute(
        conn,
        "SELECT * FROM agent_templates WHERE slug = ?",
        (slug,),
        label="get_agent_template_by_slug",
    )
    return _row_to_dict(await cur.fetchone())


async def get_agent_template_by_id(
    conn: aiosqlite.Connection, template_id: str
) -> dict | None:
    """Fetch a single agent template by primary key."""
    cur = await _execute(
        conn,
        "SELECT * FROM agent_templates WHERE id = ?",
        (template_id,),
        label="get_agent_template_by_id",
    )
    return _row_to_dict(await cur.fetchone())


async def list_agent_templates(conn: aiosqlite.Connection) -> list[dict]:
    """Return all agent templates ordered by name."""
    cur = await _execute(
        conn,
        "SELECT * FROM agent_templates ORDER BY name",
        label="list_agent_templates",
    )
    return _rows_to_dicts(await cur.fetchall())


async def update_agent_template(
    conn: aiosqlite.Connection, slug: str, **kwargs
) -> dict | None:
    """Update an agent template identified by slug.

    JSON-encodes auto_variables / internal_variables if they are lists.
    Sets updated_at automatically. Returns the updated row or None if not found.
    """
    if not kwargs:
        return await get_agent_template_by_slug(conn, slug)
    # JSON-encode list fields
    for field in ("auto_variables", "internal_variables"):
        if isinstance(kwargs.get(field), list):
            kwargs[field] = json.dumps(kwargs[field])
    kwargs["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [slug]
    await _execute(
        conn,
        f"UPDATE agent_templates SET {set_clause} WHERE slug = ?",
        values,
        label="update_agent_template",
    )
    # If slug was changed, look up by the new slug
    new_slug = kwargs.get("slug", slug)
    return await get_agent_template_by_slug(conn, new_slug)


async def delete_agent_template(conn: aiosqlite.Connection, slug: str) -> bool:
    """Delete an agent template by slug. Returns True if a row was deleted."""
    cur = await _execute(
        conn,
        "DELETE FROM agent_templates WHERE slug = ?",
        (slug,),
        label="delete_agent_template",
    )
    return cur.rowcount > 0


async def update_agent_template_id(conn: aiosqlite.Connection, old_id: str, new_id: str) -> None:
    """Migrate an agent template's ID (e.g. from random UUID to stable UUID)."""
    await _execute(
        conn,
        "UPDATE agent_templates SET id = ? WHERE id = ?",
        (new_id, old_id),
        label="update_agent_template_id",
    )


# ---------------------------------------------------------------------------
# Schedules CRUD
# ---------------------------------------------------------------------------

async def create_schedule(
    conn: aiosqlite.Connection,
    schedule_id: str,
    *,
    name: str,
    project_name: str,
    template_id: str,
    frequency: str = "daily",
    cron_expr: str | None = None,
    time_of_day: str | None = None,
    day_of_week: str | None = None,
) -> dict:
    """Create a scheduled pipeline run."""
    now = _now_iso()
    await _execute(
        conn,
        """INSERT INTO schedules
           (id, name, project_name, template_id, frequency,
            cron_expr, time_of_day, day_of_week, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (schedule_id, name, project_name, template_id, frequency,
         cron_expr, time_of_day, day_of_week, now),
        label="create_schedule",
    )
    return (await get_schedule(conn, schedule_id)) or {}


async def list_schedules(conn: aiosqlite.Connection) -> list[dict]:
    """Return all schedules ordered by created_at desc."""
    cur = await _execute(
        conn,
        "SELECT * FROM schedules ORDER BY created_at DESC",
        label="list_schedules",
    )
    return _rows_to_dicts(await cur.fetchall())


async def get_schedule(conn: aiosqlite.Connection, schedule_id: str) -> dict | None:
    """Fetch a single schedule by ID."""
    cur = await _execute(
        conn,
        "SELECT * FROM schedules WHERE id = ?",
        (schedule_id,),
        label="get_schedule",
    )
    return _row_to_dict(await cur.fetchone())


async def update_schedule(
    conn: aiosqlite.Connection, schedule_id: str, **kwargs
) -> dict | None:
    """Update fields on an existing schedule. Returns updated row or None."""
    if not kwargs:
        return await get_schedule(conn, schedule_id)
    kwargs["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [schedule_id]
    await _execute(
        conn,
        f"UPDATE schedules SET {set_clause} WHERE id = ?",
        values,
        label="update_schedule",
    )
    return await get_schedule(conn, schedule_id)


async def delete_schedule(conn: aiosqlite.Connection, schedule_id: str) -> bool:
    """Delete a schedule by ID. Returns True if a row was deleted."""
    cur = await _execute(
        conn,
        "DELETE FROM schedules WHERE id = ?",
        (schedule_id,),
        label="delete_schedule",
    )
    return cur.rowcount > 0
