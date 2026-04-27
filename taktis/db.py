from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite

from taktis.config import settings
from taktis.exceptions import DatabaseError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parse the database file path from the SQLAlchemy-style URL
# e.g. "sqlite+aiosqlite:///taktis.db" -> "taktis.db"
#      "sqlite+aiosqlite:////abs/path/db.sqlite" -> "/abs/path/db.sqlite"
# ---------------------------------------------------------------------------

_DB_URL_RE = re.compile(r"^sqlite(?:\+aiosqlite)?:///(.+)$")


def _resolve_db_path() -> str:
    m = _DB_URL_RE.match(settings.database_url)
    if m:
        return m.group(1)
    # Fallback: treat the whole string as a file path
    return settings.database_url


DATABASE_PATH: str = _resolve_db_path()

# ---------------------------------------------------------------------------
# Table creation SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS experts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    system_prompt TEXT,
    category    TEXT,
    is_builtin  INTEGER NOT NULL DEFAULT 0,
    role        TEXT,
    task_type   TEXT,
    pipeline_internal INTEGER NOT NULL DEFAULT 0,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    description             TEXT,
    working_dir             TEXT,
    default_model           TEXT,
    default_permission_mode TEXT,
    default_env_vars        TEXT,
    planning_options        TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT
);

CREATE TABLE IF NOT EXISTS project_states (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    current_phase_id        TEXT REFERENCES phases(id) ON DELETE SET NULL,
    status                  TEXT NOT NULL DEFAULT 'idle',
    decisions               TEXT,
    blockers                TEXT,
    metrics                 TEXT,
    last_session_at         TEXT,
    last_session_description TEXT
);
CREATE INDEX IF NOT EXISTS ix_project_states_status ON project_states(status);

CREATE TABLE IF NOT EXISTS phases (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    description         TEXT,
    goal                TEXT,
    success_criteria    TEXT,
    phase_number        INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'not_started',
    depends_on_phase_id TEXT REFERENCES phases(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,
    completed_at        TEXT,
    current_wave        INTEGER,
    updated_at          TEXT,
    context_config      TEXT
);
CREATE INDEX IF NOT EXISTS ix_phases_project_id ON phases(project_id);
CREATE INDEX IF NOT EXISTS ix_phases_status ON phases(status);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    phase_id        TEXT REFERENCES phases(id) ON DELETE SET NULL,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    prompt          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    wave            INTEGER NOT NULL DEFAULT 1,
    depends_on      TEXT,
    model           TEXT,
    permission_mode TEXT,
    env_vars        TEXT,
    system_prompt   TEXT,
    expert_id       TEXT REFERENCES experts(id) ON DELETE SET NULL,
    interactive     INTEGER NOT NULL DEFAULT 0,
    checkpoint_type TEXT,
    task_type       TEXT,
    session_id      TEXT,
    pid             INTEGER,
    cost_usd        REAL NOT NULL DEFAULT 0.0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    num_turns       INTEGER NOT NULL DEFAULT 0,
    peak_input_tokens INTEGER NOT NULL DEFAULT 0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    retry_policy    TEXT,
    result_summary  TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL,
    context_manifest TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_project_id ON tasks(project_id);
CREATE INDEX IF NOT EXISTS ix_tasks_phase_id ON tasks(phase_id);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS ix_tasks_status_created ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_project_status ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS ix_tasks_phase_status ON tasks(phase_id, status);

CREATE TABLE IF NOT EXISTS task_outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    content     TEXT
);
CREATE INDEX IF NOT EXISTS ix_task_outputs_task_id ON task_outputs(task_id);
CREATE INDEX IF NOT EXISTS ix_task_outputs_task_timestamp ON task_outputs(task_id, timestamp);
CREATE INDEX IF NOT EXISTS ix_task_outputs_task_event_type ON task_outputs(task_id, event_type);

CREATE TABLE IF NOT EXISTS task_templates (
    id          TEXT PRIMARY KEY,
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    prompt      TEXT,
    model       TEXT,
    expert_id   TEXT REFERENCES experts(id) ON DELETE SET NULL,
    interactive INTEGER NOT NULL DEFAULT 0,
    env_vars    TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    flow_json   TEXT NOT NULL DEFAULT '{}',
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS agent_templates (
    id                 TEXT PRIMARY KEY,
    slug               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    description        TEXT,
    prompt_text        TEXT NOT NULL,
    auto_variables     TEXT,
    internal_variables TEXT,
    is_builtin         INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    project_name TEXT NOT NULL,
    template_id  TEXT NOT NULL,
    frequency    TEXT NOT NULL,
    cron_expr    TEXT,
    time_of_day  TEXT,
    day_of_week  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_run_ok  INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT
);

"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create all tables (idempotent thanks to IF NOT EXISTS)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_CREATE_TABLES_SQL)
        # Migrate: add planning_options to projects if missing
        cur = await db.execute("PRAGMA table_info(projects)")
        proj_cols = {row[1] for row in await cur.fetchall()}
        if "planning_options" not in proj_cols:
            await db.execute(
                "ALTER TABLE projects ADD COLUMN planning_options TEXT"
            )
        # Migrate: add token/turn columns if missing
        cur = await db.execute("PRAGMA table_info(tasks)")
        cols = {row[1] for row in await cur.fetchall()}
        for col, typ, default in [
            ("input_tokens", "INTEGER", "0"),
            ("output_tokens", "INTEGER", "0"),
            ("num_turns", "INTEGER", "0"),
            ("retry_count", "INTEGER", "0"),
            ("peak_input_tokens", "INTEGER", "0"),
        ]:
            if col not in cols:
                await db.execute(
                    f"ALTER TABLE tasks ADD COLUMN {col} {typ} NOT NULL DEFAULT {default}"
                )
        # Migrate: add current_wave and updated_at to phases if missing
        _PHASES_MIGRATIONS = (
            ("current_wave", "ALTER TABLE phases ADD COLUMN current_wave INTEGER"),
            ("updated_at",   "ALTER TABLE phases ADD COLUMN updated_at TEXT"),
            ("context_config", "ALTER TABLE phases ADD COLUMN context_config TEXT"),
        )
        for _col, _stmt in _PHASES_MIGRATIONS:
            try:
                await db.execute(_stmt)
            except Exception as exc:
                # SQLite reports "duplicate column name: <col>" when the column
                # already exists — that is expected during repeat startups.
                if "duplicate column name" in str(exc).lower():
                    logger.debug(
                        "Migration: column '%s' already exists in 'phases', skipping",
                        _col,
                    )
                else:
                    raise DatabaseError(
                        f"Migration failed while adding column '{_col}' to 'phases'",
                        cause=exc,
                    ) from exc
        # Migrate: add role, task_type, pipeline_internal, is_default to experts
        _EXPERTS_MIGRATIONS = (
            ("role", "ALTER TABLE experts ADD COLUMN role TEXT"),
            ("task_type", "ALTER TABLE experts ADD COLUMN task_type TEXT"),
            ("pipeline_internal", "ALTER TABLE experts ADD COLUMN pipeline_internal INTEGER NOT NULL DEFAULT 0"),
            ("is_default", "ALTER TABLE experts ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0"),
        )
        for _col, _stmt in _EXPERTS_MIGRATIONS:
            try:
                await db.execute(_stmt)
            except Exception as exc:
                if "duplicate column name" in str(exc).lower():
                    logger.debug("Migration: column '%s' already exists in 'experts', skipping", _col)
                else:
                    raise DatabaseError(
                        f"Migration failed while adding column '{_col}' to 'experts'",
                        cause=exc,
                    ) from exc

        # Migrate: add retry_policy to tasks if missing
        if "retry_policy" not in cols:
            await db.execute(
                "ALTER TABLE tasks ADD COLUMN retry_policy TEXT"
            )

        # Migrate: add context_manifest to tasks if missing
        if "context_manifest" not in cols:
            await db.execute(
                "ALTER TABLE tasks ADD COLUMN context_manifest TEXT"
            )

        # Migrate: add retry policy to Project Planner agent nodes
        await _migrate_planning_pipeline_retry_policy(db)

        # Migrate: drop deprecated pull-context tables (removed 2026-04).
        # Safe to run repeatedly — DROP IF EXISTS is a no-op when absent.
        # Order matters: FTS references context_entries, context_reads references
        # context_entries — drop FTS first, then context_reads, then context_entries.
        for _drop_stmt in (
            "DROP TABLE IF EXISTS context_entries_fts",
            "DROP TABLE IF EXISTS context_reads",
            "DROP TABLE IF EXISTS context_entries",
        ):
            try:
                await db.execute(_drop_stmt)
            except Exception as _drop_exc:
                logger.debug("Drop pull-context table: %s (%s)", _drop_stmt, _drop_exc)

        # Seed new pipeline templates and sync built-in ones from JSON files
        try:
            await _seed_pipeline_templates(db)
        except Exception as exc:
            logger.debug("Skipping pipeline template sync: %s", exc)

        await db.commit()

    # Connection pool can be initialized separately via init_pool().
    # Not done here to avoid issues in test environments.


def _get_defaults_dir():
    """Return the path to taktis/defaults/."""
    return Path(__file__).parent / "defaults"


async def _migrate_planning_pipeline_retry_policy(db: aiosqlite.Connection) -> None:
    """One-shot migration: add retry_transient/retry_max_attempts/retry_backoff to
    Project Planner agent nodes that don't already have them."""
    import json as _json

    cur = await db.execute(
        "SELECT id, flow_json FROM pipeline_templates WHERE name = 'Project Planner'"
    )
    row = await cur.fetchone()
    if row is None:
        return

    template_id, flow_raw = row
    try:
        flow = _json.loads(flow_raw)
    except (_json.JSONDecodeError, TypeError):
        return

    changed = False
    drawflow = flow.get("drawflow", {})
    for module in drawflow.values():
        for node in (module.get("data") or {}).values():
            if node.get("name") == "agent":
                nd = node.get("data", {})
                if "retry_transient" not in nd:
                    nd["retry_transient"] = True
                    nd["retry_max_attempts"] = "2"
                    nd["retry_backoff"] = "exponential"
                    changed = True

    if changed:
        await db.execute(
            "UPDATE pipeline_templates SET flow_json = ?, updated_at = datetime('now') WHERE id = ?",
            (_json.dumps(flow), template_id),
        )
        logger.info("Migrated Project Planner: added retry policy to agent nodes")


async def _seed_pipeline_templates(db: aiosqlite.Connection) -> None:
    """Seed missing pipeline templates and sync built-in ones from JSON files.

    Default templates live in ``taktis/defaults/pipeline_templates/*.json``.
    Each file contains ``name``, ``description``, ``flow_json``, ``is_default``.

    Behaviour per template:
    - **Not in DB** → insert (seeding).
    - **In DB with ``is_default=1``** → compare ``flow_json`` and ``description``;
      update if changed (sync). User edits to built-in templates are overwritten
      so that prompt fixes propagate automatically.
    - **In DB with ``is_default=0``** → skip (user-created, never touched).
    """
    import json as _json
    import uuid

    defaults_dir = _get_defaults_dir() / "pipeline_templates"
    if not defaults_dir.is_dir():
        logger.warning("No defaults directory: %s", defaults_dir)
        return

    # Get existing templates keyed by name
    cur = await db.execute(
        "SELECT id, name, description, flow_json, is_default FROM pipeline_templates"
    )
    existing = {row[1]: dict(zip(("id", "name", "description", "flow_json", "is_default"), row))
                for row in await cur.fetchall()}

    for json_path in sorted(defaults_dir.glob("*.json")):
        try:
            data = _json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            logger.error("Skipping %s: %s", json_path.name, exc)
            continue

        name = data.get("name", json_path.stem)
        description = data.get("description", "")
        flow_json = data.get("flow_json", {})
        is_default = int(data.get("is_default", False))
        flow_json_str = _json.dumps(flow_json)

        if name not in existing:
            # New template — insert
            template_id = uuid.uuid4().hex
            await db.execute(
                """INSERT INTO pipeline_templates (id, name, description, flow_json, is_default, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                (template_id, name, description, flow_json_str, is_default),
            )
            logger.info("Seeded pipeline template '%s' from %s", name, json_path.name)
            continue

        row = existing[name]

        # User-created template (is_default=0) — never overwrite
        if not row["is_default"]:
            # But if the JSON file says is_default=true and the DB says 0,
            # upgrade it to built-in so future syncs work
            if is_default:
                await db.execute(
                    "UPDATE pipeline_templates SET is_default = 1, updated_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                logger.info("Marked pipeline template '%s' as built-in (is_default=1)", name)
                # Re-read for the comparison below
                row["is_default"] = 1
            else:
                logger.debug("Pipeline template '%s' is user-created — skipping sync", name)
                continue

        # Built-in template (is_default=1) — compare and update if changed
        # Normalize existing flow_json for comparison
        try:
            existing_flow = _json.dumps(_json.loads(row["flow_json"]))
        except (_json.JSONDecodeError, TypeError):
            existing_flow = row["flow_json"]

        needs_update = (existing_flow != flow_json_str or row["description"] != description)

        if needs_update:
            await db.execute(
                """UPDATE pipeline_templates
                   SET flow_json = ?, description = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (flow_json_str, description, row["id"]),
            )
            logger.info("Synced pipeline template '%s' from %s", name, json_path.name)
        else:
            logger.debug("Pipeline template '%s' unchanged — skipping", name)


async def factory_reset_pipeline_templates(db: aiosqlite.Connection) -> int:
    """Factory reset: delete all pipeline templates and re-seed from JSON files.

    Returns the number of templates seeded.
    """
    await db.execute("DELETE FROM pipeline_templates")
    await _seed_pipeline_templates(db)
    cur = await db.execute("SELECT COUNT(*) FROM pipeline_templates")
    count = (await cur.fetchone())[0]
    await db.commit()
    return count


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

import asyncio

_pool: asyncio.Queue[aiosqlite.Connection] | None = None


def _get_pool_size() -> int:
    """Read pool size from config (default 10)."""
    try:
        from taktis.config import settings
        return max(settings.db_pool_size, 2)
    except Exception:
        return 10


async def init_pool() -> None:
    """Initialize the connection pool (called once from init_db)."""
    global _pool
    if _pool is not None:
        return
    pool_size = _get_pool_size()
    _pool = asyncio.Queue(maxsize=pool_size)
    for _ in range(pool_size):
        conn = await aiosqlite.connect(DATABASE_PATH)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA journal_mode=WAL")
        await _pool.put(conn)
    logger.info("DB connection pool initialized (%d connections)", pool_size)


async def close_pool() -> None:
    """Close all pooled connections (called on shutdown)."""
    global _pool
    if _pool is None:
        return
    pool = _pool
    _pool = None  # prevent new borrows immediately
    closed = 0
    while not pool.empty():
        try:
            conn = pool.get_nowait()
            await conn.close()
            closed += 1
        except Exception:
            logger.warning("Error closing pooled connection", exc_info=True)
    logger.info("DB connection pool closed (%d connections)", closed)


@asynccontextmanager
async def get_session() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provide an aiosqlite connection with row_factory set.

    Usage::

        async with get_session() as conn:
            cursor = await conn.execute("SELECT ...")
            rows = await cursor.fetchall()

    Commits on clean exit, rolls back on exception.

    Uses a connection pool when available; falls back to creating a
    new connection if the pool is not yet initialized (e.g., during
    startup before :func:`init_db`).
    """
    pool_conn = False
    if _pool is not None:
        try:
            db = await asyncio.wait_for(_pool.get(), timeout=10.0)
            pool_conn = True
        except asyncio.TimeoutError:
            logger.warning("Connection pool exhausted, creating new connection")
            db = await aiosqlite.connect(DATABASE_PATH)
            db.row_factory = aiosqlite.Row
    else:
        db = await aiosqlite.connect(DATABASE_PATH)
        db.row_factory = aiosqlite.Row
    try:
        if not pool_conn:
            await db.execute("PRAGMA foreign_keys=ON")
        yield db
        await db.commit()
    except Exception:
        logger.exception("Database session error; rolling back transaction")
        try:
            await db.rollback()
        except Exception:
            logger.warning("Rollback also failed (original exception preserved)", exc_info=True)
        raise
    finally:
        if pool_conn and _pool is not None:
            try:
                _pool.put_nowait(db)
            except asyncio.QueueFull:
                await db.close()
        else:
            await db.close()


async def wal_checkpoint() -> None:
    """Trigger a WAL checkpoint to prevent unbounded WAL file growth.

    Should be called periodically (e.g. after each phase completes) to
    keep the ``-wal`` file from growing indefinitely.
    """
    async with get_session() as conn:
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    logger.info("WAL checkpoint completed")


# Backward-compat alias
async_session_factory = get_session
