import json
from pathlib import Path
from typing import get_args

from app.schemas import ArtifactKind


def test_codegen_artifact_kind_matches_published_contract() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    schema_path = repository_root / "contracts" / "v2" / "node-output-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    published_kinds = set(
        schema["$defs"]["artifact"]["properties"]["kind"]["enum"]
    )

    assert set(get_args(ArtifactKind)) == published_kinds
