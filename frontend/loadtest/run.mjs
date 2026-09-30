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
    scenario: { type: 'string', default: 'design' },
    'code-zip': { type: 'string' }, 'audio-file': { type: 'string' },
    'run-timeout-seconds': { type: 'string', default: '1800' },
    'max-run-cpus': { type: 'string', default: '2' },
    'max-run-memory-gib': { type: 'string', default: '4' },
    preflight: { type: 'boolean', default: false }, headed: { type: 'boolean', default: false },
    help: { type: 'boolean', default: false },
  } });
  if (values.help) {
    console.log('npm run stress -- --url https://inlumen.example.com --issuer https://identity.example.com/realms/inlumen --users 20 [--prompt-file prompt.txt] [--preflight] [--headed]\nFor design, extension, import and execution: --scenario audio-session --code-zip /path/pipeline-code.zip --audio-file /path/recording.wav [--rounds 1] [--ramp-seconds 30] [--timeout-seconds 180] [--run-timeout-seconds 1800] [--max-run-cpus 2] [--max-run-memory-gib 4]');
  } else {
    const count = positiveInteger(values.users, 'users');
    const baseURL = validatedURL(values.url), issuer = validatedURL(values.issuer);
    ensure(new URL(baseURL).pathname === '/', 'app_url_must_be_origin');
    const accounts = validateAccounts(JSON.parse(await readFile(values.accounts, 'utf8')), count);
    const rounds = positiveInteger(values.rounds, 'rounds');
    const timeoutMs = positiveInteger(values['timeout-seconds'], 'timeout', 3600) * 1000;
    const rampMs = values['ramp-seconds'] === '0' ? 0 : positiveInteger(values['ramp-seconds'], 'ramp', 3600) * 1000;
    const prompt = values['prompt-file'] ? await readFile(values['prompt-file'], 'utf8') : DEFAULT_PROMPT;
    ensure(!values['prompt-file'] || values.scenario === 'design', 'prompt_file_requires_design_scenario');
    const runTimeoutMs = positiveInteger(values['run-timeout-seconds'], 'run_timeout', 3600) * 1000;
    const maxRunCpus = positiveInteger(values['max-run-cpus'], 'max_run_cpus');
    const maxRunMemoryGiB = positiveInteger(values['max-run-memory-gib'], 'max_run_memory_gib');
    console.log(`${values.preflight ? 'Preflight only' : 'LIVE LLM workload'}: ${count} users, ${rounds} round(s). ${values.preflight ? 'Default workspaces will not be cleared.' : 'CLEAR ALL will erase participating users’ default workspace content before each round; final results remain there.'}`);
    const report = await runLoadTest({ baseURL, issuer, accounts, rounds, timeoutMs, rampMs, prompt,
      scenario: values.scenario, codeZip: values['code-zip'], audioFile: values['audio-file'], runTimeoutMs, maxRunCpus, maxRunMemoryGiB,
      preflight: values.preflight, headed: values.headed });
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
