import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { startPolling } from '@/utils/polling';
import {
  AlertCircle,
  Ban,
  Check,
  ChevronDown,
  Circle,
  Download,
  Eye,
  FileOutput,
  Loader2,
  PlayCircle,
  RefreshCw,
  TerminalSquare,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  cancelPipelineRun,
  downloadPipelineRunBundle,
  downloadPipelineRunLineage,
  downloadPipelineRunOutput,
  fetchPipelineRunEvents,
  fetchRunnerCapabilities,
  fetchExecutionWorkload,
  type ExecutionWorkload,
  getPipelineRun,
  isActivePipelineRun,
  listPipelineRuns,
  startPipelineRun,
  PIPELINE_RUN_STARTED_EVENT,
  type PipelineRunEvent,
  type PipelineRunOptions,
  type PipelineRunRecord,
  type RunnerCapabilities,
} from '@/features/runs/pipelineRuns';
import {
  formatOutputSize,
  isWaitingForExecution,
  presentRunPhase,
  presentRunOutputs,
  presentRunFailure,
  summarizeNodeEvents,
  type PresentedRunOutput,
} from '@/features/runs/runPresentation';
import { RunOutputViewer } from '@/components/runs/RunOutputViewer';
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

const StageIcon = ({ state }: { state: StageState }) => {
  if (state === 'complete') return <Check className="h-3.5 w-3.5" />;
  if (state === 'active') return <Loader2 className="h-3.5 w-3.5 animate-spin" />;
  if (state === 'error') return <AlertCircle className="h-3.5 w-3.5" />;
  return <Circle className="h-3.5 w-3.5" />;
};

const RunOutputRow = ({
  output,
  onOpen,
  onDownload,
  compact = false,
  showPath = false,
}: {
  output: PresentedRunOutput;
  onOpen: () => void;
  onDownload: () => void;
  compact?: boolean;
  showPath?: boolean;
}) => {
  const metadata = showPath
    ? output.path
    : [
      compact ? output.nodeLabel : output.kind || output.contentType,
      formatOutputSize(output.sizeBytes),
      output.copies > 1 ? `${output.copies} artifacts` : '',
    ].filter(Boolean).join(' · ');
  return (
    <div className="flex items-center gap-1">
      <Button
        variant="ghost"
        size="sm"
        className={cn(
          'h-auto min-w-0 flex-1 justify-start px-2 text-left',
          compact ? 'min-h-9 py-1' : 'min-h-10 py-1.5',
        )}
        onClick={onOpen}
        title={`Open ${output.filename}`}
      >
        <Eye className={cn('mr-2 shrink-0', compact ? 'h-3 w-3' : 'h-3.5 w-3.5')} />
        <span className="min-w-0">
          <span className={cn('block truncate', !compact && 'font-medium')}>{output.filename}</span>
          {metadata && (
            <span className="block truncate text-[10px] font-normal text-muted-foreground">{metadata}</span>
          )}
        </span>
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7 shrink-0"
        onClick={onDownload}
        aria-label={`Download ${output.filename}`}
        title={`Download ${output.filename}`}
      >
        <Download className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
};

export const PipelineRunPanel = () => {
  const [capabilities, setCapabilities] = useState<RunnerCapabilities | null>(null);
  const [runs, setRuns] = useState<PipelineRunRecord[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>('');
  const manuallySelected = useRef(false);
  const listInFlight = useRef(false);
  const [events, setEvents] = useState<PipelineRunEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [workload, setWorkload] = useState<ExecutionWorkload | null>(null);
  const [workloadFresh, setWorkloadFresh] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) || runs[0] || null,
    [runs, selectedRunId],
  );
  const selectedRunIdForRefresh = selectedRun?.run_id || '';
  const selectedRunStatus = selectedRun?.status;
  const nodeEvents = useMemo(
    () => summarizeNodeEvents(events.filter((event) => event.type.startsWith('node.'))),
    [events],
  );
  const technicalLogs = useMemo(
    () => events.filter((event) => event.type.endsWith('.log')),
    [events],
  );
  const failure = presentRunFailure(
    selectedRun?.error?.message || '',
    technicalLogs.map((event) => event.message || ''),
  );
  const presentedOutputs = useMemo(
    () => presentRunOutputs(selectedRun?.result?.outputs || []),
    [selectedRun?.result?.outputs],
  );
  const finalOutputs = useMemo(
    () => presentedOutputs.primary.filter((output) => output.role !== 'intermediate'),
    [presentedOutputs],
  );
  const stepOutputs = useMemo(
    () => presentedOutputs.primary.filter((output) => output.role === 'intermediate'),
    [presentedOutputs],
  );
  const [viewerPath, setViewerPath] = useState<string | null>(null);
  const openViewer = (path: string) => setViewerPath(path);
  const outstandingRunCount = useMemo(
    () => runs.filter((run) => isActivePipelineRun(run.status)).length,
    [runs],
  );
  const lastRunFailed = runs[0]?.status === 'failed';
  const capacityLimit = capabilities?.max_outstanding_runs || 0;
  const runCapacityFull = capacityLimit > 0 && outstandingRunCount >= capacityLimit;
  const globalCapacityFull = workloadFresh && workload !== null && workload.outstanding_runs >= workload.max_outstanding_runs;
  const startDisabled = submitting
    || loading
    || !capabilities?.execution_available
    || runCapacityFull
    || globalCapacityFull;

  const loadInitial = useCallback(async () => {
    if (listInFlight.current) return;
    listInFlight.current = true;
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
      listInFlight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadInitial();
  }, [loadInitial]);

  useEffect(() => {
    let disposed = false;
    const stop = startPolling(async () => {
      if (listInFlight.current) return;
      listInFlight.current = true;
      try {
        const recent = await listPipelineRuns();
        if (disposed) return;
        setRuns(recent);
        setSelectedRunId(current => manuallySelected.current && recent.some(run => run.run_id === current)
          ? current : recent[0]?.run_id || '');
      } finally { listInFlight.current = false; }
    }, 5000);
    return () => { disposed = true; stop(); };
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: number;
    const refresh = async () => {
      try {
        const next = await fetchExecutionWorkload();
        if (!stopped) { setWorkload(next); setWorkloadFresh(next.worker_available); }
      } catch { if (!stopped) setWorkloadFresh(false); }
      if (!stopped) timer = window.setTimeout(refresh, document.hidden ? 15000 : 3000);
    };
    void refresh();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, []);

  useEffect(() => {
    setEvents([]);
    setRefreshFailed(false);
    if (!selectedRunIdForRefresh) return;
    let stopped = false;
    let timer: number;
    let cursor = 0;
    const refresh = async () => {
      let active = true;
      try {
        const [record, payload] = await Promise.all([
          getPipelineRun(selectedRunIdForRefresh),
          fetchPipelineRunEvents(selectedRunIdForRefresh, cursor),
        ]);
        if (stopped) return;
        cursor = payload.next_cursor;
        setRuns(current => mergeRun(current, record));
        setEvents(current => [...current, ...(payload.events || [])]);
        setRefreshFailed(false);
        active = isActivePipelineRun(record.status);
      } catch { if (!stopped) setRefreshFailed(true); }
      if (!stopped && active) timer = window.setTimeout(refresh, document.hidden ? 15000 : 3000);
    };
    void refresh();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [selectedRunIdForRefresh, selectedRunStatus]);

  useEffect(() => {
    const follow = (event: Event) => {
      const run = (event as CustomEvent<PipelineRunRecord>).detail;
      if (!run?.run_id) return;
      setRuns((current) => mergeRun(current, run));
      setSelectedRunId(run.run_id);
    };
    window.addEventListener(PIPELINE_RUN_STARTED_EVENT, follow);
    return () => window.removeEventListener(PIPELINE_RUN_STARTED_EVENT, follow);
  }, []);

  const handleStart = async (options: PipelineRunOptions = {}) => {
    setSubmitting(true);
    setError('');
    try {
      await startPipelineRun(newIdempotencyKey(), options);
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
    ? stageStates(isWaitingForExecution(selectedRun) ? 'queued' : selectedRun.status, selectedRun.progress?.phase)
    : [];
  const stageLabels = ['Queue', stages[1] === 'complete' ? 'Runtime built' : 'Runtime', 'Pipeline', 'Results'];
  const waiting = selectedRun ? isWaitingForExecution(selectedRun) : false;
  const runEnd = selectedRun?.finished_at ? Date.parse(selectedRun.finished_at) : Date.now();
  const admitted = selectedRun?.progress?.admitted_at;
  const elapsedSeconds = selectedRun ? Math.max(0, (runEnd - Date.parse(selectedRun.created_at)) / 1000) : 0;
  const queueSeconds = selectedRun ? Math.max(0, ((admitted ? Date.parse(admitted) : runEnd) - Date.parse(selectedRun.created_at)) / 1000) : 0;
  const executionSeconds = admitted ? Math.max(0, (runEnd - Date.parse(admitted)) / 1000) : 0;
  const observationStale = selectedRun && isActivePipelineRun(selectedRun.status)
    && (selectedRun.progress?.observed_at ? Date.now() - Date.parse(selectedRun.progress.observed_at) > 15000 : elapsedSeconds > 15);
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

      <div className="mt-3 flex gap-1">
        <Button
          className="h-10 min-w-0 flex-1"
          onClick={() => { void handleStart({ cache: 'reuse' }); }}
          disabled={startDisabled}
          title="Steps whose code, parameters and inputs did not change reuse their earlier results."
        >
          {submitting ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <PlayCircle className="mr-2 h-4 w-4" />
          )}
          {lastRunFailed ? 'Resume from failed step' : 'Run current pipeline'}
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button className="h-10 w-9 shrink-0 px-0" disabled={startDisabled} aria-label="More run options">
              <ChevronDown className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-64">
            <DropdownMenuItem onSelect={() => { void handleStart({ cache: 'reuse' }); }}>
              <div>
                <div>Run, reusing unchanged steps</div>
                <div className="text-[11px] text-muted-foreground">
                  {lastRunFailed ? 'Steps that finished last time are not run again.' : 'The default for every run.'}
                </div>
              </div>
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => { void handleStart({ cache: 'refresh' }); }}>
              <div>
                <div>Run everything again</div>
                <div className="text-[11px] text-muted-foreground">Ignore earlier results and run every step.</div>
              </div>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
        A fixed snapshot is created at launch. Unchanged steps reuse earlier results. You can close the browser while it runs.
      </p>

      <div className="mt-3 text-xs text-muted-foreground">
        {workloadFresh && workload ? `${workload.active_runs} running / ${workload.max_active_runs} slots · ${workload.queued_runs} queued across inLUMEN` : 'Workload information is temporarily unavailable.'}
      </div>
      {(refreshFailed || observationStale) && <p role="status" className="mt-2 text-xs text-amber-200">Run updates are delayed. Your run may still be processing; refresh to check its status.</p>}
      {globalCapacityFull && <p role="status" className="mt-2 text-xs text-amber-200">The shared queue is full. Try again when a run finishes.</p>}

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
                onClick={() => { manuallySelected.current = true; setSelectedRunId(run.run_id); }}
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
                  {presentRunPhase(run)}
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
                {presentRunPhase(selectedRun)}
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

            >
              <div role="status" aria-live="polite" aria-atomic="true" className="flex items-center gap-1.5 font-medium">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                {presentRunPhase(selectedRun)}
              </div>
              <div className="mt-1 text-[11px] leading-relaxed text-amber-100/85">
                {waiting ? `Waiting for an execution slot${selectedRun.progress?.queue_position ? ` · position ${selectedRun.progress.queue_position}` : ''}. Your run will start automatically.` : selectedRun.progress?.message || 'Processing the saved pipeline.'}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-amber-100/70">
                <span>Total elapsed: {formatDuration(elapsedSeconds)}</span>
                <span>Queue wait: {formatDuration(queueSeconds)}</span>
                {admitted && <span>Execution: {formatDuration(executionSeconds)}</span>}
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
              {!waiting && (selectedRun.progress?.active_node_name || elapsedSeconds >= 90) && (
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
              <div className="mt-1 wrap-break-word">{failure.message}</div>
              <div className="mt-1.5 text-[11px] leading-relaxed text-red-200/80">
                {failure.hint}
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
                      event.type === 'node.cached'
                        ? 'bg-sky-400'
                        : event.status === 'failed' ? 'bg-red-400' : event.status === 'succeeded' ? 'bg-emerald-400' : 'bg-amber-400',
                    )} />
                    <span>{event.message || event.type}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {presentedOutputs.all.length > 0 && (
            <div className="mt-3 border-t border-border pt-2">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-1.5 font-medium">
                    <FileOutput className="h-3.5 w-3.5" />
                    Results
                  </div>
                  <p className="mt-0.5 text-[10px] text-muted-foreground">
                    Open a file to inspect it here. Outputs of every step are kept with the run.
                  </p>
                </div>
                <span className="shrink-0 rounded-full bg-background/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {finalOutputs.length}
                </span>
              </div>
              {finalOutputs.length > 0 ? (
                <div className="mt-1.5 space-y-1">
                  {finalOutputs.map((output) => (
                    <RunOutputRow
                      key={output.path}
                      output={output}
                      onOpen={() => openViewer(output.path)}
                      onDownload={() => {
                        void downloadPipelineRunOutput(selectedRun.run_id, output.path, output.filename);
                      }}
                    />
                  ))}
                </div>
              ) : (
                <p className="mt-2 rounded-md border border-dashed border-border p-2 text-[11px] text-muted-foreground">
                  {selectedRun.status === 'succeeded'
                    ? 'This run produced technical artifacts only; there is no user-facing result file.'
                    : 'The run stopped before its final steps produced results.'}
                </p>
              )}

              {stepOutputs.length > 0 && (
                <div className="mt-2">
                  <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                    Intermediate step outputs ({stepOutputs.length})
                  </div>
                  <div className="mt-1 space-y-1">
                    {stepOutputs.map((output) => (
                      <RunOutputRow
                        key={output.path}
                        output={output}
                        compact
                        onOpen={() => openViewer(output.path)}
                        onDownload={() => {
                          void downloadPipelineRunOutput(selectedRun.run_id, output.path, output.filename);
                        }}
                      />
                    ))}
                  </div>
                </div>
              )}

              {presentedOutputs.hiddenCount > 0 && (
                <details className="mt-2 rounded border border-border bg-background/30">
                  <summary className="cursor-pointer px-2 py-1.5 text-[11px] font-medium text-muted-foreground">
                    All artifacts and manifests ({presentedOutputs.all.length})
                  </summary>
                  <div className="space-y-1 border-t border-border p-1.5">
                    {presentedOutputs.all.map((output) => (
                      <RunOutputRow
                        key={output.path}
                        output={output}
                        compact
                        showPath
                        onOpen={() => openViewer(output.path)}
                        onDownload={() => {
                          void downloadPipelineRunOutput(selectedRun.run_id, output.path, output.filename);
                        }}
                      />
                    ))}
                  </div>
                </details>
              )}
              <RunOutputViewer
                open={viewerPath !== null}
                onOpenChange={(open) => { if (!open) setViewerPath(null); }}
                runId={selectedRun.run_id}
                outputs={presentedOutputs.all}
                initialPath={viewerPath || undefined}
              />
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
              {selectedRun.result?.steps?.length ? (
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-2 h-auto min-h-8 w-full whitespace-normal py-2"
                  title="Which step used and produced which file, as PROV-O JSON-LD"
                  onClick={() => { void downloadPipelineRunLineage(selectedRun.run_id); }}
                >
                  <Download className="mr-1 h-3.5 w-3.5" />
                  Download run lineage
                </Button>
              ) : null}
              {technicalLogs.length > 0 && (
                <div className="mt-2 border-t border-border pt-2">
                  <div className="mb-1 flex items-center gap-1 font-medium text-foreground">
                    <TerminalSquare className="h-3.5 w-3.5" />
                    Technical logs
                  </div>
                  <div className="max-h-64 space-y-2 overflow-auto">
                    {technicalLogs.map((event) => (
                      <pre key={event.id} className="whitespace-pre-wrap wrap-break-word font-mono text-[10px] leading-relaxed">
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
