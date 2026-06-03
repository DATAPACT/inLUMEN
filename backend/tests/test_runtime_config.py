import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_config import add_cors_headers  # noqa: E402


class _Headers(dict):
    def add(self, name, value):
        self[name] = value


class _Response:
    def __init__(self):
        self.headers = _Headers()


class RuntimeConfigCorsTest(unittest.TestCase):
    def test_adds_permissive_cors_headers(self):
        response = add_cors_headers(_Response(), "https://frontend.example.com")

        self.assertEqual("*", response.headers["Access-Control-Allow-Origin"])
        self.assertEqual(
            "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            response.headers["Access-Control-Allow-Methods"],
        )
        self.assertEqual(
            "Content-Type, Authorization",
            response.headers["Access-Control-Allow-Headers"],
        )
        self.assertEqual("Origin", response.headers["Vary"])


if __name__ == "__main__":
    unittest.main()
