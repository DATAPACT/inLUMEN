import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import inlumen_api  # noqa: E402
from local_api_client import LocalApiResponse  # noqa: E402


def graph_response():
    return LocalApiResponse(
        content=json.dumps(
            {
                "pipeline": {
                    "uid": "pipeline-1",
                    "version": "2.1",
                    "active_version_uid": "main",
                },
                "nodes": [{"id": "node-1", "data": {"label": "Task"}}],
                "edges": [],
            }
        ).encode(),
        status_code=200,
        headers={"Content-Type": "application/json"},
    )


class PipelineRunApiTest(unittest.TestCase):
    def setUp(self):
        self.client = inlumen_api.app.test_client()

    @patch("inlumen_api.runner_request")
    @patch(
        "inlumen_api.prepare_dagster_execution_bundle",
        return_value={
            "files": [{"path": "run-spec.json", "content": "{}"}],
            "manifest": {"targets": {"dagster": True}},
            "runtime_secrets": {"INLUMEN_SECRET_NODE_1_API_KEY": "secret"},
        },
    )
    @patch("inlumen_api._proxy", return_value=graph_response())
    def test_submission_uses_authoritative_executable_snapshot_and_returns_202(
        self, _proxy, _prepare, runner
    ):
        runner.return_value = {
            "schema_version": "inlumen.pipeline-run@1",
            "run_id": "run-1",
            "status": "queued",
        }

        response = self.client.post(
            "/api/pipeline-runs",
            json={"idempotency_key": "request-1", "graph": {"nodes": []}},
        )

        self.assertEqual(202, response.status_code)
        method, path = runner.call_args.args
        payload = runner.call_args.kwargs["payload"]
        self.assertEqual(("POST", "/v1/pipeline-runs"), (method, path))
        self.assertEqual("node-1", payload["snapshot"]["graph"]["nodes"][0]["id"])
        self.assertEqual("pipeline-1", payload["snapshot"]["pipeline_id"])
        self.assertEqual("run-spec.json", payload["snapshot"]["bundle_files"][0]["path"])
        self.assertEqual(
            "secret", payload["runtime_secrets"]["INLUMEN_SECRET_NODE_1_API_KEY"]
        )
        self.assertEqual("request-1", payload["idempotency_key"])

    @patch("inlumen_api.runner_request")
    def test_list_and_cancel_proxy_to_private_runner(self, runner):
        runner.side_effect = [
            {"runs": []},
            {"run_id": "run-1", "status": "cancelling"},
            {"removed_runs": 1, "cancelled_runs": 0},
        ]

        listed = self.client.get("/api/pipeline-runs?limit=500")
        cancelled = self.client.delete("/api/pipeline-runs/run-1")
        cleared = self.client.delete("/api/pipeline-runs")

        self.assertEqual(200, listed.status_code)
        self.assertEqual(200, cancelled.status_code)
        self.assertEqual(200, cleared.status_code)
        self.assertEqual(
            ("GET", "/v1/pipeline-runs"),
            runner.call_args_list[0].args,
        )
        self.assertEqual(100, runner.call_args_list[0].kwargs["params"]["limit"])
        self.assertEqual(
            ("DELETE", "/v1/pipeline-runs/run-1"),
            runner.call_args_list[1].args,
        )
        self.assertEqual(
            ("DELETE", "/v1/pipeline-runs"),
            runner.call_args_list[2].args,
        )

    @patch("inlumen_api.runner_request")
    @patch(
        "inlumen_api.prepare_dagster_execution_bundle",
        return_value={
            "files": [{"path": "run-spec.json", "content": "{}"}],
            "manifest": {"targets": {"dagster": True}},
            "runtime_secrets": {},
        },
    )
    @patch("inlumen_api._proxy", return_value=graph_response())
    def test_submission_preserves_runner_capacity_error(
        self, _proxy, _prepare, runner
    ):
        runner.side_effect = inlumen_api.PipelineRunnerError(
            429,
            "Run capacity is full (4/4).",
            {"code": "pipeline_run_capacity_full", "limit": 4},
        )

        response = self.client.post("/api/pipeline-runs", json={})

        self.assertEqual(429, response.status_code)
        self.assertEqual("Run capacity is full (4/4).", response.json["error"])
        self.assertEqual(
            "pipeline_run_capacity_full", response.json["details"]["code"]
        )
