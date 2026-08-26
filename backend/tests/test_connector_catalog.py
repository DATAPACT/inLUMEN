import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from connector_catalog import missing_connector_parameters, require_supported_connector


class ConnectorCatalogTest(unittest.TestCase):
    def test_registered_connector_parameters_are_authoritative(self):
        self.assertEqual(
            ["connection_url", "query"],
            missing_connector_parameters("source", "Database", {}),
        )
        self.assertEqual(
            [],
            missing_connector_parameters(
                "source",
                "Database",
                {"connection_url": "postgresql://db", "query": "select 1"},
            ),
        )

    def test_unimplemented_connector_is_rejected_before_bundle_generation(self):
        with self.assertRaisesRegex(ValueError, "not a registered managed source connector"):
            require_supported_connector("source", "Kafka")

    def test_open_rest_api_does_not_require_an_api_key(self):
        self.assertEqual(
            [],
            missing_connector_parameters(
                "source",
                "REST API",
                {"url": "https://example.test/weather"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
