import { apiFetch } from '@/lib/apiFetch';
import { INLUMEN_API_URL } from '@/config/api';
import type { LLMRequestConfig } from '@/features/chat/chatbotService';

/** A design that the backend built from a deployment artifact or from code. */
export type ArtifactDesign = {
  kind: 'inlumen-bundle' | 'argo-workflow' | 'code-zip';
  project: unknown;
  notes: string[];
  step_count: number;
  connection_count: number;
};

/** A ZIP of code: nothing is built until the user agrees to send these files to the model. */
export type CodeZipConsentRequest = {
  kind: 'code-zip';
  needs_model: true;
  name: string;
  files_to_send: Array<{ path: string; part: string; chars: number }>;
  files_not_sent: Array<{ path: string; size_bytes: number }>;
  total_chars: number;
};

export type ArtifactAnswer = ArtifactDesign | CodeZipConsentRequest;

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
  if (kind === 'inlumen-bundle') return 'inLUMEN bundle';
  return kind === 'argo-workflow' ? 'Argo workflow' : 'code';
}

export function needsModelConsent(answer: ArtifactAnswer): answer is CodeZipConsentRequest {
  return (answer as CodeZipConsentRequest).needs_model === true;
}

/** The question shown before any file of a code ZIP is sent to the model. */
export function codeZipConsentText(request: CodeZipConsentRequest, modelName: string): string {
  const shown = request.files_to_send.slice(0, 12).map(
    (file) => `  ${file.path}${file.part === 'whole file' ? '' : ` (${file.part})`}`,
  );
  const more = request.files_to_send.length - shown.length;
  const lines = [
    'This ZIP holds code, not an inLUMEN bundle or an Argo workflow.',
    `To build a design from it, these ${request.files_to_send.length} file(s) are sent to the model ${modelName}:`,
    '',
    ...shown,
    ...(more > 0 ? [`  and ${more} more`] : []),
    '',
  ];
  if (request.files_not_sent.length > 0) {
    lines.push(`${request.files_not_sent.length} other file(s) are not sent.`);
  }
  lines.push(
    'Of a CSV or TSV file only the first line is sent. Nothing is executed.',
    'The import replaces the current canvas.',
    '',
    'Send these files and build the design?',
  );
  return lines.join('\n');
}

type DesignOptions = { consent?: boolean; llmConfig?: LLMRequestConfig };

export async function designFromArtifact(file: File, options: DesignOptions = {}): Promise<ArtifactAnswer> {
  if (file.size >= 50 * 1024 * 1024) throw new Error('The file must be smaller than 50 MB');
  const body = new FormData();
  body.append('file', file);
  if (options.consent) {
    body.append('consent', 'true');
    body.append('llm_config', JSON.stringify(options.llmConfig ?? null));
  }
  const response = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/design-from-artifact`, { method: 'POST', body });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const details = typeof result.details === 'string' ? result.details : '';
    throw new Error(details || result.error || 'This file cannot be turned into a design');
  }
  return result as ArtifactAnswer;
}
