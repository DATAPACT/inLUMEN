"""Server-owned Cypher boundary. HTTP callers cannot supply executable queries."""
from __future__ import annotations

import ast
import hashlib
import re

INTERNAL_QUERY_CAPABILITY = object()
OWNED_LABELS = ("PROVENANCE_EVENT", "PIPELINE_VERSION", "PIPELINE", "STEP", "FILE")
# Preserve literals, identifiers and comments rather than rewriting their contents.
TOKENS = re.compile(r"('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:``|[^`])*`|//[^\n]*|/\*[\s\S]*?\*/)")


def workspace_label(workspace_id: str) -> str:
    return "INLUMEN_WS_" + hashlib.sha256(workspace_id.encode()).hexdigest()[:24]


def scope_cypher(query: str, workspace_id: str) -> str:
    label = workspace_label(workspace_id)
    pieces = TOKENS.split(str(query))
    for index in range(0, len(pieces), 2):
        code = pieces[index]
        for owned in OWNED_LABELS:
            code = re.sub(rf":\s*{owned}\b(?!\s*:{label}\b)", f":{owned}:{label}", code)
        code = re.sub(r"\bMATCH\s*\(entity\)", f"MATCH (entity:{label})", code, flags=re.I)
        pieces[index] = code
    return "".join(pieces)


def parameterize_literals(query: str) -> tuple[str, dict]:
    """Lift literals from trusted templates into driver parameters.

    This is not an input sanitizer: templates must be server-owned and all
    interpolated strings must first use escape_literal_content.
    """
    pieces = TOKENS.split(query)
    parameters = {}
    for index in range(1, len(pieces), 2):
        token = pieces[index]
        if token.startswith(("'", '"')):
            key = f"inlumen_literal_{len(parameters)}"
            parameters[key] = ast.literal_eval(token)
            pieces[index] = "$" + key
    return "".join(pieces), parameters


def escape_literal_content(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'").replace("\r", "\\r").replace("\n", "\\n")
