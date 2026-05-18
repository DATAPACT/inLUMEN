import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment_artifacts import (  # noqa: E402
    DeploymentArtifactValidationError,
    build_argo_workflow_object,
    build_argo_workflow_yaml,
    build_dockerfile_artifacts,
    validate_argo_workflow_object,
    validate_dockerfile_artifacts,
)


class DeploymentArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Retrieve data",
                        "type": "input",
                        "files": ["retrieve.sh", "requirements.txt"],
                    },
                },
                {
                    "id": "2",
                    "data": {
                        "label": "Process data",
                        "type": "action",
                        "files": ["process.py"],
                    },
                },
                {
                    "id": "3",
                    "data": {
                        "label": "Notify",
                        "type": "output",
                    },
                },
            ],
            "edges": [
                {"source": "1", "target": "2"},
                {"source": "2", "target": "3"},
            ],
        }

    def test_builds_one_valid_dockerfile_per_pipeline_step(self):
        artifacts = build_dockerfile_artifacts(self.graph)

        dockerfiles = artifacts["dockerfiles"]
        self.assertEqual(["1", "2", "3"], [item["flow_id"] for item in dockerfiles])
        self.assertEqual(
            ["Dockerfile.1", "Dockerfile.2", "Dockerfile.3"],
            [item["dockerfile_filename"] for item in dockerfiles],
        )
        validate_dockerfile_artifacts(
            dockerfiles,
            expected_step_ids=["1", "2", "3"],
            steps=[
                {"flow_id": "1", "files": [{"filename": "retrieve.sh"}, {"filename": "requirements.txt"}]},
                {"flow_id": "2", "files": [{"filename": "process.py"}]},
                {"flow_id": "3", "files": []},
            ],
        )
        self.assertIn("RUN pip install --no-cache-dir -r requirements.txt", dockerfiles[0]["content"])
        self.assertIn('RUN find /app -type f -name "*.sh"', dockerfiles[0]["content"])

    def test_dockerfile_guardrail_rejects_bad_format(self):
        with self.assertRaises(DeploymentArtifactValidationError) as ctx:
            validate_dockerfile_artifacts(
                [{"dockerfile_filename": "Dockerfile.1", "content": "WORKDIR /app\nCMD [\"true\"]\n"}],
                expected_step_ids=["1"],
            )

        self.assertIn("must start with a FROM instruction", str(ctx.exception))

    def test_builds_valid_argo_workflow_dag_from_graph_edges(self):
        dockerfiles = build_dockerfile_artifacts(self.graph)
        workflow = build_argo_workflow_object(self.graph, dockerfiles)

        validate_argo_workflow_object(workflow, expected_step_ids=["1", "2", "3"])
        tasks = workflow["spec"]["templates"][0]["dag"]["tasks"]
        self.assertEqual("step-1", tasks[0]["name"])
        self.assertEqual(["step-1"], tasks[1]["dependencies"])
        self.assertEqual(["step-2"], tasks[2]["dependencies"])
        self.assertEqual("inlumen/step-2:latest", workflow["spec"]["templates"][2]["container"]["image"])

    def test_argo_yaml_output_is_plain_yaml_not_markdown(self):
        dockerfiles = build_dockerfile_artifacts(self.graph)
        yaml_text = build_argo_workflow_yaml(self.graph, dockerfiles)

        self.assertIn('apiVersion: "argoproj.io/v1alpha1"', yaml_text)
        self.assertIn('kind: "Workflow"', yaml_text)
        self.assertIn('template: "step-3"', yaml_text)
        self.assertNotIn("```", yaml_text)

    def test_argo_guardrail_requires_dockerfile_for_each_step(self):
        dockerfiles = build_dockerfile_artifacts(self.graph)
        dockerfiles["dockerfiles"] = dockerfiles["dockerfiles"][:2]

        with self.assertRaises(DeploymentArtifactValidationError) as ctx:
            build_argo_workflow_object(self.graph, dockerfiles)

        self.assertIn("missing Dockerfile for step id '3'", str(ctx.exception))

    def test_argo_guardrail_rejects_cyclic_pipeline(self):
        graph = {
            "nodes": self.graph["nodes"][:2],
            "edges": [
                {"source": "1", "target": "2"},
                {"source": "2", "target": "1"},
            ],
        }
        dockerfiles = build_dockerfile_artifacts(graph)

        with self.assertRaises(DeploymentArtifactValidationError) as ctx:
            build_argo_workflow_object(graph, dockerfiles)

        self.assertIn("pipeline graph contains a cycle", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
