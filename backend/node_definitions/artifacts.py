from __future__ import annotations

import hashlib
import json
from typing import Any


def configuration_hash(
    *,
    definition_id: str,
    definition_version: int,
    implementation: dict[str, Any],
    generator: str,
    generator_version: str,
    contract_version: str,
) -> str:
    canonical = json.dumps(
        {
            "contract_version": str(contract_version),
            "definition_id": str(definition_id),
            "definition_version": int(definition_version),
            "generator": str(generator),
            "generator_version": str(generator_version),
            "implementation": implementation,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
