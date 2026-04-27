# Drawflow Pipeline JSON Schema Reference

This document is the authoritative reference for generating valid pipeline templates.
It is injected into pipeline-generating agents as context.

## Top-Level Structure

```json
{
  "name": "Pipeline Name",
  "description": "What the pipeline does",
  "flow_json": {
    "drawflow": {
      "Home": {
        "data": {
          "1": { /* node */ },
          "2": { /* node */ },
          ...
        }
      }
    }
  },
  "is_default": false
}
```

- Single-phase pipelines use module name `"Home"`
- Multi-phase: each key under `drawflow` is a phase (processed in insertion order)
- `flow_json` can be a JSON string or object

## Node Structure

Every node in `data` is keyed by its string ID:

```json
{
  "1": {
    "id": 1,
    "name": "agent",
    "data": { /* type-specific config — see Node Types below */ },
    "class": "agent",
    "html": "",
    "typenode": false,
    "inputs": {
      "input_1": {
        "connections": [
          { "node": "3", "input": "output_1" }
        ]
      }
    },
    "outputs": {
      "output_1": {
        "connections": [
          { "node": "2", "output": "input_1" }
        ]
      }
    },
    "pos_x": 50,
    "pos_y": 280
  }
}
```

### Rules

- **id** (number) must match the string key
- **name** = the node type identifier (see Node Types)
- **class** = same as name
- **html** = can be empty string `""` (UI rendering only)
- **typenode** = always `false`
- **inputs/outputs**: port objects with connection arrays
- **pos_x/pos_y**: pixel coordinates, origin top-left, flow left-to-right

### Connection Format

Connections are bidirectional references:

```json
// In node "1" outputs:
"output_1": {
  "connections": [
    { "node": "2", "output": "input_1" }
  ]
}

// In node "2" inputs (mirror):
"input_1": {
  "connections": [
    { "node": "1", "input": "output_1" }
  ]
}
```

- Port names: `input_1`, `output_1`, `output_2`, etc.
- **Both ends must declare the connection** (source output AND target input)
- Start nodes: `input_1.connections = []`
- End nodes: `output_1.connections = []`

### Coordinate Guidelines

- Start nodes: pos_x ≈ 50-100
- Flow left-to-right, increment pos_x by ~300-350 per column
- Parallel nodes: same pos_x, spaced ~180px vertically
- Typical canvas: 50-2000 horizontal, 50-800 vertical

---

## Node Types

### 1. agent

Run a Claude task with an expert persona.

**Ports:** 1 input, 1 output

```json
{
  "type_id": "agent",
  "name": "Descriptive Task Name",
  "mode": "standard",
  "model": "sonnet",
  "expert": "expert-slug-name",
  "prompt": "Your task prompt here...",
  "interactive": false,
  "template": "",
  "variable_map": "{}",
  "inject_expert_options": false,
  "inject_description": true,
  "retry_on_pattern": "",
  "max_retries": "0",
  "retry_revision_template": "",
  "retry_revision_prompt": "",
  "retry_transient": true,
  "retry_max_attempts": "2",
  "retry_backoff": "exponential"
}
```

| Field | Values | Notes |
|-------|--------|-------|
| mode | `"standard"`, `"template"` | standard = raw prompt, template = preset |
| model | `"sonnet"`, `"opus"`, `"haiku"` | opus for complex reasoning, haiku for classification |
| expert | expert slug | e.g. `"architect-general"`, `"product-manager"` |
| interactive | boolean | true = pauses for user input, supports AskUserQuestion. **IMPORTANT:** interactive agents must include a confirmation flow in their prompt — present results and ask the user to confirm, then output `===CONFIRMED===` at the very end when approved. Without this marker the task stays in `awaiting_input` forever and blocks the wave. Never mention `===CONFIRMED===` to the user — it is an internal pipeline signal. |
| template | template slug | only when mode="template" |
| variable_map | JSON string | `"{\"var\":\"NodeName\"}"` or `"{\"var\":\"Parser.section\"}"` |
| inject_expert_options | boolean | auto-injects available expert list |
| inject_description | boolean | auto-injects project description |
| retry_on_pattern | string | pattern in result triggers retry (e.g. "NEEDS REVISION") |
| max_retries | `"0"`, `"1"`, `"2"`, `"3"` | for pattern-based retry |
| retry_transient | boolean | auto-retry on transient errors |
| retry_max_attempts | `"1"`, `"2"`, `"3"`, `"5"` | for transient retry |
| retry_backoff | `"none"`, `"linear"`, `"exponential"` | backoff strategy |

**When to use:** Use agent nodes for any task requiring LLM reasoning — writing, analysis, summarization, code generation, decision-making. Agents receive upstream text as context and produce text output. Do NOT use agents to fetch external data (use `api_call` instead). Set `interactive: true` only when you need human input mid-pipeline (interviews, approvals). Use `model: "opus"` for complex multi-step reasoning, `"sonnet"` for standard tasks, `"haiku"` only for very simple tasks. The `expert` field sets the agent's persona — pick one that matches the domain (e.g. `senior-developer` for code tasks, `product-manager` for business analysis).

**Upstream context:** The agent receives concatenated text from all upstream nodes. If an upstream output_parser produced named sections, use `variable_map` to reference specific sections: `{"input": "ParserName.section_name"}`.

### 2. conditional

Route execution based on upstream content.

**Ports:** 1 input, 2 outputs (output_1 = pass, output_2 = fail)

```json
{
  "type_id": "conditional",
  "name": "Check Name",
  "condition_type": "contains",
  "condition_value": "search text",
  "case_sensitive": false
}
```

| condition_type | Behavior |
|----------------|----------|
| `contains` | Upstream text contains value |
| `not_contains` | Upstream does NOT contain value |
| `regex_match` | Python regex matches upstream |
| `result_is` | Trimmed upstream exactly equals value |
| `task_failed` | Any upstream task has failed status (value ignored) |

**When to use:** Use conditional nodes to branch pipeline execution based on upstream output content. Place after an agent or api_call to decide which path to take. Common patterns: check if a quality threshold was met (`contains` + "APPROVED"), verify data was fetched successfully (`not_contains` + "API_CALL_FAILED"), or test for specific flags in structured output. The `output_1` path runs when the condition passes; `output_2` runs when it fails. Downstream nodes on the skipped path are automatically marked as skipped.

### 3. llm_router

Classify input and route to 2-4 branches using a lightweight LLM.

**Ports:** 1 input, 4 outputs (output_1 through output_4)

```json
{
  "type_id": "llm_router",
  "name": "Route Name",
  "routing_prompt": "Classify into one category:\n- Route 1: ...\n- Route 2: ...\nRespond with ONLY the route number.",
  "model": "haiku",
  "route_count": "2"
}
```

| Field | Values |
|-------|--------|
| model | `"haiku"`, `"sonnet"` |
| route_count | `"2"`, `"3"`, `"4"` |

**When to use:** Use llm_router when you need to classify input into 2-4 categories and route to different processing sub-pipelines. The routing_prompt must instruct the LLM to respond with ONLY the route number. Use `model: "haiku"` for fast, cheap classification. Connect each output port to a different downstream path. Only ONE route fires per execution — nodes on other routes are skipped. Good for: domain classification, urgency triage, content type detection.

### 4. fan_out

Split upstream into items and run parallel agent tasks.

**Ports:** 1 input, 1 output

```json
{
  "type_id": "fan_out",
  "name": "Parallel Name",
  "split_mode": "json_array",
  "delimiter": "---",
  "expert": "expert-slug",
  "prompt_template": "Process: {item}",
  "model": "sonnet",
  "max_parallel": "10",
  "merge_strategy": "numbered",
  "merge_separator": "\n\n---\n\n"
}
```

| Field | Values | Notes |
|-------|--------|-------|
| split_mode | `"newline"`, `"delimiter"`, `"numbered_list"`, `"json_array"` | How to split |
| prompt_template | string | Use `{item}`, `{index}` (1-based), `{total}` |
| max_parallel | `"3"`, `"5"`, `"10"`, `"15"`, `"20"` | Concurrency limit |
| merge_strategy | `"concat"`, `"numbered"`, `"json_array"` | How to combine results |

**When to use:** Use fan_out when you have a LIST of items that each need the same agent processing. Example: a list of 10 news articles that each need summarization. The node splits the upstream text into items (by newlines, delimiters, JSON array), runs a parallel agent task for each item with the `prompt_template`, then merges all results. Do NOT use fan_out when you have a fixed number of different sources — use separate parallel nodes instead (e.g. 5 api_call nodes, not one fan_out).

### 5. loop

Retry upstream agent until a condition passes.

**Ports:** 1 input, 2 outputs (output_1 = pass/exit, output_2 = max exceeded)

```json
{
  "type_id": "loop",
  "name": "Review Loop",
  "condition_type": "not_contains",
  "condition_value": "CRITICAL",
  "case_sensitive": false,
  "max_iterations": "3",
  "revision_prompt": "Issues found:\n\n{feedback}\n\nRevise to fix these.",
  "on_max_exceeded": "continue"
}
```

| Field | Values | Notes |
|-------|--------|-------|
| condition_type | `"contains"`, `"not_contains"`, `"regex_match"` | TRUE = exit loop |
| max_iterations | `"1"`, `"2"`, `"3"`, `"5"` | |
| revision_prompt | string | Use `{feedback}`, `{previous}`, `{iteration}` |
| on_max_exceeded | `"continue"`, `"fail"` | continue → output_2, fail → pipeline fails |

**When to use:** Use loop for quality-gate patterns where an agent's output must meet a standard. Example: agent writes code → loop checks for "CRITICAL" issues → if found, sends revision prompt back to the agent. The loop replaces the DAG cycle pattern — you never create back-edges in the graph. The condition determines when to EXIT the loop (e.g. `not_contains` + "CRITICAL" exits when there are no critical issues). `output_1` fires on successful exit; `output_2` fires when max_iterations is reached.

### 6. human_gate

Pause for user approval/rejection.

**Ports:** 1 input, 2 outputs (output_1 = approved, output_2 = rejected)

```json
{
  "type_id": "human_gate",
  "name": "Gate Name",
  "gate_message": "Review and approve to continue.",
  "show_upstream": true
}
```

**When to use:** Use human_gate when the user wants a manual checkpoint before the pipeline continues. The user sees the upstream result and the gate_message, then clicks Approve or Reject. Approved → `output_1` path continues. Rejected → `output_2` path (or pipeline stops if nothing is connected). Good for: reviewing generated plans before execution, approving expensive operations, quality checkpoints on critical artifacts.

### 7. output_parser

Split text into named sections using markers. Instant (no LLM).

**Ports:** 1 input, 1 output

```json
{
  "type_id": "output_parser",
  "name": "Parser Name",
  "markers": "===SECTION_A===\n===SECTION_B===",
  "section_names": "section_a\nsection_b"
}
```

- Markers and section_names are newline-separated, must have equal count
- Downstream nodes access sections via variable_map: `"Parser Name.section_a"`

**When to use:** Use output_parser when an agent produces structured output with multiple named sections (using `===MARKER===` delimiters). This lets downstream nodes (file_writer, pipeline_generator) extract specific sections. Example: an architect outputs `===RATIONALE===` and `===PIPELINE_SPEC===` sections — the parser splits them so one file_writer saves the rationale and another saves the spec. Always pair with an agent that has explicit marker instructions in its prompt.

### 8. aggregator

Combine multiple upstream outputs.

**Ports:** 1 input (multiple connections), 1 output

```json
{
  "type_id": "aggregator",
  "name": "Merge Name",
  "strategy": "numbered_list",
  "separator": "\n\n---\n\n"
}
```

| strategy | Behavior |
|----------|----------|
| `concat` | Join with separator |
| `json_merge` | Merge JSON dicts |
| `numbered_list` | `1. [NodeName]\ntext\n\n2. ...` |
| `xml_wrap` | `<from node="NodeName">text</from>` |

**When to use:** Use aggregator to merge outputs from multiple parallel nodes into a single text. Connect all parallel nodes' outputs to the aggregator's single input port. Strategy choice: `numbered_list` is best for human-readable merged output (agent consumption), `xml_wrap` is best when you need the agent to distinguish which source produced which text, `json_merge` for structured data, `concat` for simple joining. Place an aggregator after any set of parallel nodes before feeding into a downstream agent or file_writer.

### 9. text_transform

Transform text without an LLM call. Instant.

**Ports:** 1 input, 1 output

```json
{
  "type_id": "text_transform",
  "name": "Transform Name",
  "operation": "wrap_xml",
  "text": "tag_name",
  "find_pattern": "",
  "use_regex": false
}
```

| operation | text field meaning |
|-----------|-------------------|
| `prepend` | Text to prepend before upstream |
| `append` | Text to append after upstream |
| `replace` | Replacement text (find_pattern = what to find) |
| `extract_json` | (ignored) Extracts JSON from fenced blocks |
| `wrap_xml` | XML tag name → `<tag>upstream</tag>` |
| `template` | Template with `{upstream}` placeholder |

**When to use:** Use text_transform for simple, deterministic text operations without burning LLM tokens. Good for: wrapping data in XML tags before feeding to an agent (`wrap_xml`), adding headers/footers to output (`prepend`/`append`), extracting JSON from markdown code fences (`extract_json`), or reformatting text with a template (`template` + `{upstream}`). Executes instantly — no LLM call, no cost.

### 10. file_writer

Write upstream result to disk. Instant.

**Ports:** 1 input, 1 output

```json
{
  "type_id": "file_writer",
  "name": "Write Name",
  "filename": "REQUIREMENTS.md",
  "source_section": "",
  "context_priority": "P2 — medium"
}
```

| Field | Notes |
|-------|-------|
| filename | Path under `.taktis/` (e.g. `"research/STACK.md"`) |
| source_section | Extract named section from upstream output_parser (empty = full text) |
| context_priority | `"none"`, `"P0 — must include"`, `"P1 — high"`, `"P2 — medium"`, `"P3 — low"`, `"P4 — trim first"` |

**When to use:** Use file_writer to persist any important result to disk. Files are written to the project's `.taktis/` directory. Set `context_priority` to make the file available to downstream phases in multi-phase pipelines (P0 = always included, P4 = included if budget allows, "none" = not injected as context). Use `source_section` to extract a specific section from an upstream output_parser instead of writing the full text. Every pipeline should end with at least one file_writer to save its final output.

### 11. plan_applier

Parse JSON plan and create DB phases/tasks. Instant.

**Ports:** 1 input, 1 output

```json
{
  "type_id": "plan_applier",
  "name": "Apply Plan",
  "await_approval": true,
  "source_section": "plan"
}
```

| Field | Notes |
|-------|-------|
| await_approval | Pause for user approval before applying |
| source_section | Extract from upstream output_parser section |

**When to use:** Use plan_applier in pipelines that generate project plans (JSON with phases/tasks). The plan_applier parses the JSON, creates real DB phases and tasks, and optionally waits for user approval first. This makes the pipeline self-extending — it can spawn new work. Only use in planning-oriented pipelines, not in data processing pipelines.

### 12. api_call

HTTP request to external URL. Instant.

**Ports:** 1 input, 1 output

```json
{
  "type_id": "api_call",
  "name": "API Name",
  "url": "https://api.example.com/endpoint",
  "method": "POST",
  "content_type": "application/json",
  "headers": "{}",
  "body_template": "{\"data\": \"{upstream}\"}",
  "timeout_seconds": "30"
}
```

| Field | Values |
|-------|--------|
| method | `"GET"`, `"POST"`, `"PUT"`, `"PATCH"`, `"DELETE"` |
| content_type | `"application/json"`, `"text/plain"`, `"application/x-www-form-urlencoded"` |
| body_template | Use `{upstream}` for upstream text. Empty = send upstream as-is |
| timeout_seconds | `"10"`, `"30"`, `"60"`, `"120"` |

**RSS/Atom Feed Support:**
The api_call node natively handles RSS (`application/rss+xml`) and Atom (`application/atom+xml`) feeds.
Use `method: "GET"` with the feed URL and leave `body_template` empty. The raw XML response is
stored as text and passed downstream. Use an agent or text_transform node to parse/summarize the XML content.

**Example — Fetch an RSS news feed:**
```json
{
  "type_id": "api_call",
  "name": "Fetch BBC News",
  "url": "https://feeds.bbci.co.uk/news/rss.xml",
  "method": "GET",
  "content_type": "text/plain",
  "headers": "{}",
  "body_template": "",
  "timeout_seconds": "30"
}
```

**Example — POST to a webhook:**
```json
{
  "type_id": "api_call",
  "name": "Send to Slack",
  "url": "https://hooks.slack.com/services/T.../B.../xxx",
  "method": "POST",
  "content_type": "application/json",
  "headers": "{}",
  "body_template": "{\"text\": \"{upstream}\"}",
  "timeout_seconds": "30"
}
```

**Response behavior:**
- Text responses (JSON, XML, HTML, RSS, Atom) are stored verbatim, up to 50 KB.
- Responses over 50 KB are truncated with a `[TRUNCATED]` marker.
- Binary responses return `[Binary response: content-type, size]`.
- Failures return `[API_CALL_FAILED: reason]`.
- **GET requests:** leave `body_template` empty — no request body is sent.
- **POST/PUT/PATCH:** if `body_template` is empty, upstream text is sent as the body.
- Use `{upstream}` placeholder in `body_template` to embed upstream node results in a custom body.
- No authentication headers? Set `headers` to `"{}"`. Need an API key? Use `{"Authorization": "Bearer YOUR_KEY"}` or `{"X-API-Key": "YOUR_KEY"}`.

**When to use:** Use api_call for ALL external data fetching — RSS feeds, REST APIs, webhooks, health checks. Agents CANNOT make HTTP requests; only api_call nodes can reach external URLs. For fetching multiple URLs in parallel, create one api_call node per URL with no upstream connections — they will all execute in the same wave (parallel). Then connect them all to a single aggregator node.

### 13. phase_settings

Configure phase metadata (multi-phase pipelines only). No execution.

**Ports:** 0 inputs, 0 outputs

```json
{
  "type_id": "phase_settings",
  "name": "Phase Settings",
  "phase_name": "Discovery Phase",
  "phase_goal": "Understand requirements",
  "success_criteria": "Requirements documented\nStakeholders identified",
  "context_files": ""
}
```

**When to use:** Only in multi-phase pipeline templates. Each Drawflow module (phase) should have exactly one phase_settings node. It sets the phase name, goal, success criteria, and which context files from prior phases to include. Not used in single-phase pipelines generated by the pipeline factory.

### 14. pipeline_generator

Convert a structured pipeline spec into a saved Drawflow template. Instant.

**Ports:** 1 input, 1 output

```json
{
  "type_id": "pipeline_generator",
  "name": "Create Pipeline Template",
  "source_section": "pipeline_spec",
  "template_name_prefix": "Generated"
}
```

| Field | Notes |
|-------|-------|
| source_section | Extract spec from upstream output_parser section (empty = full upstream text) |
| template_name_prefix | Prefix for saved template name: `"{prefix}: {spec.name}"` |

The upstream must be a valid pipeline spec JSON (see "Structured Pipeline Specification Format" below).

**When to use:** Only in meta-pipelines (like the Pipeline Factory) that generate other pipelines. The pipeline_generator takes a structured pipeline spec JSON, validates it, converts it to Drawflow format, and saves it as a new template in the database. End users never need this node — it's for pipeline-building pipelines.

---

## Available Expert Slugs (184 total, 18 categories)

### Internal (pipeline-only, 13)
`interviewer`, `synthesizer`, `roadmapper`, `plan-checker`, `question-asker`, `reality-checker`, `architecture-researcher`, `features-researcher`, `pitfalls-researcher`, `stack-researcher`, `task-advisor`, `task-discusser`, `task-researcher`

### Architecture (1)
`architect-general`

### Implementation (3)
`implementer-general`, `docs-writer-general`, `refactorer`

### Review (3)
`reviewer-general`, `accessibility-reviewer`, `security-reviewer-general`

### Testing (9)
`api-tester`, `evidence-collector`, `model-qa-specialist`, `performance-benchmarker`, `qa-lead`, `threat-detection-engineer`, `workflow-optimizer`, `accessibility-auditor`

### Engineering (26)
`ai-data-remediation-engineer`, `ai-engineer`, `autonomous-optimization-architect`, `backend-architect`, `blockchain-security-auditor`, `code-reviewer`, `data-consolidation-agent`, `data-engineer`, `database-optimizer`, `devops-automator`, `email-intelligence-engineer`, `embedded-firmware-engineer`, `feishu-integration-developer`, `filament-optimizer`, `frontend-developer`, `incident-response-commander`, `lsp-index-engineer`, `macos-spatial-metal-engineer`, `mcp-builder`, `rapid-prototyper`, `security-engineer`, `senior-developer`, `solidity-smart-contract-engineer`, `sre-site-reliability-engineer`, `tech-lead`, `terminal-integration-specialist`

### DevOps (1)
`devops`

### Marketing (29)
`ai-citation-strategist`, `app-store-optimizer`, `baidu-seo-specialist`, `carousel-growth-engine`, `china-e-commerce-operator`, `china-market-localization-strategist`, `content-creator`, `cross-border-ecommerce-specialist`, `douyin-marketing-strategist`, `growth-hacker`, `instagram-curator`, `kuaishou-strategist`, `linkedin-strategist`, `livestream-commerce-coach`, `pdd-temu-marketplace-strategist`, `podcast-strategist`, `private-domain-operator`, `reddit-strategist`, `seo-specialist`, `social-media-strategist`, `tiktok-strategist`, `twitter-strategist`, `wechat-ecosystem-strategist`, `xiaohongshu-strategist`, `zhihu-strategist`

### Sales (8)
`account-strategist`, `deal-strategist`, `discovery-coach`, `outbound-strategist`, `pipeline-analyst`, `recruitment-specialist`, `sales-coach`, `sales-engineer`

### Design (8)
`brand-guardian`, `image-prompt-engineer`, `inclusive-visuals-specialist`, `ui-designer`, `ux-architect`, `ux-researcher`, `visual-storyteller`

### Product (5)
`behavioral-nudge-engine`, `feedback-synthesizer`, `product-manager`, `product-trend-researcher`, `sprint-prioritizer`

### Paid Media (7)
`ad-creative-strategist`, `paid-media-auditor`, `paid-social-strategist`, `ppc-campaign-strategist`, `programmatic-display-buyer`, `search-query-analyst`, `video-optimization-specialist`

### Project Management (6)
`experiment-tracker`, `project-advisor`, `project-shepherd`, `senior-project-manager`, `studio-operations`, `studio-producer`

### Academic (5)
`anthropologist`, `geographer`, `historian`, `narratologist`, `psychologist`

### Game Dev (20)
`game-audio-engineer`, `game-designer`, `godot-gameplay-scripter`, `godot-multiplayer-engineer`, `godot-shader-developer`, `level-designer`, `narrative-designer`, `roblox-avatar-creator`, `roblox-experience-designer`, `roblox-systems-scripter`, `technical-artist`, `unity-architect`, `unity-editor-tool-developer`, `unity-multiplayer-engineer`, `unity-shader-graph-artist`, `unreal-multiplayer-architect`, `unreal-systems-engineer`, `unreal-technical-artist`, `unreal-world-builder`, `ux-architect`

### Specialized (28)
`accounts-payable-agent`, `agentic-identity-trust-architect`, `agents-orchestrator`, `automation-governance-architect`, `bilibili-content-strategist`, `blender-add-on-engineer`, `civil-engineer`, `compliance-auditor`, `corporate-training-designer`, `developer-advocate`, `french-consulting-navigator`, `government-digital-presales`, `korean-business-navigator`, `legal-compliance-checker`, `salesforce-architect`, `study-abroad-advisor`, `supply-chain-strategist`, `tool-evaluator`, `zk-steward`

### Spatial Computing (6)
`visionos-spatial-engineer`, `xr-cockpit-interaction-specialist`, `xr-immersive-developer`, `xr-interface-architect`

### Support (6)
`analytics-reporter`, `customer-success-agent`, `finance-tracker`, `infrastructure-maintainer`, `sales-data-extraction-agent`, `support-responder`

---

## Design Patterns

### Parallel Perspectives
Fan out same input to N agents with different experts → Aggregator.

### Adversarial Loop
Agent generates → Reviewer evaluates → Loop retries if quality fails.

### Classify and Route
LLM Router classifies → routes to specialized sub-pipelines.

### Self-Spawning
Agent outputs structured JSON → Plan Applier creates new phases/tasks.

### Gated Escalation
Agent produces result → Conditional checks quality → Human Gate for edge cases.

### Research-Synthesize-Act
Fan out researchers → Aggregator → Synthesizer agent → Action node.

### External Data Aggregation
Multiple api_call nodes fetch from different URLs in parallel → Aggregator combines → Agent summarizes/transforms → File Writer saves output.

---

## Common Pipeline Patterns — Complete Examples

These are full pipeline specification examples showing common patterns. Use them as
starting points when designing pipelines.

### Pattern: Parallel RSS/API Data Aggregation

Fetch data from multiple external sources in parallel, aggregate results, then
have an agent summarize and write to file.

```json
{
  "name": "News Digest Pipeline",
  "description": "Fetches RSS feeds from 5 news sources, aggregates them, and produces a daily summary",
  "nodes": [
    {
      "id": "1",
      "type": "api_call",
      "name": "Fetch BBC News",
      "config": {
        "url": "https://feeds.bbci.co.uk/news/rss.xml",
        "method": "GET",
        "content_type": "text/plain",
        "headers": "{}",
        "body_template": "",
        "timeout_seconds": "30"
      },
      "connections_to": ["6"]
    },
    {
      "id": "2",
      "type": "api_call",
      "name": "Fetch Reuters",
      "config": {
        "url": "https://www.reutersagency.com/feed/",
        "method": "GET",
        "content_type": "text/plain",
        "headers": "{}",
        "body_template": "",
        "timeout_seconds": "30"
      },
      "connections_to": ["6"]
    },
    {
      "id": "3",
      "type": "api_call",
      "name": "Fetch NPR News",
      "config": {
        "url": "https://feeds.npr.org/1001/rss.xml",
        "method": "GET",
        "content_type": "text/plain",
        "headers": "{}",
        "body_template": "",
        "timeout_seconds": "30"
      },
      "connections_to": ["6"]
    },
    {
      "id": "4",
      "type": "api_call",
      "name": "Fetch Al Jazeera",
      "config": {
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "method": "GET",
        "content_type": "text/plain",
        "headers": "{}",
        "body_template": "",
        "timeout_seconds": "30"
      },
      "connections_to": ["6"]
    },
    {
      "id": "5",
      "type": "api_call",
      "name": "Fetch Associated Press",
      "config": {
        "url": "https://rsshub.app/apnews/topics/apf-topnews",
        "method": "GET",
        "content_type": "text/plain",
        "headers": "{}",
        "body_template": "",
        "timeout_seconds": "30"
      },
      "connections_to": ["6"]
    },
    {
      "id": "6",
      "type": "aggregator",
      "name": "Combine All Feeds",
      "config": {
        "strategy": "xml_wrap",
        "separator": ""
      },
      "connections_to": ["7"]
    },
    {
      "id": "7",
      "type": "agent",
      "name": "News Summarizer",
      "config": {
        "mode": "standard",
        "model": "sonnet",
        "expert": "senior-developer",
        "prompt": "You have received raw RSS/XML feeds from 5 major news sources. Parse the XML items/entries from each feed, identify the top 10 most important and diverse news stories across all sources. For each story produce: a headline, a 2-3 sentence summary, the source name, and the publication date. Output as a clean markdown document with a date header and numbered stories. Focus on world news, major events, and significant developments. Avoid duplicate stories — if multiple sources cover the same event, merge them into one entry citing all sources.",
        "interactive": false
      },
      "connections_to": ["8"]
    },
    {
      "id": "8",
      "type": "file_writer",
      "name": "Write Daily Summary",
      "config": {
        "filename": "DAILY_NEWS.md",
        "source_section": "",
        "context_priority": "none"
      },
      "connections_to": []
    }
  ]
}
```

**Key design points:**
- Nodes 1-5 have no upstream connections, so they execute in wave 1 (parallel)
- Aggregator (6) waits for all 5 feeds, then combines with `xml_wrap` strategy
- Agent (7) receives the combined XML and produces a human-readable summary
- File writer (8) saves the final output
- No authentication needed — all feeds are public RSS

### Pattern: Research and Synthesize

Multiple research agents with different perspectives, aggregated and synthesized.

```json
{
  "name": "Multi-Perspective Research Pipeline",
  "description": "Three researchers analyze a topic from different angles, then a synthesizer merges findings",
  "nodes": [
    {
      "id": "1",
      "type": "agent",
      "name": "Technical Researcher",
      "config": {
        "mode": "standard",
        "model": "sonnet",
        "expert": "senior-developer",
        "prompt": "Research the technical aspects of the topic provided in context. Focus on implementation details, technology choices, and technical feasibility. Write a structured analysis.",
        "interactive": false
      },
      "connections_to": ["4"]
    },
    {
      "id": "2",
      "type": "agent",
      "name": "Market Researcher",
      "config": {
        "mode": "standard",
        "model": "sonnet",
        "expert": "market-analyst",
        "prompt": "Research the market and business aspects of the topic provided in context. Focus on market size, competition, trends, and business viability. Write a structured analysis.",
        "interactive": false
      },
      "connections_to": ["4"]
    },
    {
      "id": "3",
      "type": "agent",
      "name": "Risk Researcher",
      "config": {
        "mode": "standard",
        "model": "sonnet",
        "expert": "reality-checker",
        "prompt": "Analyze the risks and challenges of the topic provided in context. Focus on technical risks, market risks, execution risks, and mitigation strategies. Write a structured analysis.",
        "interactive": false
      },
      "connections_to": ["4"]
    },
    {
      "id": "4",
      "type": "aggregator",
      "name": "Merge Research",
      "config": {
        "strategy": "numbered_list",
        "separator": ""
      },
      "connections_to": ["5"]
    },
    {
      "id": "5",
      "type": "agent",
      "name": "Synthesizer",
      "config": {
        "mode": "standard",
        "model": "opus",
        "expert": "architect-general",
        "prompt": "You have received three research analyses covering technical, market, and risk perspectives. Synthesize them into a single comprehensive report with sections: Executive Summary, Key Findings, Recommendations, and Risk Mitigation Plan. Resolve any contradictions between the perspectives.",
        "interactive": false
      },
      "connections_to": ["6"]
    },
    {
      "id": "6",
      "type": "file_writer",
      "name": "Write Report",
      "config": {
        "filename": "RESEARCH_REPORT.md",
        "source_section": "",
        "context_priority": "none"
      },
      "connections_to": []
    }
  ]
}
```

### Pattern: Fetch, Classify, Route

Fetch external data, then classify and route to different processing paths.

```json
{
  "name": "Classify and Route Pipeline",
  "description": "Fetches data, classifies it, routes to specialized handlers",
  "nodes": [
    {
      "id": "1",
      "type": "api_call",
      "name": "Fetch Data",
      "config": {
        "url": "https://api.example.com/data",
        "method": "GET",
        "content_type": "text/plain",
        "headers": "{}",
        "body_template": "",
        "timeout_seconds": "30"
      },
      "connections_to": ["2"]
    },
    {
      "id": "2",
      "type": "llm_router",
      "name": "Classify Content",
      "config": {
        "routing_prompt": "Classify the content: Route 1 = urgent action needed, Route 2 = informational, Route 3 = requires review, Route 4 = archive. Respond with ONLY the route number.",
        "model": "haiku",
        "route_count": "4"
      },
      "connections_to": {
        "output_1": ["3"],
        "output_2": ["4"],
        "output_3": ["5"],
        "output_4": ["6"]
      }
    },
    {
      "id": "3",
      "type": "agent",
      "name": "Urgent Handler",
      "config": {
        "mode": "standard",
        "model": "sonnet",
        "expert": "senior-developer",
        "prompt": "This content has been classified as urgent. Analyze it and produce an action plan with immediate next steps.",
        "interactive": false
      },
      "connections_to": ["7"]
    },
    {
      "id": "4",
      "type": "file_writer",
      "name": "Archive Info",
      "config": {
        "filename": "INFO_LOG.md",
        "source_section": "",
        "context_priority": "none"
      },
      "connections_to": []
    },
    {
      "id": "5",
      "type": "agent",
      "name": "Review Handler",
      "config": {
        "mode": "standard",
        "model": "sonnet",
        "expert": "reviewer-general",
        "prompt": "This content needs review. Analyze it and produce a review summary with recommendations.",
        "interactive": false
      },
      "connections_to": ["7"]
    },
    {
      "id": "6",
      "type": "file_writer",
      "name": "Archive Content",
      "config": {
        "filename": "ARCHIVE.md",
        "source_section": "",
        "context_priority": "none"
      },
      "connections_to": []
    },
    {
      "id": "7",
      "type": "file_writer",
      "name": "Write Actions",
      "config": {
        "filename": "ACTIONS.md",
        "source_section": "",
        "context_priority": "none"
      },
      "connections_to": []
    }
  ]
}
```

---

## Structured Pipeline Specification Format

When an architect agent designs a pipeline, it should output this JSON format
which can be programmatically converted to Drawflow JSON:

```json
{
  "name": "Pipeline Name",
  "description": "What it does",
  "nodes": [
    {
      "id": "1",
      "type": "agent",
      "name": "Interview User",
      "config": {
        "mode": "standard",
        "model": "sonnet",
        "expert": "question-asker",
        "prompt": "Interview the user about...",
        "interactive": true
      },
      "connections_to": ["2", "3"]
    },
    {
      "id": "2",
      "type": "conditional",
      "name": "Check Depth",
      "config": {
        "condition_type": "contains",
        "condition_value": "deep",
        "case_sensitive": false
      },
      "connections_to": {
        "output_1": ["4"],
        "output_2": ["5"]
      }
    }
  ]
}
```

The `connections_to` field can be:
- A flat array `["2", "3"]` — all connect via output_1
- An object `{"output_1": ["4"], "output_2": ["5"]}` — per-port connections

**IMPORTANT: Port names MUST use `output_1`, `output_2`, etc. format.** Do NOT use semantic names like "approved", "rejected", "pass", "fail". Multi-output nodes:
- conditional: `output_1` = condition passed, `output_2` = condition failed
- human_gate: `output_1` = approved, `output_2` = rejected
- loop: `output_1` = condition passed (exit), `output_2` = max iterations exceeded
- llm_router: `output_1` through `output_4` for routes 1-4

**IMPORTANT: The graph must be acyclic (DAG).** Do NOT create cycles where node A connects to B and B connects back to A. Use the `loop` node type for retry/revision cycles instead — it handles iteration internally.

This simplified format is converted to full Drawflow JSON by the pipeline factory code.
