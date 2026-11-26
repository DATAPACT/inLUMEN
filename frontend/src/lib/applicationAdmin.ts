import type { AuthSession } from '@/context/authSession';

export const canManageApplicationLLM = (
  authEnabled: boolean,
  session: AuthSession | null | undefined,
) => authEnabled && session?.is_application_admin === true;
