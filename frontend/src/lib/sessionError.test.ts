import { describe, expect, it } from 'vitest';
import { SessionInitializationError, sessionResponseError } from './sessionError';

describe('session initialization errors', () => {
  it('identifies clock skew without blaming client configuration', async () => {
    const error = await sessionResponseError(new Response(JSON.stringify({ code: 'token_not_yet_valid' }), { status: 401 }));
    expect(error).toBeInstanceOf(SessionInitializationError);
    expect(error.message).toContain('clock synchronization');
  });
  it('does not display arbitrary backend details', async () => {
    const error = await sessionResponseError(new Response(JSON.stringify({ detail: 'sensitive upstream details' }), { status: 401 }));
    expect(error.message).toContain('server rejected your session');
    expect(error.message).not.toContain('sensitive');
  });
  it('handles non-JSON service failures', async () => {
    const error = await sessionResponseError(new Response('<html>Unavailable</html>', { status: 503 }));
    expect(error.message).toContain('HTTP 503');
    expect(error.message).not.toContain('<html>');
  });
});
