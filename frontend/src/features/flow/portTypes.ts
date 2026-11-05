/**
 * Port types derived from artifact contracts — browser mirror of
 * inlumen.contracts.port_types (packages/agents/src/inlumen/contracts/port_types.py). Both apply the rules
 * in portTypes.generated.json (synced from port_types.json there) and are tested against its cases.
 *
 * A port type is a kind, optionally followed by [] for a collection:
 * table, table[], json, any, any[]. "bundle" (mixed members) and "directory"
 * (undeclared members) describe directories that one kind cannot summarise.
 */
import spec from './portTypes.generated.json';

export type PortType = { kind: string; many: boolean };
export type PortIssue = {
  severity: 'error' | 'warning';
  code: 'incompatible-port-kinds' | 'collection-into-single' | 'single-into-collection' | 'unchecked-bundle';
  message: string;
};
type ArtifactLike = {
  kind?: unknown;
  filename?: unknown;
  path?: unknown;
  format?: unknown;
  representation?: unknown;
  members?: unknown;
  item?: unknown;
  port?: unknown;
  name?: unknown;
};

const WILDCARDS = new Set<string>(spec.wildcards);
const OPAQUE_KINDS = new Set<string>(spec.opaque_kinds);
const EXTENSION_KINDS: Record<string, string> = spec.extension_kinds;

export const FILES_AS_PARAMETER = 'files_as';
export const FILES_AS_COLLECTION = 'collection';
export const FILES_AS_SEPARATE = 'separate';
export type FilesAs = typeof FILES_AS_COLLECTION | typeof FILES_AS_SEPARATE;

const NON_DATA_FILENAMES = new Set([
  'main.py', 'requirements.txt', 'node-manifest.json', 'validation-report.json', 'inlumen.task.json',
]);

export const parsePortType = (value: unknown): PortType => {
  const text = String(value ?? '').trim().toLowerCase();
  const many = text.endsWith('[]');
  const kind = many ? text.slice(0, -2).trim() : text;
  return { kind: WILDCARDS.has(kind) ? 'any' : kind, many };
};

export const formatPortType = (type: PortType) => (type.many ? `${type.kind}[]` : type.kind);

const baseName = (path: string) => path.split('/').pop() || '';

export const kindForFilename = (filename: unknown) => {
  const name = baseName(String(filename ?? ''));
  const dot = name.lastIndexOf('.');
  const suffix = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  return EXTENSION_KINDS[suffix] || 'binary';
};

const itemKind = (item: ArtifactLike) => {
  const kind = String(item.kind ?? '').trim().toLowerCase();
  if (kind && kind !== 'binary') return kind;
  let guessed = kindForFilename(item.filename ?? item.path ?? '');
  if (guessed === 'binary' && item.format) guessed = kindForFilename(`x.${String(item.format)}`);
  return guessed;
};

export const collectionType = (kinds: Iterable<string>) => {
  const distinct = new Set([...kinds].filter(Boolean));
  if (distinct.size === 0) return 'directory';
  if (distinct.size === 1) return `${[...distinct][0]}[]`;
  return 'bundle';
};

export const artifactPortType = (artifact: ArtifactLike): string => {
  const isDirectory = artifact.representation === 'directory' || artifact.kind === 'directory';
  if (!isDirectory) return itemKind(artifact);
  if (artifact.item && typeof artifact.item === 'object') {
    // A collection of per-item results (Parallel Map / Collect).
    return `${artifactPortType(artifact.item as ArtifactLike).replace(/\[\]$/, '')}[]`;
  }
  const members = Array.isArray(artifact.members)
    ? artifact.members.filter((member): member is ArtifactLike => Boolean(member) && typeof member === 'object')
    : [];
  return collectionType(members.map(itemKind));
};

/** Stable, unique output-port ids for a Source that publishes each file separately. */
export const filePortIds = (filenames: string[]) => {
  const used = new Set<string>();
  return filenames.map((filename) => {
    const name = baseName(filename);
    const dot = name.lastIndexOf('.');
    const stem = (dot > 0 ? name.slice(0, dot) : name).toLowerCase();
    let base = stem.replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '') || 'file';
    if (!/^[a-z]/.test(base)) base = `f_${base}`;
    let candidate = base;
    let suffix = 2;
    while (used.has(candidate)) {
      candidate = `${base}_${suffix}`;
      suffix += 1;
    }
    used.add(candidate);
    return candidate;
  });
};

export const filesAs = (parameters: unknown): FilesAs => {
  const value = parameters && typeof parameters === 'object'
    ? String((parameters as Record<string, unknown>)[FILES_AS_PARAMETER] ?? '').trim().toLowerCase()
    : '';
  return value === FILES_AS_SEPARATE ? FILES_AS_SEPARATE : FILES_AS_COLLECTION;
};

export const sourceOutputTypes = (filenames: string[], mode: FilesAs, portIds: string[]) => {
  const types: Record<string, string> = {};
  if (filenames.length === 0 || portIds.length === 0) return types;
  if (mode === FILES_AS_SEPARATE) {
    const ids = filePortIds(filenames);
    ids.forEach((id, index) => {
      if (portIds.includes(id)) types[id] = kindForFilename(filenames[index]);
    });
    return types;
  }
  types[portIds[0]] = filenames.length === 1
    ? kindForFilename(filenames[0])
    : collectionType(filenames.map(kindForFilename));
  return types;
};

export const declaredOutputTypes = (declarations: ArtifactLike[], portIds: string[]) => {
  const types: Record<string, string> = {};
  if (declarations.length === 0 || portIds.length === 0) return types;
  if (declarations.length === 1 && !declarations[0].port) {
    types[portIds[0]] = artifactPortType(declarations[0]);
    return types;
  }
  declarations.forEach((item) => {
    const port = String(item.port || item.name || '');
    if (portIds.includes(port)) types[port] = artifactPortType(item);
  });
  return types;
};

const record = (value: unknown): Record<string, unknown> => {
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return {}; }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
};

/** Names of the data files attached to a node (code and manifests excluded). */
export const dataFilenames = (files: unknown) => (Array.isArray(files) ? files : []).flatMap((item) => {
  let name = '';
  if (typeof File !== 'undefined' && item instanceof File) name = item.name;
  else if (item && typeof item === 'object') {
    const entry = item as Record<string, unknown>;
    if (entry.role === 'code') return [];
    name = String(entry.filename ?? entry.name ?? '');
  } else name = String(item ?? '');
  const base = baseName(name);
  return base && !NON_DATA_FILENAMES.has(base) && !base.endsWith('.py') ? [base] : [];
});

export const artifactDeclarations = (data: Record<string, unknown>): ArtifactLike[] => {
  const parameters = record(data.param ?? data.parameters ?? data.param_json);
  const implementation = record(data.implementation);
  for (const source of [implementation, parameters]) {
    const plural = source.output_artifacts;
    if (Array.isArray(plural) && plural.length) {
      return plural.filter((item): item is ArtifactLike => Boolean(item) && typeof item === 'object');
    }
    const single = source.output_artifact;
    if (single && typeof single === 'object' && !Array.isArray(single)) return [single as ArtifactLike];
  }
  const outputs = record(record(data.generated_artifact).data_contract).outputs;
  return Array.isArray(outputs)
    ? outputs.filter((item): item is ArtifactLike => Boolean(item) && typeof item === 'object')
    : [];
};

/** Port types a node's outputs carry, inferred from its files or artifact contract. */
export const inferOutputTypes = (kind: string, data: Record<string, unknown>, portIds: string[]) => {
  if (kind === 'source') {
    const parameters = record(data.param ?? data.parameters ?? data.param_json);
    const inferred = sourceOutputTypes(dataFilenames(data.files), filesAs(parameters), portIds);
    if (Object.keys(inferred).length) return inferred;
  }
  return declaredOutputTypes(artifactDeclarations(data), portIds);
};

/** A declared type wins; a wildcard port takes what its contract implies. */
export const effectivePortType = (declared: unknown, inferred: unknown | null | undefined) => {
  const declaredType = parsePortType(declared);
  if (inferred == null || declaredType.kind !== 'any') return formatPortType(declaredType);
  const inferredType = parsePortType(inferred);
  if (declaredType.many && !inferredType.many && !OPAQUE_KINDS.has(inferredType.kind)) {
    return formatPortType(declaredType);
  }
  return formatPortType(inferredType);
};

/** Grade a connection from an output of type `source` to an input of type `target`. */
export const checkConnection = (source: unknown, target: unknown): PortIssue | null => {
  const left = parsePortType(source);
  const right = parsePortType(target);
  const l = formatPortType(left);
  const r = formatPortType(right);
  if (right.kind === 'any' && !right.many) return null;
  if (left.kind === 'any' && !left.many) return null;
  if (OPAQUE_KINDS.has(left.kind)) {
    if (OPAQUE_KINDS.has(right.kind) || right.kind === 'any') return null;
    return {
      severity: 'warning',
      code: 'unchecked-bundle',
      message: `A ${l} arrives at a ${r} input; its contents cannot be checked before running.`,
    };
  }
  if (left.kind !== 'any' && right.kind !== 'any' && left.kind !== right.kind) {
    return { severity: 'error', code: 'incompatible-port-kinds', message: `Port types are incompatible: ${l} -> ${r}.` };
  }
  if (left.many && !right.many) {
    return {
      severity: 'warning',
      code: 'collection-into-single',
      message: `A collection (${l}) arrives at a single ${r} input and is delivered as one directory. `
        + `Add a Parallel Map to process each item, or type the input as ${right.kind}[].`,
    };
  }
  if (right.many && !left.many) {
    return {
      severity: 'warning',
      code: 'single-into-collection',
      message: `A single ${l} arrives where a collection (${r}) is expected.`,
    };
  }
  return null;
};

/** Human label for a port type, e.g. "collection of tables". */
export const describePortType = (value: unknown) => {
  const type = parsePortType(value);
  if (type.kind === 'any') return type.many ? 'collection' : '';
  if (type.kind === 'bundle') return 'bundle of mixed files';
  return type.many ? `collection of ${type.kind}s` : type.kind;
};
