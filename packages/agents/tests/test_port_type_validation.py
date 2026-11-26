import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inlumen.contracts.pipeline_graph_validation import validate_pipeline_graph  # noqa: E402


def _node(node_id, kind, **data):
    return {"id": node_id, "data": {"type": kind, "label": node_id, **data}}


def _edge(source, source_port, target, target_port):
    return {"id": f"{source}-{target}", "source": source, "sourceHandle": source_port,
            "target": target, "targetHandle": target_port}


def _port_issues(nodes, edges):
    report = validate_pipeline_graph({"nodes": nodes, "edges": edges})
    return [(issue["severity"], issue["code"]) for issue in report["issues"] if issue["category"] == "ports" and issue.get("edge_id")]


TABLE_INPUT = {"inputs": [{"id": "input", "type": "table"}], "outputs": [{"id": "output"}]}


class PortTypeValidationTest(unittest.TestCase):
    def test_collection_from_multi_file_source_into_single_table_input_warns(self):
        issues = _port_issues(
            [_node("src", "source", files=[{"filename": "a.csv"}, {"filename": "b.csv"}]),
             _node("t", "task", ports=TABLE_INPUT)],
            [_edge("src", "data", "t", "input")],
        )
        self.assertEqual([("warning", "collection-into-single")], issues)

    def test_kind_mismatch_inferred_from_declared_artifact_is_an_error(self):
        issues = _port_issues(
            [_node("a", "task", param={"output_artifacts": [{"name": "r", "filename": "r.json"}]}),
             _node("t", "task", ports=TABLE_INPUT)],
            [_edge("a", "output", "t", "input")],
        )
        self.assertEqual([("error", "incompatible-port-types")], issues)

    def test_separate_source_files_type_each_port(self):
        source = _node(
            "src", "source",
            param={"files_as": "separate"},
            ports={"outputs": [{"id": "patients"}, {"id": "scan"}]},
            files=[{"filename": "patients.csv"}, {"filename": "scan.png"}],
        )
        self.assertEqual([], _port_issues(
            [source, _node("t", "task", ports=TABLE_INPUT)], [_edge("src", "patients", "t", "input")],
        ))
        self.assertEqual([("error", "incompatible-port-types")], _port_issues(
            [source, _node("t", "task", ports=TABLE_INPUT)], [_edge("src", "scan", "t", "input")],
        ))

    def test_untyped_ports_stay_permissive(self):
        issues = _port_issues(
            [_node("src", "source", files=[{"filename": "a.csv"}, {"filename": "b.png"}]),
             _node("t", "task")],
            [_edge("src", "data", "t", "input")],
        )
        self.assertEqual([], issues)



def _issue_codes(nodes, edges):
    report = validate_pipeline_graph({"nodes": nodes, "edges": edges})
    return [issue["code"] for issue in report["issues"]]


class SharedInputPortTest(unittest.TestCase):
    def test_parallel_branches_on_one_input_port_are_an_error(self):
        nodes = [
            _node("1", "source"), _node("2", "source"),
            _node("3", "task"), _node("4", "task"), _node("5", "task"),
            _node("6", "destination"),
        ]
        edges = [
            _edge("1", "data", "3", "input"), _edge("2", "data", "4", "input"),
            _edge("3", "output", "5", "input"), _edge("4", "output", "5", "input"),
            _edge("5", "output", "6", "data"),
        ]
        self.assertIn("shared-input-port", _issue_codes(nodes, edges))

    def test_named_inputs_join_parallel_branches(self):
        join = {"inputs": [{"id": "customers"}, {"id": "orders"}], "outputs": [{"id": "output"}]}
        nodes = [
            _node("1", "source"), _node("2", "source"),
            _node("3", "task"), _node("4", "task"), _node("5", "task", ports=join),
            _node("6", "destination"),
        ]
        edges = [
            _edge("1", "data", "3", "input"), _edge("2", "data", "4", "input"),
            _edge("3", "output", "5", "customers"), _edge("4", "output", "5", "orders"),
            _edge("5", "output", "6", "data"),
        ]
        self.assertNotIn("shared-input-port", _issue_codes(nodes, edges))

    def test_alternative_condition_branches_may_rejoin_one_port(self):
        nodes = [
            _node("1", "source"),
            {"id": "2", "data": {"type": "flow", "label": "Check", "template_label": "Condition",
                                 "param": {"expression": "value.ok == true"}}},
            _node("3", "task"), _node("4", "task"), _node("5", "destination"),
        ]
        edges = [
            _edge("1", "data", "2", "value"),
            _edge("2", "when_true", "3", "input"), _edge("2", "when_false", "4", "input"),
            _edge("3", "output", "5", "data"), _edge("4", "output", "5", "data"),
        ]
        self.assertNotIn("shared-input-port", _issue_codes(nodes, edges))


if __name__ == "__main__":
    unittest.main()
