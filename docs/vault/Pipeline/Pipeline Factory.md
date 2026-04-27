---
title: Pipeline Factory
tags: [pipeline, meta]
---

# Pipeline Factory

File: `taktis/core/pipeline_factory.py`
Schema reference: `taktis/defaults/PIPELINE_SCHEMA.md`

Two things share this name:

1. **The code module** — converts a structured spec into Drawflow JSON.
2. **The meta-pipeline template** — a two-phase Drawflow pipeline that *uses* the `pipeline_generator` node to create new pipelines.

## `spec_to_drawflow(spec)` (`pipeline_factory.py:158–302`)

Input: dict with `name`, `description`, `nodes` (list). Full schema in `taktis/defaults/PIPELINE_SCHEMA.md:920–973`.

Output: template dict with `name`, `description`, `flow_json` (nested Drawflow structure), `is_default`.

Process:

1. Coerce node IDs to strings.
2. Compute topological layers via Kahn's algorithm (`63–110`).
3. Build bidirectional connection maps.
4. Generate Drawflow nodes with absolute `pos_x` / `pos_y` (pos_x += 350 per layer, pos_y += 180 per node in layer).
5. Port normalization (`113–152`) — alias common LLM mistakes like `"approved"` → `"output_1"`, `"pass"` → `"output_1"`.

## `validate_spec(spec)` (`pipeline_factory.py:309–414`)

Returns list of error strings (empty = valid). Checks:

- `spec` is dict
- `nodes` list non-empty, IDs unique
- Every `type` exists in `NODE_TYPES`
- Connection targets exist; port counts match node outputs
- Required config fields present per node type
- No cycles (calls `_topological_layers()`)

## `validate_drawflow(template)` (`pipeline_factory.py:421–517`)

Structural check on already-generated Drawflow JSON:

- Bidirectional consistency — every output→input has a mirror input←output entry
- Node ID consistency
- Required `data` fields per node type
- Connections reference existing nodes

## `pipeline_generator` node

Not an executor itself. It signals the [[GraphExecutor]] that a spec conversion is needed. The executor calls `validate_spec` → `spec_to_drawflow` → `validate_drawflow` → saves to DB.

Node config fields:
- `source_section` — which `output_parser` section holds the spec (default `pipeline_spec`)
- `template_name_prefix` — final name is `"{prefix}: {spec.name}"`

## Seeded templates

Location: `taktis/defaults/pipeline_templates/`

| File | Role |
|---|---|
| `planning-pipeline.json` | Full planning: depth chooser → conditional → interview → 4 researchers → synthesis → roadmap → verification → apply |
| `pipeline-factory.json` | Meta: interview → domain classification → deep-dive research → architect → `pipeline_generator` |

### Seeding behavior (`db.py:359–448`)

`_seed_pipeline_templates()` runs during `init_db()`:

- **Not in DB** → insert.
- **In DB, `is_default=1`** → compare `flow_json`/`description` and **overwrite if changed** (so prompt fixes propagate).
- **In DB, `is_default=0`** → user-created, never touched.

### Factory reset (`db.py:450–460`)

`factory_reset_pipeline_templates()` deletes all pipeline templates and reseeds from JSON. Exposed as `/api/factory-reset`.

## Related

[[GraphExecutor]] · [[Node Types]] · [[Agent Templates]]
