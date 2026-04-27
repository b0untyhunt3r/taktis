"""Consult chat session management.

Provides :class:`ConsultSession` (a single advisory chat backed by the Claude
Agent SDK with multi-turn resume) and :class:`ConsultRegistry` (in-memory
manager with LRU eviction and TTL-based sweeping).

No database, no ProcessManager, no task creation.  Sessions are ephemeral —
they disappear when the server restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import query as sdk_query

from taktis.exceptions import ConsultError

logger = logging.getLogger(__name__)

# Sentinel that signals end-of-stream to :meth:`ConsultSession.stream_response`.
_EOF = object()

# Load all setting sources so installed plugins/skills are available.
_SETTING_SOURCES: list[str] = ["user", "project", "local"]


class ConsultSession:
    """An ephemeral, multi-turn advisory chat session.

    The session is backed by ``sdk_query`` with the ``resume`` parameter so
    that each follow-up message continues in the same Claude Code session.
    Streaming text tokens are placed on an :class:`asyncio.Queue` so callers
    can consume them via :meth:`stream_response`.

    Parameters
    ----------
    token:
        8-char hex identifier (caller-supplied, typically from
        ``uuid4().hex[:8]``).
    working_dir:
        Working directory passed to the SDK (context for Claude Code).
    system_prompt:
        Appended to the preset system prompt via ``{"type": "preset",
        "append": ...}``.
    model:
        Claude model alias (default ``"haiku"``).
    """

    def __init__(
        self,
        *,
        token: str,
        working_dir: str,
        system_prompt: str,
        model: str = "haiku",
    ) -> None:
        self.token: str = token
        self.messages: list[dict[str, str]] = []
        self._max_messages: int = 50  # Rolling window — prevent unbounded growth
        self.session_id: str | None = None
        self.model: str = model
        self.working_dir: str = working_dir
        self.system_prompt: str = system_prompt
        self.last_active: float = time.monotonic()
        self._is_running: bool = False
        self._message_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10000)
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, user_message: str) -> None:
        """Start an async query for *user_message*.

        Updates :attr:`last_active`, appends the user turn to
        :attr:`messages`, then spawns a background task that streams the
        assistant response into :attr:`_message_queue`.  Call
        :meth:`stream_response` to consume the stream.

        Raises :class:`ConsultError` if a query is already in progress.

        Parameters
        ----------
        user_message:
            The text the user typed.
        """
        if self._is_running:
            raise ConsultError(
                f"Consult session '{self.token}' is already processing a query"
            )
        self.last_active = time.monotonic()
        self.messages.append({"role": "user", "content": user_message})
        # Drain any leftover items from a previous stream that wasn't fully
        # consumed (e.g. client disconnected mid-response).
        while not self._message_queue.empty():
            try:
                self._message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._is_running = True
        self._task = asyncio.create_task(
            self._run_query(user_message),
            name=f"consult-{self.token}",
        )
        self._task.add_done_callback(self._on_query_done)

    async def stream_response(self) -> AsyncGenerator[str, None]:
        """Async generator that yields text chunks until the response ends.

        Yields
        ------
        str
            Each text token from the assistant response.
        """
        while True:
            item = await self._message_queue.get()
            if item is _EOF:
                break
            yield item

    def stop(self) -> None:
        """Cancel the running query task, if any."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_query_done(self, task: asyncio.Task[None]) -> None:
        """Done callback for the background query task (CLAUDE.md Rule 3)."""
        if task.cancelled():
            self._is_running = False
            return

        exc = task.exception()
        self._is_running = False

        if exc is not None:
            logger.error(
                "[consult %s] Query task crashed: %s",
                self.token,
                exc,
                exc_info=exc,
            )
            # Note: _run_query's finally block already puts _EOF on the queue,
            # so we do NOT put a second one here (that would poison the next
            # stream_response() call with an immediate empty return).

    async def _run_query(self, user_message: str) -> None:
        """Run ``sdk_query`` and stream text tokens onto :attr:`_message_queue`.

        Wraps unexpected failures in :class:`ConsultError`.  Puts :data:`_EOF`
        onto the queue in a ``finally`` block (using ``put_nowait`` to avoid
        re-raising ``CancelledError`` inside a ``finally``).
        """
        try:
            options = ClaudeAgentOptions(
                cwd=self.working_dir,
                model=self.model,
                system_prompt={"type": "preset", "append": self.system_prompt},
                include_partial_messages=True,
                setting_sources=_SETTING_SOURCES,
            )
            if self.session_id is not None:
                options.resume = self.session_id

            full_text_parts: list[str] = []

            try:
                async for message in sdk_query(prompt=user_message, options=options):
                    # ResultMessage — capture session_id and stop iterating.
                    if hasattr(message, "result") and hasattr(message, "total_cost_usd"):
                        self.session_id = (
                            getattr(message, "session_id", None) or self.session_id
                        )
                        break

                    # StreamEvent — extract text_delta tokens.
                    if hasattr(message, "event") and isinstance(
                        getattr(message, "event", None), dict
                    ):
                        raw = message.event
                        if raw.get("type") == "content_block_delta":
                            delta = raw.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text: str = delta.get("text", "")
                                if text:
                                    full_text_parts.append(text)
                                    await self._message_queue.put(text)

            except asyncio.CancelledError:
                raise  # propagate so the task is marked cancelled
            except ConsultError:
                raise
            except Exception as exc:
                raise ConsultError(
                    f"SDK streaming error in consult session '{self.token}'",
                    cause=exc,
                ) from exc

            accumulated = "".join(full_text_parts)
            self.messages.append({"role": "assistant", "content": accumulated})
            # Trim to rolling window — keep the most recent messages
            if len(self.messages) > self._max_messages:
                self.messages = self.messages[-self._max_messages:]
            logger.debug(
                "[consult %s] Response complete (%d chars)", self.token, len(accumulated)
            )

        except asyncio.CancelledError:
            logger.debug("[consult %s] Query task cancelled", self.token)
            raise
        except ConsultError:
            raise
        except Exception as exc:
            raise ConsultError(
                f"Unexpected error in consult session '{self.token}'",
                cause=exc,
            ) from exc
        finally:
            # put_nowait avoids re-raising CancelledError inside finally.
            self._message_queue.put_nowait(_EOF)


class ConsultRegistry:
    """In-memory registry for active :class:`ConsultSession` instances.

    Enforces a hard cap of 5 concurrent sessions with silent LRU eviction and
    a 30-minute TTL (swept every 5 minutes by :meth:`run_sweep_loop`).
    """

    _max_sessions: int = 5
    _ttl: int = 1800  # seconds (30 minutes)

    def __init__(self) -> None:
        self._sessions: dict[str, ConsultSession] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create(
        self,
        system_prompt: str,
        working_dir: str,
        model: str = "haiku",
    ) -> ConsultSession:
        """Create a new :class:`ConsultSession`.

        If the registry is at capacity, the least-recently-active session is
        silently evicted before the new one is created.

        Parameters
        ----------
        system_prompt:
            Text appended to the preset system prompt.
        working_dir:
            Working directory for the SDK process.
        model:
            Claude model alias (default ``"haiku"``).

        Returns
        -------
        ConsultSession
            The newly created session.
        """
        if len(self._sessions) >= self._max_sessions:
            lru_token = min(
                self._sessions,
                key=lambda t: self._sessions[t].last_active,
            )
            logger.debug(
                "[ConsultRegistry] Evicting LRU session %s (capacity=%d)",
                lru_token,
                self._max_sessions,
            )
            self._sessions[lru_token].stop()
            del self._sessions[lru_token]

        token = uuid4().hex[:16]
        session = ConsultSession(
            token=token,
            working_dir=working_dir,
            system_prompt=system_prompt,
            model=model,
        )
        self._sessions[token] = session
        logger.debug("[ConsultRegistry] Created session %s (total=%d)", token, len(self._sessions))
        return session

    def get(self, token: str) -> ConsultSession | None:
        """Return the session for *token*, or ``None`` if not found."""
        return self._sessions.get(token)

    def remove(self, token: str) -> None:
        """Stop and remove the session for *token* (no-op if absent)."""
        if token in self._sessions:
            self._sessions[token].stop()
            del self._sessions[token]
            logger.debug("[ConsultRegistry] Removed session %s", token)

    def sweep_expired(self) -> None:
        """Stop and remove all sessions whose :attr:`~ConsultSession.last_active`
        is older than :attr:`_ttl` seconds."""
        now = time.monotonic()
        expired = [
            token
            for token, session in self._sessions.items()
            if now - session.last_active > self._ttl
        ]
        for token in expired:
            self._sessions[token].stop()
            del self._sessions[token]
            logger.debug("[ConsultRegistry] Swept expired session %s", token)
        if expired:
            logger.info("[ConsultRegistry] Swept %d expired session(s)", len(expired))

    async def run_sweep_loop(self) -> None:
        """Coroutine that calls :meth:`sweep_expired` every 5 minutes.

        Intended to be run as a background :class:`asyncio.Task`.  Callers
        must attach a done_callback per CLAUDE.md Rule 3.
        """
        while True:
            await asyncio.sleep(300)
            self.sweep_expired()
