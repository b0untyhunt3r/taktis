"""Shared presentation helpers consumed by the Web UI.

Centralises formatting logic so status terminology, icons, durations,
and cost formatting are consistent throughout the interface.

Status values use the canonical enums from :mod:`taktis.models`
as keys so that all interfaces map the same values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from taktis.models import TaskStatus, PhaseStatus


# ---------------------------------------------------------------------------
# Status constants — single source of truth
# Uses enum values as keys so status comparisons are consistent.
# Also includes legacy/display-only statuses (idle, blocked, stopped).
# ---------------------------------------------------------------------------

STATUS_TEXT_ICONS: dict[str, str] = {
    TaskStatus.RUNNING: "[*]",
    TaskStatus.COMPLETED: "[+]",
    TaskStatus.FAILED: "[!]",
    TaskStatus.PENDING: "[ ]",
    TaskStatus.PAUSED: "[=]",
    TaskStatus.CANCELLED: "[x]",
    TaskStatus.AWAITING_INPUT: "[?]",
    # Phase statuses (str-based enums work as dict keys)
    PhaseStatus.NOT_STARTED: "[ ]",
    PhaseStatus.IN_PROGRESS: "[*]",
    PhaseStatus.COMPLETE: "[+]",
    # Display-only statuses
    "idle": "[-]",
    "blocked": "[#]",
}

STATUS_EMOJI: dict[str, str] = {
    TaskStatus.RUNNING: "\u25b6\ufe0f",        # ▶️
    TaskStatus.COMPLETED: "\u2705",             # ✅
    TaskStatus.FAILED: "\u274c",                # ❌
    TaskStatus.PENDING: "\u23f3",               # ⏳
    TaskStatus.PAUSED: "\u23f8\ufe0f",          # ⏸️
    TaskStatus.CANCELLED: "\u23f9\ufe0f",       # ⏹️
    TaskStatus.AWAITING_INPUT: "\u2753",        # ❓
    PhaseStatus.NOT_STARTED: "\u23f3",          # ⏳
    PhaseStatus.IN_PROGRESS: "\u25b6\ufe0f",    # ▶️
    PhaseStatus.COMPLETE: "\u2705",             # ✅
    # Display-only statuses
    "idle": "\u23f8\ufe0f",                     # ⏸️
    "blocked": "\U0001f6ab",                    # 🚫
    "stopped": "\u23f9\ufe0f",                  # ⏹️
}


def status_indicator(status: str) -> str:
    """Return a text-based status indicator like ``[*]``."""
    return STATUS_TEXT_ICONS.get(status, "[?]")


def status_icon(status: str) -> str:
    """Return an emoji status icon."""
    return STATUS_EMOJI.get(status, "\u2753")


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def format_duration(started_at: Any, completed_at: Any = None) -> str:
    """Return a human-readable duration string."""
    if started_at is None:
        return "--"
    end = completed_at or datetime.now(timezone.utc)
    if isinstance(started_at, str):
        started_at = datetime.fromisoformat(started_at)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    delta = end - started_at
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "--"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


# ---------------------------------------------------------------------------
# Cost formatting
# ---------------------------------------------------------------------------

def format_cost(cost: float | None) -> str:
    """Format a USD cost value for display."""
    if cost is None or cost == 0:
        return "--"
    return f"${cost:.4f}"


# ---------------------------------------------------------------------------
# ID formatting
# ---------------------------------------------------------------------------

def short_id(task_id: str) -> str:
    """Return the first 8 characters of a task/phase ID."""
    return task_id[:8] if task_id else "?"


# ---------------------------------------------------------------------------
# HTML escaping (minimal, for non-Jinja contexts)
# ---------------------------------------------------------------------------

def html_escape(text: str) -> str:
    """Minimal HTML escaping for use outside Jinja2 templates."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Task output text extraction
# ---------------------------------------------------------------------------

def extract_output_text(content: dict) -> str:
    """Extract human-readable text from a task output content dict.

    Handles the various event shapes from the Claude Agent SDK streaming.
    """
    # Errors
    if content.get("error"):
        return f"ERROR: {content['error']}"
    if content.get("stderr"):
        return f"ERROR: {content['stderr']}"

    ctype = content.get("type", "")

    # Assistant text deltas (the actual response streaming)
    if ctype == "content_block_delta":
        delta = content.get("delta", {})
        return delta.get("text", "")

    # Assistant message with string content
    if ctype == "assistant" and isinstance(content.get("content"), str):
        return content["content"]

    # Content block start with text
    if ctype == "content_block_start":
        cb = content.get("content_block", {})
        if cb.get("type") == "tool_use":
            return f"[tool: {cb.get('name', '?')}]"
        return cb.get("text", "")

    # Final result
    if ctype == "result" and isinstance(content.get("result"), str):
        return content["result"]

    # Raw non-JSON output
    if ctype == "raw_output":
        return content.get("content", "")

    # Skip metadata events
    if ctype in ("message_start", "message_stop", "message_delta",
                 "content_block_stop", "ping", "system"):
        return ""

    # Fallback: try common text fields
    if content.get("text"):
        return content["text"]
    if isinstance(content.get("content"), str) and content["content"]:
        return content["content"]

    return ""
