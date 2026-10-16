import { useEffect, useRef, useState } from 'react';
import { Activity, Loader2, Plus, Puzzle, Trash2, Upload } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import {
  checkPluginEndpoint,
  installPluginManifest,
  uninstallPlugin,
  type EndpointCheck,
} from '@/features/plugins/pluginApi';
import { loadPlugins, usePluginRegistry } from '@/features/plugins/pluginRegistry';

const STEP_TYPE_LABEL: Record<string, string> = {
  source: 'Source',
  task: 'Task',
  destination: 'Destination',
};

const BUILT_IN = 'built-in';

export function PluginManagerDialog({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const registry = usePluginRegistry();
  const [tab, setTab] = useState('installed');
  const [definition, setDefinition] = useState('');
  const [addError, setAddError] = useState('');
  const [isAdding, setIsAdding] = useState(false);
  const [busyId, setBusyId] = useState('');
  const [checks, setChecks] = useState<Record<string, EndpointCheck>>({});
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) void loadPlugins(true);
  }, [open]);

  const add = async () => {
    setAddError('');
    let manifest: unknown;
    try {
      manifest = JSON.parse(definition);
    } catch {
      setAddError('The definition is not valid JSON.');
      return;
    }
    setIsAdding(true);
    try {
      const added = await installPluginManifest(manifest);
      toast.success(`${added.name} ${added.version} added`);
      setDefinition('');
      await loadPlugins(true);
      setTab('installed');
    } catch (error) {
      setAddError(error instanceof Error ? error.message : 'Could not add the plugin.');
    } finally {
      setIsAdding(false);
    }
  };

  const loadFile = async (file: File | undefined) => {
    if (!file) return;
    setDefinition(await file.text());
    setAddError('');
  };

  const remove = async (pluginId: string, name: string) => {
    if (!window.confirm(`Remove ${name}? Pipeline steps that use it will stop validating until it is added again.`)) return;
    setBusyId(pluginId);
    try {
      await uninstallPlugin(pluginId);
      toast.success(`${name} removed`);
      await loadPlugins(true);
    } catch (error) {
      toast.error('Remove failed', { description: error instanceof Error ? error.message : undefined });
    } finally {
      setBusyId('');
    }
  };

  const check = async (pluginId: string) => {
    setBusyId(pluginId);
    try {
      const result = await checkPluginEndpoint(pluginId);
      setChecks((current) => ({ ...current, [pluginId]: result }));
    } catch (error) {
      toast.error('Endpoint check failed', { description: error instanceof Error ? error.message : undefined });
    } finally {
      setBusyId('');
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Puzzle className="h-5 w-5 text-violet-500" /> Plugins
          </DialogTitle>
          <DialogDescription>
            Plugins connect pipeline steps to third-party tools that already run as a
            deployed service. inLUMEN calls the tool&apos;s REST API.
          </DialogDescription>
        </DialogHeader>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="installed">Available ({registry.plugins.length})</TabsTrigger>
            {registry.canManage && <TabsTrigger value="add">Add plugin</TabsTrigger>}
          </TabsList>

          <TabsContent value="installed" className="space-y-3">
            {registry.error && <p role="alert" className="text-sm text-destructive">{registry.error}</p>}
            {registry.loaded && registry.plugins.length === 0 && !registry.error && (
              <p className="text-sm text-muted-foreground">
                No plugins yet.{registry.canManage ? ' Use Add plugin to define one.' : ' Ask an administrator to add one.'}
              </p>
            )}
            {registry.plugins.map((plugin) => {
              const result = checks[plugin.id];
              const builtIn = plugin.source === BUILT_IN;
              return (
                <div key={plugin.id} className="space-y-2 rounded-lg border p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{plugin.name}</span>
                        <Badge variant="outline">{STEP_TYPE_LABEL[plugin.step_type] || plugin.step_type}</Badge>
                        <Badge variant="secondary">v{plugin.version}</Badge>
                        {builtIn && <Badge variant="outline">Built-in</Badge>}
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">{plugin.description}</p>
                      <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
                        {plugin.manifest.endpoint.base_url}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <Button size="sm" variant="outline" disabled={busyId === plugin.id} onClick={() => { void check(plugin.id); }}>
                        <Activity className="mr-1.5 h-3.5 w-3.5" /> Check endpoint
                      </Button>
                      {registry.canManage && !builtIn && (
                        <Button size="sm" variant="ghost" aria-label={`Remove ${plugin.name}`}
                          disabled={busyId === plugin.id}
                          onClick={() => { void remove(plugin.id, plugin.name); }}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </div>
                  </div>
                  {result && (
                    <p className={result.reachable && result.healthy !== false ? 'text-xs text-emerald-600' : 'text-xs text-destructive'}>
                      {result.reachable
                        ? `${result.healthy === false ? 'Reachable but unhealthy' : 'Reachable'} · HTTP ${result.status} · ${result.latency_ms} ms`
                        : result.detail || 'Unreachable'}
                    </p>
                  )}
                </div>
              );
            })}
          </TabsContent>

          {registry.canManage && (
            <TabsContent value="add" className="space-y-3">
              <p className="text-xs text-muted-foreground">
                Paste or upload a plugin definition (<code>*.plugin.json</code>) written from the
                tool owner&apos;s API description. Adding a definition with an existing id updates it.
                To ship a plugin with inLUMEN, add the file to <code>backend/plugins/manifests</code> instead.
                See <code>docs/plugins.md</code> for the format.
              </p>
              <Textarea
                aria-label="Plugin definition JSON"
                className="min-h-64 font-mono text-xs"
                placeholder='{"schema_version": "inlumen.plugin@1", "id": "owner.tool", ...}'
                value={definition}
                onChange={(event) => setDefinition(event.target.value)}
              />
              {addError && <p role="alert" className="whitespace-pre-wrap text-xs text-destructive">{addError}</p>}
              <div className="flex justify-between gap-2">
                <input
                  ref={fileInput}
                  type="file"
                  accept=".json,application/json"
                  className="hidden"
                  onChange={(event) => { void loadFile(event.target.files?.[0]); event.target.value = ''; }}
                />
                <Button type="button" variant="outline" onClick={() => fileInput.current?.click()}>
                  <Upload className="mr-1.5 h-4 w-4" /> Load file
                </Button>
                <Button type="button" disabled={!definition.trim() || isAdding} onClick={() => { void add(); }}>
                  {isAdding ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <Plus className="mr-1.5 h-4 w-4" />}
                  Add plugin
                </Button>
              </div>
            </TabsContent>
          )}
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
