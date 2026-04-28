"""Generic graph executor for pipeline templates.

Translates a Drawflow JSON graph into executable waves and runs them
using the existing task infrastructure (ProcessManager, SDKProcess,
WaveScheduler patterns, EventBus).

Supports multiple node types:
- ``agent``             — unified LLM task (standard, template, or interview mode)
- ``output_parser``     — instant: split text by markers into named sections
- ``file_writer``       — instant: write upstream result to .taktis/ file
- ``plan_applier``      — instant: parse JSON plan → create phases/tasks in DB
- ``text_transform``    — instant: transform upstream text (prepend, append, replace, extract, wrap)
- ``fan_out``           — split upstream into items and run parallel agent tasks per item
- ``loop``              — review-fix cycle: evaluate condition, retry upstream agent on failure
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Optional

from datetime import datetime, timezone

from taktis import repository as repo
from taktis.core.events import (
    EVENT_PHASE_COMPLETED,
    EVENT_PHASE_FAILED,
    EVENT_PHASE_STARTED,
    EVENT_PIPELINE_GATE_WAITING,
    EVENT_PIPELINE_PLAN_READY,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_WAVE_COMPLETED,
    EVENT_WAVE_STARTED,
)
from taktis.core.node_types import get_node_type
from taktis.exceptions import PipelineError, SchedulerError

logger = logging.getLogger(__name__)


def _substitute_filename_placeholders(filename: str) -> str:
    """Replace `{{date}}` / `{{datetime}}` / `{{week_num}}` / `{{year}}` in a
    file_writer filename so cron-friendly templates produce dated paths.
    """
    if "{{" not in filename:
        return filename
    now = datetime.now(timezone.utc)
    return (
        filename
        .replace("{{date}}", now.strftime("%Y-%m-%d"))
        .replace("{{datetime}}", now.strftime("%Y-%m-%dT%H-%M"))
        .replace("{{week_num}}", f"{now.isocalendar().week:02d}")
        .replace("{{year}}", now.strftime("%Y"))
    )


# ---------------------------------------------------------------------------
# Prompt template lookup — DB-backed via agent_templates table
# ---------------------------------------------------------------------------

async def _get_template_from_db(session_factory, slug: str) -> dict:
    """Look up agent template by slug from DB.

    Returns a dict with at least ``slug``, ``prompt_text``,
    ``auto_variables``, ``internal_variables``.
    """
    async with session_factory() as conn:
        row = await repo.get_agent_template_by_slug(conn, slug)
    if row is not None:
        return row
    raise PipelineError(f"Unknown prompt template: {slug!r}", step="template_lookup")


async def get_template_variables(session_factory) -> dict[str, list[dict]]:
    """Return ``{slug: [{name, auto}]}`` for the pipeline editor UI.

    ``auto`` variables are injected by checkboxes (inject_description,
    inject_expert_options).  The rest need explicit mapping to upstream nodes.
    """
    import re as _re

    async with session_factory() as conn:
        templates = await repo.list_agent_templates(conn)
    result: dict[str, list[dict]] = {}
    for t in templates:
        tmpl = t["prompt_text"]
        cleaned = _re.sub(r"\{\{|\}\}", "", tmpl)
        raw_vars = set(_re.findall(r"\{(\w+)\}", cleaned))
        internal = set(json.loads(t["internal_variables"] or "[]")
                       if isinstance(t.get("internal_variables"), str)
                       else (t.get("internal_variables") or []))
        auto = set(json.loads(t["auto_variables"] or "[]")
                   if isinstance(t.get("auto_variables"), str)
                   else (t.get("auto_variables") or []))
        entries = []
        for v in sorted(raw_vars):
            if v in internal:
                continue
            entries.append({"name": v, "auto": v in auto})
        result[t["slug"]] = entries
    return result


async def get_template_texts(session_factory) -> dict[str, str]:
    """Return ``{slug: raw_text}`` for the editor's "Load Preset" feature."""
    async with session_factory() as conn:
        templates = await repo.list_agent_templates(conn)
    return {t["slug"]: t["prompt_text"] for t in templates}


async def get_template_list(session_factory) -> list[dict]:
    """Return list of ``{slug, name}`` for pipeline editor dropdowns."""
    async with session_factory() as conn:
        templates = await repo.list_agent_templates(conn)
    return [{"slug": t["slug"], "name": t["name"]} for t in templates]


# Node types that run as LLM tasks (create a DB task, start via ProcessManager)
_LLM_NODE_TYPES = frozenset({"agent", "llm_router"})


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """Parsed representation of a single Drawflow node."""
    drawflow_id: str            # Drawflow node ID (string)
    node_type: str              # "personality", etc.
    name: str                   # User-supplied label
    data: dict                  # Node config (expert, prompt, model, …)
    upstream: list[str] = field(default_factory=list)    # IDs of input nodes
    downstream: list[str] = field(default_factory=list)  # IDs of output nodes
    wave: int = 0               # Assigned by topological sort
    task_id: Optional[str] = None  # Set once a task is created for this node
    # Port-level downstream: {output_port: [target_node_ids]}
    downstream_ports: dict[str, list[str]] = field(default_factory=dict)
    skipped: bool = False       # Set by conditional nodes to skip branches


# ---------------------------------------------------------------------------
# Graph parsing
# ---------------------------------------------------------------------------

def _parse_module_nodes(module_data: dict) -> list[GraphNode]:
    """Parse nodes from a single Drawflow module's ``data`` dict."""
    nodes: dict[str, GraphNode] = {}

    for node_id_str, node_data in module_data.items():
        nid = str(node_data.get("id", node_id_str))
        data = node_data.get("data", {})
        node = GraphNode(
            drawflow_id=nid,
            node_type=node_data.get("name", "unknown"),
            name=data.get("name", data.get("label", f"Node {nid}")),
            data=data,
        )
        nodes[nid] = node

    for node_id_str, node_data in module_data.items():
        nid = str(node_data.get("id", node_id_str))
        if nid not in nodes:
            continue
        for port_name, port_info in (node_data.get("outputs") or {}).items():
            for conn in (port_info.get("connections") or []):
                target = str(conn["node"])
                if target in nodes:
                    nodes[nid].downstream.append(target)
                    nodes[target].upstream.append(nid)
                    # Track port-level connections for conditional routing
                    nodes[nid].downstream_ports.setdefault(port_name, []).append(target)

    return list(nodes.values())


def parse_drawflow_graph(flow_json: dict) -> list[GraphNode]:
    """Convert Drawflow export JSON into a flat list of :class:`GraphNode`.

    For single-module graphs, returns nodes from the first module found.
    """
    drawflow = flow_json.get("drawflow", flow_json)
    if isinstance(drawflow, dict):
        for module_name, module_data in drawflow.items():
            if isinstance(module_data, dict) and "data" in module_data:
                return _parse_module_nodes(module_data["data"])
        # Fallback: drawflow IS the data dict
        if all(isinstance(v, dict) and "name" in v for v in drawflow.values()):
            return _parse_module_nodes(drawflow)
    return []


def parse_drawflow_graph_multi(flow_json: dict) -> dict[str, list[GraphNode]]:
    """Parse a multi-module Drawflow export into ``{module_name: [GraphNode]}``.

    Preserves module insertion order. Each module's nodes are parsed independently.
    """
    drawflow = flow_json.get("drawflow", flow_json)
    result: dict[str, list[GraphNode]] = {}
    if isinstance(drawflow, dict):
        for module_name, module_data in drawflow.items():
            if isinstance(module_data, dict) and "data" in module_data:
                nodes = _parse_module_nodes(module_data["data"])
                if nodes:
                    result[module_name] = nodes
    return result


def topological_sort_waves(nodes: list[GraphNode]) -> list[list[GraphNode]]:
    """Assign wave numbers via Kahn's algorithm. Raises on cycle."""
    node_map = {n.drawflow_id: n for n in nodes}
    in_degree: dict[str, int] = {n.drawflow_id: 0 for n in nodes}

    for n in nodes:
        for up_id in n.upstream:
            if up_id in node_map:
                in_degree[n.drawflow_id] += 1

    queue: deque[str] = deque()
    for nid, deg in in_degree.items():
        if deg == 0:
            queue.append(nid)
            node_map[nid].wave = 1

    waves: dict[int, list[GraphNode]] = defaultdict(list)
    visited = 0

    while queue:
        nid = queue.popleft()
        node = node_map[nid]
        waves[node.wave].append(node)
        visited += 1
        for down_id in node.downstream:
            if down_id not in node_map:
                continue
            in_degree[down_id] -= 1
            node_map[down_id].wave = max(node_map[down_id].wave, node.wave + 1)
            if in_degree[down_id] == 0:
                queue.append(down_id)

    if visited != len(nodes):
        raise SchedulerError("Graph contains a cycle — cannot execute")
    return [waves[w] for w in sorted(waves)]


# ---------------------------------------------------------------------------
# Graph executor
# ---------------------------------------------------------------------------

class GraphExecutor:
    """Execute a Drawflow graph against a project.

    Handles all node types: LLM tasks (agent) and
    instant nodes (output_parser, file_writer, plan_applier, conditional).
    """

    def __init__(
        self,
        engine: Any,
        project_name: str,
        flow_json: dict | str,
        template_name: str = "Custom Flow",
    ) -> None:
        self._orch = engine
        self._project_name = project_name
        self._flow_json = (
            json.loads(flow_json) if isinstance(flow_json, str) else flow_json
        )
        self._template_name = template_name
        self._results: dict[str, str] = {}  # node_id → result text/JSON
        self._node_map: dict[str, GraphNode] = {}
        self._phase_number: int | None = None
        self._phase_id: str | None = None
        self._project: dict | None = None
        self._pending_gates: dict[str, dict] = {}  # node_id → {event, approved}
        self._cancelled = False
        self._state_summary: str = ""
        self._phase_context_config: dict = {}

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    async def execute(self) -> str:
        """Parse, create phase, execute wave by wave. Returns phase_id."""
        nodes = parse_drawflow_graph(self._flow_json)
        if not nodes:
            raise PipelineError("Graph is empty — nothing to execute", step="parse")

        waves = topological_sort_waves(nodes)
        logger.info(
            "Graph parsed: %d nodes in %d waves for project '%s'",
            len(nodes), len(waves), self._project_name,
        )

        # Create a phase (designer_phase flag prevents scheduler auto-complete)
        phase = await self._orch.create_phase(
            self._project_name,
            name=f"Flow: {self._template_name}",
            goal=f"Execute pipeline template '{self._template_name}'",
            description=f"Auto-generated phase with {len(nodes)} nodes in {len(waves)} waves",
            context_config=json.dumps({"designer_phase": True, "template_name": self._template_name}),
        )
        self._phase_id = phase["id"]
        self._phase_number = phase["phase_number"]
        self._project = await self._orch.get_project(self._project_name)
        project_id = self._project["id"]

        # Apply ${VAR} env-var substitution to soft-substitutable fields once
        # the project is loaded, so prompts/filenames/etc. are resolved before
        # any wave runs.
        self._pre_substitute_env_vars(nodes)

        self._node_map = {n.drawflow_id: n for n in nodes}
        self._phase_context_config = {"designer_phase": True}

        # Generate state summary once for the whole execution
        try:
            from taktis.core.context import generate_state_summary
            session_factory = self._orch._execution_service._session_factory
            async with session_factory() as conn:
                self._state_summary = await generate_state_summary(conn, project_id)
        except Exception:
            logger.warning("Failed to generate state summary", exc_info=True)
            self._state_summary = ""

        phase_failed = await self._run_waves(waves, project_id)

        # Finalize phase
        session_factory = self._orch._execution_service._session_factory
        async with session_factory() as conn:
            final_status = "failed" if phase_failed else "complete"
            await repo.update_phase(conn, self._phase_id, status=final_status)

        event = EVENT_PHASE_FAILED if phase_failed else EVENT_PHASE_COMPLETED
        await self._orch.event_bus.publish(event, {
            "phase_id": self._phase_id,
            "project_id": project_id,
            "project_name": self._project_name,
        })

        return self._phase_id

    async def execute_multi(self) -> str | list[str]:
        """Execute a multi-module graph. Each module = one phase.

        If the graph has a single module without a phase_settings node,
        delegates to :meth:`execute` for backward compatibility.
        Returns a single phase_id or a list of phase_ids.
        """
        modules = parse_drawflow_graph_multi(self._flow_json)

        if not modules:
            raise PipelineError("Graph is empty — nothing to execute", step="parse")

        # Single module without phase_settings → delegate to existing execute()
        if len(modules) == 1:
            module_nodes = next(iter(modules.values()))
            has_phase_settings = any(n.node_type == "phase_settings" for n in module_nodes)
            if not has_phase_settings:
                return await self.execute()

        self._project = await self._orch.get_project(self._project_name)
        project_id = self._project["id"]

        # Apply ${VAR} env-var substitution across all nodes once.
        all_nodes = [n for nodes in modules.values() for n in nodes]
        self._pre_substitute_env_vars(all_nodes)

        # Provision shared reference files into the project's .taktis/
        self._provision_reference_files()

        phase_ids: list[str] = []

        for module_name, module_nodes in modules.items():
            phase_id = await self._execute_module(
                module_name, module_nodes, project_id,
            )
            phase_ids.append(phase_id)

        return phase_ids

    async def _execute_module(
        self,
        module_name: str,
        nodes: list[GraphNode],
        project_id: str,
    ) -> str:
        """Execute a single module as its own phase. Returns phase_id."""
        # Find phase_settings node (if any)
        phase_settings_node = None
        exec_nodes = []
        for n in nodes:
            if n.node_type == "phase_settings":
                phase_settings_node = n
            else:
                exec_nodes.append(n)

        # Phase metadata from phase_settings or defaults
        if phase_settings_node:
            ps = phase_settings_node.data
            phase_name = ps.get("phase_name") or module_name
            phase_goal = ps.get("phase_goal", "")
            criteria_raw = ps.get("success_criteria", "")
            success_criteria = [c.strip() for c in criteria_raw.split("\n") if c.strip()] if criteria_raw else []
            # Context files selected by the designer
            context_files_raw = ps.get("context_files", "")
            if isinstance(context_files_raw, str):
                context_files = [f.strip() for f in context_files_raw.split(",") if f.strip()]
            elif isinstance(context_files_raw, list):
                context_files = context_files_raw
            else:
                context_files = []
        else:
            phase_name = module_name
            phase_goal = f"Execute module '{module_name}'"
            success_criteria = []
            context_files = []

        # Merge in files from context manifest (written by prior phases' file_writers)
        working_dir = self._project.get("working_dir", "") if self._project else ""
        from taktis.core.context import read_context_manifest
        manifest = read_context_manifest(working_dir)
        all_context_files = list(context_files)
        for mf in manifest:
            if mf not in all_context_files:
                all_context_files.append(mf)

        # Read per-phase review setting from phase_settings node
        phase_review = False
        if phase_settings_node:
            pr = phase_settings_node.data.get("phase_review", False)
            phase_review = pr is True or pr == "true" or pr == "on"

        # Build context_config JSON
        context_config = json.dumps({
            "designer_phase": True,
            "context_files": all_context_files,
            "template_name": self._template_name,
            "module_name": module_name,
            "phase_review": phase_review,
        })

        # Store context config for use by _create_and_start_llm_node
        self._phase_context_config = {
            "designer_phase": True,
            "context_files": all_context_files,
            "template_name": self._template_name,
            "module_name": module_name,
        }

        # Generate state summary once for the whole module
        try:
            from taktis.core.context import generate_state_summary
            session_factory = self._orch._execution_service._session_factory
            async with session_factory() as conn:
                self._state_summary = await generate_state_summary(
                    conn, project_id,
                )
        except Exception:
            logger.warning("Failed to generate state summary for pipeline", exc_info=True)
            self._state_summary = ""

        if not exec_nodes:
            raise PipelineError(
                f"Module '{module_name}' has no executable nodes", step="parse",
            )

        waves = topological_sort_waves(exec_nodes)
        logger.info(
            "Module '%s': %d nodes in %d waves",
            module_name, len(exec_nodes), len(waves),
        )

        # Create phase with context_config
        phase = await self._orch.create_phase(
            self._project_name,
            name=phase_name,
            goal=phase_goal,
            description=f"Designer phase with {len(exec_nodes)} nodes in {len(waves)} waves",
            success_criteria=success_criteria,
            context_config=context_config,
        )
        self._phase_id = phase["id"]
        self._phase_number = phase["phase_number"]
        self._node_map.update({n.drawflow_id: n for n in exec_nodes})

        phase_failed = await self._run_waves(waves, project_id)

        # Finalize phase
        session_factory = self._orch._execution_service._session_factory
        async with session_factory() as conn:
            final_status = "failed" if phase_failed else "complete"
            await repo.update_phase(conn, self._phase_id, status=final_status)

        event = EVENT_PHASE_FAILED if phase_failed else EVENT_PHASE_COMPLETED
        await self._orch.event_bus.publish(event, {
            "phase_id": self._phase_id,
            "project_id": project_id,
            "project_name": self._project_name,
        })

        return self._phase_id

    async def resume_flow(self, phase_id: str) -> str:
        """Resume a dead pipeline from where it left off.

        Reconstructs ``_results`` from completed DB tasks, re-executes instant
        nodes (deterministic), and runs remaining LLM tasks normally.
        Auto-approves plan_applier (user explicitly chose to resume).
        """
        self._project = await self._orch.get_project(self._project_name)
        if self._project is None:
            raise PipelineError("Project not found", step="resume")
        project_id = self._project["id"]

        # Load phase
        session_factory = self._orch._execution_service._session_factory
        async with session_factory() as conn:
            phase_row = await repo.get_phase_by_id(conn, phase_id)
        if phase_row is None:
            raise PipelineError(f"Phase {phase_id} not found", step="resume")
        self._phase_id = phase_id
        self._phase_number = phase_row["phase_number"]

        # Parse context_config
        cc_raw = phase_row.get("context_config") or "{}"
        try:
            cc = json.loads(cc_raw) if isinstance(cc_raw, str) else cc_raw
        except (json.JSONDecodeError, TypeError):
            cc = {}
        self._phase_context_config = cc

        # Generate state summary
        try:
            from taktis.core.context import generate_state_summary
            async with session_factory() as conn:
                self._state_summary = await generate_state_summary(conn, project_id)
        except Exception:
            self._state_summary = ""

        # Parse the graph and select the correct module for this phase
        modules = parse_drawflow_graph_multi(self._flow_json)
        target_module_name = cc.get("module_name") or phase_row.get("name", "")
        target_nodes = None

        # Try exact match on module_name from context_config
        if target_module_name and target_module_name in modules:
            target_nodes = modules[target_module_name]
        else:
            # Fall back: match phase name against module names or phase_settings
            for mod_name, mod_nodes in modules.items():
                # Check phase_settings node for matching phase_name
                for n in mod_nodes:
                    if n.node_type == "phase_settings":
                        ps_name = n.data.get("phase_name", "")
                        if ps_name == phase_row.get("name", ""):
                            target_nodes = mod_nodes
                            break
                if target_nodes:
                    break
                # Check if module name matches phase name
                if mod_name == phase_row.get("name", ""):
                    target_nodes = mod_nodes
                    break

        if target_nodes is None:
            # Last resort: if only one module, use it
            if len(modules) == 1:
                target_nodes = next(iter(modules.values()))
            else:
                raise PipelineError(
                    f"Cannot find module for phase '{phase_row.get('name', '')}' in template with modules: {list(modules.keys())}",
                    step="resume",
                )

        exec_nodes = [n for n in target_nodes if n.node_type != "phase_settings"]
        if not exec_nodes:
            raise PipelineError("No executable nodes found in graph", step="resume")

        waves = topological_sort_waves(exec_nodes)
        self._node_map = {n.drawflow_id: n for n in exec_nodes}

        # Load completed tasks for this phase and match to nodes
        async with session_factory() as conn:
            tasks = await repo.get_tasks_by_phase(conn, phase_id)

        # Match tasks to nodes by (wave, name) — more reliable than name alone
        task_by_wave_name: dict[tuple[int, str], dict] = {}
        for t in tasks:
            key = (t["wave"], t["name"])
            task_by_wave_name[key] = t

        # Reconstruct _results from completed tasks
        completed_node_ids: set[str] = set()
        for node in exec_nodes:
            if node.node_type in _LLM_NODE_TYPES:
                key = (node.wave, node.name)
                task = task_by_wave_name.get(key)
                if task and task["status"] == "completed":
                    result = await self._get_task_result(task["id"])
                    if result:
                        self._results[node.drawflow_id] = result
                        node.task_id = task["id"]
                        completed_node_ids.add(node.drawflow_id)
                        logger.info(
                            "Resume: restored result for '%s' (task %s, %d chars)",
                            node.name, task["id"], len(result),
                        )

        logger.info(
            "Resume: restored %d/%d node results, running remaining waves",
            len(completed_node_ids), len([n for n in exec_nodes if n.node_type in _LLM_NODE_TYPES]),
        )

        # Set force_apply flag so plan_applier skips approval gate
        self._force_apply = True

        # Update phase status
        async with session_factory() as conn:
            await repo.update_phase(conn, phase_id, status="in_progress")

        # Run waves — completed nodes will be skipped, instant nodes re-executed
        phase_failed = await self._run_waves_resume(waves, project_id, completed_node_ids)

        # Finalize phase
        async with session_factory() as conn:
            final_status = "failed" if phase_failed else "complete"
            await repo.update_phase(conn, self._phase_id, status=final_status)

        from taktis.core.events import EVENT_PHASE_FAILED, EVENT_PHASE_COMPLETED
        event = EVENT_PHASE_FAILED if phase_failed else EVENT_PHASE_COMPLETED
        await self._orch.event_bus.publish(event, {
            "phase_id": self._phase_id,
            "project_id": project_id,
            "project_name": self._project_name,
        })

        return self._phase_id

    async def _run_waves_resume(
        self,
        waves: list[list[GraphNode]],
        project_id: str,
        completed_node_ids: set[str],
    ) -> bool:
        """Run waves with resume logic: skip completed LLM nodes, re-run instants."""
        event_bus = self._orch.event_bus
        phase_failed = False

        for wave_idx, wave_nodes in enumerate(waves, 1):
            if self._cancelled:
                phase_failed = True
                break

            wave_num = wave_nodes[0].wave if wave_nodes else wave_idx

            # Filter skipped nodes (set by conditionals in earlier waves)
            active_nodes = [n for n in wave_nodes if not n.skipped]

            # Split
            instant_nodes = [n for n in active_nodes if n.node_type not in _LLM_NODE_TYPES]
            llm_nodes = [n for n in active_nodes if n.node_type in _LLM_NODE_TYPES]

            # Check if ALL LLM nodes in this wave are already completed
            new_llm_nodes = [n for n in llm_nodes if n.drawflow_id not in completed_node_ids]
            resumed_llm = [n for n in llm_nodes if n.drawflow_id in completed_node_ids]

            if resumed_llm:
                logger.info(
                    "Resume wave %d: %d nodes already done (%s)",
                    wave_num, len(resumed_llm),
                    ", ".join(n.name for n in resumed_llm),
                )

            # Execute instant nodes (always re-run — they're deterministic)
            for node in instant_nodes:
                try:
                    await self._execute_instant_node(node)
                except Exception as exc:
                    logger.error("Instant node %s failed on resume: %s", node.name, exc, exc_info=True)
                    phase_failed = True

            if phase_failed:
                break

            # Start only NEW LLM tasks
            if new_llm_nodes:
                logger.info(
                    "Resume wave %d: starting %d new nodes (%s)",
                    wave_num, len(new_llm_nodes),
                    ", ".join(n.name for n in new_llm_nodes),
                )
                for node in new_llm_nodes:
                    try:
                        task = await self._create_and_start_llm_node(node)
                        node.task_id = task["id"]
                    except Exception as exc:
                        logger.error("Failed to start LLM node %s: %s", node.name, exc, exc_info=True)
                        phase_failed = True

                if phase_failed:
                    break

                # Wait for new tasks
                task_ids = [n.task_id for n in new_llm_nodes if n.task_id]
                if task_ids:
                    completed_ok = await self._wait_for_wave(task_ids)
                    for node in new_llm_nodes:
                        if node.task_id:
                            result = await self._get_task_result(node.task_id)
                            if result:
                                self._results[node.drawflow_id] = result
                    if not completed_ok:
                        phase_failed = True

            # Post-completion routing for LLM router nodes (both resumed and new)
            for node in llm_nodes:
                if node.node_type == "llm_router" and node.drawflow_id in self._results:
                    self._execute_llm_router_routing(node)

            # Handle retry logic for template-mode agent nodes (new only)
            for node in new_llm_nodes:
                if node.data.get("mode") == "template":
                    try:
                        await self._handle_retry(node)
                    except Exception as exc:
                        logger.error("Retry failed for %s: %s", node.name, exc)

            await event_bus.publish(EVENT_WAVE_COMPLETED, {
                "phase_id": self._phase_id,
                "wave": wave_num,
            })

        return phase_failed

    async def _run_waves(
        self, waves: list[list[GraphNode]], project_id: str,
    ) -> bool:
        """Execute waves sequentially. Returns True if any wave failed."""
        event_bus = self._orch.event_bus
        phase_failed = False

        await event_bus.publish(EVENT_PHASE_STARTED, {
            "phase_id": self._phase_id,
            "project_id": project_id,
            "project_name": self._project_name,
            "task_count": sum(len(w) for w in waves),
            # Triggers __reload__ on the project detail page so newly created
            # phases appear without a manual refresh.
            "status": "plan_applied",
        })

        for wave_idx, wave_nodes in enumerate(waves, 1):
            if self._cancelled:
                logger.info("Graph executor cancelled — aborting wave loop")
                phase_failed = True
                break

            wave_num = wave_nodes[0].wave if wave_nodes else wave_idx
            logger.info(
                "Wave %d: %d nodes (%s)",
                wave_num, len(wave_nodes),
                ", ".join(f"{n.name}({n.node_type})" for n in wave_nodes),
            )

            await event_bus.publish(EVENT_WAVE_STARTED, {
                "phase_id": self._phase_id,
                "wave": wave_num,
                "task_ids": [n.task_id for n in wave_nodes if n.task_id],
            })

            # Filter out skipped nodes (conditional branches)
            active_nodes = [n for n in wave_nodes if not n.skipped]
            skipped = [n for n in wave_nodes if n.skipped]
            if skipped:
                logger.info(
                    "Wave %d: skipping %d nodes (conditional): %s",
                    wave_num, len(skipped),
                    ", ".join(n.name for n in skipped),
                )

            # Split into instant vs LLM nodes
            instant_nodes = [n for n in active_nodes if n.node_type not in _LLM_NODE_TYPES]
            llm_nodes = [n for n in active_nodes if n.node_type in _LLM_NODE_TYPES]

            # Execute instant nodes first (synchronous)
            for node in instant_nodes:
                try:
                    await self._execute_instant_node(node)
                except Exception as exc:
                    logger.error("Instant node %s failed: %s", node.name, exc)
                    phase_failed = True

            if phase_failed:
                break

            # Create and start LLM tasks in parallel
            for node in llm_nodes:
                try:
                    task = await self._create_and_start_llm_node(node)
                    node.task_id = task["id"]
                except Exception as exc:
                    logger.error("Failed to start LLM node %s: %s", node.name, exc, exc_info=True)
                    phase_failed = True

            if phase_failed:
                break

            # Wait for LLM tasks
            if llm_nodes:
                task_ids = [n.task_id for n in llm_nodes if n.task_id]
                if task_ids:
                    completed_ok = await self._wait_for_wave(task_ids)
                    for node in llm_nodes:
                        if node.task_id:
                            result = await self._get_task_result(node.task_id)
                            if result:
                                self._results[node.drawflow_id] = result
                    # Post-completion routing for LLM router nodes
                    for node in llm_nodes:
                        if node.node_type == "llm_router" and node.drawflow_id in self._results:
                            self._execute_llm_router_routing(node)
                    # Handle retry logic for template-mode agent nodes
                    for node in llm_nodes:
                        if node.data.get("mode") == "template":
                            try:
                                await self._handle_retry(node)
                            except Exception as exc:
                                logger.error("Retry failed for %s: %s", node.name, exc)
                    if not completed_ok:
                        phase_failed = True

            await event_bus.publish(EVENT_WAVE_COMPLETED, {
                "phase_id": self._phase_id,
                "wave": wave_num,
            })

            if phase_failed:
                break

        return phase_failed

    def _get_written_file_path(self, node: GraphNode) -> str | None:
        """Return the .taktis/-relative path for a file_writer node's output.

        Substitutes a small set of time placeholders so cron-friendly
        templates can produce dated filenames without help from the
        agent prompt:

        - ``{{date}}``      → ``YYYY-MM-DD`` (UTC)
        - ``{{datetime}}``  → ``YYYY-MM-DDTHH-MM`` (UTC, ``:`` swapped for ``-``)
        - ``{{week_num}}``  → ISO week number, zero-padded
        - ``{{year}}``      → ``YYYY``
        """
        filename = node.data.get("filename", "").strip()
        if filename:
            return _substitute_filename_placeholders(filename)
        # Legacy support for old file_target field
        file_target = node.data.get("file_target", "")
        if file_target == "requirements":
            return "REQUIREMENTS.md"
        elif file_target == "roadmap":
            return "ROADMAP.md"
        elif file_target == "verification":
            return "VERIFICATION.md"
        return None

    # ------------------------------------------------------------------
    # Variable resolution
    # ------------------------------------------------------------------

    def _resolve_variable(self, ref: str) -> str:
        """Resolve a variable reference to an upstream result.

        Supports:
        - ``"Interview"`` → full result from node named "Interview"
        - ``"Output Parser.requirements"`` → section from output_parser JSON
        """
        if "." in ref:
            node_name, section = ref.split(".", 1)
        else:
            node_name, section = ref, None

        # Find node by name
        for nid, node in self._node_map.items():
            if node.name == node_name and nid in self._results:
                raw = self._results[nid]
                if section:
                    try:
                        parsed = json.loads(raw)
                        return str(parsed.get(section, ""))
                    except (json.JSONDecodeError, TypeError):
                        return ""
                return raw
        return ""

    def _provision_reference_files(self) -> None:
        """Copy shared reference files (e.g. PIPELINE_SCHEMA.md) into the
        project's ``.taktis/`` so pipeline agents can read them."""
        from pathlib import Path

        working_dir = self._project.get("working_dir", "") if self._project else ""
        if not working_dir:
            return

        orch_dir = Path(working_dir) / ".taktis"
        orch_dir.mkdir(parents=True, exist_ok=True)

        # Copy PIPELINE_SCHEMA.md from taktis defaults
        schema_src = Path(__file__).resolve().parent.parent / "defaults" / "PIPELINE_SCHEMA.md"
        schema_dst = orch_dir / "PIPELINE_SCHEMA.md"
        if schema_src.exists() and not schema_dst.exists():
            schema_dst.write_text(schema_src.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("Provisioned PIPELINE_SCHEMA.md into %s", orch_dir)

    def _get_upstream_text(self, node: GraphNode) -> str:
        """Get concatenated upstream results for a node."""
        parts = []
        for up_id in node.upstream:
            if up_id in self._results:
                parts.append(self._results[up_id])
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # LLM node execution (unified agent node)
    # ------------------------------------------------------------------

    async def _create_and_start_llm_node(self, node: GraphNode) -> dict:
        """Build prompt, create DB task, and start it.

        The unified ``agent`` node uses a ``mode`` field:
        - ``template``: preset prompt with {variable} mapping and retry
        - ``standard``: raw prompt with expert and optional upstream context

        The ``llm_router`` node uses a lightweight LLM for classification.
        """
        # LLM Router: lightweight classification task
        if node.node_type == "llm_router":
            routing_prompt = node.data.get("routing_prompt", "")
            upstream_ctx = self._build_upstream_context(node)
            prompt = f"{upstream_ctx}\n\n{routing_prompt}" if upstream_ctx else routing_prompt
            model = node.data.get("model", "haiku")
            name = node.name or "LLM Router"

            # Create task first to get real task ID
            task = await self._orch.create_task(
                self._project_name,
                prompt=prompt,
                phase_number=self._phase_number,
                name=name,
                wave=node.wave,
                model=model,
            )
            # Build context (exclude upstream — already in prompt for routing)
            pipeline_context, context_manifest = self._build_pipeline_context(
                node, mode="llm_router",
            )
            # Write context file with REAL task ID, then update system_prompt
            from taktis.core.context import write_task_context_file
            working_dir = self._project.get("working_dir", "") if self._project else ""
            ctx_note = write_task_context_file(working_dir, task["id"], pipeline_context)
            session_factory = self._orch._execution_service._session_factory
            async with session_factory() as conn:
                updates = {}
                if ctx_note:
                    updates["system_prompt"] = ctx_note
                if context_manifest:
                    updates["context_manifest"] = json.dumps(context_manifest)
                if updates:
                    await repo.update_task(conn, task["id"], **updates)
            await self._orch.start_task(task["id"])
            return task

        mode = node.data.get("mode", "standard")

        if mode == "template":
            prompt = await self._build_template_prompt(node)
            expert_name = node.data.get("expert")
            expert_id = node.data.get("expert_id")
            model = node.data.get("model")
            interactive = bool(node.data.get("interactive", False))
            name = node.name or "Agent"
        else:
            # standard — raw prompt (upstream context now via ContextBudget)
            prompt = node.data.get("prompt", "")
            expert_name = node.data.get("expert")
            expert_id = node.data.get("expert_id")
            model = node.data.get("model")
            interactive = bool(node.data.get("interactive", False))
            name = node.name or "Agent"

        # Resolve expert_id from name if not set directly
        expert_row = None
        if not expert_id and expert_name:
            session_factory = self._orch._execution_service._session_factory
            async with session_factory() as conn:
                expert_row = await repo.get_expert_by_name(conn, expert_name)
                if expert_row:
                    expert_id = expert_row["id"]

        # Derive task_type from expert's task_type field
        if expert_id:
            if expert_row is None or expert_row.get("id") != expert_id:
                session_factory = self._orch._execution_service._session_factory
                async with session_factory() as conn:
                    expert_row = await repo.get_expert_by_id(conn, expert_id)
            task_type = expert_row.get("task_type") if expert_row else None
        else:
            task_type = None

        # Build retry policy from node config
        retry_policy = json.dumps({
            "retry_transient": bool(node.data.get("retry_transient", True)),
            "max_attempts": int(node.data.get("retry_max_attempts", "2")),
            "backoff": node.data.get("retry_backoff", "none"),
            "retry_on": ["StreamingError", "RateLimitError", "OverloadedError"],
        })

        # Create task first to get real task ID
        task = await self._orch.create_task(
            self._project_name,
            prompt=prompt,
            phase_number=self._phase_number,
            name=name,
            expert=expert_name,
            expert_id=expert_id,
            wave=node.wave,
            interactive=interactive,
            model=model,
            task_type=task_type,
            retry_policy=retry_policy,
        )

        # Build pipeline context and write TASK_CONTEXT with real task ID
        pipeline_context, context_manifest = self._build_pipeline_context(node, mode=mode)
        from taktis.core.context import write_task_context_file
        working_dir = self._project.get("working_dir", "") if self._project else ""
        ctx_note = write_task_context_file(working_dir, task["id"], pipeline_context)

        # Build system prompt: expert persona + context file pointer
        task_system_prompt = ""
        if expert_id and expert_row and expert_row.get("system_prompt"):
            task_system_prompt = expert_row["system_prompt"]
        if ctx_note:
            task_system_prompt += ctx_note
        if working_dir:
            task_system_prompt += (
                f"\n\nYour working directory is: {working_dir}\n"
                "CRITICAL: Always use the Write tool to create files — NEVER use "
                "Bash heredocs, echo, cat, or python scripts to write files. "
                "Bash file writes silently fail on network paths. The Write tool "
                "is reliable on all path types."
            )

        # Update task with system_prompt and manifest
        session_factory = self._orch._execution_service._session_factory
        async with session_factory() as conn:
            updates = {}
            if task_system_prompt:
                updates["system_prompt"] = task_system_prompt
            if context_manifest:
                updates["context_manifest"] = json.dumps(context_manifest)
            if updates:
                await repo.update_task(conn, task["id"], **updates)

        await self._orch.start_task(task["id"])
        return task

    async def _build_template_prompt(self, node: GraphNode) -> str:
        """Build a prompt from inline text or a named preset, with variable injection."""
        # Inline prompt takes priority over preset lookup
        inline_prompt = (node.data.get("prompt") or "").strip()
        if inline_prompt:
            template_str = inline_prompt
        else:
            template_name = node.data.get("template", "")
            if not template_name:
                raise PipelineError(
                    f"Node '{node.name}' has no prompt text and no preset selected",
                    step="template_lookup",
                )
            session_factory = self._orch._execution_service._session_factory
            tmpl = await _get_template_from_db(session_factory, template_name)
            template_str = tmpl["prompt_text"]

        # Build variable dict from variable_map
        variable_map_raw = node.data.get("variable_map", "{}")
        try:
            variable_map = json.loads(variable_map_raw) if isinstance(variable_map_raw, str) else variable_map_raw
        except json.JSONDecodeError:
            variable_map = {}

        variables: dict[str, str] = {}
        for var_name, ref in variable_map.items():
            variables[var_name] = self._resolve_variable(ref)

        # Inject project description
        if node.data.get("inject_description", True) and self._project:
            variables.setdefault("description", self._project.get("description", ""))

        # Inject expert options
        if node.data.get("inject_expert_options", False):
            from taktis.core.experts import format_expert_options
            session_factory = self._orch._execution_service._session_factory
            variables.setdefault(
                "expert_options",
                await format_expert_options(session_factory),
            )

        # synthesizer wrapper for roadmapper (wraps in XML tags)
        if "synthesizer" in variables and variables["synthesizer"]:
            variables["synthesizer"] = (
                f"<research_summary>\n{variables['synthesizer']}\n</research_summary>"
            )
        elif "synthesizer" not in variables:
            variables.setdefault("synthesizer", "")

        # Format plan as markdown (plan checker expects structured text)
        if "plan" in variables and variables["plan"]:
            variables["plan"] = self._format_phases_summary(
                variables["plan"]
            )

        # Fill missing template vars with empty strings to avoid KeyError
        import re
        template_vars = set(re.findall(r"\{(\w+)\}", template_str))
        for v in template_vars:
            variables.setdefault(v, "")

        return template_str.format(**variables)

    def _get_project_budget(self) -> int:
        """Read context_budget_chars from planning_options, default 150K."""
        return int(self._get_planning_options().get("context_budget_chars", 150_000))

    def _get_api_call_max_response_kb(self) -> int:
        """Read per-project api_call_max_response_kb override, default 50KB."""
        return int(self._get_planning_options().get("api_call_max_response_kb", 50))

    def _get_planning_options(self) -> dict:
        """Decode the project's planning_options JSON, with empty-dict fallback."""
        if not self._project:
            return {}
        opts = self._project.get("planning_options") or ""
        if isinstance(opts, str):
            try:
                opts = json.loads(opts) if opts else {}
            except (json.JSONDecodeError, TypeError):
                opts = {}
        return opts if isinstance(opts, dict) else {}

    def _build_pipeline_context(self, node: GraphNode, mode: str = "standard") -> tuple[str, list[dict]]:
        """Assemble pipeline context via ContextBudget for an LLM node."""
        from taktis.core.context import (
            ContextBudget, _safe_read, _ctx_dir, write_task_context_file,
        )

        budget = ContextBudget(self._get_project_budget())
        working_dir = self._project.get("working_dir", "") if self._project else ""
        ctx = _ctx_dir(working_dir)

        # P0: PROJECT.md
        proj_text = _safe_read(ctx / "PROJECT.md", "project")
        if proj_text:
            budget.add(ContextBudget.P0_MUST, "project_context", proj_text,
                       source_path=".taktis/PROJECT.md")

        # P1: Upstream results — for standard mode (llm_router has upstream in prompt already)
        if mode == "standard":
            for up_id in node.upstream:
                if up_id in self._results:
                    up_node = self._node_map.get(up_id)
                    label = up_node.name if up_node else f"Node {up_id}"
                    budget.add(ContextBudget.P1_HIGH, f"upstream_{up_id}",
                               self._results[up_id], source_path=f"pipeline:{label}")
        elif mode == "template":
            # Only add upstream NOT already mapped via variable_map
            var_map_raw = node.data.get("variable_map", "{}")
            try:
                var_map_obj = json.loads(var_map_raw) if isinstance(var_map_raw, str) else var_map_raw
            except json.JSONDecodeError:
                var_map_obj = {}
            mapped_ids = set()
            for ref in var_map_obj.values():
                if isinstance(ref, str) and ":" in ref:
                    mapped_ids.add(ref.split(":", 1)[1])
            for up_id in node.upstream:
                if up_id not in mapped_ids and up_id in self._results:
                    up_node = self._node_map.get(up_id)
                    label = up_node.name if up_node else f"Node {up_id}"
                    budget.add(ContextBudget.P1_HIGH, f"upstream_{up_id}",
                               self._results[up_id], source_path=f"pipeline:{label}")

        # Cross-phase shared files — use priority from manifest when available
        from taktis.core.context import read_context_manifest
        manifest = read_context_manifest(working_dir)
        _priority_to_level = {
            "P0_MUST": ContextBudget.P0_MUST,
            "P1_HIGH": ContextBudget.P1_HIGH,
            "P2_MEDIUM": ContextBudget.P2_MEDIUM,
            "P3_LOW": ContextBudget.P3_LOW,
            "P4_TRIM": ContextBudget.P4_TRIM,
        }
        for rel_path in self._phase_context_config.get("context_files", []):
            ftext = _safe_read(ctx / rel_path, rel_path)
            if ftext:
                priority_key = manifest.get(rel_path, "P2_MEDIUM")
                level = _priority_to_level.get(priority_key, ContextBudget.P2_MEDIUM)
                budget.add(level, f"shared_{rel_path}",
                           ftext, source_path=f".taktis/{rel_path}")

        # P3: DB state summary
        if self._state_summary:
            budget.add(ContextBudget.P3_LOW, "project_state", self._state_summary)

        return budget.assemble()

    def _build_upstream_context(self, node: GraphNode) -> str:
        """Build XML upstream context block for standard-mode agent nodes."""
        parts = []
        for up_id in node.upstream:
            up_node = self._node_map.get(up_id)
            if up_node and up_id in self._results:
                label = up_node.name or f"Node {up_id}"
                parts.append(f'<from node="{label}">\n{self._results[up_id]}\n</from>')
        if not parts:
            return ""
        return "<upstream_context>\n" + "\n".join(parts) + "\n</upstream_context>"

    @staticmethod
    def _format_phases_summary(plan_text: str) -> str:
        """Convert raw plan JSON into formatted markdown for the plan checker.

        Matches the format the old ``PlanningPipeline._spawn_plan_checker()``
        produced: phase name, goal, success criteria, task list with waves.
        """
        try:
            # plan_text might be raw JSON or a JSON string from output_parser
            plan = json.loads(plan_text) if isinstance(plan_text, str) else plan_text
        except (json.JSONDecodeError, TypeError):
            return plan_text  # Can't parse — pass through as-is

        # If it's a plan dict with "phases" key, format it
        if not isinstance(plan, dict) or "phases" not in plan:
            # Try extracting from ```json fences
            from taktis.core.planner import parse_plan_output
            plan = parse_plan_output(plan_text)
            if plan is None:
                return plan_text

        lines = []
        for phase in plan.get("phases", []):
            name = phase.get("name", "Unnamed Phase")
            goal = phase.get("goal", "")
            criteria = phase.get("success_criteria", [])
            tasks = phase.get("tasks", [])

            lines.append(f"### {name}")
            if goal:
                lines.append(f"Goal: {goal}")
            if criteria:
                lines.append("Success criteria:")
                for c in criteria:
                    lines.append(f"  - {c}")
            lines.append(f"Tasks: {len(tasks)}")
            for t in tasks:
                wave = t.get("wave", 1)
                expert = t.get("expert", "implementer-general")
                prompt = t.get("prompt", "")[:200]
                lines.append(f"  - Wave {wave}, Expert: {expert}: {prompt}")
            lines.append("")
        return "\n".join(lines)

    def _find_upstream_llm_node(self, node: GraphNode) -> GraphNode | None:
        """BFS backwards through the graph to find the nearest LLM ancestor."""
        visited: set[str] = set()
        queue = list(node.upstream)
        while queue:
            nid = queue.pop(0)
            if nid in visited:
                continue
            visited.add(nid)
            up = self._node_map.get(nid)
            if up is None:
                continue
            if up.node_type in _LLM_NODE_TYPES:
                return up
            queue.extend(up.upstream)
        return None

    async def _rerun_downstream_instant_nodes(
        self, from_node: GraphNode, stop_node: GraphNode,
    ) -> None:
        """Re-execute instant nodes downstream of *from_node*, up to *stop_node*.

        Used by the retry loop: after re-running an upstream LLM node, any
        instant nodes between it and the checker (e.g. output_parser,
        file_writer) must re-execute so the checker sees fresh data.
        """
        visited: set[str] = set()
        queue = list(from_node.downstream)
        while queue:
            nid = queue.pop(0)
            if nid in visited or nid == stop_node.drawflow_id:
                continue
            visited.add(nid)
            node = self._node_map.get(nid)
            if node is None:
                continue
            if node.node_type not in _LLM_NODE_TYPES and node.node_type != "human_gate":
                await self._execute_instant_node(node)
                queue.extend(node.downstream)

    # ------------------------------------------------------------------
    # Instant node execution (output_parser, file_writer, plan_applier)
    # ------------------------------------------------------------------

    async def _execute_instant_node(self, node: GraphNode) -> None:
        """Dispatch to the appropriate instant handler."""
        if node.node_type == "output_parser":
            self._execute_output_parser(node)
        elif node.node_type == "file_writer":
            await self._execute_file_writer(node)
        elif node.node_type == "plan_applier":
            await self._execute_plan_applier(node)
        elif node.node_type == "conditional":
            self._execute_conditional(node)
        elif node.node_type == "aggregator":
            self._execute_aggregator(node)
        elif node.node_type == "human_gate":
            await self._execute_human_gate(node)
        elif node.node_type == "api_call":
            await self._execute_api_call(node)
        elif node.node_type == "text_transform":
            self._execute_text_transform(node)
        elif node.node_type == "fan_out":
            await self._execute_fan_out(node)
        elif node.node_type == "loop":
            await self._execute_loop(node)
        elif node.node_type == "pipeline_generator":
            await self._execute_pipeline_generator(node)
        else:
            logger.warning("Unknown instant node type: %s", node.node_type)

    def _execute_output_parser(self, node: GraphNode) -> None:
        """Split upstream text by markers into named sections stored as JSON."""
        upstream = self._get_upstream_text(node)
        markers_raw = node.data.get("markers", "")
        names_raw = node.data.get("section_names", "")
        markers = [m.strip() for m in markers_raw.split("\n") if m.strip()]
        names = [n.strip() for n in names_raw.split("\n") if n.strip()]

        sections: dict[str, str] = {}
        remaining = upstream

        for i, marker in enumerate(markers):
            name = names[i] if i < len(names) else f"section_{i}"
            if marker in remaining:
                parts = remaining.split(marker, 1)
                # Content before this marker belongs to previous section or is discarded
                if i > 0:
                    prev_name = names[i - 1] if (i - 1) < len(names) else f"section_{i - 1}"
                    sections[prev_name] = parts[0].strip()
                remaining = parts[1]
            else:
                continue

        # Last section is everything remaining after the last found marker
        if markers and names:
            last_name = names[-1] if names else "last"
            # Check if we've already set it
            if last_name not in sections:
                sections[last_name] = remaining.strip()
            # Also set any names that weren't found
            for nm in names:
                sections.setdefault(nm, "")

        self._results[node.drawflow_id] = json.dumps(sections)
        logger.info("Output parser '%s': extracted %d sections", node.name, len(sections))

    async def _execute_file_writer(self, node: GraphNode) -> None:
        """Write upstream result to a file under .taktis/."""
        from pathlib import Path
        from taktis.core.context import update_context_manifest

        upstream = self._get_upstream_text(node)
        logger.info(
            "File writer '%s': upstream_ids=%s, has_content=%s, results_keys=%s",
            node.name, node.upstream, bool(upstream), list(self._results.keys()),
        )
        source_section = node.data.get("source_section", "").strip()

        # Extract section — search all results (source may be an ancestor, not direct upstream)
        content = upstream
        if source_section:
            for result_id, result_val in self._results.items():
                try:
                    parsed = json.loads(result_val)
                    if isinstance(parsed, dict) and source_section in parsed:
                        content = parsed[source_section]
                        break
                except (json.JSONDecodeError, TypeError):
                    continue

        working_dir = self._project.get("working_dir", "") if self._project else ""
        filename = self._get_written_file_path(node)

        if not filename:
            logger.warning("file_writer '%s': no filename configured", node.name)
            self._results[node.drawflow_id] = content
            return

        # Write the file
        path = Path(working_dir) / ".taktis" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        # Register in context manifest if priority is set
        context_priority = node.data.get("context_priority", "none")
        if context_priority and context_priority != "none":
            update_context_manifest(working_dir, filename, context_priority)

        # Passthrough — downstream can access the content
        self._results[node.drawflow_id] = content
        logger.info("File writer '%s': wrote %s (context: %s)", node.name, filename, context_priority)

    async def _execute_plan_applier(self, node: GraphNode) -> None:
        """Parse JSON plan and create phases/tasks in DB.

        If ``await_approval`` is set, publishes a plan-ready event and
        waits for :meth:`approve_plan` to be called before applying.
        """
        from taktis.core.planner import apply_plan, parse_plan_output

        upstream = self._get_upstream_text(node)
        source_section = node.data.get("source_section", "plan").strip()

        # Extract plan section — search all results (not just direct upstreams)
        # because the source may be an ancestor node (e.g. output_parser → plan_checker → plan_applier)
        plan_text = upstream
        if source_section:
            for result_id, result_val in self._results.items():
                try:
                    parsed = json.loads(result_val)
                    if isinstance(parsed, dict) and source_section in parsed:
                        plan_text = parsed[source_section]
                        break
                except (json.JSONDecodeError, TypeError):
                    continue

        plan = parse_plan_output(plan_text)
        if plan is None:
            raise PipelineError("Could not parse JSON plan from upstream output", step="plan_applier")

        # Approval gate (skipped on resume — user explicitly chose to resume)
        if node.data.get("await_approval", True) and not getattr(self, "_force_apply", False):
            self._pending_plan = plan
            self._approval_event = asyncio.Event()
            project_id = self._project["id"] if self._project else ""

            await self._orch.event_bus.publish(EVENT_PIPELINE_PLAN_READY, {
                "project_id": project_id,
                "project_name": self._project_name,
                "plan": plan,
            })
            logger.info("Plan ready for review — awaiting approval for '%s'", self._project_name)

            # Wait until approve_plan() is called
            await self._approval_event.wait()
            logger.info("Plan approved for '%s' — applying", self._project_name)

        # Init context files
        from taktis.core import context as ctx
        working_dir = self._project.get("working_dir", "") if self._project else ""
        ctx.init_context(working_dir, self._project_name, plan.get("project_summary", ""))

        # Apply plan
        result = await apply_plan(self._orch, self._project_name, plan)
        summary = (
            f"Plan applied: {result['phases_created']} phases, "
            f"{result['tasks_created']} tasks created."
        )
        self._results[node.drawflow_id] = summary
        logger.info("Plan applier '%s': %s", node.name, summary)

    def _execute_conditional(self, node: GraphNode) -> None:
        """Evaluate condition on upstream text and skip the inactive branch.

        Output ports: ``output_1`` = condition **passed**, ``output_2`` = condition **failed**.
        Downstream nodes on the skipped port get ``node.skipped = True`` recursively.
        """
        import re as _re

        upstream_text = self._get_upstream_text(node) or ""
        cond_type = node.data.get("condition_type", "contains")
        cond_value = node.data.get("condition_value", "")
        case_sensitive = bool(node.data.get("case_sensitive", False))

        # Evaluate condition
        if cond_type == "contains":
            haystack = upstream_text if case_sensitive else upstream_text.lower()
            needle = cond_value if case_sensitive else cond_value.lower()
            passed = needle in haystack
        elif cond_type == "not_contains":
            haystack = upstream_text if case_sensitive else upstream_text.lower()
            needle = cond_value if case_sensitive else cond_value.lower()
            passed = needle not in haystack
        elif cond_type == "regex_match":
            flags = 0 if case_sensitive else _re.IGNORECASE
            passed = bool(_re.search(cond_value, upstream_text, flags))
        elif cond_type == "result_is":
            trimmed = upstream_text.strip().lower()
            passed = trimmed == cond_value.strip().lower()
        elif cond_type == "task_failed":
            # Check if any upstream task has failed status
            passed = False
            for up_id in node.upstream:
                up_node = self._node_map.get(up_id)
                if up_node and up_node.task_id:
                    result = self._results.get(up_node.drawflow_id, "")
                    if result.startswith("FAILED"):
                        passed = True
                        break
        else:
            logger.warning("Unknown condition type '%s', defaulting to pass", cond_type)
            passed = True

        logger.info(
            "Conditional '%s': %s(%r) → %s",
            node.name, cond_type, cond_value, "PASS" if passed else "FAIL",
        )

        # Determine which port to skip
        # output_1 = pass branch, output_2 = fail branch
        skip_port = "output_2" if passed else "output_1"
        active_port = "output_1" if passed else "output_2"

        # Mark downstream nodes on the skipped port
        skip_targets = set(node.downstream_ports.get(skip_port, []))
        active_targets = set(node.downstream_ports.get(active_port, []))

        # Recursively mark skipped nodes (and their entire subgraph)
        self._mark_skipped(skip_targets - active_targets)

        # Pass upstream text through to active branch
        self._results[node.drawflow_id] = upstream_text

    def _mark_skipped(self, node_ids: set[str]) -> None:
        """Recursively mark nodes and their descendants as skipped.

        The initial ``node_ids`` (direct targets of the skipped port) are
        always skipped.  Their descendants are only skipped if ALL upstream
        parents are also skipped — nodes reachable from the active branch
        are preserved.
        """
        if not node_ids:
            return

        # Force-skip the root nodes (direct conditional port targets)
        roots = set(node_ids)
        for nid in roots:
            node = self._node_map.get(nid)
            if node:
                node.skipped = True
                logger.info("Skipping node '%s' id=%s (conditional branch root)", node.name, nid)

        # Collect all descendants of the roots
        candidates: set[str] = set()
        queue: list[str] = []
        for nid in roots:
            node = self._node_map.get(nid)
            if node:
                queue.extend(node.downstream)
        while queue:
            nid = queue.pop(0)
            if nid in candidates or nid in roots:
                continue
            candidates.add(nid)
            node = self._node_map.get(nid)
            if node:
                queue.extend(node.downstream)

        # Iteratively remove candidates that have an active (non-skipped)
        # upstream feeder.  "skipped" here means in roots or still in
        # candidates after pruning.
        changed = True
        while changed:
            changed = False
            keep: set[str] = set()
            for nid in candidates:
                node = self._node_map.get(nid)
                if node is None:
                    continue
                for up_id in node.upstream:
                    up_node = self._node_map.get(up_id)
                    if (up_id not in candidates and up_id not in roots
                            and up_node and not up_node.skipped):
                        keep.add(nid)
                        break
            if keep:
                candidates -= keep
                changed = True

        # Apply skipped flag to remaining descendants
        for nid in candidates:
            node = self._node_map.get(nid)
            if node is None:
                continue
            node.skipped = True
            logger.info("Skipping node '%s' id=%s (conditional branch)", node.name, nid)

    def approve_plan(self) -> bool:
        """Resume plan application after user approval. Called from API."""
        if hasattr(self, '_approval_event') and self._approval_event is not None:
            self._approval_event.set()
            return True
        return False

    def approve_gate(self, node_id: str) -> bool:
        """Approve a pending human gate. Called from API."""
        gate = self._pending_gates.get(node_id)
        if gate is None:
            return False
        gate["approved"] = True
        gate["event"].set()
        return True

    def reject_gate(self, node_id: str) -> bool:
        """Reject a pending human gate. Called from API."""
        gate = self._pending_gates.get(node_id)
        if gate is None:
            return False
        gate["approved"] = False
        gate["event"].set()
        return True

    def cancel(self) -> None:
        """Cancel execution — unblocks all pending gates."""
        self._cancelled = True
        for gate in self._pending_gates.values():
            gate["event"].set()
        if hasattr(self, '_approval_event') and self._approval_event is not None:
            self._approval_event.set()

    # ------------------------------------------------------------------
    # Aggregator node
    # ------------------------------------------------------------------

    def _execute_aggregator(self, node: GraphNode) -> None:
        """Combine upstream results using the configured strategy."""
        strategy = node.data.get("strategy", "concat")

        # Gather upstream results as ordered (name, text) pairs
        items: list[tuple[str, str]] = []
        for up_id in node.upstream:
            up_node = self._node_map.get(up_id)
            name = up_node.name if up_node else f"Node {up_id}"
            text = self._results.get(up_id, "")
            if text:
                items.append((name, text))

        if not items:
            logger.warning("Aggregator '%s': no upstream results to combine", node.name)
            self._results[node.drawflow_id] = ""
            return

        if strategy == "json_merge":
            merged: dict[str, Any] = {}
            all_dicts = True
            for name, text in items:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        merged.update(parsed)
                    else:
                        all_dicts = False
                        break
                except (json.JSONDecodeError, TypeError):
                    all_dicts = False
                    break
            if all_dicts:
                result = json.dumps(merged)
            else:
                # Fallback: collect as array of raw-wrapped items
                arr = []
                for name, text in items:
                    try:
                        arr.append(json.loads(text))
                    except (json.JSONDecodeError, TypeError):
                        arr.append({"raw": text})
                result = json.dumps(arr)
        elif strategy == "numbered_list":
            parts = []
            for i, (name, text) in enumerate(items, 1):
                parts.append(f"{i}. [{name}]\n{text}")
            result = "\n\n".join(parts)
        elif strategy == "xml_wrap":
            parts = []
            for name, text in items:
                parts.append(f'<from node="{name}">\n{text}\n</from>')
            result = "\n".join(parts)
        else:
            # concat (default) or unknown strategy
            if strategy != "concat":
                logger.warning(
                    "Aggregator '%s': unknown strategy '%s', falling back to concat",
                    node.name, strategy,
                )
            separator = node.data.get("separator", "\n\n---\n\n")
            result = separator.join(text for _, text in items)

        self._results[node.drawflow_id] = result
        logger.info(
            "Aggregator '%s': combined %d inputs with strategy '%s'",
            node.name, len(items), strategy,
        )

    # ------------------------------------------------------------------
    # Pipeline Generator node
    # ------------------------------------------------------------------

    async def _execute_pipeline_generator(self, node: GraphNode) -> None:
        """Convert a structured pipeline spec into a saved Drawflow template.

        Reads upstream JSON (directly or from an output_parser section),
        validates it, generates Drawflow JSON via pipeline_factory, and
        saves it as a new pipeline template in the database.
        """
        from taktis.core.pipeline_factory import spec_to_drawflow, validate_spec
        from taktis import repository as repo

        upstream = self._get_upstream_text(node)
        source_section = node.data.get("source_section", "pipeline_spec").strip()
        prefix = node.data.get("template_name_prefix", "Generated").strip()

        # Extract spec from output_parser section if specified
        spec_text = upstream
        if source_section:
            for _rid, result_val in self._results.items():
                try:
                    parsed = json.loads(result_val)
                    if isinstance(parsed, dict) and source_section in parsed:
                        spec_text = parsed[source_section]
                        break
                except (json.JSONDecodeError, TypeError):
                    continue

        # Parse spec JSON
        try:
            if isinstance(spec_text, str):
                spec = json.loads(spec_text)
            else:
                spec = spec_text
        except (json.JSONDecodeError, TypeError) as exc:
            raise PipelineError(
                f"Pipeline generator '{node.name}': could not parse spec JSON: {exc}",
                step="pipeline_generator",
            )

        # Validate (non-fatal warnings are logged, fatal errors raise)
        errors = validate_spec(spec)
        if errors:
            error_list = "\n".join(f"  - {e}" for e in errors)
            raise PipelineError(
                f"Pipeline generator '{node.name}': spec validation failed:\n{error_list}",
                step="pipeline_generator",
            )

        # Generate Drawflow JSON
        try:
            template = spec_to_drawflow(spec)
        except ValueError as exc:
            raise PipelineError(
                f"Pipeline generator '{node.name}': failed to generate Drawflow JSON: {exc}",
                step="pipeline_generator",
            )

        # Apply name prefix
        spec_name = template.get("name", "Untitled Pipeline")
        template["name"] = f"{prefix}: {spec_name}" if prefix else spec_name

        # Save to database
        from taktis.exceptions import DuplicateError
        session_factory = self._orch._execution_service._session_factory
        try:
            async with session_factory() as conn:
                saved = await repo.create_pipeline_template(conn, {
                    "name": template["name"],
                    "description": template.get("description", ""),
                    "flow_json": json.dumps(template["flow_json"]),
                    "is_default": False,
                })
        except DuplicateError:
            # Append timestamp to avoid name collision
            import time
            template["name"] = f"{template['name']} ({time.strftime('%H:%M')})"
            async with session_factory() as conn:
                saved = await repo.create_pipeline_template(conn, {
                    "name": template["name"],
                    "description": template.get("description", ""),
                    "flow_json": json.dumps(template["flow_json"]),
                    "is_default": False,
                })

        template_id = saved["id"]
        summary = (
            f"Pipeline template '{template['name']}' created (id: {template_id}). "
            f"{len(spec.get('nodes', []))} nodes."
        )
        self._results[node.drawflow_id] = summary
        logger.info("Pipeline generator '%s': %s", node.name, summary)

    # ------------------------------------------------------------------
    # Text Transform node
    # ------------------------------------------------------------------

    def _execute_text_transform(self, node: GraphNode) -> None:
        """Transform upstream text using simple string operations."""
        import re as _re
        upstream = self._get_upstream_text(node)
        operation = node.data.get("operation", "prepend")
        text = node.data.get("text", "")

        if operation == "prepend":
            result = text + upstream
        elif operation == "append":
            result = upstream + text
        elif operation == "replace":
            find = node.data.get("find_pattern", "")
            if not find:
                result = upstream
            elif node.data.get("use_regex"):
                result = _re.sub(find, text, upstream)
            else:
                result = upstream.replace(find, text)
        elif operation == "extract_json":
            # Extract first JSON from fenced code block
            match = _re.search(r"```(?:json)?\s*\n(.*?)\n```", upstream, _re.DOTALL)
            result = match.group(1).strip() if match else upstream
        elif operation == "wrap_xml":
            tag = text.strip() or "content"
            result = f"<{tag}>\n{upstream}\n</{tag}>"
        elif operation == "template":
            result = text.replace("{upstream}", upstream)
        else:
            result = upstream

        self._results[node.drawflow_id] = result
        logger.info("Text transform '%s': %s → %d chars", node.name, operation, len(result))

    # ------------------------------------------------------------------
    # Fan Out node — split upstream into items, run parallel agent tasks
    # ------------------------------------------------------------------

    async def _execute_fan_out(self, node: GraphNode) -> None:
        """Split upstream into items and run parallel agent tasks.

        Creates one task per item using the node's prompt template and expert
        configuration.  Tasks run in parallel (bounded by ``max_parallel``).
        Results are collected and merged in original item order.
        """
        import re as _re

        upstream = self._get_upstream_text(node)
        split_mode = node.data.get("split_mode", "newline")
        delimiter = node.data.get("delimiter", "---")

        # ----- Split into items -----
        if split_mode == "json_array":
            try:
                parsed = json.loads(upstream)
                if not isinstance(parsed, list):
                    items = [str(parsed)]
                else:
                    items = [
                        json.dumps(x) if not isinstance(x, str) else x
                        for x in parsed
                    ]
            except json.JSONDecodeError:
                items = [upstream]
        elif split_mode == "numbered_list":
            # Match "1. ...", "2. ...", etc.
            items = _re.split(r'\n(?=\d+\.\s)', upstream.strip())
            items = [item.strip() for item in items if item.strip()]
        elif split_mode == "delimiter":
            items = [item.strip() for item in upstream.split(delimiter) if item.strip()]
        else:  # newline
            items = [item.strip() for item in upstream.split('\n') if item.strip()]

        if not items:
            self._results[node.drawflow_id] = ""
            logger.warning("Fan out '%s': no items to process", node.name)
            return

        total = len(items)
        max_parallel = int(node.data.get("max_parallel", "10"))
        prompt_template = node.data.get(
            "prompt_template", "Process the following item:\n\n{item}",
        )
        expert = node.data.get("expert") or None
        model = node.data.get("model", "sonnet")
        merge_strategy = node.data.get("merge_strategy", "numbered")
        merge_separator = node.data.get("merge_separator", "\n\n---\n\n")

        logger.info(
            "Fan out '%s': processing %d items (max %d parallel)",
            node.name, total, max_parallel,
        )

        # ----- Run items with bounded parallelism -----
        results: dict[int, str] = {}
        semaphore = asyncio.Semaphore(max_parallel)

        async def _run_item(index: int, item: str) -> None:
            if self._cancelled:
                return
            async with semaphore:
                if self._cancelled:
                    return
                prompt = (
                    prompt_template
                    .replace("{item}", item)
                    .replace("{index}", str(index + 1))
                    .replace("{total}", str(total))
                )
                task = await self._orch.create_task(
                    self._project_name,
                    prompt=prompt,
                    phase_number=self._phase_number,
                    name=f"{node.name} [{index + 1}/{total}]",
                    expert=expert,
                    wave=node.wave,
                    model=model,
                )
                task_id = task["id"]
                await self._orch.start_task(task_id)
                await self._wait_for_wave([task_id])

                # Collect result
                result = await self._get_task_result(task_id)
                results[index] = result or ""

        await asyncio.gather(*(_run_item(i, item) for i, item in enumerate(items)))

        # ----- Merge results in original order -----
        ordered = [results.get(i, "") for i in range(total)]

        if merge_strategy == "numbered":
            merged = "\n\n".join(
                f"### Item {i + 1}\n\n{r}" for i, r in enumerate(ordered)
            )
        elif merge_strategy == "json_array":
            merged = json.dumps(ordered, indent=2)
        else:  # concat
            merged = merge_separator.join(ordered)

        self._results[node.drawflow_id] = merged
        logger.info(
            "Fan out '%s': completed %d items, merged %d chars",
            node.name, total, len(merged),
        )

    # ------------------------------------------------------------------
    # Human Gate node
    # ------------------------------------------------------------------

    async def _execute_human_gate(self, node: GraphNode) -> None:
        """Block until user approves or rejects, then route accordingly."""
        upstream_text = self._get_upstream_text(node)
        gate_message = node.data.get(
            "gate_message", "Pipeline paused — review and approve to continue.",
        )
        show_upstream = bool(node.data.get("show_upstream", True))

        gate_info: dict[str, Any] = {
            "event": asyncio.Event(),
            "approved": None,
            "message": gate_message,
            "phase_id": self._phase_id,
            "node_name": node.name,
            "upstream_preview": upstream_text[:500] if show_upstream and upstream_text else "",
        }
        self._pending_gates[node.drawflow_id] = gate_info

        # Publish waiting event for SSE
        project_id = self._project["id"] if self._project else ""
        preview = upstream_text[:500] if show_upstream and upstream_text else ""
        await self._orch.event_bus.publish(EVENT_PIPELINE_GATE_WAITING, {
            "project_id": project_id,
            "project_name": self._project_name,
            "node_id": node.drawflow_id,
            "node_name": node.name,
            "gate_message": gate_message,
            "upstream_preview": preview,
        })
        logger.info("Human gate '%s' waiting for approval", node.name)

        await gate_info["event"].wait()

        # Clean up
        self._pending_gates.pop(node.drawflow_id, None)

        if self._cancelled:
            raise PipelineError("Pipeline cancelled while waiting at gate", step="human_gate")

        approved = gate_info["approved"]
        logger.info("Human gate '%s': %s", node.name, "approved" if approved else "rejected")

        # Route like conditional: output_1 = approved, output_2 = rejected
        if approved:
            skip_port = "output_2"
        else:
            skip_port = "output_1"
        active_port = "output_1" if approved else "output_2"

        skip_targets = set(node.downstream_ports.get(skip_port, []))
        active_targets = set(node.downstream_ports.get(active_port, []))
        self._mark_skipped(skip_targets - active_targets)

        # Pass upstream text through
        self._results[node.drawflow_id] = upstream_text

    # ------------------------------------------------------------------
    # API Call node
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_api_url(url: str) -> None:
        """Reject URLs that could cause SSRF."""
        import ipaddress
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PipelineError(
                f"API Call: unsupported URL scheme '{parsed.scheme}' — only http/https allowed",
                step="api_call",
            )
        hostname = parsed.hostname or ""
        if not hostname:
            raise PipelineError("API Call: URL has no hostname", step="api_call")

        # Check for obvious private hostnames
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            raise PipelineError(
                f"API Call: requests to '{hostname}' are blocked (SSRF protection)",
                step="api_call",
            )

        # Resolve hostname and check IP ranges
        try:
            addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
            for family, _, _, _, sockaddr in addr_infos:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    raise PipelineError(
                        f"API Call: resolved IP {ip} is private (SSRF protection)",
                        step="api_call",
                    )
        except socket.gaierror:
            pass  # DNS resolution may fail — let httpx handle it

    def _project_env_vars(self) -> dict:
        """Return the project's default_env_vars as a dict (parsing JSON if needed)."""
        ev = (self._project or {}).get("default_env_vars") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except (ValueError, TypeError):
                return {}
        return ev if isinstance(ev, dict) else {}

    def _pre_substitute_env_vars(self, nodes) -> None:
        """Soft-substitute ``${VAR}`` in the registered fields of every node.

        Runs once after the project has been loaded.  Unresolved tokens stay
        literal so a missing variable shows up as ``${MISSING}`` in the
        rendered prompt/filename rather than silently dropping. ``api_call``
        is excluded — it has its own strict resolver that fails fast.
        """
        from taktis.core.env_vars import SUBSTITUTABLE_FIELDS
        for node in nodes:
            if node.node_type == "api_call":
                continue
            fields = SUBSTITUTABLE_FIELDS.get(node.node_type, ())
            if not fields:
                continue
            for field in fields:
                value = node.data.get(field)
                if isinstance(value, str) and value and "$" in value:
                    new_v, _ = self._substitute_env_vars(value)
                    node.data[field] = new_v

    def _substitute_env_vars(self, text: str) -> tuple[str, list[str]]:
        """Replace ``${VAR}`` tokens against project env_vars then os.environ.

        Returns (substituted_text, unresolved_var_names).
        """
        import os
        import re as _re
        if not text or "$" not in text:
            return text, []
        project_env = self._project_env_vars()
        unresolved: list[str] = []

        def replace(m: "_re.Match[str]") -> str:
            var = m.group(1)
            if var in project_env:
                return str(project_env[var])
            if var in os.environ:
                return os.environ[var]
            unresolved.append(var)
            return m.group(0)

        return _re.sub(r"\$\{(\w+)\}", replace, text), unresolved

    async def _execute_api_call(self, node: GraphNode) -> None:
        """Make an HTTP request and store the response."""
        import httpx

        upstream_text = self._get_upstream_text(node)
        url_raw = (node.data.get("url") or "").strip()
        method = (node.data.get("method") or "POST").upper()
        content_type = node.data.get("content_type", "application/json")
        timeout_seconds = int(node.data.get("timeout_seconds", "30"))
        body_template = node.data.get("body_template", "").strip()

        if not url_raw:
            self._results[node.drawflow_id] = "[API_CALL_FAILED: no URL configured]"
            logger.warning("API Call '%s': no URL configured", node.name)
            return

        # Resolve ${VAR} substitutions across url, headers, body BEFORE validation.
        url, url_unresolved = self._substitute_env_vars(url_raw)
        if url_unresolved:
            msg = f"unresolved ${{{','.join(url_unresolved)}}} in URL — set on project Environment Variables panel or .env"
            self._results[node.drawflow_id] = f"[API_CALL_FAILED: {msg}]"
            logger.warning("API Call '%s': %s", node.name, msg)
            return

        # Validate URL (SSRF protection)
        try:
            self._validate_api_url(url)
        except PipelineError as exc:
            self._results[node.drawflow_id] = f"[API_CALL_FAILED: {exc}]"
            logger.warning("API Call '%s': %s", node.name, exc)
            return

        # Parse headers, then substitute env vars in each value.
        # An empty string (the designer's default for "no headers") must
        # be treated the same as a missing key — `dict.get(k, default)`
        # only falls back when the key is absent, not when it is present
        # with a falsy value, so use `or "{}"` to cover both.
        headers_raw = node.data.get("headers") or "{}"
        try:
            headers = json.loads(headers_raw) if isinstance(headers_raw, str) else headers_raw
            if not isinstance(headers, dict):
                headers = {}
        except json.JSONDecodeError as exc:
            self._results[node.drawflow_id] = f"[API_CALL_FAILED: invalid headers JSON: {exc}]"
            return
        all_unresolved: list[str] = []
        for hk, hv in list(headers.items()):
            new_v, unres = self._substitute_env_vars(str(hv))
            headers[hk] = new_v
            all_unresolved.extend(unres)
        if all_unresolved:
            msg = f"unresolved ${{{','.join(sorted(set(all_unresolved)))}}} in headers"
            self._results[node.drawflow_id] = f"[API_CALL_FAILED: {msg}]"
            logger.warning("API Call '%s': %s", node.name, msg)
            return

        headers.setdefault("Content-Type", content_type)

        # Build body — substitute ${VAR} first, then {upstream}
        if body_template:
            body_subbed, body_unres = self._substitute_env_vars(body_template)
            if body_unres:
                msg = f"unresolved ${{{','.join(sorted(set(body_unres)))}}} in body"
                self._results[node.drawflow_id] = f"[API_CALL_FAILED: {msg}]"
                logger.warning("API Call '%s': %s", node.name, msg)
                return
            body = body_subbed.replace("{upstream}", upstream_text)
        elif method in ("POST", "PUT", "PATCH"):
            body = upstream_text
        else:
            body = None

        # Execute request
        max_response = self._get_api_call_max_response_kb() * 1024
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=True,
            ) as client:
                resp = await client.request(method, url, headers=headers, content=body)

                # Check content type for binary
                resp_ct = resp.headers.get("content-type", "")
                if resp_ct and not any(
                    t in resp_ct for t in ("text/", "application/json", "application/xml", "application/rss+xml", "application/atom+xml")
                ):
                    result = f"[Binary response: {resp_ct}, {len(resp.content)} bytes, status={resp.status_code}]"
                else:
                    result = resp.text[:max_response]
                    if len(resp.text) > max_response:
                        result += f"\n[TRUNCATED: response was {len(resp.text)} bytes]"

        except httpx.TimeoutException:
            result = f"[API_CALL_FAILED: timeout after {timeout_seconds}s]"
            logger.warning("API Call '%s': timed out after %ds", node.name, timeout_seconds)
        except httpx.HTTPError as exc:
            result = f"[API_CALL_FAILED: {type(exc).__name__}: {exc}]"
            logger.warning("API Call '%s': HTTP error: %s", node.name, exc)
        except Exception as exc:
            result = f"[API_CALL_FAILED: {type(exc).__name__}: {exc}]"
            logger.warning("API Call '%s': unexpected error: %s", node.name, exc)

        self._results[node.drawflow_id] = result
        logger.info("API Call '%s': %s %s → stored %d chars", node.name, method, url, len(result))

    # ------------------------------------------------------------------
    # LLM Router post-completion routing
    # ------------------------------------------------------------------

    def _execute_llm_router_routing(self, node: GraphNode) -> None:
        """Parse LLM result and route to the matching output port."""
        import re as _re

        result = self._results.get(node.drawflow_id, "")
        route_count = int(node.data.get("route_count", "2"))

        # Multi-tier route extraction
        route_num = None
        stripped = result.strip()

        # Tier 1: exact single digit
        m = _re.match(r'^\s*([1-4])\s*$', stripped)
        if m:
            route_num = int(m.group(1))

        # Tier 2: "route/option/choice N" pattern
        if route_num is None:
            m = _re.search(r'(?:route|option|choice)\s*[:.]?\s*([1-4])\b', stripped, _re.IGNORECASE)
            if m:
                route_num = int(m.group(1))

        # Tier 3: last digit 1-4 found in response
        if route_num is None:
            matches = _re.findall(r'\b([1-4])\b', stripped)
            if matches:
                route_num = int(matches[-1])

        # Tier 4: default
        if route_num is None:
            logger.warning(
                "LLM Router '%s': could not extract route from response: %s",
                node.name, stripped[:200],
            )
            route_num = 1

        # Clamp to route_count
        if route_num > route_count:
            logger.warning(
                "LLM Router '%s': extracted route %d exceeds route_count %d, defaulting to 1",
                node.name, route_num, route_count,
            )
            route_num = 1

        logger.info("LLM Router '%s': routing to output_%d", node.name, route_num)

        # Skip all non-selected ports
        active_port = f"output_{route_num}"
        active_targets = set(node.downstream_ports.get(active_port, []))
        skip_all: set[str] = set()
        for port_name, targets in node.downstream_ports.items():
            if port_name != active_port:
                skip_all.update(targets)
        self._mark_skipped(skip_all - active_targets)

    # ------------------------------------------------------------------
    # Loop node — general-purpose review-fix cycle
    # ------------------------------------------------------------------

    async def _execute_loop(self, node: GraphNode) -> None:
        """Evaluate condition and retry upstream agent if it fails.

        Generalizes the review-fix cycle: if the condition fails, find
        the nearest upstream LLM node, re-run it with a revision prompt
        containing feedback, re-execute intermediate instant nodes, and
        re-evaluate.  Routes to output_1 on pass, output_2 on max exceeded.
        """
        import re as _re

        condition_type = node.data.get("condition_type", "not_contains")
        condition_value = node.data.get("condition_value", "")
        case_sensitive = bool(node.data.get("case_sensitive", False))
        max_iterations = int(node.data.get("max_iterations", "3"))
        revision_prompt_tmpl = node.data.get(
            "revision_prompt",
            "The previous attempt had issues:\n\n{feedback}\n\nPlease revise.",
        )
        on_max_exceeded = node.data.get("on_max_exceeded", "continue")

        def check_condition(text: str) -> bool:
            """Return True if condition PASSES (loop should exit)."""
            check_text = text if case_sensitive else text.upper()
            check_value = condition_value if case_sensitive else condition_value.upper()

            if condition_type == "contains":
                return check_value in check_text
            elif condition_type == "not_contains":
                return check_value not in check_text
            elif condition_type == "regex_match":
                flags = 0 if case_sensitive else _re.IGNORECASE
                return bool(_re.search(condition_value, text, flags))
            return True

        # Get current upstream result
        result = self._get_upstream_text(node)

        # Check if condition already passes
        if check_condition(result):
            logger.info("Loop '%s': condition passed on first check — exiting loop", node.name)
            self._results[node.drawflow_id] = result
            # Skip output_2 branch
            skip_targets = set(node.downstream_ports.get("output_2", []))
            if skip_targets:
                self._mark_skipped(skip_targets)
            return

        # Find upstream LLM node to retry
        upstream_llm = self._find_upstream_llm_node(node)
        if not upstream_llm:
            logger.warning("Loop '%s': no upstream LLM node found — cannot retry", node.name)
            self._results[node.drawflow_id] = result
            # Skip output_2 since we can't retry
            skip_targets = set(node.downstream_ports.get("output_2", []))
            if skip_targets:
                self._mark_skipped(skip_targets)
            return

        # Retry loop
        for iteration in range(max_iterations):
            if self._cancelled:
                raise PipelineError("Pipeline cancelled during loop", step="loop")

            logger.info(
                "Loop '%s': iteration %d/%d — condition failed, retrying upstream '%s'",
                node.name, iteration + 1, max_iterations, upstream_llm.name,
            )

            # Build revision prompt
            previous_output = self._results.get(upstream_llm.drawflow_id, "")
            revision_prompt = (revision_prompt_tmpl
                .replace("{feedback}", result)
                .replace("{previous}", previous_output)
                .replace("{iteration}", str(iteration + 1)))

            # Create and run revision task
            revision_task = await self._orch.create_task(
                self._project_name,
                prompt=revision_prompt,
                phase_number=self._phase_number,
                name=f"{upstream_llm.name} Revision {iteration + 1}",
                expert=upstream_llm.data.get("expert"),
                wave=node.wave,
                model=upstream_llm.data.get("model", "sonnet"),
            )
            await self._orch.start_task(revision_task["id"])
            await self._wait_for_wave([revision_task["id"]])

            # Get revision result
            revision_result = await self._get_task_result(revision_task["id"])
            if revision_result:
                self._results[upstream_llm.drawflow_id] = revision_result

            # Re-execute instant nodes between upstream LLM and this loop node
            await self._rerun_downstream_instant_nodes(upstream_llm, node)

            # Get the new upstream text (may have been transformed by instant nodes)
            result = self._get_upstream_text(node)

            # Check condition again
            if check_condition(result):
                logger.info("Loop '%s': condition passed after iteration %d", node.name, iteration + 1)
                self._results[node.drawflow_id] = result
                # Skip output_2 branch
                skip_targets = set(node.downstream_ports.get("output_2", []))
                if skip_targets:
                    self._mark_skipped(skip_targets)
                return

        # Max iterations exceeded
        logger.warning("Loop '%s': max iterations (%d) exceeded", node.name, max_iterations)
        self._results[node.drawflow_id] = result

        if on_max_exceeded == "fail":
            raise PipelineError(
                f"Loop '{node.name}' exceeded {max_iterations} iterations",
                step="loop",
            )

        # Route to output_2 (max exceeded branch), skip output_1
        skip_targets = set(node.downstream_ports.get("output_1", []))
        if skip_targets:
            self._mark_skipped(skip_targets)

    # ------------------------------------------------------------------
    # Retry logic for template-mode agent nodes
    # ------------------------------------------------------------------

    async def _handle_retry(self, node: GraphNode) -> None:
        """Check if a template-mode agent node's result triggers a retry loop.

        If ``retry_on_pattern`` is set and found in the result, re-run
        the upstream roadmapper with revision template + checker feedback.
        """
        pattern = node.data.get("retry_on_pattern", "").strip()
        if not pattern:
            return

        max_retries = int(node.data.get("max_retries", "0"))
        if max_retries <= 0:
            return

        result = self._results.get(node.drawflow_id, "")
        revision_inline = (node.data.get("retry_revision_prompt") or "").strip()
        revision_template = node.data.get("retry_revision_template", "").strip()

        for attempt in range(max_retries):
            if pattern.upper() not in result.upper():
                break  # Passed — no retry needed

            logger.info(
                "Retry %d/%d for node '%s': pattern '%s' found in result",
                attempt + 1, max_retries, node.name, pattern,
            )

            if not revision_inline and not revision_template:
                logger.warning("No revision prompt or preset configured — cannot retry")
                break

            # Traverse graph backwards to find the nearest LLM ancestor
            # (Plan Checker → Output Parser → Roadmapper: we need Roadmapper)
            upstream_node = self._find_upstream_llm_node(node)

            if not upstream_node:
                logger.warning("No upstream LLM node found for retry")
                break

            # Build revision prompt — inline text takes priority over preset
            if revision_inline:
                revision_tmpl_str = revision_inline
            else:
                session_factory = self._orch._execution_service._session_factory
                revision_tmpl = await _get_template_from_db(session_factory, revision_template)
                revision_tmpl_str = revision_tmpl["prompt_text"]
            variables: dict[str, str] = {
                "issues": result,
                "previous_plan_text": self._results.get(upstream_node.drawflow_id, ""),
            }

            # Resolve upstream node's variable_map for context
            orig_var_map = upstream_node.data.get("variable_map", "{}")
            try:
                orig_vars = json.loads(orig_var_map) if isinstance(orig_var_map, str) else orig_var_map
            except json.JSONDecodeError:
                orig_vars = {}
            for var_name, ref in orig_vars.items():
                variables.setdefault(var_name, self._resolve_variable(ref))

            if self._project:
                variables.setdefault("description", self._project.get("description", ""))

            from taktis.core.experts import format_expert_options
            session_factory = self._orch._execution_service._session_factory
            variables.setdefault("expert_options", await format_expert_options(session_factory))
            # Wrap synthesizer in XML tags (same as _build_template_prompt)
            if variables.get("synthesizer"):
                variables["synthesizer"] = (
                    f"<research_summary>\n{variables['synthesizer']}\n</research_summary>"
                )
            else:
                variables.setdefault("synthesizer", "")

            import re
            tmpl_vars = set(re.findall(r"\{(\w+)\}", revision_tmpl_str))
            for v in tmpl_vars:
                variables.setdefault(v, "")

            revision_prompt = revision_tmpl_str.format(**variables)

            # Run revision task
            revision_task = await self._orch.create_task(
                self._project_name,
                prompt=revision_prompt,
                phase_number=self._phase_number,
                name=f"{upstream_node.name} Revision {attempt + 1}",
                expert=upstream_node.data.get("expert"),
                wave=node.wave,
                model=upstream_node.data.get("model", "opus"),
            )
            await self._orch.start_task(revision_task["id"])
            await self._wait_for_wave([revision_task["id"]])
            revision_result = await self._get_task_result(revision_task["id"])
            if revision_result:
                self._results[upstream_node.drawflow_id] = revision_result

            # Re-execute instant nodes between upstream and checker
            # (e.g. Output Parser, file writers) so they see the revised output
            await self._rerun_downstream_instant_nodes(upstream_node, node)

            # Re-run checker
            checker_prompt = await self._build_template_prompt(node)
            checker_task = await self._orch.create_task(
                self._project_name,
                prompt=checker_prompt,
                phase_number=self._phase_number,
                name=f"{node.name} Re-check {attempt + 1}",
                expert=node.data.get("expert"),
                wave=node.wave,
                model=node.data.get("model", "opus"),
            )
            await self._orch.start_task(checker_task["id"])
            await self._wait_for_wave([checker_task["id"]])
            result = await self._get_task_result(checker_task["id"]) or result
            self._results[node.drawflow_id] = result

    # ------------------------------------------------------------------
    # Wait for wave completion
    # ------------------------------------------------------------------

    async def _wait_for_wave(self, task_ids: list[str]) -> bool:
        """Wait for all tasks to reach terminal state. Returns True if all OK.

        Unlike the wave scheduler, the graph executor must wait for interactive
        tasks to fully complete (``awaiting_input`` means the user hasn't
        finished yet).  Downstream nodes need the final result, not an
        intermediate one.

        Uses event-driven waiting (subscribes to task completion/failure events)
        with a periodic DB poll fallback.  Events are authoritative — a DB
        status read alone is not trusted for "failed" without event confirmation
        to avoid WAL stale-read races on Windows.
        """
        terminal = {"completed", "failed", "cancelled"}
        ok_statuses = {"completed"}
        pending = set(task_ids)
        results: dict[str, str] = {}
        session_factory = self._orch._execution_service._session_factory
        event_bus = self._orch.event_bus

        q_completed = event_bus.subscribe(EVENT_TASK_COMPLETED)
        q_failed = event_bus.subscribe(EVENT_TASK_FAILED)

        async def _drain_events() -> None:
            """Drain event queues and update results.

            For completed events, verify the DB status — interactive tasks
            may have been downgraded to ``awaiting_input`` by the
            on_complete callback *before* the event was published.
            """
            for q, status in [(q_completed, "completed"), (q_failed, "failed")]:
                while True:
                    try:
                        envelope = q.get_nowait()
                        data = envelope.get("data", {})
                        tid = data.get("task_id")
                        if tid not in pending:
                            continue
                        # Verify actual DB status — interactive tasks go to
                        # awaiting_input even though the event says completed
                        if status == "completed":
                            async with session_factory() as conn:
                                task = await repo.get_task(conn, tid)
                            if task and task["status"] == "awaiting_input":
                                logger.info(
                                    "Task %s event=completed but DB=awaiting_input — still waiting", tid
                                )
                                continue
                        results[tid] = status
                        pending.discard(tid)
                    except asyncio.QueueEmpty:
                        break

        try:
            # Initial check — tasks may already be done before we subscribed
            async with session_factory() as conn:
                for tid in list(pending):
                    task = await repo.get_task(conn, tid)
                    if task and task["status"] in terminal:
                        results[tid] = task["status"]
                        pending.discard(tid)

            while pending:
                await asyncio.sleep(2)
                await _drain_events()
                if not pending:
                    break

                # Fallback DB poll for tasks whose events we may have missed
                async with session_factory() as conn:
                    for tid in list(pending):
                        task = await repo.get_task(conn, tid)
                        if task is None:
                            pending.discard(tid)
                            continue
                        if task["status"] in terminal:
                            results[tid] = task["status"]
                            pending.discard(tid)
        finally:
            event_bus.unsubscribe(EVENT_TASK_COMPLETED, q_completed)
            event_bus.unsubscribe(EVENT_TASK_FAILED, q_failed)

        # Log non-OK results
        for tid in task_ids:
            status = results.get(tid)
            if status and status not in ok_statuses:
                logger.warning("Task %s ended with status %s", tid, status)

        return all(results.get(tid) in ok_statuses for tid in task_ids)

    # ------------------------------------------------------------------
    # Result collection
    # ------------------------------------------------------------------

    async def _get_task_result(self, task_id: str) -> Optional[str]:
        """Read full task result from the LAST 'result' event, falling back to result_summary.

        Interactive tasks produce multiple result events (one per conversation
        turn).  We need the final one — not an intermediate result that may
        contain stale or misleading text.
        """
        session_factory = self._orch._execution_service._session_factory
        async with session_factory() as conn:
            # Scan the last 50 outputs and pick the LAST result event
            outputs = await repo.get_task_outputs(conn, task_id, tail=50)
            last_result: Optional[str] = None
            for out in outputs:
                content = out.get("content")
                if isinstance(content, dict) and content.get("type") == "result":
                    full = content.get("result")
                    if full:
                        last_result = full
                elif isinstance(content, str):
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict) and parsed.get("type") == "result":
                            full = parsed.get("result")
                            if full:
                                last_result = full
                    except (json.JSONDecodeError, TypeError):
                        pass
            if last_result is not None:
                return last_result
            # Fallback to result_summary (truncated to 2000 chars)
            task = await repo.get_task(conn, task_id)
            if task is None:
                return None
            return task.get("result_summary")
