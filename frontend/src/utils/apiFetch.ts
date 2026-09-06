import { graphWrite, resetPersistence } from '@/features/flow/persistenceState';
import { AUTH_ENABLED } from '@/config/auth';

/**
 * Module-level token store.
 * Updated by AuthContext when an SSO_TOKEN postMessage is received from toolbox-ui.
 */
let _token: string | null = null;
let _workspaceId: string | null = null;

export const setAuthToken = (token: string | null): void => {
  _token = token;
};

export const setActiveWorkspaceId = (workspaceId: string | null): void => {
  if (_workspaceId !== workspaceId) resetPersistence();
  _workspaceId = workspaceId;
};

/**
 * Drop-in replacement for `fetch` that injects `Authorization: Bearer <token>`
 * when AUTH_ENABLED is true and a token is available.
 * Falls back to a plain fetch if auth is disabled or no token has been received yet.
 */
export const apiFetch = (url: string, init?: RequestInit): Promise<Response> => {
  const headers = new Headers(init?.headers);
  if (AUTH_ENABLED && _token) headers.set('Authorization', `Bearer ${_token}`);
  if (_workspaceId) headers.set('X-InLumen-Workspace-Id', _workspaceId);
  const path = new URL(url, window.location.origin).pathname;
  const mutation = !['GET', 'HEAD', 'OPTIONS'].includes((init?.method || 'GET').toUpperCase());
  const graph = /^\/api\/(graph\/|pipeline\/(graph|history\/restore|versions))/.test(path);
  const send = () => fetch(url, (AUTH_ENABLED && _token) || _workspaceId || headers.has("If-Match") ? { ...init, headers } : init);
  if (mutation && graph) return graphWrite((revision) => {
    if (revision) headers.set('If-Match', revision);
    return send();
  });
  return send();
};
