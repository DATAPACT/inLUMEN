import { useEffect, useMemo, useState } from 'react';
import { ChatbotConfigForm } from './ChatbotConfigForm';
import { Button } from './ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from './ui/dialog';
import { INLUMEN_API_URL } from '@/config/api';
import { apiFetch } from '@/utils/apiFetch';
import { APPLICATION_LLM_ID, getDefaultChatbotConfig, type ChatbotConfig } from '@/services/chatbotService';

interface SharedSettings {
  config: (ChatbotConfig & { has_api_key?: boolean }) | null;
  enabled: boolean;
  revision: number;
}

const url = `${INLUMEN_API_URL}/api/admin/application-llm`;

async function readResponse(response: Response): Promise<SharedSettings> {
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || payload.error || 'Could not save shared LLM settings.');
  return payload as SharedSettings;
}

export function ApplicationLLMSettings({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [settings, setSettings] = useState<SharedSettings | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState('');
  const config: ChatbotConfig = useMemo(() => ({
    ...(settings?.config || getDefaultChatbotConfig()),
    id: APPLICATION_LLM_ID,
    name: 'Application-provided LLM',
    hasApiKey: settings?.config?.has_api_key ?? false,
    apiKey: '',
  }), [settings]);

  useEffect(() => {
    const controller = new AbortController();
    apiFetch(url, { signal: controller.signal })
      .then(readResponse)
      .then(value => {
        if (controller.signal.aborted) return;
        setSettings(value);
        setEnabled(value.enabled);
      })
      .catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Could not load shared LLM settings.'); });
    return () => controller.abort();
  }, []);

  if (!settings) return (
    <Dialog open onOpenChange={open => { if (!open) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Manage shared LLM</DialogTitle>
          <DialogDescription>{error || 'Loading shared configuration…'}</DialogDescription>
        </DialogHeader>
        {error && <Button onClick={onClose}>Close</Button>}
      </DialogContent>
    </Dialog>
  );

  return <ChatbotConfigForm
    isOpen onClose={onClose} initialConfig={config}
    sharedAccess={{ enabled, onChange: setEnabled }}
    saveConfig={async next => {
      // Never use personal-config helpers: their offline fallback must not save
      // an administrator's shared key or imply that an unsuccessful save worked.
      const { apiKey, ...metadata } = next;
      const saved = await readResponse(await apiFetch(url, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: { ...metadata, api_key: apiKey || '' }, enabled, revision: settings.revision }),
      }));
      if (!saved.config) throw new Error('Server did not return the shared configuration.');
      return { ...saved.config, apiKey: '', hasApiKey: saved.config.has_api_key };
    }}
    onConfigSaved={() => onSaved()}
  />;
}
