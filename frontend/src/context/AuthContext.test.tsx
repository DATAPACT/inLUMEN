import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider } from './AuthContext';
import { useAuthSession } from './authSession';

const mocks = vi.hoisted(() => ({
  enabled: true,
  init: vi.fn(), logout: vi.fn(), login: vi.fn(), updateToken: vi.fn(),
  apiFetch: vi.fn(), setAuthToken: vi.fn(), setActiveWorkspaceId: vi.fn(),
}));
vi.mock('@/config/auth', () => ({
  get AUTH_ENABLED() { return mocks.enabled; },
  KEYCLOAK_URL: 'https://sso.example.test', KEYCLOAK_REALM: 'test',
  KEYCLOAK_CLIENT_ID: 'frontend', TOOLBOX_ORIGIN: 'https://host.example.test',
}));
vi.mock('@/utils/apiFetch', () => mocks);
vi.mock('keycloak-js', () => ({ default: function () {
  return { token: 'test-token', init: mocks.init, logout: mocks.logout,
    login: mocks.login, updateToken: mocks.updateToken };
} }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function Harness() {
  const { session, signOut } = useAuthSession();
  return <><p>{session?.user.display_name}</p><p>{session?.active_workspace_id}</p><button onClick={() => void signOut()}>Sign out</button></>;
}

describe('account session lifecycle', () => {
  let root: Root;
  let container: HTMLDivElement;
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('fetch', mocks.apiFetch);
    mocks.enabled = true;
    mocks.init.mockResolvedValue(true);
    mocks.logout.mockResolvedValue(undefined);
    mocks.apiFetch.mockResolvedValue(new Response(JSON.stringify({
      user: { id: 'user-a', subject: 'subject-a', display_name: 'Alice' },
      active_workspace_id: 'workspace-a', workspaces: [{ id: 'workspace-a', name: 'Personal workspace', role: 'owner' }],
    })));
    container = document.createElement('div');
    root = createRoot(container);
  });
  afterEach(async () => { await act(async () => root.unmount()); vi.unstubAllGlobals(); });

  it('exposes the server-resolved identity and ends SSO on sign out', async () => {
    await act(async () => root.render(<AuthProvider><Harness /></AuthProvider>));
    expect(container.textContent).toContain('Alice');
    expect(container.textContent).toContain('workspace-a');
    await act(async () => container.querySelector('button')?.click());
    expect(mocks.logout).toHaveBeenCalledWith({ redirectUri: `${window.location.origin}/` });
    expect(mocks.setAuthToken).toHaveBeenLastCalledWith(null);
    expect(mocks.setActiveWorkspaceId).toHaveBeenLastCalledWith(null);
    expect(container.textContent).toBe('Signing out…');
    expect(container.textContent).not.toContain('Alice');
  });

  it('preserves no-auth local mode without contacting Keycloak or the backend', async () => {
    mocks.enabled = false;
    await act(async () => root.render(<AuthProvider><Harness /></AuthProvider>));
    expect(container.textContent).toContain('Local user');
    expect(container.textContent).toContain('local-workspace');
    expect(mocks.init).not.toHaveBeenCalled();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it('reports failed remote logout without restoring the private workspace', async () => {
    mocks.logout.mockRejectedValue(new Error('Network error'));
    await act(async () => root.render(<AuthProvider><Harness /></AuthProvider>));
    await act(async () => container.querySelector('button')?.click());
    expect(container.textContent).toContain('Sign out could not be completed');
    expect(container.textContent).not.toContain('Alice');
  });

  it('remounts private UI on workspace/account changes but not token refresh', async () => {
    vi.stubGlobal('self', {});
    let mounts = 0;
    function PrivateUI() {
      const { session } = useAuthSession();
      const [instance] = React.useState(() => ++mounts);
      return <p>{session?.user.id}:{session?.active_workspace_id}:{instance}</p>;
    }
    await act(async () => root.render(<AuthProvider><PrivateUI /></AuthProvider>));
    const sendSession = async (user: string, workspace: string) => {
      mocks.apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({
        user: { id: user, subject: user, display_name: user },
        active_workspace_id: workspace, workspaces: [{ id: workspace, name: workspace, role: 'owner' }],
      })));
      await act(async () => window.dispatchEvent(new MessageEvent('message', {
        source: window.parent, origin: 'https://host.example.test', data: { type: 'SSO_TOKEN', token: user },
      })));
    };
    await sendSession('alice', 'workspace-a');
    expect(container.textContent).toBe('alice:workspace-a:1');
    await sendSession('alice', 'workspace-a');
    expect(container.textContent).toBe('alice:workspace-a:1');
    await sendSession('alice', 'workspace-other');
    expect(container.textContent).toBe('alice:workspace-other:2');
    await sendSession('bob', 'workspace-b');
    expect(container.textContent).toBe('bob:workspace-b:3');
  });

  it('does not let an older bootstrap replace a newer authenticated identity', async () => {
    vi.stubGlobal('self', {});
    let finishOld!: (response: Response) => void;
    mocks.apiFetch.mockReturnValueOnce(new Promise<Response>(resolve => { finishOld = resolve; }));
    mocks.apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({
      user: { id: 'bob', subject: 'bob', display_name: 'Bob' },
      active_workspace_id: 'workspace-b', workspaces: [],
    })));
    await act(async () => root.render(<AuthProvider><Harness /></AuthProvider>));
    const message = (token: string) => window.dispatchEvent(new MessageEvent('message', {
      source: window.parent, origin: 'https://host.example.test', data: { type: 'SSO_TOKEN', token },
    }));
    await act(async () => { message('alice-token'); message('bob-token'); });
    await act(async () => finishOld(new Response(JSON.stringify({
      user: { id: 'alice', subject: 'alice', display_name: 'Alice' },
      active_workspace_id: 'workspace-a', workspaces: [],
    }))));
    expect(container.textContent).toContain('Bob');
    expect(container.textContent).not.toContain('Alice');
    expect(mocks.setAuthToken).toHaveBeenLastCalledWith('bob-token');
  });
});
