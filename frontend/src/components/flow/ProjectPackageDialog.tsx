import { useRef, useState } from 'react';
import { Download, Upload } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { apiFetch } from '@/utils/apiFetch';
import { INLUMEN_API_URL } from '@/config/api';
import { getGraphRevision, getPersistenceState, graphReadTicket } from '@/features/flow/persistenceState';

type Preview = { name: string; nodes: number; files: number; definitions: number; secret_parameters: number };
const endpoint = `${INLUMEN_API_URL}/api/pipeline/package`;

async function checkResponse(response: Response) {
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.error || 'The package could not be processed.');
  }
  return response;
}

function ready() {
  const state = getPersistenceState();
  if (state.pending || state.error) throw new Error('Wait for the design to finish saving, or resolve its save error first.');
}

export function ProjectPackageDialog({ open, onOpenChange, onImported }: {
  open: boolean; onOpenChange: (open: boolean) => void; onImported: () => Promise<void>;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selection, setSelection] = useState<{ file: File; preview: Preview; revision: string | null; ticket: string } | null>(null);

  const perform = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try { ready(); await operation(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Package operation failed.'); }
    finally { setBusy(false); }
  };

  const exportPackage = () => perform(async () => {
    const response = await checkResponse(await apiFetch(endpoint));
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'inlumen-project.zip';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  const previewPackage = (file: File) => perform(async () => {
    setSelection(null);
    if (file.size > 50 * 1024 * 1024) throw new Error('Choose a package smaller than 50 MB.');
    const revision = getGraphRevision();
    const ticket = graphReadTicket();
    const response = await checkResponse(await apiFetch(`${endpoint}?preview=true`, {
      method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: file,
    }));
    setSelection({ file, preview: await response.json(), revision, ticket });
  });

  const importPackage = () => perform(async () => {
    if (!selection) return;
    if (selection.revision !== getGraphRevision() || selection.ticket !== graphReadTicket()) {
      setSelection(null);
      throw new Error('The workspace changed. Select the package again to review the import.');
    }
    await checkResponse(await apiFetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: selection.file,
    }));
    setSelection(null);
    await onImported();
    onOpenChange(false);
  });

  return <Dialog open={open} onOpenChange={value => {
    if (busy) return;
    setSelection(null); setError(''); onOpenChange(value);
  }}>
    <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg" onInteractOutside={event => { if (busy) event.preventDefault(); }}>
      <DialogHeader>
        <DialogTitle>Project package</DialogTitle>
        <DialogDescription>Move a complete pipeline between workspaces, including its design, code, attached data, and reusable pipelines.</DialogDescription>
      </DialogHeader>
      <p className="text-sm text-muted-foreground">Exports use the saved design. Secret runtime parameter values are omitted; attached files are included as supplied.</p>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" disabled={busy} onClick={exportPackage}><Download className="mr-2 h-4 w-4" />Export package</Button>
        <Button variant="outline" disabled={busy} onClick={() => input.current?.click()}><Upload className="mr-2 h-4 w-4" />Choose package</Button>
        <input ref={input} aria-label="Choose project package" type="file" accept=".zip" className="hidden" onChange={event => {
          const file = event.target.files?.[0]; event.target.value = ''; if (file) void previewPackage(file);
        }} />
      </div>
      {selection && <div className="space-y-2 rounded-md border p-3 text-sm">
        <p className="font-medium">{selection.preview.name}</p>
        <p>{selection.preview.nodes} node{selection.preview.nodes === 1 ? '' : 's'} · {selection.preview.files} file{selection.preview.files === 1 ? '' : 's'} · {selection.preview.definitions} reusable pipeline{selection.preview.definitions === 1 ? '' : 's'}</p>
        <p>This replaces the current canvas. Saved versions remain available. Reusable pipelines are imported as new definitions.</p>
        {selection.preview.secret_parameters > 0 && <p>{selection.preview.secret_parameters} secret parameter{selection.preview.secret_parameters === 1 ? ' will need a value' : 's will need values'} after import.</p>}
      </div>}
      {busy && <p role="status" className="text-sm text-muted-foreground">Processing package…</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <DialogFooter>
        <Button variant="outline" disabled={busy} onClick={() => { setSelection(null); onOpenChange(false); }}>Close</Button>
        {selection && <Button disabled={busy} onClick={importPackage}>Replace canvas and import</Button>}
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
