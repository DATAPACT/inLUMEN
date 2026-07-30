from __future__ import annotations

import hashlib
import json
from typing import Any

from model_plans import resolve_implementation_plan


def implementation_plan_from_data(data: Any) -> dict[str, Any]:
    """Return the canonical model implementation plan from a node payload."""
    if not isinstance(data, dict):
        return {}

    implementation = data.get("implementation")
    if isinstance(implementation, dict) and implementation:
        return resolve_implementation_plan(
            implementation,
            label=str(data.get("label") or ""),
            description=str(data.get("description") or ""),
        )

    parameters = data.get("param")
    if not isinstance(parameters, dict):
        raw_parameters = data.get("param_json")
        try:
            parameters = (
                json.loads(raw_parameters)
                if isinstance(raw_parameters, str)
                else {}
            )
        except (TypeError, ValueError):
            parameters = {}

    if isinstance(parameters, dict):
        model_plan = parameters.get("model_plan")
        if isinstance(model_plan, dict) and model_plan:
            return resolve_implementation_plan(
                model_plan,
                label=str(data.get("label") or ""),
                description=str(data.get("description") or ""),
            )
    return {}


def configuration_definition_id(
    data: Any,
    *,
    flow_id: str = "",
) -> str:
    """Provide a stable configuration identity for catalog and dynamic nodes."""
    payload = data if isinstance(data, dict) else {}
    definition_id = str(payload.get("definition_id") or "").strip()
    if definition_id:
        return definition_id

    dynamic_flow_id = str(
        flow_id
        or payload.get("flow_id")
        or payload.get("id")
        or ""
    ).strip()
    return (
        f"inlumen.dynamic-step.{dynamic_flow_id}"
        if dynamic_flow_id
        else "inlumen.dynamic-step"
    )


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
