import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planWorkspaceReset, applyWorkspaceReset } from '../reset-workspaces.mjs';

const actors = [1, 2].map(i => ({ username: `user${i}`, session: { user: { id: `u${i}` }, active_workspace_id: `w${i}` } }));
test('reset preview resolves every owned workspace without a mutation', async () => {
  const calls = [];
  const plan = await planWorkspaceReset(actors, async (actor, path, options) => {
    calls.push({ path, options });
    return { workspaces: [1, 2].map(i => ({ id: `${actor.username}-w${i}`, role: 'owner' })) };
  });
  assert.equal(plan.length, 4);
  assert.ok(calls.every(call => call.options === undefined));
});
test('reset stops before mutation if any allowlisted user has a non-owned workspace', async () => {
  await assert.rejects(planWorkspaceReset(actors, async actor => ({ workspaces: [{ id: actor.username, role: actor === actors[1] ? 'editor' : 'owner' }] })), /non_owned_workspace/);
});
test('apply cancels and waits for active execution and generation before clearing each scoped workspace', async () => {
  const calls = [], active = new Set(['run', 'gen']);
  const api = async (_actor, path, options) => {
    calls.push({ path, ...options });
    if (options.method === 'DELETE') { active.delete(path.endsWith('/run') ? 'run' : 'gen'); return {}; }
    if (path.includes('?')) return { runs: active.has(path.includes('generation') ? 'gen' : 'run') ? [{ run_id: path.includes('generation') ? 'gen' : 'run', status: 'running' }] : [] };
    if (path.endsWith('/clear-all')) { assert.equal(active.size, 0); return { status: 'ok' }; }
    return { nodes: [] };
  };
  const result = await applyWorkspaceReset([{ actor: actors[0], workspace_id: 'w1' }], api, { pollMs: 1 });
  assert.equal(result[0].reset, true);
  assert.ok(calls.every(call => call.workspace === 'w1'));
  assert.ok(calls.findIndex(call => call.method === 'DELETE') < calls.findIndex(call => call.method === 'POST'));
});
test('a stuck cancellation never clears workspace content', async () => {
  let cleared = false;
  await assert.rejects(applyWorkspaceReset([{ actor: actors[0], workspace_id: 'w1' }], async (_actor, path) => {
    if (path.endsWith('/clear-all')) cleared = true;
    return { runs: [{ run_id: 'run', status: 'cancelling' }] };
  }, { timeoutMs: 0, pollMs: 1 }), /workspace_still_busy/);
  assert.equal(cleared, false);
});
