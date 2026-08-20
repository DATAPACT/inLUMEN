import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_agent.contract import (  # noqa: E402
    default_input_port_expression,
    default_output_port_expression,
    normalize_agent_implementation,
    require_agent_step_type,
    validate_insertion_kind,
)


class PipelineAgentContractTest(unittest.TestCase):
    def test_accepts_only_the_five_palette_component_types(self):
        self.assertEqual("task", require_agent_step_type("task"))

        with self.assertRaisesRegex(ValueError, "available Pipeline Components"):
            require_agent_step_type("model-training")

    def test_rejects_structurally_invalid_insertions(self):
        validate_insertion_kind("source", initial=True)
        validate_insertion_kind("task", initial=False)
        validate_insertion_kind("flow", initial=False)
        validate_insertion_kind("subpipeline", initial=False)

        with self.assertRaisesRegex(ValueError, "initial insertion"):
            validate_insertion_kind("task", initial=True)
        with self.assertRaisesRegex(ValueError, "inserted between"):
            validate_insertion_kind("destination", initial=False)

    def test_port_fallbacks_cover_legacy_and_configured_components(self):
        input_expression = default_input_port_expression("node")
        output_expression = default_output_port_expression("node")

        self.assertIn("node.primary_input_port", input_expression)
        self.assertIn("THEN 'value'", input_expression)
        self.assertIn("THEN 'items'", input_expression)
        self.assertIn("node.primary_output_port", output_expression)
        self.assertIn("THEN 'when_true'", output_expression)
        self.assertIn("THEN 'item'", output_expression)

    def test_tasks_only_accept_python_implementation_packages(self):
        self.assertEqual(
            {"kind": "generated-code"},
            normalize_agent_implementation({"kind": "generated-code"}),
        )
        self.assertEqual(
            {"kind": "python", "language": "python"},
            normalize_agent_implementation({"kind": "python", "language": "python"}),
        )
        with self.assertRaisesRegex(ValueError, "generated-code.*python"):
            normalize_agent_implementation({"kind": "container"})


if __name__ == "__main__":
    unittest.main()
