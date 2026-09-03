import unittest
from unittest.mock import patch

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
