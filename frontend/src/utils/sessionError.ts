export class SessionInitializationError extends Error {}

export const sessionResponseError = async (response: Response): Promise<SessionInitializationError> => {
  // Only display known messages; never expose arbitrary upstream errors or tokens.
  const payload = await response.json().catch(() => null);
  if (response.status === 401 && payload?.code === 'token_not_yet_valid') {
    return new SessionInitializationError(
      'Signed in, but the server rejected the token timing. Ask your administrator to check clock synchronization between Keycloak and the application server.',
    );
  }
  if (response.status === 401) {
    return new SessionInitializationError('Signed in, but the server rejected your session. Sign in again; if this persists, contact your administrator.');
  }
  return new SessionInitializationError(`Signed in, but your workspace could not be initialized (HTTP ${response.status}). Please try again or contact your administrator.`);
};
