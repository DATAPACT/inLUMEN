import { AUTH_ENABLED } from '@/config/auth';

// Identity comes only from the validated /api/session response, never storage.
let scope: string | null = AUTH_ENABLED ? null : '';
let generation = 0;

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
      if (!available()) return;
      try { window[kind].setItem(prefix + key, value); } catch { /* Storage may be unavailable/full. */ }
    },
    removeItem(key) {
      if (!available()) return;
      try { window[kind].removeItem(prefix + key); } catch { /* Storage may be unavailable. */ }
    },
  };
};
