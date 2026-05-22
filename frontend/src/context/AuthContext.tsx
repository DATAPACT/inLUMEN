import React, { useEffect, useState } from 'react';
import Keycloak from 'keycloak-js';
import {
  AUTH_ENABLED,
  KEYCLOAK_CLIENT_ID,
  KEYCLOAK_REALM,
  KEYCLOAK_URL,
  TOOLBOX_ORIGIN,
} from '@/config/auth';
import { setAuthToken } from '@/utils/apiFetch';

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

    if (window.self !== window.top) {
      const handleMessage = (event: MessageEvent) => {
        if (event.source !== window.parent) return;
        if (TOOLBOX_ORIGIN !== '*' && event.origin !== TOOLBOX_ORIGIN) return;
        const data = event.data;
        if (data?.type === 'SSO_TOKEN' && typeof data.token === 'string') {
          setAuthToken(data.token);
          markReady();
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

    const syncToken = () => setAuthToken(keycloak.token ?? null);
    const refreshToken = async () => {
      try {
        await keycloak.updateToken(60);
        syncToken();
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
      .then((authenticated) => {
        if (!mounted) return;
        if (!authenticated) {
          return keycloak.login();
        }
        syncToken();
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
