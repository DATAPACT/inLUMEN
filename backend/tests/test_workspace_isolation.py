import unittest
from unittest.mock import Mock, patch

from auth_middleware import validate_production_auth_configuration
from neo4j_api import _scope_cypher, _validate_workspace_cypher, _workspace_label
from workspace_storage import node_bucket_name
from workspace_store import WorkspaceAccessDenied, resolve_principal


class WorkspaceIsolationTests(unittest.TestCase):
    def test_development_principals_receive_separate_default_workspaces(self):
        with patch.dict("os.environ", {"APP_ENV": "development"}, clear=True):
            first = resolve_principal({"iss": "issuer", "sub": "user-a"})
            second = resolve_principal({"iss": "issuer", "sub": "user-b"})
            self.assertNotEqual(first.workspace_id, second.workspace_id)
            with self.assertRaises(WorkspaceAccessDenied):
                resolve_principal(
                    {"iss": "issuer", "sub": "user-a"}, second.workspace_id
                )

    def test_production_refuses_to_start_with_auth_disabled(self):
        with patch.dict(
            "os.environ", {"APP_ENV": "production", "AUTH_ENABLED": "false"}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                validate_production_auth_configuration()

    def test_graph_query_gets_distinct_server_derived_workspace_labels(self):
        query = "MATCH (p:PIPELINE)-[:HAS_STEP]->(s:STEP) RETURN p, s"

        first = _scope_cypher(query, "workspace-a")
        second = _scope_cypher(query, "workspace-b")

        self.assertIn(f":{_workspace_label('workspace-a')}", first)
        self.assertIn(f":{_workspace_label('workspace-b')}", second)
        self.assertNotEqual(first, second)


    def test_label_check_requires_both_tokens_for_owned_labels(self):
        from neo4j_api import _WorkspaceQueryRunner, _label_exists

        workspace = "new-workspace"
        own_label = _workspace_label(workspace)
        other_label = _workspace_label("other-workspace")
        for labels, expected in (
            ([], False),
            (["PIPELINE", other_label], False),
            ([own_label], False),
            (["PIPELINE", own_label], True),
        ):
            with self.subTest(labels=labels):
                runner = Mock()
                runner.run.return_value.single.return_value = {"labels": labels}
                session = _WorkspaceQueryRunner(runner, workspace)
                self.assertEqual(_label_exists(session, "PIPELINE"), expected)

    def test_empty_workspace_poll_skips_pipeline_query(self):
        from neo4j_api import app, _WorkspaceQueryRunner, neo4j_get_pipeline_updated_at

        runner = Mock()
        runner.run.return_value.single.return_value = {
            "labels": ["PIPELINE", _workspace_label("other-workspace")]
        }
        session = _WorkspaceQueryRunner(runner, "new-workspace")
        with app.test_request_context(), patch("neo4j_api.driver") as driver:
            driver.session.return_value.__enter__.return_value = session
            response, status = neo4j_get_pipeline_updated_at.__wrapped__()
        self.assertEqual(status, 200)
        self.assertEqual(response.get_json(), {"updated_at": None})
        runner.run.assert_called_once_with(
            "CALL db.labels() YIELD label RETURN collect(label) AS labels"
        )

    def test_raw_cypher_rejects_unscoped_nodes(self):
        with self.assertRaises(ValueError):
            _validate_workspace_cypher("MATCH (n) DETACH DELETE n")

    def test_raw_cypher_allows_owned_labels_and_bound_reuse(self):
        _validate_workspace_cypher(
            "MATCH (p:PIPELINE)-[:HAS_STEP]->(s:STEP) "
            "SET p.updated_at=datetime() WITH p MATCH (p) RETURN p"
        )

    def test_object_bucket_names_differ_between_workspaces(self):
        first = node_bucket_name("node-1", "workspace-a")
        second = node_bucket_name("node-1", "workspace-b")
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 63)
        self.assertLessEqual(len(second), 63)


if __name__ == "__main__":
    unittest.main()
