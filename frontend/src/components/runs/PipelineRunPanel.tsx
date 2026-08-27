import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Ban,
  Check,
  Circle,
  Download,
  FileOutput,
  Loader2,
  PlayCircle,
  RefreshCw,
  TerminalSquare,
} from 'lucide-react';

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

type StageState = 'pending' | 'active' | 'complete' | 'error';

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

const formatDate = (value?: string | null) => {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? ''
    : parsed.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
};

const formatDuration = (seconds: number) => {
  const safeSeconds = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(safeSeconds / 60);
  const remainder = safeSeconds % 60;
  return minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
};

const formatMemory = (bytes: number) => {
  const gibibytes = bytes / (1024 ** 3);
  return `${Number.isInteger(gibibytes) ? gibibytes : gibibytes.toFixed(1)} GiB`;
};

const stageStates = (
  status: PipelineRunRecord['status'],
  phase?: string | null,
): StageState[] => {
  if (status === 'queued') return ['active', 'pending', 'pending', 'pending'];
  if (status === 'preparing') return ['complete', 'active', 'pending', 'pending'];
  if (status === 'cancelling') return ['complete', 'complete', 'active', 'pending'];
  if (status === 'running') {
    if (phase && phase !== 'running_pipeline') {
      return ['complete', 'active', 'pending', 'pending'];
    }
    return ['complete', 'complete', 'active', 'pending'];
  }
  if (status === 'succeeded') return ['complete', 'complete', 'complete', 'complete'];
  if (status === 'cancelled') return ['complete', 'complete', 'pending', 'pending'];
  return ['complete', 'complete', 'error', 'pending'];
};

const failureHint = (message: string) => {
  const normalized = message.toLowerCase();
  if (normalized.includes('huggingface') || normalized.includes('cached files')) {
    return 'The model was not available in the isolated runtime. Verify that the uploaded code uses a reviewed, pinned model so inLUMEN can prefetch it.';
  }
  if (normalized.includes('no csv') || normalized.includes('no .wav') || normalized.includes('pipeline_input_dir')) {
    return 'Check that the source node has the expected input file and that the task reads it directly from PIPELINE_INPUT_DIR.';
  }
  if (normalized.includes('environment variable') || normalized.includes('keyerror')) {
    return 'Open the task Inspector and configure the runtime environment value reported by the script.';
  }
  return 'Open Technical logs for the full Dagster trace. The tested snapshot is also available below for local debugging.';
};

const StageIcon = ({ state }: { state: StageState }) => {
  if (state === 'complete') return <Check className="h-3.5 w-3.5" />;
  if (state === 'active') return <Loader2 className="h-3.5 w-3.5 animate-spin" />;
  if (state === 'error') return <AlertCircle className="h-3.5 w-3.5" />;
  return <Circle className="h-3.5 w-3.5" />;
};

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
  const nodeEvents = useMemo(
    () => events.filter((event) => event.type.startsWith('node.')),
    [events],
  );
  const technicalLogs = useMemo(
    () => events.filter((event) => event.type.endsWith('.log')),
    [events],
  );
  const outstandingRunCount = useMemo(
    () => runs.filter((run) => isActivePipelineRun(run.status)).length,
    [runs],
  );
  const capacityLimit = capabilities?.max_outstanding_runs || 0;
  const runCapacityFull = capacityLimit > 0 && outstandingRunCount >= capacityLimit;

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

  const stages = selectedRun
    ? stageStates(selectedRun.status, selectedRun.progress?.phase)
    : [];
  const stageLabels = ['Snapshot', 'Runtime', 'Pipeline', 'Results'];
  const outputs = selectedRun?.result?.outputs || [];
  const elapsedSeconds = selectedRun?.started_at
    ? Math.max(0, (Date.now() - new Date(selectedRun.started_at).valueOf()) / 1000)
    : 0;
  const heartbeatAgeSeconds = selectedRun?.progress?.heartbeat_at
    ? Math.max(0, (Date.now() - new Date(selectedRun.progress.heartbeat_at).valueOf()) / 1000)
    : null;
  const heartbeatIsStale = heartbeatAgeSeconds !== null && heartbeatAgeSeconds > 45;

  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-border p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Run pipeline</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Test the current saved pipeline in the background with Dagster.
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

      <Button
        className="mt-3 h-10 w-full"
        onClick={() => { void handleStart(); }}
        disabled={
          submitting
          || loading
          || !capabilities?.execution_available
          || runCapacityFull
        }
      >
        {submitting ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <PlayCircle className="mr-2 h-4 w-4" />
        )}
        Run current pipeline
      </Button>
      <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
        A fixed snapshot is created at launch. You can close the browser while it runs.
      </p>

      {runCapacityFull && (
        <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-200">
          Run capacity is full ({outstandingRunCount}/{capacityLimit}). Wait for a run to finish or cancel an active run before launching another.
        </div>
      )}

      {!capabilities?.execution_available && capabilities?.message && (
        <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-200">
          {capabilities.message}
        </div>
      )}
      {error && (
        <div className="mt-3 rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {runs.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Recent runs
          </div>
          <div className="max-h-40 space-y-1 overflow-y-auto pr-1">
            {runs.slice(0, 5).map((run) => (
              <button
                type="button"
                key={run.run_id}
                onClick={() => setSelectedRunId(run.run_id)}
                className={cn(
                  'flex w-full items-center justify-between gap-2 rounded-md border px-2 py-2 text-left text-xs',
                  selectedRun?.run_id === run.run_id
                    ? 'border-primary/50 bg-primary/10'
                    : 'border-border bg-muted/20 hover:bg-muted/40',
                )}
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">
                    {run.snapshot.pipeline_version || 'Pipeline'} · {run.snapshot.node_count} nodes
                  </span>
                  <span className="block text-[10px] text-muted-foreground">
                    {formatDate(run.created_at)} · {run.run_id.slice(0, 8)}
                  </span>
                </span>
                <span className={cn('shrink-0 font-medium capitalize', statusClass(run.status))}>
                  {run.status}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {selectedRun && (
        <div className="mt-3 rounded-md border border-border bg-muted/20 p-2.5 text-xs">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="font-medium">Run status</div>
              <div className={cn('mt-0.5 font-medium capitalize', statusClass(selectedRun.status))}>
                {selectedRun.status}
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

          <div className="mt-3 grid grid-cols-4 gap-1" aria-label="Run progress">
            {stageLabels.map((label, index) => (
              <div key={label} className="min-w-0 text-center">
                <div
                  className={cn(
                    'mx-auto flex h-7 w-7 items-center justify-center rounded-full border',
                    stages[index] === 'complete' && 'border-emerald-500/40 bg-emerald-500/15 text-emerald-400',
                    stages[index] === 'active' && 'border-amber-500/40 bg-amber-500/15 text-amber-300',
                    stages[index] === 'error' && 'border-red-500/40 bg-red-500/15 text-red-400',
                    stages[index] === 'pending' && 'border-border text-muted-foreground',
                  )}
                >
                  <StageIcon state={stages[index]} />
                </div>
                <div className="mt-1 truncate text-[10px] text-muted-foreground">{label}</div>
              </div>
            ))}
          </div>

          {isActivePipelineRun(selectedRun.status) && (
            <div
              className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-amber-100"
              aria-live="polite"
            >
              <div className="flex items-center gap-1.5 font-medium">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                {selectedRun.progress?.active_node_name || 'Pipeline is working'}
              </div>
              <div className="mt-1 text-[11px] leading-relaxed text-amber-100/85">
                {selectedRun.progress?.message
                  || 'Dagster is executing the isolated pipeline snapshot. No failure has been reported.'}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-amber-100/70">
                <span>Total elapsed: {formatDuration(elapsedSeconds)}</span>
                {selectedRun.progress?.resource_profile && (
                  <span className="capitalize">
                    Profile: {selectedRun.progress.resource_profile.replace('_', ' ')}
                  </span>
                )}
                {typeof selectedRun.progress?.resource_cpu === 'number'
                  && typeof selectedRun.progress?.resource_memory_bytes === 'number' && (
                  <span>
                    {selectedRun.progress.resource_cpu} CPU · {formatMemory(selectedRun.progress.resource_memory_bytes)}
                  </span>
                )}
                {typeof selectedRun.progress?.queue_position === 'number' && (
                  <span>Queue position: {selectedRun.progress.queue_position}</span>
                )}
                {typeof selectedRun.progress?.node_elapsed_seconds === 'number' && (
                  <span>Current node: {formatDuration(selectedRun.progress.node_elapsed_seconds)}</span>
                )}
                {heartbeatAgeSeconds !== null && (
                  <span className={heartbeatIsStale ? 'text-red-300' : undefined}>
                    Heartbeat: {formatDuration(heartbeatAgeSeconds)} ago
                  </span>
                )}
              </div>
              {(selectedRun.progress?.active_node_name || elapsedSeconds >= 90) && (
                <div className="mt-1.5 text-[10px] leading-relaxed text-amber-100/65">
                  {heartbeatIsStale
                    ? 'No fresh heartbeat has arrived for 45 seconds. The node may be busy or stalled; check again shortly or cancel the run if it remains unchanged.'
                    : heartbeatAgeSeconds !== null
                      ? 'CPU model inference can take several minutes. Fresh heartbeats confirm that the run is active.'
                      : 'CPU model inference can take several minutes; waiting for the first detailed node heartbeat.'}
                </div>
              )}
            </div>
          )}

          {selectedRun.error?.message && (
            <div className="mt-3 rounded-md border border-red-500/30 bg-red-500/10 p-2 text-red-200">
              <div className="flex items-center gap-1.5 font-medium">
                <AlertCircle className="h-3.5 w-3.5" />
                What went wrong
              </div>
              <div className="mt-1 break-words">{selectedRun.error.message}</div>
              <div className="mt-1.5 text-[11px] leading-relaxed text-red-200/80">
                {failureHint(selectedRun.error.message)}
              </div>
            </div>
          )}

          {nodeEvents.length > 0 && (
            <div className="mt-3 border-t border-border pt-2">
              <div className="font-medium">Pipeline activity</div>
              <div className="mt-1.5 space-y-1 text-muted-foreground">
                {nodeEvents.map((event) => (
                  <div key={event.id} className="flex items-start gap-1.5">
                    <span className={cn(
                      'mt-1 h-1.5 w-1.5 shrink-0 rounded-full',
                      event.status === 'failed' ? 'bg-red-400' : event.status === 'succeeded' ? 'bg-emerald-400' : 'bg-amber-400',
                    )} />
                    <span>{event.message || event.type}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {outputs.length > 0 && (
            <div className="mt-3 border-t border-border pt-2">
              <div className="flex items-center gap-1.5 font-medium">
                <FileOutput className="h-3.5 w-3.5" />
                Results
              </div>
              {outputs.map((output, index) => {
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
                    <span className="truncate">{filename}</span>
                  </Button>
                );
              })}
            </div>
          )}

          <details className="mt-3 rounded border border-border bg-background/40 p-2">
            <summary className="cursor-pointer font-medium text-foreground">
              Run details and technical logs
            </summary>
            <div className="mt-2 text-muted-foreground">
              <div>Run ID: <span className="break-all font-mono">{selectedRun.run_id}</span></div>
              <div className="mt-0.5">Snapshot: {selectedRun.snapshot.node_count} nodes · {selectedRun.engine}</div>
              <Button
                variant="outline"
                size="sm"
                className="mt-2 h-auto min-h-8 w-full whitespace-normal py-2"
                onClick={() => { void downloadPipelineRunBundle(selectedRun.run_id); }}
              >
                <Download className="mr-1 h-3.5 w-3.5" />
                Download tested snapshot
              </Button>
              {technicalLogs.length > 0 && (
                <div className="mt-2 border-t border-border pt-2">
                  <div className="mb-1 flex items-center gap-1 font-medium text-foreground">
                    <TerminalSquare className="h-3.5 w-3.5" />
                    Technical logs
                  </div>
                  <div className="max-h-64 space-y-2 overflow-auto">
                    {technicalLogs.map((event) => (
                      <pre key={event.id} className="whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed">
                        {event.message || event.type}
                      </pre>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </details>
        </div>
      )}

      {!loading && runs.length === 0 && (
        <div className="mt-3 rounded-md border border-dashed border-border p-3 text-center text-xs text-muted-foreground">
          No runs yet. Launch the current pipeline to test its code, inputs, and artifact contract.
        </div>
      )}
    </div>
  );
};
