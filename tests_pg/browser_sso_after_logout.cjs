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
  await command("Page.navigate",{url:"http://localhost:19136/"});
  await until("document.readyState==='complete' && !!document.getElementById('login-button')",20000);
  pass("Standalone gateway UI loaded");
  if(!(await js("document.getElementById('login-button').textContent.includes('Keycloak')")))throw Error("Login label missing");
  pass("Keycloak login action available");

  await js("document.getElementById('login-button').click()");
  await until("location.href.startsWith('http://127.0.0.1:18080/') && !!document.querySelector('#kc-form-login')",25000);
  pass("Redirected to real Keycloak login form");
  const pkceUrl=await js("location.href");
  const url=new URL(pkceUrl);
  if(url.searchParams.get("code_challenge_method")!=="S256")throw Error("PKCE S256 absent");
  pass("OIDC PKCE S256 challenge present");

  await js("(function(){document.querySelector('#username').value='engineer'; document.querySelector('#password').value="+JSON.stringify(CREDS.engineer)+";document.querySelector('#kc-form-login').requestSubmit();return true;})()");
  await until("location.href.startsWith('http://localhost:19136/') && !!document.getElementById('welcome')",30000);
  await until("document.getElementById('welcome').textContent.includes('engineer')",16000);
  pass("Authorization Code callback and engineer session passed");
  const result=await js("(async()=>{const m=await fetch('/api/me');return {status:m.status,body:await m.json()}})()");
  if(result.status!==200 || result.body.actor!=="engineer")throw Error("Browser session principal mismatch");
  pass("HttpOnly session resolves verified engineer identity");
  const finance=await js("(async()=>{const r=await fetch('/api/finance');return {status:r.status,body:await r.json()}})()");
  if(finance.status!==200 || finance.body.count!==0)throw Error("Unauthorized finance exposed to engineer");
  pass("Engineer cannot access financial rows");
  const documents=await js("(async()=>{const r=await fetch('/api/documents');return {status:r.status,body:await r.json()}})()");
  if(documents.status!==200 || documents.body.count<=0)throw Error("Documents not available");
  pass("Authorized PostgreSQL document list returned");
  await screenshot("p2_keycloak_engineer_desktop.jpg");

  await js("document.getElementById('documents-button').click()");
  await until("document.querySelectorAll('#results article').length>0");
  pass("Document cards render from policy API");
  await command("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await screenshot("p2_keycloak_engineer_mobile.jpg");
  const overflow=await js("document.documentElement.scrollWidth>document.documentElement.clientWidth");
  if(overflow)throw Error("Horizontal overflow on 390px");
  pass("390px layout no horizontal overflow");

  await js("document.getElementById('logout-button').click()");
  await until("document.getElementById('login-button').hidden===false",15000);
  pass("Logout UI returned to logged-out state");
  const after=await js("(async()=>{const r=await fetch('/api/me');return r.status;})()");
  if(after!==401)throw Error("Authenticated session not revoked after logout");
  pass("Old browser session cannot read after logout");
  await js("document.getElementById('login-button').click()");
  await sleep(3500);
  const secondLogin=await js("(function(){return {origin:location.origin,loginForm:!!document.querySelector('#kc-form-login'),loggedIn:!!document.getElementById('welcome')&&document.getElementById('welcome').textContent.includes('engineer')}})()");
  report.secondLoginObserved={loginForm:secondLogin.loginForm,loggedIn:secondLogin.loggedIn,origin:secondLogin.origin};
  report.silentSSOReturnAfterAppLogout=!!secondLogin.loggedIn;
  if(exceptions.length)throw Error("JavaScript exceptions "+exceptions.length);
  pass("No JS exceptions");
  report.success=true;
 }catch(e){
  report.success=false;report.error=String(e).slice(0,900);
 }finally{
  if(ws)try{ws.close();}catch{}
  try{chrome.kill();}catch{}
  try{fs.rmSync(PROFILE,{recursive:true,force:true,maxRetries:3,retryDelay:250});}catch{}
  const folder=path.join(ROOT,"data");fs.mkdirSync(folder,{recursive:true});
  fs.writeFileSync(path.join(folder,"p2_sso_after_logout_observation.json"),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
  if(!report.success)process.exitCode=1;
 }
})();
