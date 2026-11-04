"""Parallel Map scopes: which steps run once per item.

A Parallel Map opens a scope and a Collect closes it. Every step reachable
from the Map without passing through a Collect is in the scope's body and runs
once per item; the Collect gathers the per-item results back into one
collection for the steps after it.

    Source -> Parallel Map -> Extract -> Parse -> Collect -> Aggregate
                              \\_____ body _____/

Rules (v1):
- every Parallel Map reaches exactly one Collect, and every body path ends there;
- a body step receives data only from the Map or from other body steps;
- body steps feed only body steps or the scope's Collect;
- scopes do not nest, and a Collect closes exactly one Map.

Canonical copy: shared/map_scope.py (synced by scripts/sync_shared.py); the
browser mirror is frontend/src/features/flow/mapScope.ts.
"""
from __future__ import annotations

from collections import deque
from typing import Iterable, NamedTuple

MAP_TEMPLATE = "parallel map"
COLLECT_TEMPLATE = "collect"


class Connection(NamedTuple):
    source: str
    source_port: str
    target: str
    target_port: str


class MapScope(NamedTuple):
    map: str
    collect: str
    body: list[str]


class ScopeIssue(NamedTuple):
    code: str
    message: str
    node_id: str


def template_key(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def is_map(template: object) -> bool:
    return template_key(template) == MAP_TEMPLATE


def is_collect(template: object) -> bool:
    return template_key(template) == COLLECT_TEMPLATE


def map_scopes(
    templates: dict[str, str],
    connections: Iterable[Connection],
    labels: dict[str, str] | None = None,
) -> tuple[list[MapScope], list[ScopeIssue]]:
    """Resolve every Parallel Map's scope; ``templates`` maps node id -> Flow template."""
    labels = labels or {}
    edges = [Connection(*item) for item in connections]
    outgoing: dict[str, list[Connection]] = {}
    incoming: dict[str, list[Connection]] = {}
    for edge in edges:
        outgoing.setdefault(edge.source, []).append(edge)
        incoming.setdefault(edge.target, []).append(edge)

    def name(node_id: str) -> str:
        return repr(labels.get(node_id) or node_id)

    scopes: list[MapScope] = []
    issues: list[ScopeIssue] = []
    closed_by: dict[str, list[str]] = {}
    for map_id in sorted(node for node, template in templates.items() if is_map(template)):
        body: list[str] = []
        collects: list[str] = []
        seen = {map_id}
        queue = deque(edge.target for edge in outgoing.get(map_id, []))
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            if is_collect(templates.get(node)):
                if node not in collects:
                    collects.append(node)
                continue
            if is_map(templates.get(node)):
                issues.append(ScopeIssue(
                    "nested-map",
                    f"Parallel Map {name(node)} is inside the per-item steps of {name(map_id)}. "
                    "Collect the first map before starting another.",
                    node,
                ))
                continue
            body.append(node)
            queue.extend(edge.target for edge in outgoing.get(node, []))

        if not collects:
            issues.append(ScopeIssue(
                "map-without-collect",
                f"Parallel Map {name(map_id)} never reaches a Collect. Add a Collect after the "
                "per-item steps so later steps receive all results together.",
                map_id,
            ))
            continue
        if len(collects) > 1:
            issues.append(ScopeIssue(
                "map-multiple-collects",
                f"Parallel Map {name(map_id)} reaches several Collects "
                f"({', '.join(name(item) for item in collects)}); its per-item steps must end at one.",
                map_id,
            ))
            continue
        collect_id = collects[0]
        closed_by.setdefault(collect_id, []).append(map_id)
        # Per-item steps lie between the Map and its Collect; a step reached
        # from the map that never reaches the Collect is a leak, not body.
        reachable = set(body)
        reaches_collect: set[str] = set()
        pending = [edge.source for edge in incoming.get(collect_id, [])]
        while pending:
            node = pending.pop()
            if node in reaches_collect or node not in reachable:
                continue
            reaches_collect.add(node)
            pending.extend(edge.source for edge in incoming.get(node, []))
        body = [node for node in body if node in reaches_collect]
        members = set(body)
        for node in body:
            for edge in incoming.get(node, []):
                if edge.source != map_id and edge.source not in members:
                    issues.append(ScopeIssue(
                        "map-side-input",
                        f"{name(node)} runs once per item of {name(map_id)} but also receives "
                        f"{name(edge.source)} from outside the map. Per-item steps take their "
                        "input only from the map.",
                        node,
                    ))
            for edge in outgoing.get(node, []):
                if edge.target != collect_id and edge.target not in members:
                    issues.append(ScopeIssue(
                        "map-scope-leak",
                        f"{name(node)} runs once per item of {name(map_id)} and cannot feed "
                        f"{name(edge.target)} directly. Route per-item results through "
                        f"{name(collect_id)}.",
                        node,
                    ))
        for edge in incoming.get(collect_id, []):
            if edge.source != map_id and edge.source not in members:
                issues.append(ScopeIssue(
                    "collect-outside-input",
                    f"Collect {name(collect_id)} receives {name(edge.source)}, which is not a "
                    f"per-item step of {name(map_id)}.",
                    collect_id,
                ))
        ordered = _topological(body, edges)
        scopes.append(MapScope(map_id, collect_id, ordered))

    for collect_id in sorted(node for node, template in templates.items() if is_collect(template)):
        maps = closed_by.get(collect_id, [])
        if not maps:
            issues.append(ScopeIssue(
                "collect-without-map",
                f"Collect {name(collect_id)} does not close a Parallel Map. Place it after the "
                "steps that run once per item.",
                collect_id,
            ))
        elif len(maps) > 1:
            issues.append(ScopeIssue(
                "collect-closes-several-maps",
                f"Collect {name(collect_id)} closes several Parallel Maps; give each map its own Collect.",
                collect_id,
            ))
    return scopes, issues


def _topological(body: list[str], edges: list[Connection]) -> list[str]:
    members = set(body)
    indegree = {node: 0 for node in body}
    for edge in edges:
        if edge.source in members and edge.target in members:
            indegree[edge.target] += 1
    ready = sorted(node for node, count in indegree.items() if count == 0)
    ordered = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for edge in edges:
            if edge.source == node and edge.target in members:
                indegree[edge.target] -= 1
                if indegree[edge.target] == 0:
                    ready.append(edge.target)
                    ready.sort()
    return ordered + [node for node in body if node not in ordered]
