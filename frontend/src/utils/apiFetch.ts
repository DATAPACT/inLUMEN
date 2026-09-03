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
  _workspaceId = workspaceId;
};

/**
 * Drop-in replacement for `fetch` that injects `Authorization: Bearer <token>`
 * when AUTH_ENABLED is true and a token is available.
 * Falls back to a plain fetch if auth is disabled or no token has been received yet.
 */
export const apiFetch = (url: string, init?: RequestInit): Promise<Response> => {
  if (!(AUTH_ENABLED && _token) && !_workspaceId) {
    return fetch(url, init);
  }
  const headers = new Headers(init?.headers);
  if (AUTH_ENABLED && _token) {
    headers.set('Authorization', `Bearer ${_token}`);
  }
  if (_workspaceId) {
    headers.set('X-InLumen-Workspace-Id', _workspaceId);
  }
  return fetch(url, { ...init, headers });
};
