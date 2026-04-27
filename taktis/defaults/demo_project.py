"""Seed a 'Hello Taktis' demo project on fresh installs.

Empty dashboards make a poor first impression. This module ships a
pre-baked one-phase project with five completed tasks so a brand-new
install has something to look at — a real timeline, real costs,
real durations, real outputs flowing across waves.

Idempotent: only runs when the ``projects`` table is empty. If the user
deletes the demo, it stays deleted on the next startup. Skipped silently
if any of the referenced expert personas are missing (e.g. someone
trimmed the experts directory).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

DEMO_PROJECT_NAME = "Hello Taktis"

_DEMO_DESCRIPTION = (
    "A pre-baked demo so a fresh install isn't an empty dashboard. "
    "Click any task ID below to see its prompt and output, look at how "
    "the wave 2 synthesizer reads from the four wave 1 outputs, and "
    "notice how the costs and durations add up. Safe to delete — the "
    "seed only fires when no other project exists, so once you have "
    "your own work this will not come back."
)

# (name, prompt, wave, expert, duration_seconds, cost_usd, output_text)
_TASKS: list[tuple[str, str, int, str, int, float, str]] = [
    (
        "Greet the user",
        "Welcome a first-time taktis user in 3-4 sentences. Make it clear "
        "this 'Hello Taktis' project is a pre-baked demo of what a completed "
        "phase looks like — it ships with a fresh install and was not run by "
        "the user. Encourage them to click a task ID, expand the result, and "
        "look at how outputs flow into the wave 2 synthesizer. Sign off warmly.",
        1,
        "interviewer",
        14,
        0.0021,
        "Welcome to **taktis** — you're looking at a pre-baked demo, not a "
        "project you ran. Every fresh install ships with this 'Hello Taktis' "
        "phase so the dashboard isn't blank on first launch. Click any task "
        "ID to see its prompt and output, look at how the synthesizer in wave 2 "
        "reads from the four wave 1 outputs, and notice the costs and durations "
        "adding up. When you're ready for the real thing, hit **+ New Project** "
        "on the dashboard.",
    ),
    (
        "Frame taktis historically",
        "What problem does taktis solve, framed historically? Compare to make, "
        "Airflow, and human team coordination. 3-4 sentences, no bullet points.",
        1,
        "historian",
        18,
        0.0027,
        "If `make` taught us how to express dependencies between artifacts, and "
        "Airflow taught us how to schedule those dependencies across machines, "
        "**taktis** is the same lesson applied to LLM agents — the dependency "
        "graph is the contract, the scheduler honours it, and the artifacts are "
        "markdown files on disk. The novelty isn't agents; agents are everywhere "
        "now. The novelty is treating a coordinated multi-agent run as a first-"
        "class workflow object that you can save, version, schedule, resume, and "
        "show to a non-engineer in a browser tab.",
    ),
    (
        "Walk the layer model",
        "In 5 short bullets, walk a reader through how a Drawflow graph in the "
        "browser turns into running Claude processes: graph → DAG → waves → "
        "tasks → SDK processes → checkpoints. One line per bullet.",
        1,
        "architect-general",
        22,
        0.0034,
        "- **Drawflow** is what you draw in the browser — a graph of typed nodes "
        "(agent, fan-out, conditional, loop, etc.).\n"
        "- **GraphExecutor** parses that graph into a DAG, topologically sorts "
        "it, and groups nodes into **waves** — sets that can run in parallel.\n"
        "- **WaveScheduler** runs one wave at a time in order; within a wave, "
        "tasks run concurrently up to a 15-slot semaphore.\n"
        "- Each task runs as an **SDKProcess** — a managed Claude Agent SDK "
        "subprocess with streaming output and tool approval.\n"
        "- After every successful wave a **checkpoint** is written, so a crashed "
        "run resumes from the next wave instead of from task 1.",
    ),
    (
        "Suggest five next actions",
        "List 5 specific, clickable next steps for a new user — things they can "
        "do in the next 60 seconds that show value. Reference real menu items "
        "and template names.",
        1,
        "product-manager",
        16,
        0.0024,
        "1. Open **/projects** and create a real project pointing at a working "
        "directory you own.\n"
        "2. Open **/pipelines** and load the **Pipeline Factory** template — "
        "describe what you want in plain language and watch taktis design a "
        "pipeline for you.\n"
        "3. Open **/experts** and skim the 184 personas — pick one whose domain "
        "isn't yours (a `historian` for technical debt, a `narratologist` for a "
        "code audit) for the next single task you run.\n"
        "4. Open **/schedules** and browse the cron-driven runs; the **PR "
        "Controller** template is cron-safe and shows how scheduled pipelines "
        "work end-to-end.\n"
        "5. Delete this project — it's just a seed, you won't break anything, "
        "and a clean dashboard is fine to live with once you've spun up real work.",
    ),
    (
        "Synthesize the welcome",
        "Read the four wave 1 outputs (greeting, historical framing, layer model, "
        "next actions) and produce a 5-sentence synthesized welcome. Introduce "
        "taktis, frame it historically, sketch the layer model in one sentence, "
        "and close with one specific 'try this first' action.",
        2,
        "synthesizer",
        24,
        0.0048,
        "**taktis** is a self-hosted orchestrator that runs Claude Code agents "
        "as a coordinated DAG of typed nodes — the same idea `make` had for "
        "build artifacts and Airflow had for data pipelines, applied to LLM "
        "agents. You draw a graph in the browser; the engine groups it into "
        "waves, runs each wave in parallel up to 15 slots, and checkpoints "
        "between waves so a crash resumes mid-flight. This **Hello Taktis** "
        "project is a pre-baked demo — every task you see here is a static "
        "seed, not a real run. To start your own work, open **/pipelines**, "
        "load the **Pipeline Factory** template, and describe what you want — "
        "taktis will design and wire up a pipeline for you. Welcome aboard.",
    ),
]


async def seed_demo_project(db: aiosqlite.Connection) -> None:
    """Insert the Hello Taktis demo iff no projects currently exist.

    Called from ``init_db`` after pipeline templates are seeded.  Quietly
    skips on any DB error so a seeding hiccup never blocks startup.
    """
    cur = await db.execute("SELECT COUNT(*) FROM projects")
    row = await cur.fetchone()
    if row is None or row[0] > 0:
        return  # User already has projects (or query failed) — never auto-seed.

    expert_names = [t[3] for t in _TASKS]
    placeholders = ",".join("?" * len(expert_names))
    cur = await db.execute(
        f"SELECT name, id FROM experts WHERE name IN ({placeholders})",
        expert_names,
    )
    expert_id_by_name = {row[0]: row[1] for row in await cur.fetchall()}
    missing = set(expert_names) - set(expert_id_by_name)
    if missing:
        logger.info(
            "Skipping demo project seed — missing experts: %s",
            sorted(missing),
        )
        return

    project_id = uuid.uuid4().hex
    phase_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    total_demo_seconds = sum(t[4] for t in _TASKS) + 6  # +6s for setup overhead
    phase_started = now - timedelta(seconds=total_demo_seconds)

    await db.execute(
        """INSERT INTO projects
           (id, name, description, working_dir, default_model,
            default_permission_mode, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            DEMO_PROJECT_NAME,
            _DEMO_DESCRIPTION,
            "",  # No working dir — read-only demo.
            "sonnet",
            "default",
            phase_started.isoformat(),
            now.isoformat(),
        ),
    )

    await db.execute(
        """INSERT INTO phases
           (id, project_id, name, description, goal, success_criteria,
            phase_number, status, created_at, completed_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            phase_id,
            project_id,
            "Welcome",
            "First-look phase — four parallel agents in wave 1, a synthesizer in wave 2.",
            "Greet the user and orient them around what taktis does.",
            "Each wave 1 task contributes a paragraph; the wave 2 synthesizer combines them.",
            1,
            "completed",
            phase_started.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )

    # Lay out tasks on a timeline: wave 1 tasks start together; wave 2
    # starts after the slowest wave 1 task finishes.
    wave1_tasks = [t for t in _TASKS if t[2] == 1]
    wave1_max_duration = max(t[4] for t in wave1_tasks)
    wave2_offset = wave1_max_duration + 2  # +2s grace between waves

    for (name, prompt, wave, expert_name, duration, cost, output) in _TASKS:
        task_id = uuid.uuid4().hex[:8]
        start_offset = 0 if wave == 1 else wave2_offset
        task_started = phase_started + timedelta(seconds=start_offset)
        task_completed = task_started + timedelta(seconds=duration)
        # First-line summary makes the projects page list readable.
        result_summary = output.split("\n", 1)[0][:240]

        await db.execute(
            """INSERT INTO tasks
               (id, phase_id, project_id, name, prompt, status, wave,
                model, permission_mode, expert_id, interactive,
                cost_usd, input_tokens, output_tokens, num_turns,
                retry_count, result_summary, started_at, completed_at,
                created_at)
               VALUES (?, ?, ?, ?, ?, 'completed', ?, 'sonnet', 'default',
                       ?, 0, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (
                task_id, phase_id, project_id, name, prompt, wave,
                expert_id_by_name[expert_name],
                cost,
                # Realistic-looking token counts; not load-bearing.
                int(cost * 60_000),  # input tokens approx
                int(cost * 12_000),  # output tokens approx
                1,  # num_turns
                result_summary,
                task_started.isoformat(),
                task_completed.isoformat(),
                task_started.isoformat(),
            ),
        )

        await db.execute(
            "INSERT INTO task_outputs (task_id, timestamp, event_type, content) VALUES (?, ?, ?, ?)",
            (
                task_id,
                task_completed.isoformat(),
                "result",
                json.dumps({
                    "type": "result",
                    "subtype": "success",
                    "result": output,
                    "cost_usd": cost,
                    "duration_ms": duration * 1000,
                    "is_error": False,
                    "is_checkpoint": False,
                    "input_tokens": int(cost * 60_000),
                    "output_tokens": int(cost * 12_000),
                    "num_turns": 1,
                }),
            ),
        )

    logger.info("Seeded '%s' demo project (%s)", DEMO_PROJECT_NAME, project_id)
