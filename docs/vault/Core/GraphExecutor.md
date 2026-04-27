---
title: GraphExecutor
tags: [core, runtime, pipeline]
---

# GraphExecutor

File: `taktis/core/graph_executor.py`

Converts a **Drawflow JSON template** into executable waves and runs them using the same infrastructure as [[WaveScheduler]] (ProcessManager, EventBus patterns). Handles both LLM nodes (`agent`, `llm_router`) and instant nodes (`output_parser`, `file_writer`, `plan_applier`, `conditional`, `text_transform`, etc.). See [[Node Types]].

## Entry points

- `execute(template, project)` (`graph_executor.py:270`) — single-module template → single phase.
- `execute_multi(template, project)` (`graph_executor.py:325`) — multi-module template → one phase per module. If the template is a single module without a `phase_settings` node, delegates to `execute()` for backward compat (`338–342`).

## `execute()` flow (`graph_executor.py:270–323`)

1. `parse_drawflow_graph()` (`272`) — normalize nodes and connections.
2. `topological_sort_waves()` (`276`) — Kahn's algorithm, assigns wave numbers.
3. Create a phase with `context_config.designer_phase = True` (`283–289`). This flag tells [[WaveScheduler]] "don't touch the phase lifecycle."
4. Generate a one-shot state summary (`299–306`).
5. `_run_waves()` (`308`) — wave loop, same shape as [[WaveScheduler]].
6. Finalize phase status (`310–320`).

## Conditional skip — three-phase algorithm

`conditional` node routes output_1/output_2 by evaluating upstream text. `_mark_skipped()` at `graph_executor.py:1448–1572`:

1. **Evaluate** condition (`1461–1488`): `contains`, `not_contains`, `regex_match`, `result_is`, `task_failed`.
2. **Mark direct roots** (`1521–1527`): force-skip the immediate downstream targets on the inactive port.
3. **Pruned descent** (`1548–1572`):
   - Collect all descendants of the skipped roots.
   - Iteratively prune candidates that still have an **active** upstream feeder.
   - Remaining nodes get `skipped=True`.

This preserves nodes that are reachable from the *active* branch even if they're also downstream of a skipped node.

## Interactive result extraction

`_get_task_result(task_id)` at `graph_executor.py:2509–2542`:

- Reads the last 50 task outputs (`2519`).
- Scans **backwards** for the last `result` event (`2521–2535`). Interactive tasks emit multiple results; we want the final one.
- Fallback: `task.result_summary` from the DB row (`2542`).

## Context assembly at `create_task`

For LLM nodes, `_create_and_start_llm_node()` (`graph_executor.py:896–1035`):

1. Create the task row in DB first to get a real `task_id` (`988–1000`).
2. Build pipeline context with priority levels (`_build_pipeline_context` at `1114–1178`):
   - **P0 must** — `PROJECT.md`
   - **P1 high** — upstream results (skipping variables already mapped in template mode)
   - **P2 medium** — cross-phase shared files via manifest
   - **P3 low** — DB state summary
3. Write `.taktis/TASK_CONTEXT_{task_id}.md` (`1006`).
4. Build system prompt: expert persona + context file pointer + working-dir note (`1009–1021`).
5. Update task with `system_prompt` and `context_manifest` (`1024–1032`).
6. `ProcessManager.start_task()` (`1034`).

See [[Context Chain]], [[Context Budget]].

## Utility node ancestor search

`file_writer`, `plan_applier`, and `pipeline_generator` reference a `source_section` (e.g., `===PLAN===`). They search **all cached results**, not just the direct upstream:

- file_writer: `graph_executor.py:1353–1365`
- plan_applier: `graph_executor.py:1398–1411`
- pipeline_generator: `graph_executor.py:1694–1708`

Each iterates every result, tries to parse as JSON, and checks for the requested `source_section` key. This lets data flow through intermediate nodes (e.g., `Output Parser → Plan Checker → Apply Plan`) without explicit DAG plumbing.

## Related

[[WaveScheduler]] · [[Node Types]] · [[Pipeline Factory]] · [[Context Budget]]
