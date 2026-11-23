// Regression for out-of-order upload indexing polls and user session changes.
// Executes the production function verbatim in a tiny deterministic DOM mock.
'use strict';
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {resolve}=require('node:path');
const vm=require('node:vm');
const source=readFileSync(resolve(__dirname,'../app.js'),'utf8');
const start=source.indexOf('async function loadUploadJobStatus(){');
const end=source.indexOf('async function aiUpload(',start);
assert(start>0&&end>start);
const note={children:[],append(x){this.children.push(x)}};
const state={me:{role:'engineer'},uploadDocId:'A',sessionSerial:3};
let called=[];
let resolveRequest;
let response=new Promise(resolve=>{resolveRequest=resolve});
const request=path=>{called.push(path);return response};
const context={state,request,el:(tag,className,text)=>({tag,className,text}),$:id=> {
  assert.equal(id,'ai-upload-result');return note
}};
vm.runInNewContext(source.slice(start,end)+'\nthis.poll=loadUploadJobStatus;',context);
const poll=context.poll;
(async()=>{
  // A slow status response for document A cannot overwrite B's notice.
  const old=poll();
  state.uploadDocId='B';
  resolveRequest({index_status:'indexed'});
  await old;
  assert.equal(state.uploadDocId,'B');
  assert.equal(note.children.length,0);
  assert.deepEqual(called,['/api/uploads/A/status']);
  // A completed poll in another signed-in session cannot affect the new user.
  let unlock;
  response=new Promise(resolve=>unlock=resolve);
  const switched=poll();
  state.me={role:'finance'};
  state.sessionSerial=4;
  unlock({index_status:'failed'});
  await switched;
  assert.equal(note.children.length,0);
  assert.equal(state.uploadDocId,'B');
  // Correct current response appends one status, and stops polling B.
  response=Promise.resolve({index_status:'indexed'});
  await poll();
  assert.equal(state.uploadDocId,null);
  assert.equal(note.children.length,1);
  assert.equal(note.children[0].className,'ai-hint');
  await poll();
  assert.equal(note.children.length,1);
  // Pending statuses should keep polling; failed status produces one warning.
  state.uploadDocId='C';
  response=Promise.resolve({index_status:'pending'});
  await poll();
  assert.equal(state.uploadDocId,'C');
  assert.equal(note.children.length,1);
  response=Promise.resolve({index_status:'failed'});
  await poll();
  assert.equal(state.uploadDocId,null);
  assert.equal(note.children.length,2);
  assert.equal(note.children[1].className,'ai-warning-line');
  // Temporary errors should not terminate the job.
  state.uploadDocId='D';
  response=Promise.reject(new Error('temporary 503'));
  await poll();
  assert.equal(state.uploadDocId,'D');
  assert.equal(note.children.length,2);
  console.log('PASS 8 upload polling cases: stale doc, stale session, indexed, pending, failed, repeat, retry');
})().catch(e=>{console.error(e);process.exitCode=1});
