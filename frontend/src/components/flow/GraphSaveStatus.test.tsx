import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { resetPersistence } from '@/features/flow/persistenceState';
import { GraphSaveStatus } from './GraphSaveStatus';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

it('shows no recovery button when the pipeline is saved', async () => {
  resetPersistence();
  const container = document.createElement('div');
  const root = createRoot(container);
  await act(async () => root.render(<GraphSaveStatus onDownload={vi.fn()} onReload={vi.fn()} onRetry={vi.fn()} />));
  expect(container.querySelector('[data-save-state]')?.getAttribute('data-save-state')).toBe('saved');
  expect(container.querySelector('button')).toBeNull();
  expect(container.textContent).toBe('');
  await act(async () => root.unmount());
});
