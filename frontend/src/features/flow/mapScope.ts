/**
 * Parallel Map scopes — browser mirror of inlumen.contracts.map_scope
 * (packages/agents/src/inlumen/contracts/map_scope.py).
 *
 * A Parallel Map opens a scope and a Collect closes it; every step between
 * them runs once per item. Both implementations are tested against the same
 * graphs (mapScope.test.ts, backend/tests/test_map_scope.py).
 */
export type ScopeConnection = { source: string; sourcePort: string; target: string; targetPort: string };
export type MapScope = { map: string; collect: string; body: string[] };
export type ScopeIssue = { code: string; message: string; nodeId: string };

const templateKey = (value: unknown) => String(value ?? "").trim().toLowerCase().split(/\s+/).join(" ");
export const isMapTemplate = (value: unknown) => templateKey(value) === "parallel map";
export const isCollectTemplate = (value: unknown) => templateKey(value) === "collect";

export const mapScopes = (
  templates: Record<string, string>,
  connections: ScopeConnection[],
  labels: Record<string, string> = {},
): { scopes: MapScope[]; issues: ScopeIssue[] } => {
  const outgoing = new Map<string, ScopeConnection[]>();
  const incoming = new Map<string, ScopeConnection[]>();
  connections.forEach((edge) => {
    outgoing.set(edge.source, [...(outgoing.get(edge.source) || []), edge]);
    incoming.set(edge.target, [...(incoming.get(edge.target) || []), edge]);
  });
  const name = (id: string) => `“${labels[id] || id}”`;
  const scopes: MapScope[] = [];
  const issues: ScopeIssue[] = [];
  const closedBy = new Map<string, string[]>();

  Object.keys(templates).filter((id) => isMapTemplate(templates[id])).sort().forEach((mapId) => {
    const reachable: string[] = [];
    const collects: string[] = [];
    const seen = new Set([mapId]);
    const queue = (outgoing.get(mapId) || []).map((edge) => edge.target);
    while (queue.length) {
      const node = queue.shift()!;
      if (seen.has(node)) continue;
      seen.add(node);
      if (isCollectTemplate(templates[node])) {
        if (!collects.includes(node)) collects.push(node);
        continue;
      }
      if (isMapTemplate(templates[node])) {
        issues.push({ code: "nested-map", nodeId: node,
          message: `Parallel Map ${name(node)} is inside the per-item steps of ${name(mapId)}. Collect the first map before starting another.` });
        continue;
      }
      reachable.push(node);
      queue.push(...(outgoing.get(node) || []).map((edge) => edge.target));
    }
    if (collects.length === 0) {
      issues.push({ code: "map-without-collect", nodeId: mapId,
        message: `Parallel Map ${name(mapId)} never reaches a Collect. Add a Collect after the per-item steps so later steps receive all results together.` });
      return;
    }
    if (collects.length > 1) {
      issues.push({ code: "map-multiple-collects", nodeId: mapId,
        message: `Parallel Map ${name(mapId)} reaches several Collects (${collects.map(name).join(", ")}); its per-item steps must end at one.` });
      return;
    }
    const collectId = collects[0];
    closedBy.set(collectId, [...(closedBy.get(collectId) || []), mapId]);
    const reachableSet = new Set(reachable);
    const reachesCollect = new Set<string>();
    const pending = (incoming.get(collectId) || []).map((edge) => edge.source);
    while (pending.length) {
      const node = pending.pop()!;
      if (reachesCollect.has(node) || !reachableSet.has(node)) continue;
      reachesCollect.add(node);
      pending.push(...(incoming.get(node) || []).map((edge) => edge.source));
    }
    const body = reachable.filter((node) => reachesCollect.has(node));
    const members = new Set(body);
    reachable.filter((node) => !reachesCollect.has(node)).forEach((node) => {
      // A branch leaving a per-item step is a map-scope-leak (below); one leaving the Map itself is reported here.
      if ((incoming.get(node) || []).some((edge) => edge.source === mapId)) {
        issues.push({ code: "map-branch-without-collect", nodeId: node,
          message: `${name(node)} receives per-item data from ${name(mapId)} but never reaches ${name(collectId)}, so it would not run once per item. Route it into ${name(collectId)}, or connect it after ${name(collectId)}.` });
      }
    });
    body.forEach((node) => {
      (incoming.get(node) || []).forEach((edge) => {
        if (edge.source !== mapId && !members.has(edge.source)) issues.push({ code: "map-side-input", nodeId: node,
          message: `${name(node)} runs once per item of ${name(mapId)} but also receives ${name(edge.source)} from outside the map. Per-item steps take their input only from the map.` });
      });
      (outgoing.get(node) || []).forEach((edge) => {
        if (edge.target !== collectId && !members.has(edge.target)) issues.push({ code: "map-scope-leak", nodeId: node,
          message: `${name(node)} runs once per item of ${name(mapId)} and cannot feed ${name(edge.target)} directly. Route per-item results through ${name(collectId)}.` });
      });
    });
    (incoming.get(collectId) || []).forEach((edge) => {
      if (edge.source !== mapId && !members.has(edge.source)) issues.push({ code: "collect-outside-input", nodeId: collectId,
        message: `Collect ${name(collectId)} receives ${name(edge.source)}, which is not a per-item step of ${name(mapId)}.` });
    });
    scopes.push({ map: mapId, collect: collectId, body });
  });

  Object.keys(templates).filter((id) => isCollectTemplate(templates[id])).sort().forEach((collectId) => {
    const maps = closedBy.get(collectId) || [];
    if (maps.length === 0) issues.push({ code: "collect-without-map", nodeId: collectId,
      message: `Collect ${name(collectId)} does not close a Parallel Map. Place it after the steps that run once per item.` });
    else if (maps.length > 1) issues.push({ code: "collect-closes-several-maps", nodeId: collectId,
      message: `Collect ${name(collectId)} closes several Parallel Maps; give each map its own Collect.` });
  });
  return { scopes, issues };
};
