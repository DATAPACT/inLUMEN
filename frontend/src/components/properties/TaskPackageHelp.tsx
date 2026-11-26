import guide from '@/features/flow/taskPackageGuide.generated.json';
import { copyExternalPrompt, downloadTaskTemplate } from '@/features/flow/taskPackages';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
export function TaskPackageHelp() {
  const act = async (fn: () => Promise<void>, message: string) => {
    try { await fn(); toast.success(message); } catch (e) { toast.error(e instanceof Error ? e.message : 'Action failed'); }
  };
  return <div className="space-y-2 text-xs text-muted-foreground">
    <p className="font-medium text-foreground">Task runtime contract</p>
    <p>{guide.guide.split('\n')[0]}</p>
    <p>Each connection supplies one file or directory bundle in PIPELINE_INPUT_DIR. Use INLUMEN_INPUT_MANIFEST to identify inputs. Write only the declared output in PIPELINE_OUTPUT_DIR; use PIPELINE_WORK_DIR for temporary files. Port names never create implicit subdirectories.</p>
    <pre className="overflow-x-auto rounded bg-muted p-2">{JSON.stringify(guide.example, null, 2)}</pre>
    <p>The copied prompt includes your saved pipeline, exact Task folders, the full manifest schema, and model-loading requirements. Model revisions must be verified commit hashes; branches such as main are not accepted.</p>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" onClick={() => void act(downloadTaskTemplate, 'Template downloaded')}>Download Task template</Button>
      <Button size="sm" variant="outline" onClick={() => void act(copyExternalPrompt, 'External-generation prompt copied')}>Copy external-generation prompt</Button>
    </div>
    <p>Package validation checks declarations and code structure. Model availability and successful execution are verified by a run.</p>
  </div>;
}
