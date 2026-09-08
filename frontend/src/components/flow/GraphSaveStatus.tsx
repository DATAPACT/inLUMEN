import { useState, useSyncExternalStore } from 'react';
import { getPersistenceState, subscribePersistence } from '@/features/flow/persistenceState';
import { Button } from '@/components/ui/button';

export function GraphSaveStatus({ onDownload, onReload }: { onDownload: () => void; onReload: () => Promise<void> }) {
  const [reloading, setReloading] = useState(false);
  const reload = async () => {
    setReloading(true);
    try { await onReload(); } finally { setReloading(false); }
  };
  const state = useSyncExternalStore(subscribePersistence, getPersistenceState);
  return <div className="absolute left-3 top-3 z-20 max-w-md rounded-lg border bg-background/95 px-3 py-2 text-xs shadow-sm" role="status" aria-live="polite">
    {state.error ? <>
      <p className="text-[hsl(var(--danger-text))]">Not saved: {state.error}</p>
      <div className="mt-2 flex gap-2">
        <Button size="sm" variant="outline" onClick={onDownload}>Download draft</Button>
        <Button size="sm" variant="outline" onClick={() => { void reload(); }} disabled={state.pending > 0 || reloading}>Reload saved graph</Button>
      </div>
    </> : reloading ? 'Reloading saved graph…' : state.pending ? 'Saving…' : 'Changes saved'}
  </div>;
}
