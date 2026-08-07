import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import ValidationRequest, health, repair, validate, validate_and_repair


class DeploymentValidationApiTest(unittest.TestCase):
    def setUp(self):
        self.request = ValidationRequest(
            bundle_path="/tmp/generated-bundle",
            targets={"argo": True, "dagster": False},
            validate_argo=True,
            validate_dagster=False,
            materialize=False,
            reinstall=True,
            skip_install=False,
            argo_lint=True,
            argo_dry_run=True,
            timeout_seconds=120,
        )

    def test_health_reports_ready(self):
        self.assertEqual({"status": "ok"}, health())

    @patch("app.main.validate_deployment_bundle")
    def test_validate_forwards_every_validation_option(self, validate_bundle):
        validate_bundle.return_value = {"ok": True}

        result = validate(self.request)

        self.assertEqual({"ok": True}, result)
        validate_bundle.assert_called_once_with(
            Path("/tmp/generated-bundle"),
            targets={"argo": True, "dagster": False},
            validate_argo=True,
            validate_dagster=False,
            materialize=False,
            reinstall=True,
            skip_install=False,
            argo_lint=True,
            argo_dry_run=True,
            timeout_seconds=120,
        )

    @patch("app.main.repair_deployment_bundle")
    def test_repair_forwards_bundle_and_targets(self, repair_bundle):
        repair_bundle.return_value = {"changed": True}

        result = repair(self.request)

        self.assertEqual({"changed": True}, result)
        repair_bundle.assert_called_once_with(
            Path("/tmp/generated-bundle"),
            targets={"argo": True, "dagster": False},
        )

    @patch("app.main.validate_and_repair_deployment_bundle")
    def test_validate_and_repair_forwards_every_option(self, validate_and_repair_bundle):
        validate_and_repair_bundle.return_value = {"ok": True, "changed": True}

        result = validate_and_repair(self.request)

        self.assertEqual({"ok": True, "changed": True}, result)
        validate_and_repair_bundle.assert_called_once_with(
            Path("/tmp/generated-bundle"),
            targets={"argo": True, "dagster": False},
            validate_argo=True,
            validate_dagster=False,
            materialize=False,
            reinstall=True,
            skip_install=False,
            argo_lint=True,
            argo_dry_run=True,
            timeout_seconds=120,
        )


if __name__ == "__main__":
    unittest.main()
