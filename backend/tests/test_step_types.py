import unittest

from step_types import normalize_step_type


class StepTypeNormalizationTest(unittest.TestCase):
    def test_preserves_every_canonical_type(self):
        for step_type in (
            "action",
            "input",
            "output",
            "config",
            "storage",
            "api",
            "custom",
        ):
            with self.subTest(step_type=step_type):
                self.assertEqual(step_type, normalize_step_type(step_type.upper()))

    def test_normalizes_known_pipeline_aliases(self):
        aliases = {
            "Data Source": "input",
            "feature-engineering": "action",
            "notification": "output",
            "database": "storage",
            "api-call": "api",
            "model-config": "config",
        }
        for value, expected in aliases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, normalize_step_type(value))

    def test_uses_keyword_fallbacks_for_agent_generated_labels(self):
        self.assertEqual("input", normalize_step_type("streaming_input_adapter"))
        self.assertEqual("output", normalize_step_type("quality_report_writer"))
        self.assertEqual("api", normalize_step_type("external_endpoint_client"))

    def test_rejects_an_invalid_default(self):
        self.assertEqual("custom", normalize_step_type("unknown", default="custom"))
        self.assertEqual("action", normalize_step_type("unknown", default="invalid"))


if __name__ == "__main__":
    unittest.main()
