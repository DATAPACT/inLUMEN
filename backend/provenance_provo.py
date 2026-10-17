from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote


PROV_NAMESPACE = "http://www.w3.org/ns/prov#"
INLUMEN_NAMESPACE = "urn:inlumen:prov:"
BASE_IRI = "urn:inlumen:provenance:"

PROV_O_CONTEXT = {
    "@version": 1.1,
    "prov": PROV_NAMESPACE,
    "inlumen": INLUMEN_NAMESPACE,
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "label": "rdfs:label",
    "generatedAtTime": {
        "@id": "prov:generatedAtTime",
        "@type": "xsd:dateTime",
    },
    "startedAtTime": {
        "@id": "prov:startedAtTime",
        "@type": "xsd:dateTime",
    },
    "endedAtTime": {
        "@id": "prov:endedAtTime",
        "@type": "xsd:dateTime",
    },
    "used": {
        "@id": "prov:used",
        "@type": "@id",
    },
    "wasGeneratedBy": {
        "@id": "prov:wasGeneratedBy",
        "@type": "@id",
    },
    "wasAssociatedWith": {
        "@id": "prov:wasAssociatedWith",
        "@type": "@id",
    },
    "wasAttributedTo": {
        "@id": "prov:wasAttributedTo",
        "@type": "@id",
    },
    "wasRevisionOf": {
        "@id": "prov:wasRevisionOf",
        "@type": "@id",
    },
    "specializationOf": {
        "@id": "prov:specializationOf",
        "@type": "@id",
    },
    "actedOnBehalfOf": {
        "@id": "prov:actedOnBehalfOf",
        "@type": "@id",
    },
    "value": "prov:value",
    "action": "inlumen:action",
    "summary": "inlumen:summary",
    "details": {
        "@id": "inlumen:details",
        "@type": "@json",
    },
    "sessionId": "inlumen:sessionId",
    "versionUid": "inlumen:versionUid",
    "historyDirection": "inlumen:historyDirection",
    "sourceSnapshotFingerprint": "inlumen:sourceSnapshotFingerprint",
    "targetSnapshotFingerprint": "inlumen:targetSnapshotFingerprint",
    "updatedAt": {
        "@id": "inlumen:updatedAt",
        "@type": "xsd:dateTime",
    },
}


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _iri_part(value: Any, fallback: str) -> str:
    return quote(_text(value, fallback), safe="")


def _typed_node(node_id: str, *types: str, label: str = "") -> dict[str, Any]:
    node: dict[str, Any] = {
        "@id": node_id,
        "@type": list(types),
    }
    if label:
        node["label"] = label
    return node


def _set_time(node: dict[str, Any], key: str, value: Any) -> None:
    timestamp = _text(value)
    if timestamp:
        node[key] = timestamp


def _agent_definition(actor: str, agent_id: str, operator_id: str) -> dict[str, Any]:
    normalized = actor.lower()
    if normalized == "agent":
        node = _typed_node(
            agent_id,
            "prov:SoftwareAgent",
            "inlumen:PipelineEditorAgent",
            label="inLUMEN pipeline editor agent",
        )
        node["actedOnBehalfOf"] = operator_id
        return node
    if normalized == "system":
        return _typed_node(
            agent_id,
            "prov:SoftwareAgent",
            "inlumen:BackendService",
            label="inLUMEN backend",
        )
    if normalized == "manual":
        return _typed_node(
            agent_id,
            "prov:Agent",
            "inlumen:HumanOperator",
            label="inLUMEN operator",
        )
    return _typed_node(agent_id, "prov:Agent", label=actor or "Unknown agent")


def provenance_prov_o_filename(version_name: str | None, version_uid: str | None) -> str:
    label = _text(version_name, _text(version_uid, "main")).lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in label).strip("-")
    return f"inlumen-provenance-{safe or 'main'}.jsonld"


def build_prov_o_jsonld(payload: dict[str, Any]) -> dict[str, Any]:
    pipeline = payload.get("pipeline") if isinstance(payload.get("pipeline"), dict) else {}
    version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []

    pipeline_uid = _iri_part(pipeline.get("uid"), "design")
    version_uid = _iri_part(version.get("uid"), "main")
    pipeline_id = f"{BASE_IRI}pipeline:{pipeline_uid}"
    version_id = f"{pipeline_id}:version:{version_uid}"
    bundle_id = f"{BASE_IRI}bundle:{pipeline_uid}:{version_uid}"
    operator_id = f"{BASE_IRI}agent:operator"

    pipeline_label = _text(
        pipeline.get("name") or pipeline.get("label"),
        "Current design pipeline",
    )
    version_label = _text(version.get("name"), "Main")

    graph: list[dict[str, Any]] = []
    pipeline_node = _typed_node(
        pipeline_id,
        "prov:Entity",
        "inlumen:Pipeline",
        label=pipeline_label,
    )
    _set_time(pipeline_node, "generatedAtTime", pipeline.get("created_at"))
    _set_time(pipeline_node, "updatedAt", pipeline.get("updated_at"))
    graph.append(pipeline_node)

    normalized_events = [
        event for event in events if isinstance(event, dict)
    ]
    initial_state_id = f"{version_id}:state:initial"
    initial_state = _typed_node(
        initial_state_id,
        "prov:Entity",
        "inlumen:PipelineState",
        label=f"{version_label} initial state",
    )
    initial_state["specializationOf"] = pipeline_id
    _set_time(initial_state, "generatedAtTime", version.get("created_at"))
    graph.append(initial_state)

    agents: dict[str, dict[str, Any]] = {}
    previous_state_id = initial_state_id

    for index, event in enumerate(normalized_events):
        event_uid = _iri_part(event.get("uid"), f"event-{index + 1}")
        activity_id = f"{bundle_id}:activity:{event_uid}"
        actor = _text(event.get("actor"), "system")
        actor_key = _iri_part(actor.lower(), "system")
        agent_id = f"{BASE_IRI}agent:{actor_key}"
        agents.setdefault(
            agent_id,
            _agent_definition(actor, agent_id, operator_id),
        )
        if actor.lower() == "agent":
            agents.setdefault(
                operator_id,
                _agent_definition("manual", operator_id, operator_id),
            )

        is_last_event = index == len(normalized_events) - 1
        next_state_id = (
            version_id
            if is_last_event
            else f"{version_id}:state:{event_uid}"
        )
        action = _text(event.get("action"), "change")
        summary = _text(event.get("summary"), "Pipeline graph was modified.")
        timestamp = _text(event.get("created_at"))
        details = event.get("details") if isinstance(event.get("details"), dict) else {}

        activity = _typed_node(
            activity_id,
            "prov:Activity",
            "inlumen:PipelineModification",
            label=summary,
        )
        activity["action"] = action
        activity["summary"] = summary
        activity["used"] = [previous_state_id]
        activity["wasAssociatedWith"] = agent_id
        activity["versionUid"] = _text(event.get("version_uid"), _text(version.get("uid"), "main"))
        session_id = _text(details.get("session_id"))
        if session_id:
            activity["sessionId"] = session_id
        history_direction = _text(details.get("direction"))
        if history_direction:
            activity["historyDirection"] = history_direction
        source_fingerprint = _text(details.get("source_snapshot_fingerprint"))
        if source_fingerprint:
            activity["sourceSnapshotFingerprint"] = source_fingerprint
        target_fingerprint = _text(details.get("target_snapshot_fingerprint"))
        if target_fingerprint:
            activity["targetSnapshotFingerprint"] = target_fingerprint
        if details:
            activity["details"] = details
        _set_time(activity, "startedAtTime", timestamp)
        _set_time(activity, "endedAtTime", timestamp)

        user_query = _text(details.get("user_query"))
        if user_query:
            query_id = f"{bundle_id}:query:{event_uid}"
            query_node = _typed_node(
                query_id,
                "prov:Entity",
                "inlumen:UserQuery",
                label="User query",
            )
            query_node["value"] = user_query
            query_node["wasAttributedTo"] = operator_id
            graph.append(query_node)
            activity["used"].append(query_id)
            agents.setdefault(
                operator_id,
                _agent_definition("manual", operator_id, operator_id),
            )

        state_types = ["prov:Entity", "inlumen:PipelineState"]
        if is_last_event:
            state_types.append("inlumen:PipelineVersion")
        state = _typed_node(
            next_state_id,
            *state_types,
            label=version_label if is_last_event else f"{version_label} state {index + 1}",
        )
        state["specializationOf"] = pipeline_id
        state["wasGeneratedBy"] = activity_id
        state["wasRevisionOf"] = previous_state_id
        _set_time(state, "generatedAtTime", timestamp)
        if is_last_event:
            _set_time(state, "updatedAt", version.get("updated_at"))

        graph.extend([activity, state])
        previous_state_id = next_state_id

    if not normalized_events:
        version_node = _typed_node(
            version_id,
            "prov:Entity",
            "inlumen:PipelineVersion",
            label=version_label,
        )
        version_node["specializationOf"] = pipeline_id
        version_node["wasRevisionOf"] = initial_state_id
        _set_time(
            version_node,
            "generatedAtTime",
            version.get("updated_at") or version.get("created_at"),
        )
        _set_time(version_node, "updatedAt", version.get("updated_at"))
        graph.append(version_node)

    graph.extend(agents.values())
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "@context": PROV_O_CONTEXT,
        "@id": bundle_id,
        "@type": "prov:Bundle",
        "label": f"inLUMEN provenance for {version_label}",
        "generatedAtTime": generated_at,
        "@graph": graph,
    }


RUN_PROV_O_CONTEXT = {
    **PROV_O_CONTEXT,
    "wasInformedBy": {"@id": "prov:wasInformedBy", "@type": "@id"},
    "partOf": {"@id": "inlumen:partOf", "@type": "@id"},
    "sha256": "inlumen:sha256",
    "sizeBytes": {"@id": "inlumen:sizeBytes", "@type": "xsd:integer"},
    "path": "inlumen:path",
    "port": "inlumen:port",
    "status": "inlumen:status",
}


def _epoch_iso(value: Any) -> str:
    if isinstance(value, (int, float)):
        return (
            datetime.fromtimestamp(value, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    return _text(value)


def _digest(value: Any) -> str:
    return _text(value).removeprefix("sha256:")


def build_run_prov_o_jsonld(run: dict[str, Any]) -> dict[str, Any]:
    """Describe one pipeline run as PROV-O: steps, what they used and generated.

    Artifacts are identified by content digest, so the output a producer
    generated and the input a consumer used resolve to the same entity.
    """
    run_id = _text(run.get("run_id"), "run")
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    run_iri = f"{BASE_IRI}run:{_iri_part(run_id, 'run')}"
    graph: list[dict[str, Any]] = []
    entities: dict[str, dict[str, Any]] = {}

    def entity(digest: str, path: str, size: Any = None) -> str:
        entity_id = f"{BASE_IRI}artifact:sha256:{digest}"
        node = entities.setdefault(
            entity_id, _typed_node(entity_id, "prov:Entity", "inlumen:RunArtifact")
        )
        node["sha256"] = digest
        if path and not node.get("label"):
            node["label"] = path
        if isinstance(size, int):
            node["sizeBytes"] = size
        return entity_id

    run_node = _typed_node(run_iri, "prov:Activity", "inlumen:PipelineRun", label=f"Pipeline run {run_id}")
    run_node["status"] = _text(result.get("status") or run.get("status"))
    _set_time(run_node, "startedAtTime", run.get("started_at") or run.get("created_at"))
    _set_time(run_node, "endedAtTime", run.get("finished_at"))
    graph.append(run_node)

    step_iris: dict[str, str] = {}
    steps = [item for item in result.get("steps") or [] if isinstance(item, dict)]
    for step in steps:
        flow_id = _text(step.get("flow_id"), "step")
        step_iri = f"{run_iri}:step:{_iri_part(flow_id, 'step')}"
        step_iris[flow_id] = step_iri
        node = _typed_node(
            step_iri, "prov:Activity", "inlumen:StepExecution",
            label=_text(step.get("node_label"), f"Step {flow_id}"),
        )
        node["partOf"] = run_iri
        node["status"] = _text(step.get("status"))
        _set_time(node, "startedAtTime", _epoch_iso(step.get("started_at")))
        _set_time(node, "endedAtTime", _epoch_iso(step.get("finished_at")))
        used = [
            entity(_digest(item.get("sha256")), _text(item.get("path")), item.get("size_bytes"))
            for item in step.get("inputs") or []
            if isinstance(item, dict) and _digest(item.get("sha256"))
        ]
        if used:
            node["used"] = sorted(set(used))
        informed_by = sorted({
            _text(item.get("source_node"))
            for item in step.get("connections") or []
            if isinstance(item, dict) and _text(item.get("source_node"))
        })
        if informed_by:
            node["wasInformedBy"] = [
                step_iris.get(source) or f"{run_iri}:step:{_iri_part(source, 'step')}"
                for source in informed_by
            ]
        graph.append(node)
        for item in step.get("outputs") or []:
            if isinstance(item, dict) and _digest(item.get("sha256")):
                generated = entities[entity(_digest(item.get("sha256")), "", item.get("size_bytes"))]
                generated.setdefault("wasGeneratedBy", step_iri)

    # Public output paths, ports and producers from the run result (these
    # cover runs recorded before step receipts existed as well).
    for output in result.get("outputs") or []:
        if not isinstance(output, dict) or not _digest(output.get("sha256")):
            continue
        node = entities[entity(_digest(output.get("sha256")), "", output.get("size_bytes"))]
        node["path"] = _text(output.get("path"))
        node["label"] = _text(output.get("filename"), node["path"])
        if output.get("port"):
            node["port"] = _text(output.get("port"))
        producer = _text(output.get("source_node"))
        if producer and "wasGeneratedBy" not in node:
            node["wasGeneratedBy"] = step_iris.get(producer) or f"{run_iri}:step:{_iri_part(producer, 'step')}"

    graph.extend(entities.values())
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "@context": RUN_PROV_O_CONTEXT,
        "@id": f"{run_iri}:bundle",
        "@type": "prov:Bundle",
        "label": f"inLUMEN lineage for pipeline run {run_id}",
        "generatedAtTime": generated_at,
        "@graph": graph,
    }
