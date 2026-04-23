import React, { useEffect } from 'react';
import { AUTH_ENABLED, TOOLBOX_ORIGIN } from '@/config/auth';
import { setAuthToken } from '@/utils/apiFetch';

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  useEffect(() => {
    if (!AUTH_ENABLED) return;

    // Skip when not running inside an iframe
    if (window.self === window.top) return;

    const handleMessage = (event: MessageEvent) => {
      if (event.source !== window.parent) return;
      if (TOOLBOX_ORIGIN !== '*' && event.origin !== TOOLBOX_ORIGIN) return;
      const data = event.data;
      if (data?.type === 'SSO_TOKEN' && typeof data.token === 'string') {
        setAuthToken(data.token);
      }
    };

    window.addEventListener('message', handleMessage);

    // Notify parent that the iframe is ready. This triggers toolbox-ui's SSOTokenBridge.
    window.parent.postMessage({ type: 'IFRAME_READY' }, TOOLBOX_ORIGIN === '*' ? '*' : TOOLBOX_ORIGIN);

    return () => window.removeEventListener('message', handleMessage);
  }, []);

  return <>{children}</>;
};
