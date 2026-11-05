import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inlumen.contracts.map_scope import map_scopes  # noqa: E402

FLOWS = {"map": "Parallel Map", "collect": "Collect"}


def codes(edges, templates=FLOWS):
    return [issue.code for issue in map_scopes(templates, edges)[1]]


class MapScopeTest(unittest.TestCase):
    def test_body_is_every_step_between_map_and_collect_in_order(self):
        scopes, issues = map_scopes(FLOWS, [
            ("src", "data", "map", "items"),
            ("map", "item", "extract", "input"),
            ("extract", "output", "parse", "input"),
            ("parse", "output", "collect", "item"),
            ("collect", "items", "aggregate", "input"),
        ])
        self.assertEqual([], issues)
        self.assertEqual([("map", "collect", ["extract", "parse"])], [tuple(scope) for scope in scopes])

    def test_structural_errors(self):
        self.assertEqual(["map-without-collect"], codes([
            ("map", "item", "a", "input"), ("a", "output", "dest", "data"),
        ], {"map": "Parallel Map"}))
        self.assertIn("map-side-input", codes([
            ("map", "item", "a", "input"), ("lookup", "output", "a", "input"),
            ("a", "output", "collect", "item"),
        ]))
        self.assertIn("map-scope-leak", codes([
            ("map", "item", "a", "input"), ("a", "output", "collect", "item"),
            ("a", "output", "dest", "data"),
        ]))
        self.assertEqual(["collect-without-map"], codes([("a", "output", "collect", "item")], {"collect": "Collect"}))
        self.assertIn("nested-map", codes([
            ("map", "item", "map2", "items"), ("map2", "item", "collect", "item"),
        ], {"map": "Parallel Map", "map2": "Parallel Map", "collect": "Collect"}))


if __name__ == "__main__":
    unittest.main()
