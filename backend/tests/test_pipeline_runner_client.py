import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline_runner_client  # noqa: E402


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class PipelineRunnerClientTest(unittest.TestCase):
    @patch.object(pipeline_runner_client, "RUNNER_SERVICE_API_KEY", "runner-token")
    @patch.object(pipeline_runner_client, "RUNNER_SERVICE_URL", "http://runner:8020")
    @patch("pipeline_runner_client.urlopen")
    def test_private_request_uses_bearer_auth_and_json(self, urlopen):
        urlopen.return_value = FakeResponse({"run_id": "run-1", "status": "queued"})

        result = pipeline_runner_client.runner_request(
            "POST",
            "/v1/pipeline-runs",
            payload={"idempotency_key": "request-1"},
        )

        self.assertEqual("run-1", result["run_id"])
        outbound = urlopen.call_args.args[0]
        headers = {name.lower(): value for name, value in outbound.header_items()}
        self.assertEqual("Bearer runner-token", headers["authorization"])
        self.assertEqual("POST", outbound.get_method())
        self.assertEqual(
            {"idempotency_key": "request-1"},
            json.loads(outbound.data.decode()),
        )

    @patch.object(pipeline_runner_client, "RUNNER_SERVICE_API_KEY", "")
    def test_missing_private_credential_fails_closed(self):
        with self.assertRaises(pipeline_runner_client.PipelineRunnerError) as raised:
            pipeline_runner_client.runner_request("GET", "/v1/pipeline-runs")

        self.assertEqual(503, raised.exception.status_code)

