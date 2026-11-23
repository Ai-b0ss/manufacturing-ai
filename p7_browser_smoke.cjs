/* P7 isolated Chrome CDP acceptance; no global browser profile. */
'use strict';
const fs=require('fs'),os=require('os'),path=require('path'),cp=require('child_process');
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'factory-p7-'));
const port=Number(process.env.P7_BROWSER_PORT||19128);
const chrome=cp.spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
 ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
  '--remote-debugging-port=0','--window-size=1440,1000','--user-data-dir='+profile,
  'about:blank'],{stdio:'ignore',windowsHide:true});
let ws,seq=1,pending=new Map(),exceptions=[];
const sleep=n=>new Promise(resolve=>setTimeout(resolve,n));
async function command(method,params={}){
 const id=seq++;
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method))},15000);
  pending.set(id,{resolve,reject,timer});
  ws.send(JSON.stringify({id,method,params}));
 });
}
async function js(expression){
 const response=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
 if(response.result.exceptionDetails)throw Error(JSON.stringify(response.result.exceptionDetails));
 return response.result.result.value;
}
async function until(expression,timeout=12000){
 const started=Date.now();
 while(Date.now()-started<timeout){
  try{if(await js(expression))return true}catch{}
  await sleep(130);
 }
 throw Error('Browser condition timeout: '+expression);
}
async function screenshot(name){
 const image=await command('Page.captureScreenshot',{format:'jpeg',quality:84,captureBeyondViewport:false});
 const folder=path.join(__dirname,'data');fs.mkdirSync(folder,{recursive:true});
 fs.writeFileSync(path.join(folder,name),Buffer.from(image.result.data,'base64'));
}
(async()=>{
 const report={};
 try{
  let debuggerPort=0;
  for(let tries=0;tries<75;tries++){
   const file=path.join(profile,'DevToolsActivePort');
   if(fs.existsSync(file)){
    debuggerPort=Number(fs.readFileSync(file,'utf8').split(/\r?\n/)[0]);
    if(debuggerPort)break;
   }
   await sleep(100);
  }
  if(!debuggerPort)throw Error('Chrome debugging port not found');
  const tabs=await(await fetch('http://127.0.0.1:'+debuggerPort+'/json')).json();
  if(!tabs.length)throw Error('No CDP tabs');
  ws=new WebSocket(tabs[0].webSocketDebuggerUrl.replace('localhost','127.0.0.1'));
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
  ws.onmessage=event=>{
   const data=JSON.parse(event.data);
   if(data.method==='Runtime.exceptionThrown')
    exceptions.push(data.params?.exceptionDetails?.text||'Unknown JS error');
   if(pending.has(data.id)){
    const task=pending.get(data.id);pending.delete(data.id);clearTimeout(task.timer);
    data.error?task.reject(Error(data.error.message)):task.resolve(data);
   }
  };
  await command('Page.enable');await command('Runtime.enable');
  await command('Page.navigate',{url:'http://127.0.0.1:'+port+'/'});
  await until("document.readyState==='complete' && !!document.querySelector('[data-demo-role=technologist]')");
  await js("document.querySelector('[data-demo-role=technologist]').click()");
  await until("!document.getElementById('workspace').hidden");
  report.technologist=await js("(async()=>{const nav=document.getElementById('engineering-navigation');const response=await fetch('/api/engineering/ask?q=focuser_drawtube');return {navHidden:nav.hidden,code:response.status}})()");
  if(!report.technologist.navHidden || report.technologist.code!==403)
    throw Error('Engineering ACL failed for technologist');

  await js("document.getElementById('switch-demo').click()");
  await until("!document.getElementById('auth').hidden && document.getElementById('workspace').hidden");
  await js("document.querySelector('[data-demo-role=engineer]').click()");
  await until("!document.getElementById('workspace').hidden && !document.getElementById('engineering-navigation').hidden");
  await js("document.getElementById('engineering-navigation').click()");
  await until("!document.getElementById('page-engineering').hidden && document.querySelectorAll('#engineering-result .engineering-source').length===3",18000);
  report.engineer=await js("({text:document.getElementById('engineering-result').innerText.slice(0,900),filenames:[...document.querySelectorAll('#engineering-result .engineering-source-head strong')].map(x=>x.textContent),downloads:document.querySelectorAll('#engineering-result a[download]').length,historical:document.getElementById('engineering-historical').innerText.slice(0,280),stats:document.getElementById('engineering-summary').innerText})");
  await screenshot('p7_engineering_desktop.jpg');

  await js("document.querySelector('[data-engineering-example=\"040M07C01.0P026D\"]').click()");
  await until("document.querySelectorAll('#engineering-result .engineering-source').length===0 && document.getElementById('engineering-result').innerText.includes('Невозможно подтвердить связь')");
  report.negative=await js("document.getElementById('engineering-result').innerText.slice(0,200)");
  await js("document.querySelector('[data-engineering-example=\"lower_square_tube\"]').click()");
  await until("document.getElementById('engineering-result').innerText.includes('SUS304') && document.querySelectorAll('#engineering-result .engineering-source').length===3");
  for(const width of [1440,1024,768,390]){
    await command('Emulation.setDeviceMetricsOverride',{width,height:950,deviceScaleFactor:1,mobile:width<=768});
    await sleep(250);
    report['w'+width]=await js("({viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,sources:document.querySelectorAll('#engineering-result .engineering-source').length})");
    if(width===390)await screenshot('p7_engineering_mobile.jpg');
  }
  report.exceptions=exceptions;
  report.ok=report.engineer.downloads===3
   && report.engineer.filenames.includes('focuser_drawtube.pdf')
   && report.engineer.filenames.includes('focuser_drawtube.STEP')
   && report.engineer.historical.includes('318.03-2009')
   && report.engineer.text.includes('6061-T6')
   && [1440,1024,768,390].every(width=>{
     const item=report['w'+width];return item.scroll<=item.viewport+2 && item.sources===3;
   })
   && exceptions.length===0;
  if(!report.ok)throw Error('P7 browser acceptance failed');
  console.log(JSON.stringify(report));
 }catch(error){
  console.error(JSON.stringify({...report,ok:false,error:String(error),exceptions}));
  process.exitCode=1;
 }finally{
  if(ws)ws.close();
  chrome.kill();
  try{fs.rmSync(profile,{recursive:true,force:true})}catch{}
 }
})();
