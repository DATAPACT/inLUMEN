#!/usr/bin/env python3
"""Local stand-in for the example "Text Anonymizer" tool (for tests and demos).

The plugin definition in ``text-anonymizer.plugin.json`` points at this server.
Endpoints: ``/tools/text-anonymizer/health`` and ``/tools/text-anonymizer/v1/anonymize``.

Run:  python examples/plugins/mock_tool_server.py   (listens on :8099)

Environment:
  MOCK_TOOL_PORT     listen port (default 8099)
  MOCK_TOOL_API_KEY  API key the tool accepts (default demo-key)

Standard library only. Not for production use.
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")


def anonymize(text: str, replacement: str, entities: str) -> tuple[str, int]:
    removed = 0
    if entities in {"email", "all"}:
        text, count = EMAIL_RE.subn(replacement, text)
        removed += count
    if entities in {"phone", "all"}:
        text, count = PHONE_RE.subn(replacement, text)
        removed += count
    return text, removed


class Handler(BaseHTTPRequestHandler):
    server_version = "MockTool/1"

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parts = [unquote(part) for part in urlparse(self.path).path.strip("/").split("/") if part]
        if parts == ["tools", "text-anonymizer", "health"]:
            return self._json(200, {"status": "ok"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path.rstrip("/") != "/tools/text-anonymizer/v1/anonymize":
            return self._json(404, {"error": "not found"})
        if self.headers.get("X-API-Key") != os.getenv("MOCK_TOOL_API_KEY", "demo-key"):
            return self._json(401, {"error": "invalid API key"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        query = parse_qs(url.query)
        replacement = (query.get("replacement") or ["[REDACTED]"])[0]
        entities = (query.get("entities") or ["all"])[0]
        if entities not in {"email", "phone", "all"}:
            return self._json(400, {"error": "entities must be email, phone or all"})
        text, removed = anonymize(raw, replacement, entities)
        return self._json(200, {"text": text, "removed": removed})

    def log_message(self, format, *args):  # noqa: A002
        print(f"[mock-tool] {self.address_string()} {format % args}")


def main() -> None:
    port = int(os.getenv("MOCK_TOOL_PORT", "8099"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[mock-tool] text anonymizer: http://localhost:{port}/tools/text-anonymizer")
    server.serve_forever()


if __name__ == "__main__":
    main()
