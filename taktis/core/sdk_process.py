"""Wraps the Claude Agent SDK for all task execution."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

# Result text patterns that indicate an error rather than a normal agent
# response. The SDK surfaces these as a ResultMessage with is_error=True
# whose ``result`` field contains the raw error text. We treat them as
# task failures so retry/review logic can kick in instead of silently
# accepting the error text as the task's final output.
#
# ERROR_TYPE_* constants are classifiers returned by ``_classify_result_error``
# so callers (retry policy, UI labels, logs) can distinguish kinds of
# failures: transport 5xx errors are usually transient and safe to retry
# after 30 min; USAGE_LIMIT errors mean the subscription window is
# exhausted and retrying is pointless until the stated reset time.
ERROR_TYPE_API = "api_error"               # "API Error: 500 ..."
ERROR_TYPE_PROCESS_EXIT = "process_exit"   # "Claude Code process exited with code N"
ERROR_TYPE_USAGE_LIMIT = "usage_limit"     # "You've hit your limit · resets ..."
ERROR_TYPE_CONTEXT_OVERFLOW = "context_overflow"   # "Prompt is too long"

_ERROR_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (ERROR_TYPE_API,
     re.compile(r"^\s*API Error:\s*\d+", re.IGNORECASE)),
    (ERROR_TYPE_PROCESS_EXIT,
     re.compile(r"^\s*Claude Code process exited with code\s*\d+", re.IGNORECASE)),
    (ERROR_TYPE_USAGE_LIMIT,
     re.compile(r"(you'?ve hit your limit|usage limit|limit\s*·\s*resets)", re.IGNORECASE)),
    (ERROR_TYPE_CONTEXT_OVERFLOW,
     re.compile(r"^\s*(prompt is too long|context length exceeded|input length exceeds)", re.IGNORECASE)),
]


def _classify_result_error(event: dict[str, Any]) -> str | None:
    """Return the error-type classifier for a result event, or None if clean.

    Order of checks:
    1. Match the result text against each known error pattern — this gives
       the most specific label.
    2. Fall back to a generic ``"is_error"`` label when the SDK set the
       is_error flag but the text doesn't match any known pattern.
    """
    text = event.get("result") or ""
    if isinstance(text, str):
        for label, pattern in _ERROR_PATTERNS:
            if pattern.search(text):
                return label
    if event.get("is_error"):
        return "is_error"
    return None


def _result_event_is_error(event: dict[str, Any]) -> bool:
    """Return True if a ``result`` event represents an error of any kind."""
    return _classify_result_error(event) is not None

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    query as sdk_query,
)
from claude_agent_sdk.types import (
    HookMatcher,
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from taktis.exceptions import StreamingError, TaskExecutionError

# Load all setting sources so installed plugins/skills are available in SDK sessions.
# Without this, the SDK passes ``--setting-sources ""`` which disables user config.
_SETTING_SOURCES: list[str] = ["user", "project", "local"]

logger = logging.getLogger(__name__)

# Map Taktis permission modes to valid Claude Code --permission-mode values.
# SDK accepts: "default", "acceptEdits", "plan", "bypassPermissions".
_PERMISSION_MODE_MAP: dict[str, PermissionMode] = {
    "auto": "bypassPermissions",
    "bypassPermissions": "bypassPermissions",
    "plan": "plan",
    "acceptEdits": "acceptEdits",
    "default": "default",
}


class SDKProcess:
    """Wraps the Claude Agent SDK for task execution.

    Two modes:
    - **Non-interactive** (``interactive=False``): Uses ``query()`` one-shot.
      Runs to completion autonomously with ``permission_mode`` controlling
      tool access (typically ``bypassPermissions``).
    - **Interactive** (``interactive=True``): Uses ``ClaudeSDKClient`` for
      multi-turn conversations.  Tool approval requests surface via the
      ``can_use_tool`` callback so the user can approve/deny from the UI.
    """

    # Timeouts for queue overflow protection (seconds).
    # Class-level so tests can override on individual instances.
    ENQUEUE_TIMEOUT: float = 5.0
    EOF_TIMEOUT: float = 30.0

    def __init__(
        self,
        task_id: str,
        prompt: str,
        working_dir: str,
        model: str = "sonnet",
        permission_mode: str | None = None,
        system_prompt: str | None = None,
        env_vars: dict[str, str] | None = None,
        interactive: bool = False,
    ) -> None:
        self.task_id = task_id
        self._prompt = prompt
        self._working_dir = working_dir
        self._model = model
        self._permission_mode = permission_mode
        self._system_prompt = system_prompt
        self._env_vars = env_vars or {}
        self._interactive = interactive

        self.client: ClaudeSDKClient | None = None
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.pid: int | None = None  # SDK doesn't expose PID

        # Output event queue.  10 000 items is ~10 MB of event dicts;
        # large enough for long-running tasks without blocking the SDK
        # callback, small enough to avoid runaway memory if the consumer
        # falls behind.
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._pending_permission: _PendingPermission | None = None
        self._run_task: asyncio.Task | None = None
        self._exit_code: int | None = None
        self._is_running = False
        self._session_id: str | None = None

        # Cost tracking
        self.total_cost_usd: float = 0.0
        self.result_text: str | None = None

    # ------------------------------------------------------------------
    # Queue helpers (overflow protection)
    # ------------------------------------------------------------------

    async def _safe_enqueue(self, event: dict[str, Any]) -> None:
        """Put event on queue with timeout. Drop and log if queue stays full."""
        try:
            await asyncio.wait_for(
                self._message_queue.put(event), timeout=self.ENQUEUE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] Output queue full for 5s, dropping event type=%s",
                self.task_id, event.get("type"),
            )

    async def _enqueue_eof(self) -> None:
        """Enqueue _eof sentinel. MUST succeed or the monitor hangs forever."""
        try:
            await asyncio.wait_for(
                self._message_queue.put({"type": "_eof"}),
                timeout=self.EOF_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.critical(
                "[%s] CRITICAL: Could not enqueue _eof after 30s. "
                "Monitor will hang. Force-draining queue.",
                self.task_id,
            )
            # Nuclear option: drain the queue and force-put _eof
            while not self._message_queue.empty():
                try:
                    self._message_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                self._message_queue.put_nowait({"type": "_eof"})
            except asyncio.QueueFull:
                logger.critical("[%s] Queue still full after drain!", self.task_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> dict[str, str] | None:
        """Build the system_prompt value for ClaudeAgentOptions.

        Uses ``--append-system-prompt`` mode so Claude Code loads its default
        system prompt (which includes installed skills and CLAUDE.md) and then
        appends our task-specific instructions.  When no custom prompt is set,
        returns ``None`` which makes the SDK pass ``--system-prompt ""``
        (suppresses default prompt — used for tasks that need a blank slate).
        """
        if self._system_prompt:
            return {"type": "preset", "append": self._system_prompt}
        return None

    async def _finalize_exit(self, error_type: str | None, mode: str) -> None:
        """Set exit code + enqueue error event after the stream loop.

        Shared post-stream logic for all five runner methods.

        ``error_type`` is the classifier from ``_classify_result_error``
        (e.g. ``"api_error"``, ``"usage_limit"``) or ``None`` for a
        successful result.
        """
        if error_type is not None:
            logger.warning(
                "[%s] SDKProcess %s got %s result: %r",
                self.task_id, mode, error_type, (self.result_text or "")[:200],
            )
            await self._safe_enqueue({
                "type": "error",
                "error_type": error_type,
                "content": self.result_text or f"{error_type} error in result",
            })
            self._exit_code = 1
        else:
            self._exit_code = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start processing the task prompt."""
        self.started_at = datetime.now(timezone.utc)
        self._is_running = True

        if self._interactive:
            self._run_task = asyncio.create_task(
                self._run_interactive(), name=f"sdk-{self.task_id}",
            )
        else:
            self._run_task = asyncio.create_task(
                self._run_oneshot(), name=f"sdk-{self.task_id}",
            )
        logger.info("[%s] SDKProcess started (interactive=%s)", self.task_id, self._interactive)

    async def start_continuation(self, message: str, resume_session_id: str) -> None:
        """Resume a previous session with a follow-up message."""
        self.started_at = datetime.now(timezone.utc)
        self._is_running = True
        self._session_id = resume_session_id

        if self._interactive:
            self._run_task = asyncio.create_task(
                self._run_interactive_continuation(message, resume_session_id),
                name=f"sdk-{self.task_id}-continue",
            )
        else:
            self._run_task = asyncio.create_task(
                self._run_continuation(message, resume_session_id),
                name=f"sdk-{self.task_id}-continue",
            )
        logger.info("[%s] SDKProcess continuing session %s (interactive=%s)",
                     self.task_id, resume_session_id[:12], self._interactive)

    async def _run_continuation(self, message: str, resume_session_id: str) -> None:
        """Resume a previous session via ``query()`` with ``resume``."""
        try:
            options = ClaudeAgentOptions(
                cwd=self._working_dir,
                model=self._model,
                system_prompt=self._build_system_prompt(),
                env=self._env_vars,
                include_partial_messages=True,
                resume=resume_session_id,
                setting_sources=_SETTING_SOURCES,
            )
            sdk_mode = _PERMISSION_MODE_MAP.get(self._permission_mode or "")
            if sdk_mode:
                options.permission_mode = sdk_mode

            error_type: str | None = None
            try:
                async for msg in sdk_query(prompt=message, options=options):
                    event = self._message_to_event(msg)
                    if event:
                        await self._safe_enqueue(event)
                        if event.get("type") == "result":
                            self.total_cost_usd += event.get("cost_usd", 0.0)
                            self.result_text = event.get("result")
                            self._session_id = event.get("session_id") or resume_session_id
                            error_type = _classify_result_error(event)
                            break  # SDK stream may hang after result
            except Exception as exc:
                streaming_exc = exc if isinstance(exc, StreamingError) else StreamingError(
                    f"sdk_query streaming loop error for task '{self.task_id}'",
                    cause=exc,
                )
                logger.exception("[%s] SDKProcess continuation streaming error", self.task_id)
                await self._safe_enqueue({"type": "error", "content": str(streaming_exc)})
                self._exit_code = 1
                return  # outer finally still executes

            await self._finalize_exit(error_type, "continuation")
        except Exception as exc:
            logger.exception("[%s] SDKProcess continuation failed", self.task_id)
            await self._safe_enqueue({
                "type": "error", "content": str(exc),
            })
            self._exit_code = 1
        finally:
            self._is_running = False
            self.finished_at = datetime.now(timezone.utc)
            await self._enqueue_eof()

    # -- Non-interactive: one-shot query() --------------------------------

    async def _run_oneshot(self) -> None:
        """Run via ``query()`` — fire-and-forget, no multi-turn."""
        try:
            options = ClaudeAgentOptions(
                cwd=self._working_dir,
                model=self._model,
                system_prompt=self._build_system_prompt(),
                env=self._env_vars,
                include_partial_messages=True,
                setting_sources=_SETTING_SOURCES,
            )
            sdk_mode = _PERMISSION_MODE_MAP.get(self._permission_mode or "")
            if sdk_mode:
                options.permission_mode = sdk_mode

            error_type: str | None = None
            try:
                async for message in sdk_query(prompt=self._prompt, options=options):
                    event = self._message_to_event(message)
                    if event:
                        await self._safe_enqueue(event)
                        if event.get("type") == "result":
                            self.total_cost_usd = event.get("cost_usd", 0.0)
                            self.result_text = event.get("result")
                            self._session_id = event.get("session_id")
                            error_type = _classify_result_error(event)
                            # Break immediately — the SDK stream may hang
                            # waiting for stream close timeout after yielding
                            # the result. All useful data has been received.
                            break
            except Exception as exc:
                streaming_exc = exc if isinstance(exc, StreamingError) else StreamingError(
                    f"sdk_query streaming loop error for task '{self.task_id}'",
                    cause=exc,
                )
                logger.exception("[%s] SDKProcess oneshot streaming error", self.task_id)
                await self._safe_enqueue({"type": "error", "content": str(streaming_exc)})
                self._exit_code = 1
                return  # outer finally still executes

            await self._finalize_exit(error_type, "oneshot")
        except Exception as exc:
            logger.exception("[%s] SDKProcess oneshot failed", self.task_id)
            await self._safe_enqueue({
                "type": "error", "content": str(exc),
            })
            self._exit_code = 1
        finally:
            self._is_running = False
            self.finished_at = datetime.now(timezone.utc)
            await self._enqueue_eof()

    # -- Interactive: ClaudeSDKClient with can_use_tool -------------------

    async def _run_interactive(self) -> None:
        """Run via ``ClaudeSDKClient`` — multi-turn with permission callbacks."""
        try:
            options = ClaudeAgentOptions(
                cwd=self._working_dir,
                model=self._model,
                system_prompt=self._build_system_prompt(),
                env=self._env_vars,
                include_partial_messages=True,
                setting_sources=_SETTING_SOURCES,
                hooks={
                    "PreToolUse": [
                        HookMatcher(matcher=None, hooks=[_keep_alive_hook])
                    ]
                },
            )
            # Always use can_use_tool for interactive tasks — it's the only
            # reliable way to handle WebFetch/network tool permissions (Claude
            # Code's bypassPermissions doesn't cover network tools).
            # Additionally set permission_mode if available, so Claude Code
            # auto-approves file/edit tools without routing through stdio.
            options.can_use_tool = self._handle_permission
            sdk_mode = _PERMISSION_MODE_MAP.get(self._permission_mode or "")
            if sdk_mode:
                options.permission_mode = sdk_mode

            self.client = ClaudeSDKClient(options=options)
            await self.client.connect(self._prompt_stream(self._prompt))

            error_type: str | None = None
            try:
                async for message in self.client.receive_response():
                    event = self._message_to_event(message)
                    if event:
                        await self._safe_enqueue(event)
                        if event.get("type") == "result":
                            self.total_cost_usd = event.get("cost_usd", 0.0)
                            self.result_text = event.get("result")
                            self._session_id = event.get("session_id")
                            error_type = _classify_result_error(event)
                            # Break immediately — the SDK stream may hang
                            # waiting for stream close timeout after yielding
                            # the result. All useful data has been received.
                            break
            except Exception as exc:
                streaming_exc = exc if isinstance(exc, StreamingError) else StreamingError(
                    f"receive_response streaming loop error for task '{self.task_id}'",
                    cause=exc,
                )
                logger.exception("[%s] SDKProcess interactive streaming error", self.task_id)
                await self._safe_enqueue({"type": "error", "content": str(streaming_exc)})
                self._exit_code = 1
                return  # outer finally still executes

            await self._finalize_exit(error_type, "interactive")
        except Exception as exc:
            logger.exception("[%s] SDKProcess interactive failed", self.task_id)
            await self._safe_enqueue({
                "type": "error", "content": str(exc),
            })
            self._exit_code = 1
        finally:
            self._is_running = False
            self.finished_at = datetime.now(timezone.utc)
            await self._enqueue_eof()

    @staticmethod
    async def _prompt_stream(prompt: str):
        """Wrap a string prompt as an async iterable for streaming mode."""
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }

    # -- Interactive continuation (resume with tool approval) ---------------

    async def _run_interactive_continuation(
        self, message: str, resume_session_id: str
    ) -> None:
        """Resume an interactive session via ``ClaudeSDKClient`` with tool approval."""
        try:
            options = ClaudeAgentOptions(
                cwd=self._working_dir,
                model=self._model,
                system_prompt=self._build_system_prompt(),
                env=self._env_vars,
                include_partial_messages=True,
                setting_sources=_SETTING_SOURCES,
                resume=resume_session_id,
                hooks={
                    "PreToolUse": [
                        HookMatcher(matcher=None, hooks=[_keep_alive_hook])
                    ]
                },
            )
            # Same as _run_interactive: always use can_use_tool for network
            # tools, plus permission_mode for file/edit auto-approval.
            options.can_use_tool = self._handle_permission
            sdk_mode = _PERMISSION_MODE_MAP.get(self._permission_mode or "")
            if sdk_mode:
                options.permission_mode = sdk_mode

            self.client = ClaudeSDKClient(options=options)
            await self.client.connect(self._prompt_stream(message))

            error_type: str | None = None
            try:
                async for msg in self.client.receive_response():
                    event = self._message_to_event(msg)
                    if event:
                        await self._safe_enqueue(event)
                        if event.get("type") == "result":
                            self.total_cost_usd += event.get("cost_usd", 0.0)
                            self.result_text = event.get("result")
                            self._session_id = event.get("session_id") or resume_session_id
                            error_type = _classify_result_error(event)
                            # Break immediately — the SDK stream may hang
                            # waiting for stream close timeout after yielding
                            # the result. All useful data has been received.
                            break
            except Exception as exc:
                streaming_exc = exc if isinstance(exc, StreamingError) else StreamingError(
                    f"receive_response streaming loop error for task '{self.task_id}'",
                    cause=exc,
                )
                logger.exception("[%s] SDKProcess interactive continuation streaming error", self.task_id)
                await self._safe_enqueue({"type": "error", "content": str(streaming_exc)})
                self._exit_code = 1
                return  # outer finally still executes

            await self._finalize_exit(error_type, "interactive continuation")
        except Exception as exc:
            logger.exception("[%s] SDKProcess interactive continuation failed", self.task_id)
            await self._safe_enqueue({
                "type": "error", "content": str(exc),
            })
            self._exit_code = 1
        finally:
            self._is_running = False
            self.finished_at = datetime.now(timezone.utc)
            await self._enqueue_eof()

    # -- Follow-up (interactive only) -------------------------------------

    async def send_input(self, text: str) -> None:
        """Send a follow-up message to an interactive conversation."""
        if not self._interactive:
            raise TaskExecutionError(
                "Cannot send input to non-interactive task", task_id=self.task_id,
            )
        if self.client is None:
            raise TaskExecutionError(
                "SDKProcess not connected", task_id=self.task_id,
            )

        self._is_running = True
        self._run_task = asyncio.create_task(
            self._run_followup(text), name=f"sdk-{self.task_id}-followup",
        )

    async def _run_followup(self, prompt: str) -> None:
        """Run a follow-up query on an already-connected client."""
        try:
            await self.client.query(prompt)
            error_type: str | None = None
            try:
                async for message in self.client.receive_response():
                    event = self._message_to_event(message)
                    if event:
                        await self._safe_enqueue(event)
                        if event.get("type") == "result":
                            self.total_cost_usd += event.get("cost_usd", 0.0)
                            self.result_text = event.get("result")
                            classified = _classify_result_error(event)
                            if classified is not None:
                                error_type = classified
            except Exception as exc:
                streaming_exc = exc if isinstance(exc, StreamingError) else StreamingError(
                    f"receive_response streaming loop error for task '{self.task_id}'",
                    cause=exc,
                )
                logger.exception("[%s] SDKProcess follow-up streaming error", self.task_id)
                await self._safe_enqueue({"type": "error", "content": str(streaming_exc)})
                self._exit_code = 1
                return  # outer finally still executes

            await self._finalize_exit(error_type, "follow-up")
        except Exception as exc:
            logger.exception("[%s] SDKProcess follow-up failed", self.task_id)
            await self._safe_enqueue({
                "type": "error", "content": str(exc),
            })
            self._exit_code = 1
        finally:
            self._is_running = False
            self.finished_at = datetime.now(timezone.utc)
            await self._enqueue_eof()

    # -- Stop -------------------------------------------------------------

    async def stop(self) -> None:
        """Stop the SDK process."""
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception:
                logger.debug("[%s] Interrupt failed", self.task_id)
            try:
                await self.client.disconnect()
            except Exception as exc:
                logger.warning("[%s] Disconnect failed: %s", self.task_id, exc)

        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

        self._is_running = False
        self.finished_at = datetime.now(timezone.utc)
        self._exit_code = -1
        logger.info("[%s] SDKProcess stopped", self.task_id)

    # -- Output stream ----------------------------------------------------

    #: If no output arrives within this many seconds, assume the stream is dead.
    STREAM_HEARTBEAT_TIMEOUT: float = 300.0  # 5 minutes

    async def stream_output(self) -> AsyncIterator[dict[str, Any]]:
        """Yield output events. Stops on ``_eof`` sentinel.

        If no event arrives within :attr:`STREAM_HEARTBEAT_TIMEOUT` seconds
        and the process is no longer running, the stream is considered dead
        and a synthetic error event is yielded before terminating.
        """
        while True:
            try:
                event = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=self.STREAM_HEARTBEAT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if not self._is_running:
                    logger.error(
                        "[%s] Stream heartbeat timeout: no output for %ds "
                        "and process is not running — ending stream",
                        self.task_id, self.STREAM_HEARTBEAT_TIMEOUT,
                    )
                    yield {
                        "type": "error",
                        "error": (
                            f"Stream timeout: no output for "
                            f"{int(self.STREAM_HEARTBEAT_TIMEOUT)}s "
                            f"and process is no longer running"
                        ),
                    }
                    break
                # Process still running but idle — warn and keep waiting
                logger.warning(
                    "[%s] Stream idle for %ds while process still running "
                    "— may be stalled",
                    self.task_id, int(self.STREAM_HEARTBEAT_TIMEOUT),
                )
                yield {
                    "type": "error",
                    "error": (
                        f"No output for {int(self.STREAM_HEARTBEAT_TIMEOUT)}s "
                        f"— task may be stalled"
                    ),
                }
                continue
            if event.get("type") == "_eof":
                break
            yield event

    # ------------------------------------------------------------------
    # Permission handling (interactive only)
    # ------------------------------------------------------------------

    # Read-only tools that can be auto-approved for interactive tasks
    _AUTO_APPROVE_TOOLS = {"Read", "Glob", "Grep"}

    async def _handle_permission(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Called by the SDK when a tool needs approval."""
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(input_data)

        # Auto-approve read-only tools
        if tool_name in self._AUTO_APPROVE_TOOLS:
            return PermissionResultAllow()

        pending = _PendingPermission(tool_name, input_data)
        self._pending_permission = pending

        await self._safe_enqueue({
            "type": "permission_request",
            "tool_name": tool_name,
            "tool_input": input_data,
            "is_checkpoint": True,
        })

        logger.info("[%s] Awaiting permission for %s", self.task_id, tool_name)
        result = await pending.wait()
        self._pending_permission = None
        return result

    async def _handle_ask_user_question(
        self, input_data: dict[str, Any]
    ) -> PermissionResultAllow:
        """Handle AskUserQuestion tool."""
        pending = _PendingPermission("AskUserQuestion", input_data)
        self._pending_permission = pending

        await self._safe_enqueue({
            "type": "ask_user_question",
            "questions": input_data.get("questions", []),
            "is_checkpoint": True,
        })

        logger.info("[%s] Awaiting user answers", self.task_id)
        result = await pending.wait()
        self._pending_permission = None
        return result

    async def approve_tool(self, updated_input: dict | None = None) -> None:
        """Approve the pending permission request."""
        if self._pending_permission is None:
            raise TaskExecutionError(
                "No pending permission request", task_id=self.task_id,
            )
        inp = updated_input or self._pending_permission.input_data
        self._pending_permission.resolve(PermissionResultAllow(updated_input=inp))

    async def deny_tool(self, message: str = "User denied this action") -> None:
        """Deny the pending permission request."""
        if self._pending_permission is None:
            raise TaskExecutionError(
                "No pending permission request", task_id=self.task_id,
            )
        self._pending_permission.resolve(PermissionResultDeny(message=message))

    async def answer_questions(self, answers: dict[str, str]) -> None:
        """Answer a pending AskUserQuestion request."""
        if self._pending_permission is None:
            raise TaskExecutionError(
                "No pending question", task_id=self.task_id,
            )
        self._pending_permission.resolve(
            PermissionResultAllow(
                updated_input={
                    "questions": self._pending_permission.input_data.get("questions", []),
                    "answers": answers,
                }
            )
        )

    @property
    def has_pending_permission(self) -> bool:
        return self._pending_permission is not None

    @property
    def pending_permission_info(self) -> dict | None:
        if self._pending_permission is None:
            return None
        return {
            "tool_name": self._pending_permission.tool_name,
            "input": self._pending_permission.input_data,
        }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _message_to_event(message: Any) -> dict[str, Any] | None:
        """Convert an SDK message to a dict event."""
        # ResultMessage
        if hasattr(message, "result") and hasattr(message, "total_cost_usd"):
            usage = getattr(message, "usage", None) or {}
            return {
                "type": "result",
                "subtype": getattr(message, "subtype", "success"),
                "result": getattr(message, "result", ""),
                "cost_usd": getattr(message, "total_cost_usd", 0.0),
                "duration_ms": getattr(message, "duration_ms", 0),
                "session_id": getattr(message, "session_id", None),
                "is_error": getattr(message, "is_error", False),
                "is_checkpoint": False,
                "input_tokens": (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                ),
                "output_tokens": usage.get("output_tokens", 0),
                "num_turns": getattr(message, "num_turns", 0),
            }

        # StreamEvent (partial messages — token-by-token streaming)
        if hasattr(message, "event") and isinstance(getattr(message, "event", None), dict):
            raw = message.event
            raw_type = raw.get("type", "")

            # content_block_delta — individual text tokens
            if raw_type == "content_block_delta":
                delta = raw.get("delta", {})
                delta_type = delta.get("type", "")
                if delta_type == "text_delta":
                    return {
                        "type": "content_block_delta",
                        "delta": {"text": delta.get("text", "")},
                        "is_checkpoint": False,
                    }
                elif delta_type == "thinking_delta":
                    return {
                        "type": "content_block_delta",
                        "delta": {"thinking": delta.get("thinking", "")},
                        "is_checkpoint": False,
                    }
                return None  # skip other delta types

            # content_block_start — marks beginning of a block
            if raw_type == "content_block_start":
                cb = raw.get("content_block", {})
                cb_type = cb.get("type", "")
                if cb_type == "tool_use":
                    return {
                        "type": "content_block_start",
                        "content_block": {
                            "type": "tool_use",
                            "id": cb.get("id", ""),
                            "name": cb.get("name", ""),
                            "input": cb.get("input", {}),
                        },
                        "is_checkpoint": False,
                    }
                elif cb_type == "thinking":
                    return {
                        "type": "content_block_start",
                        "content_block": {"type": "thinking"},
                        "is_checkpoint": False,
                    }
                elif cb_type == "text":
                    return {
                        "type": "content_block_start",
                        "content_block": {"type": "text"},
                        "is_checkpoint": False,
                    }
                return None

            # content_block_stop — marks end of a block
            if raw_type == "content_block_stop":
                return {
                    "type": "content_block_stop",
                    "is_checkpoint": False,
                }

            # Skip message_start, message_delta, message_stop, ping
            return None

        # AssistantMessage (full blocks — fallback when partial not available)
        if hasattr(message, "content") and isinstance(message.content, list):
            assistant_blocks: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for block in message.content:
                if hasattr(block, "text") and not hasattr(block, "tool_use_id"):
                    assistant_blocks.append({"type": "text", "text": block.text})
                elif hasattr(block, "name") and hasattr(block, "input"):
                    assistant_blocks.append({
                        "type": "tool_use",
                        "name": block.name,
                        "input": block.input,
                    })
                elif hasattr(block, "thinking"):
                    assistant_blocks.append({"type": "thinking", "thinking": block.thinking})
                elif hasattr(block, "tool_use_id"):
                    content = getattr(block, "content", None)
                    if content is None:
                        content = ""
                    tool_results.append({
                        "tool_use_id": block.tool_use_id,
                        "content": content,
                    })
            # Tool result message (user role)
            if tool_results:
                return {
                    "type": "user",
                    "tool_use_results": tool_results,
                    "is_checkpoint": False,
                }
            # Assistant message with ALL content blocks
            if assistant_blocks:
                return {
                    "type": "assistant",
                    "message": {"content": assistant_blocks},
                    "is_checkpoint": False,
                }

        # SystemMessage
        if hasattr(message, "subtype"):
            return {
                "type": "system",
                "subtype": getattr(message, "subtype", ""),
                "is_checkpoint": False,
            }

        return None


class _PendingPermission:
    """Holds a pending permission request and its resolution future."""

    def __init__(self, tool_name: str, input_data: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.input_data = input_data
        self._future: asyncio.Future = asyncio.get_running_loop().create_future()

    def resolve(self, result: PermissionResultAllow | PermissionResultDeny) -> None:
        if not self._future.done():
            self._future.set_result(result)

    async def wait(self) -> PermissionResultAllow | PermissionResultDeny:
        return await self._future


async def _keep_alive_hook(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
    """Dummy PreToolUse hook required to keep the stream open for can_use_tool."""
    return {"continue_": True}
