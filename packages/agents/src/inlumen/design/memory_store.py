"""A :class:`~inlumen.design.store.GraphStore` that keeps the graph in Python.

Each operation reproduces the backend's Cypher query for it: the same
preconditions, layout rules, flow ids, port handles and result rows. The store
contract tests run the same scenarios against this store and the Neo4j one.

One workspace holds one design pipeline and a catalog of reusable pipelines.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from inlumen.contracts.graph_records import flow_edges, flows_from_edges, step_node, step_properties
from inlumen.contracts.subpipeline_reference import (
    derive_subpipeline_interface,
    normalize_reusable_pipeline_graph,
    public_ports_for_interface,
)

from .store import FlowConfiguration, NewStep, Predecessor, Rows, SubpipelineConfiguration

_NUMERIC = re.compile(r"^[0-9]+$")
_STEP_FIELDS = (
    "uid", "flow_id", "type", "label", "description", "template_label",
    "configuration_status", "ports_json", "primary_input_port",
    "primary_output_port", "x", "y",
)
_PIPELINE_FIELDS = (
    "uid", "name", "label", "description", "version", "active_version_uid",
    "settings_json", "status", "last_run_id", "last_run_status", "last_run_engine",
    "last_run_snapshot_sha256", "last_run_started_at", "last_run_finished_at",
    "last_run_duration_ms", "last_run_output_count", "last_run_error",
    "created_at", "updated_at",
)


def _records_provenance(operation):
    """After a graph mutation the backend records a provenance event, which ensures
    a design pipeline exists and normalizes its version (``_ensure_design_pipeline``).
    Mirror that, whether or not the mutation matched anything."""

    async def wrapper(self, *args, **kwargs):
        try:
            return await operation(self, *args, **kwargs)
        finally:
            self._ensure_design_pipeline()

    wrapper.__name__ = operation.__name__
    wrapper.__doc__ = operation.__doc__
    return wrapper


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _rows(rows: list[dict[str, Any]]) -> Rows:
    return json.dumps(rows, ensure_ascii=False)


def _lower(value: object) -> str:
    return str(value or "").lower()


def _default_input_port(step: dict[str, Any]) -> str:
    """``contract.default_input_port_expression`` for one step."""
    if step.get("primary_input_port") is not None:
        return step["primary_input_port"]
    template = _lower(step.get("template_label"))
    if step.get("type") == "flow" and template == "condition":
        return "value"
    if step.get("type") == "flow" and template == "parallel map":
        return "items"
    if step.get("type") == "flow" and template == "collect":
        return "item"
    if step.get("type") == "destination":
        return "data"
    return "input"


def _default_output_port(step: dict[str, Any]) -> str:
    """``contract.default_output_port_expression`` for one step."""
    if step.get("primary_output_port") is not None:
        return step["primary_output_port"]
    template = _lower(step.get("template_label"))
    if step.get("type") == "source":
        return "data"
    if step.get("type") == "flow" and template == "condition":
        return "when_true"
    if step.get("type") == "flow" and template == "parallel map":
        return "item"
    if step.get("type") == "flow" and template == "collect":
        return "items"
    return "output"


def _file_refs(data: dict[str, Any]) -> list[dict[str, str]]:
    raw_files = data.get("file_buckets") if isinstance(data.get("file_buckets"), list) else data.get("files")
    refs, seen = [], set()
    for item in raw_files if isinstance(raw_files, list) else []:
        if isinstance(item, str):
            filename, bucket, role = item.strip(), "", ""
        elif isinstance(item, dict):
            filename = str(item.get("filename") or item.get("name") or "").strip()
            bucket = str(item.get("bucket") or "").strip().lower()
            role = str(item.get("role") or "").strip().lower()
        else:
            continue
        if not filename or (filename, bucket) in seen:
            continue
        seen.add((filename, bucket))
        # Same fields as the backend's FILE records; there is no object storage here.
        refs.append({
            "filename": filename, "bucket": bucket,
            "role": role if role in {"code", "data"} else None,
            "snapshot_bucket": None, "snapshot_object": None, "added_at": _now(),
        })
    return refs


class InMemoryGraphStore:
    """A design workspace in memory.

    ``graph`` (a canvas graph, as :meth:`get_graph` returns) seeds the design
    pipeline. ``reusable_pipelines`` seeds the catalog: each item has a
    ``name``, a ``graph`` and optionally a ``description`` and ``uid``.
    """

    def __init__(
        self,
        graph: dict | None = None,
        *,
        reusable_pipelines: Iterable[dict[str, Any]] = (),
    ):
        self._pipelines: list[dict[str, Any]] = []
        self._steps: dict[str, dict[str, Any]] = {}  # uid -> STEP properties
        self._members: dict[str, str] = {}  # step uid -> pipeline uid
        self._files: dict[str, list[dict[str, str]]] = {}
        self._flows: list[dict[str, Any]] = []  # {source, target (uids), source_port, target_port}
        self._versions: dict[str, dict[str, Any]] = {}  # PIPELINE_VERSION by uid
        self._version_links: set[tuple[str, str]] = set()  # (pipeline uid, version uid)
        for item in reusable_pipelines:
            self._add_reusable(
                str(item.get("name") or ""),
                str(item.get("description") or ""),
                normalize_reusable_pipeline_graph(item.get("graph") or {}),
                uid=item.get("uid"),
            )
        if graph is not None:
            self._restore(graph)

    # Queries over the stored records

    def _design_pipelines(self) -> list[dict[str, Any]]:
        return [p for p in self._pipelines if p.get("status") == "design"]

    def _reusable(self) -> list[dict[str, Any]]:
        return [p for p in self._pipelines if p.get("status") == "reusable"]

    def _steps_of(self, pipeline: dict[str, Any]) -> list[dict[str, Any]]:
        return [step for uid, step in self._steps.items() if self._members.get(uid) == pipeline["uid"]]

    def _active_design(self) -> dict[str, Any] | None:
        """The design pipeline with the most steps, then the latest update."""
        candidates = self._design_pipelines()
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda p: (-len(self._steps_of(p)), _descending(p.get("updated_at"))),
        )[0]

    def _step(self, pipeline: dict[str, Any], **match: Any) -> dict[str, Any] | None:
        for step in self._steps_of(pipeline):
            if all(step.get(key) == value for key, value in match.items()):
                return step
        return None

    def _design_step(self, **match: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for pipeline in self._design_pipelines():
            step = self._step(pipeline, **match)
            if step is not None:
                return pipeline, step
        return None

    def _outgoing(self, step: dict[str, Any]) -> list[dict[str, Any]]:
        return [flow for flow in self._flows if flow["source"] == step["uid"]]

    def _incoming(self, step: dict[str, Any]) -> list[dict[str, Any]]:
        return [flow for flow in self._flows if flow["target"] == step["uid"]]

    def _between(self, source: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
        return [f for f in self._flows if f["source"] == source["uid"] and f["target"] == target["uid"]]

    def _merge_flow(self, source: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
        """``MERGE (source)-[:FLOWS_TO]->(target)``: every existing one, or a new one."""
        existing = self._between(source, target)
        if existing:
            return existing
        flow = {"source": source["uid"], "target": target["uid"], "source_port": None, "target_port": None}
        self._flows.append(flow)
        return [flow]

    def _reachable(self, start: dict[str, Any], *, include_start: bool, max_hops: int | None = None) -> list[dict[str, Any]]:
        """Steps reachable over FLOWS_TO, in breadth-first order, without repeats."""
        order: list[str] = [start["uid"]] if include_start else []
        seen = {start["uid"]}
        frontier, hops = [start["uid"]], 0
        while frontier and (max_hops is None or hops < max_hops):
            hops += 1
            following = []
            for uid in frontier:
                for flow in self._flows:
                    if flow["source"] == uid and flow["target"] not in seen:
                        seen.add(flow["target"])
                        following.append(flow["target"])
                        order.append(flow["target"])
            frontier = following
        return [self._steps[uid] for uid in order if uid in self._steps]

    def _reaches(self, start: dict[str, Any], goal: dict[str, Any], max_hops: int) -> bool:
        frontier, hops = {start["uid"]}, 0
        while frontier and hops < max_hops:
            hops += 1
            frontier = {f["target"] for f in self._flows if f["source"] in frontier}
            if goal["uid"] in frontier:
                return True
        return False

    def _next_flow_id(self, steps: Iterable[dict[str, Any]]) -> str:
        numbers = [int(s["flow_id"]) for s in steps if _NUMERIC.match(str(s.get("flow_id") or ""))]
        return str(max(numbers, default=0) + 1)

    def _create_step(self, pipeline: dict[str, Any], step: NewStep, **placement: Any) -> dict[str, Any]:
        props = {"uid": str(uuid.uuid4()), **deepcopy(step.props), **placement}
        self._steps[props["uid"]] = props
        self._members[props["uid"]] = pipeline["uid"]
        return props

    def _get_or_create_design(self) -> dict[str, Any]:
        pipeline = self._active_design()
        if pipeline is None:
            now = _now()
            pipeline = {
                "uid": str(uuid.uuid4()), "name": "", "label": "", "description": "",
                "version": "1.0", "created_at": now, "updated_at": now, "status": "design",
            }
            self._pipelines.append(pipeline)
        return pipeline

    def _ensure_design_pipeline(self) -> dict[str, Any]:
        pipeline = self._active_design()
        if pipeline is None:
            now = _now()
            pipeline = {
                "uid": str(uuid.uuid4()), "name": "", "label": "", "description": "",
                "version": "Main", "active_version_uid": "main",
                "created_at": now, "updated_at": now, "status": "design",
            }
            self._pipelines.append(pipeline)
            return pipeline
        pipeline["version"] = "Main" if pipeline.get("active_version_uid") is None else (pipeline.get("version") or "Main")
        pipeline["active_version_uid"] = pipeline.get("active_version_uid") or "main"
        pipeline["updated_at"] = pipeline.get("updated_at") or _now()
        return pipeline

    def _delete_step(self, step: dict[str, Any]) -> None:
        uid = step["uid"]
        self._flows = [f for f in self._flows if uid not in (f["source"], f["target"])]
        self._steps.pop(uid, None)
        self._members.pop(uid, None)
        self._files.pop(uid, None)

    def _add_reusable(self, name: str, description: str, graph: dict, *, uid: str | None = None) -> dict[str, Any]:
        interface = derive_subpipeline_interface(graph)
        now = _now()
        record = {
            "uid": uid or str(uuid.uuid4()), "name": name, "label": name,
            "description": description, "status": "reusable",
            "graph_json": json.dumps(graph, ensure_ascii=True, sort_keys=True),
            "interface_json": json.dumps(interface, ensure_ascii=True, sort_keys=True),
            "public_ports_json": json.dumps(public_ports_for_interface(interface), ensure_ascii=True, sort_keys=True),
            "node_count": len(graph.get("nodes") or []), "edge_count": len(graph.get("edges") or []),
            "created_at": now, "updated_at": now,
        }
        self._pipelines.append(record)
        return record

    def _reusable_row(self, record: dict[str, Any]) -> dict[str, Any]:
        # Reusable pipelines kept here have no legacy PIPELINE_VERSION nodes.
        return {
            "pipeline_uid": record.get("uid"), "pipeline_name": record.get("name"),
            "version_uid": None, "version_name": None,
            "graph_json": record.get("graph_json"),
            "interface_json": record.get("interface_json"),
            "public_ports_json": record.get("public_ports_json"),
        }

    # Reads

    async def get_graph(self) -> dict:
        return self.graph()

    def graph(self) -> dict:
        """The design pipeline as a canvas graph (the synchronous :meth:`get_graph`)."""
        empty = {"updated_at": None, "nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}
        pipeline_record = self._active_design()
        if pipeline_record is None:
            return empty
        pipeline = {key: pipeline_record.get(key) for key in _PIPELINE_FIELDS}
        settings: dict = {}
        settings_json = pipeline.get("settings_json")
        if isinstance(settings_json, str) and settings_json.strip():
            try:
                parsed = json.loads(settings_json)
                settings = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                settings = {}
        steps = self._steps_of(pipeline_record)
        pipeline["design_pipeline_count"] = len(self._design_pipelines())
        pipeline["step_count"] = len(steps)
        active_version_uid = pipeline.get("active_version_uid")
        if isinstance(active_version_uid, str) and active_version_uid.strip():
            linked = (pipeline_record["uid"], active_version_uid.strip()) in self._version_links
            version = self._versions.get(active_version_uid.strip()) if linked else None
            pipeline["active_version_name"] = version.get("name") if version else None

        def resolve_reusable(pipeline_uid: str, _legacy_version_uid: str | None):
            for record in self._reusable():
                if record.get("uid") == pipeline_uid and record.get("graph_json") is not None:
                    return {"pipeline_name": record.get("name"), "graph_json": record.get("graph_json")}
            return None

        nodes = []
        for step in steps:
            node = step_node(deepcopy(step), deepcopy(self._files.get(step["uid"], [])), resolve_reusable=resolve_reusable)
            if node is not None:
                nodes.append(node)
        flows = []
        for step in steps:
            for flow in self._outgoing(step):
                target = self._steps.get(flow["target"])
                if target is not None:
                    flows.append({
                        "source": step.get("flow_id"), "target": target.get("flow_id"),
                        "source_port": flow["source_port"], "target_port": flow["target_port"],
                    })
        return {
            "updated_at": pipeline.get("updated_at"),
            "pipeline": pipeline,
            "settings": settings,
            "nodes": nodes,
            "edges": flow_edges(flows, nodes),
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }

    async def overview(self) -> Rows:
        rows = []
        for pipeline in self._pipelines:
            projected = {key: pipeline.get(key) for key in (
                "uid", "name", "label", "description", "version", "status", "created_at", "updated_at")}
            for step in self._steps_of(pipeline) or [None]:
                outgoing = [
                    (flow, self._steps[flow["target"]])
                    for flow in (self._outgoing(step) if step else [])
                    if flow["target"] in self._steps
                ] or [(None, None)]
                for flow, following in outgoing:
                    flow_id = str(step.get("flow_id")) if step and step.get("flow_id") is not None else None
                    rows.append({
                        "pipeline": projected,
                        "step": {key: step.get(key) for key in _STEP_FIELDS} if step else None,
                        "step_order": int(flow_id) if flow_id and _NUMERIC.match(flow_id) else None,
                        "flow": {"source_port": flow["source_port"], "target_port": flow["target_port"]} if flow else None,
                        "next_step": {key: following.get(key) for key in _STEP_FIELDS} if following else None,
                    })
        rows.sort(key=lambda row: (
            row["pipeline"]["label"] is None, row["pipeline"]["label"] or "",
            row["step_order"] is None, row["step_order"] or 0,
        ))
        return _rows(rows)

    async def find_predecessor(self, flow_id: str) -> Rows:
        pipeline = self._active_design()
        step = self._step(pipeline, flow_id=flow_id) if pipeline else None
        if step is None:
            return _rows([])
        return _rows([{"predecessor": {
            "pipeline_uid": pipeline["uid"], "flow_id": step.get("flow_id"), "type": step.get("type"),
            "template_label": step.get("template_label"), "ports_json": step.get("ports_json"),
            "primary_output_port": step.get("primary_output_port"),
        }}])

    async def connection_context(self, source_flow_id: str, target_flow_id: str) -> Rows:
        rows = []
        for pipeline in self._design_pipelines():
            source = self._step(pipeline, flow_id=source_flow_id)
            target = self._step(pipeline, flow_id=target_flow_id)
            if source and target and source.get("type") != "destination" and target.get("type") != "source":
                rows.append({"connection_context": {
                    "source": {key: source.get(key) for key in (
                        "flow_id", "type", "template_label", "ports_json", "primary_output_port")},
                    "target": {key: target.get(key) for key in (
                        "flow_id", "type", "template_label", "ports_json", "primary_input_port")},
                }})
        return _rows(rows)

    async def list_reusable_pipelines(self) -> Rows:
        records = sorted(self._reusable(), key=lambda p: (p.get("name") is None, p.get("name") or ""))
        return _rows([{"reusable_pipeline": {
            "pipeline_uid": p.get("uid"), "pipeline_name": p.get("name"), "description": p.get("description"),
            "interface_json": p.get("interface_json"), "node_count": p.get("node_count"),
            "edge_count": p.get("edge_count"),
        }} for p in records])

    async def find_reusable_pipeline_by_name(self, name: str) -> Rows:
        wanted = name.strip().lower()
        for record in self._reusable():
            if str(record.get("name") or "").strip().lower() == wanted:
                return _rows([{"reusable_pipeline": {
                    "pipeline_uid": record.get("uid"), "pipeline_name": record.get("name")}}])
        return _rows([])

    async def find_reusable_pipelines(
        self,
        *,
        pipeline_uid: str,
        pipeline_name: str,
        version_uid: str,
        version_name: str,
    ) -> Rows:
        wanted = pipeline_name.strip().lower()

        def matches(record: dict[str, Any]) -> bool:
            if pipeline_uid:
                return record.get("uid") == pipeline_uid
            name = record.get("name")
            if name is None:
                return False
            name = str(name).strip().lower()
            return name == wanted or name in wanted

        found = [r for r in self._reusable() if matches(r) and r.get("graph_json") is not None]
        found.sort(key=lambda r: (str(r.get("name") or "").strip().lower() != wanted, str(r.get("name") or "")))
        return _rows([{"reusable_pipeline": self._reusable_row(r)} for r in found[:2]])

    async def get_reusable_pipeline(self, pipeline_uid: str, version_uid: str) -> Rows:
        for record in self._reusable():
            if record.get("uid") == pipeline_uid and record.get("graph_json") is not None:
                return _rows([{"reusable_pipeline": self._reusable_row(record)}])
        return _rows([])

    async def inspect_subpipeline_step(self, flow_id: str) -> Rows:
        found = self._design_step(flow_id=flow_id, type="subpipeline")
        if found is None:
            return _rows([])
        _, step = found
        connected_inputs, connected_outputs = [], []
        for flow in self._incoming(step):
            if flow["target_port"] is not None and flow["target_port"] not in connected_inputs:
                connected_inputs.append(flow["target_port"])
        for flow in self._outgoing(step):
            if flow["source_port"] is not None and flow["source_port"] not in connected_outputs:
                connected_outputs.append(flow["source_port"])
        return _rows([{"subpipeline_context": {
            "current_ports_json": step.get("ports_json"),
            "connected_inputs": connected_inputs,
            "connected_outputs": connected_outputs,
        }}])

    # Writes

    @_records_provenance
    async def upsert_pipeline(self, name: str, description: str, version: str) -> Rows:
        existing = self._active_design()
        design_pipeline_count = len(self._design_pipelines())
        now = _now()
        if existing is None:
            pipeline = {
                "uid": str(uuid.uuid4()), "name": name, "label": name, "description": description,
                "version": version if version != "" else "Main", "active_version_uid": "main",
                "created_at": now, "updated_at": now, "status": "design",
            }
            self._pipelines.append(pipeline)
            created = True
        else:
            pipeline = existing
            pipeline["name"] = name if name != "" else (pipeline.get("name") or pipeline.get("label") or "")
            pipeline["label"] = name if name != "" else (pipeline.get("label") or pipeline.get("name") or "")
            pipeline["description"] = description if description != "" else (pipeline.get("description") or "")
            pipeline["version"] = version if version != "" else (pipeline.get("version") or "Main")
            pipeline["updated_at"] = now
            created = False
        active_version_uid = pipeline.get("active_version_uid") or "main"
        active_version_name = "Main" if active_version_uid == "main" else (pipeline.get("version") or "Main")
        version_record = self._versions.get(active_version_uid)
        if version_record is None:
            version_record = {
                "uid": active_version_uid, "created_at": now,
                "version_index": 0 if active_version_uid == "main" else None,
                "is_main": active_version_uid == "main",
            }
            self._versions[active_version_uid] = version_record
        version_record.update({
            "name": active_version_name, "version": active_version_name,
            "description": pipeline.get("description") or "", "updated_at": now,
        })
        self._version_links.add((pipeline["uid"], active_version_uid))
        pipeline["active_version_uid"] = active_version_uid
        pipeline["version"] = active_version_name
        pipeline["description"] = version_record["description"]
        pipeline["updated_at"] = now
        return _rows([{"pipeline": {
            "uid": pipeline["uid"], "name": pipeline.get("name"), "label": pipeline.get("label"),
            "description": pipeline.get("description"), "version": pipeline.get("version"),
            "active_version_uid": pipeline["active_version_uid"],
            "active_version_name": version_record["name"],
            "active_version_description": version_record["description"],
            "status": pipeline.get("status"), "created": created,
            "design_pipeline_count": design_pipeline_count,
            "created_at": pipeline.get("created_at"), "updated_at": pipeline.get("updated_at"),
        }}])

    def _step_row(self, pipeline: dict[str, Any], created: dict[str, Any], step: NewStep, **extra: Any) -> dict[str, Any]:
        row = {
            "flow_id": created["flow_id"], "uid": created["uid"], "type": created.get("type"),
            "label": created.get("label"), "description": created.get("description"),
            "x": created["x"], "y": created["y"], **extra,
            "pipeline_updated_at": pipeline.get("updated_at"),
        }
        if step.subpipeline:
            reference = step.subpipeline.reference
            row.update({
                "referenced_pipeline_uid": reference["pipeline_uid"],
                "referenced_pipeline_name": reference["pipeline_name"],
                "referenced_version_uid": reference["version_uid"],
                "referenced_version_name": reference["version_name"],
                "public_inputs": list(step.subpipeline.input_ids),
                "public_outputs": list(step.subpipeline.output_ids),
            })
        return {"step": row}

    @_records_provenance
    async def create_step(
        self,
        step: NewStep,
        *,
        target_port: str,
        predecessor: Predecessor | None = None,
    ) -> Rows:
        if predecessor is not None:
            pipeline = next((
                p for p in self._design_pipelines() if p["uid"] == predecessor.pipeline_uid
            ), None)
            previous = self._step(pipeline, flow_id=predecessor.flow_id) if pipeline else None
            if previous is None:
                return _rows([])
            next_flow_id = self._next_flow_id(self._steps_of(pipeline))
            existing_targets = {flow["target"] for flow in self._outgoing(previous)}
            if previous.get("type") == "destination" or not (
                previous.get("type") == "flow" or predecessor.allow_fan_out or not existing_targets
            ):
                return _rows([])
            previous_x = float(previous.get("x") or 0.0)
            previous_y = float(previous.get("y") or 0.0)
            condition = _lower(previous.get("template_label")) == "condition"
            y = previous_y
            if condition and predecessor.source_port == "when_true":
                y = previous_y - 180.0
            elif condition and predecessor.source_port == "when_false":
                y = previous_y + 180.0
            created = self._create_step(pipeline, step, flow_id=next_flow_id, x=previous_x + 300.0, y=y)
            flows = self._merge_flow(previous, created)
            for flow in flows:
                flow["source_port"] = predecessor.source_port
                flow["target_port"] = target_port
            pipeline["updated_at"] = _now()
            return _rows([self._step_row(
                pipeline, created, step,
                after_flow_id=previous.get("flow_id"),
                source_port=flows[0]["source_port"],
                target_port=flows[0]["target_port"],
            )])

        pipeline = self._get_or_create_design()
        pipeline["updated_at"] = _now()
        next_flow_id = self._next_flow_id(self._steps_of(pipeline))
        if step.step_type == "source":
            # Every Source is an independent root: parallel inputs are
            # stacked vertically and never chained to an existing tail.
            source_count = len([s for s in self._steps_of(pipeline) if s.get("type") == "source"])
            created = self._create_step(pipeline, step, flow_id=next_flow_id, x=0.0, y=float(source_count) * 220.0)
            return _rows([self._step_row(pipeline, created, step)])

        tails = [s for s in self._steps_of(pipeline) if not self._outgoing(s)]
        if len(tails) > 1:
            return _rows([])
        previous = tails[0] if tails else None
        if previous is not None and (step.step_type == "source" or previous.get("type") == "destination"):
            return _rows([])
        if previous is None:
            x = y = 0.0
        else:
            x = float(previous.get("x") or 0.0) + 300.0
            y = float(previous.get("y") or 0.0)
            if _lower(previous.get("template_label")) == "condition":
                y -= 180.0
        created = self._create_step(pipeline, step, flow_id=next_flow_id, x=x, y=y)
        if previous is not None:
            template = _lower(previous.get("template_label"))
            if previous.get("type") == "source":
                source_port = previous.get("primary_output_port") or "data"
            elif previous.get("type") == "subpipeline":
                source_port = previous.get("primary_output_port") or "output"
            elif previous.get("type") == "flow" and template == "condition":
                source_port = "when_true"
            elif previous.get("type") == "flow" and template == "parallel map":
                source_port = "item"
            elif previous.get("type") == "flow" and template == "collect":
                source_port = "items"
            else:
                source_port = "output"
            for flow in self._merge_flow(previous, created):
                flow["source_port"] = source_port
                flow["target_port"] = target_port
        return _rows([self._step_row(pipeline, created, step)])

    @_records_provenance
    async def connect_steps(
        self,
        source_flow_id: str,
        target_flow_id: str,
        source_port: str,
        target_port: str,
        allow_fan_out: bool,
    ) -> Rows:
        for pipeline in self._design_pipelines():
            source = self._step(pipeline, flow_id=source_flow_id)
            target = self._step(pipeline, flow_id=target_flow_id)
            if not source or not target or source.get("type") == "destination" or target.get("type") == "source":
                continue
            existing_targets = {flow["target"] for flow in self._outgoing(source)}
            if not (
                source.get("type") == "flow" or allow_fan_out or not existing_targets
                or target["uid"] in existing_targets
            ):
                return _rows([])
            flows = self._merge_flow(source, target)
            now = _now()
            for flow in flows:
                flow["source_port"] = source_port
                flow["target_port"] = target_port
            pipeline["updated_at"] = now
            is_condition = source.get("type") == "flow" and _lower(source.get("template_label")) == "condition"

            def branch_targets(port: str) -> list[dict[str, Any] | None]:
                found: list[dict[str, Any]] = []
                if is_condition:
                    for flow in self._outgoing(source):
                        branch = self._steps.get(flow["target"])
                        if flow["source_port"] == port and branch is not None and branch not in found:
                            found.append(branch)
                return found or [None]

            rows = []
            for flow in flows:
                for true_target in branch_targets("when_true"):
                    for false_target in branch_targets("when_false"):
                        laid_out = true_target is not None and false_target is not None
                        if laid_out:
                            merge = self._reaches(true_target, false_target, 32)
                            source_y = float(source.get("y") or 0.0)
                            true_target["y"] = source_y - 180.0
                            false_target["y"] = source_y if merge else source_y + 180.0
                        rows.append({"connection": {
                            "source_flow_id": source.get("flow_id"), "source_label": source.get("label"),
                            "source_port": flow["source_port"],
                            "target_flow_id": target.get("flow_id"), "target_label": target.get("label"),
                            "target_port": flow["target_port"],
                            "fan_out_allowed": source.get("type") == "flow" or allow_fan_out,
                            "pipeline_updated_at": pipeline["updated_at"],
                            "branch_layout_applied": laid_out,
                        }})
            return _rows(rows)
        return _rows([])

    @_records_provenance
    async def disconnect_steps(
        self,
        source_flow_id: str,
        target_flow_id: str,
        source_port: str,
        target_port: str,
    ) -> Rows:
        for pipeline in self._design_pipelines():
            source = self._step(pipeline, flow_id=source_flow_id)
            target = self._step(pipeline, flow_id=target_flow_id)
            if not source or not target:
                continue
            matched = [
                flow for flow in self._between(source, target)
                if (flow["source_port"] or "") == source_port and (flow["target_port"] or "") == target_port
            ]
            if not matched:
                return _rows([])
            self._flows = [flow for flow in self._flows if all(flow is not m for m in matched)]
            pipeline["updated_at"] = _now()
            return _rows([{"disconnected": {
                "source_flow_id": source.get("flow_id"), "source_label": source.get("label"),
                "source_port": source_port,
                "target_flow_id": target.get("flow_id"), "target_label": target.get("label"),
                "target_port": target_port,
                "deleted_connection_count": len(matched),
                "pipeline_updated_at": pipeline["updated_at"],
            }}])
        return _rows([])

    @_records_provenance
    async def configure_flow_step(self, configuration: FlowConfiguration) -> Rows:
        found = self._design_step(flow_id=configuration.flow_id, type="flow")
        if found is None:
            return _rows([])
        pipeline, step = found
        step["template_label"] = configuration.behavior
        step["param_json"] = json.dumps(configuration.parameters, sort_keys=True)
        step["configuration_status"] = "configured" if configuration.behavior == "Condition" else "unconfigured"
        step["ports_json"] = configuration.ports_json
        pipeline["updated_at"] = _now()
        incoming, outgoing = self._incoming(step), self._outgoing(step)
        for flow in incoming:
            flow["target_port"] = configuration.input_port
        for flow in outgoing:
            keep = configuration.behavior == "Condition" and flow["source_port"] in ("when_true", "when_false")
            if not keep:
                flow["source_port"] = configuration.default_output_port
        return _rows([{"flow_step": {
            "flow_id": step.get("flow_id"), "uid": step.get("uid"), "label": step.get("label"),
            "behavior": step["template_label"], "ports": step["ports_json"],
            "migrated_incoming_connections": len(incoming),
            "migrated_outgoing_connections": len(outgoing),
            "pipeline_updated_at": pipeline["updated_at"],
        }}])

    async def create_reusable_pipeline(
        self,
        name: str,
        description: str,
        graph: dict,
        interface: dict,
        public_ports: dict,
    ) -> Rows:
        record = self._add_reusable(name, description, graph)
        record["interface_json"] = json.dumps(interface, ensure_ascii=True, sort_keys=True)
        record["public_ports_json"] = json.dumps(public_ports, ensure_ascii=True, sort_keys=True)
        return _rows([{"reusable_pipeline": {
            "pipeline_uid": record["uid"], "pipeline_name": record["name"],
            "interface_json": record["interface_json"], "public_ports_json": record["public_ports_json"],
        }}])

    async def configure_subpipeline_step(self, configuration: SubpipelineConfiguration) -> Rows:
        found = self._design_step(flow_id=configuration.flow_id, type="subpipeline")
        if found is None:
            return _rows([])
        pipeline, step = found
        step["subpipeline_json"] = configuration.definition_json
        step["ports_json"] = configuration.ports_json
        step["primary_input_port"] = configuration.input_ids[0]
        step["primary_output_port"] = configuration.output_ids[0]
        pipeline["updated_at"] = _now()
        for flow in self._incoming(step):
            flow["target_port"] = configuration.input_mapping.get(flow["target_port"], flow["target_port"])
        for flow in self._outgoing(step):
            flow["source_port"] = configuration.output_mapping.get(flow["source_port"], flow["source_port"])
        return _rows([{"subpipeline_step": {
            "flow_id": step.get("flow_id"),
            "referenced_pipeline_uid": configuration.pipeline_uid,
            "referenced_version_uid": configuration.version_uid,
            "public_inputs": list(configuration.input_ids),
            "public_outputs": list(configuration.output_ids),
            "input_port_mapping": dict(configuration.input_mapping),
            "output_port_mapping": dict(configuration.output_mapping),
            "pipeline_updated_at": pipeline["updated_at"],
        }}])

    @_records_provenance
    async def insert_step(
        self,
        step: NewStep,
        *,
        before_flow_id: str,
        after_flow_id: str | None,
        new_input_port: str,
        new_output_port: str,
    ) -> Rows:
        found = self._design_step(flow_id=before_flow_id)
        if found is None:
            return _rows([])
        pipeline, before = found
        after = old_flow = None
        if after_flow_id is None:
            if self._incoming(before):
                return _rows([])
        else:
            after = self._step(pipeline, flow_id=after_flow_id)
            if after is None or not self._between(after, before):
                return _rows([])
            old_flow = self._between(after, before)[0]
        insert_x = float(before.get("x") or 0.0)
        insert_y = float(before.get("y") or 0.0)
        downstream = self._reachable(before, include_start=True)
        for node in downstream:
            node["x"] = float(node.get("x") or 0.0) + 300.0
        # Flow ids are unique across every stored step, as in the backend.
        next_flow_id = self._next_flow_id(self._steps.values())
        if old_flow is not None:
            old_source_port, old_target_port = old_flow["source_port"], old_flow["target_port"]
            self._flows = [flow for flow in self._flows if flow is not old_flow]
        created = self._create_step(pipeline, step, flow_id=next_flow_id, x=insert_x, y=insert_y)
        if after is None:
            for flow in self._merge_flow(created, before):
                flow["source_port"] = new_output_port
                flow["target_port"] = _default_input_port(before)
        else:
            for flow in self._merge_flow(after, created):
                flow["source_port"] = old_source_port if old_source_port is not None else _default_output_port(after)
                flow["target_port"] = new_input_port
            for flow in self._merge_flow(created, before):
                flow["source_port"] = new_output_port
                flow["target_port"] = old_target_port if old_target_port is not None else _default_input_port(before)
        pipeline["updated_at"] = _now()
        placement = {"after_flow_id": after.get("flow_id")} if after is not None else {}
        row = {
            "mode": "initial" if after is None else "between",
            "flow_id": created["flow_id"], "uid": created["uid"], "type": created.get("type"),
            "label": created.get("label"), "description": created.get("description"),
            "x": created["x"], "y": created["y"], **placement,
            "before_flow_id": before.get("flow_id"),
            "shifted_flow_ids": [node.get("flow_id") for node in downstream],
            "pipeline_updated_at": pipeline["updated_at"],
        }
        return _rows([{"step": row}])

    @_records_provenance
    async def delete_step(self, step_uid: str) -> Rows:
        found = self._design_step(uid=step_uid)
        if found is None:
            return _rows([])
        pipeline, step = found
        members = {s["uid"] for s in self._steps_of(pipeline)}
        incoming = [f for f in self._incoming(step) if f["source"] in members]
        outgoing = [f for f in self._outgoing(step) if f["target"] in members]
        previous = self._steps[incoming[0]["source"]] if len(incoming) == 1 else None
        following = self._steps[outgoing[0]["target"]] if len(outgoing) == 1 else None
        previous_source_port = incoming[0]["source_port"] if len(incoming) == 1 else None
        following_target_port = outgoing[0]["target_port"] if len(outgoing) == 1 else None
        downstream = self._reachable(following, include_start=True) if following else []
        if len(incoming) <= 1 and len(outgoing) == 1:
            for node in downstream:
                node["x"] = float(node.get("x") or 0.0) - 300.0
        reconnected = len(incoming) == 1 and len(outgoing) == 1
        if reconnected:
            for flow in self._merge_flow(previous, following):
                flow["source_port"] = (
                    previous_source_port if previous_source_port is not None else _default_output_port(previous)
                )
                flow["target_port"] = (
                    following_target_port if following_target_port is not None else _default_input_port(following)
                )
        flow_id, label = step.get("flow_id"), step.get("label")
        self._delete_step(step)
        pipeline["updated_at"] = _now()
        return _rows([{"deleted_step": {
            "step_uid": step_uid, "flow_id": flow_id, "label": label,
            "incoming_count": len(incoming), "outgoing_count": len(outgoing),
            "chain_reconnected": reconnected, "pipeline_updated_at": pipeline["updated_at"],
        }}])

    @_records_provenance
    async def delete_all_steps(self) -> Rows:
        pipeline = self._active_design()
        if pipeline is None:
            return _rows([])
        steps = self._steps_of(pipeline)
        for step in steps:
            self._delete_step(step)
        pipeline["updated_at"] = _now()
        return _rows([{"pipeline": {
            "pipeline_uid": pipeline["uid"],
            "pipeline_label": pipeline.get("label") or pipeline.get("name") or "",
            "deleted_step_count": len(steps),
            "pipeline_updated_at": pipeline["updated_at"],
        }}])

    async def restore_graph(self, graph: dict) -> None:
        self._restore(graph)

    def _restore(self, graph: dict) -> None:
        """Replace the design pipeline with ``graph``, as the backend's graph sync does."""
        raw_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        raw_edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
        nodes, seen = [], set()
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                continue
            data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else raw_node
            flow_id = str(raw_node.get("id") or data.get("id") or "").strip()
            if not flow_id or flow_id in seen:
                continue
            seen.add(flow_id)
            files = _file_refs(data)
            nodes.append((step_properties(flow_id, raw_node, data, files), files))

        pipeline = self._ensure_design_pipeline()
        now = _now()

        visible = {props["flow_id"] for props, _ in nodes}
        for step in list(self._steps.values()):
            if step.get("flow_id") is not None and str(step["flow_id"]) not in visible:
                self._delete_step(step)
        by_flow_id = {str(step.get("flow_id")): step for step in self._steps.values()}
        for props, files in nodes:
            step = by_flow_id.get(props["flow_id"])
            if step is None:
                step = {"uid": str(uuid.uuid4())}
                self._steps[step["uid"]] = step
            step.update(props)
            self._members[step["uid"]] = pipeline["uid"]
            self._files[step["uid"]] = files
        self._flows = []
        by_flow_id = {str(step.get("flow_id")): step for step in self._steps.values()}
        for flow in flows_from_edges(raw_edges, visible):
            source, target = by_flow_id[flow["source"]], by_flow_id[flow["target"]]
            if not any(
                f["source"] == source["uid"] and f["target"] == target["uid"]
                and f["source_port"] == flow["source_port"] and f["target_port"] == flow["target_port"]
                for f in self._flows
            ):
                self._flows.append({
                    "source": source["uid"], "target": target["uid"],
                    "source_port": flow["source_port"], "target_port": flow["target_port"],
                })
        settings = graph.get("settings") if isinstance(graph.get("settings"), dict) else {}
        pipeline["settings_json"] = json.dumps(settings, ensure_ascii=False)
        pipeline["updated_at"] = now


def _descending(value: object) -> tuple[bool, str]:
    """Sort key for ``ORDER BY value DESC``: Neo4j sorts nulls first when descending."""
    text = str(value or "")
    return (value is not None, "".join(chr(0x10FFFF - ord(c)) for c in text))
