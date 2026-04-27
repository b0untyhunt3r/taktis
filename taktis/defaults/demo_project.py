"""Seed a starter Daily News Briefing project on fresh installs.

A blank dashboard makes a poor first impression. Fresh installs get a
small starter project pre-wired:

  - a project named "Daily News Briefing" with no tasks yet,
  - a schedule that runs the World News Briefing pipeline against that
    project every day at 10:00 UTC.

Idempotent: only fires when the ``projects`` table is empty. If the user
deletes the project, it stays deleted on the next startup. Skipped if
the World News Briefing template is missing (e.g. someone trimmed
``defaults/pipeline_templates/``).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

PROJECT_NAME = "Daily News Briefing"
TEMPLATE_NAME = "World News Briefing"
SCHEDULE_NAME = "Daily news briefing — 10:00 UTC"
SCHEDULE_TIME = "10:00"  # UTC; the cron scheduler operates in UTC.

_DESCRIPTION = (
    "Pre-wired starter project. A daily schedule runs the World News "
    "Briefing pipeline at 10:00 UTC, pulling Hacker News and four world-"
    "news RSS feeds (BBC, Al Jazeera, The Guardian, NPR) and producing a "
    "top-10 briefing under .taktis/news-briefings/. Set ANTHROPIC_API_KEY "
    "(or run `claude login`) and the next 10:00 UTC tick will start "
    "producing briefings; click 'Run Now' on /schedules to trigger one "
    "immediately. Retime or disable the schedule on /schedules; delete "
    "this project at any time and it will not be re-created."
)


async def seed_demo_project(db: aiosqlite.Connection) -> None:
    """If ``projects`` is empty, insert the Daily News Briefing project + schedule.

    Called from the engine's ``initialize`` after the pipeline templates
    and experts have been loaded. Quietly skips on any DB error so a
    seeding hiccup never blocks startup.
    """
    cur = await db.execute("SELECT COUNT(*) FROM projects")
    row = await cur.fetchone()
    if row is None or row[0] > 0:
        return  # User already has projects (or query failed) — never auto-seed.

    cur = await db.execute(
        "SELECT id FROM pipeline_templates WHERE name = ?",
        (TEMPLATE_NAME,),
    )
    row = await cur.fetchone()
    if row is None:
        logger.info(
            "Skipping starter project seed — '%s' template not found",
            TEMPLATE_NAME,
        )
        return
    template_id = row[0]

    project_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO projects
           (id, name, description, working_dir, default_model,
            default_permission_mode, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            PROJECT_NAME,
            _DESCRIPTION,
            "",  # No working dir — file_writer writes under .taktis/ relative to the server CWD.
            "sonnet",
            "default",
            now,
            now,
        ),
    )

    schedule_id = uuid.uuid4().hex[:8]
    await db.execute(
        """INSERT INTO schedules
           (id, name, project_name, template_id, frequency,
            time_of_day, enabled, created_at)
           VALUES (?, ?, ?, ?, 'daily', ?, 1, ?)""",
        (
            schedule_id,
            SCHEDULE_NAME,
            PROJECT_NAME,
            template_id,
            SCHEDULE_TIME,
            now,
        ),
    )

    logger.info(
        "Seeded '%s' project (%s) with daily %s UTC schedule",
        PROJECT_NAME, project_id, SCHEDULE_TIME,
    )
