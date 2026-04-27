"""Extract ``${VAR}`` references from a Drawflow pipeline JSON.

Used to surface "required environment variables" on the pipeline editor and
on the new-project form, so users know which env-vars to set before running
a scheduled pipeline. The runtime substitution itself happens in
``GraphExecutor._substitute_env_vars`` against project default_env_vars and
``os.environ``.
"""

from __future__ import annotations

import json
import re
from typing import Any

_VAR_RE = re.compile(r"\$\{(\w+)\}")

# Node-type → list of string fields whose values are ``${VAR}``-substituted
# at runtime.  ``api_call`` runs its own strict resolver (failing on
# unresolved vars); the others soft-substitute via
# ``GraphExecutor._pre_substitute_env_vars`` and leave unresolved tokens
# literal so the user notices in the rendered output.
SUBSTITUTABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "api_call": ("url", "headers", "body_template"),
    "agent": ("prompt", "retry_revision_prompt"),
    "fan_out": ("prompt",),
    "llm_router": ("routing_prompt",),
    "file_writer": ("filename",),
    "text_transform": ("value",),
    "aggregator": ("header", "separator"),
    "pipeline_generator": ("target_template_name",),
}


def extract_required_env_vars(flow_json: Any) -> list[str]:
    """Return the sorted, unique list of ``${VAR}`` names referenced by api_call
    nodes in a Drawflow flow.

    Accepts either a parsed dict or a JSON string. Returns ``[]`` for any
    malformed input — callers should not depend on this raising.
    """
    if isinstance(flow_json, str):
        try:
            flow_json = json.loads(flow_json)
        except (ValueError, TypeError):
            return []
    if not isinstance(flow_json, dict):
        return []
    drawflow = flow_json.get("drawflow", flow_json)
    if not isinstance(drawflow, dict):
        return []
    found: set[str] = set()
    for module in drawflow.values():
        if not isinstance(module, dict):
            continue
        nodes = module.get("data", {})
        if not isinstance(nodes, dict):
            continue
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            fields = SUBSTITUTABLE_FIELDS.get(node.get("name", ""), ())
            if not fields:
                continue
            data = node.get("data") or {}
            for field in fields:
                value = data.get(field, "")
                if isinstance(value, str) and value:
                    for m in _VAR_RE.finditer(value):
                        found.add(m.group(1))
    return sorted(found)


def enrich_template(template: dict) -> dict:
    """Mutate ``template`` to add a ``required_env_vars`` key derived from its
    ``flow_json``. Returns the same dict for chaining.
    """
    flow = template.get("flow_json")
    if isinstance(flow, str):
        try:
            flow = json.loads(flow)
        except (ValueError, TypeError):
            flow = None
    template["required_env_vars"] = extract_required_env_vars(flow) if flow else []
    return template
