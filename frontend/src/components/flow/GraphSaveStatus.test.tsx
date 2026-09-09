import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { resetPersistence } from '@/features/flow/persistenceState';
import { GraphSaveStatus } from './GraphSaveStatus';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

it('identifies the retained draft as a downloadable recovery copy while saved', async () => {
  resetPersistence();
  const container = document.createElement('div');
  const root = createRoot(container);
  const download = vi.fn();
  await act(async () => root.render(<GraphSaveStatus onDownload={vi.fn()} onReload={vi.fn()} onRetry={vi.fn()} onDownloadPrevious={download} />));
  expect(container.querySelector('[data-save-state]')?.getAttribute('data-save-state')).toBe('saved');
  const button = container.querySelector('button')!;
  expect(button.textContent).toBe('Download recovery copy');
  expect(button.title).toContain('before reloading');
  await act(async () => button.click());
  expect(download).toHaveBeenCalledOnce();
  await act(async () => root.unmount());
});
