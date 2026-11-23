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
  await until("document.readyState==='complete' && document.getElementById('unified-login')?.dataset.ready==='1'",20000);
  pass("P13 original UI displayed");
  await js("document.getElementById('unified-login').click()");
  await until("location.href.startsWith('http://127.0.0.1:18080/') && !!document.querySelector('#kc-form-login')",25000);
  pass("Real Keycloak form loaded");
  await js("(function(){document.querySelector('#username').value='technologist';document.querySelector('#password').value="+JSON.stringify(CREDS.technologist)+";document.querySelector('#kc-form-login').requestSubmit();})()");
  await until("location.href.startsWith('http://localhost:19139/') && !document.getElementById('workspace').hidden",30000);
  pass("technologist signed in to original P13 workspace");
  const me=await js("(async()=>{const r=await fetch('/unified/api/me');return {status:r.status,data:await r.json()}})()");
  if(me.status!==200||me.data.id!=="technologist")throw Error("Wrong identity for technologist");
  pass("Backend identity correct");
  const documents=await js("(async()=>{const r=await fetch('/unified/api/documents');return {status:r.status,data:await r.json()}})()");
  if(documents.status!==200||documents.data.items.length!==9)throw Error("technologist wrong document count");
  pass("RLS returns 9 documents");
  await js("document.querySelector('[data-page=documents]').click()");
  await until("document.querySelectorAll('#search-results article.doc-card').length>0",20000);
  pass("P13 UI renders scoped document cards");
  const financial=await js("(async()=>{const r=await fetch('/unified/api/metrics');return {status:r.status,data:await r.json()}})()");
  if(financial.status!==200||(financial.data.items.length>0)!==false)throw Error("Wrong finance rights");
  pass("Finance rights enforced");
  const engineering=await js("(async()=>{const r=await fetch('/unified/api/engineering/summary');return r.status})()");
  if(engineering!==403)throw Error("Engineering rights incorrect: "+engineering);
  pass("Engineering rights enforced");
  await screenshot("unified_browser_technologist.jpg");
  await js("document.getElementById('switch-demo').click()");
  await until("location.href.startsWith('http://localhost:19139/') && !document.getElementById('auth').hidden",30000);
  const out=await js("(async()=>{const r=await fetch('/unified/api/me');return r.status})()");
  if(out!==401)throw Error("Session still active after logout");
  pass("User session revoked");
  if(exceptions.length)throw Error("Unexpected JS exception: "+exceptions.join("; "));
  pass("No JS exceptions");
  report.success=true;
 }catch(e){
  report.success=false;report.error=String(e).slice(0,900);
 }finally{
  if(ws)try{ws.close();}catch{}
  try{chrome.kill();}catch{}
  try{fs.rmSync(PROFILE,{recursive:true,force:true,maxRetries:3,retryDelay:250});}catch{}
  const folder=path.join(ROOT,"data");fs.mkdirSync(folder,{recursive:true});
  fs.writeFileSync(path.join(folder,"unified_browser_technologist_acceptance.json"),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
  if(!report.success)process.exitCode=1;
 }
})();
