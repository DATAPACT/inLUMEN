import { AlertTriangle, Ban, CheckCircle2, FileCode2, FileText, Loader2, ShieldCheck, Sparkles, Table2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  artifactKindLabel,
  type ArtifactDesign,
  type CodeZipConsentRequest,
} from '@/features/flow/designFromArtifact';

const CODE_FILE = /\.(py|r|sql|sh)$/i;

function fileName(path: string): { folder: string; name: string } {
  const index = path.lastIndexOf('/');
  return index < 0 ? { folder: '', name: path } : { folder: path.slice(0, index + 1), name: path.slice(index + 1) };
}

function FileIcon({ path, part }: { path: string; part: string }) {
  if (part === 'first line only') return <Table2 className="h-4 w-4 shrink-0 text-sky-500" />;
  if (CODE_FILE.test(path)) return <FileCode2 className="h-4 w-4 shrink-0 text-violet-500" />;
  return <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />;
}

type ConsentProps = {
  request: CodeZipConsentRequest | null;
  fileName: string;
  modelName: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

/** Asks before the files of a code ZIP are sent to the model. */
export function CodeImportConsentDialog({ request, fileName: zipName, modelName, busy, onConfirm, onCancel }: ConsentProps) {
  const sent = request?.files_to_send ?? [];
  const notSent = request?.files_not_sent ?? [];
  return (
    <Dialog open={request !== null} onOpenChange={(open) => { if (!open && !busy) onCancel(); }}>
      <DialogContent className="max-h-[85vh] max-w-xl overflow-y-auto" data-testid="code-import-consent">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-violet-500" /> Build a design from this code?
          </DialogTitle>
          <DialogDescription>
            <span className="font-medium text-foreground">{zipName}</span> holds code, not an inLUMEN bundle or an
            Argo workflow. The model reads the files below and proposes the steps and connections.
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center justify-between gap-3 rounded-lg border bg-muted/40 px-3 py-2">
          <span className="text-xs text-muted-foreground">Sent to the model</span>
          <span className="truncate font-mono text-xs">{modelName}</span>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">Files that are sent</h3>
            <Badge variant="secondary">{sent.length}</Badge>
          </div>
          <ul className="max-h-56 divide-y overflow-y-auto rounded-lg border">
            {sent.map((file) => {
              const { folder, name } = fileName(file.path);
              return (
                <li key={file.path} className="flex items-center gap-2 px-3 py-2">
                  <FileIcon path={file.path} part={file.part} />
                  <span className="min-w-0 flex-1 truncate font-mono text-xs" title={file.path}>
                    <span className="text-muted-foreground">{folder}</span>{name}
                  </span>
                  <Badge variant={file.part === 'whole file' ? 'outline' : 'secondary'} className="shrink-0 text-[10px]">
                    {file.part === 'first line only' ? 'first line only' : file.part}
                  </Badge>
                </li>
              );
            })}
          </ul>
        </div>

        {notSent.length > 0 && (
          <details className="rounded-lg border px-3 py-2 text-xs">
            <summary className="flex cursor-pointer items-center gap-2 text-muted-foreground">
              <Ban className="h-3.5 w-3.5" /> {notSent.length} other file{notSent.length === 1 ? ' is' : 's are'} not sent
            </summary>
            <ul className="mt-2 max-h-28 space-y-1 overflow-y-auto font-mono text-[11px] text-muted-foreground">
              {notSent.map((file) => <li key={file.path} className="truncate" title={file.path}>{file.path}</li>)}
            </ul>
          </details>
        )}

        <ul className="space-y-1.5 text-xs text-muted-foreground">
          <li className="flex items-start gap-2">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
            Nothing is executed. Of a table only the first line is sent, for the column names.
          </li>
          <li className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
            The import replaces the current canvas.
          </li>
        </ul>

        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={busy}>Cancel</Button>
          <Button onClick={onConfirm} disabled={busy || sent.length === 0}>
            {busy
              ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Reading the code…</>
              : <><Sparkles className="mr-2 h-4 w-4" /> Send and build design</>}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** What an import from an artifact or from code produced, and what the file did not say. */
export function ImportSummaryDialog({ design, onClose }: { design: ArtifactDesign | null; onClose: () => void }) {
  const inferred = design?.kind === 'code-zip';
  return (
    <Dialog open={design !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-h-[85vh] max-w-xl overflow-y-auto" data-testid="import-summary">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <CheckCircle2 className="h-5 w-5 text-emerald-500" />
            Design imported from {design ? artifactKindLabel(design.kind) : ''}
          </DialogTitle>
          <DialogDescription>
            {design?.step_count ?? 0} steps and {design?.connection_count ?? 0} connections are on the canvas.
          </DialogDescription>
        </DialogHeader>
        {inferred && (
          <p className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
            The model inferred this design from the code. Check each step against the code.
          </p>
        )}
        <ul className="space-y-2 text-sm">
          {/* For code, the first note is the caution shown above. */}
          {(design?.notes ?? []).slice(inferred ? 1 : 0).map((note, index) => (
            <li key={index} className="rounded-lg border px-3 py-2 leading-relaxed text-muted-foreground">{note}</li>
          ))}
        </ul>
        <DialogFooter>
          <Button onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
