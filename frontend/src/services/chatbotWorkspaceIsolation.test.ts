import { beforeEach, describe, expect, it, vi } from 'vitest';
import { installTestStorage } from '@/test-utils/storage';
vi.mock('@/config/auth', () => ({ AUTH_ENABLED: true }));
const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('@/utils/apiFetch', () => ({ apiFetch: mocks.fetch }));
import { fetchChatbotConfigs, createChatbotConfig, readSelectedChatbotConfigId, writeSelectedChatbotConfigId } from './chatbotService';
import { getWorkspaceStorage, setWorkspaceStorageScope } from '@/utils/workspaceStorage';

describe('account-scoped chatbot settings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installTestStorage();
    setWorkspaceStorageScope('alice', 'workspace-a');
  });
  it('does not share the selected model configuration', () => {
    writeSelectedChatbotConfigId('alice-config');
    setWorkspaceStorageScope('bob', 'workspace-b');
    expect(readSelectedChatbotConfigId()).toBeNull();
  });
  it('makes the application config usable in each workspace without storing a secret', async () => {
    const managed = {
      id: 'application-llm', name: 'Application-provided LLM',
      provider: 'custom', baseUrl: 'https://llm.test/v1',
      model: 'chat', codegenModel: 'code', has_api_key: true,
      readOnly: true, applicationProvided: true,
    };
    mocks.fetch.mockImplementation(async () => new Response(JSON.stringify({ configs: [managed] })));
    for (const user of ['alice', 'bob']) {
      setWorkspaceStorageScope(user, `workspace-${user}`);
      const configs = await fetchChatbotConfigs();
      expect(configs).toHaveLength(1);
      expect(configs[0]).toMatchObject({
        id: 'application-llm', readOnly: true, applicationProvided: true,
        hasApiKey: true, apiKey: '', model: 'chat', codegenModel: 'code',
      });
      const overrides = JSON.parse(getWorkspaceStorage().getItem('inlumen-chatbot-config-overrides')!);
      expect(overrides['id:application-llm']).not.toHaveProperty('apiKey');
    }
    mocks.fetch.mockResolvedValueOnce(new Response(JSON.stringify({ configs: [] })));
    expect(await fetchChatbotConfigs()).toEqual([]);
  });
  it('discards an old user’s successful cache response after account switching', async () => {
    let resolve!: (response: Response) => void;
    mocks.fetch.mockReturnValue(new Promise<Response>(done => { resolve = done; }));
    const request = fetchChatbotConfigs();
    setWorkspaceStorageScope('bob', 'workspace-b');
    resolve(new Response(JSON.stringify({ configs: [{ id: 'alice-config', name: 'Alice private config' }] })));
    expect(await request).toEqual([]);
    expect(getWorkspaceStorage().getItem('inlumen-chatbot-remote-config-cache')).toBeNull();
  });
  it('does not save another user’s API key via the offline fallback', async () => {
    let reject!: (reason: Error) => void;
    mocks.fetch.mockReturnValue(new Promise<Response>((_, fail) => { reject = fail; }));
    const request = createChatbotConfig({ name: 'Alice', provider: 'custom', model: 'model', baseUrl: 'https://example.test', apiKey: 'alice-secret' });
    setWorkspaceStorageScope('bob', 'workspace-b');
    reject(new Error('Offline'));
    expect(await request).toBeNull();
    expect(getWorkspaceStorage().getItem('inlumen-chatbot-local-configs')).toBeNull();
    expect(getWorkspaceStorage().getItem('inlumen-chatbot-config-overrides')).toBeNull();
  });
});
