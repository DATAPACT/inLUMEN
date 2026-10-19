import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import {
  ChevronDown,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  ChevronUp,
  Copy,
  Link2,
  Table2,
} from 'lucide-react';

import { TablePreview, TextPreview } from '@/components/runs/PreviewPanes';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { copyText } from '@/features/runs/copyText';
import {
  canShowAsTable,
  collapsedSummary,
  findRecordArrays,
  fitsInline,
  isContainer,
  jsonPath,
  LONG_STRING_CHARS,
  recordsToTable,
  valueAt,
  type JsonSegment,
} from '@/features/runs/jsonView';
import { cn } from '@/lib/utils';

const JSON_CHILD_LIMIT = 200;
const TABLE_ROW_LIMIT = 1000;
const DEFAULT_DEPTH = 2;
const VIEW_MODE_KEY = 'inlumen-run-output-json-view';

export type JsonViewMode = 'compact' | 'tree' | 'table' | 'raw';
const VIEW_MODES: { mode: JsonViewMode; label: string }[] = [
  { mode: 'compact', label: 'Compact' },
  { mode: 'tree', label: 'Tree' },
  { mode: 'table', label: 'Table' },
  { mode: 'raw', label: 'Raw' },
];

const readViewMode = (): JsonViewMode => {
  try {
    const saved = window.localStorage.getItem(VIEW_MODE_KEY);
    if (VIEW_MODES.some(({ mode }) => mode === saved)) return saved as JsonViewMode;
  } catch {
    // Storage can be unavailable (private windows, blocked site data).
  }
  return 'compact';
};

const saveViewMode = (mode: JsonViewMode) => {
  try {
    window.localStorage.setItem(VIEW_MODE_KEY, mode);
  } catch {
    // The choice just isn't remembered.
  }
};

const matches = (value: string, query: string) =>
  Boolean(query) && value.toLocaleLowerCase().includes(query);

const subtreeMatches = (value: unknown, query: string): boolean => {
  if (!query) return false;
  if (!isContainer(value)) return matches(String(value), query);
  return Object.entries(value).some(
    ([key, child]) => matches(key, query) || subtreeMatches(child, query),
  );
};

const Highlight = ({ text, query }: { text: string; query: string }) => {
  if (!matches(text, query)) return <>{text}</>;
  const start = text.toLocaleLowerCase().indexOf(query);
  return (
    <>
      {text.slice(0, start)}
      <mark className="rounded-sm bg-amber-300/60 text-foreground data-[current=true]:bg-amber-500 data-[current=true]:ring-2 data-[current=true]:ring-amber-500">
        {text.slice(start, start + query.length)}
      </mark>
      {text.slice(start + query.length)}
    </>
  );
};

type JsonViewContextValue = {
  query: string;
  compact: boolean;
  defaultDepth: number;
  onShowTable?: (path: JsonSegment[]) => void;
};

const JsonViewContext = createContext<JsonViewContextValue>({
  query: '',
  compact: false,
  defaultDepth: DEFAULT_DEPTH,
});

const scalarTone = (value: unknown) => (typeof value === 'string'
  ? 'text-emerald-700 dark:text-emerald-400'
  : typeof value === 'number'
    ? 'text-sky-600 dark:text-sky-400'
    : 'text-violet-600 dark:text-violet-400');

const JsonScalar = ({ value }: { value: unknown }) => {
  const { query, compact } = useContext(JsonViewContext);
  const [full, setFull] = useState(false);
  const text = typeof value === 'string' ? JSON.stringify(value) : String(value);
  const cut = compact && !full && typeof value === 'string' && value.length > LONG_STRING_CHARS
    && !matches(text, query);
  return (
    <span className={scalarTone(value)}>
      <Highlight text={cut ? `${text.slice(0, LONG_STRING_CHARS)}…` : text} query={query} />
      {cut && (
        <button type="button" className="ml-1 font-sans text-primary hover:underline" onClick={() => setFull(true)}>
          show more
        </button>
      )}
    </span>
  );
};

/** A flat container on one line, e.g. `{ start: 12, label: "Oslo" }`. */
const InlineValue = ({ value }: { value: Record<string, unknown> | unknown[] }) => {
  const { query } = useContext(JsonViewContext);
  const isArray = Array.isArray(value);
  const entries = Object.entries(value);
  const [open, close] = isArray ? ['[', ']'] : ['{ ', ' }'];
  if (!entries.length) return <span className="text-muted-foreground">{isArray ? '[]' : '{}'}</span>;
  return (
    <span className="whitespace-normal">
      <span className="text-muted-foreground">{open}</span>
      {entries.map(([key, child], index) => (
        <span key={key}>
          {!isArray && (
            <span className="text-foreground/80"><Highlight text={key} query={query} />: </span>
          )}
          <JsonScalar value={child} />
          {index < entries.length - 1 && <span className="text-muted-foreground">, </span>}
        </span>
      ))}
      <span className="text-muted-foreground">{close}</span>
    </span>
  );
};

const NodeActions = ({ path, value }: { path: JsonSegment[]; value: unknown }) => {
  const { onShowTable } = useContext(JsonViewContext);
  const action = 'rounded-sm p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground';
  return (
    <span className="ml-2 inline-flex shrink-0 gap-0.5 opacity-0 transition-opacity group-hover/row:opacity-100 group-focus-within/row:opacity-100">
      <button
        type="button"
        className={action}
        title="Copy value"
        aria-label={`Copy value of ${jsonPath(path)}`}
        onClick={() => { void copyText(JSON.stringify(value, null, 2), 'value'); }}
      >
        <Copy className="h-3 w-3" />
      </button>
      <button
        type="button"
        className={action}
        title="Copy path"
        aria-label={`Copy path ${jsonPath(path)}`}
        onClick={() => { void copyText(jsonPath(path), 'path'); }}
      >
        <Link2 className="h-3 w-3" />
      </button>
      {onShowTable && canShowAsTable(value) && (
        <button
          type="button"
          className={action}
          title="Show as table"
          aria-label={`Show ${jsonPath(path)} as a table`}
          onClick={() => onShowTable(path)}
        >
          <Table2 className="h-3 w-3" />
        </button>
      )}
    </span>
  );
};

const Row = ({ path, value, children }: { path: JsonSegment[]; value: unknown; children: React.ReactNode }) => (
  <div className="group/row flex items-start rounded-sm hover:bg-muted/40">
    <div className="min-w-0">{children}</div>
    <NodeActions path={path} value={value} />
  </div>
);

export const JsonTree = ({
  value,
  name,
  path = [],
  depth = 0,
  query,
}: {
  value: unknown;
  name?: string;
  path?: JsonSegment[];
  depth?: number;
  /** Overrides the search query; used when the tree is rendered on its own. */
  query?: string;
}) => {
  const context = useContext(JsonViewContext);
  const view = query === undefined ? context : { ...context, query: query.trim().toLocaleLowerCase() };
  const [open, setOpen] = useState(depth < view.defaultDepth);
  const [limit, setLimit] = useState(JSON_CHILD_LIMIT);
  const expanded = open || subtreeMatches(value, view.query);
  const label = name !== undefined && (
    <span className="text-foreground/80"><Highlight text={name} query={view.query} />: </span>
  );
  const body = (() => {
    if (!isContainer(value)) {
      return <Row path={path} value={value}><div className="pl-8 -indent-4 break-words">{label}<JsonScalar value={value} /></div></Row>;
    }
    if (view.compact && fitsInline(value)) {
      return <Row path={path} value={value}><div className="pl-8 -indent-4">{label}<InlineValue value={value} /></div></Row>;
    }
    const entries = Object.entries(value);
    const isArray = Array.isArray(value);
    const count = isArray ? `[${entries.length}]` : `{${entries.length}}`;
    return (
      <>
        <Row path={path} value={value}>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-sm text-left hover:bg-muted"
            aria-expanded={expanded}
            onClick={() => setOpen(!expanded)}
          >
            <ChevronRight className={cn('h-3 w-3 shrink-0 transition-transform', expanded && 'rotate-90')} />
            {label}
            <span className="text-muted-foreground">{view.compact && !expanded ? collapsedSummary(value) : count}</span>
          </button>
        </Row>
        {expanded && (
          <div className="border-l border-border/60">
            {entries.slice(0, limit).map(([key, child]) => (
              <JsonTree
                key={key}
                value={child}
                name={isArray ? undefined : key}
                path={[...path, isArray ? Number(key) : key]}
                depth={depth + 1}
              />
            ))}
            {entries.length > limit && (
              <button
                type="button"
                className="pl-4 text-xs text-primary hover:underline"
                onClick={() => setLimit(limit + JSON_CHILD_LIMIT)}
              >
                Show {Math.min(JSON_CHILD_LIMIT, entries.length - limit)} more of {entries.length - limit} remaining
              </button>
            )}
          </div>
        )}
      </>
    );
  })();
  const tree = <div className={depth > 0 ? 'pl-4' : ''}>{body}</div>;
  return query === undefined ? tree : <JsonViewContext.Provider value={view}>{tree}</JsonViewContext.Provider>;
};

const sameSegments = (left: JsonSegment[], right: JsonSegment[]) =>
  left.length === right.length && left.every((segment, index) => segment === right[index]);

const JsonSearch = ({
  rootRef,
  query,
  onQueryChange,
  refreshKey,
}: {
  rootRef: React.RefObject<HTMLDivElement>;
  query: string;
  onQueryChange: (query: string) => void;
  refreshKey: string;
}) => {
  const [current, setCurrent] = useState(0);
  const [count, setCount] = useState(0);
  const marks = useCallback(
    () => [...(rootRef.current?.querySelectorAll('mark') ?? [])] as HTMLElement[],
    [rootRef],
  );
  const focus = useCallback((index: number) => {
    const found = marks();
    setCount(found.length);
    if (!found.length) return;
    const next = (index + found.length) % found.length;
    found.forEach((mark, position) => { mark.dataset.current = String(position === next); });
    found[next].scrollIntoView?.({ block: 'center' });
    setCurrent(next);
  }, [marks]);

  // Matches deep in the tree are expanded but may be off-screen: show the first.
  useEffect(() => { focus(0); }, [focus, query, refreshKey]);

  const hasQuery = Boolean(query.trim());
  return (
    <div className="flex items-center gap-1">
      <Input
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          focus(current + (event.shiftKey ? -1 : 1));
        }}
        placeholder="Search keys and values"
        aria-label="Search keys and values"
        className="h-8 w-56 text-xs"
      />
      {hasQuery && (
        <>
          <span className="min-w-16 text-center text-xs text-muted-foreground" aria-live="polite">
            {count ? `${current + 1} of ${count}` : 'No matches'}
          </span>
          <Button variant="ghost" size="icon" className="h-7 w-7" disabled={!count} aria-label="Previous match" onClick={() => focus(current - 1)}>
            <ChevronUp className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" disabled={!count} aria-label="Next match" onClick={() => focus(current + 1)}>
            <ChevronDown className="h-4 w-4" />
          </Button>
        </>
      )}
    </div>
  );
};

export const JsonOutputView = ({ value }: { value: unknown }) => {
  const tableSources = useMemo(() => findRecordArrays(value), [value]);
  const [savedMode, setSavedMode] = useState(readViewMode);
  const [tablePath, setTablePath] = useState<JsonSegment[] | null>(null);
  const [query, setQuery] = useState('');
  const [minified, setMinified] = useState(false);
  // Expand/collapse remounts the tree so every node takes the new default depth.
  const [expansion, setExpansion] = useState({ depth: DEFAULT_DEPTH, generation: 0 });
  const rootRef = useRef<HTMLDivElement>(null);

  const activeTablePath = tablePath ?? tableSources[0] ?? null;
  const mode: JsonViewMode = savedMode === 'table' && !activeTablePath ? 'compact' : savedMode;
  const chooseMode = (next: JsonViewMode) => {
    setSavedMode(next);
    saveViewMode(next);
  };
  const showTable = useCallback((path: JsonSegment[]) => {
    setTablePath(path);
    setSavedMode('table');
    saveViewMode('table');
  }, []);
  const expandTo = (depth: number) =>
    setExpansion(({ generation }) => ({ depth, generation: generation + 1 }));

  const tableOptions = useMemo(() => {
    if (!activeTablePath || tableSources.some((path) => sameSegments(path, activeTablePath))) return tableSources;
    return [activeTablePath, ...tableSources];
  }, [activeTablePath, tableSources]);
  const table = useMemo(() => {
    if (mode !== 'table' || !activeTablePath) return null;
    const records = valueAt(value, activeTablePath) as Record<string, unknown>[];
    return { total: records.length, ...recordsToTable(records.slice(0, TABLE_ROW_LIMIT)) };
  }, [activeTablePath, mode, value]);
  const raw = useMemo(
    () => (mode === 'raw' ? JSON.stringify(value, null, minified ? undefined : 2) : ''),
    [minified, mode, value],
  );
  const context = useMemo<JsonViewContextValue>(() => ({
    query: query.trim().toLocaleLowerCase(),
    compact: mode === 'compact',
    defaultDepth: expansion.depth,
    onShowTable: showTable,
  }), [expansion.depth, mode, query, showTable]);

  const showsTree = mode === 'compact' || mode === 'tree';
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-md border p-0.5" role="group" aria-label="JSON view">
          {VIEW_MODES.filter(({ mode: option }) => option !== 'table' || activeTablePath).map(({ mode: option, label }) => (
            <button
              key={option}
              type="button"
              aria-pressed={mode === option}
              onClick={() => chooseMode(option)}
              className={cn(
                'h-7 rounded-sm px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground',
                mode === option && 'bg-primary text-primary-foreground hover:text-primary-foreground',
              )}
            >
              {label}
            </button>
          ))}
        </div>
        {showsTree && (
          <>
            <JsonSearch
              rootRef={rootRef}
              query={query}
              onQueryChange={setQuery}
              refreshKey={`${mode}:${expansion.generation}`}
            />
            <div className="ml-auto flex items-center gap-1">
              <Button variant="ghost" size="sm" className="h-8 px-2 text-xs" onClick={() => expandTo(Infinity)}>
                <ChevronsUpDown className="mr-1 h-3.5 w-3.5" />
                Expand all
              </Button>
              <Button variant="ghost" size="sm" className="h-8 px-2 text-xs" onClick={() => expandTo(1)}>
                <ChevronsDownUp className="mr-1 h-3.5 w-3.5" />
                Collapse all
              </Button>
              <label className="flex items-center gap-1 text-xs text-muted-foreground">
                Level
                <select
                  className="h-8 rounded-md border bg-background px-1 text-xs text-foreground"
                  aria-label="Expand to level"
                  value={Number.isFinite(expansion.depth) ? String(expansion.depth) : ''}
                  onChange={(event) => expandTo(Number(event.target.value))}
                >
                  {!Number.isFinite(expansion.depth) && <option value="">All</option>}
                  {[1, 2, 3, 4].map((depth) => <option key={depth} value={depth}>{depth}</option>)}
                </select>
              </label>
            </div>
          </>
        )}
        {mode === 'table' && tableOptions.length > 1 && (
          <select
            className="h-8 max-w-xs rounded-md border bg-background px-2 font-mono text-xs"
            aria-label="Array shown as a table"
            value={jsonPath(activeTablePath ?? [])}
            onChange={(event) => {
              const next = tableOptions.find((path) => jsonPath(path) === event.target.value);
              if (next) setTablePath(next);
            }}
          >
            {tableOptions.map((path) => <option key={jsonPath(path)} value={jsonPath(path)}>{jsonPath(path)}</option>)}
          </select>
        )}
        {mode === 'raw' && (
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={minified} onChange={(event) => setMinified(event.target.checked)} />
            Minified
          </label>
        )}
      </div>
      {showsTree && (
        <div ref={rootRef} className="min-h-0 flex-1 overflow-auto rounded-md border p-2 font-mono text-xs leading-5">
          <JsonViewContext.Provider value={context}>
            <JsonTree key={expansion.generation} value={value} />
          </JsonViewContext.Provider>
        </div>
      )}
      {table && (
        <>
          {tableOptions.length === 1 && activeTablePath && activeTablePath.length > 0 && (
            <p className="font-mono text-xs text-muted-foreground">{jsonPath(activeTablePath)}</p>
          )}
          <TablePreview key={jsonPath(activeTablePath ?? [])} columns={table.columns} rows={table.rows} />
          {table.total > TABLE_ROW_LIMIT && (
            <p className="text-xs text-muted-foreground">
              Showing the first {TABLE_ROW_LIMIT.toLocaleString()} of {table.total.toLocaleString()} items. Switch to Compact or Raw to see the rest.
            </p>
          )}
        </>
      )}
      {mode === 'raw' && <TextPreview text={raw} />}
    </div>
  );
};
