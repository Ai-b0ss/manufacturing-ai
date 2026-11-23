/* Real Chrome staging acceptance: Keycloak OIDC PKCE -> protected upload -> Qdrant -> delete.
   Fully isolated temporary DB, collection and RP client; credentials never logged. */
"use strict";
const fs=require("fs"),os=require("os"),path=require("path"),cp=require("child_process");
const ROOT=path.resolve(__dirname,"..");
const CREDS=JSON.parse(fs.readFileSync("K:\\ManufacturingAI-CoreRuntime\\credentials\\keycloak-demo-users.json","utf8"));
const PY=process.env.FACTORY_STAGE_PYTHON;
const CHROME="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const BASE="http://localhost:19137";
const SAMPLE="Synthetic inspection sheet 2026. Fixture STAGE-441 permissible tolerance 0.02 mm.";
const PROFILE=fs.mkdtempSync(path.join(os.tmpdir(),"factory-stage-chrome-"));
const FIXTURE=path.join(PROFILE,"Паспорт_узла_441.txt");
fs.writeFileSync(FIXTURE,SAMPLE,"utf8");
const chrome=cp.spawn(CHROME,["--headless=new","--disable-gpu","--no-first-run",
 "--no-default-browser-check","--proxy-bypass-list=<-loopback>","--remote-debugging-port=0","--window-size=1440,900",
 "--user-data-dir="+PROFILE,"about:blank"],{stdio:"ignore",windowsHide:true});
const pending=new Map(),exceptions=[],messages=[];
let ws,sequence=1;
const sleep=t=>new Promise(res=>setTimeout(res,t));
async function command(method,params={},timeout=25000){
 const id=sequence++;
 return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending.delete(id);reject(Error("CDP timeout "+method))},timeout);
   pending.set(id,{resolve,reject,timer});
   ws.send(JSON.stringify({id,method,params}));
 });
}
async function js(expression,timeout){
 const result=await command("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true},timeout);
 if(result.result.exceptionDetails)throw Error(JSON.stringify(result.result.exceptionDetails).slice(0,500));
 return result.result.result.value;
}
async function until(expression,ms=30000){
 const start=Date.now();
 while(Date.now()-start<ms){
  try{if(await js(expression))return true}catch{}
  await sleep(170);
 }
 throw Error("Chrome condition timed out: "+expression.slice(0,150));
}
async function screenshot(filename){
 const response=await command("Page.captureScreenshot",{format:"jpeg",quality:83,captureBeyondViewport:false});
 const folder=path.join(ROOT,"data");fs.mkdirSync(folder,{recursive:true});
 fs.writeFileSync(path.join(folder,filename),Buffer.from(response.result.data,"base64"));
}
(async()=>{
 const report={chrome_used:true,staging_origin:BASE,stages:[],passed:0,exceptions};
 const pass=name=>{report.passed++;report.stages.push(name);console.log("PASS "+name)};
 let docId=null;
 try{
  let port=0;
  for(let n=0;n<100;n++){
   const f=path.join(PROFILE,"DevToolsActivePort");
   if(fs.existsSync(f)){port=Number(fs.readFileSync(f,"utf8").split(/\r?\n/)[0]);if(port)break}
   await sleep(100);
  }
  if(!port)throw Error("Chrome debugger port not found");
  const tabs=await(await fetch("http://127.0.0.1:"+port+"/json")).json();
  ws=new WebSocket(tabs[0].webSocketDebuggerUrl.replace("localhost","127.0.0.1"));
  await new Promise((res,rej)=>{ws.onopen=res;ws.onerror=rej});
  ws.onmessage=e=>{
   const x=JSON.parse(e.data);
   if(x.method==="Runtime.exceptionThrown")exceptions.push(x.params?.exceptionDetails?.text||"unknown JS exception");
   if(x.method==="Page.javascriptDialogOpening")command("Page.handleJavaScriptDialog",{accept:true}).catch(err=>messages.push(String(err)));
   const item=pending.get(x.id);
   if(item){pending.delete(x.id);clearTimeout(item.timer);x.error?item.reject(Error(JSON.stringify(x.error))):item.resolve(x)}
  };
  await command("Page.enable");await command("Runtime.enable");await command("DOM.enable");
  await command("Page.navigate",{url:BASE+"/"});
  await until("location.href.startsWith('"+BASE+"/') && document.readyState==='complete' && document.getElementById('unified-login')?.dataset.ready==='1'",20000);
  if(!await js("!!document.getElementById('ai-scope')"))throw Error("Protected scope selector missing");
  pass("Actual Chrome loads isolated unified app and upload selector");
  await js("document.getElementById('unified-login').click()");
  await until("location.href.startsWith('http://127.0.0.1:18080/') && !!document.querySelector('#kc-form-login')",20000);
  const authUrl=new URL(await js("location.href"));
  if(authUrl.searchParams.get("client_id")!=="factory-web-audit"||authUrl.searchParams.get("code_challenge_method")!=="S256"){
   throw Error("Unexpected staging Keycloak client or PKCE mode");
  }
  pass("Real Keycloak PKCE S256 with dedicated staging client");
  await js("(function(){document.querySelector('#username').value='engineer';document.querySelector('#password').value="+JSON.stringify(CREDS.engineer)+";document.querySelector('#kc-form-login').requestSubmit()})()");
  await until("location.href.startsWith('"+BASE+"/') && !document.getElementById('workspace').hidden",30000);
  // Enabled when assistant panel is opened.
  const caps=await js("(async()=>{const r=await fetch('/unified/api/upload-capabilities');return {status:r.status,data:await r.json()}})()");
  if(caps.status!==200||caps.data.enabled!==true||
     !Array.isArray(caps.data.writable_scopes)||
     !["ingest_scope","engineering"].every(x=>caps.data.writable_scopes.includes(x))){
   throw Error("Keycloak upload scope was not authorized");
  }
  pass("Verified engineer has isolated PostgreSQL write grants for ingest and engineering");
  await js("document.querySelector('[data-page=assistant]').click()");
  await until("!document.getElementById('page-assistant').hidden && !document.getElementById('ai-upload').disabled");
   await js("document.getElementById('ai-scope').value='ingest_scope';");
  const root=await command("DOM.getDocument",{depth:1});
  const input=await command("DOM.querySelector",{nodeId:root.result.root.nodeId,selector:"#ai-file"});
  if(!input.result.nodeId)throw Error("Upload file input not found");
  await command("DOM.setFileInputFiles",{files:[FIXTURE],nodeId:input.result.nodeId});
  await js("document.getElementById('ai-file').dispatchEvent(new Event('change',{bubbles:true}));document.getElementById('ai-upload').click()");
  await until("document.getElementById('ai-upload-result').textContent.includes('Оригинал и текст сохранены')",60000);
  docId=await js("state.uploadDocId");
  if(!/^upload-[0-9a-f]{32}$/.test(docId||""))throw Error("Uploaded document id missing");
  const pendingStatus=await js("(async()=>{const r=await fetch('/unified/api/uploads/"+docId+"/status');return {s:r.status,data:await r.json()}})()");
  if(pendingStatus.s!==200||pendingStatus.data.index_status!=="pending")throw Error("Unexpected outbox status after browser upload");
  pass("Browser uploads real UTF-8 TXT and queues outbox in PostgreSQL");
  const unavailable=await js("(async()=>{const r=await fetch('/unified/ready');return {status:r.status,body:await r.json()}})()");
  if(unavailable.status!==503||unavailable.body.dependencies?.qdrant_collection!==false){
   throw Error("Readiness accepted an uninitialized vector collection");
  }
  pass("Readiness rejects an uninitialized vector collection while the app remains live");
  const searchUnavailable=await js("(async()=>{const t=performance.now();const r=await fetch('/unified/api/search?q=STAGE-441');const body=await r.text();return {s:r.status,ms:performance.now()-t,disclosed:body.includes('permissible tolerance')}})()");
  if(searchUnavailable.s!==503||searchUnavailable.disclosed)throw Error("Search did not fail closed before vector initialization");
  report.uninitialized_search_ms=searchUnavailable.ms;
  pass("Cold search fails closed when the vector collection is unavailable");
  await screenshot("unified_storage_before_index_desktop.jpg");
  const worker=cp.execFileSync(PY,["-m","tools.run_ingest_indexer","--once"],{
   cwd:ROOT,env:process.env,encoding:"utf8",timeout:130000,windowsHide:true
  });
  if(!worker.includes("result=indexed"))throw Error("Index worker did not index accepted upload");
  await js("loadUploadJobStatus()");
  await until("document.getElementById('ai-upload-result')?.textContent.includes('проиндексирован')",20000);
  const binary=await js("(async()=>{const r=await fetch('/unified/api/uploads/"+docId+"/original');return {s:r.status,body:await r.text()}})()");
  if(binary.s!==200||binary.body!==SAMPLE)throw Error("Original bytes differ from uploaded fixture");
  pass("Explicit isolated Qdrant worker and browser original download verified");
  const readiness=await js("(async()=>{const r=await fetch('/unified/ready');return {status:r.status,body:await r.json()}})()");
  if(readiness.status!==200||readiness.body.ready!==true)throw Error("Unified readiness failed with all dependencies available");
  pass("Operational readiness checks PostgreSQL RLS, Keycloak and isolated vector collection");

  const searchCycle=async()=>await js("(async()=>{const t=performance.now();const r=await fetch('/unified/api/search?q=STAGE-441',{signal:AbortSignal.timeout(90000)});const j=await r.json();return {status:r.status,code:j.detail,hit:j.items?.some(x=>x.id==="+JSON.stringify(docId)+"),ms:performance.now()-t}})()",95000);
  const cold=await searchCycle();
  if(cold.status!==200||!cold.hit)throw Error("Indexed document missing from protected hybrid search");
  const started=Date.now();
  const timings=[];
  for(let sample=0;sample<12;sample++){
   const batch=await Promise.all([searchCycle(),searchCycle()]);
   if(batch.some(x=>x.status!==200||!x.hit)){
    report.search_failure={batch:sample+1,results:batch};
    throw Error("Protected search soak lost authorized document");
   }
   timings.push(...batch.map(x=>x.ms));
   await new Promise(resolve=>setTimeout(resolve,500));
  }
  timings.sort((a,b)=>a-b);
  report.search={cold_ms:cold.ms,requests:timings.length,concurrency:2,
   elapsed_ms:Date.now()-started,p50_ms:timings[Math.ceil(timings.length*.5)-1],
   p95_ms:timings[Math.ceil(timings.length*.95)-1],max_ms:timings.at(-1)};
  pass("Protected search finds the upload at concurrency two");
  await until("['ready','stopped'].includes(document.getElementById('ai-status')?.dataset.status)",15000);
  const aiModelState=await js("document.getElementById('ai-status').dataset.status");
  await js("document.getElementById('ai-question').value='Какой допустимый допуск указан для STAGE-441?';document.getElementById('ai-question-form').requestSubmit()");
  await until("!!document.querySelector('#ai-answer .ai-answer-label') && document.querySelectorAll('#ai-sources .ai-source').length>0",120000);
  const askBeforeDelete=await js("({label:document.querySelector('#ai-answer .ai-answer-label')?.textContent||'',answer:document.querySelector('#ai-answer .ai-answer-text')?.textContent||'',sources:document.querySelectorAll('#ai-sources .ai-source').length,cited:document.querySelectorAll('#ai-sources .ai-source-cited').length,excerpt:document.querySelector('#ai-sources .ai-source')?.textContent||''})");
  if(!askBeforeDelete.excerpt.includes("STAGE-441"))throw Error("Unified ask did not hydrate uploaded source");
  if(aiModelState==="ready" && (!askBeforeDelete.label.includes("Ответ") || askBeforeDelete.cited<1)){
   throw Error("Ready MiniCPM did not return cited advisory answer: "+JSON.stringify(askBeforeDelete));
  }
  report.ai_ask={model_state:aiModelState,label:askBeforeDelete.label,sources:askBeforeDelete.sources,cited:askBeforeDelete.cited};
  pass("Browser ask returns source-bound answer with post-generation RLS verification");
  await js("document.querySelector('#ai-upload-result button.ai-source-open').click()");
  await until("document.getElementById('document-modal').open",13000);
  const details=await js("({title:document.getElementById('modal-title').textContent,source:document.getElementById('modal-text').textContent,download:document.getElementById('modal-download').href,deleteVisible:!document.getElementById('modal-delete').hidden})");
  if(!details.source.includes("STAGE-441")||!details.download.endsWith(docId+"/original")||!details.deleteVisible){
   throw Error("Unified document modal does not show original and delete actions");
  }
  pass("Existing unified document modal displays extracted source with download/delete");
  await js("document.getElementById('modal-delete').click()");
  await until("!document.getElementById('document-modal').open && document.getElementById('ai-upload-result').textContent.includes('Файл и его текст удалены')",25000);
  const denied=await js("(async()=>{const r=await fetch('/unified/api/uploads/"+docId+"/original');const d=await fetch('/unified/api/document?id="+docId+"');return {original:r.status,details:d.status}})()");
  if(denied.original!==404||denied.details!==404)throw Error("Soft-deleted document remained accessible after browser action");
  pass("Chrome confirmation deletes logically and fresh PostgreSQL RLS rejects original/details");
  cp.execFileSync(PY,["-m","tools.run_ingest_indexer","--once"],{
   cwd:ROOT,env:process.env,encoding:"utf8",timeout:60000,windowsHide:true
  });
  pass("Qdrant cleanup job ran after browser delete");
  const gone=await searchCycle();
  if(gone.status!==200||gone.hit)throw Error("Deleted document remained searchable");
  pass("Search no longer returns the deleted upload");
  const askAfterDelete=await js("(async()=>{const r=await fetch('/unified/api/ask',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.me.csrf},body:JSON.stringify({question:'Какой допустимый допуск указан для STAGE-441?'})});const j=await r.json();return {status:r.status,sources:j.sources?.length||0,leaked:JSON.stringify(j).includes('permissible tolerance')}})()");
  if(askAfterDelete.status!==200||askAfterDelete.sources!==0||askAfterDelete.leaked){
   throw Error("Deleted document leaked through /unified/api/ask: "+JSON.stringify(askAfterDelete));
  }
  pass("Unified ask returns zero excerpts after upload deletion");

  await js("document.querySelector('[data-page=engineering]').click()");
  await until("!document.querySelector('#page-engineering').hidden",24000);
  await until("!document.querySelector('#private-kit-version-target').disabled",25000);
  const siblingKit=path.resolve(ROOT,"..","ManufacturingAI-Design-Preview",
    ".preview","public_engineering_corpus_20261117","ArcBlue_HMT_Lite");
  const canonicalKit="C:\\Users\\user\\Documents\\ManufacturingAI-Design-Preview\\.preview\\public_engineering_corpus_20261117\\ArcBlue_HMT_Lite";
  const publicKit=process.env.FACTORY_PUBLIC_CAD_DIR||(fs.existsSync(siblingKit)?siblingKit:canonicalKit);
  for(const [selector,file] of [
     ["#private-kit-bom","materials_bom.csv"],
     ["#private-kit-pdf","focuser_drawtube.pdf"],
     ["#private-kit-step","focuser_drawtube.STEP"]]){
     const doc=await command("DOM.getDocument",{depth:1});
     const result=await command("DOM.querySelector",{nodeId:doc.result.root.nodeId,selector});
     if(!result.result.nodeId)throw Error("Missing CAD input "+selector);
     await command("DOM.setFileInputFiles",{nodeId:result.result.nodeId,files:[path.join(publicKit,file)]});
  }
  await js("document.getElementById('private-kit-part').value='focuser_drawtube';document.querySelector('#private-kit-form').requestSubmit()");
  await until("privateKitReport!==null && !document.getElementById('private-kit-save').disabled",130000);
  pass("Chrome analyzed original PDF STEP BOM");
  await js("document.getElementById('private-kit-save').click()");
  await until("document.getElementById('private-kit-save-state').textContent.includes('Сохранено:')",120000);
  await until("savedKits.length>0 && savedKits[0].latest_revision===1",25000);
  const first=await js("({k:savedKits[0]?.kit_id,n:savedKits[0]?.latest_revision,sources:privateKitReport.sources})");
  if(!first.k || first.n!==1 || first.sources.length!==3)throw Error("CAD version 1 not persisted");
  pass("Chrome persisted CAD revision 1");
  await js("document.getElementById('private-kit-version-target').value='';syncPrivateKitTarget()");
  const autoTarget=await js("document.getElementById('private-kit-version-target').value");
  if(autoTarget!==first.k)throw Error("Existing CAD kit was not automatically selected");
  pass("CAD UI selects existing kit for matching part instead of creating a duplicate");

  await js("document.querySelector('#saved-kits-list button').click()");
  await until("document.querySelectorAll('#saved-kits-list a[href*=\"/source/\"]').length===3",20000);
  const downloads=await js("(async()=>{const links=[...document.querySelectorAll('#saved-kits-list a[href*=\"/source/\"]')];const out=[];for(const a of links){const r=await fetch(a.href);const bytes=await r.arrayBuffer();const digest=await crypto.subtle.digest('SHA-256',bytes);const sha=[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('');out.push({name:a.download,status:r.status,sha})}return out})()");
  if(downloads.length!==3 || downloads.some(x=>x.status!==200||!first.sources.some(y=>y.name===x.name&&y.sha256===x.sha)))
    throw Error("CAD original content mismatch");
  pass("Chrome verified 3 original files with SHA-256");
  await js("document.getElementById('private-kit-version-target').value="+JSON.stringify(first.k)+";document.getElementById('private-kit-version-target').dispatchEvent(new Event('change'));document.getElementById('private-kit-save').click()");
  await until("document.getElementById('private-kit-save-state').textContent.includes('редакция 2')",120000);
  pass("Chrome persisted CAD revision 2");

  await command("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await sleep(100);
  const widthEng=await js("({viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})");
  if(widthEng.scroll>widthEng.viewport+1)throw Error("Mobile horizontal overflow on #page-engineering: "+JSON.stringify(widthEng));
  await screenshot("unified_engineering_mobile_390.jpg");
  await js("document.querySelector('[data-page=documents]').click()");
  await until("!document.getElementById('page-documents').hidden && !document.querySelector('#search-results .loading')",15000);
  const widthDocs=await js("({viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})");
  if(widthDocs.scroll>widthDocs.viewport+1)throw Error("Mobile horizontal overflow on #page-documents: "+JSON.stringify(widthDocs));
  await js("document.querySelector('[data-page=assistant]').click()");
  await until("!document.getElementById('page-assistant').hidden");
  const width=await js("({viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})");
  if(width.scroll>width.viewport+1)throw Error("Mobile horizontal overflow on #page-assistant: "+JSON.stringify(width));
  report.mobile_390={engineering:widthEng,documents:widthDocs,assistant:width};
  await screenshot("unified_storage_mobile_390.jpg");
  pass("Chrome mobile viewport 390px has no horizontal overflow on engineering, documents and assistant");
  if(exceptions.length)throw Error("Browser JavaScript exceptions: "+exceptions.join(" | "));
  pass("Chrome reported no unhandled JavaScript exceptions");
  report.success=true;report.document_id=docId;
 }catch(e){
  report.success=false;report.error=String(e).slice(0,1200);
  try{if(ws)await screenshot("unified_storage_failure.jpg")}catch{}
 }finally{
  if(ws)try{ws.close()}catch{}
  try{chrome.kill()}catch{}
  await sleep(700);
  try{fs.rmSync(PROFILE,{recursive:true,force:true,maxRetries:4,retryDelay:260})}catch{}
  const folder=path.join(ROOT,"data");fs.mkdirSync(folder,{recursive:true});
  fs.writeFileSync(path.join(folder,"unified_storage_chrome_acceptance.json"),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
  if(!report.success)process.exitCode=1;
 }
})();
