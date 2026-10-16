import { useEffect, useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronRight, Download, FileText, Loader2 } from 'lucide-react';

import { AssistantMessageContent } from '@/components/chat/AssistantMessageContent';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import {
  downloadPipelineRunOutput,
  fetchPipelineRunOutputBlob,
  fetchPipelineRunOutputPreview,
  type RunOutputPreview,
  type RunOutputPreviewColumn,
} from '@/features/runs/pipelineRuns';
import {
  formatOutputSize,
  groupRunOutputsByStep,
  type PresentedRunOutput,
} from '@/features/runs/runPresentation';
import { cn } from '@/lib/utils';

type RunOutputViewerProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  runId: string;
  outputs: PresentedRunOutput[];
  initialPath?: string;
};

const JSON_CHILD_LIMIT = 200;

const matches = (value: string, query: string) =>
  Boolean(query) && value.toLocaleLowerCase().includes(query);

const subtreeMatches = (value: unknown, query: string): boolean => {
  if (!query) return false;
  if (value === null || typeof value !== 'object') return matches(String(value), query);
  return Object.entries(value as Record<string, unknown>).some(
    ([key, child]) => matches(key, query) || subtreeMatches(child, query),
  );
};

const Highlight = ({ text, query }: { text: string; query: string }) => {
  if (!matches(text, query)) return <>{text}</>;
  const start = text.toLocaleLowerCase().indexOf(query);
  return (
    <>
      {text.slice(0, start)}
      <mark className="rounded-sm bg-amber-300/60 text-foreground">{text.slice(start, start + query.length)}</mark>
      {text.slice(start + query.length)}
    </>
  );
};

const JsonScalar = ({ value, query }: { value: unknown; query: string }) => {
  const text = typeof value === 'string' ? JSON.stringify(value) : String(value);
  const tone = typeof value === 'string'
    ? 'text-emerald-600 dark:text-emerald-400'
    : typeof value === 'number'
      ? 'text-sky-600 dark:text-sky-400'
      : 'text-violet-600 dark:text-violet-400';
  return <span className={tone}><Highlight text={text} query={query} /></span>;
};

export const JsonTree = ({
  value,
  name,
  depth = 0,
  query = '',
}: {
  value: unknown;
  name?: string;
  depth?: number;
  query?: string;
}) => {
  const isContainer = value !== null && typeof value === 'object';
  const [open, setOpen] = useState(depth < 2);
  const [limit, setLimit] = useState(JSON_CHILD_LIMIT);
  const expanded = open || subtreeMatches(value, query);
  const label = name !== undefined && (
    <span className="text-foreground/80"><Highlight text={name} query={query} />: </span>
  );
  if (!isContainer) {
    return <div className="pl-4">{label}<JsonScalar value={value} query={query} /></div>;
  }
  const entries = Object.entries(value as Record<string, unknown>);
  const isArray = Array.isArray(value);
  const summary = isArray ? `[${entries.length}]` : `{${entries.length}}`;
  return (
    <div className={depth > 0 ? 'pl-4' : ''}>
      <button
        type="button"
        className="inline-flex items-center gap-1 rounded-sm text-left hover:bg-muted"
        aria-expanded={expanded}
        onClick={() => setOpen(!expanded)}
      >
        <ChevronRight className={cn('h-3 w-3 transition-transform', expanded && 'rotate-90')} />
        {label}
        <span className="text-muted-foreground">{summary}</span>
      </button>
      {expanded && (
        <div className="border-l border-border/60">
          {entries.slice(0, limit).map(([key, child]) => (
            <JsonTree key={key} value={child} name={isArray ? undefined : key} depth={depth + 1} query={query} />
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
    </div>
  );
};

const compareCells = (type: string, left = '', right = '') => {
  if (type === 'integer' || type === 'number') {
    const delta = Number(left) - Number(right);
    if (!Number.isNaN(delta)) return delta;
  }
  return left.localeCompare(right, undefined, { numeric: true });
};

export const TablePreview = ({
  columns,
  rows,
}: {
  columns: RunOutputPreviewColumn[];
  rows: string[][];
}) => {
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState<{ index: number; direction: 1 | -1 } | null>(null);
  const visibleRows = useMemo(() => {
    const query = filter.trim().toLocaleLowerCase();
    const filtered = query
      ? rows.filter((row) => row.some((cell) => cell.toLocaleLowerCase().includes(query)))
      : rows;
    if (!sort) return filtered;
    const type = columns[sort.index]?.type || 'string';
    return [...filtered].sort(
      (left, right) => sort.direction * compareCells(type, left[sort.index], right[sort.index]),
    );
  }, [columns, filter, rows, sort]);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <Input
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
        placeholder="Filter loaded rows"
        aria-label="Filter loaded rows"
        className="h-8 max-w-xs text-xs"
      />
      <div className="min-h-0 flex-1 overflow-auto rounded-md border">
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 bg-muted">
            <tr>
              <th className="w-10 px-2 py-1.5 text-right font-normal text-muted-foreground">#</th>
              {columns.map((column, index) => (
                <th key={`${column.name}-${index}`} className="whitespace-nowrap px-2 py-1.5 text-left">
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 font-medium hover:text-primary"
                    onClick={() => setSort(
                      sort?.index === index
                        ? (sort.direction === 1 ? { index, direction: -1 } : null)
                        : { index, direction: 1 },
                    )}
                  >
                    {column.name}
                    {sort?.index === index && (sort.direction === 1
                      ? <ArrowUp className="h-3 w-3" />
                      : <ArrowDown className="h-3 w-3" />)}
                  </button>
                  <div className="text-[10px] font-normal text-muted-foreground">{column.type}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-t border-border/60 odd:bg-muted/20">
                <td className="px-2 py-1 text-right text-muted-foreground">{rowIndex + 1}</td>
                {columns.map((column, index) => (
                  <td key={`${column.name}-${index}`} className="max-w-[24rem] truncate px-2 py-1" title={row[index]}>
                    {row[index] ?? ''}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="text-xs text-muted-foreground">
        {filter ? `${visibleRows.length} of ${rows.length} loaded rows match` : `${rows.length} rows loaded`}
      </div>
    </div>
  );
};

const TextPreview = ({ text }: { text: string }) => {
  const lines = text.endsWith('\n') ? text.slice(0, -1).split('\n') : text.split('\n');
  return (
    <div className="min-h-0 flex-1 overflow-auto rounded-md border bg-muted/30">
      <pre className="text-xs leading-5">
        {lines.map((line, index) => (
          <div key={index} className="flex">
            <span className="w-12 shrink-0 select-none pr-3 text-right text-muted-foreground">{index + 1}</span>
            <code className="whitespace-pre-wrap break-all pr-3">{line}</code>
          </div>
        ))}
      </pre>
    </div>
  );
};

const MediaPreview = ({
  runId,
  output,
  preview,
}: {
  runId: string;
  output: PresentedRunOutput;
  preview: Extract<RunOutputPreview, { inline: boolean }>;
}) => {
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    if (!preview.inline) return undefined;
    let objectUrl = '';
    let cancelled = false;
    fetchPipelineRunOutputBlob(runId, output.path)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [output.path, preview.inline, runId]);

  if (!preview.inline) {
    return <p className="text-sm text-muted-foreground">This file is too large to display here. Download it to open it.</p>;
  }
  if (error) return <p className="text-sm text-red-500">{error}</p>;
  if (!url) return <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />;
  if (preview.kind === 'image') {
    return <img src={url} alt={output.filename} className="mx-auto max-h-full max-w-full rounded-md border object-contain" />;
  }
  if (preview.kind === 'audio') return <audio src={url} controls className="w-full" />;
  if (preview.kind === 'video') return <video src={url} controls className="max-h-full max-w-full" />;
  return <object data={url} type="application/pdf" className="h-full min-h-[60vh] w-full rounded-md border" aria-label={output.filename} />;
};

const mergePreview = (current: RunOutputPreview, next: RunOutputPreview): RunOutputPreview => {
  if (current.kind === 'table' && next.kind === 'table') {
    return { ...next, columns: current.columns, rows: [...current.rows, ...next.rows] };
  }
  if ((current.kind === 'text' || current.kind === 'markdown') && next.kind === current.kind) {
    return { ...next, text: current.text + next.text };
  }
  return next;
};

const PreviewBody = ({
  runId,
  output,
  preview,
}: {
  runId: string;
  output: PresentedRunOutput;
  preview: RunOutputPreview;
}) => {
  const [jsonQuery, setJsonQuery] = useState('');
  switch (preview.kind) {
    case 'table':
      return <TablePreview columns={preview.columns} rows={preview.rows} />;
    case 'json':
      return (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <Input
            value={jsonQuery}
            onChange={(event) => setJsonQuery(event.target.value)}
            placeholder="Search keys and values"
            aria-label="Search keys and values"
            className="h-8 max-w-xs text-xs"
          />
          <div className="min-h-0 flex-1 overflow-auto rounded-md border p-2 font-mono text-xs leading-5">
            <JsonTree value={preview.value} query={jsonQuery.trim().toLocaleLowerCase()} />
          </div>
        </div>
      );
    case 'markdown':
      return (
        <div className="min-h-0 flex-1 overflow-auto rounded-md border p-4">
          <AssistantMessageContent content={preview.text} />
        </div>
      );
    case 'text':
      return <TextPreview text={preview.text} />;
    case 'image':
    case 'audio':
    case 'video':
    case 'pdf':
      return (
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto">
          <MediaPreview runId={runId} output={output} preview={preview} />
        </div>
      );
    default:
      return (
        <p className="text-sm text-muted-foreground">
          This file can&apos;t be displayed in the browser. Download it to open it with another tool.
        </p>
      );
  }
};

export const RunOutputViewer = ({
  open,
  onOpenChange,
  runId,
  outputs,
  initialPath,
}: RunOutputViewerProps) => {
  const groups = useMemo(() => groupRunOutputsByStep(outputs), [outputs]);
  const [selectedPath, setSelectedPath] = useState(initialPath || outputs[0]?.path || '');
  const [preview, setPreview] = useState<RunOutputPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (open) setSelectedPath(initialPath || outputs[0]?.path || '');
  }, [initialPath, open, outputs]);

  const selected = outputs.find((output) => output.path === selectedPath) || null;
  const selectedOutputPath = selected?.path || '';

  useEffect(() => {
    if (!open || !selectedOutputPath) return undefined;
    let cancelled = false;
    setLoading(true);
    setError('');
    setPreview(null);
    fetchPipelineRunOutputPreview(runId, selectedOutputPath)
      .then((next) => { if (!cancelled) setPreview(next); })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, runId, selectedOutputPath]);

  const loadMore = async () => {
    if (!selected || !preview?.next_offset) return;
    setLoadingMore(true);
    try {
      const next = await fetchPipelineRunOutputPreview(runId, selected.path, { offset: preview.next_offset });
      setPreview((current) => (current ? mergePreview(current, next) : next));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[85vh] w-[95vw] max-w-6xl gap-0 overflow-hidden p-0">
        <aside className="hidden w-64 shrink-0 overflow-y-auto border-r bg-muted/20 p-2 sm:block" aria-label="Run outputs">
          {groups.map((group) => (
            <section key={group.key || 'other'} className="mb-3">
              <h3 className="flex items-center gap-1.5 px-2 py-1 text-xs font-semibold text-muted-foreground">
                <span className="truncate">{group.label}</span>
                {group.role === 'final' && <Badge variant="secondary" className="px-1 py-0 text-[10px]">Result</Badge>}
              </h3>
              {group.outputs.map((output) => (
                <button
                  key={output.path}
                  type="button"
                  onClick={() => setSelectedPath(output.path)}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-muted',
                    output.path === selectedPath && 'bg-muted font-medium',
                  )}
                  aria-current={output.path === selectedPath ? 'true' : undefined}
                >
                  <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate">{output.filename}</span>
                  {output.port && output.port !== 'output' && (
                    <span className="shrink-0 text-[10px] text-muted-foreground">{output.port}</span>
                  )}
                </button>
              ))}
            </section>
          ))}
        </aside>
        <div className="flex min-w-0 flex-1 flex-col gap-3 p-4 pr-12">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <DialogTitle className="truncate text-base">{selected?.filename || 'Run outputs'}</DialogTitle>
              <DialogDescription className="text-xs">
                {[
                  selected?.nodeLabel || (selected?.sourceNode ? `Step ${selected.sourceNode}` : ''),
                  selected?.port && selected.port !== 'output' ? `port ${selected.port}` : '',
                  formatOutputSize(selected?.sizeBytes ?? null),
                ].filter(Boolean).join(' · ') || 'Pick an output to preview it.'}
              </DialogDescription>
            </div>
            {selected && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => { void downloadPipelineRunOutput(runId, selected.path, selected.filename); }}
              >
                <Download className="mr-1 h-4 w-4" />
                Download
              </Button>
            )}
          </div>
          {loading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading preview" />}
          {error && <p className="text-sm text-red-500">{error}</p>}
          {selected && preview && !loading && (
            <>
              <PreviewBody key={selected.path} runId={runId} output={selected} preview={preview} />
              {preview.has_more && preview.next_offset != null && (
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span>
                    Showing the first {formatOutputSize(preview.next_offset)} of {formatOutputSize(preview.size_bytes)}
                  </span>
                  <Button variant="outline" size="sm" disabled={loadingMore} onClick={() => { void loadMore(); }}>
                    {loadingMore && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
                    Load more
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
};
