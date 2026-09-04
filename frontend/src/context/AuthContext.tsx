import React, { useEffect, useRef, useState } from 'react';
import Keycloak from 'keycloak-js';
import {
  AUTH_ENABLED,
  KEYCLOAK_CLIENT_ID,
  KEYCLOAK_REALM,
  KEYCLOAK_URL,
  TOOLBOX_ORIGIN,
} from '@/config/auth';
import { INLUMEN_API_URL } from '@/config/api';
import { setActiveWorkspaceId, setAuthToken } from '@/utils/apiFetch';
import { SessionInitializationError, sessionResponseError } from '@/utils/sessionError';
import { AuthSessionContext, LOCAL_SESSION, type AuthSession } from './authSession';
import { setWorkspaceStorageScope } from '@/utils/workspaceStorage';

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [ready, setReady] = useState(!AUTH_ENABLED);
  const [error, setError] = useState<string | null>(null);
  const [session, setSession] = useState<AuthSession | null>(AUTH_ENABLED ? null : LOCAL_SESSION);
  const [signingOut, setSigningOut] = useState(false);
  const logoutRef = useRef<(() => Promise<void>) | null>(null);
  const embedded = window.self !== window.top;

  useEffect(() => {
    if (!AUTH_ENABLED) return;

    let mounted = true;
    let loggingOut = false;
    let refreshTimer: number | undefined;
    let bootstrapSequence = 0;

    const clearSession = () => {
      bootstrapSequence += 1;
      setAuthToken(null);
      setActiveWorkspaceId(null);
      setWorkspaceStorageScope(null, null);
      if (mounted) { setSession(null); setReady(false); }
    };

    const markReady = () => {
      if (mounted) setReady(true);
    };

    const bootstrapSession = async (token: string) => {
      if (!mounted || loggingOut) return;
      const sequence = ++bootstrapSequence;
      let response: Response;
      try {
        // Validate this token before replacing the currently active identity.
        response = await fetch(`${INLUMEN_API_URL}/api/session`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      } catch {
        if (!mounted || loggingOut || sequence !== bootstrapSequence) return;
        throw new SessionInitializationError('Signed in, but the application server could not be reached. Please check your connection and try again.');
      }
      if (!mounted || loggingOut || sequence !== bootstrapSequence) return;
      if (!response.ok) {
        const error = await sessionResponseError(response);
        if (!mounted || loggingOut || sequence !== bootstrapSequence) return;
        throw error;
      }
      const payload = await response.json().catch(() => null);
      if (!mounted || loggingOut || sequence !== bootstrapSequence) return;
      const workspaceId = String(payload?.active_workspace_id || '').trim();
      if (!workspaceId || !payload?.user?.id || !Array.isArray(payload?.workspaces)) {
        throw new SessionInitializationError('Signed in, but the server returned an invalid workspace session. Contact your administrator.');
      }
      if (!mounted || loggingOut) return;
      setAuthToken(token);
      setActiveWorkspaceId(workspaceId);
      setWorkspaceStorageScope(payload.user.id, workspaceId);
      setSession(payload as AuthSession);
    };

    if (window.self !== window.top) {
      const handleMessage = (event: MessageEvent) => {
        if (event.source !== window.parent) return;
        if (TOOLBOX_ORIGIN !== '*' && event.origin !== TOOLBOX_ORIGIN) return;
        const data = event.data;
        if (data?.type === 'SSO_TOKEN' && typeof data.token === 'string') {
          void bootstrapSession(data.token)
            .then(markReady)
            .catch((err) => {
              console.error('Workspace initialization failed', err);
              setAuthToken(null);
              setActiveWorkspaceId(null);
              setWorkspaceStorageScope(null, null);
              if (mounted) setError(err instanceof SessionInitializationError ? err.message : 'Signed in, but the workspace could not be initialized.');
            });
        }
      };

      window.addEventListener('message', handleMessage);

      // Notify parent that the iframe is ready. This triggers toolbox-ui's SSOTokenBridge.
      window.parent.postMessage({ type: 'IFRAME_READY' }, TOOLBOX_ORIGIN === '*' ? '*' : TOOLBOX_ORIGIN);

      return () => {
        mounted = false;
        clearSession();
        window.removeEventListener('message', handleMessage);
      };
    }

    const keycloak = new Keycloak({
      url: KEYCLOAK_URL,
      realm: KEYCLOAK_REALM,
      clientId: KEYCLOAK_CLIENT_ID,
    });

    const syncToken = async () => {
      if (!keycloak.token) {
        clearSession();
        return;
      }
      await bootstrapSession(keycloak.token);
    };
    const refreshToken = async () => {
      if (!mounted || loggingOut) return;
      try {
        await keycloak.updateToken(60);
        await syncToken();
      } catch {
        if (!mounted || loggingOut) return;
        clearSession();
        await keycloak.login();
      }
    };

    keycloak.onTokenExpired = () => {
      void refreshToken();
    };
    keycloak.onAuthLogout = () => {
      if (!mounted || loggingOut) return;
      clearSession();
      void keycloak.login();
    };

    logoutRef.current = async () => {
      if (loggingOut) return;
      loggingOut = true;
      if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
      setSigningOut(true);
      setSession(null);
      setWorkspaceStorageScope(null, null);
      setAuthToken(null);
      setActiveWorkspaceId(null);
      try {
        // The adapter supplies the ID-token hint and ends the Keycloak SSO session.
        await keycloak.logout({ redirectUri: `${window.location.origin}/` });
      } catch {
        if (mounted) setError('Sign out could not be completed with Keycloak. Reload to try again; your identity-provider session may still be active.');
      }
    };

    keycloak
      .init({
        onLoad: 'login-required',
        pkceMethod: 'S256',
        checkLoginIframe: false,
      })
      .then(async (authenticated) => {
        if (!mounted || loggingOut) return;
        if (!authenticated) {
          return keycloak.login();
        }
        await syncToken();
        if (!mounted || loggingOut) return;
        refreshTimer = window.setInterval(() => {
          void refreshToken();
        }, 30000);
        markReady();
      })
      .catch((err) => {
        console.error('Keycloak initialization failed', err);
        setAuthToken(null);
        setActiveWorkspaceId(null);
        setWorkspaceStorageScope(null, null);
        if (mounted) {
          setError(err instanceof SessionInitializationError ? err.message : 'Keycloak sign-in could not be completed. Please try again or contact your administrator.');
        }
      });

    return () => {
      mounted = false;
      logoutRef.current = null;
      if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
      setAuthToken(null);
      setActiveWorkspaceId(null);
      setWorkspaceStorageScope(null, null);
    };
  }, []);

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 text-center text-sm text-destructive">
        {error}
      </div>
    );
  }

  if (!ready || (AUTH_ENABLED && !session && !signingOut)) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 text-sm text-muted-foreground">
        Signing in...
      </div>
    );
  }

  if (signingOut) {
    return <div role="status" className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">Signing out…</div>;
  }

  return (
    <AuthSessionContext.Provider value={{ session, authEnabled: AUTH_ENABLED, embedded, signOut: async () => { await logoutRef.current?.(); } }}>
      <React.Fragment key={`${session?.user.id}:${session?.active_workspace_id}`}>{children}</React.Fragment>
    </AuthSessionContext.Provider>
  );
};
