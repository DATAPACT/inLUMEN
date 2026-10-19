import { useEffect, useMemo, useRef, useState } from 'react';
import { ClipboardCopy, Download, FileText, Loader2 } from 'lucide-react';

import { AssistantMessageContent } from '@/components/chat/AssistantMessageContent';
import { JsonOutputView } from '@/components/runs/JsonOutputView';
import { TablePreview, TextPreview } from '@/components/runs/PreviewPanes';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import {
  downloadPipelineRunOutput,
  fetchPipelineRunOutputBlob,
  fetchPipelineRunOutputPreview,
  type RunOutputPreview,
} from '@/features/runs/pipelineRuns';
import { copyText } from '@/features/runs/copyText';
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

/** Text-like outputs up to this size can be copied whole (the JSON preview limit). */
const COPY_MAX_BYTES = 2 * 1024 * 1024;
const COPYABLE_KINDS = new Set(['table', 'json', 'text', 'markdown']);

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
  switch (preview.kind) {
    case 'table':
      return <TablePreview columns={preview.columns} rows={preview.rows} />;
    case 'json':
      return <JsonOutputView value={preview.value} />;
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

  // The run list refreshes in the background and hands us new output arrays;
  // only opening the viewer (or asking for another file) picks the selection.
  const outputsRef = useRef(outputs);
  outputsRef.current = outputs;
  useEffect(() => {
    if (open) setSelectedPath(initialPath || outputsRef.current[0]?.path || '');
  }, [initialPath, open]);

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

  const [copying, setCopying] = useState(false);
  const canCopyFile = Boolean(
    selected && preview && COPYABLE_KINDS.has(preview.kind)
    && (preview.size_bytes ?? Infinity) <= COPY_MAX_BYTES,
  );
  const copyFile = async () => {
    if (!selected) return;
    setCopying(true);
    try {
      const blob = await fetchPipelineRunOutputBlob(runId, selected.path);
      await copyText(await blob.text(), 'file');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setCopying(false);
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
              <div className="flex gap-2">
                {canCopyFile && (
                  <Button variant="outline" size="sm" disabled={copying} onClick={() => { void copyFile(); }}>
                    {copying ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <ClipboardCopy className="mr-1 h-4 w-4" />}
                    Copy
                  </Button>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => { void downloadPipelineRunOutput(runId, selected.path, selected.filename); }}
                >
                  <Download className="mr-1 h-4 w-4" />
                  Download
                </Button>
              </div>
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
