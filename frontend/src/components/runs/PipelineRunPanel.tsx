import { useCallback, useEffect, useMemo, useState } from 'react';
import { Ban, Download, Loader2, PlayCircle, RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  cancelPipelineRun,
  downloadPipelineRunBundle,
  downloadPipelineRunOutput,
  fetchPipelineRunEvents,
  fetchRunnerCapabilities,
  getPipelineRun,
  isActivePipelineRun,
  listPipelineRuns,
  startPipelineRun,
  type PipelineRunEvent,
  type PipelineRunRecord,
  type RunnerCapabilities,
} from '@/features/runs/pipelineRuns';
import { cn } from '@/lib/utils';


const statusClass = (status: PipelineRunRecord['status']) => {
  if (status === 'succeeded') return 'text-emerald-400';
  if (status === 'failed' || status === 'partial') return 'text-red-400';
  if (status === 'cancelled') return 'text-muted-foreground';
  return 'text-amber-400';
};

const mergeRun = (runs: PipelineRunRecord[], next: PipelineRunRecord) => [
  next,
  ...runs.filter((run) => run.run_id !== next.run_id),
];

const newIdempotencyKey = () =>
  globalThis.crypto?.randomUUID?.() || `run-${Date.now()}-${Math.random().toString(16).slice(2)}`;

export const PipelineRunPanel = () => {
  const [capabilities, setCapabilities] = useState<RunnerCapabilities | null>(null);
  const [runs, setRuns] = useState<PipelineRunRecord[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>('');
  const [events, setEvents] = useState<PipelineRunEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) || runs[0] || null,
    [runs, selectedRunId],
  );
  const selectedRunIdForRefresh = selectedRun?.run_id || '';
  const selectedRunStatus = selectedRun?.status;
  const lifecycleEvents = useMemo(
    () => events.filter((event) => event.type !== 'dagster.log'),
    [events],
  );
  const dagsterLogs = useMemo(
    () => events.filter((event) => event.type === 'dagster.log'),
    [events],
  );

  const loadInitial = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [nextCapabilities, recentRuns] = await Promise.all([
        fetchRunnerCapabilities(),
        listPipelineRuns(),
      ]);
      setCapabilities(nextCapabilities);
      setRuns(recentRuns);
      setSelectedRunId((current) => current || recentRuns[0]?.run_id || '');
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Failed to load background runs.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadInitial();
  }, [loadInitial]);

  useEffect(() => {
    if (!selectedRunIdForRefresh || !selectedRunStatus) {
      setEvents([]);
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      try {
        const [record, eventPayload] = await Promise.all([
          getPipelineRun(selectedRunIdForRefresh),
          fetchPipelineRunEvents(selectedRunIdForRefresh, 0),
        ]);
        if (cancelled) return;
        setRuns((current) => mergeRun(current, record));
        setEvents(eventPayload.events || []);
      } catch (nextError) {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : 'Failed to refresh the run.');
        }
      }
    };
    void refresh();
    const interval = isActivePipelineRun(selectedRunStatus)
      ? window.setInterval(() => { void refresh(); }, 1000)
      : undefined;
    return () => {
      cancelled = true;
      if (interval !== undefined) window.clearInterval(interval);
    };
  }, [selectedRunIdForRefresh, selectedRunStatus]);

  const handleStart = async () => {
    setSubmitting(true);
    setError('');
    try {
      const run = await startPipelineRun(newIdempotencyKey());
      setRuns((current) => mergeRun(current, run));
      setSelectedRunId(run.run_id);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Failed to start the run.');
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async () => {
    if (!selectedRun) return;
    setSubmitting(true);
    setError('');
    try {
      const run = await cancelPipelineRun(selectedRun.run_id);
      setRuns((current) => mergeRun(current, run));
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Failed to cancel the run.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-border p-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Background pipeline runs</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Runs are owned by the runner service and continue after this browser closes.
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0"
          aria-label="Refresh pipeline runs"
          onClick={() => { void loadInitial(); }}
          disabled={loading}
        >
          <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
        </Button>
      </div>

      {capabilities?.message && (
        <div className="mt-3 rounded-md border border-border bg-muted/20 p-2 text-xs text-muted-foreground">
          {capabilities.message}
        </div>
      )}

      <Button
        className="mt-3 h-auto min-h-10 w-full whitespace-normal px-3 py-2"
        onClick={() => { void handleStart(); }}
        disabled={submitting || loading || !capabilities?.execution_available}
      >
        {submitting ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <PlayCircle className="mr-2 h-4 w-4" />
        )}
        Run saved pipeline with Dagster
      </Button>

      {error && <div className="mt-3 text-xs text-red-400">{error}</div>}

      {runs.length > 0 && (
        <div className="mt-3 space-y-1">
          {runs.slice(0, 5).map((run) => (
            <button
              type="button"
              key={run.run_id}
              onClick={() => setSelectedRunId(run.run_id)}
              className={cn(
                'flex w-full items-center justify-between rounded-md border px-2 py-2 text-left text-xs',
                selectedRun?.run_id === run.run_id
                  ? 'border-primary/50 bg-primary/10'
                  : 'border-border bg-muted/20',
              )}
            >
              <span className="font-mono">{run.run_id.slice(0, 8)}</span>
              <span className={cn('font-medium capitalize', statusClass(run.status))}>
                {run.status}
              </span>
            </button>
          ))}
        </div>
      )}

      {selectedRun && (
        <div className="mt-3 rounded-md border border-border bg-muted/20 p-2 text-xs">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate font-medium">Run {selectedRun.run_id}</div>
              <div className="mt-1 text-muted-foreground">
                {selectedRun.snapshot.node_count} nodes · {selectedRun.engine}
              </div>
            </div>
            {isActivePipelineRun(selectedRun.status) && (
              <Button
                variant="outline"
                size="sm"
                className="h-8 shrink-0"
                onClick={() => { void handleCancel(); }}
                disabled={submitting || selectedRun.status === 'cancelling'}
              >
                <Ban className="mr-1 h-3.5 w-3.5" />
                Cancel
              </Button>
            )}
          </div>
          {selectedRun.error?.message && (
            <div className="mt-2 text-red-400">{selectedRun.error.message}</div>
          )}
          <Button
            variant="outline"
            size="sm"
            className="mt-2 h-auto min-h-8 w-full whitespace-normal py-2 text-left"
            onClick={() => { void downloadPipelineRunBundle(selectedRun.run_id); }}
          >
            <Download className="mr-1 h-3.5 w-3.5" />
            Download tested Dagster snapshot
          </Button>
          {(selectedRun.result?.outputs || []).map((output, index) => {
            const path = String(output.path || '');
            const filename = String(output.filename || path.split('/').pop() || `output-${index + 1}`);
            if (!path) return null;
            return (
              <Button
                key={path}
                variant="ghost"
                size="sm"
                className="mt-1 h-8 w-full justify-start"
                onClick={() => {
                  void downloadPipelineRunOutput(selectedRun.run_id, path, filename);
                }}
              >
                <Download className="mr-1 h-3.5 w-3.5" />
                {filename}
              </Button>
            );
          })}
          {events.length > 0 && (
            <div className="mt-2 space-y-1 border-t border-border pt-2 text-muted-foreground">
              {lifecycleEvents.map((event) => (
                <div key={event.id}>{event.message || event.type}</div>
              ))}
              {dagsterLogs.length > 0 && (
                <details className="rounded border border-border bg-background/40 p-2">
                  <summary className="cursor-pointer font-medium text-foreground">
                    Dagster logs ({dagsterLogs.length})
                  </summary>
                  <div className="mt-2 max-h-64 space-y-2 overflow-auto">
                    {dagsterLogs.map((event) => (
                      <pre key={event.id} className="whitespace-pre-wrap break-words font-mono text-[11px]">
                        {event.message || event.type}
                      </pre>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}
        </div>
      )}

      {!loading && runs.length === 0 && (
        <div className="mt-3 text-xs text-muted-foreground">No background runs yet.</div>
      )}
    </div>
  );
};
