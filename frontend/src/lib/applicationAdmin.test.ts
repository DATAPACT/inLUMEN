import { describe, expect, it } from 'vitest';
import { LOCAL_SESSION, type AuthSession } from '@/context/authSession';
import { canManageApplicationLLM } from './applicationAdmin';

const adminSession: AuthSession = {
  ...LOCAL_SESSION,
  user: { id: 'admin', subject: 'admin', display_name: 'Admin' },
  active_workspace_id: 'workspace',
  workspaces: [{ id: 'workspace', name: 'Workspace', role: 'owner' }],
  is_application_admin: true,
};

describe('canManageApplicationLLM', () => {
  it('does not allow local no-auth sessions even when they have admin-like development privileges', () => {
    expect(canManageApplicationLLM(false, LOCAL_SESSION)).toBe(false);
  });

  it('requires both authentication and application-admin role', () => {
    expect(canManageApplicationLLM(true, adminSession)).toBe(true);
    expect(canManageApplicationLLM(true, { ...adminSession, is_application_admin: false })).toBe(false);
    expect(canManageApplicationLLM(true, null)).toBe(false);
  });
});
