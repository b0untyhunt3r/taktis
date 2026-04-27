---
title: AskUserQuestion Flow
tags: [pipeline, interactive, sdk]
---

# AskUserQuestion Flow

Interactive tasks expose radio-button prompts to the user by calling the SDK's `AskUserQuestion` tool. The end of the conversation is signaled with `===CONFIRMED===`.

## The `question-asker` expert

Category: `internal` (therefore `pipeline_internal: true` — hidden from manual task UI).
File: `taktis/experts/question-asker.md`

This expert's prompt instructs Claude to use `AskUserQuestion` with a structured `questions[].options` array, then emit `===CONFIRMED===` once the user is done.

## Intercept in SDKProcess

`_handle_ask_user_question()` (`sdk_process.py:688–704`):

1. Build `_PendingPermission` object
2. Put `ask_user_question` event on the message queue with the questions array (`695–699`)
3. `await pending.wait()`

See [[SDKProcess]].

## UI render

The event flows through [[EventBus]] → [[SSE Architecture]] `/events/task/{id}` → the task detail page. The page reads `pending_approval.input.questions[].options` and renders each question as a radio-button group.

## Answer submission

Frontend POSTs to `/api/tasks/{id}/approve-answers` with the selected `{question_id: option_label}` map. The route calls `approve_tool(updated_input=answers)` on the pending permission, which resolves as `PermissionResultAllow(updated_input={answers: ...})`. Claude sees the answers as if they were the original tool call's output.

## `===CONFIRMED===` completion

Interactive tasks sit in `awaiting_input` after every turn. A background `_poll_confirmed()` task in `execution_service.py` scans task outputs for the marker. When found:

- Status transitions `awaiting_input` → `completed`
- Result captured to `result_summary`
- `.taktis/RESULT_{task_id}.md` written (see [[Context Chain]])

## Where the marker appears

The convention is used in multiple places:

- `taktis/agent_templates/simple-interview.md`
- `taktis/agent_templates/deep-interview.md`
- `taktis/experts/question-asker.md`
- `taktis/core/prompts.py` (`DISCUSS_TASK_PROMPT`)
- `taktis/defaults/PIPELINE_SCHEMA.md` (documentation)

## Related

[[SDKProcess]] · [[Expert System]] · [[SSE Architecture]] · [[Node Types]]
