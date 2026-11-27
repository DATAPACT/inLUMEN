import { apiFetch } from '@/lib/apiFetch';
import { INLUMEN_API_URL } from '@/config/api';

/** A design that the backend built from a deployment artifact. */
export type ArtifactDesign = {
  kind: 'inlumen-bundle' | 'argo-workflow';
  project: unknown;
  notes: string[];
  step_count: number;
  connection_count: number;
};

export const IMPORT_ACCEPT = '.json,.zip,.yaml,.yml';

const BUNDLE_SCHEMA_PREFIX = 'inlumen.deployment-bundle@';

/** True for a parsed JSON file that is a bundle manifest or an Argo workflow, not Project JSON. */
export function isArtifactJson(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as { schema_version?: unknown; kind?: unknown; apiVersion?: unknown };
  return String(candidate.schema_version || '').startsWith(BUNDLE_SCHEMA_PREFIX)
    || String(candidate.apiVersion || '').startsWith('argoproj.io/');
}

/** True when the file must go to the backend: a ZIP or a YAML file. */
export function isArtifactFile(file: { name: string }): boolean {
  return /\.(zip|ya?ml)$/i.test(file.name);
}

export function artifactKindLabel(kind: ArtifactDesign['kind']): string {
  return kind === 'inlumen-bundle' ? 'inLUMEN bundle' : 'Argo workflow';
}

export async function designFromArtifact(file: File): Promise<ArtifactDesign> {
  if (file.size >= 50 * 1024 * 1024) throw new Error('The file must be smaller than 50 MB');
  const body = new FormData();
  body.append('file', file);
  const response = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/design-from-artifact`, { method: 'POST', body });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.details || result.error || 'This file cannot be turned into a design');
  return result as ArtifactDesign;
}
