import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node_ports import (  # noqa: E402
    default_input_port_id,
    default_output_port_id,
    normalize_node_ports,
    ports_json,
)


class NodePortsTest(unittest.TestCase):
    def test_uses_defaults_for_each_structural_kind(self):
        self.assertEqual([], normalize_node_ports(None, "source")["inputs"])
        self.assertEqual([], normalize_node_ports(None, "sink")["outputs"])
        self.assertEqual("input", normalize_node_ports(None, "task")["inputs"][0]["id"])

    def test_normalizes_explicit_ports_and_unique_ids(self):
        ports = normalize_node_ports(
            {
                "inputs": [{"id": "Audio Input", "label": "audio"}],
                "outputs": [
                    {"id": "Result", "label": "transcript", "data_type": "Document"},
                    {"id": "Result", "label": "confidence"},
                ],
            },
            "task",
        )

        self.assertEqual("audio-input", ports["inputs"][0]["id"])
        self.assertEqual("result", ports["outputs"][0]["id"])
        self.assertEqual("result-2", ports["outputs"][1]["id"])
        self.assertEqual("Document", ports["outputs"][0]["data_type"])

    def test_parses_and_serializes_json_at_the_storage_boundary(self):
        source = normalize_node_ports(
            json.dumps({"inputs": [{"id": "ignored"}], "outputs": []}),
            "source",
        )
        self.assertEqual({"inputs": [], "outputs": []}, source)
        self.assertEqual(source, json.loads(ports_json(source, "source")))

    def test_resolves_default_connection_handles(self):
        self.assertEqual("data", default_output_port_id("source"))
        self.assertEqual("input", default_input_port_id("task"))
        self.assertEqual("data", default_input_port_id("sink"))
        self.assertEqual("", default_output_port_id("sink"))


if __name__ == "__main__":
    unittest.main()
