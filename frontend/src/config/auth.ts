/**
 * Master switch for Keycloak authentication.
 * Controlled via AUTH_ENABLED in the root .env; Docker Compose exposes the same
 * value to Vite as VITE_AUTH_ENABLED. Standalone frontend/dev setups can still
 * set VITE_AUTH_ENABLED directly in frontend/.env.
 *
 * true  — frontend listens for postMessage tokens and injects Authorization headers;
 *         backend validates JWT on every request (requires AUTH_ENABLED=true env var too).
 * false — auth is completely disabled; no headers are sent, no listeners are registered.
 */
export const AUTH_ENABLED = import.meta.env.VITE_AUTH_ENABLED === 'true';

export const KEYCLOAK_URL =
  ((import.meta.env.VITE_KEYCLOAK_URL as string) || 'http://localhost:8081').replace(/\/$/, '');
export const KEYCLOAK_REALM = (import.meta.env.VITE_KEYCLOAK_REALM as string) || 'inlumen';
export const KEYCLOAK_CLIENT_ID =
  (import.meta.env.VITE_KEYCLOAK_CLIENT_ID as string) || 'inlumen-frontend';

const configuredToolboxOrigin = ((import.meta.env.VITE_TOOLBOX_ORIGIN as string) || '').trim();

const inferParentOrigin = (): string => {
  if (typeof document === 'undefined' || !document.referrer) {
    return '';
  }

  try {
    return new URL(document.referrer).origin;
  } catch {
    return '';
  }
};

/**
 * Allowed origin for incoming postMessage events from toolbox-ui.
 * Prefer inferring the parent origin from the iframe referrer so normal Keycloak
 * SSO setup does not need a frontend env var. VITE_TOOLBOX_ORIGIN remains a
 * backwards-compatible override for deployments that hide referrers.
 */
export const TOOLBOX_ORIGIN = configuredToolboxOrigin || inferParentOrigin() || '*';
