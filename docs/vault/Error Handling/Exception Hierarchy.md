---
title: Exception Hierarchy
tags: [errors, exceptions]
---

# Exception Hierarchy

File: `taktis/exceptions.py`

Every Taktis failure raises one of these typed exceptions. Raw `Exception` is almost never caught or raised directly — see [[Six Rules]].

## Class tree

```
Exception
└── TaktisError                       # base (exceptions.py:17)
    ├── TaskExecutionError            # task runtime failure
    │                                 # +task_id
    ├── ContextError                  # .taktis/ I/O failure
    │                                 # +path
    ├── DatabaseError                 # query/migration/connection
    │   └── DuplicateError            # UNIQUE constraint
    │                                 # +constraint
    ├── PipelineError                 # pipeline step failure
    │                                 # +step
    ├── SchedulerError                # wave scheduling problem
    ├── StreamingError                # async stream failure
    └── ConsultError                  # advisor chat failure
```

## Extra fields

| Exception | Extra field | Use |
|---|---|---|
| `TaskExecutionError` | `task_id` | Tie failure to a specific task row |
| `ContextError` | `path` | `.taktis/` file path |
| `DuplicateError` | `constraint` | SQL UNIQUE constraint name |
| `PipelineError` | `step` | Pipeline step/node ID |

All inherit a `message` + optional `cause` from `TaktisError` (`exceptions.py:17`).

## Cause chaining

Typed exceptions are raised with `from` so the traceback preserves the underlying cause:

```python
except aiosqlite.Error as exc:
    raise DatabaseError("query failed") from exc
```

Never `raise DatabaseError(str(exc))` — that loses the chain.

## User-facing formatting

`format_error_for_user()` (in `exceptions.py`) produces a human-friendly string for the web UI without leaking internals.

## Related

[[Six Rules]]
