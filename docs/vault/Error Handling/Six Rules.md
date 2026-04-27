---
title: Six Error-Handling Rules
tags: [errors, rules, policy]
---

# Six Error-Handling Rules

Source: `docs/ERROR_HANDLING.md` (169 lines, worth reading in full)

These rules define how Taktis handles failures. Every PR touching error paths is expected to conform.

## Rule 1 — Use the typed hierarchy (`ERROR_HANDLING.md:7–47`)

Raise from the [[Exception Hierarchy]]. Chain causes with `from exc`. Format for the UI with `format_error_for_user()` — never expose raw tracebacks.

## Rule 2 — Never swallow silently (`ERROR_HANDLING.md:62–88`)

`except:` with a bare `pass` is forbidden. Minimum response: log `WARNING`. Preferred: re-raise as a typed [[Exception Hierarchy]] error. This is why you can grep the codebase for `except` and see very few empty blocks.

## Rule 3 — All `asyncio.create_task` gets a done-callback (`ERROR_HANDLING.md:93–119`)

Use `make_done_callback()` factory. The callback publishes `EVENT_SYSTEM_CRASHED` to [[EventBus]] on unhandled failure, so background task crashes surface in the UI instead of vanishing.

See `ExecutionService._make_task_done_callback` (`execution_service.py:102`).

## Rule 4 — File I/O in try/except (`ERROR_HANDLING.md:123–144`)

**Writes** — wrap and raise `ContextError`. Fail-fast.

**Reads** — use `_safe_read()`. Degrade gracefully: log `WARNING`, skip the file, continue with the context you do have.

## Rule 5 — Route-level errors produce error HTML (`ERROR_HANDLING.md:149–153`)

htmx endpoints return `<div class="error">...</div>` with status 400 on validation errors. 500s are handled by the global Starlette handler.

## Rule 6 — Fail-fast vs notify-and-continue (`ERROR_HANDLING.md:157–168`)

| Operation | Policy | Why |
|---|---|---|
| DB startup migration | **Fail-fast** | Schema must be correct |
| `write_task_result` | **Fail-fast** | Losing a result is losing work |
| Phase review step failure | **Fail-fast** | [[Phase Review]] depends on review.md |
| Scheduler exception | **Fail-fast** | Corrupted schedule state |
| Read single context file | **Continue** | Graceful degradation — `WARNING`, skip |
| [[SDKProcess]] streaming error | **Continue** | Put error on queue, set `exit=1`, let scheduler decide |
| Background worker crash | **Continue** | Rule 3 — callback publishes `EVENT_SYSTEM_CRASHED` |

## Related

[[Exception Hierarchy]] · [[EventBus]] · [[Engine and Services]]
