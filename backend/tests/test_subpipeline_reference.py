import unittest

from subpipeline_reference import (
    derive_subpipeline_interface,
    missing_explicit_port_contracts,
    normalize_reusable_pipeline_graph,
    plan_subpipeline_port_migration,
)


class SubpipelineReferenceTest(unittest.TestCase):
    def test_port_migration_keeps_stable_compatible_ids(self):
        previous = {"inputs": [{"id": "audio", "type": "Audio"}], "outputs": []}
        following = {"inputs": [{"id": "audio", "type": "Audio"}], "outputs": []}

        result = plan_subpipeline_port_migration(previous, following, ["audio"], [])

        self.assertTrue(result["compatible"])
        self.assertEqual({"audio": "audio"}, result["input_mapping"])

    def test_port_migration_maps_an_unambiguous_rename(self):
        previous = {"inputs": [], "outputs": [{"id": "analysis", "name": "Result", "type": "Object"}]}
        following = {"inputs": [], "outputs": [{"id": "result", "name": "Result", "type": "Object"}]}

        result = plan_subpipeline_port_migration(previous, following, [], ["analysis"])

        self.assertTrue(result["compatible"])
        self.assertEqual({"analysis": "result"}, result["output_mapping"])

    def test_port_migration_requires_a_choice_when_targets_are_ambiguous(self):
        previous = {"inputs": [{"id": "record", "type": "Object"}], "outputs": []}
        following = {
            "inputs": [
                {"id": "primary", "type": "Object"},
                {"id": "secondary", "type": "Object"},
            ],
            "outputs": [],
        }

        result = plan_subpipeline_port_migration(previous, following, ["record"], [])

        self.assertFalse(result["compatible"])
        self.assertEqual("record", result["conflicts"][0]["port"])
        self.assertEqual(["primary", "secondary"], [item["id"] for item in result["conflicts"][0]["candidates"]])

    def test_requested_port_mapping_resolves_an_ambiguous_contract(self):
        previous = {"inputs": [{"id": "record", "type": "Object"}], "outputs": []}
        following = {
            "inputs": [
                {"id": "primary", "type": "Object"},
                {"id": "secondary", "type": "Object"},
            ],
            "outputs": [],
        }

        result = plan_subpipeline_port_migration(
            previous,
            following,
            ["record"],
            [],
            requested_inputs={"record": "secondary"},
        )

        self.assertTrue(result["compatible"])
        self.assertEqual({"record": "secondary"}, result["input_mapping"])

    def test_normalizes_compact_agent_snapshot_for_the_pipeline_editor(self):
        graph = {
            "nodes": [
                {"id": "audio", "type": "source", "label": "Audio Input", "template": "Audio Recording"},
                {
                    "id": "transcribe",
                    "type": "task",
                    "label": "Transcription",
                    "template": "Speech-to-Text",
                    "implementation": {"kind": "generated-code", "task": "speech-to-text"},
                },
                {"id": "output", "type": "destination", "label": "Analysis Output"},
            ],
            "edges": [
                {"source": "audio", "source_port": "data", "target": "transcribe", "target_port": "input"},
                {"source": "transcribe", "source_port": "output", "target": "output", "target_port": "data"},
            ],
        }

        normalized = normalize_reusable_pipeline_graph(graph)

        self.assertEqual(3, len(normalized["nodes"]))
        self.assertEqual("custom", normalized["nodes"][0]["type"])
        self.assertEqual("source", normalized["nodes"][0]["data"]["type"])
        self.assertEqual({"x": 280.0, "y": 120.0}, normalized["nodes"][1]["position"])
        self.assertEqual("input", normalized["edges"][0]["targetHandle"])
        self.assertEqual("output", normalized["edges"][1]["sourceHandle"])
        self.assertEqual(["audio", "transcribe", "output"], missing_explicit_port_contracts(graph))

    def test_derives_contract_from_normalized_explicit_boundaries(self):
        graph = normalize_reusable_pipeline_graph({
            "nodes": [
                {
                    "id": "audio",
                    "type": "source",
                    "label": "Audio Input",
                    "ports": {"inputs": [], "outputs": [{"id": "audio", "type": "Audio"}]},
                },
                {
                    "id": "output",
                    "type": "destination",
                    "label": "Analysis Output",
                    "ports": {"inputs": [{"id": "analysis", "type": "Object"}], "outputs": []},
                },
            ],
            "edges": [{"source": "audio", "source_port": "audio", "target": "output", "target_port": "analysis"}],
        })

        interface = derive_subpipeline_interface(graph)

        self.assertEqual("Audio", interface["inputs"][0]["type"])
        self.assertEqual({"node": "audio", "port": "audio"}, interface["inputs"][0]["internal"])
        self.assertEqual("Object", interface["outputs"][0]["type"])
        self.assertEqual([], missing_explicit_port_contracts(graph))


if __name__ == "__main__":
    unittest.main()
