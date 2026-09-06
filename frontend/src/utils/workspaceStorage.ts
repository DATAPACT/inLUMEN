import { reportPersistenceError } from '@/features/flow/persistenceState';
import { AUTH_ENABLED } from '@/config/auth';

// Identity comes only from the validated /api/session response, never storage.
let scope: string | null = AUTH_ENABLED ? null : '';
let generation = 0;
const protectedDrafts = new Set<string>();

export const setWorkspaceStorageScope = (userId: string | null, workspaceId: string | null) => {
  const next = !AUTH_ENABLED ? '' : userId && workspaceId
    ? `inlumen:v1:${encodeURIComponent(userId)}:${encodeURIComponent(workspaceId)}:`
    : null;
  if (next !== scope) { scope = next; generation += 1; }
};

export const captureWorkspaceGeneration = () => generation;
export const isWorkspaceGenerationCurrent = (value: number) => value === generation;

export type WorkspaceStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export const getWorkspaceStorage = (kind: 'localStorage' | 'sessionStorage' = 'localStorage'): WorkspaceStorage => {
  const prefix = scope;
  const captured = generation;
  const available = () => prefix !== null && captured === generation && typeof window !== 'undefined';
  // Handles are bound to one session. Late callbacks from an old session cannot
  // read/write the next user's data (or re-create state after logout).
  return {
    getItem(key) {
      if (!available()) return null;
      try { return window[kind].getItem(prefix + key); } catch { return null; }
    },
    setItem(key, value) {
      if (!available() || protectedDrafts.has(prefix + key)) return;
      try { window[kind].setItem(prefix + key, value); } catch { queueMicrotask(() => reportPersistenceError(new Error("Browser draft storage is full or unavailable. Download your draft to keep a copy."))); }
    },
    removeItem(key) {
      if (!available()) return;
      try { window[kind].removeItem(prefix + key); } catch { /* Storage may be unavailable. */ }
    },
  };
};

/** Invalid drafts are retained for recovery; they must never crash the canvas. */
export function readStoredArray<T>(storage: WorkspaceStorage, key: string, valid: (value: unknown) => value is T): T[] {
  const raw = storage.getItem(key);
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || !parsed.every(valid)) throw new Error('Invalid draft');
    return parsed;
  } catch {
    protectedDrafts.add((scope || '') + key);
    storage.setItem(key + ':recovery', raw);
    queueMicrotask(() => reportPersistenceError(new Error('A browser draft could not be read. The original draft has been retained; reload the saved graph to recover.')));
    return [];
  }
}

export function releaseDraftProtection() {
  for (const key of protectedDrafts) if (key.startsWith(scope || '')) protectedDrafts.delete(key);
}
