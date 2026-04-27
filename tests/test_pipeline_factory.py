"""Tests for taktis.core.pipeline_factory."""
import pytest

from taktis.core.pipeline_factory import (
    spec_to_drawflow,
    validate_drawflow,
    validate_spec,
)
from taktis.core.graph_executor import parse_drawflow_graph


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _simple_3_node_spec():
    """agent -> conditional -> 2 agents (one per branch)."""
    return {
        "name": "Test Pipeline",
        "description": "Simple branching test",
        "nodes": [
            {
                "id": "1",
                "type": "agent",
                "name": "Researcher",
                "config": {
                    "mode": "standard",
                    "model": "sonnet",
                    "expert": "architect-general",
                    "prompt": "Research the topic.",
                },
                "connections_to": ["2"],
            },
            {
                "id": "2",
                "type": "conditional",
                "name": "Check Depth",
                "config": {
                    "condition_type": "contains",
                    "condition_value": "deep",
                    "case_sensitive": False,
                },
                "connections_to": {
                    "output_1": ["3"],
                    "output_2": ["4"],
                },
            },
            {
                "id": "3",
                "type": "agent",
                "name": "Deep Dive",
                "config": {
                    "mode": "standard",
                    "model": "opus",
                    "prompt": "Do a deep dive.",
                },
                "connections_to": [],
            },
            {
                "id": "4",
                "type": "agent",
                "name": "Quick Summary",
                "config": {
                    "mode": "standard",
                    "model": "haiku",
                    "prompt": "Give a quick summary.",
                },
                "connections_to": [],
            },
        ],
    }


def _fan_out_aggregator_spec():
    """agent -> fan_out -> aggregator -> agent."""
    return {
        "name": "Fan Out Pipeline",
        "description": "Parallel processing with aggregation",
        "nodes": [
            {
                "id": "1",
                "type": "agent",
                "name": "Planner",
                "config": {
                    "mode": "standard",
                    "model": "sonnet",
                    "prompt": "List items to process.",
                },
                "connections_to": ["2"],
            },
            {
                "id": "2",
                "type": "fan_out",
                "name": "Parallel Workers",
                "config": {
                    "split_mode": "newline",
                    "prompt_template": "Process: {item}",
                    "model": "haiku",
                },
                "connections_to": ["3"],
            },
            {
                "id": "3",
                "type": "aggregator",
                "name": "Merge Results",
                "config": {
                    "strategy": "numbered_list",
                },
                "connections_to": ["4"],
            },
            {
                "id": "4",
                "type": "agent",
                "name": "Synthesizer",
                "config": {
                    "mode": "standard",
                    "model": "sonnet",
                    "prompt": "Synthesize all research.",
                },
                "connections_to": [],
            },
        ],
    }


# ---------------------------------------------------------------------------
# spec_to_drawflow: basic conversion
# ---------------------------------------------------------------------------

class TestSpecToDrawflow:
    """Test spec_to_drawflow conversion."""

    def test_simple_3_node_pipeline(self):
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)

        assert result["name"] == "Test Pipeline"
        assert result["description"] == "Simple branching test"
        assert result["is_default"] is False

        data = result["flow_json"]["drawflow"]["Home"]["data"]
        assert set(data.keys()) == {"1", "2", "3", "4"}

        # Node 1 should be agent type
        assert data["1"]["name"] == "agent"
        assert data["1"]["data"]["type_id"] == "agent"
        assert data["1"]["data"]["name"] == "Researcher"
        assert data["1"]["data"]["prompt"] == "Research the topic."

        # Node 1 output connects to node 2
        out_conns = data["1"]["outputs"]["output_1"]["connections"]
        assert len(out_conns) == 1
        assert out_conns[0]["node"] == "2"

        # Node 2 input connects back to node 1
        in_conns = data["2"]["inputs"]["input_1"]["connections"]
        assert len(in_conns) == 1
        assert in_conns[0]["node"] == "1"
        assert in_conns[0]["input"] == "output_1"

    def test_conditional_has_two_outputs(self):
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        cond = data["2"]
        assert "output_1" in cond["outputs"]
        assert "output_2" in cond["outputs"]

        # output_1 -> node 3, output_2 -> node 4
        assert cond["outputs"]["output_1"]["connections"][0]["node"] == "3"
        assert cond["outputs"]["output_2"]["connections"][0]["node"] == "4"

    def test_bidirectional_connections_conditional(self):
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        # Node 3 input should reference node 2 output_1
        in_conns = data["3"]["inputs"]["input_1"]["connections"]
        assert any(
            c["node"] == "2" and c["input"] == "output_1" for c in in_conns
        )

        # Node 4 input should reference node 2 output_2
        in_conns = data["4"]["inputs"]["input_1"]["connections"]
        assert any(
            c["node"] == "2" and c["input"] == "output_2" for c in in_conns
        )

    def test_fan_out_aggregator(self):
        spec = _fan_out_aggregator_spec()
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        assert len(data) == 4

        # fan_out node
        fan = data["2"]
        assert fan["name"] == "fan_out"
        assert fan["data"]["split_mode"] == "newline"

        # aggregator node
        agg = data["3"]
        assert agg["name"] == "aggregator"
        assert agg["data"]["strategy"] == "numbered_list"

    def test_default_config_filled(self):
        spec = {
            "name": "Minimal",
            "description": "",
            "nodes": [
                {
                    "id": "1",
                    "type": "agent",
                    "name": "Bare Agent",
                    "config": {},
                    "connections_to": [],
                }
            ],
        }
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]
        agent_data = data["1"]["data"]

        # Defaults from NODE_TYPES should be present
        assert agent_data["mode"] == "standard"
        assert agent_data["model"] == "sonnet"
        assert agent_data["inject_description"] is True
        assert agent_data["retry_transient"] is True

    def test_html_and_class(self):
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        for nid, node in data.items():
            assert node["html"] == ""
            assert node["typenode"] is False
            assert node["class"] == node["name"]

    def test_id_field_is_int(self):
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        for str_id, node in data.items():
            assert node["id"] == int(str_id)

    def test_empty_nodes_raises(self):
        with pytest.raises(ValueError, match="at least one node"):
            spec_to_drawflow({"name": "X", "nodes": []})

    def test_missing_nodes_raises(self):
        with pytest.raises(ValueError, match="'nodes' list"):
            spec_to_drawflow({"name": "X"})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be a dict"):
            spec_to_drawflow("not a dict")


# ---------------------------------------------------------------------------
# Auto-positioning
# ---------------------------------------------------------------------------

class TestAutoPositioning:
    """Test that topological layout produces reasonable coordinates."""

    def test_linear_chain_positions(self):
        """Nodes in a chain should have increasing pos_x."""
        spec = {
            "name": "Chain",
            "description": "",
            "nodes": [
                {"id": "1", "type": "agent", "name": "A", "config": {},
                 "connections_to": ["2"]},
                {"id": "2", "type": "agent", "name": "B", "config": {},
                 "connections_to": ["3"]},
                {"id": "3", "type": "agent", "name": "C", "config": {},
                 "connections_to": []},
            ],
        }
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        assert data["1"]["pos_x"] < data["2"]["pos_x"]
        assert data["2"]["pos_x"] < data["3"]["pos_x"]

    def test_parallel_nodes_same_x(self):
        """Nodes with the same upstream should share pos_x."""
        spec = {
            "name": "Parallel",
            "description": "",
            "nodes": [
                {"id": "1", "type": "agent", "name": "Start", "config": {},
                 "connections_to": ["2", "3"]},
                {"id": "2", "type": "agent", "name": "Branch A", "config": {},
                 "connections_to": []},
                {"id": "3", "type": "agent", "name": "Branch B", "config": {},
                 "connections_to": []},
            ],
        }
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        assert data["2"]["pos_x"] == data["3"]["pos_x"]
        assert data["2"]["pos_y"] != data["3"]["pos_y"]

    def test_layer_spacing(self):
        """Layers should be 350px apart."""
        spec = {
            "name": "Spacing",
            "description": "",
            "nodes": [
                {"id": "1", "type": "agent", "name": "A", "config": {},
                 "connections_to": ["2"]},
                {"id": "2", "type": "agent", "name": "B", "config": {},
                 "connections_to": []},
            ],
        }
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        assert data["1"]["pos_x"] == 50
        assert data["2"]["pos_x"] == 50 + 350

    def test_vertical_spacing(self):
        """Nodes within a layer should be 180px apart vertically."""
        spec = {
            "name": "Vertical",
            "description": "",
            "nodes": [
                {"id": "1", "type": "agent", "name": "Start", "config": {},
                 "connections_to": ["2", "3", "4"]},
                {"id": "2", "type": "agent", "name": "A", "config": {},
                 "connections_to": []},
                {"id": "3", "type": "agent", "name": "B", "config": {},
                 "connections_to": []},
                {"id": "4", "type": "agent", "name": "C", "config": {},
                 "connections_to": []},
            ],
        }
        result = spec_to_drawflow(spec)
        data = result["flow_json"]["drawflow"]["Home"]["data"]

        ys = sorted([data[nid]["pos_y"] for nid in ["2", "3", "4"]])
        assert ys[1] - ys[0] == 180
        assert ys[2] - ys[1] == 180


# ---------------------------------------------------------------------------
# validate_spec
# ---------------------------------------------------------------------------

class TestValidateSpec:
    """Test validate_spec catches problems."""

    def test_valid_spec_no_errors(self):
        errors = validate_spec(_simple_3_node_spec())
        assert errors == []

    def test_duplicate_ids(self):
        spec = {
            "name": "Bad",
            "nodes": [
                {"id": "1", "type": "agent", "name": "A", "config": {}},
                {"id": "1", "type": "agent", "name": "B", "config": {}},
            ],
        }
        errors = validate_spec(spec)
        assert any("Duplicate" in e for e in errors)

    def test_unknown_node_type(self):
        spec = {
            "name": "Bad",
            "nodes": [
                {"id": "1", "type": "magic_widget", "name": "X", "config": {}},
            ],
        }
        errors = validate_spec(spec)
        assert any("unknown type" in e for e in errors)

    def test_missing_connection_target(self):
        spec = {
            "name": "Bad",
            "nodes": [
                {"id": "1", "type": "agent", "name": "A", "config": {},
                 "connections_to": ["99"]},
            ],
        }
        errors = validate_spec(spec)
        assert any("does not exist" in e for e in errors)

    def test_port_exceeds_max(self):
        spec = {
            "name": "Bad",
            "nodes": [
                {
                    "id": "1",
                    "type": "conditional",
                    "name": "Cond",
                    "config": {"condition_type": "contains", "condition_value": "x"},
                    "connections_to": {
                        "output_1": ["2"],
                        "output_2": ["2"],
                        "output_3": ["2"],  # conditional only has 2 outputs
                    },
                },
                {"id": "2", "type": "agent", "name": "A", "config": {}},
            ],
        }
        errors = validate_spec(spec)
        assert any("exceeds max outputs" in e for e in errors)

    def test_cycle_detected(self):
        spec = {
            "name": "Cycle",
            "nodes": [
                {"id": "1", "type": "agent", "name": "A", "config": {},
                 "connections_to": ["2"]},
                {"id": "2", "type": "agent", "name": "B", "config": {},
                 "connections_to": ["1"]},
            ],
        }
        errors = validate_spec(spec)
        assert any("cycle" in e.lower() for e in errors)

    def test_empty_nodes(self):
        errors = validate_spec({"name": "X", "nodes": []})
        assert any("at least one node" in e for e in errors)

    def test_missing_type(self):
        spec = {
            "name": "Bad",
            "nodes": [
                {"id": "1", "name": "A", "config": {}},
            ],
        }
        errors = validate_spec(spec)
        assert any("missing 'type'" in e for e in errors)

    def test_not_a_dict(self):
        errors = validate_spec("string")
        assert errors == ["spec must be a dict"]

    def test_fan_out_aggregator_valid(self):
        errors = validate_spec(_fan_out_aggregator_spec())
        assert errors == []


# ---------------------------------------------------------------------------
# validate_drawflow
# ---------------------------------------------------------------------------

class TestValidateDrawflow:
    """Test validate_drawflow catches problems in Drawflow JSON."""

    def test_valid_drawflow_no_errors(self):
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        errors = validate_drawflow(result["flow_json"])
        assert errors == []

    def test_asymmetric_connection(self):
        """Manually break a back-reference to trigger asymmetric error."""
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        flow = result["flow_json"]

        # Remove the back-reference from node 2 to node 1
        data = flow["drawflow"]["Home"]["data"]
        data["2"]["inputs"]["input_1"]["connections"] = []

        errors = validate_drawflow(flow)
        assert any("Asymmetric" in e for e in errors)

    def test_missing_target_node(self):
        """Point output to a nonexistent node."""
        spec = {
            "name": "X",
            "description": "",
            "nodes": [
                {"id": "1", "type": "agent", "name": "A", "config": {},
                 "connections_to": []},
            ],
        }
        result = spec_to_drawflow(spec)
        flow = result["flow_json"]
        data = flow["drawflow"]["Home"]["data"]

        # Add a bogus connection
        data["1"]["outputs"]["output_1"]["connections"].append(
            {"node": "999", "output": "input_1"}
        )

        errors = validate_drawflow(flow)
        assert any("does not exist" in e for e in errors)

    def test_id_mismatch(self):
        spec = {
            "name": "X",
            "description": "",
            "nodes": [
                {"id": "1", "type": "agent", "name": "A", "config": {},
                 "connections_to": []},
            ],
        }
        result = spec_to_drawflow(spec)
        flow = result["flow_json"]
        data = flow["drawflow"]["Home"]["data"]

        # Corrupt the id field
        data["1"]["id"] = 99

        errors = validate_drawflow(flow)
        assert any("does not match" in e for e in errors)

    def test_missing_module_data(self):
        flow = {"drawflow": {"Home": {}}}
        errors = validate_drawflow(flow)
        assert any("missing 'data'" in e for e in errors)


# ---------------------------------------------------------------------------
# Round-trip: spec -> drawflow -> parse_drawflow_graph
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Ensure spec_to_drawflow output is parseable by graph_executor."""

    def test_simple_round_trip(self):
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        flow = result["flow_json"]

        nodes = parse_drawflow_graph(flow)
        assert len(nodes) == 4

        node_map = {n.drawflow_id: n for n in nodes}
        assert "1" in node_map
        assert "2" in node_map
        assert "3" in node_map
        assert "4" in node_map

        # Node 1 is upstream of node 2
        assert "2" in node_map["1"].downstream
        assert "1" in node_map["2"].upstream

        # Conditional routes
        assert "3" in node_map["2"].downstream
        assert "4" in node_map["2"].downstream

    def test_fan_out_round_trip(self):
        spec = _fan_out_aggregator_spec()
        result = spec_to_drawflow(spec)
        flow = result["flow_json"]

        nodes = parse_drawflow_graph(flow)
        assert len(nodes) == 4

        node_map = {n.drawflow_id: n for n in nodes}
        assert node_map["2"].node_type == "fan_out"
        assert node_map["3"].node_type == "aggregator"

        # Chain: 1 -> 2 -> 3 -> 4
        assert "2" in node_map["1"].downstream
        assert "3" in node_map["2"].downstream
        assert "4" in node_map["3"].downstream

    def test_drawflow_validates_after_round_trip(self):
        """Generated Drawflow should pass validate_drawflow."""
        for spec_fn in [_simple_3_node_spec, _fan_out_aggregator_spec]:
            spec = spec_fn()
            result = spec_to_drawflow(spec)
            errors = validate_drawflow(result["flow_json"])
            assert errors == [], f"Errors for {spec['name']}: {errors}"

    def test_conditional_port_routing_preserved(self):
        """Conditional downstream_ports should route output_1/output_2 correctly."""
        spec = _simple_3_node_spec()
        result = spec_to_drawflow(spec)
        flow = result["flow_json"]

        nodes = parse_drawflow_graph(flow)
        node_map = {n.drawflow_id: n for n in nodes}

        cond = node_map["2"]
        assert "3" in cond.downstream_ports.get("output_1", [])
        assert "4" in cond.downstream_ports.get("output_2", [])
