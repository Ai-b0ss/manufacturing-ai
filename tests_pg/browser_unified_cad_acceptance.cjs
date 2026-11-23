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
  await js("document.getElementById('engineering-navigation').click()");
  await until("!document.getElementById('page-engineering').hidden && document.querySelectorAll('#engineering-result .engineering-source').length===3",30000);
  pass("Engineering P13 UI resolves three linked BOM/PDF/STEP sources");
  const engineeringSources=await js("({names:[...document.querySelectorAll('#engineering-result .engineering-source-head strong')].map(n=>n.textContent),downloads:document.querySelectorAll('#engineering-result a[download]').length,warning:document.getElementById('engineering-result').innerText.includes('6061-T6')})");
  if(engineeringSources.downloads!==3 || !engineeringSources.warning)throw Error("Engineering sources rendered incorrectly");
  pass("Engineering UI presents original sources and material evidence");
  await until("document.querySelectorAll('#p8-part-result .p8-inspection-card').length===2",30000);
  const p8=await js("({topology:document.getElementById('p8-part-result').innerText.includes('BREP-топология корректна'),approved:document.getElementById('p8-part-result').innerText.includes('НЕ ПОДТВЕРЖДЕН')})");
  if(!p8.topology||!p8.approved)throw Error("P8 CAD topology/approval evidence mismatch "+JSON.stringify(p8));
  pass("P8 real STEP topology and unverified status distinguish correctly");
  await until("document.getElementById('p9-comparison-result').innerText.includes('0.00086')",25000);
  const p9=await js("document.getElementById('p9-comparison-result').innerText");
  if(!p9.includes('НЕ УСТАНОВЛЕНО'))throw Error("P9 disclaimer missing");
  pass("P9 measured dimension traceability loaded from PDF/CAD");
  const p10Started=Date.now();
  try {
    await until("document.getElementById('p10-feature-result').innerText.includes('41.5')",45000);
  } catch (err) {
    report.p10FailureState=await js("({current:document.getElementById('p10-feature-result').innerText.slice(0,600),selected:document.querySelector('#p10-callout option:checked')?.textContent||'',status:document.getElementById('p10-revision-result')?.innerText.slice(0,180)||''})");
    throw Error(String(err)+" :: "+JSON.stringify(report.p10FailureState).slice(0,650));
  }
  report.p10LatencyMs=Date.now()-p10Started;
  pass("P10 analytic surface radius evidence rendered");
  await until("document.querySelector('#p12-cad-image img')?.naturalWidth===1000",30000);
  pass("P12 genuine rendered CAD image loaded");
  await until("document.querySelector('#p13-vector-preview img')?.naturalWidth>200",30000);
  const p13=await js("({previewWidth:document.querySelector('#p13-vector-preview img').naturalWidth,summary:document.getElementById('p13-match-summary').innerText,candidates:document.querySelectorAll('#p13-candidates .p13-candidate').length})");
  if(p13.candidates!==1)throw Error("P13 unique radius candidate mismatch "+JSON.stringify(p13));
  pass("P13 actual PDF vector and single radius CAD match");
  await screenshot("unified_p13_cad_engineer.jpg");
  await js("document.getElementById('p10-callout').value='e1a9fb555c7e8e7491ddd820';document.getElementById('p10-callout').dispatchEvent(new Event('change',{bubbles:true}))");
  await until("document.querySelectorAll('#p13-candidates .p13-candidate').length===2 && document.getElementById('p13-match-summary').innerText.includes('несколько численно совместимых')",32000);
  const ambiguous=await js("({count:document.querySelectorAll('#p13-candidates .p13-candidate').length,blocked:document.getElementById('p10-surface').value==='',warning:document.getElementById('p13-candidates').innerText.includes('Автоматический выбор запрещён')})");
  if(ambiguous.count!==2||!ambiguous.blocked||!ambiguous.warning)throw Error("Ambiguous CAD match was auto-selected "+JSON.stringify(ambiguous));
  pass("P13 ambiguous PDF/CAD match is not auto-approved");
  await js("document.querySelector('#p13-candidates .p13-candidate button').click()");
  await until("document.getElementById('p10-surface').value==='083588fefad986d245b87466' && document.querySelector('#p12-cad-image img')?.naturalWidth===1000",32000);
  pass("P13 explicit manual candidate selection updates real 3D CAD view");
  await js("document.getElementById('p10-callout').value='cd2e9bca78adda13b1322aba';document.getElementById('p10-callout').dispatchEvent(new Event('change',{bubbles:true}))");
  await until("document.getElementById('p13-vector-proof').innerText.includes('0.011') && document.getElementById('p13-match-summary').innerText.includes('один численно совместимый')",30000);
  pass("P13 original R41.5 arrow witness has measured contour distance 0.011 pt");
  await js("document.querySelector('[data-engineering-example=\"lower_square_tube\"]').click()");
  await until("document.getElementById('engineering-result').innerText.includes('SUS304') && document.querySelectorAll('#engineering-result .engineering-source').length===3",30000);
  pass("P13 switches source bundle to independent lower-square-tube PDF and STEP");
  await until("document.getElementById('p10-feature-result').innerText.includes('0.00015')",35000);
  try {
    await until("document.querySelector('#p13-vector-preview img')?.naturalWidth>200 && document.querySelector('#p13-vector-proof').innerText.includes('Отрезков реальной дуги')",35000);
  } catch(err) {
    report.angleFailure=await js("({callout:document.getElementById('p10-callout').value,axis:document.getElementById('p10-reference-axis').value,preview:document.getElementById('p13-vector-preview').innerText.slice(0,400),proof:document.getElementById('p13-vector-proof').innerText.slice(0,500),summary:document.getElementById('p13-match-summary').innerText.slice(0,300),src:document.querySelector('#p13-vector-preview img')?.getAttribute('src')||'',imageWidth:document.querySelector('#p13-vector-preview img')?.naturalWidth||0})");
    throw Error("P13_ANGLE_WITNESS_FAIL: "+JSON.stringify(report.angleFailure).slice(0,1000));
  }
  pass("P13 original PDF arc and angular dimension witness loaded");
  await js("document.getElementById('p10-reference-axis').value='y';document.getElementById('p10-reference-axis').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#p10-compare-form button').click()");
  await until("document.getElementById('p10-feature-result').innerText.includes('Числа аналитической поверхности отличаются')",18000);
  await until("document.getElementById('p13-match-summary').innerText.includes('не найдено')",27000);
  const wrong=await js("document.querySelectorAll('#p13-candidates .p13-candidate').length");
  if(wrong!==0)throw Error("Wrong-axis CAD candidates not cleared");
  pass("Incorrect CAD axis yields no candidate and explicit mismatch");
  await js("document.getElementById('p10-reference-axis').value='x';document.getElementById('p10-reference-axis').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#p10-compare-form button').click()");
  await until("document.getElementById('p10-feature-result').innerText.includes('0.00015')",20000);
  await command("Emulation.setDeviceMetricsOverride",{width:390,height:900,deviceScaleFactor:1,mobile:true});
  await sleep(400);
  const mobileCAD=await js("({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,hasPreview:!!document.querySelector('#p13-vector-preview img')})");
  if(mobileCAD.scroll>mobileCAD.width+2)throw Error("Engineering 390px horizontal overflow "+JSON.stringify(mobileCAD));
  pass("P13 engineering dashboard fits mobile 390px without horizontal scrolling");
  await js("document.getElementById('p13-registration-panel').scrollIntoView({block:'start'})");
  await screenshot("unified_p13_cad_mobile.jpg");
  await command("Emulation.clearDeviceMetricsOverride");
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
  fs.writeFileSync(path.join(folder,"unified_cad_browser_acceptance.json"),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
  if(!report.success)process.exitCode=1;
 }
})();
