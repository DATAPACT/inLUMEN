import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { runLoadTest } from '../runner.mjs';

// Real Chromium + local HTTP fixture; no Keycloak, LLM or external service calls.
async function fixture(status = 200) {
  const workspaces = new Map();
  let counter = 0, chats = 0;
  const graph = {nodes:[{id:'a'},{id:'b'}],edges:[{source:'a',target:'b'}]};
  const server = createServer(async (req,res) => {
    const url = new URL(req.url, 'http://localhost');
    res.setHeader('Content-Type','text/html');
    const user = req.headers.authorization?.replace('Bearer ', '');
    const scope = req.headers['x-inlumen-workspace-id'];
    const json = (data, code=200) => {res.writeHead(code, {'Content-Type':'application/json'}); res.end(JSON.stringify(data));};
    if (url.pathname === '/realms/inlumen/login') {
      res.end('<form action="/realms/inlumen/auth" method="post"><input id="username" name="username"><input id="password" name="password" type="password"><button id="kc-login">Log in</button></form>'); return;
    }
    if (url.pathname === '/realms/inlumen/auth') {
      let body=''; for await (const chunk of req) body+=chunk;
      const username=new URLSearchParams(body).get('username');
      res.writeHead(302, {'Set-Cookie':`user=${username}; Path=/; HttpOnly`,Location:'/'}); res.end(); return;
    }
    if (url.pathname === '/') {
      const username = req.headers.cookie?.match(/user=([^;]+)/)?.[1];
      if (!username) {res.writeHead(302,{Location:'/realms/inlumen/login'});res.end();return;}
      res.end(`<button id="settings">Settings</button><div id="dialog" role="dialog" hidden><button aria-haspopup="menu" id="menu">Choose config</button><button id="close">Close</button></div><button role="menuitem" id="item" hidden>Application-provided LLM</button><button id="chat">Chat</button><div id="panel" hidden><textarea placeholder="Describe the pipeline..."></textarea><button id="send">Send</button></div><div id="canvas"></div><script>
      const api=(path,options={})=>fetch(path,{...options,headers:{Authorization:'Bearer ${username}','Content-Type':'application/json'}});
      api('/api/session');
      settings.onclick=()=>dialog.hidden=false; menu.onclick=()=>item.hidden=false; item.onclick=()=>item.hidden=true; close.onclick=()=>dialog.hidden=true;
      document.getElementById('close').onclick=()=>dialog.hidden=true;
      chat.onclick=()=>panel.hidden=false;
      send.onclick=async()=>{const r=await api('/simple_chat',{method:'POST',body:JSON.stringify({llm_config:{credential_id:'application-llm'}})});if(r.ok){await r.json();canvas.innerHTML='<div class="react-flow__node">Node</div>';}};
      </script>`); return;
    }
    if (!user) {json({},401);return;}
    if (url.pathname === '/api/session') {json({user:{id:user},active_workspace_id:scope||`personal-${user}`,is_application_admin:false});return;}
    if (url.pathname === '/api/workspaces') {const id=`w${++counter}`;workspaces.set(id,{user,graph:{nodes:[],edges:[]}});json({workspace:{id}},201);return;}
    if (url.pathname === '/api/chatbot-configs') {json({configs:[{id:'application-llm',has_api_key:true,model:'fixture'}]});return;}
    const workspace = workspaces.get(scope);
    if (!workspace || workspace.user !== user) {json({},404);return;}
    if (url.pathname === '/api/pipeline/graph') {json(workspace.graph);return;}
    if (url.pathname === '/simple_chat') {chats++;await new Promise(r=>setTimeout(r,250));workspace.graph=graph;json({graph,sync:{guardrail_passed:true}},status);return;}
    json({},404);
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const baseURL=`http://127.0.0.1:${server.address().port}`;
  return {baseURL, chats:()=>chats, close:()=>new Promise(r=>server.close(r))};
}
for (const scenario of [{preflight:true,status:200}, {preflight:false,status:200}, {preflight:false,status:524}]) {
  test(`browser workload preflight=${scenario.preflight}, HTTP=${scenario.status}`, {timeout:60000}, async () => {
    const server=await fixture(scenario.status);
    try {
      const report=await runLoadTest({baseURL:server.baseURL,issuer:server.baseURL+'/realms/inlumen',accounts:[1,2,3].map(i=>({username:'user'+i,password:'test-password'})),rounds:2,preflight:scenario.preflight,timeoutMs:5000,onProgress:()=>{}});
      assert.equal(report.failure,null,JSON.stringify(report));
      assert.equal(report.passed,scenario.status===200);
      assert.equal(server.chats(),scenario.preflight?0:scenario.status===200?6:3);
      if (!scenario.preflight && scenario.status===200) assert.ok(report.summary.peak_observed_chat_requests>=2);
      if (scenario.status===524) assert.ok(report.results.every(r=>r.failure==='chat_http_524'));
      assert.ok(!JSON.stringify(report).includes('test-password'));
    } finally {await server.close();}
  });
}
