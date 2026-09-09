"""Opt-in local storage integration; UUID workspaces are removed after each test.

Uses real RS256 verification with an in-process test JWKS and membership fixture.
Never point the storage configuration at production.
"""
import base64
import io
import json
import os
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import inlumen_api as api
from auth_middleware import require_auth
from workspace_store import Principal, WorkspaceAccessDenied
from workspace_storage import node_bucket_name, workspace_bucket_prefix, version_snapshot_bucket


@unittest.skipUnless(os.getenv("RUN_AUTH_FILE_INTEGRATION") == "1", "requires local Neo4j and MinIO")
class AuthenticatedFileWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workspace = "file-regression-" + uuid.uuid4().hex
        self.other = "file-regression-" + uuid.uuid4().hex
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.env = patch.dict(os.environ, {"AUTH_ENABLED": "true", "APP_ENV": "production",
            "KEYCLOAK_ISSUER": "https://test.invalid/realm", "KEYCLOAK_AUDIENCE": "inlumen-test"})
        self.env.start()
        self.jwks = patch("auth_middleware._get_jwks_client")
        self.jwks.start().return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=self.key.public_key())
        def principal(claims, requested):
            workspace = self.workspace if claims["sub"] == "owner" else self.other
            if requested and requested != workspace:
                raise WorkspaceAccessDenied("not a member")
            return Principal(claims["sub"], claims["sub"], claims["iss"], workspace, workspace, "owner")
        self.membership = patch("auth_middleware.resolve_principal", side_effect=principal)
        self.membership.start()
        self.client = api.app.test_client()
        self.headers = self.headers_for()

    def headers_for(self, subject="owner", expired=False):
        now = int(time.time())
        token = jwt.encode({"iss": "https://test.invalid/realm", "sub": subject,
            "aud": "inlumen-test", "iat": now - 120, "exp": now - 60 if expired else now + 600}, self.key, algorithm="RS256")
        return {"Authorization": "Bearer " + token,
                "X-InLumen-Workspace-Id": self.workspace if subject == "owner" else self.other}

    def tearDown(self):
        from neo4j_api import _base_driver, _workspace_label
        from minio_gateway import get_minio_client
        try:
            for workspace in (self.workspace, self.other):
                with _base_driver.session() as session:
                    session.run(f"MATCH (n:{_workspace_label(workspace)}) DETACH DELETE n").consume()
                    session.run("MATCH (r:WORKSPACE_REVISION {workspace_id:$workspace}) DELETE r",
                                workspace=workspace).consume()
                client = get_minio_client()
                for bucket in client.list_buckets():
                    if bucket.name.startswith(workspace_bucket_prefix(workspace)) or bucket.name == version_snapshot_bucket(workspace):
                        for obj in client.list_objects(bucket.name, recursive=True):
                            client.remove_object(bucket.name, obj.object_name)
                        client.remove_bucket(bucket.name)
        finally:
            self.membership.stop()
            self.jwks.stop()
            self.env.stop()

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_upload_replace_edit_save_reload_stage_and_reject_unauthorized(self):
        graph = {"nodes": [{"id": "1", "position": {"x": 0, "y": 0},
            "data": {"type": "source", "label": "Isolated input", "file_buckets": []}}], "edges": []}
        self.ok(self.client.post("/api/pipeline/graph", headers=self.headers, json={"graph": graph}))
        for content in (b"value\nfirst\n", b"value\nreplacement\n"):
            uploaded = self.ok(self.client.post("/api/nodes/1/files", headers=self.headers,
                data={"file": (io.BytesIO(content), "real.csv"), "role": "data"}))
            ref = uploaded["file_reference"]
            self.assertEqual(ref, {"filename": "real.csv", "bucket": node_bucket_name("1", self.workspace), "role": "data"})
            graph["nodes"][0]["data"]["file_buckets"] = [ref]
            self.ok(self.client.post("/api/pipeline/versions/active", headers=self.headers, json={"graph": graph}))
            loaded = self.ok(self.client.get("/api/pipeline/graph", headers=self.headers))
            refs = loaded["nodes"][0]["data"]["file_buckets"]
            self.assertEqual(refs[0]["bucket"], ref["bucket"])
            response = self.client.get("/api/files/content?container_id=1&filename=real.csv", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, content)
        # Retain the saved immutable location before editing the live file.
        self.ok(self.client.post("/api/pipeline/versions", headers=self.headers,
                                 json={"name": "Before edit", "graph": loaded}))
        versions = self.ok(self.client.get("/api/pipeline/versions?include_graph=true", headers=self.headers))
        snapshot = next(v["graph"] for v in versions["versions"] if v["name"] == "Before edit")
        edited = self.ok(self.client.put("/api/nodes/1/files/text", headers=self.headers,
            json={"container_id": "1", "filename": "real.csv", "content": "value\nedited\n"}))
        graph["nodes"][0]["data"]["file_buckets"] = [edited["file_reference"]]
        self.ok(self.client.post("/api/pipeline/versions/active", headers=self.headers, json={"graph": graph}))
        self.ok(self.client.post("/api/pipeline/versions/active", headers=self.headers, json={"graph": graph}))
        self.assertEqual(self.client.get("/api/files/content?container_id=1&filename=real.csv", headers=self.headers).data, b"value\nedited\n")
        # The real authenticated request dispatches to storage and packages bytes.
        with api.app.test_request_context(headers=self.headers):
            @require_auth
            def stage():
                context = api._build_pipeline_codegen_context(snapshot, include_samples=False)
                return api._prepare_codegen_request({"context": context})
            staged = stage()
        descriptor = staged["context"]["graph"]["nodes"][0]["files"][0]
        self.assertEqual(base64.b64decode(descriptor["sample"]["content_base64"]), b"value\nreplacement\n")
        self.assertNotIn("Bearer", json.dumps(staged))
        if os.getenv("AUTH_FILE_STAGED_OUTPUT"):
            from pathlib import Path
            Path(os.environ["AUTH_FILE_STAGED_OUTPUT"]).write_text(json.dumps(descriptor))
        for headers, status in (({}, 401), (self.headers_for(expired=True), 401),
                ({**self.headers_for("outsider"), "X-InLumen-Workspace-Id": self.workspace}, 404)):
            response = self.client.get("/api/files/content?container_id=1&filename=real.csv", headers=headers)
            self.assertEqual(response.status_code, status, response.data)
        cross = self.client.get("/api/files/content", headers=self.headers_for("outsider"),
            query_string={"container_id": node_bucket_name("1", self.workspace), "filename": "real.csv"})
        self.assertEqual(cross.status_code, 404, cross.data)
        bad_edit = self.client.put("/api/nodes/1/files/text", headers=self.headers,
            json={"container_id": ref["bucket"], "filename": "real.csv", "content": "wrong"})
        self.assertEqual(bad_edit.status_code, 400)
