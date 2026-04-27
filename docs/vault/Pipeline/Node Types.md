---
title: Node Types
tags: [pipeline, nodes]
---

# Pipeline Node Types

File: `taktis/core/node_types.py`

Fourteen node types power the Drawflow pipeline editor. Each registers via `register_node_type()` (`node_types.py:57`) into the global `NODE_TYPES` dict (`54`). The UI form is generated from each node's `config_schema` (list of `ConfigField`).

## Contract

Every node is a `NodeType` dataclass (`node_types.py:18–47`):

```python
NodeType(
    type_id: str,
    label: str,
    category: str,
    description: str,
    inputs: int,
    outputs: int,
    config_schema: list[ConfigField],
    color: str,
)
```

`ConfigField` fields: `key`, `label`, `field_type` (`expert_select`, `textarea`, `select`, `text`, `checkbox`), `required`, `default`, `options`, `hint`.

## The 14 nodes

| # | type_id | Lines | Role |
|---|---|---|---|
| 1 | `agent` | 74–127 | Claude task; `standard` or `template` mode, expert persona, retry_policy for transient errors |
| 2 | `output_parser` | 129–145 | Splits upstream text by markers (e.g. `===SECTION===`) so downstream can reference named sections |
| 3 | `file_writer` | 147–169 | Writes upstream result to `.taktis/`; `context_priority` (P0–P4 or "none") controls inclusion in later phases |
| 4 | `plan_applier` | 171–186 | Parses a plan JSON section and creates phases/tasks via [[Planner]]; optional human approval |
| 5 | `conditional` | 188–208 | Two outputs; routes by `contains`/`not_contains`/`regex_match`/`result_is`/`task_failed` |
| 6 | `phase_settings` | 211–229 | Zero-port metadata node: phase name, goal, success criteria, cross-phase `context_files` |
| 7 | `aggregator` | 231–246 | Combines parallel inputs (`concat`, `json_merge`, `numbered_list`, `xml_wrap`) |
| 8 | `human_gate` | 248–265 | Pauses; user approves (output_1) or rejects (output_2) |
| 9 | `api_call` | 267–296 | HTTP fetch (RSS/Atom/webhook) with headers, body template, timeout |
| 10 | `llm_router` | 298–319 | Lightweight classifier → 2–4 branches (`haiku` or `sonnet`) |
| 11 | `text_transform` | 321–344 | Non-LLM ops: `prepend`, `append`, `replace`, `extract_json`, `wrap_xml`, `template` |
| 12 | `fan_out` | 346–381 | Splits upstream into items, runs parallel agent tasks (1-based, max 20 concurrent) |
| 13 | `loop` | 383–412 | Retries upstream agent until condition passes (max 1–5 iterations); revision prompt supports `{feedback}`, `{previous}`, `{iteration}` |
| 14 | `pipeline_generator` | 414–432 | Meta-node: reads structured spec from upstream, runs `spec_to_drawflow()`, saves as new template — see [[Pipeline Factory]] |

## Agent node modes

- **standard** — raw `prompt_text` + optional expert. Upstream context prepended.
- **template** — DB-backed [[Agent Templates]] with `{variable}` mapping + retry logic.

## Instant vs LLM nodes

- **LLM**: `agent`, `llm_router` — actually invoke Claude, cost money.
- **Instant**: `output_parser`, `file_writer`, `plan_applier`, `conditional`, `aggregator`, `text_transform`, `api_call`, `pipeline_generator`, `phase_settings` — no API calls.
- **Interactive**: `human_gate`, and `agent` in interview mode.

## Source section ancestor search

`file_writer`, `plan_applier`, `pipeline_generator` search **all cached results** for their configured `source_section` (e.g. `===PLAN===`), not just their direct upstream. See [[GraphExecutor]] for the ancestor-search mechanism.

## Related

[[GraphExecutor]] · [[Pipeline Factory]] · [[Agent Templates]]
