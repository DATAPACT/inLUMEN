import io
import unittest

from flask import Flask, jsonify, request

from local_api_client import (
    LocalApiHTTPError,
    LocalApiResponse,
    dispatch_flask_request,
)


class LocalApiClientTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

        @self.app.post("/echo-json")
        def echo_json():
            return jsonify(request.get_json())

        @self.app.get("/query")
        def query():
            return jsonify(value=request.args.get("value"))

        @self.app.post("/upload")
        def upload():
            uploaded = request.files["file"]
            return jsonify(
                filename=uploaded.filename,
                content=uploaded.read().decode("utf-8"),
                label=request.form.get("label"),
            )

    def test_dispatches_json_and_decodes_the_response(self):
        response = dispatch_flask_request(
            self.app,
            "echo-json",
            method="POST",
            json_payload={"status": "ok"},
        )

        self.assertTrue(response.ok)
        self.assertEqual({"status": "ok"}, response.json())
        self.assertIn("application/json", response.headers["Content-Type"])

    def test_dispatches_query_parameters(self):
        response = dispatch_flask_request(
            self.app,
            "/query",
            params={"value": "pipeline 1"},
        )

        self.assertEqual({"value": "pipeline 1"}, response.json())

    def test_rewinds_and_dispatches_multipart_files(self):
        stream = io.BytesIO(b"runtime source")
        stream.seek(len(stream.getvalue()))

        response = dispatch_flask_request(
            self.app,
            "upload",
            method="POST",
            files={"file": ("main.py", stream, "text/x-python")},
            form={"label": "node-1"},
        )

        self.assertEqual(
            {
                "filename": "main.py",
                "content": "runtime source",
                "label": "node-1",
            },
            response.json(),
        )

    def test_raise_for_status_includes_the_local_response(self):
        response = LocalApiResponse(
            content=b'{"error":"unavailable"}',
            status_code=503,
            headers={},
        )

        self.assertFalse(response.ok)
        with self.assertRaisesRegex(LocalApiHTTPError, "503.*unavailable"):
            response.raise_for_status()


if __name__ == "__main__":
    unittest.main()
