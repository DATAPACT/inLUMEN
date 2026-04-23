/**
 * Master switch for Keycloak authentication.
 * Controlled via AUTH_ENABLED in the root .env; Docker Compose exposes the same
 * value to Vite as VITE_AUTH_ENABLED.
 *
 * true  — frontend listens for postMessage tokens and injects Authorization headers;
 *         backend validates JWT on every request (requires AUTH_ENABLED=true env var too).
 * false — auth is completely disabled; no headers are sent, no listeners are registered.
 */
export const AUTH_ENABLED = import.meta.env.VITE_AUTH_ENABLED === 'true';

/**
 * Allowed origin for incoming postMessage events from toolbox-ui.
 * Set to the exact origin of toolbox-ui in production (e.g. "https://toolbox.example.com").
 * Use '*' only in local development.
 */
export const TOOLBOX_ORIGIN = '*';
