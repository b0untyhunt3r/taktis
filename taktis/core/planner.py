"""Auto-planning: creates a planner task that chats with the user,
then generates phases and tasks from the structured output."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class PlanApplier(Protocol):
    """Interface for the subset of Taktis that apply_plan needs.

    Breaks the circular dependency: planner.py no longer needs to know
    about the full Taktis type.
    """

    async def get_project(self, name: str) -> dict | None: ...
    async def create_phase(self, *, project_name: str, name: str, goal: str) -> dict: ...
    async def create_task(
        self, *, project_name: str, prompt: str, phase_number: int,
        wave: int = 1, expert: str | None = None,
    ) -> dict: ...
    async def delete_phase(self, project_name: str, phase_number: int) -> bool: ...
    async def list_experts(self) -> list[dict]: ...
    async def publish_event(self, event: str, data: dict) -> None: ...

def _repair_json(text: str) -> str:
    """Attempt to fix common JSON issues from LLM output.

    Handles: unescaped newlines/tabs inside strings, trailing commas.
    """
    # Fix unescaped control characters inside JSON string values.
    # Walk through the text tracking whether we're inside a string.
    result = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == '\\' and i + 1 < len(text):
                # Escaped character — keep as-is
                result.append(ch)
                result.append(text[i + 1])
                i += 2
                continue
            elif ch == '"':
                in_string = False
                result.append(ch)
            elif ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            else:
                result.append(ch)
        else:
            if ch == '"':
                in_string = True
            result.append(ch)
        i += 1

    repaired = ''.join(result)
    # Remove trailing commas before } or ]
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
    return repaired


def _extract_plan_lenient(text: str) -> dict[str, Any] | None:
    """Lenient plan extraction when JSON is broken.

    LLM-generated JSON often has unescaped quotes/newlines inside prompt
    strings (especially when prompts contain HTML).  This function
    re-escapes prompt values using the "wave" key as a reliable delimiter,
    then retries parsing.  Falls back to truncating prompts if that fails.
    """
    # Strategy 1: re-escape prompt values using "wave" as end-delimiter.
    # Pattern: "prompt": "...(anything)...",\n  "wave"
    # We capture the raw content and re-escape it with json.dumps.
    def _fix_prompts(json_str: str) -> str:
        pattern = r'"prompt":\s*"(.*?)"\s*,\s*\n(\s*"wave")'
        def _replacer(m):
            content = m.group(1)
            indent_wave = m.group(2)
            escaped = json.dumps(content)[1:-1]  # strip outer quotes
            return f'"prompt": "{escaped}",\n{indent_wave}'
        return re.sub(pattern, _replacer, json_str, flags=re.DOTALL)

    fixed = _fix_prompts(text)
    try:
        data = json.loads(fixed)
        if "phases" in data:
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    # Strategy 2: truncate prompts entirely as last resort
    cleaned = re.sub(
        r'"prompt"\s*:\s*".*?"(?=\s*,\s*"wave")',
        '"prompt": "(prompt truncated for parsing)"',
        text,
        flags=re.DOTALL,
    )
    try:
        data = json.loads(cleaned)
        if "phases" in data:
            logger.warning("Plan parsed with truncated prompts")
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _try_parse_plan_match(raw_match: str) -> dict[str, Any] | None:
    """Try parsing a regex match as a plan JSON (as-is, repaired, lenient)."""
    raw = raw_match.strip()
    for attempt_text in [raw, _repair_json(raw)]:
        try:
            data = json.loads(attempt_text)
            if "phases" in data and isinstance(data["phases"], list):
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return _extract_plan_lenient(raw)


def parse_plan_output(result_text: str) -> dict[str, Any] | None:
    """Extract the JSON plan from Claude's response.

    Looks for a ```json ... ``` block containing the plan structure.
    Returns the parsed dict or None if not found/invalid.
    """
    if not result_text:
        return None

    # Try to find ```json ... ``` block
    # Phase 1: non-greedy — handles multiple distinct JSON blocks (last one wins)
    for match in reversed(re.findall(r"```json\s*\n(.*?)```", result_text, re.DOTALL)):
        result = _try_parse_plan_match(match)
        if result:
            return result

    # Phase 2: greedy — handles embedded ``` inside JSON (e.g. code examples
    # in task prompts).  Captures from first ```json to the LAST ```.
    for match in re.findall(r"```json\s*\n(.*)```", result_text, re.DOTALL):
        result = _try_parse_plan_match(match)
        if result:
            return result

    # Fallback: try to find raw JSON object with "phases" key
    try:
        # Find the largest JSON object in the text
        brace_depth = 0
        start = None
        for i, ch in enumerate(result_text):
            if ch == "{":
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and start is not None:
                    candidate = result_text[start : i + 1]
                    try:
                        data = json.loads(candidate)
                        if "phases" in data:
                            return data
                    except (json.JSONDecodeError, TypeError):
                        pass
                    start = None
    except Exception:
        pass

    logger.warning("Could not parse plan JSON from result")
    return None


_FILE_EXT_PATTERN = re.compile(
    r'(?:^|[\s`"\'/\\(])(\w[\w./-]*\.(?:html|css|js|json|md|py|ts|tsx|jsx|vue|svelte|yaml|yml|toml|cfg|ini|sh|sql))\b',
)

# Only files inside an explicit write section ("FILES TO CREATE:",
# "FILES TO WRITE:", etc.) count as write-collisions. Read-only references
# like `shared/content.json` mentioned in prompt prose must not trigger
# same-wave bumps, or N fan-out tasks that share the same input will be
# serialized into N separate waves.
_WRITE_SECTION_PATTERN = re.compile(
    r'FILES?\s+TO\s+(?:CREATE|WRITE|MODIFY|EDIT|UPDATE|CHANGE):\s*\n?(.+?)'
    r'(?=\n\s*\n|\n[A-Z][A-Z \-]+:|\Z)',
    re.IGNORECASE | re.DOTALL,
)


def _extract_write_files(prompt: str) -> set[str]:
    """Return file paths the task will WRITE, for collision detection.

    Prefers an explicit ``FILES TO CREATE:`` / ``FILES TO WRITE:`` /
    ``FILES TO MODIFY:`` section (the Roadmapper emits this on every
    task).  Falls back to scanning the whole prompt only when no such
    section exists, for back-compat with agents that don't emit it.
    """
    write_sections = _WRITE_SECTION_PATTERN.findall(prompt)
    scan = "\n".join(write_sections) if write_sections else prompt
    return {m.lower() for m in _FILE_EXT_PATTERN.findall(scan)}


def _auto_assign_waves(tasks_data: list[dict]) -> list[dict]:
    """Validate and fix wave assignments based on file overlap.

    Preserves the roadmapper's wave assignments but checks for file
    collisions within each wave.  Tasks that conflict with a same-wave
    peer are bumped to the next available wave — all other assignments
    are left untouched.

    Tasks without an explicit wave (wave 0 or missing) are assigned
    using greedy file-overlap analysis.
    """
    # Extract file paths each task will write (keep full path for accurate
    # collision detection — design-01/index.html ≠ design-02/index.html).
    # Uses the prompt's explicit write-section when present so shared read
    # inputs don't get flagged as collisions.
    task_files: list[set[str]] = [
        _extract_write_files(t.get("prompt", "")) for t in tasks_data
    ]

    if not tasks_data:
        return tasks_data

    # If all tasks have explicit waves, only fix actual file collisions
    all_have_waves = all(t.get("wave", 0) >= 1 for t in tasks_data)

    if all_have_waves:
        # Validate: within each wave, check for file collisions and bump
        max_wave = max(t["wave"] for t in tasks_data)
        for wave_num in range(1, max_wave + 1):
            indices = [i for i, t in enumerate(tasks_data) if t["wave"] == wave_num]
            seen_files: dict[int, set[str]] = {}  # idx -> files (already placed)
            for i in indices:
                if not task_files[i]:
                    continue
                conflict = any(task_files[i] & task_files[j] for j in seen_files)
                if conflict:
                    tasks_data[i]["wave"] = max_wave + 1
                    max_wave = tasks_data[i]["wave"]
                else:
                    seen_files[i] = task_files[i]
        return tasks_data

    # Fallback: greedy wave assignment for tasks missing wave numbers
    waves: list[list[int]] = []
    for i, files in enumerate(task_files):
        placed = False
        for wave_idx, wave_task_indices in enumerate(waves):
            conflict = any(files & task_files[j] for j in wave_task_indices)
            if not conflict:
                wave_task_indices.append(i)
                tasks_data[i]["wave"] = wave_idx + 1
                placed = True
                break
        if not placed:
            waves.append([i])
            tasks_data[i]["wave"] = len(waves)

    return tasks_data


async def apply_plan(applier: PlanApplier, project_name: str, plan: dict[str, Any]) -> dict:
    """Create phases and tasks from a parsed plan dict.

    *applier* must satisfy the :class:`PlanApplier` protocol — typically
    the :class:`Taktis` facade (which implements it directly) or a
    thin adapter.

    Returns a summary dict with counts.
    """
    from taktis.core.context import (
        init_context,
        write_phase_plan,
    )

    project = await applier.get_project(project_name)
    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    # Pre-validate all expert names before creating anything
    referenced_experts: set[str] = set()
    for phase_data in plan.get("phases", []):
        for task_data in phase_data.get("tasks", []):
            expert_name = task_data.get("expert")
            if expert_name:
                referenced_experts.add(expert_name)

    if referenced_experts:
        all_experts = await applier.list_experts()
        known_names = {e["name"] for e in all_experts}
        # Normalize: LLMs sometimes use underscores instead of hyphens
        known_normalized = {n.replace("_", "-").lower(): n for n in known_names}
        # Auto-fix mismatched names in the plan before validation
        for phase_data in plan.get("phases", []):
            for task_data in phase_data.get("tasks", []):
                expert_name = task_data.get("expert")
                if expert_name and expert_name not in known_names:
                    norm = expert_name.replace("_", "-").lower()
                    fixed = known_normalized.get(norm)
                    # Try with -general suffix (original experts were renamed)
                    if not fixed:
                        fixed = known_normalized.get(norm + "-general")
                    # Try prefix match (e.g. "architect" -> "architect-general")
                    if not fixed:
                        candidates = [v for k, v in known_normalized.items()
                                      if k.startswith(norm + "-") or k.startswith(norm)]
                        if len(candidates) == 1:
                            fixed = candidates[0]
                    if fixed:
                        logger.info("Auto-fixed expert name: %r -> %r", expert_name, fixed)
                        task_data["expert"] = fixed
        # Re-collect after fixes
        referenced_experts = {
            t.get("expert") for p in plan.get("phases", [])
            for t in p.get("tasks", []) if t.get("expert")
        }
        invalid = referenced_experts - known_names
        if invalid:
            raise ValueError(
                f"Plan references unknown expert(s): {', '.join(sorted(invalid))}. "
                f"Known experts: {', '.join(sorted(known_names))}"
            )

    working_dir = project.get("working_dir", ".")
    summary = plan.get("project_summary", "")

    # Update PROJECT.md with the planning summary
    init_context(working_dir, project_name, summary)

    phases_created = 0
    tasks_created = 0
    created_phase_numbers: list[int] = []  # Track for rollback on failure

    try:
        for phase_data in plan.get("phases", []):
            phase_name = phase_data.get("name", f"Phase {phases_created + 1}")
            # Strip "Phase N —" or "Phase N:" prefix to avoid "Phase 2: Phase 1 — ..."
            phase_name = re.sub(r'^Phase\s+\d+\s*[—:–-]\s*', '', phase_name).strip() or phase_name
            phase_goal = phase_data.get("goal", "")
            phase_tasks = phase_data.get("tasks", [])
            # Richer fields from roadmapper
            success_criteria = phase_data.get("success_criteria", [])
            requirements = phase_data.get("requirements", [])

            # Build an enriched goal with success criteria
            enriched_goal = phase_goal
            if success_criteria:
                enriched_goal += "\n\nSuccess Criteria:\n" + "\n".join(
                    f"- {c}" for c in success_criteria
                )
            if requirements:
                enriched_goal += f"\n\nRequirements: {', '.join(requirements)}"

            # Create phase
            phase = await applier.create_phase(
                project_name=project_name,
                name=phase_name,
                goal=enriched_goal,
            )
            phase_number = phase["phase_number"]
            created_phase_numbers.append(phase_number)
            phases_created += 1

            # Auto-assign waves based on file overlap in prompts
            phase_tasks = _auto_assign_waves(phase_tasks)

            # Write phase PLAN.md
            write_phase_plan(working_dir, phase_number, phase_name, enriched_goal, phase_tasks)

            # Create tasks
            for task_data in phase_tasks:
                prompt = task_data.get("prompt", "")
                if not prompt:
                    continue

                wave = task_data.get("wave", 1)
                expert = task_data.get("expert")

                await applier.create_task(
                    project_name=project_name,
                    prompt=prompt,
                    phase_number=phase_number,
                    wave=wave,
                    expert=expert,
                )
                tasks_created += 1
    except Exception:
        # Rollback: delete any phases we created (cascades to their tasks)
        for pn in reversed(created_phase_numbers):
            try:
                await applier.delete_phase(project_name=project_name, phase_number=pn)
            except Exception as cleanup_exc:
                logger.warning(
                    "apply_plan rollback: failed to delete phase %d of '%s': %s",
                    pn, project_name, cleanup_exc,
                )
        raise

    logger.info(
        "Applied plan for '%s': %d phases, %d tasks",
        project_name, phases_created, tasks_created,
    )

    # Notify SSE clients that new phases were created so they can reload
    if phases_created > 0:
        from taktis.core.events import EVENT_PHASE_COMPLETED
        try:
            await applier.publish_event(
                EVENT_PHASE_COMPLETED,
                {
                    "project_name": project_name,
                    "project_id": project.get("id", ""),
                    "phase_id": "",
                    "status": "plan_applied",
                    "phases_created": phases_created,
                },
            )
        except Exception:
            logger.debug("Could not publish plan_applied event for '%s'", project_name)

    return {
        "phases_created": phases_created,
        "tasks_created": tasks_created,
        "project_summary": summary,
    }
