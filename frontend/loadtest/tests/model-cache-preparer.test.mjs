import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { createModelCachePreparer } from '../model-cache-preparer.mjs';

test('cache preparation keeps workspace data on stdin and requires shell-safe SSH inputs', async () => {
  for (const host of ['-oProxyCommand=whoami', 'user@host;touch bad', '$(whoami)']) {
    assert.throws(() => createModelCachePreparer({ host, script: '/srv/warm.py' }), /invalid_warm_models_host/);
  }
  for (const script of ['relative.py', '/srv/warm.py;whoami', '/srv/$(whoami).py', '/srv/with space.py']) {
    assert.throws(() => createModelCachePreparer({ host: 'operator@vm', script }), /invalid_warm_models_script/);
  }
  const workspaces = [{ workspace_id: 'workspace-with-"-quote', user_id: 'participant', round: 1 }];
  let received = '';
  const prepare = createModelCachePreparer({ host: 'operator@vm', script: '/srv/warm.py',
    execute(file, args, options, callback) {
      assert.equal(file, 'ssh');
      assert.deepEqual(args, ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'operator@vm', 'python3 /srv/warm.py']);
      assert.equal(options.timeout, 300000);
      const stdin = new PassThrough();
      stdin.on('data', chunk => { received += chunk; });
      stdin.on('end', () => callback(null));
      return { stdin };
    },
  });
  await prepare(workspaces);
  assert.deepEqual(JSON.parse(received), workspaces);
});

test('failed cache preparation exposes a stable error rather than private SSH output', async () => {
  const prepare = createModelCachePreparer({ host: 'operator@vm', script: '/srv/warm.py',
    execute(file, args, options, callback) {
      const stdin = new PassThrough();
      stdin.resume();
      stdin.on('end', () => callback(new Error('private server output')));
      return { stdin };
    },
  });
  await assert.rejects(prepare([{ workspace_id: 'w' }]), error =>
    error.code === 'model_cache_preparation_failed' && !error.message.includes('private'));
});
