import { execFile } from 'node:child_process';
import { ensure, LoadTestError } from './core.mjs';

// ssh joins remote arguments into a shell command. Keep both inputs free of
// shell syntax; workspace IDs travel exclusively through JSON on stdin.
export function createModelCachePreparer({ host, script, execute = execFile }) {
  ensure(typeof host === 'string' && /^[A-Za-z0-9_][A-Za-z0-9_.@-]*$/.test(host), 'invalid_warm_models_host');
  ensure(typeof script === 'string' && /^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(script), 'invalid_warm_models_script');
  return async workspaces => {
    await new Promise((resolve, reject) => {
      const child = execute('ssh', ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', host, `python3 ${script}`],
        { timeout: 300000, maxBuffer: 1024 * 1024 }, error => {
          if (error) reject(new LoadTestError('model_cache_preparation_failed'));
          else resolve();
        });
      child.stdin.on('error', () => {}); // Failed connections can close stdin early.
      child.stdin.end(JSON.stringify(workspaces));
    });
    console.log(`${workspaces.length} test workspace model caches ready.`);
  };
}
