---
title: SDKProcess
tags: [core, runtime, sdk]
---

# SDKProcess

File: `taktis/core/sdk_process.py`

Every task in Taktis is one `SDKProcess`. It wraps the **Claude Agent SDK** with two execution paths (`sdk_process.py:95–105`):

- **Non-interactive** — `query()` one-shot, defaults to `permission_mode="bypassPermissions"`.
- **Interactive** — `ClaudeSDKClient` + `can_use_tool` callback for real-time tool approval.

No subprocess spawning; everything runs in-process.

## Non-interactive path

`_run_oneshot()` (`sdk_process.py:322–372`) calls `sdk_query(prompt=self._prompt, options=options)`. When a `ResultMessage` arrives, it **breaks immediately** (`343–351`) to avoid SDK stream hangs. `include_partial_messages=True` enables token-by-token streaming to SSE.

## Interactive path

`_run_interactive()` (`sdk_process.py:376–440`) creates `ClaudeSDKClient` and wires `can_use_tool` → `_handle_permission` (`397`). Both `can_use_tool` and `permission_mode` are set **together** (`392–400`) — the workaround for [systemic permission bugs](#permission-workaround) in the SDK.

## Auto-approve read-only tools

`_AUTO_APPROVE_TOOLS = {"Read", "Glob", "Grep"}` at `sdk_process.py:657`. `_handle_permission` resolves these without surfacing UI dialogs (`670–671`).

## AskUserQuestion flow

`_handle_ask_user_question()` at `sdk_process.py:688–704`:

1. Constructs a `_PendingPermission` object.
2. Enqueues an `ask_user_question` event with the `questions` array (`695–699`).
3. Awaits `pending.wait()`.

The UI renders radio buttons from `pending_approval.input.questions[].options`. The frontend calls `/api/tasks/{id}/approve-answers`, which calls `answer_questions()` (`723–736`) — it resolves with a `PermissionResultAllow(updated_input={answers: ...})`.

See [[AskUserQuestion Flow]].

## `===CONFIRMED===` marker

Not handled inside `SDKProcess` itself. It's a **protocol convention**: interactive prompts (`discuss_task`, `simple-interview`, `deep-interview`, `question-asker`) instruct Claude to emit `===CONFIRMED===` when the conversation is truly done. `execution_service.py` runs `_poll_confirmed()` which scans task output for the marker and flips status from `awaiting_input` to `completed`.

## Streaming

- `include_partial_messages=True` in `ClaudeAgentOptions` (`sdk_process.py:330, 384`).
- Output events flow through `_PermissionCallbacks` and the message queue into `stream_output()`, consumed by [[ProcessManager]] `_monitor_output`.
- SSE endpoints in [[Web App]] subscribe via [[EventBus]] and forward to the browser.

## Permission workaround

Both layers are active simultaneously (`sdk_process.py:397–400`):

```python
options.can_use_tool = self._handle_permission
sdk_mode = _PERMISSION_MODE_MAP.get(self._permission_mode or "")
if sdk_mode:
    options.permission_mode = sdk_mode
```

Reason (from memory + comments): `can_use_tool` reliably intercepts network tools; `permission_mode` handles file/edit tools natively. Using either alone leaves gaps. See `project_permission_bugs.md` in user memory.

## Related

[[ProcessManager]] · [[AskUserQuestion Flow]] · [[EventBus]] · [[Engine and Services]]
