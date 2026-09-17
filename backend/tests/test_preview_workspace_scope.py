import sys
import unittest
from pathlib import Path

from flask import Flask, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_middleware import current_workspace_id  # noqa: E402
from local_api_client import dispatch_flask_request  # noqa: E402
from workspace_store import WORKSPACE_HEADER  # noqa: E402


class PreviewWorkspaceScopeTest(unittest.TestCase):
    def test_only_the_private_in_process_override_can_select_a_preview_workspace(self):
        app = Flask(__name__)

        @app.get("/workspace")
        def workspace():
            return jsonify({"workspace_id": current_workspace_id()})

        external = dispatch_flask_request(
            app,
            "/workspace",
            headers={
                WORKSPACE_HEADER: "attacker-selected-workspace",
                "Inlumen-Internal-Workspace-Id": "attacker-selected-preview",
            },
        )
        internal = dispatch_flask_request(
            app,
            "/workspace",
            internal_workspace_id="preview-workspace-123",
        )

        self.assertEqual("local-workspace", external.json()["workspace_id"])
        self.assertEqual("preview-workspace-123", internal.json()["workspace_id"])


if __name__ == "__main__":
    unittest.main()
