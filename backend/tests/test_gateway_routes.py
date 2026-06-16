import ast
import unittest
from pathlib import Path


GATEWAY_PATH = Path(__file__).resolve().parents[1] / "inlumen_api.py"


class GatewayRoutesTest(unittest.TestCase):
    def test_dagster_generation_route_is_registered_on_gateway_app(self):
        tree = ast.parse(GATEWAY_PATH.read_text())
        registered_routes = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "add_url_rule"
                and isinstance(func.value, ast.Name)
                and func.value.id == "app"
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            registered_routes.append(node.args[0].value)

        self.assertIn("/agentic_generate_dagster_definitions", registered_routes)


if __name__ == "__main__":
    unittest.main()
