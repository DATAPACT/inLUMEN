import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import { AccountMenu } from './AccountMenu';
import { AuthSessionContext, LOCAL_SESSION } from '@/context/authSession';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe('account menu', () => {
  it.each([
    { authEnabled: true, embedded: false, logout: true },
    { authEnabled: false, embedded: false, logout: false },
    { authEnabled: true, embedded: true, logout: false },
  ])('shows identity and appropriate logout controls: %j', async ({ authEnabled, embedded, logout }) => {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const signOut = vi.fn().mockResolvedValue(undefined);
    try {
      await act(async () => root.render(
        <AuthSessionContext.Provider value={{ session: LOCAL_SESSION, authEnabled, embedded, signOut }}>
          <AccountMenu />
        </AuthSessionContext.Provider>,
      ));
      expect(container.querySelector('button')?.getAttribute('aria-label')).toBe('Account: Local user');
      await act(async () => {
        container.querySelector('button')?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
      });
      expect(document.body.textContent).toContain('Local workspace');
      expect(document.body.textContent).toContain('local-user');
      const signOutItem = Array.from(document.querySelectorAll('[role="menuitem"]')).find(item => item.textContent === 'Sign out');
      expect(Boolean(signOutItem)).toBe(logout);
      if (signOutItem) {
        await act(async () => (signOutItem as HTMLElement).click());
        expect(signOut).toHaveBeenCalledOnce();
      }
      if (!authEnabled) expect(document.body.textContent).toContain('Authentication disabled');
      if (embedded) expect(document.body.textContent).toContain('host application');
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });
});
