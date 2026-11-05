import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inlumen.contracts.port_types import (  # noqa: E402
    SPEC,
    artifact_port_type,
    check_connection,
    effective_port_type,
    file_port_ids,
    source_output_types,
)


class PortTypeSpecCasesTest(unittest.TestCase):
    """The browser runs the same cases (frontend/src/features/flow/portTypes.test.ts)."""

    def test_connection_checks(self):
        for case in SPEC["cases"]["check"]:
            with self.subTest(case=case):
                issue = check_connection(case["source"], case["target"])
                result = f"{issue.severity}:{issue.code}" if issue else None
                self.assertEqual(case["result"], result)

    def test_artifact_types(self):
        for case in SPEC["cases"]["artifact"]:
            with self.subTest(case=case):
                self.assertEqual(case["type"], artifact_port_type(case["artifact"]))

    def test_file_port_ids(self):
        for case in SPEC["cases"]["file_port_ids"]:
            self.assertEqual(case["ids"], file_port_ids(case["filenames"]))

    def test_source_outputs(self):
        for case in SPEC["cases"]["source_outputs"]:
            with self.subTest(case=case):
                self.assertEqual(
                    case["types"],
                    source_output_types(case["filenames"], case["files_as"], case["ports"]),
                )

    def test_effective_types(self):
        for case in SPEC["cases"]["effective"]:
            with self.subTest(case=case):
                self.assertEqual(case["type"], effective_port_type(case["declared"], case["inferred"]))


if __name__ == "__main__":
    unittest.main()
