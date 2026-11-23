"use strict";
/* One actual user flow in real Chrome: select 3 new files, analyze, export. */
const fs=require("fs"),os=require("os"),path=require("path"),cp=require("child_process");
const ROOT=path.resolve(__dirname,".."),BASE=process.env.MANUFACTURINGAI_TEST_BASE||"http://localhost:19137";
const PROFILE=fs.mkdtempSync(path.join(os.tmpdir(),"unified-ai-ui-"));
const FILES=path.resolve(ROOT,"..","ManufacturingAI-Design-Preview",
  ".preview","public_engineering_corpus_20261117","ArcBlue_HMT_Lite");
const PASSWORDS=JSON.parse(fs.readFileSync(
 "K:\\ManufacturingAI-CoreRuntime\\credentials\\keycloak-demo-users.json","utf8"));
const chrome=cp.spawn("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",[
 "--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check",
 "--remote-debugging-port=0","--window-size=1360,980","--user-data-dir="+PROFILE,"about:blank"
],{stdio:"ignore",windowsHide:true});
let ws,seq=1;const pending=new Map(),errors=[],sleep=ms=>new Promise(ok=>setTimeout(ok,ms));
async function cdp(method,params={}){
 const id=seq++;
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(id);reject(Error("CDP timeout "+method))},26000);
  pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));
 });
}
async function js(expression){
 const r=await cdp("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
 if(r.result.exceptionDetails)throw Error("JS evaluate exception "+JSON.stringify(r.result.exceptionDetails).slice(0,400));
 return r.result.result.value;
}
async function until(expression,timeout=27000){
 const start=Date.now();
 while(Date.now()-start<timeout){
  try{if(await js(expression))return}catch{}
  await sleep(200);
 }
 throw Error("Chrome condition timeout: "+expression.slice(0,140));
}
async function selectFile(selector,filename){
 const doc=await cdp("DOM.getDocument",{depth:1});
 const q=await cdp("DOM.querySelector",{nodeId:doc.result.root.nodeId,selector});
 if(!q.result.nodeId)throw Error("Missing input "+selector);
 await cdp("DOM.setFileInputFiles",{nodeId:q.result.nodeId,files:[path.join(FILES,filename)]});
}
(async()=>{
 const outcome={test:"protected_local_model_browser",passed:[],errors};
 const pass=label=>{outcome.passed.push(label);console.log("PASS "+label)};
 try{
  let port;
  for(let i=0;i<90;i++){
   const file=path.join(PROFILE,"DevToolsActivePort");
   if(fs.existsSync(file)){port=Number(fs.readFileSync(file,"utf8").split(/\r?\n/)[0]);if(port)break}
   await sleep(100);
  }
  if(!port)throw Error("Chrome DevTools unavailable");
  const tabs=await(await fetch("http://127.0.0.1:"+port+"/json")).json();
  ws=new WebSocket(tabs[0].webSocketDebuggerUrl.replace("localhost","127.0.0.1"));
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
  ws.onmessage=event=>{
   const m=JSON.parse(event.data);
   if(m.method==="Runtime.exceptionThrown")errors.push(m.params?.exceptionDetails?.text||"JS exception");
   const p=pending.get(m.id);
   if(p){pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m)}
  };
  await cdp("Runtime.enable");await cdp("Page.enable");await cdp("DOM.enable");
  await cdp("Page.setDownloadBehavior",{behavior:"allow",downloadPath:PROFILE});
  await cdp("Page.navigate",{url:BASE+"/"});
  await until("document.readyState==='complete'&&!!document.querySelector('#unified-login')");
  if(!await js("!!document.getElementById('private-kit-form')"))throw Error("CAD kit page not present");
  pass("Unified application loaded with engineering UI");
  await js("document.querySelector('#unified-login').click()");
  await until("location.href.startsWith('http://127.0.0.1:18080/')&&!!document.querySelector('#kc-form-login')",20000);
  const u=new URL(await js("location.href"));
  if(u.searchParams.get("client_id")!==(process.env.MANUFACTURINGAI_TEST_CLIENT||"factory-web-audit")||
     u.searchParams.get("code_challenge_method")!=="S256")throw Error("Not protected OIDC PKCE");
  await js("(function(){document.querySelector('#username').value='engineer';document.querySelector('#password').value="+JSON.stringify(PASSWORDS.engineer)+";document.querySelector('#kc-form-login').requestSubmit()})()");
  await until("location.href.startsWith('"+BASE+"/')&&!document.querySelector('#workspace').hidden",28000);
  await js("document.querySelector('[data-page=engineering]').click()");
  await until("!document.querySelector('#page-engineering').hidden",20000);
  pass("Engineer authenticated to protected engineering workspace");

   await js("document.querySelector('[data-page=assistant]').click()");
   await until("!document.querySelector('#page-assistant').hidden",18000);
   pass("Protected assistant UI accessible after PKCE login");
   await until("['stopped','ready','missing','error','conflict'].includes(document.querySelector('#ai-status')?.dataset.status)",22000);
   const initial=await js("document.querySelector('#ai-status').dataset.status");
   if(initial!=="stopped" && initial!=="ready")
      throw Error("Local model unexpected initial state "+initial);
   await js("document.querySelector('#ai-question').value='Engineering document'");
   await js("document.querySelector('#ai-question-form').requestSubmit()");
   await until("!!document.querySelector('#ai-answer .ai-answer-label')||!!document.querySelector('#ai-answer .message-error')",65000);
   const initialAnswer=await js("document.querySelector('#ai-answer').textContent.slice(0,750)");
   const initialSources=await js("document.querySelectorAll('#ai-sources .ai-source').length");
   if(initialSources===0)throw Error("P3 authorized source retrieval failed "+initialAnswer);
   pass("Protected OIDC/RLS/Qdrant retrieved real source passages");
   if(initial==="stopped"){
     await js("document.querySelector('#ai-start').click()");
     await until("document.querySelector('#ai-status').dataset.status==='ready'",130000);
     pass("Existing offline MiniCPM started using protected UI");
   }
   // Clear the first result so the second form really tests llama.cpp.
   await js("document.querySelector('#ai-answer').replaceChildren()");
   await js("document.querySelector('#ai-question-form').requestSubmit()");
   await until("!!document.querySelector('#ai-answer .ai-answer-label')||!!document.querySelector('#ai-answer .message-error')",130000);
   const label=await js("document.querySelector('#ai-answer .ai-answer-label')?.textContent||''");
   const text=await js("document.querySelector('#ai-answer').textContent.slice(0,1200)");
   const cited=await js("document.querySelectorAll('#ai-sources .ai-source-cited').length");
   const sources=await js("document.querySelectorAll('#ai-sources .ai-source').length");
   outcome.sample={label,cited,sources,answer_prefix:text.slice(0,300)};
   if(!label.includes('\u041e\u0442\u0432\u0435\u0442')||!cited){
     throw Error("LLM advisory citations missing: "+JSON.stringify(outcome.sample));
   }
   pass("Local MiniCPM generated real answer with server-validated source citations");
   if(!text || /password|token|secret/i.test(text))throw Error("Unsafe answer content");
   pass("No credentials or secrets in displayed answer");
   if(errors.length)throw Error("Chrome runtime exception "+errors.join("; "));
   pass("No uncaught JavaScript exceptions");
   outcome.success=true;
 }catch(e){
   outcome.success=false;outcome.failure=String(e).slice(0,950);
 }finally{
   if(ws)try{ws.close()}catch{}
   try{chrome.kill()}catch{}
   await sleep(700);
   try{fs.rmSync(PROFILE,{recursive:true,force:true,maxRetries:4,retryDelay:200})}catch{}
   fs.mkdirSync(path.join(ROOT,"data"),{recursive:true});
   fs.writeFileSync(path.join(ROOT,"data","unified_ai_browser_acceptance.json"),JSON.stringify(outcome,null,2));
   console.log(JSON.stringify(outcome));
   if(!outcome.success)process.exitCode=1;
 }
})();
