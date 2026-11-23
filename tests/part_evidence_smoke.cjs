const fs=require('fs'),os=require('os'),path=require('path'),cp=require('child_process');
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'factory-design-'));
const chrome=cp.spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
 ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-port=19149',
  '--window-size=1440,1000','--user-data-dir='+profile,'about:blank'],{stdio:'ignore',windowsHide:true});
let ws,n=1,pending=new Map(),exceptions=[];
const sleep=t=>new Promise(r=>setTimeout(r,t));
async function send(method,params={}){
 const id=n++;
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP '+method+' timeout'))},9000);
  pending.set(id,{resolve,reject,timer});
  ws.send(JSON.stringify({id,method,params}));
 });
}
async function js(expression){
 const res=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
 if(res.result.exceptionDetails)throw Error('Evaluate '+JSON.stringify(res.result.exceptionDetails));
 return res.result.result.value;
}
async function until(expression,timeout=9000){
 const t=Date.now();
 while(Date.now()-t<timeout){try{if(await js(expression))return}catch(e){}await sleep(110)}
 throw Error('Wait timed out '+expression);
}
async function snap(name){
 const r=await send('Page.captureScreenshot',{format:'jpeg',quality:78,captureBeyondViewport:false});
 fs.writeFileSync(path.join(__dirname,'..','.preview',name),Buffer.from(r.result.data,'base64'));
}
(async()=>{
 const result={};
 try{
  let tabs;
  for(let i=0;i<70;i++){
   try{tabs=await(await fetch('http://127.0.0.1:19149/json')).json();if(tabs.length)break}catch(e){}
   await sleep(110);
  }
  if(!tabs)throw Error('Chrome CDP unreachable');
  ws=new WebSocket(tabs[0].webSocketDebuggerUrl.replace('localhost','127.0.0.1'));
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
  ws.onmessage=e=>{
   const msg=JSON.parse(e.data),p=pending.get(msg.id);
   if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params?.exceptionDetails?.text||'JavaScript exception');
   if(p){clearTimeout(p.timer);pending.delete(msg.id);msg.error?p.reject(Error(msg.error.message)):p.resolve(msg)}
  };
  await send('Page.enable');await send('Runtime.enable');
  await send('Page.navigate',{url:'http://127.0.0.1:19127/'});
  await until("document.readyState==='complete' && !!document.querySelector('[data-demo-role=technologist]')");
  await js("document.querySelector('[data-demo-role=technologist]').click()");
  await until("!document.getElementById('workspace').hidden");
  await js("document.querySelector('[data-page=part]').click()");
  await until("!document.getElementById('page-part').hidden && document.querySelector('#part-result .part-result-card')");
  result.workshop=await js("({text:document.getElementById('part-result').innerText,sourceCount:document.querySelectorAll('.part-evidence-item').length})");
  await js("document.getElementById('switch-demo').click()");
  await until("!document.getElementById('auth').hidden && document.getElementById('workspace').hidden");
  await js("document.querySelector('[data-demo-role=engineer]').click()");
  await until("!document.getElementById('workspace').hidden");
  await js("document.querySelector('[data-page=part]').click()");
  await until("!document.getElementById('page-part').hidden && document.querySelectorAll('#part-result .part-evidence-open').length===2");
  result.newLot=await js("({text:document.getElementById('part-result').innerText,sourceCount:document.querySelectorAll('#part-result .part-evidence-open').length})");
  await snap('p6-engineering-new-lot.jpg');
  await js("document.querySelectorAll('#part-result .part-evidence-open')[1].click()");
  await until("document.getElementById('document-modal').open");
  result.openedSource=await js("document.getElementById('modal-title').innerText");
  await js("document.getElementById('close-modal').click()");
  await until("!document.getElementById('document-modal').open");
  await js("document.getElementById('lot-id').value='LOT-2025-12';document.getElementById('part-form').requestSubmit()");
  await until("document.getElementById('part-result').innerText.includes('Партию нужно проверить')");
  result.oldLot=await js("document.getElementById('part-result').innerText");
  await snap('p6-engineering-old-lot.jpg');
  await js("document.getElementById('part-code').value='UNKNOWN-999';document.getElementById('part-form').requestSubmit()");
  await until("document.getElementById('part-result').innerText.includes('Деталь не найдена')");
  result.unknown=await js("({text:document.getElementById('part-result').innerText,sourceCount:document.querySelectorAll('#part-result .part-evidence-item').length})");
  await js("document.getElementById('part-code').value='АБВГ.712345.004-02';document.getElementById('lot-id').value='LOT-2026-10';document.getElementById('part-form').requestSubmit()");
  await until("document.querySelectorAll('#part-result .part-evidence-item').length===2");
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:850,deviceScaleFactor:1,mobile:true});
  await sleep(330);
  result.mobile=await js("({scroll:document.documentElement.scrollWidth,viewport:document.documentElement.clientWidth,actionCount:document.querySelectorAll('#part-result .part-evidence-open').length})");
  await snap('p6-engineering-mobile.jpg');
  result.errors=exceptions;
  result.ok=result.workshop.sourceCount===0 &&
            !result.workshop.text.includes('ИИ-017') &&
            result.workshop.text.includes('не подтверждена') &&
            result.newLot.sourceCount===2 &&
            result.newLot.text.includes('02') &&
            result.openedSource.includes('ИИ-017') &&
            result.oldLot.includes('ДОРАБОТАТЬ') &&
            result.oldLot.includes('Партию нужно проверить') &&
            result.unknown.sourceCount===0 &&
            result.mobile.scroll<=result.mobile.viewport+2 &&
            result.mobile.actionCount===2 &&
            exceptions.length===0;
  if(!result.ok)throw Error('Engineering review browser assertions failed');
  console.log(JSON.stringify(result));
 }catch(error){console.error(JSON.stringify({...result,error:String(error)}));process.exitCode=1}
 finally{
   if(ws)ws.close();
   chrome.kill();
   try{fs.rmSync(profile,{recursive:true,force:true})}catch{}
 }
})();
