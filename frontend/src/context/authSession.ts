import { createContext, useContext } from 'react';

export interface AuthSession {
  user: { id: string; subject: string; display_name: string };
  active_workspace_id: string;
  workspaces: Array<{ id: string; name: string; role: string }>;
}

export const LOCAL_SESSION: AuthSession = {
  user: { id: 'local-user', subject: 'local-user', display_name: 'Local user' },
  active_workspace_id: 'local-workspace',
  workspaces: [{ id: 'local-workspace', name: 'Local workspace', role: 'owner' }],
};

export interface AuthSessionContextValue {
  session: AuthSession | null;
  authEnabled: boolean;
  embedded: boolean;
  signOut: () => Promise<void>;
}

export const AuthSessionContext = createContext<AuthSessionContextValue | null>(null);

export const useAuthSession = () => {
  const context = useContext(AuthSessionContext);
  if (!context) throw new Error('useAuthSession requires AuthProvider');
  return context;
};
