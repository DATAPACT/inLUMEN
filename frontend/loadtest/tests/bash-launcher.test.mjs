import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const launcher = fileURLToPath(new URL('../../../scripts/stress-test.sh', import.meta.url));

test('Bash launcher resolves caller paths, protects participant workspaces and keeps preflight offline cache-free', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'inlumen bash '));
  try {
    await writeFile(join(directory, 'accounts.json'), '[{"username":"test","password":"private-password"}]');
    await writeFile(join(directory, 'code.zip'), 'fixture');
    await writeFile(join(directory, 'audio.wav'), 'fixture');
    const base = ['--accounts', 'accounts.json', '--users', '20', '--url', 'https://test.example',
      '--issuer', 'https://auth.example/realms/test', '--ssh-host', 'operator@vm', '--warm-models-script', '/srv/warm.py'];
    const live = spawnSync('bash', [launcher, ...base, '--code-zip', 'code.zip', '--audio-file', 'audio.wav', '--dry-run'],
      { cwd: directory, encoding: 'utf8' });
    assert.equal(live.status, 0, live.stderr);
    assert.match(live.stdout, /--users 20/);
    assert.match(live.stdout, /--workspace-mode isolated/);
    assert.match(live.stdout, /--synchronized-run/);
    assert.match(live.stdout, /--warm-models-host operator@vm/);
    assert.ok(live.stdout.includes(directory.replaceAll(' ', '\\ ') + '/code.zip'));
    assert.ok(!live.stdout.includes('private-password'));
    const preflight = spawnSync('bash', [launcher, ...base, '--preflight', '--dry-run'], { cwd: directory, encoding: 'utf8' });
    assert.equal(preflight.status, 0, preflight.stderr);
    assert.match(preflight.stdout, /--workspace-mode default/);
    assert.ok(!preflight.stdout.includes('--warm-models-host'));
    assert.deepEqual((await readdir(directory)).sort(), ['accounts.json', 'audio.wav', 'code.zip']);
    const invalid = spawnSync('bash', [launcher, ...base, '--preflight', '--users', '0'], { cwd: directory, encoding: 'utf8' });
    assert.equal(invalid.status, 2);
    const secretURL = spawnSync('bash', [launcher, ...base, '--preflight', '--url', 'https://user:private-password@test.example'],
      { cwd: directory, encoding: 'utf8' });
    assert.equal(secretURL.status, 2);
    assert.ok(!(secretURL.stdout + secretURL.stderr).includes('private-password'));
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('Bash launcher preserves test failures and cleans up its VM monitor', { timeout: 15000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'inlumen-launcher-'));
  try {
    await writeFile(join(directory, 'accounts.json'), '[]');
    // A local stub exercises orchestration only: no deployment, browser, SSH,
    // model downloads or paid requests are touched by this test.
    await writeFile(join(directory, 'node'), '#!/usr/bin/env bash\nprintf "fixture test failure\\n"\nsleep 0.5\nexit 7\n', { mode: 0o700 });
    const marker = join(directory, 'monitor-stopped');
    await writeFile(join(directory, 'ssh'), `#!/usr/bin/env bash
if [[ "\${*: -1}" == 'command -v vmstat >/dev/null' ]]; then exit 0; fi
trap 'printf stopped > "$MONITOR_STOP_MARKER"; exit 0' TERM
while true; do sleep 0.1; done
`, { mode: 0o700 });
    const output = join(directory, 'reports');
    const result = spawnSync('bash', [launcher, '--preflight', '--accounts', 'accounts.json',
      '--output', output, '--ssh-host', 'operator@vm', '--monitor-vm'], {
      cwd: directory, encoding: 'utf8', timeout: 10000,
      env: { ...process.env, PATH: `${directory}:${process.env.PATH}`, MONITOR_STOP_MARKER: marker },
    });
    assert.equal(result.status, 7, JSON.stringify(result));
    assert.equal(await readFile(marker, 'utf8'), 'stopped');
    const [run] = await readdir(output);
    assert.match(await readFile(resolve(output, run, 'stress.log'), 'utf8'), /fixture test failure/);
    assert.ok((await readdir(resolve(output, run))).includes('vmstat.log'));
  } finally { await rm(directory, { recursive: true, force: true }); }
});
