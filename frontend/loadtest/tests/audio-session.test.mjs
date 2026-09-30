import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import JSZip from 'jszip';
import { runLoadTest } from '../runner.mjs';
import { AUDIO_PROMPTS, validateAudioGraph } from '../core.mjs';
import { loadSessionAssets } from '../audio-session.mjs';

const folders = ['Transcription', 'Named Entity Recognition', 'Data Anonymization', 'Sentiment Analysis'].map(name => `nodes/${name}`);
const outputs = ['transcription.json', 'entities.json', 'anonymized.json', 'sentiment.json'];

async function syntheticBundle() {
  const archive = new JSZip();
  for (const [index, folder] of folders.entries()) {
    archive.file(`${folder}/main.py`, 'print("synthetic package fixture")\n');
    archive.file(`${folder}/inlumen.task.json`, JSON.stringify({ version: 1, inputs: [],
      output: { type: 'file', path: outputs[index], format: 'json' } }));
  }
  return archive.generateAsync({ type: 'nodebuffer' });
}
function graphFor(user, extended) {
  const names = extended ? ['Audio Upload', 'Transcription', 'Named Entity Recognition', 'Data Anonymization', 'Sentiment Analysis', 'Results Output']
    : ['Audio Upload', 'Transcription', 'Sentiment Analysis', 'Results Output'];
  const nodes = names.map((label, i) => ({ id: `${user}-${i}`, data: { label, type: i === 0 ? 'source' : i === names.length - 1 ? 'destination' : 'task' } }));
  return { nodes, edges: nodes.slice(1).map((node, i) => ({ source: nodes[i].id, target: node.id, targetHandle: `participant-port-${i}` })) };
}

test('checks the requested graph order and rejects bypasses', () => {
  const graph = graphFor('opaque', true);
  assert.equal(validateAudioGraph(graph, true).ner, 'opaque-2');
  graph.nodes[4].data.description = 'Analyze sentiment after transcription and anonymization';
  validateAudioGraph(graph, true);
  graph.edges[3].source = graph.nodes[1].id;
  assert.throws(() => validateAudioGraph(graph, true), /incorrect_audio_pipeline_order/);
});

async function fixture(mode, bytes) {
  const states = new Map(), messages = [], uploadedCodes = [], uploadedAudio = [], submissions = [], appliedPreviews = [];
  const previewMode = ['preview_success', 'preview_apply_failure', 'review_disabled'].includes(mode);
  const server = createServer(async (req, res) => {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    const path = new URL(req.url, 'http://localhost').pathname;
    const user = req.headers.authorization?.replace('Bearer ', '');
    const scope = req.headers['x-inlumen-workspace-id'];
    const json = (value, status = 200, headers = {}) => { res.writeHead(status, { 'Content-Type': 'application/json', ...headers }); res.end(JSON.stringify(value)); };
    const body = async () => { const chunks = []; for await (const chunk of req) chunks.push(chunk); return Buffer.concat(chunks); };
    if (path === '/realms/inlumen/login') {
      res.end('<form action="/realms/inlumen/auth" method="post"><input id="username" name="username"><input id="password" name="password"><button id="kc-login">Log in</button></form>'); return;
    }
    if (path === '/realms/inlumen/auth') {
      const name = new URLSearchParams((await body()).toString()).get('username');
      res.writeHead(302, { 'Set-Cookie': `user=${name}; Path=/; HttpOnly`, Location: '/' }); res.end(); return;
    }
    if (path === '/') {
      const name = req.headers.cookie?.match(/user=([^;]+)/)?.[1];
      if (!name) { res.writeHead(302, { Location: '/realms/inlumen/login' }); res.end(); return; }
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(`<button id="clear">Clear all</button><div role="alertdialog" aria-label="Clear the entire workspace?" id="confirmation" hidden><button id="confirmClear">Clear workspace</button></div>
      <button id="settings">Settings</button><div role="dialog" id="settingsDialog" hidden><button aria-haspopup="menu" id="menu">Config</button><p id="selected" hidden>Application-provided LLM · Managed by your administrator. No API key needed.</p><button id="closeSettings">Close</button></div><button role="menuitem" id="item" hidden>Application-provided LLM</button>
      <button role="switch" aria-label="Preview AI graph changes before applying" aria-checked="true" id="reviewAI">Review AI</button><button id="chat">Chat</button><textarea placeholder="Describe the pipeline..."></textarea><button id="send">Send</button><div id="canvas"></div>
      <div role="dialog" aria-label="Review proposed graph" id="preview" hidden><button id="applyPreview">Apply to canvas</button></div>
      <button id="library" aria-pressed="false">Library</button><button role="tab">Run</button><button id="upload">Upload code ZIP</button><input type="file" accept=".zip,application/zip" id="zip">
      <div role="dialog" aria-label="Review code ZIP" id="review" hidden><div id="matches"></div><button id="revalidate">Revalidate</button><button id="importCode">Import 4 Task packages</button></div><button id="run">Run current pipeline</button>
      <script>
      const api=(path,options={})=>fetch(path,{...options,headers:{Authorization:'Bearer ${name}',...(options.headers||{})}});
      api('/api/session');
      clear.onclick=()=>confirmation.hidden=false;
      confirmClear.onclick=async()=>{confirmation.hidden=true;clear.disabled=true;await api('/api/workspace/clear-all',{method:'POST'});canvas.innerHTML='';clear.disabled=false;};
      settings.onclick=()=>settingsDialog.hidden=false;menu.onclick=()=>item.hidden=false;item.onclick=()=>{item.hidden=true;selected.hidden=false;};closeSettings.onclick=()=>settingsDialog.hidden=true;
      library.onclick=()=>library.setAttribute('aria-pressed','true');
      reviewAI.onclick=()=>reviewAI.setAttribute('aria-checked',String(reviewAI.getAttribute('aria-checked')!=='true'));
      let proposal;
      send.onclick=async()=>{send.disabled=true;const response=await api('/simple_chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:document.querySelector('textarea').value,preview_changes:reviewAI.getAttribute('aria-checked')==='true',llm_config:{credential_id:'application-llm'}})});if(response.ok){const data=await response.json();if(data.sync.preview_pending){proposal=data.graph;preview.hidden=false;}else{canvas.innerHTML=data.graph.nodes.map(()=>'<div class="react-flow__node">Node</div>').join('');}document.querySelector('textarea').value='';}send.disabled=false;};
      applyPreview.onclick=async()=>{const response=await api('/api/pipeline/graph',{method:'POST',headers:{'Content-Type':'application/json','If-Match':'"1"'},body:JSON.stringify({graph:proposal})});if(response.ok){canvas.innerHTML=proposal.nodes.map(()=>'<div class="react-flow__node">Node</div>').join('');preview.hidden=true;}};
      upload.onclick=()=>zip.click();
      let code, report, mappings={};
      async function validate(){const form=new FormData();form.append('file',code);form.append('mappings',JSON.stringify(mappings));const response=await api('/api/pipeline/task-packages/validate',{method:'POST',body:form});report=await response.json();return report;}
      zip.onchange=async()=>{code=zip.files[0];await validate();matches.innerHTML='';for(const pkg of report.packages){const select=document.createElement('select');select.setAttribute('aria-label','Target Task for '+pkg.folder);select.append(new Option('Choose',''));for(const node of report.nodes)select.append(new Option(node.data.label,node.id));select.value='';select.onchange=async()=>{mappings[pkg.folder]=select.value;await validate();};matches.append(select);}review.hidden=false;};
      revalidate.onclick=()=>validate();
      importCode.onclick=async()=>{const form=new FormData();form.append('file',code);form.append('mappings',JSON.stringify(mappings));form.append('digest',report.digest);const response=await api('/api/pipeline/task-packages/import',{method:'POST',headers:{'If-Match':report.graph_revision},body:form});if(response.ok)review.hidden=true;};
      run.onclick=()=>api('/api/pipeline-runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idempotency_key:'fixture-one-run'})});
      </script>`); return;
    }
    if (!user) { json({}, 401); return; }
    if (path === '/api/session') {
      if (!states.has(user)) states.set(user, { graph: graphFor(user, false), chat: 0, polls: 0 });
      json({ user: { id: user }, active_workspace_id: `personal-${user}`, is_application_admin: false }); return;
    }
    if (scope !== `personal-${user}`) { json({}, 404); return; }
    const state = states.get(user);
    if (path === '/api/chatbot-configs') { json({ configs: [{ id: 'application-llm', has_api_key: true, model: 'fixture' }] }); return; }
    if (path === '/api/workspace/clear-all') { state.graph = { nodes: [], edges: [] }; state.chat = 0; json({ status: 'ok' }); return; }
    if (path === '/api/pipeline/graph') {
      if (req.method === 'POST') {
        const proposal = JSON.parse((await body()).toString()).graph;
        assert.equal(req.headers['if-match'], '"1"');
        if (mode === 'preview_apply_failure') { json({ code: 'graph_conflict' }, 409); return; }
        state.graph = proposal;
        appliedPreviews.push(user);
      }
      json(state.graph, 200, { ETag: 'W/"1"', 'X-InLumen-Graph-Revision': '1' }); return;
    }
    if (path === '/simple_chat') {
      const { message, preview_changes } = JSON.parse((await body()).toString());
      const pending = previewMode && preview_changes !== false;
      messages.push({ user, message, preview_changes }); state.chat++;
      await new Promise(resolve => setTimeout(resolve, 100));
      const proposal = graphFor(user, state.chat === 2);
      if (!pending) state.graph = proposal;
      json({ graph: proposal, sync: { guardrail_passed: true, preview_pending: pending } }, mode === 'second_chat_failure' && state.chat === 2 ? 524 : 200); return;
    }
    if (path.startsWith('/api/pipeline/task-packages/')) {
      const content = await body();
      assert.ok(content.includes(bytes), 'identical ZIP bytes uploaded');
      const mapping = JSON.parse(content.toString().match(/name="mappings"\r\n\r\n([^\r]*)/)[1]);
      const roles = validateAudioGraph(state.graph, true);
      const roleNames = ['transcription', 'ner', 'anonymization', 'sentiment'];
      const packages = folders.map((folder, i) => ({ folder, node_id: mapping[folder] || null, manifest: { output: { path: outputs[i] } } }));
      const valid = mode !== 'invalid_zip' && packages.every((pkg, i) => pkg.node_id === roles[roleNames[i]]);
      if (path.endsWith('/import')) {
        assert.ok(valid); assert.equal(req.headers['if-match'], '"1"');
        uploadedCodes.push(user); json({ imported: 4 }); return;
      }
      json({ valid, digest: 'fixture-digest', graph_revision: '"1"', packages, nodes: state.graph.nodes.filter(n => n.data.type === 'task') }); return;
    }
    if (path.endsWith('/files')) {
      assert.equal(path, `/api/nodes/${user}-0/files`);
      assert.equal(req.headers['if-match'], '"1"');
      uploadedAudio.push(await body()); json({ status: 'ok' }); return;
    }
    if (path === '/api/pipeline-runs' && req.method === 'POST') { submissions.push(user); json({ run_id: `run-${user}`, status: 'queued' }, 202); return; }
    if (path === `/api/pipeline-runs/run-${user}`) {
      state.polls++;
      const status = state.polls < 2 ? 'running' : mode === 'run_failure' ? 'failed' : 'succeeded';
      json({ run_id: `run-${user}`, status, progress: { phase: 'running_pipeline', resource_cpu: ['excessive_allocation', 'custom_allocation_limits'].includes(mode) ? 4 : 2, resource_memory_bytes: 4 * 1024 ** 3 },
        result: { outputs: outputs.filter(name => mode !== 'missing_artifact' || name !== 'anonymized.json').map(filename => ({ filename, path: `outputs/${user}/${filename}` })) } }); return;
    }
    if (path.startsWith(`/api/pipeline-runs/run-${user}/outputs/`)) {
      const filename = path.split('/').at(-1);
      const data = { 'transcription.json': { text: 'Hello Alice, I am happy.', chunks: [] },
        'entities.json': { text: 'Hello Alice, I am happy.', entities: [{ label: 'PER', text: 'Alice' }] },
        'anonymized.json': { text: 'Hello <REDACTED>, I am happy.', redactions: [{}], entity_count: 1 },
        'sentiment.json': { anonymized_text: 'Hello <REDACTED>, I am happy.', sentiment: { label: 'positive' } } };
      json(data[filename]); return;
    }
    json({}, 404);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { baseURL: `http://127.0.0.1:${server.address().port}`, messages, uploadedCodes, uploadedAudio, submissions, appliedPreviews,
    close: () => new Promise(resolve => server.close(resolve)) };
}

for (const mode of ['success', 'second_chat_failure', 'invalid_zip', 'run_failure', 'missing_artifact', 'excessive_allocation', 'custom_allocation_limits', 'excessive_memory', 'preview_success', 'preview_apply_failure', 'review_disabled']) {
  test(`audio session browser workload: ${mode}`, { timeout: 90000 }, async () => {
    const bytes = await syntheticBundle();
    const server = await fixture(mode, bytes);
    const dir = await mkdtemp(join(tmpdir(), 'inlumen-session-'));
    const codeZip = join(dir, 'synthetic-code.zip');
    await writeFile(codeZip, bytes);
    const audioFile = join(dir, 'session.wav');
    const audioBytes = Buffer.from('RIFF session fixture WAV bytes');
    await writeFile(audioFile, audioBytes);
    try {
      const users = ['success', 'preview_success'].includes(mode) ? 20 : 2;
      const report = await runLoadTest({ baseURL: server.baseURL, issuer: server.baseURL + '/realms/inlumen',
        accounts: Array.from({ length: users }, (_, i) => ({ username: `user${i + 1}`, password: 'fixture-password' })),
        scenario: 'audio-session', reviewAIChanges: mode === 'review_disabled' ? false : undefined, codeZip, audioFile, timeoutMs: 5000, runTimeoutMs: 5000, pollMs: 20,
        maxRunCpus: mode === 'custom_allocation_limits' ? 4 : 2,
        maxRunMemoryGiB: mode === 'excessive_memory' ? 2 : 4, onProgress: () => {} });
      assert.equal(report.failure, null, JSON.stringify(report));
      assert.equal(report.passed, ['success', 'custom_allocation_limits', 'preview_success', 'review_disabled'].includes(mode), JSON.stringify(report));
      assert.deepEqual(report.allocation_limits, { cpu: mode === 'custom_allocation_limits' ? 4 : 2,
        memory_bytes: (mode === 'excessive_memory' ? 2 : 4) * 1024 ** 3 });
      const expectedMessages = mode === 'preview_apply_failure' ? AUDIO_PROMPTS.slice(0, 1) : AUDIO_PROMPTS;
      assert.equal(server.messages.length, users * expectedMessages.length);
      for (let i = 1; i <= users; i++) assert.deepEqual(server.messages.filter(message => message.user === `user${i}`).map(item => item.message), expectedMessages);
      const shouldRun = !['second_chat_failure', 'invalid_zip', 'preview_apply_failure'].includes(mode);
      assert.equal(server.submissions.length, shouldRun ? users : 0);
      assert.equal(server.uploadedCodes.length, shouldRun ? users : 0);
      assert.ok(server.uploadedAudio.every(body => body.includes(audioBytes)));
      assert.equal(report.results.length, users);
      if (mode === 'success') {
        assert.ok(report.summary.peak_observed_chat_requests > 1);
        assert.ok(report.results.every(result => result.stages.length === 5 && result.stages.at(-1).artifacts_verified.length === 4));
        assert.equal(report.shared_assets.audio_bytes, audioBytes.length);
      }
      if (mode === 'second_chat_failure') assert.ok(report.results.every(result => result.failure === 'chat_http_524' && result.outcome_may_be_running));
      if (mode === 'run_failure' || mode === 'missing_artifact') assert.ok(report.results.every(result => !result.outcome_may_be_running));
      if (['excessive_allocation', 'excessive_memory'].includes(mode)) assert.ok(report.results.every(result => result.failure === 'execution_allocation_exceeds_configured_limits'));
      if (mode === 'preview_success') {
        assert.equal(server.appliedPreviews.length, users * 2);
        assert.ok(report.results.every(result => result.stages.slice(0, 2).every(stage => stage.preview_applied)));
      }
      if (mode === 'review_disabled') {
        assert.equal(report.review_ai_changes, false);
        assert.equal(server.appliedPreviews.length, 0);
        assert.ok(server.messages.every(message => message.preview_changes === false));
      }
      if (mode === 'preview_apply_failure') assert.ok(report.results.every(result => result.failure === 'graph_preview_apply_http_409'));
      const text = JSON.stringify(report);
      assert.ok(!text.includes('fixture-password') && !text.includes('Hello Alice'));
    } finally { await server.close(); await rm(dir, { recursive: true }); }
  });
}

test('missing session assets fail before launching a browser', async () => {
  await assert.rejects(loadSessionAssets(), /audio_session_requires/);
});
