import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import { resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import { runLoadTest } from './runner.mjs';
import { ensure, positiveInteger, validateAccounts, validatedURL, safeFailure, DEFAULT_PROMPT } from './core.mjs';

try {
  const { values } = parseArgs({ options: {
    url: { type: 'string' }, issuer: { type: 'string' }, users: { type: 'string', default: '1' },
    accounts: { type: 'string', default: 'loadtest/accounts.local.json' }, rounds: { type: 'string', default: '1' },
    'timeout-seconds': { type: 'string', default: '180' }, 'ramp-seconds': { type: 'string', default: '0' },
    'prompt-file': { type: 'string' }, output: { type: 'string', default: 'loadtest/results' },
    preflight: { type: 'boolean', default: false }, headed: { type: 'boolean', default: false },
    help: { type: 'boolean', default: false },
  } });
  if (values.help) {
    console.log('npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 20 [--rounds 1] [--preflight] [--headed] [--ramp-seconds 30] [--timeout-seconds 180] [--prompt-file prompt.txt]');
  } else {
    const count = positiveInteger(values.users, 'users');
    const baseURL = validatedURL(values.url), issuer = validatedURL(values.issuer);
    ensure(new URL(baseURL).pathname === '/', 'app_url_must_be_origin');
    const accounts = validateAccounts(JSON.parse(await readFile(values.accounts, 'utf8')), count);
    const rounds = positiveInteger(values.rounds, 'rounds');
    const timeoutMs = positiveInteger(values['timeout-seconds'], 'timeout', 3600) * 1000;
    const rampMs = values['ramp-seconds'] === '0' ? 0 : positiveInteger(values['ramp-seconds'], 'ramp', 3600) * 1000;
    const prompt = values['prompt-file'] ? await readFile(values['prompt-file'], 'utf8') : DEFAULT_PROMPT;
    console.log(`${values.preflight ? 'Preflight only' : 'LIVE LLM workload'}: ${count} users, ${rounds} round(s). Fresh test workspaces will be retained.`);
    const report = await runLoadTest({ baseURL, issuer, accounts, rounds, timeoutMs, rampMs, prompt, preflight: values.preflight, headed: values.headed });
    try { report.load_generator_commit = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); } catch { report.load_generator_commit = null; }
    report.server_commit = 'Record the deployed VM commit separately.';
    const directory = resolve(values.output, report.run_id);
    await mkdir(directory, { recursive: true, mode: 0o700 });
    await writeFile(resolve(directory, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
    console.log(JSON.stringify(report.summary, null, 2));
    if (report.failure) console.error(`Stopped during ${report.failure.phase}: ${report.failure.code}`);
    console.log(`Report: ${directory}/report.json`);
    process.exitCode = report.passed ? 0 : 1;
  }
} catch (error) {
  console.error(`Load test could not start: ${safeFailure(error)}. Check arguments, account file and Playwright installation. No raw credentials or server error bodies are logged.`);
  process.exitCode = 1;
}
