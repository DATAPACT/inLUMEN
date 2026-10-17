import { useEffect, useMemo, useState } from 'react';
import { Eye, Loader2, RotateCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { RunOutputViewer } from '@/components/runs/RunOutputViewer';
import { listPipelineRuns, startPipelineRun, type PipelineRunRecord } from '@/features/runs/pipelineRuns';
import {
  formatOutputSize,
  outputsForNode,
  presentRunOutputs,
} from '@/features/runs/runPresentation';

const RECENT_RUNS_TO_SEARCH = 10;

const formatRunDate = (value?: string | null) => {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? ''
    : parsed.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
};

/**
 * Outputs this node produced in the most recent run that reached it.
 * Runs are fetched only when the section is opened, so selecting nodes stays
 * free of network calls.
 */
export const NodeRunOutputs = ({ nodeId }: { nodeId: string }) => {
  const [expanded, setExpanded] = useState(false);
  const [run, setRun] = useState<PipelineRunRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [viewerPath, setViewerPath] = useState<string | null>(null);
  const [rerunState, setRerunState] = useState<'idle' | 'starting' | 'started'>('idle');

  const rerunFromHere = async () => {
    setRerunState('starting');
    setError('');
    try {
      const key = globalThis.crypto?.randomUUID?.() || `rerun-${Date.now()}`;
      await startPipelineRun(key, { cache: 'reuse', rerun_from: nodeId });
      setRerunState('started');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setRerunState('idle');
    }
  };

  useEffect(() => {
    setRun(null);
    setError('');
    setViewerPath(null);
    setRerunState('idle');
  }, [nodeId]);

  useEffect(() => {
    if (!expanded || run) return undefined;
    let cancelled = false;
    setLoading(true);
    setError('');
    listPipelineRuns(RECENT_RUNS_TO_SEARCH)
      .then((runs) => {
        if (cancelled) return;
        setRun(runs.find(
          (candidate) => outputsForNode(candidate.result?.outputs || [], nodeId).length > 0,
        ) || null);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [expanded, nodeId, run]);

  const nodeOutputs = useMemo(
    () => outputsForNode(run?.result?.outputs || [], nodeId),
    [nodeId, run],
  );
  const allOutputs = useMemo(
    () => presentRunOutputs(run?.result?.outputs || []).all,
    [run],
  );

  return (
    <details
      className="rounded-lg border border-border bg-muted/10 p-3"
      onToggle={(event) => setExpanded((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer text-sm font-semibold">Latest run outputs</summary>
      <div className="mt-2 space-y-1 text-xs">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mb-1 w-full"
          disabled={rerunState === 'starting'}
          title="Run this step and every step after it again; earlier steps reuse their results."
          onClick={() => { void rerunFromHere(); }}
        >
          {rerunState === 'starting'
            ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
            : <RotateCcw className="mr-2 h-3.5 w-3.5" />}
          Re-run from this step
        </Button>
        {rerunState === 'started' && (
          <p role="status" className="text-muted-foreground">Run started. Follow it in the Run panel.</p>
        )}
        {loading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading runs" />}
        {error && <p className="text-red-500">{error}</p>}
        {!loading && !error && expanded && !run && (
          <p className="text-muted-foreground">No recent run has produced outputs for this step yet.</p>
        )}
        {run && (
          <>
            <p className="text-muted-foreground">
              From the {run.status} run of {formatRunDate(run.finished_at || run.updated_at)}.
            </p>
            {nodeOutputs.map((output) => (
              <Button
                key={output.path}
                type="button"
                variant="ghost"
                size="sm"
                className="h-auto w-full justify-start px-2 py-1 text-left"
                onClick={() => setViewerPath(output.path)}
              >
                <Eye className="mr-2 h-3.5 w-3.5 shrink-0" />
                <span className="min-w-0 flex-1 truncate">{output.filename}</span>
                <span className="ml-2 shrink-0 text-[10px] text-muted-foreground">
                  {formatOutputSize(output.sizeBytes)}
                </span>
              </Button>
            ))}
            <RunOutputViewer
              open={viewerPath !== null}
              onOpenChange={(open) => { if (!open) setViewerPath(null); }}
              runId={run.run_id}
              outputs={allOutputs}
              initialPath={viewerPath || undefined}
            />
          </>
        )}
      </div>
    </details>
  );
};
