"""Tests for taktis.core.planner — JSON repair and plan parsing."""

from __future__ import annotations

import json

import pytest

from taktis.core.planner import (
    _auto_assign_waves,
    _repair_json,
    parse_plan_output,
)


# ======================================================================
# _repair_json
# ======================================================================


class TestRepairJson:
    """Tests for _repair_json — fix common LLM JSON issues."""

    def test_valid_json_passes_through(self):
        valid = '{"name": "test", "count": 42}'
        assert json.loads(_repair_json(valid)) == {"name": "test", "count": 42}

    def test_unescaped_newline_in_string(self):
        bad = '{"prompt": "line one\nline two"}'
        repaired = _repair_json(bad)
        parsed = json.loads(repaired)
        assert parsed["prompt"] == "line one\nline two"

    def test_unescaped_tab_in_string(self):
        bad = '{"prompt": "col1\tcol2"}'
        repaired = _repair_json(bad)
        parsed = json.loads(repaired)
        assert parsed["prompt"] == "col1\tcol2"

    def test_unescaped_carriage_return_in_string(self):
        bad = '{"prompt": "line\rone"}'
        repaired = _repair_json(bad)
        parsed = json.loads(repaired)
        assert parsed["prompt"] == "line\rone"

    def test_already_escaped_sequences_preserved(self):
        good = '{"path": "C:\\\\Users\\\\admin"}'
        repaired = _repair_json(good)
        parsed = json.loads(repaired)
        assert parsed["path"] == "C:\\Users\\admin"

    def test_mixed_escaped_and_unescaped(self):
        bad = '{"text": "escaped\\nnewline and raw\nnewline"}'
        repaired = _repair_json(bad)
        parsed = json.loads(repaired)
        assert parsed["text"] == "escaped\nnewline and raw\nnewline"

    def test_trailing_comma_in_object(self):
        bad = '{"a": 1, "b": 2,}'
        repaired = _repair_json(bad)
        assert json.loads(repaired) == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        bad = '{"items": [1, 2, 3,]}'
        repaired = _repair_json(bad)
        assert json.loads(repaired) == {"items": [1, 2, 3]}

    def test_trailing_comma_with_whitespace(self):
        bad = '{"a": 1 ,  \n}'
        repaired = _repair_json(bad)
        assert json.loads(repaired) == {"a": 1}

    def test_nested_objects(self):
        bad = '{"outer": {"inner": "has\nnewline",}}'
        repaired = _repair_json(bad)
        parsed = json.loads(repaired)
        assert parsed["outer"]["inner"] == "has\nnewline"

    def test_empty_object(self):
        assert json.loads(_repair_json("{}")) == {}

    def test_empty_string_value(self):
        assert json.loads(_repair_json('{"k": ""}')) == {"k": ""}

    def test_multiple_strings_with_newlines(self):
        bad = '{"a": "line\none", "b": "line\ntwo"}'
        repaired = _repair_json(bad)
        parsed = json.loads(repaired)
        assert parsed["a"] == "line\none"
        assert parsed["b"] == "line\ntwo"

    def test_large_json_performance(self):
        """Ensure repair handles large inputs without hanging."""
        items = ", ".join(f'{{"id": {i}, "text": "item {i}"}}' for i in range(500))
        big = f'{{"phases": [{items}]}}'
        repaired = _repair_json(big)
        parsed = json.loads(repaired)
        assert len(parsed["phases"]) == 500


# ======================================================================
# parse_plan_output
# ======================================================================


class TestParsePlanOutput:
    """Tests for parse_plan_output — extracting JSON plans from LLM text."""

    def test_extracts_json_block(self):
        text = 'Here is the plan:\n```json\n{"phases": [{"name": "P1", "tasks": []}]}\n```'
        result = parse_plan_output(text)
        assert result is not None
        assert result["phases"][0]["name"] == "P1"

    def test_returns_none_for_empty(self):
        assert parse_plan_output("") is None
        assert parse_plan_output(None) is None

    def test_returns_none_for_no_json(self):
        assert parse_plan_output("Just some text without JSON") is None

    def test_returns_none_for_json_without_phases(self):
        text = '```json\n{"name": "not a plan"}\n```'
        assert parse_plan_output(text) is None

    def test_uses_last_json_block(self):
        text = (
            '```json\n{"phases": [{"name": "old"}]}\n```\n'
            'Updated plan:\n'
            '```json\n{"phases": [{"name": "new"}]}\n```'
        )
        result = parse_plan_output(text)
        assert result["phases"][0]["name"] == "new"

    def test_repairs_broken_json(self):
        bad_json = '{"phases": [{"name": "P1", "tasks": [],}]}'
        text = f"```json\n{bad_json}\n```"
        result = parse_plan_output(text)
        assert result is not None
        assert result["phases"][0]["name"] == "P1"

    def test_embedded_backticks_in_json(self):
        """Plan JSON that contains triple-backtick code examples in prompts."""
        plan = {
            "phases": [
                {
                    "name": "Phase 1",
                    "tasks": [
                        {
                            "name": "Write code",
                            "prompt": "Create:\n```python\ndef foo():\n    pass\n```\nDone.",
                        }
                    ],
                }
            ]
        }
        text = f"Here is the plan:\n```json\n{json.dumps(plan, indent=2)}\n```\nEnd."
        result = parse_plan_output(text)
        assert result is not None
        assert result["phases"][0]["name"] == "Phase 1"
        assert "```python" in result["phases"][0]["tasks"][0]["prompt"]

    def test_fallback_to_raw_json_object(self):
        text = 'Some text before {"phases": [{"name": "inline"}]} and after'
        result = parse_plan_output(text)
        assert result is not None
        assert result["phases"][0]["name"] == "inline"


# ======================================================================
# _auto_assign_waves
# ======================================================================


class TestAutoAssignWaves:
    """Tests for _auto_assign_waves — file-overlap wave grouping."""

    def test_no_overlap_same_wave(self):
        tasks = [
            {"prompt": "Work on foo.py", "wave": 1},
            {"prompt": "Work on bar.py", "wave": 1},
        ]
        result = _auto_assign_waves(tasks)
        assert result[0]["wave"] == result[1]["wave"]

    def test_file_overlap_different_waves(self):
        tasks = [
            {"prompt": "Edit main.py to add feature", "wave": 1},
            {"prompt": "Refactor main.py for cleanup", "wave": 1},
        ]
        result = _auto_assign_waves(tasks)
        assert result[0]["wave"] != result[1]["wave"]

    def test_empty_tasks(self):
        assert _auto_assign_waves([]) == []

    def test_single_task(self):
        tasks = [{"prompt": "Do something with app.py", "wave": 1}]
        result = _auto_assign_waves(tasks)
        assert len(result) == 1
        assert result[0]["wave"] == 1

    def test_preserves_task_data(self):
        tasks = [
            {"prompt": "Work on foo.py", "wave": 1, "expert": "implementer"},
        ]
        result = _auto_assign_waves(tasks)
        assert result[0]["expert"] == "implementer"

    def test_shared_read_input_does_not_split_fan_out(self):
        # Fan-out tasks that all READ a shared input (e.g. shared/content.json)
        # but WRITE to disjoint paths must stay in the same wave.
        shared = "INPUTS: shared/content.json\n\n"
        tasks = [
            {
                "prompt": shared + "FILES TO CREATE: design-01/index.html\n",
                "wave": 1,
            },
            {
                "prompt": shared + "FILES TO CREATE: design-02/index.html\n",
                "wave": 1,
            },
            {
                "prompt": shared + "FILES TO CREATE: design-03/index.html\n",
                "wave": 1,
            },
        ]
        result = _auto_assign_waves(tasks)
        assert [t["wave"] for t in result] == [1, 1, 1]

    def test_write_section_collision_still_bumps(self):
        # Two tasks both declaring the SAME write file must be serialized.
        tasks = [
            {"prompt": "FILES TO CREATE: app.py\n", "wave": 1},
            {"prompt": "FILES TO WRITE: app.py\n", "wave": 1},
        ]
        result = _auto_assign_waves(tasks)
        assert result[0]["wave"] != result[1]["wave"]
