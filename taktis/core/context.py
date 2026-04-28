"""Manages .taktis/ context files in the project working directory.

Context files provide shared state between tasks, similar to GSD's .planning/ directory.
Each task gets context injected into its system prompt so it knows what the project is
about and what previous tasks have accomplished.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from taktis.exceptions import ContextError

logger = logging.getLogger(__name__)

CONTEXT_DIR = ".taktis"

# Maximum individual context file size (1 MB) to prevent memory issues.
# Files exceeding this are truncated with a marker in get_phase_context().
# Rationale: Claude's context window is ~200K tokens ≈ ~800KB of text;
# a single 1 MB file would consume most of the available context.
_MAX_CONTEXT_FILE_SIZE = 1_048_576

import re
import threading
import time

_SAFE_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Phase context cache — keyed on (working_dir, phase_number)
_phase_context_cache: dict[tuple[str, int | None], tuple[float, str, list[dict]]] = {}
_phase_context_lock = threading.Lock()
_PHASE_CONTEXT_TTL = 60.0  # seconds — safety net; explicit invalidation is primary


# ------------------------------------------------------------------
# ContextBudget — priority-aware context assembly with char budget
# ------------------------------------------------------------------


@dataclass
class ContextSection:
    priority: int       # 0=must, 1=high, 2=medium, 3=low, 4=trim-first
    tag: str            # XML tag name
    content: str        # Full text
    source_path: str    # File path hint for truncation message
    summary: str = ""   # Optional ~500 char summary


class ContextBudget:
    P0_MUST = 0
    P1_HIGH = 1
    P2_MEDIUM = 2
    P3_LOW = 3
    P4_TRIM = 4

    def __init__(self, budget_chars: int = 150_000):
        self._budget = max(budget_chars, 1000)
        self._sections: list[ContextSection] = []

    def add(self, priority: int, tag: str, content: str,
            source_path: str = "", summary: str = "") -> None:
        if not content or not content.strip():
            return
        self._sections.append(ContextSection(
            priority=priority, tag=tag, content=content,
            source_path=source_path, summary=summary,
        ))

    def assemble(self) -> tuple[str, list[dict]]:
        sorted_sections = sorted(self._sections, key=lambda s: s.priority)
        remaining = self._budget
        included: list[tuple[ContextSection, str, str]] = []
        manifest: list[dict] = []

        for sec in sorted_sections:
            entry = {"tag": sec.tag, "priority": sec.priority,
                     "chars_full": len(sec.content), "chars_used": 0, "mode": "omitted"}
            if remaining <= 0 and sec.priority > self.P0_MUST:
                manifest.append(entry)
                continue
            if len(sec.content) <= remaining:
                included.append((sec, sec.content, "full"))
                entry.update(chars_used=len(sec.content), mode="full")
                remaining -= len(sec.content)
            elif sec.summary and len(sec.summary) <= remaining:
                text = sec.summary + f"\n\n[Full content in {sec.source_path}]"
                included.append((sec, text, "summary"))
                entry.update(chars_used=len(text), mode="summary")
                remaining -= len(text)
            elif sec.priority <= self.P0_MUST:
                truncated = sec.content[:max(remaining - 80, 0)]
                truncated += f"\n\n[... truncated — full file at {sec.source_path}]"
                included.append((sec, truncated, "truncated"))
                entry.update(chars_used=len(truncated), mode="truncated")
                remaining -= len(truncated)
            manifest.append(entry)

        parts = [f"<{sec.tag}>\n{text}\n</{sec.tag}>" for sec, text, _ in included]
        header = ("# Project Context\n\n"
                  "The following context describes the project, "
                  "its current state, and prior work.\n\n")
        assembled = header + "\n\n".join(parts) if parts else ""
        return assembled, manifest


def invalidate_phase_context(working_dir: str, phase_number: int | None = None) -> None:
    """Invalidate cached phase context. Called after context files change."""
    with _phase_context_lock:
        if phase_number is not None:
            # Invalidate entries for this phase
            keys_to_remove = [
                k for k in _phase_context_cache
                if k[0] == working_dir and k[1] == phase_number
            ]
            for k in keys_to_remove:
                del _phase_context_cache[k]
        else:
            # Invalidate all phases for this working_dir
            keys_to_remove = [k for k in _phase_context_cache if k[0] == working_dir]
            for k in keys_to_remove:
                del _phase_context_cache[k]


def clear_phase_context_cache() -> None:
    """Clear the entire cache. Useful for testing."""
    with _phase_context_lock:
        _phase_context_cache.clear()


def _validate_path_component(value: str, label: str) -> str:
    """Validate that *value* is safe for use as a path component.

    Raises :class:`ValueError` if the value contains path separators,
    null bytes, or other dangerous characters.
    """
    if not value:
        raise ValueError(f"{label} must not be empty")
    if not _SAFE_TASK_ID_RE.match(value):
        raise ValueError(
            f"{label} contains invalid characters: {value!r}"
        )
    return value


def _assert_within(path: Path, root: Path, label: str = "Path") -> None:
    """Assert that *path* resolves to a location within *root*.

    Uses ``Path.relative_to()`` which is immune to string-prefix false
    positives (e.g. ``C:\\project-evil`` vs ``C:\\project``).

    Raises :class:`ValueError` on path traversal attempts.
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"{label} escapes context directory: {path}")
    except OSError as exc:
        raise ValueError(f"Cannot resolve {label}: {exc}") from exc


def _ctx_dir(working_dir: str) -> Path:
    return Path(working_dir) / CONTEXT_DIR


def init_context(working_dir: str, project_name: str, description: str = "") -> None:
    """Initialize the .taktis/ directory with PROJECT.md."""
    ctx = _ctx_dir(working_dir)
    try:
        ctx.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContextError(
            "Failed to create context directory",
            path=str(ctx),
            cause=exc,
        ) from exc

    phases_dir = ctx / "phases"
    try:
        phases_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise ContextError(
            "Failed to create phases directory",
            path=str(phases_dir),
            cause=exc,
        ) from exc

    project_md = ctx / "PROJECT.md"
    if not project_md.exists():
        try:
            project_md.write_text(
                f"# {project_name}\n\n"
                f"{description}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise ContextError(
                "Failed to write project context file",
                path=str(project_md),
                cause=exc,
            ) from exc
        logger.info("Created %s", project_md)


def read_context(working_dir: str) -> dict[str, str]:
    """Read all context files. Returns dict with keys: project."""
    ctx = _ctx_dir(working_dir)
    result = {}

    project_md = ctx / "PROJECT.md"
    if project_md.exists():
        try:
            result["project"] = project_md.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContextError(
                "Failed to read project context file",
                path=str(project_md),
                cause=exc,
            ) from exc

    return result


def init_phase_dir(working_dir: str, phase_number: int) -> None:
    """Create the phase directory if it doesn't exist."""
    phase_dir = _ctx_dir(working_dir) / "phases" / str(phase_number)
    try:
        phase_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContextError(
            f"Failed to create phase {phase_number} directory",
            path=str(phase_dir),
            cause=exc,
        ) from exc


def write_phase_plan(
    working_dir: str, phase_number: int, name: str, goal: str, tasks: list[dict],
) -> None:
    """Write PLAN.md for a phase."""
    init_phase_dir(working_dir, phase_number)
    plan_path = _ctx_dir(working_dir) / "phases" / str(phase_number) / "PLAN.md"

    lines = [f"# Phase {phase_number}: {name}\n\n"]
    if goal:
        lines.append(f"**Goal:** {goal}\n\n")
    lines.append("## Tasks\n\n")
    for i, t in enumerate(tasks, 1):
        wave = t.get("wave", 1)
        expert = t.get("expert", "—")
        prompt = t.get("prompt", "")
        lines.append(f"### Task {i} (Wave {wave}, Expert: {expert})\n\n{prompt}\n\n")

    try:
        plan_path.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        raise ContextError(
            f"Failed to write phase {phase_number} plan",
            path=str(plan_path),
            cause=exc,
        ) from exc
    logger.info("Wrote phase plan: %s", plan_path)


# ------------------------------------------------------------------
# Summary extraction — first meaningful paragraph, skipping LLM preamble
# ------------------------------------------------------------------

_PREAMBLE_STARTS = (
    "I'll ", "I will ", "Let me ", "Sure,", "Sure ", "I am ",
    "Here's ", "Here is ", "Okay,", "Ok,",
)


def _extract_summary(text: str, max_chars: int = 500) -> str:
    """Extract first meaningful paragraph, skipping LLM preamble."""
    if not text:
        return ""
    paragraphs = text.split("\n\n")
    for para in paragraphs:
        candidate = para.strip()
        if len(candidate) > 50 and not any(
            candidate.startswith(p) for p in _PREAMBLE_STARTS
        ):
            return candidate[:max_chars]
    for para in paragraphs:
        candidate = para.strip()
        if len(candidate) > 50:
            return candidate[:max_chars]
    return text[:max_chars].rstrip()


def write_task_result(
    working_dir: str,
    phase_number: int,
    task_id: str,
    result: str,
    *,
    task_name: str = "",
    wave: int = 0,
) -> None:
    """Write a per-task result file. Race-free — each task writes its own file."""
    _validate_path_component(task_id, "task_id")
    init_phase_dir(working_dir, phase_number)
    phase_dir = _ctx_dir(working_dir) / "phases" / str(phase_number)
    path = phase_dir / f"RESULT_{task_id}.md"
    _assert_within(path, _ctx_dir(working_dir), "Result path")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = f"## Wave {wave}: {task_name}\n*{now}*\n\n{result}\n"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ContextError(
            f"Failed to write task result for {task_id}",
            path=str(path),
            cause=exc,
        ) from exc
    logger.info("Wrote task result: %s", path)

    # Write compact summary alongside full result
    summary = _extract_summary(result, max_chars=500)
    summary_path = phase_dir / f"RESULT_{task_id}.summary.md"
    try:
        summary_path.write_text(
            f"## Wave {wave}: {task_name} (Summary)\n\n{summary}\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to write summary for task %s", task_id)

    invalidate_phase_context(working_dir, phase_number)


# ------------------------------------------------------------------
# Research & extended context files
# ------------------------------------------------------------------


def write_research_file(working_dir: str, filename: str, content: str) -> None:
    """Write a research file to .taktis/research/."""
    # Validate filename to prevent path traversal (filename may come from
    # LLM-generated pipeline output).
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(
            f"Research filename contains invalid characters: {filename!r}"
        )
    # Strip .md/.MD suffix before validating the base name
    base = filename
    if base.lower().endswith(".md"):
        base = base[:-3]
    _validate_path_component(base, "research filename")

    research_dir = _ctx_dir(working_dir) / "research"
    try:
        research_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContextError(
            "Failed to create research directory",
            path=str(research_dir),
            cause=exc,
        ) from exc
    path = research_dir / filename
    _assert_within(path, research_dir, "Research file path")
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ContextError(
            f"Failed to write research file '{filename}'",
            path=str(path),
            cause=exc,
        ) from exc
    logger.info("Wrote research file: %s", path)


def read_research_files(working_dir: str) -> dict[str, str]:
    """Read all research files. Returns dict of filename → content."""
    research_dir = _ctx_dir(working_dir) / "research"
    if not research_dir.exists():
        return {}
    result: dict[str, str] = {}
    try:
        entries = sorted(research_dir.iterdir())
    except OSError as exc:
        logger.warning("Could not list research directory %s: %s", research_dir, exc)
        return {}
    for f in entries:
        if f.is_file() and f.suffix == ".md":
            try:
                result[f.name] = f.read_text(encoding="utf-8")
            except OSError as exc:
                raise ContextError(
                    f"Failed to read research file '{f.name}'",
                    path=str(f),
                    cause=exc,
                ) from exc
    return result




def write_requirements(working_dir: str, content: str) -> None:
    """Convenience: write REQUIREMENTS.md to .taktis/."""
    path = _ctx_dir(working_dir) / "REQUIREMENTS.md"
    path.write_text(content, encoding="utf-8")


def write_roadmap(working_dir: str, content: str) -> None:
    """Convenience: write ROADMAP.md to .taktis/."""
    path = _ctx_dir(working_dir) / "ROADMAP.md"
    path.write_text(content, encoding="utf-8")


def write_verification(working_dir: str, content: str) -> None:
    """Convenience: write VERIFICATION.md to .taktis/."""
    path = _ctx_dir(working_dir) / "VERIFICATION.md"
    path.write_text(content, encoding="utf-8")


def write_task_discuss(
    working_dir: str, phase_number: int, task_id: str, content: str,
) -> None:
    """Write DISCUSS_{task_id}.md (discuss-task output) for a specific task."""
    _validate_path_component(task_id, "task_id")
    init_phase_dir(working_dir, phase_number)
    path = _ctx_dir(working_dir) / "phases" / str(phase_number) / f"DISCUSS_{task_id}.md"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ContextError(
            f"Failed to write discuss file for task {task_id}",
            path=str(path),
            cause=exc,
        ) from exc
    logger.info("Wrote task discuss: %s", path)


def write_task_research(
    working_dir: str, phase_number: int, task_id: str, content: str,
) -> None:
    """Write RESEARCH_{task_id}.md for a specific task."""
    _validate_path_component(task_id, "task_id")
    init_phase_dir(working_dir, phase_number)
    path = _ctx_dir(working_dir) / "phases" / str(phase_number) / f"RESEARCH_{task_id}.md"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ContextError(
            f"Failed to write research file for task {task_id}",
            path=str(path),
            cause=exc,
        ) from exc
    logger.info("Wrote task research: %s", path)


def write_phase_review(working_dir: str, phase_number: int, content: str) -> None:
    """Write REVIEW.md for a specific phase."""
    init_phase_dir(working_dir, phase_number)
    path = _ctx_dir(working_dir) / "phases" / str(phase_number) / "REVIEW.md"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ContextError(
            f"Failed to write phase {phase_number} review file",
            path=str(path),
            cause=exc,
        ) from exc
    logger.info("Wrote phase review: %s", path)
    invalidate_phase_context(working_dir, phase_number)


# ------------------------------------------------------------------
# Context injection for task system prompts
# ------------------------------------------------------------------


def _safe_read(path: Path, label: str) -> str | None:
    """Read *path* and return its text, or None on I/O failure.

    On failure a WARNING is emitted so the problem is visible in logs
    without crashing the calling task.  Files exceeding
    ``_MAX_CONTEXT_FILE_SIZE`` are truncated with a marker.
    """
    try:
        size = path.stat().st_size
        if size > _MAX_CONTEXT_FILE_SIZE:
            logger.warning(
                "get_phase_context: truncating %s (%d bytes > %d limit)",
                label, size, _MAX_CONTEXT_FILE_SIZE,
            )
            with open(path, "r", encoding="utf-8") as f:
                text = f.read(_MAX_CONTEXT_FILE_SIZE)
            return text + "\n\n[... truncated — file exceeded size limit ...]"
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "get_phase_context: skipping %s — failed to read %s: %s",
            label,
            path,
            exc,
        )
        return None


def _read_summary(phase_dir: Path, task_id: str) -> str:
    """Read the summary file for a task result, or return empty string."""
    summary_path = phase_dir / f"RESULT_{task_id}.summary.md"
    if summary_path.exists():
        return _safe_read(summary_path, f"task {task_id} summary") or ""
    return ""


_PRIORITY_MAP = {
    "P0 — must include": "P0_MUST",
    "P1 — high": "P1_HIGH",
    "P2 — medium": "P2_MEDIUM",
    "P3 — low": "P3_LOW",
    "P4 — trim first": "P4_TRIM",
}


def update_context_manifest(working_dir: str, filename: str, priority: str) -> None:
    """Register a file in .taktis/context_manifest.json with its priority."""
    import json as _json
    ctx = _ctx_dir(working_dir)
    manifest_path = ctx / "context_manifest.json"
    manifest: dict[str, str] = {}
    if manifest_path.exists():
        try:
            manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            manifest = {}
    manifest[filename] = _PRIORITY_MAP.get(priority, priority)
    try:
        manifest_path.write_text(_json.dumps(manifest, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to update context manifest: %s", exc)


def read_context_manifest(working_dir: str) -> dict[str, str]:
    """Read the context manifest — returns {filename: priority_key}."""
    import json as _json
    ctx = _ctx_dir(working_dir)
    manifest_path = ctx / "context_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        return _json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return {}


def _build_budgeted_context(
    working_dir: str,
    phase_number: int | None,
    budget_chars: int = 150_000,
    state_summary: str = "",
) -> tuple[str, list[dict]]:
    """Read all CACHEABLE context files and build context via ContextBudget.

    Reads: PROJECT.md, REQUIREMENTS.md, ROADMAP.md,
    research/SUMMARY.md, VERIFICATION.md, prior-phase REVIEW.md files,
    current-phase PLAN.md, current-phase RESULT_*.md, plus state_summary.

    Does NOT include per-task DISCUSS/RESEARCH files (those are task-specific).

    Returns ``(assembled_text, manifest)`` tuple from the budget.
    """
    ctx = _ctx_dir(working_dir)
    if not ctx.exists():
        return "", []

    budget = ContextBudget(budget_chars)

    # P0_MUST: Project context (always included — system-generated)
    text = _safe_read(ctx / "PROJECT.md", "project context")
    if text is not None:
        budget.add(ContextBudget.P0_MUST, "project_context", text,
                   source_path=str(ctx / "PROJECT.md"))

    # P3_LOW: State summary (passed as parameter, from DB)
    if state_summary:
        budget.add(ContextBudget.P3_LOW, "project_state", state_summary,
                   source_path="(generated from DB)")

    # Load files registered in context_manifest.json by pipeline file_writers
    _priority_to_level = {
        "P0_MUST": ContextBudget.P0_MUST,
        "P1_HIGH": ContextBudget.P1_HIGH,
        "P2_MEDIUM": ContextBudget.P2_MEDIUM,
        "P3_LOW": ContextBudget.P3_LOW,
        "P4_TRIM": ContextBudget.P4_TRIM,
    }
    manifest = read_context_manifest(working_dir)
    for mf_filename, mf_priority in manifest.items():
        mf_path = ctx / mf_filename
        text = _safe_read(mf_path, mf_filename)
        if text is not None:
            level = _priority_to_level.get(mf_priority, ContextBudget.P3_LOW)
            label = mf_filename.replace("/", "_").replace(".", "_")
            budget.add(level, label, text, source_path=str(mf_path))

    # Reviews from prior phases (P3_LOW)
    if phase_number is not None:
        phases_dir = ctx / "phases"
        if phases_dir.exists():
            try:
                phase_nums = sorted(
                    int(d.name)
                    for d in phases_dir.iterdir()
                    if d.is_dir() and d.name.isdigit()
                )
            except OSError as exc:
                logger.warning("Could not list phases directory %s: %s", phases_dir, exc)
                phase_nums = []
            for pn in phase_nums:
                if pn >= phase_number:
                    break
                review_path = phases_dir / str(pn) / "REVIEW.md"
                if review_path.exists():
                    text = _safe_read(review_path, f"phase {pn} review")
                    if text is not None:
                        budget.add(
                            ContextBudget.P3_LOW,
                            f'prior_phase_review phase="{pn}"',
                            text,
                            source_path=str(review_path),
                        )

        # P0_MUST: Current phase plan
        plan_path = phases_dir / str(phase_number) / "PLAN.md"
        if plan_path.exists():
            text = _safe_read(plan_path, f"phase {phase_number} plan")
            if text is not None:
                budget.add(ContextBudget.P0_MUST, "current_phase_plan", text,
                           source_path=str(plan_path))

        # P4_TRIM: Current phase task results from prior waves
        phase_dir = phases_dir / str(phase_number)
        if phase_dir.exists():
            _MAX_RESULT_FILES = 50
            try:
                result_files = sorted(phase_dir.glob("RESULT_*.md"))
            except OSError as exc:
                logger.warning("Could not list task results in %s: %s", phase_dir, exc)
                result_files = []
            # Exclude .summary.md companion files
            result_files = [rf for rf in result_files if not rf.name.endswith(".summary.md")]
            if len(result_files) > _MAX_RESULT_FILES:
                logger.info(
                    "Phase %s has %d result files, capping at %d",
                    phase_number, len(result_files), _MAX_RESULT_FILES,
                )
                result_files = result_files[-_MAX_RESULT_FILES:]
            for rf in result_files:
                text = _safe_read(rf, f"task result {rf.name}")
                if text:
                    # Extract task_id from filename: RESULT_{task_id}.md
                    task_id_part = rf.stem.replace("RESULT_", "", 1)
                    summary = _read_summary(phase_dir, task_id_part)

                    if summary:
                        # Push summary at P3_LOW; full RESULT_{id}.md remains on disk as the source
                        budget.add(
                            ContextBudget.P3_LOW,
                            "prior_task_result",
                            summary,
                            source_path=str(rf),
                        )
                    else:
                        # No summary available: full content at P4_TRIM
                        budget.add(
                            ContextBudget.P4_TRIM,
                            "prior_task_result",
                            text,
                            source_path=str(rf),
                        )

    return budget.assemble()


def _get_task_specific_context(
    working_dir: str,
    phase_number: int | None,
    task_id: str | None,
) -> str:
    """Read per-task DISCUSS and RESEARCH files. Returns string (may be empty).

    These files are task-specific and must NOT be cached — different tasks
    within the same wave have different discuss/research files.
    """
    if phase_number is None or task_id is None:
        return ""

    try:
        _validate_path_component(task_id, "task_id")
    except ValueError:
        logger.warning("get_phase_context: invalid task_id %r, skipping per-task files", task_id)
        return ""

    ctx = _ctx_dir(working_dir)
    phases_dir = ctx / "phases"
    if not phases_dir.exists():
        return ""

    parts: list[str] = []

    discuss_path = phases_dir / str(phase_number) / f"DISCUSS_{task_id}.md"
    if discuss_path.exists():
        text = _safe_read(discuss_path, f"task {task_id} discuss")
        if text is not None:
            parts.append(
                f"<task_decisions>\n{text}\n</task_decisions>"
            )

    research_path = phases_dir / str(phase_number) / f"RESEARCH_{task_id}.md"
    if research_path.exists():
        text = _safe_read(research_path, f"task {task_id} research")
        if text is not None:
            parts.append(
                f"<task_research>\n{text}\n</task_research>"
            )

    if not parts:
        return ""

    return "\n\n" + "\n\n".join(parts)


def get_phase_context(
    working_dir: str,
    phase_number: int | None,
    task_id: str | None = None,
    prior_wave_task_ids: list[str] | None = None,
    all_expert_names: list[str] | None = None,
    state_summary: str = "",
    budget_chars: int = 150_000,
) -> tuple[str, list[dict]]:
    """Build the full context string to inject into a task's system prompt.

    Includes: PROJECT.md, REQUIREMENTS.md, ROADMAP.md,
    research/SUMMARY.md, current phase PLAN.md, prior phase REVIEW.md files,
    task results from prior waves, and per-task discuss/research files.

    File read errors are treated as non-fatal: a warning is logged and the
    affected section is omitted so that a single unreadable file does not
    abort context assembly for the entire task.

    Base content (everything except per-task DISCUSS/RESEARCH files) is cached
    per (working_dir, phase_number) with a TTL. Explicit invalidation is
    triggered by write_task_result() and write_phase_review().

    Returns ``(context_text, manifest)`` where *manifest* lists each section
    with its priority, size, and inclusion mode.
    """
    cache_key = (working_dir, phase_number)
    now = time.monotonic()

    with _phase_context_lock:
        cached = _phase_context_cache.get(cache_key)
        if cached is not None:
            cached_time, cached_content, cached_manifest = cached
            if now - cached_time < _PHASE_CONTEXT_TTL:
                base_content = cached_content
                manifest = cached_manifest
                # Append task-specific content outside the lock
                task_content = _get_task_specific_context(working_dir, phase_number, task_id)
                text = base_content + task_content if task_content else base_content
                return text, manifest

    # Cache miss — full read
    base_content, manifest = _build_budgeted_context(
        working_dir, phase_number,
        budget_chars=budget_chars, state_summary=state_summary,
    )

    with _phase_context_lock:
        _phase_context_cache[cache_key] = (now, base_content, manifest)

    task_content = _get_task_specific_context(working_dir, phase_number, task_id)
    text = base_content + task_content if task_content else base_content
    return text, manifest


def write_task_context_file(
    working_dir: str, task_id: str, context_text: str,
) -> str | None:
    """Write context to TASK_CONTEXT_{task_id}.md. Returns system prompt instruction or None."""
    if not context_text:
        return None
    ctx_file = _ctx_dir(working_dir) / f"TASK_CONTEXT_{task_id}.md"
    try:
        ctx_file.write_text(context_text, encoding="utf-8")
        rel_path = f".taktis/TASK_CONTEXT_{task_id}.md"
        return (f"\nIMPORTANT: Read the file {rel_path} "
                "for full project context before starting work.")
    except OSError:
        logger.warning("Failed to write TASK_CONTEXT_%s.md", task_id)
        return None


# ------------------------------------------------------------------
# Async wrappers — prevent blocking the event loop on slow filesystems
# ------------------------------------------------------------------

def cleanup_task_context_file(working_dir: str, task_id: str) -> None:
    """Remove ``.taktis/TASK_CONTEXT_{task_id}.md`` once a task has finished.

    The TASK_CONTEXT file is a per-task scratch artifact: graph_executor
    writes upstream results into it at task-create time so the agent can
    Read it. After completion, the same content is preserved in
    ``RESULT_{task_id}.md`` (and the agent's response is in the DB), so the
    scratch file is just cruft. Idempotent — safe to call when the file
    never existed or was already removed.
    """
    if not task_id:
        return
    path = _ctx_dir(working_dir) / f"TASK_CONTEXT_{task_id}.md"
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove %s: %s", path, exc)


async def async_cleanup_task_context_file(working_dir: str, task_id: str) -> None:
    """Non-blocking version of :func:`cleanup_task_context_file`."""
    await asyncio.to_thread(cleanup_task_context_file, working_dir, task_id)


async def async_write_task_result(
    working_dir: str,
    phase_number: int,
    task_id: str,
    result: str,
    *,
    task_name: str = "",
    wave: int = 0,
) -> None:
    """Non-blocking version of :func:`write_task_result`."""
    await asyncio.to_thread(
        write_task_result, working_dir, phase_number, task_id, result,
        task_name=task_name, wave=wave,
    )


async def async_get_phase_context(
    working_dir: str,
    phase_number: int | None,
    task_id: str | None = None,
    state_summary: str = "",
    budget_chars: int = 150_000,
) -> tuple[str, list[dict]]:
    """Non-blocking version of :func:`get_phase_context`."""
    return await asyncio.to_thread(
        get_phase_context, working_dir, phase_number,
        task_id=task_id, state_summary=state_summary,
        budget_chars=budget_chars,
    )


async def async_write_phase_review(
    working_dir: str, phase_number: int, content: str,
) -> None:
    """Non-blocking version of :func:`write_phase_review`."""
    await asyncio.to_thread(write_phase_review, working_dir, phase_number, content)


# ------------------------------------------------------------------
# Supersession marker — explicit opt-in banners on prior context files
# ------------------------------------------------------------------

_SUPERSEDE_MARKER_RE = re.compile(r"===SUPERSEDE:\s*([^=]+)===")
_SUPERSEDED_PREFIX = "> **SUPERSEDED**"


def _apply_supersession_sync(
    working_dir: str, task_id: str, phase_number: int | None, result_text: str,
) -> list[str]:
    if not result_text:
        return []
    match = _SUPERSEDE_MARKER_RE.search(result_text)
    if not match:
        return []

    ctx_root = _ctx_dir(working_dir).resolve()
    raw_list = match.group(1).strip()
    if not raw_list:
        return []

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    phase_ref = (
        f"phases/{phase_number}/RESULT_{task_id}.md"
        if phase_number is not None
        else f"RESULT_{task_id}.md"
    )
    banner = (
        f"> **SUPERSEDED** by task `{task_id}` on {now_iso}.\n"
        f"> See `{phase_ref}` for the decision.\n"
        "> The content below is historical and should NOT be treated as authoritative.\n\n"
        "---\n\n"
    )

    modified: list[str] = []
    for entry in raw_list.split(","):
        rel = entry.strip()
        if not rel:
            continue
        candidate = (ctx_root / rel)
        try:
            resolved = candidate.resolve()
            resolved.relative_to(ctx_root)
        except (ValueError, OSError):
            logger.warning(
                "apply_supersession: rejecting out-of-root path %r", rel,
            )
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            existing = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "apply_supersession: failed to read %s: %s", resolved, exc,
            )
            continue
        if existing.lstrip().startswith(_SUPERSEDED_PREFIX):
            continue
        try:
            resolved.write_text(banner + existing, encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "apply_supersession: failed to write %s: %s", resolved, exc,
            )
            continue
        modified.append(rel)
        logger.info("Supersession banner applied to %s (task %s)", rel, task_id)

    if modified:
        invalidate_phase_context(working_dir, phase_number)
    return modified


async def apply_supersession_if_marked(
    working_dir: str, task_id: str, phase_number: int | None, result_text: str,
) -> list[str]:
    """If result_text contains a SUPERSEDE marker, prepend a banner to each
    named .taktis/ file. Returns the list of files actually modified.

    Idempotent: files already beginning with the SUPERSEDED marker are skipped.
    Paths escaping .taktis/ are silently rejected.
    """
    return await asyncio.to_thread(
        _apply_supersession_sync, working_dir, task_id, phase_number, result_text,
    )


# ------------------------------------------------------------------
# DB-backed state summary — replaces file-based STATE.md
# ------------------------------------------------------------------


async def generate_state_summary(conn, project_id: str) -> str:
    """Query DB for phase/task status, return concise summary. Replaces STATE.md.

    Takes an already-open aiosqlite connection to avoid pool contention.
    """
    from taktis import repository as repo
    phases = await repo.list_phases(conn, project_id)
    if not phases:
        return ""
    lines = ["# Project State\n"]
    for ph in phases:
        name = ph.get("name", f"Phase {ph['phase_number']}")
        status = ph["status"]
        tasks = await repo.get_tasks_by_phase(conn, ph["id"])
        total = len(tasks)
        done = sum(1 for t in tasks if t["status"] == "completed")
        fail = sum(1 for t in tasks if t["status"] == "failed")
        run = sum(1 for t in tasks if t["status"] == "running")
        line = f"- **{name}** [{status}]: {done}/{total} done"
        if fail:
            line += f", {fail} failed"
        if run:
            line += f", {run} running"
        lines.append(line)
    return "\n".join(lines)
