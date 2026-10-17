import { useState } from 'react';
import { Activity, Eye, EyeOff, Puzzle } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { checkPluginEndpoint, type EndpointCheck, type PluginManifest, type PluginParameter } from '@/features/plugins/pluginApi';
import { DEFAULT_PLUGIN_OUTPUT_MODE, isRequiredParameter, parametersForOperation, PLUGIN_OUTPUT_MODES } from '@/features/plugins/pluginTemplates';

export type PluginOption = { value: string; label: string; description?: string };

type Props = {
  options: PluginOption[];
  currentTemplate: string;
  manifest?: PluginManifest;
  missingPluginId?: string | null;
  param: Record<string, unknown>;
  secretParamKeys: string[];
  secretDrafts: Record<string, string>;
  configuredSecrets: Set<string>;
  revealedSecrets: Set<string>;
  canManagePlugins: boolean;
  onSelectPlugin: (template: string | null) => void;
  onParameterChange: (name: string, value: string) => void;
  onSecretCommit: (name: string) => void;
  onToggleSecret: (name: string) => void;
  onAddSecret: (name: string) => void;
  onManagePlugins: () => void;
};

const displayValue = (value: unknown) =>
  value === undefined || value === null ? '' : typeof value === 'string' ? value : JSON.stringify(value);

const fieldId = (name: string) => `plugin-parameter-${name}`;

export function PluginStepSettings({
  options,
  currentTemplate,
  manifest,
  missingPluginId,
  param,
  secretParamKeys,
  secretDrafts,
  configuredSecrets,
  revealedSecrets,
  canManagePlugins,
  onSelectPlugin,
  onParameterChange,
  onSecretCommit,
  onToggleSecret,
  onAddSecret,
  onManagePlugins,
}: Props) {
  const [check, setCheck] = useState<EndpointCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const selected = manifest ? currentTemplate : missingPluginId ? currentTemplate : '';
  const operationId = String(param.operation || manifest?.operations[0]?.id || '');
  const operation = manifest?.operations.find((item) => item.id === operationId) || manifest?.operations[0];
  const parameters: PluginParameter[] = manifest ? parametersForOperation(manifest, operationId) : [];

  const runCheck = async () => {
    if (!manifest) return;
    setChecking(true);
    try {
      setCheck(await checkPluginEndpoint(manifest.id));
    } catch (error) {
      setCheck({ reachable: false, url: manifest.endpoint.base_url, detail: error instanceof Error ? error.message : 'Check failed' });
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="space-y-2">
        <Label htmlFor="plugin-selection">Tool</Label>
        <select
          id="plugin-selection"
          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
          value={selected}
          onChange={(event) => onSelectPlugin(event.target.value || null)}
        >
          <option value="">No plugin</option>
          {missingPluginId && !manifest && (
            <option value={currentTemplate}>{missingPluginId} (not installed)</option>
          )}
          {options.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onManagePlugins}>
          {canManagePlugins ? 'Manage plugins…' : 'View plugins…'}
        </Button>
      </div>

      {missingPluginId && !manifest && (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
          Plugin {missingPluginId} is not available on this server. Ask an administrator to add its definition, or choose another tool.
        </p>
      )}

      {manifest && (
        <>
          <div className="rounded-md border border-violet-500/25 bg-violet-500/5 p-3 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <Puzzle className="h-4 w-4 text-violet-500" />
              <span className="font-medium text-foreground">{manifest.name}</span>
              <Badge variant="secondary" className="text-[10px]">v{manifest.version}</Badge>
              {manifest.publisher && <span className="text-muted-foreground">by {manifest.publisher}</span>}
            </div>
            <p className="mt-1 text-muted-foreground">{manifest.description}</p>
            <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
              {operation ? `${operation.method} ` : ''}{manifest.endpoint.base_url}{operation?.path || ''}
            </p>
            <div className="mt-2 flex items-center gap-2">
              <Button type="button" variant="outline" size="sm" className="h-7" disabled={checking} onClick={() => { void runCheck(); }}>
                <Activity className="mr-1.5 h-3.5 w-3.5" /> Check endpoint
              </Button>
              {check && (
                <span className={check.reachable && check.healthy !== false ? 'text-emerald-600' : 'text-destructive'}>
                  {check.reachable ? `Reachable · HTTP ${check.status}` : check.detail || 'Unreachable'}
                </span>
              )}
            </div>
          </div>

          {manifest.operations.length > 1 && (
            <div className="space-y-1.5">
              <Label htmlFor={fieldId('operation')}>Operation</Label>
              <select
                id={fieldId('operation')}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                value={operationId}
                onChange={(event) => onParameterChange('operation', event.target.value)}
              >
                {manifest.operations.map((item) => (
                  <option key={item.id} value={item.id}>{item.label || item.id}</option>
                ))}
              </select>
              {operation?.description && <p className="text-[11px] text-muted-foreground">{operation.description}</p>}
            </div>
          )}

          {operation?.response?.merge && (
            <div className="space-y-1.5">
              <Label htmlFor={fieldId('output_mode')} className="text-xs">Output</Label>
              <select
                id={fieldId('output_mode')}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                value={String(param.output_mode || DEFAULT_PLUGIN_OUTPUT_MODE)}
                onChange={(event) => onParameterChange('output_mode', event.target.value)}
              >
                {PLUGIN_OUTPUT_MODES.map((mode) => (
                  <option key={mode.value} value={mode.value}>{mode.label}</option>
                ))}
              </select>
              <p className="text-[11px] text-muted-foreground">
                {param.output_mode === 'annotations'
                  ? `Publishes only the ${manifest.name} result; later steps do not get the input text.`
                  : `Publishes the input (e.g. a transcript) with the result added under "${operation.response.merge.key}", so later steps still get the text.`}
              </p>
            </div>
          )}

          {parameters.map((parameter) => (
            <div key={parameter.name} className="space-y-1.5">
              <Label htmlFor={fieldId(parameter.name)} className="text-xs">
                {parameter.label || parameter.name}
                {isRequiredParameter(parameter) && <span className="text-destructive"> *</span>}
              </Label>
              {parameter.enum?.length ? (
                <select
                  id={fieldId(parameter.name)}
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={displayValue(param[parameter.name] ?? parameter.default)}
                  onChange={(event) => onParameterChange(parameter.name, event.target.value)}
                >
                  {!isRequiredParameter(parameter) && parameter.default === undefined && <option value="">—</option>}
                  {parameter.enum.map((item) => (
                    <option key={displayValue(item)} value={displayValue(item)}>{displayValue(item)}</option>
                  ))}
                </select>
              ) : (
                <Input
                  id={fieldId(parameter.name)}
                  value={displayValue(param[parameter.name])}
                  placeholder={parameter.default !== undefined ? `Default: ${displayValue(parameter.default)}` : parameter.type || 'string'}
                  onChange={(event) => onParameterChange(parameter.name, event.target.value)}
                />
              )}
              {parameter.description && <p className="text-[11px] text-muted-foreground">{parameter.description}</p>}
            </div>
          ))}

          {(manifest.secrets || []).map((secret) => {
            const stored = configuredSecrets.has(secret.name);
            const revealed = revealedSecrets.has(secret.name);
            const tracked = secretParamKeys.includes(secret.name);
            if (!tracked) {
              return (
                <div key={secret.name} className="flex items-center justify-between gap-2 rounded-md border border-dashed p-2">
                  <div className="min-w-0 text-xs">
                    <span className="font-medium">{secret.label || secret.name}</span>
                    <span className="text-muted-foreground"> · optional</span>
                    {secret.description && <p className="text-[11px] text-muted-foreground">{secret.description}</p>}
                  </div>
                  <Button type="button" variant="outline" size="sm" className="h-7"
                    onClick={() => onAddSecret(secret.name)}>
                    Add
                  </Button>
                </div>
              );
            }
            return (
              <div key={secret.name} className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <Label htmlFor={fieldId(secret.name)} className="text-xs">
                    {secret.label || secret.name}
                    {secret.required !== false && <span className="text-destructive"> *</span>}
                  </Label>
                  {stored && <Badge variant="outline" className="text-[10px]">Stored securely</Badge>}
                </div>
                <div className="relative">
                  <Input
                    id={fieldId(secret.name)}
                    type={revealed ? 'text' : 'password'}
                    autoComplete="off"
                    value={secretDrafts[secret.name] || ''}
                    placeholder={stored ? 'Stored securely — enter to replace' : 'Enter secret value'}
                    onChange={(event) => onParameterChange(secret.name, event.target.value)}
                    onBlur={() => onSecretCommit(secret.name)}
                    className="pr-9"
                  />
                  <Button type="button" variant="ghost" size="icon" className="absolute right-0 top-0 h-9 w-9"
                    aria-label={revealed ? `Hide ${secret.name}` : `Show ${secret.name}`}
                    onClick={() => onToggleSecret(secret.name)}>
                    {revealed ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </Button>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  {secret.description || 'Encrypted and injected only when the step runs.'}
                </p>
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
