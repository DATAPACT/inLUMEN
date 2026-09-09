import { useState, useSyncExternalStore } from 'react';
import { getPersistenceState, persistenceEpoch, reportPersistenceError, subscribePersistence } from '@/features/flow/persistenceState';
import { Button } from '@/components/ui/button';

type Props = {
  onDownload: () => void;
  onReload: () => Promise<void>;
  onRetry: () => Promise<void>;
  onDownloadPrevious?: () => void;
};

export function GraphSaveStatus({ onDownload, onReload, onRetry, onDownloadPrevious }: Props) {
  const [recovering, setRecovering] = useState(false);
  const state = useSyncExternalStore(subscribePersistence, getPersistenceState);
  const recover = async (action: () => Promise<void>) => {
    const epoch = persistenceEpoch();
    setRecovering(true);
    try { await action(); } catch (error) { if (epoch === persistenceEpoch()) reportPersistenceError(error); }
    finally { setRecovering(false); }
  };
  return <div className="flex flex-wrap items-center gap-1 text-xs" aria-label="Pipeline save status" data-save-state={state.error ? 'error' : recovering || state.pending ? 'saving' : 'saved'}>
    {state.error && <>
      <span className="px-1 text-[hsl(var(--danger-text))]" role="status" aria-live="polite" title={state.error}>
        {recovering ? 'Recovering…' : 'Couldn’t save'}
      </span>
      <span className="sr-only">{state.error}</span>
      {!state.conflict && <Button size="sm" variant="ghost" className="h-7 px-2" disabled={state.pending > 0 || recovering} onClick={() => { void recover(onRetry); }}>Retry save</Button>}
      <Button size="sm" variant="ghost" className="h-7 px-2" onClick={onDownload}>Download draft</Button>
      <Button size="sm" variant="ghost" className="h-7 px-2" title="Keep a local copy of this draft and load the saved graph" disabled={state.pending > 0 || recovering} onClick={() => { void recover(onReload); }}>Reload saved graph</Button>
    </>}
    {!state.error && onDownloadPrevious && <Button size="sm" variant="ghost" className="h-7 px-2" title="Download the local recovery copy retained before reloading the saved pipeline" onClick={onDownloadPrevious}>Download recovery copy</Button>}
  </div>;
}
