from __future__ import annotations

import json
from typing import Any

from .artifacts import (
    configuration_definition_id,
    configuration_hash,
    implementation_plan_from_data,
)
from model_plans import resolve_implementation_plan


VALID_CONFIGURATION_STATUSES = {"unconfigured", "valid", "invalid"}


def definition_properties_from_data(data: Any) -> dict[str, Any]:
    """Convert a React Flow node definition payload to Neo4j-safe properties."""
    if not isinstance(data, dict):
        return {}

    definition_id = str(data.get("definition_id") or "").strip()
    if not definition_id:
        return {}

    try:
        definition_version = int(data.get("definition_version") or 1)
    except (TypeError, ValueError):
        definition_version = 1

    implementation = data.get("implementation")
    if not isinstance(implementation, dict):
        implementation = {}
    implementation = resolve_implementation_plan(
        implementation,
        label=str(data.get("label") or ""),
        description=str(data.get("description") or ""),
    )

    properties: dict[str, Any] = {
        "definition_id": definition_id,
        "definition_version": max(definition_version, 1),
        "implementation_json": json.dumps(
            implementation,
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
    configuration_status = str(
        data.get("configuration_status") or ""
    ).strip().lower()
    if configuration_status in VALID_CONFIGURATION_STATUSES:
        properties["configuration_status"] = configuration_status
    generated_artifact = data.get("generated_artifact")
    if isinstance(generated_artifact, dict):
        properties["generated_artifact_json"] = json.dumps(
            generated_artifact,
            ensure_ascii=False,
            sort_keys=True,
        )
    return properties


def normalize_definition_properties(properties: dict[str, Any]) -> None:
    """Normalize definition fields in-place before storing a STEP node."""
    definition_properties = definition_properties_from_data(properties)
    properties.pop("implementation", None)
    properties.pop("implementation_json", None)
    properties.pop("configuration_status", None)
    properties.pop("generated_artifact", None)
    properties.pop("generated_artifact_json", None)

    if definition_properties:
        properties.update(definition_properties)
        return

    properties.pop("definition_id", None)
    properties.pop("definition_version", None)


def definition_data_from_properties(properties: Any) -> dict[str, Any]:
    """Convert Neo4j STEP properties back to React Flow node data."""
    if not isinstance(properties, dict):
        return {}
    definition_id = str(properties.get("definition_id") or "").strip()

    try:
        definition_version = max(
            int(properties.get("definition_version") or 1),
            1,
        )
    except (TypeError, ValueError):
        definition_version = 1

    implementation_json = properties.get("implementation_json")
    try:
        implementation = (
            json.loads(implementation_json)
            if isinstance(implementation_json, str)
            else {}
        )
    except (TypeError, ValueError):
        implementation = {}
    if not isinstance(implementation, dict) or not implementation:
        implementation = implementation_plan_from_data(properties)
    else:
        implementation = resolve_implementation_plan(
            implementation,
            label=str(properties.get("label") or ""),
            description=str(properties.get("description") or ""),
        )

    data: dict[str, Any] = {}
    if definition_id:
        data.update(
            {
                "definition_id": definition_id,
                "definition_version": definition_version,
                "implementation": implementation,
            }
        )
    elif implementation:
        data["implementation"] = implementation
    configuration_status = str(
        properties.get("configuration_status") or ""
    ).strip().lower()
    if configuration_status in VALID_CONFIGURATION_STATUSES:
        data["configuration_status"] = configuration_status

    generated_artifact_json = properties.get("generated_artifact_json")
    try:
        generated_artifact = (
            json.loads(generated_artifact_json)
            if isinstance(generated_artifact_json, str)
            else {}
        )
    except (TypeError, ValueError):
        generated_artifact = {}
    if isinstance(generated_artifact, dict) and generated_artifact:
        artifact_hash = str(generated_artifact.get("configuration_hash") or "")
        contract = generated_artifact.get("data_contract")
        contract_version = (
            str(contract.get("version") or "")
            if isinstance(contract, dict)
            else ""
        )
        current_hash = configuration_hash(
            definition_id=configuration_definition_id(properties),
            definition_version=definition_version,
            implementation=implementation,
            generator=str(generated_artifact.get("generator") or ""),
            generator_version=str(
                generated_artifact.get("generator_version") or ""
            ),
            contract_version=contract_version,
        )
        generated_artifact["status"] = (
            "current" if artifact_hash and artifact_hash == current_hash else "stale"
        )
        data["generated_artifact"] = generated_artifact
    return data
