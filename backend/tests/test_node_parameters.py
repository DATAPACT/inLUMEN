import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node_parameters import (  # noqa: E402
    is_sensitive_parameter_name,
    normalize_secret_param_keys,
    secret_params_json,
    without_secret_param_values,
)


class NodeParametersTest(unittest.TestCase):
    def test_detects_common_credential_names(self):
        self.assertTrue(is_sensitive_parameter_name("api_key"))
        self.assertTrue(is_sensitive_parameter_name("client-secret"))
        self.assertTrue(is_sensitive_parameter_name("access.token"))
        self.assertFalse(is_sensitive_parameter_name("threshold"))

    def test_infers_secrets_but_respects_an_explicit_visibility_choice(self):
        parameters = {"api_key": "secret", "threshold": 0.8}

        self.assertEqual(["api_key"], normalize_secret_param_keys(None, parameters))
        self.assertEqual([], normalize_secret_param_keys([], parameters))
        self.assertEqual(
            ["api_key"],
            normalize_secret_param_keys('["api_key", "missing"]', parameters),
        )
        self.assertEqual(["api_key"], json.loads(secret_params_json(None, parameters)))

    def test_removes_secret_values_before_graph_persistence(self):
        self.assertEqual(
            {"api_key": "", "threshold": 0.8},
            without_secret_param_values(
                {"api_key": "do-not-persist", "threshold": 0.8},
                ["api_key"],
            ),
        )


if __name__ == "__main__":
    unittest.main()
