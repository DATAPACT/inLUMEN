import React, { useEffect, useState } from 'react';
import Keycloak from 'keycloak-js';
import {
  AUTH_ENABLED,
  KEYCLOAK_CLIENT_ID,
  KEYCLOAK_REALM,
  KEYCLOAK_URL,
  TOOLBOX_ORIGIN,
} from '@/config/auth';
import { INLUMEN_API_URL } from '@/config/api';
import { apiFetch, setActiveWorkspaceId, setAuthToken } from '@/utils/apiFetch';

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [ready, setReady] = useState(!AUTH_ENABLED);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!AUTH_ENABLED) return;

    let mounted = true;
    let refreshTimer: number | undefined;

    const markReady = () => {
      if (mounted) setReady(true);
    };

    const bootstrapSession = async (token: string) => {
      setAuthToken(token);
      setActiveWorkspaceId(null);
      const response = await apiFetch(`${INLUMEN_API_URL}/api/session`);
      if (!response.ok) {
        throw new Error(`Workspace bootstrap failed (${response.status}).`);
      }
      const payload = await response.json();
      const workspaceId = String(payload?.active_workspace_id || '').trim();
      if (!workspaceId) {
        throw new Error('Workspace bootstrap returned no active workspace.');
      }
      setActiveWorkspaceId(workspaceId);
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
              if (mounted) setError('Signed in, but the workspace could not be initialized.');
            });
        }
      };

      window.addEventListener('message', handleMessage);

      // Notify parent that the iframe is ready. This triggers toolbox-ui's SSOTokenBridge.
      window.parent.postMessage({ type: 'IFRAME_READY' }, TOOLBOX_ORIGIN === '*' ? '*' : TOOLBOX_ORIGIN);

      return () => {
        mounted = false;
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
        setAuthToken(null);
        setActiveWorkspaceId(null);
        return;
      }
      await bootstrapSession(keycloak.token);
    };
    const refreshToken = async () => {
      try {
        await keycloak.updateToken(60);
        await syncToken();
      } catch {
        setAuthToken(null);
        await keycloak.login();
      }
    };

    keycloak.onTokenExpired = () => {
      void refreshToken();
    };
    keycloak.onAuthLogout = () => {
      setAuthToken(null);
      void keycloak.login();
    };

    keycloak
      .init({
        onLoad: 'login-required',
        pkceMethod: 'S256',
        checkLoginIframe: false,
      })
      .then(async (authenticated) => {
        if (!mounted) return;
        if (!authenticated) {
          return keycloak.login();
        }
        await syncToken();
        refreshTimer = window.setInterval(() => {
          void refreshToken();
        }, 30000);
        markReady();
      })
      .catch((err) => {
        console.error('Keycloak initialization failed', err);
        if (mounted) {
          setError('Keycloak sign-in failed. Check that the inlumen realm and frontend client exist.');
        }
      });

    return () => {
      mounted = false;
      if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
      setAuthToken(null);
      setActiveWorkspaceId(null);
    };
  }, []);

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 text-center text-sm text-destructive">
        {error}
      </div>
    );
  }

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 text-sm text-muted-foreground">
        Signing in...
      </div>
    );
  }

  return <>{children}</>;
};
