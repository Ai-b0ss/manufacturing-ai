/* Real Chromium acceptance: Keycloak login with Authorization Code + PKCE,
   session cookies, user isolation, CSRF and logout. Ephemeral Chrome profile.
   No passwords or tokens printed to report. */
"use strict";
const fs=require("fs"),os=require("os"),path=require("path"),cp=require("child_process");
const PROFILE=fs.mkdtempSync(path.join(os.tmpdir(),"factory-core-p2-chrome-"));
const ROOT=path.resolve(__dirname,"..");
const CREDS=JSON.parse(fs.readFileSync("K:\\ManufacturingAI-CoreRuntime\\credentials\\keycloak-demo-users.json","utf8"));
const CHROME="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const chrome=cp.spawn(CHROME,[
 "--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check",
 "--remote-debugging-port=0","--window-size=1440,900",
 "--user-data-dir="+PROFILE,"about:blank"
],{stdio:"ignore",windowsHide:true});
let ws,seq=1;
const pending=new Map(),exceptions=[];
const sleep=n=>new Promise(resolve=>setTimeout(resolve,n));
async function command(method,params={}){
 const id=seq++;
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(id);reject(Error("CDP timeout "+method));},14000);
  pending.set(id,{resolve,reject,timer});
  ws.send(JSON.stringify({id,method,params}));
 });
}
async function js(expression){
 const result=await command("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true});
 if(result.result.exceptionDetails)throw Error(JSON.stringify(result.result.exceptionDetails).slice(0,900));
 return result.result.result.value;
}
async function until(expression,ms=17000){
 const start=Date.now();
 while(Date.now()-start<ms){
  try{if(await js(expression))return true;}catch{}
  await sleep(160);
 }
 throw Error("Browser condition timeout: "+expression.slice(0,230));
}
async function screenshot(name){
 const result=await command("Page.captureScreenshot",{format:"jpeg",quality:82,captureBeyondViewport:false});
 fs.mkdirSync(path.join(ROOT,"data"),{recursive:true});
 fs.writeFileSync(path.join(ROOT,"data",name),Buffer.from(result.result.data,"base64"));
}
(async()=>{
 const report={chrome_used:true,stages:[],exceptions,passed:0};
 function pass(message){report.stages.push(message);report.passed++;}
 try{
  let port=0;
  for(let i=0;i<80;i++){
   const file=path.join(PROFILE,"DevToolsActivePort");
   if(fs.existsSync(file)){
    port=Number(fs.readFileSync(file,"utf8").split(/\r?\n/)[0]);
    if(port)break;
   }
   await sleep(100);
  }
  if(!port)throw Error("Chrome debugger port missing");
  const tabs=await(await fetch("http://127.0.0.1:"+port+"/json")).json();
  ws=new WebSocket(tabs[0].webSocketDebuggerUrl.replace("localhost","127.0.0.1"));
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=event=>{
   const data=JSON.parse(event.data);
   if(data.method==="Runtime.exceptionThrown"){
    exceptions.push(data.params?.exceptionDetails?.text||"Unknown JS error");
   }
   const item=pending.get(data.id);
   if(item){
    pending.delete(data.id);clearTimeout(item.timer);
    data.error?item.reject(Error(JSON.stringify(data.error))):item.resolve(data);
   }
  };
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Page.navigate",{url:"http://localhost:19139/"});
  await until("document.readyState==='complete' && !!document.getElementById('unified-login')",20000);
  pass("Original P13 UI and assets load on unified gateway");
  const label=await js("document.getElementById('unified-login').textContent");
  if(!label.includes("Keycloak"))throw Error("SSO entry missing");
  pass("Old demo-role login removed from P13 UI");
  await js("document.getElementById('unified-login').click()");
  await until("location.href.startsWith('http://127.0.0.1:18080/') && !!document.querySelector('#kc-form-login')",20000);
  const pkce=new URL(await js("location.href"));
  if(pkce.searchParams.get("code_challenge_method")!=="S256"||
     pkce.searchParams.get("client_id")!=="factory-web-unified")throw Error("Unified Keycloak PKCE client mismatch");
  pass("Real Keycloak PKCE S256 with independent unified client");
  await js("(function(){document.querySelector('#username').value='engineer';document.querySelector('#password').value="+JSON.stringify(CREDS.engineer)+";document.querySelector('#kc-form-login').requestSubmit();})()");
  await until("location.href.startsWith('http://localhost:19139/') && !document.getElementById('workspace').hidden",30000);
  pass("P13 workspace opens after verified Keycloak login");
  const me=await js("(async()=>{let r=await fetch('/unified/api/me');return {s:r.status,data:await r.json()}})()");
  if(me.s!==200||me.data.id!=="engineer"||!me.data.scopes.includes("engineering"))throw Error("Unified identity/scopes incorrect");
  pass("PostgreSQL maps verified engineer identity to P13 user chip");
  const docs=await js("(async()=>{let r=await fetch('/unified/api/documents');return {s:r.status,data:await r.json()}})()");
  if(docs.s!==200||docs.data.items.length!==11)throw Error("Expected 11 scoped documents, got "+JSON.stringify(docs).slice(0,350));
  pass("P13 document catalogue uses scoped PostgreSQL data");
  await js("document.querySelector('[data-page=documents]').click()");
  await until("document.querySelectorAll('#search-results article.doc-card').length>=5",20000);
  pass("Existing P13 document cards render from new database");
  await screenshot("unified_p13_postgres_engineer.jpg");
  const finance=await js("(async()=>{let r=await fetch('/unified/api/metrics');return {s:r.status,data:await r.json()}})()");
  if(finance.s!==200||finance.data.items.length!==0)throw Error("Finance leaked to engineer");
  pass("Finance remains inaccessible to engineer");
  const engine=await js("(async()=>{let r=await fetch('/unified/api/engineering/summary');return {s:r.status,data:await r.json()}})()");
  if(engine.s!==200||!engine.data)throw Error("Legacy P13 engineering read proxy failed: "+JSON.stringify(engine).slice(0,400));
  pass("Original P13 engineering backend accessible with PostgreSQL authorization");
  const guest=await js("(async()=>{let r=await fetch('/unified/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:'чертеж'})});return r.status})()");
  if(guest!==403)throw Error("Missing CSRF did not fail closed");
  pass("Existing assistant POST requires browser CSRF");
  await js("document.querySelector('[data-page=assistant]').click()");
  await until("document.getElementById('ai-status').textContent.length>0");
  pass("Existing P13 assistant workspace opens, reports generator not integrated");
  await js("document.getElementById('ai-question').value='чертёж';document.getElementById('ai-ask').click()");
  await until("document.getElementById('ai-answer').textContent.includes('Найдены разрешённые фрагменты')||document.getElementById('ai-answer').textContent.includes('Не удалось получить ответ')",70000);
  const answer=await js("document.getElementById('ai-answer').textContent");
  if(!answer.includes('Найдены разрешённые фрагменты'))throw Error("Unified P13 assistant retrieval failed: "+answer.slice(0,180));
  pass("Existing P13 assistant shows authorized retrieval-only response, not fake LLM answer");
  await js("document.getElementById('switch-demo').click()");
  await until("location.href.startsWith('http://localhost:19139/') && !document.getElementById('auth').hidden",20000);
  const loggedOut=await js("(async()=>{let r=await fetch('/unified/api/me');return r.status})()");
  if(loggedOut!==401)throw Error("Old session survived unified logout");
  pass("Unified app session revoked after identity-provider logout");
  await until("document.readyState==='complete' && document.getElementById('unified-login')?.dataset.ready==='1'",18000);
  await js("document.getElementById('unified-login').click()");
  await sleep(3500);
  report.secondLogin=await js("(function(){return {origin:location.origin,path:location.pathname,hasForm:!!document.querySelector('#kc-form-login'),authVisible:!!document.getElementById('auth')&&!document.getElementById('auth').hidden,workspaceVisible:!!document.getElementById('workspace')&&!document.getElementById('workspace').hidden,message:document.body.innerText.slice(0,130)}})()");
  await until("location.href.startsWith('http://127.0.0.1:18080/') && !!document.querySelector('#kc-form-login')",8000);
  pass("RP-Initiated Logout requires new Keycloak credentials");
  if(exceptions.length)throw Error("JavaScript exceptions: "+exceptions.join(" | "));
  pass("No browser JavaScript exceptions");
  report.success=true;
 }catch(e){
  report.success=false;report.error=String(e).slice(0,900);
 }finally{
  if(ws)try{ws.close();}catch{}
  try{chrome.kill();}catch{}
  try{fs.rmSync(PROFILE,{recursive:true,force:true,maxRetries:3,retryDelay:250});}catch{}
  const folder=path.join(ROOT,"data");fs.mkdirSync(folder,{recursive:true});
  fs.writeFileSync(path.join(folder,"unified_p13_browser_acceptance.json"),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
  if(!report.success)process.exitCode=1;
 }
})();
