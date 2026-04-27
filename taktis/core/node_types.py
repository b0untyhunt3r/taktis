"""Extensible node type registry for the pipeline builder.

Each NodeType defines metadata (label, color, ports) and a config schema
that drives both the editor UI and graph executor.  New node types are
registered at import time via ``register_node_type()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ConfigField:
    """One user-configurable field shown in the editor's config panel."""
    key: str                    # e.g. "expert", "prompt"
    label: str                  # human label
    field_type: str             # "expert_select" | "textarea" | "select" | "text" | "checkbox"
    required: bool = False
    default: Any = None
    options: Optional[list] = None   # for "select" fields
    hint: Optional[str] = None       # help text shown below the field

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NodeType:
    """Describes one kind of node that can appear on the Drawflow canvas."""
    type_id: str                # unique key, e.g. "personality"
    label: str                  # display name
    category: str               # "agent", "integration", "control"
    description: str
    inputs: int                 # number of input ports (0 = start node)
    outputs: int                # number of output ports
    config_schema: list[ConfigField] = field(default_factory=list)
    color: str = "#3b82f6"      # left-border accent colour

    def to_dict(self) -> dict:
        d = asdict(self)
        d["config_schema"] = [f.to_dict() for f in self.config_schema]
        return d


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

NODE_TYPES: dict[str, NodeType] = {}


def register_node_type(node_type: NodeType) -> None:
    """Register a node type (idempotent — last-write wins)."""
    NODE_TYPES[node_type.type_id] = node_type


def get_node_type(type_id: str) -> Optional[NodeType]:
    return NODE_TYPES.get(type_id)


def list_node_types() -> list[NodeType]:
    return list(NODE_TYPES.values())


# ---------------------------------------------------------------------------
# Built-in node types
# ---------------------------------------------------------------------------

register_node_type(NodeType(
    type_id="agent",
    label="Agent",
    category="agent",
    description="Run a Claude task — standard prompt, template with variables, or interactive interview",
    inputs=1,
    outputs=1,
    config_schema=[
        # Mode selector — drives which fields are visible in the UI
        ConfigField("mode", "Mode", "select", required=True,
                    default="standard",
                    options=["standard", "template"]),
        # Shared fields
        ConfigField("model", "Model", "select", required=False, default="sonnet",
                    options=["sonnet", "opus", "haiku"]),
        # Standard + Template: expert selection
        ConfigField("expert", "Expert Persona", "expert_select", required=False),
        # Standard: raw prompt
        ConfigField("prompt", "Task Prompt", "textarea", required=False, default=""),
        # Standard + Template: interactive toggle
        ConfigField("interactive", "Interactive", "checkbox", required=False, default=False),
        # Template: preset + variable mapping
        ConfigField("template", "Load Preset", "select", required=False,
                    options=[]),
        ConfigField("variable_map", "Variable Mapping (JSON)", "textarea",
                    required=False, default="{}"),
        ConfigField("inject_expert_options", "Inject Expert Options", "checkbox",
                    required=False, default=False),
        ConfigField("inject_description", "Inject Project Description", "checkbox",
                    required=False, default=True),
        # Template: retry
        ConfigField("retry_on_pattern", "Retry Trigger Pattern", "text",
                    required=False, default=""),
        ConfigField("max_retries", "Max Retries", "select", required=False,
                    default="0", options=["0", "1", "2", "3"]),
        ConfigField("retry_revision_template", "Revision Preset", "select",
                    required=False, default="",
                    options=[]),
        ConfigField("retry_revision_prompt", "Revision Prompt Text", "textarea",
                    required=False, default=""),
        # Transient error retry policy
        ConfigField("retry_transient", "Auto-Retry on Errors", "checkbox",
                    required=False, default=True,
                    hint="Automatically retry on transient errors (streaming, rate limits)"),
        ConfigField("retry_max_attempts", "Max Retry Attempts", "select",
                    required=False, default="2",
                    options=["1", "2", "3", "5"]),
        ConfigField("retry_backoff", "Retry Backoff", "select",
                    required=False, default="none",
                    options=["none", "linear", "exponential"],
                    hint="none=immediate, linear=2s/4s/6s, exponential=2s/4s/8s"),
    ],
    color="#3b82f6",
))

register_node_type(NodeType(
    type_id="output_parser",
    label="Output Parser",
    category="transform",
    description="Split upstream output into named sections using text markers",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("markers", "Section Markers (one per line)", "textarea",
                    required=True,
                    default="===REQUIREMENTS===\n===ROADMAP===\n===PLAN==="),
        ConfigField("section_names", "Section Names (one per line)", "textarea",
                    required=True,
                    default="requirements\nroadmap\nplan"),
    ],
    color="#f59e0b",
))

register_node_type(NodeType(
    type_id="file_writer",
    label="Write File",
    category="action",
    description="Write upstream result to a file in .taktis/",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("filename", "Filename", "text",
                    required=True, default="",
                    hint="Path under .taktis/ — e.g. 'REQUIREMENTS.md' or 'research/STACK.md'. "
                         "Supports time placeholders for dated outputs: {{date}} (YYYY-MM-DD, UTC), "
                         "{{datetime}} (YYYY-MM-DDTHH-MM, UTC), {{week_num}} (ISO week, zero-padded), "
                         "{{year}}. Substituted at write time. Example: 'briefings/{{date}}.md'."),
        ConfigField("source_section", "Source Section", "text",
                    required=False, default="",
                    hint="Extract a named key from an upstream Output Parser (e.g. 'requirements'). Leave empty to write the full upstream text."),
        ConfigField("context_priority", "Include in Context", "select",
                    required=False, default="none",
                    options=["none", "P0 — must include", "P1 — high", "P2 — medium", "P3 — low", "P4 — trim first"],
                    hint="Makes this file available to all downstream consumers: later pipeline phases, "
                         "manual tasks, and scheduler-driven phases. Higher priority files are included first "
                         "when context budget is limited. 'none' = saved to disk only."),
    ],
    color="#06b6d4",
))

register_node_type(NodeType(
    type_id="plan_applier",
    label="Apply Plan",
    category="action",
    description="Parse JSON plan from upstream and create phases + tasks in the database",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("await_approval", "Require User Approval", "checkbox",
                    required=False, default=True),
        ConfigField("source_section", "Plan Section", "text",
                    required=False, default="plan",
                    hint="Key to extract from an upstream Output Parser (e.g. 'plan'). The extracted JSON is parsed and applied as phases/tasks."),
    ],
    color="#10b981",
))

register_node_type(NodeType(
    type_id="conditional",
    label="Conditional",
    category="control",
    description="Route to different branches based on upstream output content",
    inputs=1,
    outputs=2,       # output_1 = pass (condition met), output_2 = fail
    config_schema=[
        ConfigField("condition_type", "Condition Type", "select", required=True,
                    default="contains",
                    options=["contains", "not_contains", "regex_match",
                             "result_is", "task_failed"]),
        ConfigField("condition_value", "Value / Pattern", "text", required=False,
                    default="",
                    hint="Text to search for, regex pattern, or status value. "
                         "For 'task_failed' this is ignored."),
        ConfigField("case_sensitive", "Case Sensitive", "checkbox",
                    required=False, default=False),
    ],
    color="#eab308",  # yellow — decision point
))

register_node_type(NodeType(
    type_id="phase_settings",
    label="Phase Settings",
    category="control",
    description="Configure phase name, goal, success criteria, and cross-phase context files",
    inputs=0,
    outputs=0,
    config_schema=[
        ConfigField("phase_name", "Phase Name", "text", required=True, default=""),
        ConfigField("phase_goal", "Phase Goal", "textarea", required=False, default=""),
        ConfigField("success_criteria", "Success Criteria (one per line)", "textarea",
                    required=False, default=""),
        ConfigField("context_files", "Context Files from Previous Phases", "multi_select",
                    required=False, default=""),
        ConfigField("phase_review", "Phase Review", "checkbox",
                    required=False, default=False,
                    hint="After this phase completes, a reviewer inspects the output. CRITICALs trigger auto-fix attempts (up to 3)."),
    ],
    color="#a855f7",
))

register_node_type(NodeType(
    type_id="aggregator",
    label="Aggregator",
    category="transform",
    description="Combine parallel upstream outputs into a single result",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("strategy", "Merge Strategy", "select", required=True,
                    default="concat",
                    options=["concat", "json_merge", "numbered_list", "xml_wrap"]),
        ConfigField("separator", "Separator (for concat)", "text",
                    required=False, default="\n\n---\n\n"),
    ],
    color="#8b5cf6",
))

register_node_type(NodeType(
    type_id="human_gate",
    label="Human Gate",
    category="control",
    description="Pause execution and wait for user approval before continuing",
    inputs=1,
    outputs=2,
    config_schema=[
        ConfigField("gate_message", "Message to User", "textarea",
                    required=False,
                    default="Pipeline paused — review and approve to continue.",
                    hint="Shown to the user when the gate activates"),
        ConfigField("show_upstream", "Show Upstream Result", "checkbox",
                    required=False, default=True,
                    hint="Display the upstream output in the approval dialog"),
    ],
    color="#f97316",
))

register_node_type(NodeType(
    type_id="api_call",
    label="API Call / Webhook",
    category="action",
    description="Make an HTTP request to an external URL (webhooks, APIs)",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("url", "URL", "text", required=True, default="",
                    hint="The endpoint URL. Use ${VAR} for project env-vars or shell env-vars; "
                         "{upstream} only works in the body, not the URL."),
        ConfigField("method", "HTTP Method", "select", required=True,
                    default="POST",
                    options=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ConfigField("content_type", "Content Type", "select", required=False,
                    default="application/json",
                    options=["application/json", "text/plain",
                             "application/x-www-form-urlencoded"]),
        ConfigField("headers", "Headers (JSON)", "textarea", required=False,
                    default="{}",
                    hint='JSON object of custom headers, e.g. {"Authorization": "Bearer ${GH_TOKEN}"}. '
                         'Values support ${VAR} substitution.'),
        ConfigField("body_template", "Body Template", "textarea", required=False,
                    default="",
                    hint="Request body. Use {upstream} for upstream text and ${VAR} "
                         "for env-vars. Leave empty to send upstream as-is."),
        ConfigField("timeout_seconds", "Timeout (seconds)", "select", required=False,
                    default="30",
                    options=["10", "30", "60", "120"]),
    ],
    color="#ec4899",
))

register_node_type(NodeType(
    type_id="llm_router",
    label="LLM Router",
    category="control",
    description="Use a lightweight LLM to classify input and route to different branches",
    inputs=1,
    outputs=4,
    config_schema=[
        ConfigField("routing_prompt", "Routing Prompt", "textarea", required=True,
                    default="Classify the following input into exactly one category.\n\n"
                            "Categories:\n- Route 1: {describe}\n- Route 2: {describe}\n\n"
                            "Respond with ONLY the route number (1, 2, 3, or 4). Nothing else.",
                    hint="Prompt sent to the LLM. Must produce a single route number."),
        ConfigField("model", "Model", "select", required=False,
                    default="haiku",
                    options=["haiku", "sonnet"]),
        ConfigField("route_count", "Number of Routes", "select", required=True,
                    default="2",
                    options=["2", "3", "4"]),
    ],
    color="#14b8a6",
))

register_node_type(NodeType(
    type_id="text_transform",
    label="Text Transform",
    category="transform",
    description="Transform upstream text without an LLM call — prepend, append, replace, extract, wrap",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("operation", "Operation", "select", required=True,
                    default="prepend",
                    options=["prepend", "append", "replace", "extract_json",
                             "wrap_xml", "template"]),
        ConfigField("text", "Text", "textarea", required=False, default="",
                    hint="Text to prepend/append, or template with {upstream}. "
                         "For wrap_xml, the tag name."),
        ConfigField("find_pattern", "Find Pattern", "text", required=False,
                    default="",
                    hint="For 'replace': text or regex to find"),
        ConfigField("use_regex", "Use Regex", "checkbox", required=False,
                    default=False,
                    hint="Treat find_pattern as regex"),
    ],
    color="#64748b",
))

register_node_type(NodeType(
    type_id="fan_out",
    label="Fan Out",
    category="control",
    description="Split upstream into items and run the same agent task once per item in parallel",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("split_mode", "Split Mode", "select", required=True,
                    default="newline",
                    options=["newline", "delimiter", "numbered_list", "json_array"],
                    hint="How to split upstream text into items"),
        ConfigField("delimiter", "Custom Delimiter", "text",
                    required=False, default="---",
                    hint="Delimiter string (only used with 'delimiter' mode)"),
        ConfigField("expert", "Expert Persona", "expert_select", required=False,
                    hint="Expert to use for each parallel task"),
        ConfigField("prompt_template", "Prompt Template", "textarea",
                    required=False, default="Process the following item:\n\n{item}",
                    hint="Prompt for each item. Use {item} for the current item, {index} for 1-based index, {total} for total count."),
        ConfigField("model", "Model", "select", required=False, default="sonnet",
                    options=["sonnet", "opus", "haiku"]),
        ConfigField("max_parallel", "Max Parallel", "select", required=False,
                    default="10",
                    options=["3", "5", "10", "15", "20"],
                    hint="Maximum concurrent tasks"),
        ConfigField("merge_strategy", "Merge Strategy", "select", required=False,
                    default="numbered",
                    options=["concat", "numbered", "json_array"],
                    hint="How to combine results from all items"),
        ConfigField("merge_separator", "Merge Separator", "text",
                    required=False, default="\n\n---\n\n",
                    hint="Separator between results (for concat mode)"),
    ],
    color="#f472b6",  # pink
))

register_node_type(NodeType(
    type_id="loop",
    label="Loop",
    category="control",
    description="Retry upstream agent when condition fails — enables review-fix cycles",
    inputs=1,
    outputs=2,  # output_1 = pass (exit loop), output_2 = max iterations exceeded
    config_schema=[
        ConfigField("condition_type", "Pass Condition", "select", required=True,
                    default="not_contains",
                    options=["contains", "not_contains", "regex_match"],
                    hint="Condition that must be TRUE to exit the loop (pass to output_1)"),
        ConfigField("condition_value", "Value / Pattern", "text", required=True,
                    default="CRITICAL",
                    hint="When condition passes, the loop exits. E.g. not_contains 'CRITICAL' = exit when no criticals found."),
        ConfigField("case_sensitive", "Case Sensitive", "checkbox",
                    required=False, default=False),
        ConfigField("max_iterations", "Max Iterations", "select", required=True,
                    default="3",
                    options=["1", "2", "3", "5"]),
        ConfigField("revision_prompt", "Revision Prompt", "textarea",
                    required=False, default="The previous attempt had issues:\n\n{feedback}\n\nPlease revise your output to address these issues.",
                    hint="Prompt sent to upstream agent on retry. Use {feedback} for the loop node's input, {previous} for the agent's last output, {iteration} for current attempt number."),
        ConfigField("on_max_exceeded", "When Max Exceeded", "select", required=False,
                    default="continue",
                    options=["continue", "fail"],
                    hint="'continue' routes to output_2 with last result; 'fail' marks the pipeline as failed"),
    ],
    color="#ef4444",  # red — attention-worthy control node
))

register_node_type(NodeType(
    type_id="pipeline_generator",
    label="Generate Pipeline",
    category="action",
    description="Convert a structured pipeline spec (JSON) into a saved Drawflow template",
    inputs=1,
    outputs=1,
    config_schema=[
        ConfigField("source_section", "Source Section", "text",
                    required=False, default="pipeline_spec",
                    hint="Extract spec from an upstream Output Parser section. "
                         "Leave empty to use full upstream text."),
        ConfigField("template_name_prefix", "Template Name Prefix", "text",
                    required=False, default="Generated",
                    hint="Prefix for the generated template name. "
                         "Final name: '{prefix}: {spec.name}'"),
    ],
    color="#22d3ee",  # cyan — creation/generative
))
