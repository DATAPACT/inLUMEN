import unittest
from unittest.mock import patch

from neo4j_api import app, _scope_cypher, _validate_workspace_cypher
from permissions import permits
from workspace_queries import parameterize_literals, escape_literal_content


class SecurityBoundaryTests(unittest.TestCase):
    def test_scope_handles_whitespace_without_changing_literal_data(self):
        query = "MATCH (n: STEP) WHERE n.label = ':STEP' RETURN n"
        scoped = _scope_cypher(query, "alice")
        self.assertIn("(n:STEP:INLUMEN_WS_", scoped)
        self.assertIn("n.label = ':STEP'", scoped)
        self.assertNotEqual(scoped, _scope_cypher(query, "bob"))

    def test_http_cannot_claim_internal_query_capability(self):
        with patch.dict("os.environ", {"AUTH_ENABLED": "false"}):
            response = app.test_client().post('/neo4j_run_query', json={
                "query": "MATCH (n: STEP) RETURN n", "query_capability": True,
            }, headers={"inlumen.query_capability": "true"})
        self.assertEqual(response.status_code, 403)

    def test_user_values_are_driver_parameters(self):
        value = "backslash\\' RETURN secret // :STEP"
        query, params = parameterize_literals("RETURN '" + escape_literal_content(value) + "' AS label")
        self.assertEqual(query, "RETURN $inlumen_literal_0 AS label")
        self.assertEqual(list(params.values()), [value])

    def test_procedures_are_rejected(self):
        with self.assertRaises(ValueError):
            _validate_workspace_cypher("CALL db.labels() YIELD label RETURN label")

    def test_workspace_roles_are_enforced(self):
        for role in ('owner', 'editor', 'runner', 'viewer'):
            self.assertTrue(permits(role, 'GET', '/api/pipeline/graph'))
            self.assertEqual(permits(role, 'POST', '/api/graph/nodes'), role in ('owner', 'editor'))
            self.assertEqual(permits(role, 'POST', '/api/pipeline-runs'), role != 'viewer')
            self.assertEqual(permits(role, 'POST', '/api/workspace/clear-all'), role == 'owner')
        self.assertFalse(permits('unknown', 'GET', '/api/pipeline/graph'))


class DocumentAndSnapshotBoundaryTests(unittest.TestCase):
    def test_missing_document_never_clears_graph(self):
        from neo4j_api import app
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            response = app.test_client().post('/neo4j_sync_graph', json={})
        self.assertEqual(response.status_code, 422)

    def test_snapshot_cannot_copy_from_another_workspace(self):
        from neo4j_api import _copy_minio_object
        with patch('neo4j_api.bucket_belongs_to_workspace', return_value=False), patch('neo4j_api.read_object_bytes') as read:
            with self.assertRaises(PermissionError):
                _copy_minio_object('foreign', 'key', 'local', 'key')
            read.assert_not_called()

    def test_document_rejects_duplicate_ids_and_dangling_edges(self):
        from graph_document import validate_graph_document
        for graph in [
            {'nodes': [{'id': '1'}, {'id': '1'}], 'edges': []},
            {'nodes': [{'id': '1'}], 'edges': [{'source': '1', 'target': '2'}]},
        ]:
            with self.assertRaises(ValueError): validate_graph_document(graph)
        validate_graph_document({'nodes': [], 'edges': []})
