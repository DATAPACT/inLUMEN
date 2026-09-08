import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_credential_store


class LLMCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = os.environ.get("INLUMEN_SECRET_DB_PATH")
        self.previous_key_path = os.environ.get("INLUMEN_SECRET_KEY_PATH")
        self.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ.pop("DATABASE_URL", None)
        os.environ["INLUMEN_SECRET_DB_PATH"] = str(Path(self.temp_dir.name) / "secrets.sqlite3")
        os.environ["INLUMEN_SECRET_KEY_PATH"] = str(Path(self.temp_dir.name) / "secrets.key")
        if llm_credential_store._ENGINE is not None:
            llm_credential_store._ENGINE.dispose()
        llm_credential_store._ENGINE = None
        llm_credential_store._ENGINE_URL = ""

    def tearDown(self):
        if llm_credential_store._ENGINE is not None:
            llm_credential_store._ENGINE.dispose()
        llm_credential_store._ENGINE = None
        llm_credential_store._ENGINE_URL = ""
        if self.previous_db_path is None:
            os.environ.pop("INLUMEN_SECRET_DB_PATH", None)
        else:
            os.environ["INLUMEN_SECRET_DB_PATH"] = self.previous_db_path
        if self.previous_key_path is None:
            os.environ.pop("INLUMEN_SECRET_KEY_PATH", None)
        else:
            os.environ["INLUMEN_SECRET_KEY_PATH"] = self.previous_key_path
        if self.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.previous_database_url
        self.temp_dir.cleanup()

    def test_encrypts_and_scopes_credentials(self):
        with patch.object(llm_credential_store, "current_workspace_id", return_value="workspace-a"):
            llm_credential_store.save_llm_credential("config-a", "provider-secret")
            self.assertTrue(llm_credential_store.has_llm_credential("config-a"))
            self.assertEqual("provider-secret", llm_credential_store.get_llm_credential("config-a"))
        with patch.object(llm_credential_store, "current_workspace_id", return_value="workspace-b"):
            self.assertIsNone(llm_credential_store.get_llm_credential("config-a"))
        self.assertNotIn(
            "provider-secret",
            (Path(self.temp_dir.name) / "secrets.sqlite3").read_bytes().decode("latin-1"),
        )


if __name__ == "__main__":
    unittest.main()
