import { beforeEach, describe, expect, it, vi } from 'vitest';
import { installTestStorage } from '@/test-utils/storage';
const settings = vi.hoisted(() => ({ enabled: true }));
vi.mock('@/config/auth', () => ({ get AUTH_ENABLED() { return settings.enabled; } }));
import { getWorkspaceStorage, setWorkspaceStorageScope } from './workspaceStorage';

describe('workspace browser storage', () => {
  beforeEach(() => {
    settings.enabled = true;
    installTestStorage();
    setWorkspaceStorageScope(null, null);
  });

  it('isolates accounts, workspaces, chat sessions, drafts, and LLM settings', () => {
    const keys = ['ai-flow-nodes', 'ai-flow-edges', 'chat-session-id', 'inlumen-chat-history', 'inlumen-chatbot-config-overrides', 'inlumen-selected-chatbot-config-id'];
    setWorkspaceStorageScope('alice', 'workspace-a');
    const alice = getWorkspaceStorage();
    keys.forEach(key => alice.setItem(key, 'Alice private data'));
    setWorkspaceStorageScope('bob', 'workspace-b');
    const bob = getWorkspaceStorage();
    keys.forEach(key => expect(bob.getItem(key)).toBeNull());
    bob.setItem(keys[0], 'Bob draft');
    setWorkspaceStorageScope('alice', 'workspace-other');
    expect(getWorkspaceStorage().getItem(keys[0])).toBeNull();
    setWorkspaceStorageScope('alice', 'workspace-a');
    keys.forEach(key => expect(getWorkspaceStorage().getItem(key)).toBe('Alice private data'));
  });

  it('does not adopt or delete legacy shared data for authenticated users', () => {
    window.localStorage.setItem('ai-flow-nodes', 'unknown owner');
    setWorkspaceStorageScope('alice', 'workspace-a');
    expect(getWorkspaceStorage().getItem('ai-flow-nodes')).toBeNull();
    getWorkspaceStorage().removeItem('ai-flow-nodes');
    expect(window.localStorage.getItem('ai-flow-nodes')).toBe('unknown owner');
  });

  it('preserves legacy storage only in auth-disabled mode', () => {
    window.localStorage.setItem('ai-flow-nodes', 'local draft');
    settings.enabled = false;
    setWorkspaceStorageScope(null, null);
    expect(getWorkspaceStorage().getItem('ai-flow-nodes')).toBe('local draft');
  });

  it('disables stale handles on logout and switching, even if the same user returns', () => {
    setWorkspaceStorageScope('alice', 'workspace-a');
    const old = getWorkspaceStorage();
    old.setItem('draft', 'original');
    setWorkspaceStorageScope(null, null);
    expect(getWorkspaceStorage().getItem('draft')).toBeNull();
    old.setItem('draft', 'late response');
    setWorkspaceStorageScope('alice', 'workspace-a');
    old.removeItem('draft');
    expect(old.getItem('draft')).toBeNull();
    expect(getWorkspaceStorage().getItem('draft')).toBe('original');
  });

  it('keeps handles valid across refreshes of the same session', () => {
    setWorkspaceStorageScope('alice', 'workspace-a');
    const storage = getWorkspaceStorage();
    setWorkspaceStorageScope('alice', 'workspace-a');
    storage.setItem('draft', 'valid');
    expect(storage.getItem('draft')).toBe('valid');
  });

  it('also namespaces session storage without confusing identity delimiters', () => {
    setWorkspaceStorageScope('user:a', 'b');
    getWorkspaceStorage('sessionStorage').setItem('key', 'private');
    setWorkspaceStorageScope('user', 'a:b');
    expect(getWorkspaceStorage('sessionStorage').getItem('key')).toBeNull();
  });

  it('does not crash when browser storage is unavailable', () => {
    setWorkspaceStorageScope('alice', 'workspace-a');
    Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw new Error('Blocked'); } });
    const storage = getWorkspaceStorage();
    expect(storage.getItem('draft')).toBeNull();
    expect(() => storage.setItem('draft', 'value')).not.toThrow();
    expect(() => storage.removeItem('draft')).not.toThrow();
  });
});
