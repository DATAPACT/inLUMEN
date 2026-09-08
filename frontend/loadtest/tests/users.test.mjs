import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,stat,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
test('user generator writes private files and refuses overwrite',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'inlumen-users-'));
 try {
  const accounts=join(dir,'accounts.json'), payload=join(dir,'import.json');
  const args=[new URL('../create-users.mjs',import.meta.url).pathname,'--count','3','--accounts',accounts,'--import',payload];
  const output=execFileSync(process.execPath,args,{encoding:'utf8'});
  const original=await readFile(accounts,'utf8');
  const users=JSON.parse(original), imported=JSON.parse(await readFile(payload,'utf8'));
  assert.equal(users.length,3);assert.equal(imported.ifResourceExists,'FAIL');
  assert.equal((await stat(accounts)).mode&0o777,0o600);
  assert.equal(new Set(users.map(u=>u.password)).size,3);
  assert.ok(users.every(u=>!output.includes(u.password)));
  assert.ok(imported.users.every(u=>!u.realmRoles&&!u.clientRoles));
  assert.throws(()=>execFileSync(process.execPath,args,{stdio:'pipe'}));
  assert.equal(await readFile(accounts,'utf8'),original);
 } finally {await rm(dir,{recursive:true,force:true});}
});
