import unittest

from step_types import normalize_step_type


class StepTypeNormalizationTest(unittest.TestCase):
    def test_preserves_every_canonical_type(self):
        for step_type in ("source", "task", "destination", "flow", "subpipeline"):
            with self.subTest(step_type=step_type):
                self.assertEqual(step_type, normalize_step_type(step_type.upper()))

    def test_normalizes_known_pipeline_aliases(self):
        aliases = {
            "Data Source": "source",
            "processing step": "task",
            "feature-engineering": "task",
            "notification": "destination",
            "data store": "task",
            "database": "task",
            "integration": "task",
            "api-call": "task",
            "parameters": "task",
            "model-config": "task",
            "human approval": "flow",
            "nested pipeline": "subpipeline",
        }
        for value, expected in aliases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, normalize_step_type(value))

    def test_uses_keyword_fallbacks_for_agent_generated_labels(self):
        self.assertEqual("source", normalize_step_type("streaming_input_adapter"))
        self.assertEqual("destination", normalize_step_type("quality_report_writer"))
        self.assertEqual("task", normalize_step_type("external_endpoint_client"))

    def test_rejects_an_invalid_default(self):
        self.assertEqual("flow", normalize_step_type("unknown", default="flow"))
        self.assertEqual("task", normalize_step_type("unknown", default="invalid"))


if __name__ == "__main__":
    unittest.main()
