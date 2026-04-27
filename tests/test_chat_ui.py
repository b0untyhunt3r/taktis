"""Tests for chat UI improvements: tool display, message cards, result handling.

Covers:
- _extract_result_text: all content types (str, list, dict, None, empty)
- _process_tool_results: both new (tool_use_results) and legacy (tool_use_result) formats
- _accumulate_output_blocks: full pipeline with tool results attached to tool_use blocks
- _message_to_event: multi-block messages, all block types
- _sse_html_for_event: HTML output for all event types
- _tool_summary: per-tool parameter extraction
"""
from __future__ import annotations

import json
import pytest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

# Import the functions under test from app.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from taktis.web.app import (
    _extract_result_text,
    _process_tool_results,
    _accumulate_output_blocks,
    _tool_summary,
    _TOOL_ICONS,
)
from taktis.core.sdk_process import SDKProcess


# ── _extract_result_text ─────────────────────────────────────────────────


class TestExtractResultText:
    def test_none_returns_no_output(self):
        assert _extract_result_text(None) == "(no output)"

    def test_empty_string_returns_no_output(self):
        assert _extract_result_text("") == "(no output)"

    def test_string_returned_as_is(self):
        assert _extract_result_text("hello world") == "hello world"

    def test_long_string(self):
        text = "x" * 10000
        assert _extract_result_text(text) == text

    def test_list_of_text_dicts(self):
        content = [
            {"type": "text", "text": "line 1"},
            {"type": "text", "text": "line 2"},
        ]
        assert _extract_result_text(content) == "line 1\nline 2"

    def test_list_of_dicts_without_text(self):
        content = [{"type": "image", "data": "base64..."}]
        result = _extract_result_text(content)
        assert "image" in result

    def test_list_of_strings(self):
        content = ["file1.py", "file2.py"]
        assert _extract_result_text(content) == "file1.py\nfile2.py"

    def test_list_mixed(self):
        content = [{"text": "hello"}, "raw string", 42]
        result = _extract_result_text(content)
        assert "hello" in result
        assert "raw string" in result
        assert "42" in result

    def test_empty_list(self):
        # Empty list is truthy for isinstance but has no items
        assert _extract_result_text([]) == ""

    def test_integer(self):
        assert _extract_result_text(42) == "42"

    def test_dict(self):
        result = _extract_result_text({"key": "value"})
        assert "key" in result

    def test_bool_false(self):
        # bool False should not trigger the None/empty check
        assert _extract_result_text(False) == "False"


# ── _process_tool_results ────────────────────────────────────────────────


class TestProcessToolResults:
    """Test both new (tool_use_results) and legacy (tool_use_result) formats."""

    def test_new_format_attaches_to_last_tool_use(self):
        blocks = [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
        ]
        event = {
            "type": "user",
            "tool_use_results": [
                {"tool_use_id": "123", "content": "file contents here"},
            ],
        }
        _process_tool_results(event, blocks)
        assert blocks[1]["result"] == "file contents here"

    def test_new_format_multiple_results(self):
        blocks = [
            {"type": "tool_use", "name": "Read", "input": {}},
            {"type": "tool_use", "name": "Grep", "input": {}},
        ]
        event = {
            "type": "user",
            "tool_use_results": [
                {"tool_use_id": "1", "content": "file A contents"},
                {"tool_use_id": "2", "content": "grep matches"},
            ],
        }
        _process_tool_results(event, blocks)
        # Second result attaches to second tool_use (reverse iteration)
        assert blocks[1]["result"] == "grep matches"
        assert blocks[0]["result"] == "file A contents"

    def test_new_format_none_content(self):
        blocks = [{"type": "tool_use", "name": "Bash", "input": {}}]
        event = {
            "type": "user",
            "tool_use_results": [
                {"tool_use_id": "1", "content": None},
            ],
        }
        _process_tool_results(event, blocks)
        assert blocks[0]["result"] == "(no output)"

    def test_new_format_no_matching_tool_use(self):
        blocks = [{"type": "text", "text": "hello"}]
        event = {
            "type": "user",
            "tool_use_results": [
                {"tool_use_id": "1", "content": "orphan result"},
            ],
        }
        _process_tool_results(event, blocks)
        # Should create a standalone tool_result block
        assert blocks[-1]["type"] == "tool_result"
        assert blocks[-1]["text"] == "orphan result"

    def test_legacy_format_single_result(self):
        blocks = [{"type": "tool_use", "name": "Read", "input": {}}]
        event = {
            "type": "user",
            "tool_use_result": "legacy file contents",
        }
        _process_tool_results(event, blocks)
        assert blocks[0]["result"] == "legacy file contents"

    def test_legacy_format_list_content(self):
        blocks = [{"type": "tool_use", "name": "Read", "input": {}}]
        event = {
            "type": "user",
            "tool_use_result": [
                {"type": "text", "text": "line 1"},
                {"type": "text", "text": "line 2"},
            ],
        }
        _process_tool_results(event, blocks)
        assert "line 1" in blocks[0]["result"]
        assert "line 2" in blocks[0]["result"]

    def test_legacy_format_no_matching_tool_use(self):
        blocks = []
        event = {"type": "user", "tool_use_result": "orphan"}
        _process_tool_results(event, blocks)
        assert blocks[0]["type"] == "tool_result"

    def test_truncation_at_5000(self):
        blocks = [{"type": "tool_use", "name": "Read", "input": {}}]
        event = {
            "type": "user",
            "tool_use_results": [
                {"tool_use_id": "1", "content": "x" * 10000},
            ],
        }
        _process_tool_results(event, blocks)
        assert len(blocks[0]["result"]) == 5000

    def test_no_result_data(self):
        blocks = [{"type": "tool_use", "name": "Read", "input": {}}]
        event = {"type": "user"}  # No tool_use_result or tool_use_results
        _process_tool_results(event, blocks)
        # Should not modify blocks
        assert "result" not in blocks[0]

    def test_new_format_takes_precedence(self):
        """If both formats present, new format wins."""
        blocks = [{"type": "tool_use", "name": "Read", "input": {}}]
        event = {
            "type": "user",
            "tool_use_results": [{"content": "new format"}],
            "tool_use_result": "legacy format",
        }
        _process_tool_results(event, blocks)
        assert blocks[0]["result"] == "new format"

    def test_already_has_result_skips(self):
        """Don't overwrite existing results."""
        blocks = [
            {"type": "tool_use", "name": "Read", "input": {}, "result": "existing"},
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]
        event = {
            "type": "user",
            "tool_use_results": [{"content": "new result"}],
        }
        _process_tool_results(event, blocks)
        assert blocks[0]["result"] == "existing"  # Not overwritten
        assert blocks[1]["result"] == "new result"  # Attached to second


# ── _tool_summary ────────────────────────────────────────────────────────


class TestToolSummary:
    def test_read_shows_file_path(self):
        assert _tool_summary("Read", {"file_path": "/tmp/test.py"}) == "/tmp/test.py"

    def test_edit_shows_file_path(self):
        result = _tool_summary("Edit", {"file_path": "/tmp/test.py", "old_string": "a", "new_string": "b"})
        assert "/tmp/test.py" in result

    def test_write_shows_file_path(self):
        assert _tool_summary("Write", {"file_path": "/tmp/out.md"}) == "/tmp/out.md"

    def test_bash_shows_command(self):
        result = _tool_summary("Bash", {"command": "ls -la /tmp"})
        assert "ls -la" in result

    def test_bash_truncates_long_command(self):
        result = _tool_summary("Bash", {"command": "x" * 200})
        assert len(result) <= 80

    def test_grep_shows_pattern(self):
        assert _tool_summary("Grep", {"pattern": "def foo"}) == "def foo"

    def test_glob_shows_pattern(self):
        assert _tool_summary("Glob", {"pattern": "*.py"}) == "*.py"

    def test_unknown_tool_shows_first_value(self):
        result = _tool_summary("ToolSearch", {"query": "select:Read", "max_results": 5})
        assert "select:Read" in result

    def test_no_input(self):
        assert _tool_summary("Read", None) == ""

    def test_empty_input(self):
        assert _tool_summary("Read", {}) == ""

    def test_html_escaped(self):
        result = _tool_summary("Bash", {"command": "echo '<script>'"})
        assert "<script>" not in result
        assert "&lt;script&gt;" in result


# ── _accumulate_output_blocks ────────────────────────────────────────────


class TestAccumulateOutputBlocks:
    """Test the full pipeline: events → blocks with tool results attached."""

    def _make_event(self, content: dict) -> dict:
        return {"id": 1, "content": content, "timestamp": "2024-01-01T00:00:00"}

    def test_simple_text(self):
        events = [
            self._make_event({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}),
        ]
        blocks = _accumulate_output_blocks(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "hello"

    def test_tool_use_with_result(self):
        events = [
            self._make_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "test.py"}},
                ]},
            }),
            self._make_event({
                "type": "user",
                "tool_use_results": [{"tool_use_id": "1", "content": "file contents"}],
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        tool_block = [b for b in blocks if b["type"] == "tool_use"][0]
        assert tool_block["name"] == "Read"
        assert tool_block["input"]["file_path"] == "test.py"
        assert tool_block["result"] == "file contents"

    def test_tool_use_legacy_result(self):
        events = [
            self._make_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ]},
            }),
            self._make_event({
                "type": "user",
                "tool_use_result": "file1.py\nfile2.py",
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        tool_block = [b for b in blocks if b["type"] == "tool_use"][0]
        assert "file1.py" in tool_block["result"]

    def test_multi_block_assistant(self):
        """Multiple content blocks in one assistant message."""
        events = [
            self._make_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "Let me read the file"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
                ]},
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "text"
        assert blocks[1]["type"] == "tool_use"

    def test_multiple_tool_results_match_tool_uses(self):
        events = [
            self._make_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "b.py"}},
                ]},
            }),
            self._make_event({
                "type": "user",
                "tool_use_results": [
                    {"tool_use_id": "1", "content": "content of a.py"},
                    {"tool_use_id": "2", "content": "content of b.py"},
                ],
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        tool_blocks = [b for b in blocks if b["type"] == "tool_use"]
        assert len(tool_blocks) == 2
        # Results attach in reverse order — second result to second tool, first to first
        assert tool_blocks[1]["result"] == "content of b.py"
        assert tool_blocks[0]["result"] == "content of a.py"

    def test_thinking_block(self):
        events = [
            self._make_event({
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "let me think"}]},
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        assert blocks[0]["type"] == "thinking"
        assert blocks[0]["text"] == "let me think"

    def test_user_message(self):
        events = [
            self._make_event({"type": "user_message", "text": "user reply"}),
        ]
        blocks = _accumulate_output_blocks(events)
        assert blocks[0]["type"] == "user_message"
        assert blocks[0]["text"] == "user reply"

    def test_error_event(self):
        events = [
            self._make_event({"type": "error", "error": "something broke"}),
        ]
        blocks = _accumulate_output_blocks(events)
        assert blocks[0]["type"] == "error"

    def test_streaming_deltas_in_progress(self):
        """In-progress turn: accumulate streaming deltas."""
        events = [
            self._make_event({"type": "content_block_start", "content_block": {"type": "text"}}),
            self._make_event({"type": "content_block_delta", "delta": {"text": "hello "}}),
            self._make_event({"type": "content_block_delta", "delta": {"text": "world"}}),
        ]
        blocks = _accumulate_output_blocks(events)
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "hello world"
        assert blocks[0]["in_progress"] is True

    def test_streaming_tool_use_start(self):
        events = [
            self._make_event({
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Read", "input": {"file_path": "test.py"}},
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["name"] == "Read"

    def test_streaming_tool_result_in_progress(self):
        events = [
            self._make_event({
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Bash", "input": {}},
            }),
            self._make_event({"type": "content_block_stop"}),
            self._make_event({
                "type": "user",
                "tool_use_results": [{"content": "command output"}],
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        tool_block = [b for b in blocks if b["type"] == "tool_use"][0]
        assert tool_block["result"] == "command output"

    def test_empty_events(self):
        assert _accumulate_output_blocks([]) == []

    def test_result_without_assistant(self):
        events = [
            self._make_event({"type": "result", "result": "final answer"}),
        ]
        blocks = _accumulate_output_blocks(events)
        assert blocks[0]["type"] == "result"
        assert blocks[0]["text"] == "final answer"

    def test_tool_result_none_content(self):
        events = [
            self._make_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "true"}},
                ]},
            }),
            self._make_event({
                "type": "user",
                "tool_use_results": [{"tool_use_id": "1", "content": None}],
            }),
        ]
        blocks = _accumulate_output_blocks(events)
        tool_block = [b for b in blocks if b["type"] == "tool_use"][0]
        assert tool_block["result"] == "(no output)"


# ── _message_to_event (SDK layer) ───────────────────────────────────────


@dataclass
class MockTextBlock:
    text: str

@dataclass
class MockThinkingBlock:
    thinking: str
    signature: str = ""

@dataclass
class MockToolUseBlock:
    id: str
    name: str
    input: dict

@dataclass
class MockToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool = False

@dataclass
class MockAssistantMessage:
    content: list = field(default_factory=list)

@dataclass
class MockResultMessage:
    result: str = ""
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    session_id: str = ""
    is_error: bool = False
    subtype: str = "success"
    usage: dict = field(default_factory=dict)
    num_turns: int = 1

@dataclass
class MockStreamEvent:
    event: dict = field(default_factory=dict)


class TestMessageToEvent:
    """Test SDKProcess._message_to_event with mock SDK objects."""

    def test_text_block(self):
        msg = MockAssistantMessage(content=[MockTextBlock(text="hello")])
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "assistant"
        assert result["message"]["content"][0]["type"] == "text"
        assert result["message"]["content"][0]["text"] == "hello"

    def test_tool_use_block(self):
        msg = MockAssistantMessage(content=[
            MockToolUseBlock(id="t1", name="Read", input={"file_path": "test.py"}),
        ])
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "assistant"
        assert result["message"]["content"][0]["type"] == "tool_use"
        assert result["message"]["content"][0]["name"] == "Read"
        assert result["message"]["content"][0]["input"]["file_path"] == "test.py"

    def test_thinking_block(self):
        msg = MockAssistantMessage(content=[MockThinkingBlock(thinking="hmm")])
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "assistant"
        assert result["message"]["content"][0]["type"] == "thinking"

    def test_tool_result_block(self):
        msg = MockAssistantMessage(content=[
            MockToolResultBlock(tool_use_id="t1", content="file contents"),
        ])
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "user"
        assert result["tool_use_results"][0]["content"] == "file contents"
        assert result["tool_use_results"][0]["tool_use_id"] == "t1"

    def test_multi_block_assistant_all_captured(self):
        """Critical: ALL blocks in a multi-block message must be captured."""
        msg = MockAssistantMessage(content=[
            MockTextBlock(text="reading file"),
            MockToolUseBlock(id="t1", name="Read", input={"file_path": "x.py"}),
            MockTextBlock(text="done"),
        ])
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "assistant"
        content = result["message"]["content"]
        assert len(content) == 3
        assert content[0] == {"type": "text", "text": "reading file"}
        assert content[1]["type"] == "tool_use"
        assert content[1]["name"] == "Read"
        assert content[2] == {"type": "text", "text": "done"}

    def test_multi_tool_results(self):
        msg = MockAssistantMessage(content=[
            MockToolResultBlock(tool_use_id="t1", content="result 1"),
            MockToolResultBlock(tool_use_id="t2", content="result 2"),
        ])
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "user"
        assert len(result["tool_use_results"]) == 2
        assert result["tool_use_results"][0]["content"] == "result 1"
        assert result["tool_use_results"][1]["content"] == "result 2"

    def test_tool_result_none_content(self):
        msg = MockAssistantMessage(content=[
            MockToolResultBlock(tool_use_id="t1", content=None),
        ])
        result = SDKProcess._message_to_event(msg)
        assert result["tool_use_results"][0]["content"] == ""  # None -> ""

    def test_tool_result_list_content(self):
        msg = MockAssistantMessage(content=[
            MockToolResultBlock(
                tool_use_id="t1",
                content=[{"type": "text", "text": "line 1"}, {"type": "text", "text": "line 2"}],
            ),
        ])
        result = SDKProcess._message_to_event(msg)
        content = result["tool_use_results"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["text"] == "line 1"

    def test_result_message(self):
        msg = MockResultMessage(result="final answer", total_cost_usd=0.05)
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "result"
        assert result["result"] == "final answer"

    def test_stream_event_text_delta(self):
        msg = MockStreamEvent(event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"},
        })
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "content_block_delta"
        assert result["delta"]["text"] == "hello"

    def test_stream_event_thinking_delta(self):
        msg = MockStreamEvent(event={
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        })
        result = SDKProcess._message_to_event(msg)
        assert result["delta"]["thinking"] == "hmm"

    def test_stream_event_input_json_delta_dropped(self):
        msg = MockStreamEvent(event={
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"file'},
        })
        result = SDKProcess._message_to_event(msg)
        assert result is None

    def test_stream_event_tool_use_start(self):
        msg = MockStreamEvent(event={
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        })
        result = SDKProcess._message_to_event(msg)
        assert result["content_block"]["type"] == "tool_use"
        assert result["content_block"]["name"] == "Read"
        assert result["content_block"]["id"] == "t1"

    def test_stream_event_content_block_stop(self):
        msg = MockStreamEvent(event={"type": "content_block_stop"})
        result = SDKProcess._message_to_event(msg)
        assert result["type"] == "content_block_stop"

    def test_stream_event_ping_dropped(self):
        msg = MockStreamEvent(event={"type": "ping"})
        result = SDKProcess._message_to_event(msg)
        assert result is None

    def test_empty_content_list(self):
        msg = MockAssistantMessage(content=[])
        result = SDKProcess._message_to_event(msg)
        assert result is None

    def test_mixed_assistant_and_tool_result_blocks(self):
        """Edge case: message with both text and tool_result blocks."""
        msg = MockAssistantMessage(content=[
            MockTextBlock(text="some text"),
            MockToolResultBlock(tool_use_id="t1", content="result"),
        ])
        result = SDKProcess._message_to_event(msg)
        # tool_results takes precedence
        assert result["type"] == "user"
        assert len(result["tool_use_results"]) == 1


# ── _TOOL_ICONS ──────────────────────────────────────────────────────────


class TestToolIcons:
    def test_all_common_tools_have_icons(self):
        for tool in ["read", "edit", "write", "bash", "grep", "glob"]:
            assert tool in _TOOL_ICONS, f"Missing icon for {tool}"

    def test_default_fallback_works(self):
        # Unknown tools should use the default gear icon
        icon = _TOOL_ICONS.get("nonexistent", "&#9881;&#65039;")
        assert icon == "&#9881;&#65039;"


# ── JSON serialization (ensure events can be stored in DB) ───────────────


class TestEventSerialization:
    """Verify that events produced by _message_to_event can be JSON serialized."""

    def test_assistant_with_tool_use_serializes(self):
        msg = MockAssistantMessage(content=[
            MockToolUseBlock(id="t1", name="Read", input={"file_path": "test.py"}),
        ])
        event = SDKProcess._message_to_event(msg)
        # Must not raise
        serialized = json.dumps(event)
        assert "Read" in serialized

    def test_tool_result_with_list_content_serializes(self):
        msg = MockAssistantMessage(content=[
            MockToolResultBlock(
                tool_use_id="t1",
                content=[{"type": "text", "text": "hello"}],
            ),
        ])
        event = SDKProcess._message_to_event(msg)
        serialized = json.dumps(event)
        assert "hello" in serialized

    def test_tool_result_with_none_content_serializes(self):
        msg = MockAssistantMessage(content=[
            MockToolResultBlock(tool_use_id="t1", content=None),
        ])
        event = SDKProcess._message_to_event(msg)
        serialized = json.dumps(event)
        assert '"content": ""' in serialized

    def test_multi_block_message_serializes(self):
        msg = MockAssistantMessage(content=[
            MockTextBlock(text="hello"),
            MockToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            MockThinkingBlock(thinking="reasoning"),
        ])
        event = SDKProcess._message_to_event(msg)
        serialized = json.dumps(event)
        deserialized = json.loads(serialized)
        assert len(deserialized["message"]["content"]) == 3
