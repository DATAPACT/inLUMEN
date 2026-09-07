import { test } from 'node:test';
import assert from 'node:assert/strict';
import { positiveInteger, validatedURL, validateAccounts, validateSessions, validateGraph, summarize, safeFailure } from '../core.mjs';

test('rejects unsafe URLs, duplicate users and invalid counts', () => {
  assert.equal(validatedURL('https://app.example.com/'), 'https://app.example.com');
  for (const url of ['http://app.example.com', 'https://user:secret@app.example.com', 'https://app.example.com/?token=x']) assert.throws(() => validatedURL(url));
  for (const count of [0, -1, 1.5, 'bad']) assert.throws(() => positiveInteger(count, 'users'));
  assert.throws(() => validateAccounts([{username:'a',password:'x'}, {username:'A',password:'y'}], 2));
  assert.throws(() => validateAccounts([null], 1));
});
test('requires distinct non-admin authenticated users and workspaces', () => {
  const a = {user:{id:'one'}, active_workspace_id:'w1'};
  const b = {user:{id:'two'}, active_workspace_id:'w2'};
  validateSessions([a,b]);
  for (const sessions of [[], [a,a], [a,{...b,is_application_admin:true}], [a,{...b,active_workspace_id:'w1'}], [{user:{id:'x'}}]]) assert.throws(() => validateSessions(sessions));
});
test('rejects invalid pipeline graph references', () => {
  validateGraph({nodes:[{id:'a'},{id:'b'}],edges:[{source:'a',target:'b'}]});
  assert.throws(() => validateGraph({nodes:[{id:'a'},{id:'b'}],edges:[{source:'a',target:'missing'}]}));
  assert.throws(() => validateGraph({nodes:[],edges:[]}));
});
test('reports failures separately from successful latency and counts overlap', () => {
  const summary = summarize([
    {ok:true, elapsed_ms:100, request_started_ms:0, request_finished_ms:100},
    {ok:true, elapsed_ms:200, request_started_ms:50, request_finished_ms:250},
    {ok:false, elapsed_ms:300, request_started_ms:250, request_finished_ms:550},
  ],3);
  assert.equal(summary.peak_observed_chat_requests,2);
  assert.equal(summary.success_rate,2/3);
  assert.equal(summary.successful_latency_ms.p95,200);
  assert.equal(safeFailure(new Error('secret-token')), 'browser_or_network_error');
});
