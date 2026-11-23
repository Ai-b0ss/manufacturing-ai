"use strict";
/* One actual user flow in real Chrome: select 3 new files, analyze, export. */
const fs=require("fs"),os=require("os"),path=require("path"),cp=require("child_process");
const ROOT=path.resolve(__dirname,".."),BASE="http://localhost:19137";
const PROFILE=fs.mkdtempSync(path.join(os.tmpdir(),"private-cad-ui-"));
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
 const outcome={test:"private_cad_three_file_browser",passed:[],errors};
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
  pass("CAD new-file form is visible in Unified application");
  await js("document.querySelector('#unified-login').click()");
  await until("location.href.startsWith('http://127.0.0.1:18080/')&&!!document.querySelector('#kc-form-login')",20000);
  const u=new URL(await js("location.href"));
  if(u.searchParams.get("client_id")!=="factory-web-audit"||
     u.searchParams.get("code_challenge_method")!=="S256")throw Error("Not protected OIDC PKCE");
  await js("(function(){document.querySelector('#username').value='engineer';document.querySelector('#password').value="+JSON.stringify(PASSWORDS.engineer)+";document.querySelector('#kc-form-login').requestSubmit()})()");
  await until("location.href.startsWith('"+BASE+"/')&&!document.querySelector('#workspace').hidden",28000);
  await js("document.querySelector('[data-page=engineering]').click()");
  await until("!document.querySelector('#page-engineering').hidden",20000);
  pass("Engineer authenticated to protected engineering workspace");
  await selectFile("#private-kit-bom","materials_bom.csv");
  await selectFile("#private-kit-pdf","focuser_drawtube.pdf");
  await selectFile("#private-kit-step","focuser_drawtube.STEP");
  await js("document.getElementById('private-kit-part').value='focuser_drawtube'");
  const files=await js("['private-kit-bom','private-kit-pdf','private-kit-step'].map(id=>document.getElementById(id).files[0]?.name)");
  if(!files.every(Boolean))throw Error("Native file input selection failed");
  pass("Native Chrome file selectors loaded three original user files");
  await js("document.querySelector('#private-kit-form').requestSubmit()");
  await until("!document.querySelector('#private-kit-export').disabled||document.querySelector('#private-kit-result').textContent.includes('Не удалось проверить')",47000);
  const report=await js("({text:document.querySelector('#private-kit-result').textContent.slice(0,3500),cards:document.querySelectorAll('.private-kit .kit-report-file').length})");
  if(report.cards!==3||!report.text.includes("R41.5")||
     !report.text.includes("114")||!report.text.includes("производство не разрешено"))
    throw Error("CAD report invalid "+JSON.stringify(report).slice(0,850));
  pass("New real STEP geometry, BOM material and PDF R41.5 are shown together");
  await js("document.querySelector('#private-kit-export').click()");
  let filename;
  for(let i=0;i<45;i++){
   const items=fs.readdirSync(PROFILE).filter(x=>x.startsWith("cad-check-")&&x.endsWith(".md"));
   if(items.length){filename=path.join(PROFILE,items[0]);break}
   await sleep(150);
  }
  if(!filename)throw Error("No Markdown report downloaded");
  const document=fs.readFileSync(filename,"utf8");
  if(!document.includes("R41.5")||
     (document.match(/SHA-256:/g)||[]).length!==3||
     !document.includes("НЕ ЯВЛЯЕТСЯ РАЗРЕШЕНИЕМ НА ИЗГОТОВЛЕНИЕ"))
    throw Error("Export missing source evidence/restriction");
  pass("Actual Markdown export keeps 3 original SHA-256 identifiers and warnings");
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
  fs.writeFileSync(path.join(ROOT,"data","private_cad_browser_acceptance.json"),JSON.stringify(outcome,null,2));
  console.log(JSON.stringify(outcome));
  if(!outcome.success)process.exitCode=1;
 }
})();
