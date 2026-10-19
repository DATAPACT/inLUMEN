/** Layout helpers for the JSON views of the run output viewer. */

export type JsonSegment = string | number;

export type JsonTable = {
  columns: { name: string; type: string }[];
  rows: string[][];
};

/** Lines longer than this stay in tree layout in the compact view. */
export const INLINE_MAX_CHARS = 100;
/** Arrays of scalars up to this many items are shown wrapped on one line. */
export const INLINE_ARRAY_MAX_ITEMS = 200;
/** Strings longer than this are cut in the compact view. */
export const LONG_STRING_CHARS = 200;
const PREVIEW_KEYS = 3;
const MAX_TABLE_SOURCES = 20;
const TABLE_SEARCH_DEPTH = 4;

export const isContainer = (value: unknown): value is Record<string, unknown> | unknown[] =>
  value !== null && typeof value === 'object';

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  isContainer(value) && !Array.isArray(value);

const IDENTIFIER = /^[A-Za-z_$][\w$]*$/;

/** JSONPath for a node, e.g. `$.annotations[3].label` or `$["first name"]`. */
export const jsonPath = (segments: JsonSegment[]) =>
  segments.reduce<string>((path, segment) => {
    if (typeof segment === 'number') return `${path}[${segment}]`;
    return IDENTIFIER.test(segment) ? `${path}.${segment}` : `${path}[${JSON.stringify(segment)}]`;
  }, '$');

export const valueAt = (root: unknown, segments: JsonSegment[]): unknown =>
  segments.reduce<unknown>(
    (value, segment) => (isContainer(value) ? (value as Record<string, unknown>)[segment as string] : undefined),
    root,
  );

/**
 * Whether the compact view shows a container on one line: arrays of short
 * scalars (wrapped to the panel width) and flat objects that fit in a line.
 */
export const fitsInline = (value: unknown): boolean => {
  if (!isContainer(value)) return true;
  const children = Object.values(value);
  if (children.length === 0) return true;
  if (children.some((child) => isContainer(child)
    || (typeof child === 'string' && child.length > LONG_STRING_CHARS))) {
    return false;
  }
  if (Array.isArray(value)) return value.length <= INLINE_ARRAY_MAX_ITEMS;
  return JSON.stringify(value).length <= INLINE_MAX_CHARS;
};

/** What a closed node holds: `{ id, label, … }` or `[340 × { id, label, … }]`. */
export const collapsedSummary = (value: unknown): string => {
  if (!isContainer(value)) return '';
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    const first = value[0];
    if (isPlainObject(first) && value.every(isPlainObject)) {
      return `[${value.length} × ${collapsedSummary(first)}]`;
    }
    const kinds = new Set(value.map((item) => (item === null ? 'null' : Array.isArray(item) ? 'array' : typeof item)));
    return kinds.size === 1 ? `[${value.length} × ${[...kinds][0]}]` : `[${value.length} items]`;
  }
  const keys = Object.keys(value);
  if (keys.length === 0) return '{}';
  const shown = keys.slice(0, PREVIEW_KEYS).join(', ');
  return `{ ${shown}${keys.length > PREVIEW_KEYS ? ', …' : ''} }`;
};

const isRecordArray = (value: unknown): value is Record<string, unknown>[] =>
  Array.isArray(value) && value.length > 0 && value.every(isPlainObject);

/** Paths of the arrays of objects that the table view can show, root first. */
export const findRecordArrays = (root: unknown): JsonSegment[][] => {
  const found: JsonSegment[][] = [];
  const queue: { value: unknown; path: JsonSegment[] }[] = [{ value: root, path: [] }];
  while (queue.length && found.length < MAX_TABLE_SOURCES) {
    const { value, path } = queue.shift()!;
    if (isRecordArray(value)) {
      found.push(path);
      continue;
    }
    if (isPlainObject(value) && path.length < TABLE_SEARCH_DEPTH) {
      for (const [key, child] of Object.entries(value)) queue.push({ value: child, path: [...path, key] });
    }
  }
  return found;
};

export const canShowAsTable = (value: unknown) => isRecordArray(value);

/** Same column types as the backend's CSV preview, so sorting behaves alike. */
const inferType = (values: string[]) => {
  const present = values.map((value) => value.trim()).filter(Boolean);
  if (!present.length) return 'empty';
  if (present.every((value) => /^[-+]?\d+$/.test(value))) return 'integer';
  if (present.every((value) => !Number.isNaN(Number(value)))) return 'number';
  if (present.every((value) => ['true', 'false'].includes(value.toLowerCase()))) return 'boolean';
  if (present.every((value) => /^\d{4}-\d{2}-\d{2}([T ][\d:.+\-Z]*)?$/.test(value))) return 'date';
  return 'string';
};

const cellText = (value: unknown) => {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
};

/** One row per object and one column per key, in first-seen order. */
export const recordsToTable = (records: Record<string, unknown>[]): JsonTable => {
  const names: string[] = [];
  const seen = new Set<string>();
  for (const record of records) {
    for (const key of Object.keys(record)) {
      if (!seen.has(key)) {
        seen.add(key);
        names.push(key);
      }
    }
  }
  const rows = records.map((record) => names.map((name) => cellText(record[name])));
  return {
    columns: names.map((name, index) => ({ name, type: inferType(rows.map((row) => row[index])) })),
    rows,
  };
};
