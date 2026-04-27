"""Convert structured pipeline specifications into valid Drawflow JSON.

Pure functions -- no async, no DB access.  The spec format is defined in
``taktis/defaults/PIPELINE_SCHEMA.md`` under "Structured Pipeline
Specification Format".

Main entry points:
- ``spec_to_drawflow(spec)``   -- spec dict -> complete Drawflow template dict
- ``validate_spec(spec)``      -- spec dict -> list of error strings
- ``validate_drawflow(flow)``  -- Drawflow flow_json dict -> list of error strings
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from taktis.core.node_types import NODE_TYPES


# -- Multi-output node types and their output port counts --------------------

_MULTI_OUTPUT_COUNTS: dict[str, int] = {
    "conditional": 2,
    "human_gate": 2,
    "loop": 2,
    "llm_router": 4,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_output_count(type_id: str) -> int:
    """Return the number of output ports for a node type."""
    if type_id in _MULTI_OUTPUT_COUNTS:
        return _MULTI_OUTPUT_COUNTS[type_id]
    nt = NODE_TYPES.get(type_id)
    if nt is not None:
        return nt.outputs
    return 1


def _get_input_count(type_id: str) -> int:
    nt = NODE_TYPES.get(type_id)
    if nt is not None:
        return nt.inputs
    return 1


def _default_config(type_id: str) -> dict[str, Any]:
    """Build a dict of default values from the NODE_TYPES registry."""
    nt = NODE_TYPES.get(type_id)
    if nt is None:
        return {}
    defaults: dict[str, Any] = {}
    for f in nt.config_schema:
        if f.default is not None:
            defaults[f.key] = f.default
    return defaults


def _topological_layers(nodes: list[dict]) -> dict[str, int]:
    """Assign each node ID to a layer (0-based) via Kahn's algorithm.

    Raises ``ValueError`` on cycles.
    """
    # Build adjacency from connections_to
    children: dict[str, list[str]] = defaultdict(list)
    parents: dict[str, list[str]] = defaultdict(list)
    all_ids = {n["id"] for n in nodes}

    for node in nodes:
        conn = node.get("connections_to", [])
        targets: list[str] = []
        if isinstance(conn, list):
            targets = conn
        elif isinstance(conn, dict):
            for port_targets in conn.values():
                targets.extend(port_targets)
        for t in targets:
            if t in all_ids:
                children[node["id"]].append(t)
                parents[t].append(node["id"])

    in_degree: dict[str, int] = {nid: 0 for nid in all_ids}
    for nid in all_ids:
        in_degree[nid] = len(parents[nid])

    queue: deque[str] = deque()
    layer: dict[str, int] = {}
    for nid in all_ids:
        if in_degree[nid] == 0:
            queue.append(nid)
            layer[nid] = 0

    visited = 0
    while queue:
        nid = queue.popleft()
        visited += 1
        for child in children[nid]:
            in_degree[child] -= 1
            layer[child] = max(layer.get(child, 0), layer[nid] + 1)
            if in_degree[child] == 0:
                queue.append(child)

    if visited != len(all_ids):
        raise ValueError("Graph contains a cycle")

    return layer


# -- Port name normalization ------------------------------------------------

# Common LLM mistakes when naming output ports
_PORT_ALIASES: dict[str, str] = {
    # human_gate
    "approved": "output_1",
    "rejected": "output_2",
    "approve": "output_1",
    "reject": "output_2",
    # conditional / loop
    "pass": "output_1",
    "fail": "output_2",
    "true": "output_1",
    "false": "output_2",
    "yes": "output_1",
    "no": "output_2",
    "passed": "output_1",
    "failed": "output_2",
    # llm_router
    "route_1": "output_1",
    "route_2": "output_2",
    "route_3": "output_3",
    "route_4": "output_4",
    "route1": "output_1",
    "route2": "output_2",
    "route3": "output_3",
    "route4": "output_4",
}


def _normalize_port(port: str) -> str:
    """Normalize a port name to output_N format."""
    p = port.strip().lower()
    if p in _PORT_ALIASES:
        return _PORT_ALIASES[p]
    # Already valid
    if p.startswith("output_") and p[7:].isdigit():
        return p
    return port  # return as-is, validation will catch it


# ---------------------------------------------------------------------------
# spec_to_drawflow
# ---------------------------------------------------------------------------

def spec_to_drawflow(spec: dict) -> dict:
    """Convert a structured pipeline specification into a Drawflow template dict.

    Parameters
    ----------
    spec : dict
        Must contain ``name``, ``description``, and ``nodes`` (list).
        See PIPELINE_SCHEMA.md "Structured Pipeline Specification Format".

    Returns
    -------
    dict
        Complete template dict with ``name``, ``description``, ``flow_json``,
        and ``is_default``.

    Raises
    ------
    ValueError
        If the spec is fundamentally malformed (missing keys, cycles).
    """
    if not isinstance(spec, dict):
        raise ValueError("spec must be a dict")
    if "nodes" not in spec or not isinstance(spec["nodes"], list):
        raise ValueError("spec must contain a 'nodes' list")
    if not spec["nodes"]:
        raise ValueError("spec must contain at least one node")

    nodes = spec["nodes"]
    # Coerce all IDs to strings (LLMs sometimes output integers)
    for n in nodes:
        n["id"] = str(n["id"])
        conn = n.get("connections_to", [])
        if isinstance(conn, list):
            n["connections_to"] = [str(t) for t in conn]
        elif isinstance(conn, dict):
            n["connections_to"] = {k: [str(t) for t in v] for k, v in conn.items()}
    node_ids = {n["id"] for n in nodes}

    # Compute layers for positioning
    layers = _topological_layers(nodes)

    # Group nodes by layer for vertical positioning
    layer_groups: dict[int, list[dict]] = defaultdict(list)
    for node in nodes:
        layer_groups[layers[node["id"]]].append(node)

    # Build reverse mapping: for each target node, track which source+port connects to it
    # Key: target_id -> list of (source_id, output_port)
    incoming: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for node in nodes:
        conn = node.get("connections_to", [])
        type_id = node.get("type", "agent")
        if isinstance(conn, list):
            # All connections go through output_1
            for target in conn:
                if target in node_ids:
                    incoming[target].append((node["id"], "output_1"))
        elif isinstance(conn, dict):
            for port, targets in conn.items():
                norm_port = _normalize_port(port)
                for target in targets:
                    if target in node_ids:
                        incoming[target].append((node["id"], norm_port))

    # Build Drawflow data nodes
    data: dict[str, dict] = {}

    for node in nodes:
        nid = node["id"]
        type_id = node.get("type", "agent")
        name = node.get("name", f"Node {nid}")
        config = node.get("config", {})

        # Merge defaults with provided config
        full_config = _default_config(type_id)
        full_config.update(config)
        full_config["type_id"] = type_id
        full_config["name"] = name

        # Build outputs
        output_count = _get_output_count(type_id)
        outputs: dict[str, dict] = {}
        conn = node.get("connections_to", [])

        # Parse connections_to into per-port targets
        port_targets: dict[str, list[str]] = defaultdict(list)
        if isinstance(conn, list):
            for target in conn:
                if target in node_ids:
                    port_targets["output_1"].append(target)
        elif isinstance(conn, dict):
            for port, targets in conn.items():
                norm_port = _normalize_port(port)
                for target in targets:
                    if target in node_ids:
                        port_targets[norm_port].append(target)

        for i in range(1, output_count + 1):
            port_name = f"output_{i}"
            connections = []
            for target in port_targets.get(port_name, []):
                connections.append({"node": target, "output": "input_1"})
            outputs[port_name] = {"connections": connections}

        # Build inputs
        input_count = _get_input_count(type_id)
        inputs: dict[str, dict] = {}
        for i in range(1, input_count + 1):
            port_name = f"input_{i}"
            connections = []
            for src_id, src_port in incoming.get(nid, []):
                connections.append({"node": src_id, "input": src_port})
            inputs[port_name] = {"connections": connections}

        # Position
        layer_idx = layers[nid]
        idx_in_layer = layer_groups[layer_idx].index(node)
        pos_x = 50 + layer_idx * 350
        pos_y = 50 + idx_in_layer * 180

        data[nid] = {
            "id": int(nid),
            "name": type_id,
            "data": full_config,
            "class": type_id,
            "html": "",
            "typenode": False,
            "inputs": inputs,
            "outputs": outputs,
            "pos_x": pos_x,
            "pos_y": pos_y,
        }

    return {
        "name": spec.get("name", "Untitled Pipeline"),
        "description": spec.get("description", ""),
        "flow_json": {
            "drawflow": {
                "Home": {
                    "data": data,
                }
            }
        },
        "is_default": False,
    }


# ---------------------------------------------------------------------------
# validate_spec
# ---------------------------------------------------------------------------

def validate_spec(spec: dict) -> list[str]:
    """Validate a structured pipeline specification.

    Returns a list of error messages.  Empty list means valid.
    """
    errors: list[str] = []

    if not isinstance(spec, dict):
        return ["spec must be a dict"]

    nodes = spec.get("nodes")
    if not isinstance(nodes, list):
        return ["spec must contain a 'nodes' list"]

    if not nodes:
        errors.append("Pipeline must contain at least one node")
        return errors

    # Check unique IDs
    seen_ids: set[str] = set()
    for node in nodes:
        nid = node.get("id")
        if nid is None:
            errors.append(f"Node missing 'id': {node.get('name', '?')}")
            continue
        if nid in seen_ids:
            errors.append(f"Duplicate node ID: {nid}")
        seen_ids.add(nid)

    # Check node types
    for node in nodes:
        type_id = node.get("type")
        if type_id is None:
            errors.append(f"Node {node.get('id', '?')} missing 'type'")
        elif type_id not in NODE_TYPES:
            errors.append(f"Node {node.get('id', '?')}: unknown type '{type_id}'")

    # Check connection targets exist
    for node in nodes:
        nid = node.get("id", "?")
        conn = node.get("connections_to", [])
        targets: list[tuple[str, str]] = []  # (port, target_id)
        if isinstance(conn, list):
            for t in conn:
                targets.append(("output_1", t))
        elif isinstance(conn, dict):
            for port, port_targets in conn.items():
                for t in port_targets:
                    targets.append((_normalize_port(port), t))

        for port, target in targets:
            if target not in seen_ids:
                errors.append(
                    f"Node {nid}: connection target '{target}' does not exist"
                )

    # Check port counts for multi-output connections
    for node in nodes:
        nid = node.get("id", "?")
        type_id = node.get("type")
        if type_id is None or type_id not in NODE_TYPES:
            continue
        conn = node.get("connections_to", [])
        if isinstance(conn, dict):
            expected = _get_output_count(type_id)
            for port in conn:
                # Normalize common LLM port name mistakes
                norm = _normalize_port(port)
                # Extract port number
                try:
                    port_num = int(norm.split("_")[-1])
                except (ValueError, IndexError):
                    errors.append(
                        f"Node {nid}: invalid port name '{port}'"
                    )
                    continue
                if port_num > expected:
                    errors.append(
                        f"Node {nid} ({type_id}): port '{port}' exceeds "
                        f"max outputs ({expected})"
                    )

    # Check required config fields
    for node in nodes:
        nid = node.get("id", "?")
        type_id = node.get("type")
        if type_id is None or type_id not in NODE_TYPES:
            continue
        nt = NODE_TYPES[type_id]
        config = node.get("config", {})
        for field in nt.config_schema:
            if field.required and field.key not in config:
                # Check if there's a default -- if so, it will be filled in
                if field.default is None:
                    errors.append(
                        f"Node {nid} ({type_id}): missing required config "
                        f"field '{field.key}'"
                    )

    # Check for cycles
    try:
        _topological_layers(nodes)
    except ValueError:
        errors.append("Graph contains a cycle")

    return errors


# ---------------------------------------------------------------------------
# validate_drawflow
# ---------------------------------------------------------------------------

def validate_drawflow(flow_json: dict) -> list[str]:
    """Validate an already-generated Drawflow JSON dict.

    Checks structural integrity: bidirectional connections, required fields,
    and node ID consistency.

    Parameters
    ----------
    flow_json : dict
        The ``flow_json`` portion of a template (contains ``drawflow`` key).

    Returns
    -------
    list[str]
        Error messages.  Empty means valid.
    """
    errors: list[str] = []

    drawflow = flow_json.get("drawflow", {})
    if not isinstance(drawflow, dict):
        return ["flow_json.drawflow must be a dict"]

    for module_name, module in drawflow.items():
        if not isinstance(module, dict) or "data" not in module:
            errors.append(f"Module '{module_name}' missing 'data'")
            continue

        data = module["data"]
        existing_ids = set(data.keys())

        for str_id, node in data.items():
            # ID consistency
            node_id = node.get("id")
            if node_id is not None and str(node_id) != str_id:
                errors.append(
                    f"Node key '{str_id}' does not match id field {node_id}"
                )

            # Required data fields per node type
            node_data = node.get("data", {})
            type_id = node_data.get("type_id") or node.get("name")
            if type_id and type_id in NODE_TYPES:
                nt = NODE_TYPES[type_id]
                for field in nt.config_schema:
                    if field.required and field.key not in node_data:
                        if field.default is None:
                            errors.append(
                                f"Node {str_id}: missing required data "
                                f"field '{field.key}'"
                            )

            # Check output connections reference existing nodes
            for port_name, port in (node.get("outputs") or {}).items():
                for conn in (port.get("connections") or []):
                    target = str(conn.get("node", ""))
                    if target not in existing_ids:
                        errors.append(
                            f"Node {str_id} {port_name}: target node "
                            f"'{target}' does not exist"
                        )

            # Check input connections reference existing nodes
            for port_name, port in (node.get("inputs") or {}).items():
                for conn in (port.get("connections") or []):
                    source = str(conn.get("node", ""))
                    if source not in existing_ids:
                        errors.append(
                            f"Node {str_id} {port_name}: source node "
                            f"'{source}' does not exist"
                        )

            # Check bidirectionality: every output connection must have
            # a matching input connection on the target
            for port_name, port in (node.get("outputs") or {}).items():
                for conn in (port.get("connections") or []):
                    target_id = str(conn.get("node", ""))
                    target_input_port = conn.get("output", "input_1")
                    if target_id not in existing_ids:
                        continue  # already reported above
                    target_node = data[target_id]
                    target_inputs = target_node.get("inputs", {})
                    target_port = target_inputs.get(target_input_port, {})
                    target_conns = target_port.get("connections", [])
                    # Look for a back-reference from target to this node
                    found = any(
                        str(tc.get("node", "")) == str_id
                        and tc.get("input", "") == port_name
                        for tc in target_conns
                    )
                    if not found:
                        errors.append(
                            f"Asymmetric connection: node {str_id} "
                            f"{port_name} -> node {target_id} "
                            f"{target_input_port}, but no back-reference"
                        )

    return errors
