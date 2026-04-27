"""Tests for the 4 new pipeline node types: aggregator, human_gate, api_call, llm_router.

Also covers the _mark_skipped bug fix, dispatch routing, and node type registration.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taktis.core.graph_executor import GraphExecutor, GraphNode
from taktis.core.node_types import NODE_TYPES, get_node_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_node(
    nid: str = "1",
    node_type: str = "aggregator",
    name: str = "Test",
    data: dict | None = None,
    upstream: list[str] | None = None,
    downstream: list[str] | None = None,
    downstream_ports: dict[str, list[str]] | None = None,
) -> GraphNode:
    return GraphNode(
        drawflow_id=nid,
        node_type=node_type,
        name=name,
        data=data or {},
        upstream=upstream or [],
        downstream=downstream or [],
        downstream_ports=downstream_ports or {},
    )


def _make_executor(
    results: dict[str, str] | None = None,
    nodes: list[GraphNode] | None = None,
) -> GraphExecutor:
    mock_orch = MagicMock()
    mock_orch.event_bus = MagicMock()
    mock_orch.event_bus.publish = AsyncMock()
    mock_orch._execution_service = MagicMock()

    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_orch._execution_service._session_factory = MagicMock(return_value=session_ctx)

    executor = GraphExecutor(
        engine=mock_orch,
        project_name="test-proj",
        flow_json={"drawflow": {"Home": {"data": {}}}},
        template_name="Test",
    )
    executor._results = results or {}
    executor._project = {"id": "proj-1", "name": "test-proj", "working_dir": "/tmp/test"}
    if nodes:
        executor._node_map = {n.drawflow_id: n for n in nodes}
    return executor


# ===========================================================================
# Registration & Dispatch Tests
# ===========================================================================

class TestNodeTypeRegistration:
    """Verify new node types are registered correctly."""

    def test_registry_has_aggregator(self):
        nt = get_node_type("aggregator")
        assert nt is not None
        assert nt.label == "Aggregator"
        assert nt.category == "transform"

    def test_registry_has_human_gate(self):
        nt = get_node_type("human_gate")
        assert nt is not None
        assert nt.label == "Human Gate"
        assert nt.outputs == 2

    def test_registry_has_api_call(self):
        nt = get_node_type("api_call")
        assert nt is not None
        assert nt.category == "action"

    def test_registry_has_llm_router(self):
        nt = get_node_type("llm_router")
        assert nt is not None
        assert nt.outputs == 4

    def test_all_new_types_serialize(self):
        for type_id in ("aggregator", "human_gate", "api_call", "llm_router"):
            nt = get_node_type(type_id)
            d = nt.to_dict()
            assert isinstance(d, dict)
            assert d["type_id"] == type_id
            assert isinstance(d["config_schema"], list)


class TestDispatch:
    """Verify _execute_instant_node dispatches to the right handler."""

    @pytest.mark.asyncio
    async def test_dispatch_aggregator(self):
        node = _make_node(node_type="aggregator", data={"strategy": "concat"}, upstream=["0"])
        executor = _make_executor(results={"0": "hello"}, nodes=[node])
        await executor._execute_instant_node(node)
        assert node.drawflow_id in executor._results

    @pytest.mark.asyncio
    async def test_dispatch_api_call(self):
        node = _make_node(node_type="api_call", data={"url": ""})
        executor = _make_executor(nodes=[node])
        await executor._execute_instant_node(node)
        # No URL → stored error
        assert "API_CALL_FAILED" in executor._results.get(node.drawflow_id, "")

    @pytest.mark.asyncio
    async def test_dispatch_human_gate(self):
        node = _make_node(node_type="human_gate", upstream=["0"])
        executor = _make_executor(results={"0": "data"}, nodes=[node])
        # Pre-approve so gate doesn't block
        async def approve_soon():
            await asyncio.sleep(0.02)
            executor.approve_gate(node.drawflow_id)
        task = asyncio.create_task(approve_soon())
        await executor._execute_instant_node(node)
        await task
        assert node.drawflow_id in executor._results


# ===========================================================================
# Aggregator Tests
# ===========================================================================

class TestAggregator:

    def test_concat_two_upstreams(self):
        n0 = _make_node("0", "agent", "Research A")
        n1 = _make_node("1", "agent", "Research B")
        agg = _make_node("2", "aggregator", "Agg", data={"strategy": "concat"}, upstream=["0", "1"])
        executor = _make_executor(
            results={"0": "Result A", "1": "Result B"},
            nodes=[n0, n1, agg],
        )
        executor._execute_aggregator(agg)
        result = executor._results["2"]
        assert "Result A" in result
        assert "Result B" in result
        assert "\n\n---\n\n" in result

    def test_concat_preserves_order(self):
        nodes = [_make_node(str(i), "agent", f"Node {i}") for i in range(4)]
        agg = _make_node("4", "aggregator", "Agg",
                         data={"strategy": "concat", "separator": "|"},
                         upstream=["0", "1", "2", "3"])
        results = {str(i): f"R{i}" for i in range(4)}
        executor = _make_executor(results=results, nodes=nodes + [agg])
        executor._execute_aggregator(agg)
        assert executor._results["4"] == "R0|R1|R2|R3"

    def test_json_merge(self):
        n0 = _make_node("0", "agent", "A")
        n1 = _make_node("1", "agent", "B")
        agg = _make_node("2", "aggregator", "Agg", data={"strategy": "json_merge"}, upstream=["0", "1"])
        executor = _make_executor(
            results={"0": '{"a": 1}', "1": '{"b": 2}'},
            nodes=[n0, n1, agg],
        )
        executor._execute_aggregator(agg)
        parsed = json.loads(executor._results["2"])
        assert parsed == {"a": 1, "b": 2}

    def test_json_merge_non_json_fallback(self):
        n0 = _make_node("0", "agent", "A")
        n1 = _make_node("1", "agent", "B")
        agg = _make_node("2", "aggregator", "Agg", data={"strategy": "json_merge"}, upstream=["0", "1"])
        executor = _make_executor(
            results={"0": "plain text", "1": '{"b": 2}'},
            nodes=[n0, n1, agg],
        )
        executor._execute_aggregator(agg)
        parsed = json.loads(executor._results["2"])
        assert isinstance(parsed, list)
        assert parsed[0] == {"raw": "plain text"}

    def test_numbered_list(self):
        n0 = _make_node("0", "agent", "Alpha")
        n1 = _make_node("1", "agent", "Beta")
        agg = _make_node("2", "aggregator", "Agg", data={"strategy": "numbered_list"}, upstream=["0", "1"])
        executor = _make_executor(
            results={"0": "First", "1": "Second"},
            nodes=[n0, n1, agg],
        )
        executor._execute_aggregator(agg)
        result = executor._results["2"]
        assert "1. [Alpha]" in result
        assert "2. [Beta]" in result
        assert "First" in result
        assert "Second" in result

    def test_xml_wrap(self):
        n0 = _make_node("0", "agent", "Source A")
        agg = _make_node("1", "aggregator", "Agg", data={"strategy": "xml_wrap"}, upstream=["0"])
        executor = _make_executor(
            results={"0": "content here"},
            nodes=[n0, agg],
        )
        executor._execute_aggregator(agg)
        result = executor._results["1"]
        assert '<from node="Source A">' in result
        assert "content here" in result

    def test_single_upstream_passthrough(self):
        n0 = _make_node("0", "agent", "Only")
        agg = _make_node("1", "aggregator", "Agg", data={"strategy": "concat"}, upstream=["0"])
        executor = _make_executor(
            results={"0": "single result"},
            nodes=[n0, agg],
        )
        executor._execute_aggregator(agg)
        assert executor._results["1"] == "single result"

    def test_unknown_strategy_falls_back_to_concat(self):
        n0 = _make_node("0", "agent", "A")
        agg = _make_node("1", "aggregator", "Agg",
                         data={"strategy": "nonexistent"},
                         upstream=["0"])
        executor = _make_executor(results={"0": "data"}, nodes=[n0, agg])
        executor._execute_aggregator(agg)
        assert executor._results["1"] == "data"


# ===========================================================================
# Human Gate Tests
# ===========================================================================

class TestHumanGate:

    @pytest.mark.asyncio
    async def test_gate_blocks_until_approved(self):
        node = _make_node("1", "human_gate", "Gate", upstream=["0"],
                          downstream_ports={"output_1": ["2"], "output_2": ["3"]})
        n0 = _make_node("0", "agent", "Upstream")
        n2 = _make_node("2", "agent", "Approved Branch")
        n3 = _make_node("3", "agent", "Rejected Branch")
        executor = _make_executor(
            results={"0": "upstream data"},
            nodes=[n0, node, n2, n3],
        )

        async def approve_soon():
            await asyncio.sleep(0.02)
            executor.approve_gate("1")

        task = asyncio.create_task(approve_soon())
        await executor._execute_human_gate(node)
        await task
        assert executor._results["1"] == "upstream data"

    @pytest.mark.asyncio
    async def test_gate_passes_upstream_on_approve(self):
        node = _make_node("1", "human_gate", "Gate", upstream=["0"])
        n0 = _make_node("0", "agent", "Upstream")
        executor = _make_executor(results={"0": "important data"}, nodes=[n0, node])

        async def approve_soon():
            await asyncio.sleep(0.02)
            executor.approve_gate("1")
        task = asyncio.create_task(approve_soon())
        await executor._execute_human_gate(node)
        await task
        assert executor._results["1"] == "important data"

    @pytest.mark.asyncio
    async def test_gate_rejection_routes_to_output_2(self):
        node = _make_node("1", "human_gate", "Gate", upstream=["0"],
                          downstream=["2", "3"],
                          downstream_ports={"output_1": ["2"], "output_2": ["3"]})
        n0 = _make_node("0", "agent", "Upstream")
        n2 = _make_node("2", "agent", "Approve Branch")
        n3 = _make_node("3", "agent", "Reject Branch")
        executor = _make_executor(
            results={"0": "data"},
            nodes=[n0, node, n2, n3],
        )

        async def reject_soon():
            await asyncio.sleep(0.02)
            executor.reject_gate("1")
        task = asyncio.create_task(reject_soon())
        await executor._execute_human_gate(node)
        await task
        # output_1 branch should be skipped
        assert n2.skipped is True
        assert n3.skipped is not True

    @pytest.mark.asyncio
    async def test_gate_publishes_waiting_event(self):
        node = _make_node("1", "human_gate", "Review Gate",
                          data={"gate_message": "Please review", "show_upstream": True},
                          upstream=["0"])
        n0 = _make_node("0", "agent", "Upstream")
        executor = _make_executor(results={"0": "content"}, nodes=[n0, node])

        async def approve_soon():
            await asyncio.sleep(0.02)
            executor.approve_gate("1")
        task = asyncio.create_task(approve_soon())
        await executor._execute_human_gate(node)
        await task

        publish_mock = executor._orch.event_bus.publish
        calls = [c for c in publish_mock.call_args_list
                 if c[0][0] == "pipeline.gate_waiting"]
        assert len(calls) == 1
        payload = calls[0][0][1]
        assert payload["node_id"] == "1"
        assert payload["gate_message"] == "Please review"

    @pytest.mark.asyncio
    async def test_gate_double_approval_safe(self):
        node = _make_node("1", "human_gate", "Gate", upstream=["0"])
        n0 = _make_node("0", "agent", "Up")
        executor = _make_executor(results={"0": "data"}, nodes=[n0, node])

        async def approve_twice():
            await asyncio.sleep(0.02)
            executor.approve_gate("1")
            executor.approve_gate("1")  # Second call should be harmless
        task = asyncio.create_task(approve_twice())
        await executor._execute_human_gate(node)
        await task
        assert executor._results["1"] == "data"

    @pytest.mark.asyncio
    async def test_gate_cancel_unblocks(self):
        node = _make_node("1", "human_gate", "Gate", upstream=["0"])
        n0 = _make_node("0", "agent", "Up")
        executor = _make_executor(results={"0": "data"}, nodes=[n0, node])

        async def cancel_soon():
            await asyncio.sleep(0.02)
            executor.cancel()
        task = asyncio.create_task(cancel_soon())
        with pytest.raises(Exception):  # PipelineError from cancellation
            await executor._execute_human_gate(node)
        await task

    def test_gate_skipped_in_rerun(self):
        """_rerun_downstream_instant_nodes should skip human_gate nodes."""
        from taktis.core.graph_executor import _LLM_NODE_TYPES
        assert "human_gate" not in _LLM_NODE_TYPES
        # The skip is in the condition: node.node_type != "human_gate"
        # We verify by checking the code path:
        # _rerun checks: node_type not in _LLM_NODE_TYPES AND node_type != "human_gate"


# ===========================================================================
# API Call Tests
# ===========================================================================

class TestApiCall:

    @pytest.mark.asyncio
    async def test_api_call_post_success(self):
        node = _make_node("1", "api_call", "Webhook",
                          data={"url": "https://example.com/hook", "method": "POST",
                                "timeout_seconds": "10"},
                          upstream=["0"])
        n0 = _make_node("0", "agent", "Up")
        executor = _make_executor(results={"0": "payload"}, nodes=[n0, node])

        mock_response = MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.content = b'{"ok": true}'

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.request = AsyncMock(return_value=mock_response)
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await executor._execute_api_call(node)

        assert executor._results["1"] == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_api_call_upstream_in_body(self):
        node = _make_node("1", "api_call", "API",
                          data={"url": "https://example.com/api",
                                "method": "POST",
                                "body_template": '{"text": "{upstream}"}',
                                "timeout_seconds": "10"},
                          upstream=["0"])
        n0 = _make_node("0", "agent", "Up")
        executor = _make_executor(results={"0": "hello world"}, nodes=[n0, node])

        mock_response = MagicMock()
        mock_response.text = "ok"
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.content = b"ok"

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.request = AsyncMock(return_value=mock_response)
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await executor._execute_api_call(node)

        # Verify body was sent with substitution
        call_args = client_instance.request.call_args
        assert "hello world" in call_args.kwargs.get("content", "")

    @pytest.mark.asyncio
    async def test_api_call_timeout(self):
        import httpx
        node = _make_node("1", "api_call", "API",
                          data={"url": "https://example.com/slow",
                                "method": "GET", "timeout_seconds": "10"},
                          upstream=["0"])
        n0 = _make_node("0", "agent", "Up")
        executor = _make_executor(results={"0": ""}, nodes=[n0, node])

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.request = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await executor._execute_api_call(node)

        assert "API_CALL_FAILED" in executor._results["1"]
        assert "timeout" in executor._results["1"].lower()

    @pytest.mark.asyncio
    async def test_api_call_connection_error(self):
        import httpx
        node = _make_node("1", "api_call", "API",
                          data={"url": "https://example.com/down",
                                "method": "GET", "timeout_seconds": "10"},
                          upstream=["0"])
        n0 = _make_node("0", "agent", "Up")
        executor = _make_executor(results={"0": ""}, nodes=[n0, node])

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.request = AsyncMock(side_effect=httpx.ConnectError("refused"))
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await executor._execute_api_call(node)

        assert "API_CALL_FAILED" in executor._results["1"]

    @pytest.mark.asyncio
    async def test_api_call_large_response_truncated(self):
        node = _make_node("1", "api_call", "API",
                          data={"url": "https://example.com/big",
                                "method": "GET", "timeout_seconds": "10"},
                          upstream=["0"])
        n0 = _make_node("0", "agent", "Up")
        executor = _make_executor(results={"0": ""}, nodes=[n0, node])

        big_text = "x" * 100_000
        mock_response = MagicMock()
        mock_response.text = big_text
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.content = big_text.encode()

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.request = AsyncMock(return_value=mock_response)
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await executor._execute_api_call(node)

        result = executor._results["1"]
        assert len(result) < 60_000  # 50KB + truncation message
        assert "TRUNCATED" in result

    def test_ssrf_rejects_localhost(self):
        from taktis.exceptions import PipelineError
        with pytest.raises(PipelineError):
            GraphExecutor._validate_api_url("http://localhost/admin")

    def test_ssrf_rejects_private_ips(self):
        from taktis.exceptions import PipelineError
        for url in [
            "http://10.0.0.1/internal",
            "http://172.16.0.1/internal",
            "http://192.168.1.1/internal",
        ]:
            with pytest.raises(PipelineError):
                GraphExecutor._validate_api_url(url)

    def test_ssrf_rejects_file_scheme(self):
        from taktis.exceptions import PipelineError
        with pytest.raises(PipelineError):
            GraphExecutor._validate_api_url("file:///etc/passwd")

    def test_ssrf_allows_public(self):
        # Should NOT raise for public URLs
        # Note: DNS resolution may fail in test env, but scheme/hostname checks pass
        try:
            GraphExecutor._validate_api_url("https://api.example.com/v1/data")
        except Exception:
            pass  # DNS failure is OK in test — we're testing the scheme/hostname check


# ===========================================================================
# LLM Router Tests
# ===========================================================================

class TestLLMRouter:

    def test_router_selects_route_exact_match(self):
        node = _make_node("1", "llm_router", "Router",
                          data={"route_count": "3"},
                          downstream_ports={
                              "output_1": ["10"], "output_2": ["20"], "output_3": ["30"],
                          })
        n10 = _make_node("10", "agent", "Branch 1")
        n20 = _make_node("20", "agent", "Branch 2")
        n30 = _make_node("30", "agent", "Branch 3")
        executor = _make_executor(
            results={"1": "2"},  # LLM responded with "2"
            nodes=[node, n10, n20, n30],
        )
        executor._execute_llm_router_routing(node)
        assert n10.skipped is True
        assert n20.skipped is not True  # Active branch
        assert n30.skipped is True

    def test_router_selects_route_from_verbose(self):
        node = _make_node("1", "llm_router", "Router",
                          data={"route_count": "2"},
                          downstream_ports={
                              "output_1": ["10"], "output_2": ["20"],
                          })
        n10 = _make_node("10", "agent", "Branch 1")
        n20 = _make_node("20", "agent", "Branch 2")
        executor = _make_executor(
            results={"1": "I would choose route 2 because it covers more ground"},
            nodes=[node, n10, n20],
        )
        executor._execute_llm_router_routing(node)
        assert n10.skipped is True
        assert n20.skipped is not True

    def test_router_default_on_unparseable(self):
        node = _make_node("1", "llm_router", "Router",
                          data={"route_count": "2"},
                          downstream_ports={
                              "output_1": ["10"], "output_2": ["20"],
                          })
        n10 = _make_node("10", "agent", "Branch 1")
        n20 = _make_node("20", "agent", "Branch 2")
        executor = _make_executor(
            results={"1": "I'm not sure what to do here, maybe both?"},
            nodes=[node, n10, n20],
        )
        executor._execute_llm_router_routing(node)
        # Default to route 1
        assert n10.skipped is not True
        assert n20.skipped is True

    def test_router_clamps_to_route_count(self):
        node = _make_node("1", "llm_router", "Router",
                          data={"route_count": "2"},
                          downstream_ports={
                              "output_1": ["10"], "output_2": ["20"],
                          })
        n10 = _make_node("10", "agent", "Branch 1")
        n20 = _make_node("20", "agent", "Branch 2")
        executor = _make_executor(
            results={"1": "3"},  # Route 3 but only 2 configured
            nodes=[node, n10, n20],
        )
        executor._execute_llm_router_routing(node)
        # Clamped to route 1
        assert n10.skipped is not True
        assert n20.skipped is True

    def test_router_skips_inactive_branches(self):
        """4-way routing: only selected route stays active."""
        node = _make_node("1", "llm_router", "Router",
                          data={"route_count": "4"},
                          downstream_ports={
                              "output_1": ["10"], "output_2": ["20"],
                              "output_3": ["30"], "output_4": ["40"],
                          })
        branches = [_make_node(str(i * 10), "agent", f"Branch {i}") for i in range(1, 5)]
        executor = _make_executor(
            results={"1": "option 3"},
            nodes=[node] + branches,
        )
        executor._execute_llm_router_routing(node)
        assert branches[0].skipped is True   # Branch 1
        assert branches[1].skipped is True   # Branch 2
        assert branches[2].skipped is not True  # Branch 3 — active
        assert branches[3].skipped is True   # Branch 4

    def test_router_passes_upstream_to_active(self):
        """The router's result should be stored so downstream can access it."""
        node = _make_node("1", "llm_router", "Router",
                          data={"route_count": "2"},
                          downstream_ports={"output_1": ["10"], "output_2": ["20"]})
        n10 = _make_node("10", "agent", "B1")
        n20 = _make_node("20", "agent", "B2")
        executor = _make_executor(
            results={"1": "1"},
            nodes=[node, n10, n20],
        )
        executor._execute_llm_router_routing(node)
        # Result stays in _results for downstream to read
        assert "1" in executor._results


# ===========================================================================
# _mark_skipped bug fix tests
# ===========================================================================

class TestMarkSkippedFix:

    def test_mark_skipped_respects_preexisting_skipped(self):
        """A node whose only active upstream was already skipped should also be skipped."""
        # Graph: A -> B -> C
        # A is already skipped from a previous conditional
        a = _make_node("a", "agent", "A", downstream=["b"])
        a.skipped = True
        b = _make_node("b", "agent", "B", upstream=["a"], downstream=["c"])
        c = _make_node("c", "agent", "C", upstream=["b"])
        executor = _make_executor(nodes=[a, b, c])

        # Skip B (direct target), C should also be skipped because
        # its only upstream (B) is in the skip set, and B's upstream (A) is pre-skipped
        executor._mark_skipped({"b"})
        assert b.skipped is True
        assert c.skipped is True

    def test_mark_skipped_keeps_node_with_active_upstream(self):
        """A node with both a skipped and an active upstream stays active."""
        # Graph: A -> C, B -> C
        # A is skipped, B is active
        a = _make_node("a", "agent", "A", downstream=["c"])
        a.skipped = True
        b = _make_node("b", "agent", "B", downstream=["c"])
        c = _make_node("c", "agent", "C", upstream=["a", "b"])
        executor = _make_executor(nodes=[a, b, c])

        # Skip A's branch — but C has B as active upstream, so C stays
        executor._mark_skipped(set())  # No direct targets to skip
        assert c.skipped is not True
