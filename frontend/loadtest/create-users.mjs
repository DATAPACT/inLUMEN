import { randomBytes } from 'node:crypto';
import { mkdir, writeFile, unlink } from 'node:fs/promises';
import { dirname } from 'node:path';
import { parseArgs } from 'node:util';
import { ensure, positiveInteger } from './core.mjs';

const { values } = parseArgs({ options: {
  count: { type: 'string', default: '20' }, prefix: { type: 'string', default: 'loadtest' },
  accounts: { type: 'string', default: 'loadtest/accounts.local.json' },
  import: { type: 'string', default: 'loadtest/keycloak-users.local.json' },
} });
try {
  const count = positiveInteger(values.count, 'count');
  ensure(/^[a-z][a-z0-9-]{0,30}$/.test(values.prefix), 'invalid_prefix');
  const accounts = Array.from({ length: count }, (_, i) => ({
    username: `${values.prefix}-${String(i + 1).padStart(3, '0')}`,
    password: `Lt!${randomBytes(24).toString('base64url')}9a`,
  }));
  const users = accounts.map((account, i) => ({
    username: account.username, enabled: true, emailVerified: true,
    email: `${account.username}@example.invalid`, firstName: 'Load test', lastName: String(i + 1),
    requiredActions: [], credentials: [{ type: 'password', value: account.password, temporary: false }],
  }));
  await mkdir(dirname(values.accounts), { recursive: true });
  await mkdir(dirname(values.import), { recursive: true });
  await writeFile(values.accounts, JSON.stringify(accounts, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  try {
    await writeFile(values.import, JSON.stringify({ ifResourceExists: 'FAIL', users }, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  } catch (error) { await unlink(values.accounts); throw error; }
  console.log(`Created ${count} test identities in ${values.accounts} and ${values.import}. Import the second file into the existing inlumen realm. Existing files/accounts are never overwritten.`);
} catch {
  console.error('Could not create test account files. Check arguments and choose output files that do not already exist.');
  process.exitCode = 1;
}
