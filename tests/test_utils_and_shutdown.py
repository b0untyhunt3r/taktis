"""Tests for taktis.utils.parse_json_field and Taktis.shutdown().

parse_json_field: full coverage of type dispatch + JSON parsing + error paths.
shutdown: verifies graceful teardown sequence, None-safety, and idempotency.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.utils import parse_json_field


# ===========================================================================
# parse_json_field
# ===========================================================================


class TestParseJsonFieldNone:
    """None input returns the default value."""

    def test_none_returns_implicit_default(self):
        assert parse_json_field(None) is None

    def test_none_returns_custom_default(self):
        sentinel = {"fallback": True}
        assert parse_json_field(None, default=sentinel) is sentinel

    def test_none_returns_empty_list_default(self):
        assert parse_json_field(None, default=[]) == []


class TestParseJsonFieldPassthrough:
    """list and dict inputs are returned as-is (no re-parsing)."""

    def test_list_passthrough(self):
        data = [1, 2, 3]
        result = parse_json_field(data)
        assert result is data  # exact same object

    def test_dict_passthrough(self):
        data = {"key": "value"}
        result = parse_json_field(data)
        assert result is data

    def test_empty_list_passthrough(self):
        data = []
        result = parse_json_field(data)
        assert result is data

    def test_empty_dict_passthrough(self):
        data = {}
        result = parse_json_field(data)
        assert result is data

    def test_nested_dict_passthrough(self):
        data = {"a": {"b": [1, 2]}}
        result = parse_json_field(data)
        assert result is data


class TestParseJsonFieldValidJSON:
    """Valid JSON strings are parsed correctly."""

    def test_json_object_string(self):
        result = parse_json_field('{"name": "test"}')
        assert result == {"name": "test"}

    def test_json_array_string(self):
        result = parse_json_field('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_json_nested_object(self):
        data = {"phases": [{"name": "p1", "tasks": []}], "version": 2}
        result = parse_json_field(json.dumps(data))
        assert result == data

    def test_json_string_null(self):
        """json.loads("null") returns Python None."""
        result = parse_json_field("null")
        assert result is None

    def test_json_string_true(self):
        assert parse_json_field("true") is True

    def test_json_string_false(self):
        assert parse_json_field("false") is False

    def test_json_string_number(self):
        assert parse_json_field("42") == 42

    def test_json_string_float(self):
        assert parse_json_field("3.14") == pytest.approx(3.14)

    def test_json_string_quoted(self):
        """A JSON-encoded string (with quotes) parses to a Python string."""
        assert parse_json_field('"hello"') == "hello"


class TestParseJsonFieldInvalid:
    """Invalid or unparseable inputs return the default."""

    def test_invalid_json_string(self):
        assert parse_json_field("{bad json}") is None

    def test_empty_string(self):
        assert parse_json_field("") is None

    def test_empty_string_custom_default(self):
        assert parse_json_field("", default="FALLBACK") == "FALLBACK"

    def test_plain_text(self):
        assert parse_json_field("not json at all") is None

    def test_truncated_json(self):
        assert parse_json_field('{"key": "val') is None

    def test_integer_input_triggers_type_error(self):
        """An int is not str/list/dict/None, so json.loads(int) raises TypeError."""
        result = parse_json_field(42, default="NOPE")  # type: ignore[arg-type]
        assert result == "NOPE"

    def test_float_input_triggers_type_error(self):
        result = parse_json_field(3.14, default="NOPE")  # type: ignore[arg-type]
        assert result == "NOPE"

    def test_bool_input_triggers_type_error(self):
        result = parse_json_field(True, default="NOPE")  # type: ignore[arg-type]
        assert result == "NOPE"


# ===========================================================================
# Taktis.shutdown()
# ===========================================================================


def _make_initialized_engine():
    """Create an Taktis with mocked internals simulating post-initialize."""
    from taktis.core.engine import Taktis

    orch = Taktis()
    orch._initialized = True

    orch.process_manager = MagicMock()
    orch.process_manager.stop_all = AsyncMock()

    orch.state_tracker = MagicMock()
    orch.state_tracker.stop = AsyncMock()

    orch.event_bus = MagicMock()

    return orch


class TestShutdownNotInitialized:
    """shutdown() is a no-op when _initialized is False."""

    @pytest.mark.asyncio
    async def test_shutdown_when_not_initialized(self):
        from taktis.core.engine import Taktis

        orch = Taktis()
        assert orch._initialized is False

        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock) as mock_close:
            await orch.shutdown()

        # Nothing should have been called
        mock_close.assert_not_called()
        assert orch._initialized is False


class TestShutdownCallSequence:
    """shutdown() calls stop_all, stop, close_pool, clears event_bus, sets flag."""

    @pytest.mark.asyncio
    async def test_calls_process_manager_stop_all(self):
        orch = _make_initialized_engine()
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock):
            await orch.shutdown()
        orch.process_manager.stop_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_calls_state_tracker_stop(self):
        orch = _make_initialized_engine()
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock):
            await orch.shutdown()
        orch.state_tracker.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_calls_close_pool(self):
        orch = _make_initialized_engine()
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock) as mock_close:
            await orch.shutdown()
        mock_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sets_initialized_false(self):
        orch = _make_initialized_engine()
        assert orch._initialized is True
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock):
            await orch.shutdown()
        assert orch._initialized is False

    @pytest.mark.asyncio
    async def test_calls_event_bus_clear(self):
        orch = _make_initialized_engine()
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock):
            await orch.shutdown()
        orch.event_bus.clear.assert_called_once()


class TestShutdownNoneSafety:
    """shutdown() handles None process_manager and state_tracker gracefully."""

    @pytest.mark.asyncio
    async def test_process_manager_none(self):
        orch = _make_initialized_engine()
        orch.process_manager = None
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock):
            await orch.shutdown()  # should not raise
        assert orch._initialized is False

    @pytest.mark.asyncio
    async def test_state_tracker_none(self):
        orch = _make_initialized_engine()
        orch.state_tracker = None
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock):
            await orch.shutdown()  # should not raise
        assert orch._initialized is False

    @pytest.mark.asyncio
    async def test_both_none(self):
        orch = _make_initialized_engine()
        orch.process_manager = None
        orch.state_tracker = None
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock) as mock_close:
            await orch.shutdown()
        mock_close.assert_awaited_once()
        orch.event_bus.clear.assert_called_once()
        assert orch._initialized is False


class TestShutdownIdempotency:
    """Calling shutdown() twice is safe -- second call is a no-op."""

    @pytest.mark.asyncio
    async def test_double_shutdown(self):
        orch = _make_initialized_engine()
        with patch("taktis.core.engine.close_pool", new_callable=AsyncMock) as mock_close:
            await orch.shutdown()
            assert orch._initialized is False

            # Reset mocks to verify second call does nothing
            orch.process_manager.stop_all.reset_mock()
            orch.state_tracker.stop.reset_mock()
            mock_close.reset_mock()
            orch.event_bus.clear.reset_mock()

            await orch.shutdown()

        # Second shutdown must not call anything (early return)
        orch.process_manager.stop_all.assert_not_awaited()
        orch.state_tracker.stop.assert_not_awaited()
        mock_close.assert_not_called()
        orch.event_bus.clear.assert_not_called()
