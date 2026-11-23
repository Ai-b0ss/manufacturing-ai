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

  await until("document.querySelectorAll('#p8-part-result .p8-inspection-card').length===2",18000);
  report.p8Initial=await js("({topology:document.getElementById('p8-part-result').innerText.includes('BREP-топология корректна'),noApproval:document.getElementById('p8-part-result').innerText.includes('НЕ ПОДТВЕРЖДЕН'),catalog:document.getElementById('p8-source-select').options.length})");
  await js("document.getElementById('p8-source-check').click()");
  await until("document.getElementById('p8-source-result').innerText.includes('1842300') && document.getElementById('p8-source-result').innerText.includes('H')",12000);
  report.passport=await js("({text:document.getElementById('p8-source-result').innerText.slice(0,450),history:document.querySelectorAll('#p8-source-result .p8-history p').length})");
  await js("document.getElementById('p8-compare-button').click()");
  await until("document.querySelectorAll('#p8-compare-result .p8-compare-side').length===2",14000);
  report.releaseComparison=await js("({text:document.getElementById('p8-compare-result').innerText.slice(0,600),sides:document.querySelectorAll('#p8-compare-result .p8-compare-side').length,codes:[...document.querySelectorAll('#p8-compare-result .p8-compare-side')].map(x=>x.innerText.includes('BCN3DSigma-M01C01.0P01I'))})");
  await js("document.getElementById('p8-notice-button').click()");
  await until("document.getElementById('p8-notice-result').innerText.includes('БВАИ.107-2012')",12000);
  report.notice=await js("document.getElementById('p8-notice-result').innerText.slice(0,500)");
  await js("document.getElementById('p8-part-panel').scrollIntoView({block:'start'})");
  await sleep(160);
  await screenshot('p8_engineering_desktop.jpg');


  await js("document.querySelector('[data-engineering-example=\"040M07C01.0P026D\"]').click()");
  await until("document.querySelectorAll('#engineering-result .engineering-source').length===0 && document.getElementById('engineering-result').innerText.includes('Невозможно подтвердить связь')");
  report.negative=await js("document.getElementById('engineering-result').innerText.slice(0,200)");
  await js("document.querySelector('[data-engineering-example=\"lower_square_tube\"]').click()");
  await until("document.getElementById('engineering-result').innerText.includes('SUS304') && document.querySelectorAll('#engineering-result .engineering-source').length===3");
  for(const width of [1440,1024,768,390]){
    await command('Emulation.setDeviceMetricsOverride',{width,height:950,deviceScaleFactor:1,mobile:width<=768});
    await sleep(250);
    report['w'+width]=await js("({viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,sources:document.querySelectorAll('#engineering-result .engineering-source').length})");
    if(width===390){
      report.mobileOverflow=await js("([...document.querySelectorAll('*')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.right>innerWidth+3&&getComputedStyle(e).visibility!=='hidden'}).slice(0,20).map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,65),right:Math.round(e.getBoundingClientRect().right),scroll:e.scrollWidth})))");
      report.mobileLayout=await js("(['html','body','.app-shell','.sidebar','.nav','.app-body','.main-content','#page-engineering','.p8-panel','#p8-source-select'].map(q=>{const e=document.querySelector(q);if(!e)return null;const r=e.getBoundingClientRect();return {q,width:Math.round(r.width),left:Math.round(r.left),right:Math.round(r.right),scroll:e.scrollWidth,overflow:getComputedStyle(e).overflowX}}))");
      report.mobileIntrinsicOverflow=await js("([...document.querySelectorAll('#page-engineering *')].filter(e=>e.clientWidth>0&&e.scrollWidth>e.clientWidth+10).map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,60),client:e.clientWidth,scroll:e.scrollWidth,excess:e.scrollWidth-e.clientWidth})).sort((a,b)=>b.excess-a.excess).slice(0,25))");
      await screenshot('p8_engineering_mobile.jpg');
    }
  }
  report.exceptions=exceptions;
  report.ok=report.p8Initial.topology && report.p8Initial.catalog>=19 && report.passport.history>=2 && report.releaseComparison.sides===2 && report.releaseComparison.codes.some(Boolean) && report.notice.toLowerCase().includes('не подтверждены') && report.engineer.downloads===3
   && report.engineer.filenames.includes('focuser_drawtube.pdf')
   && report.engineer.filenames.includes('focuser_drawtube.STEP')
   && report.engineer.historical.includes('318.03-2009')
   && report.engineer.text.includes('6061-T6')
   && [1440,1024,768,390].every(width=>{
     const item=report['w'+width];return item.scroll<=item.viewport+2 && item.sources===3;
   })
   && exceptions.length===0;
  if(!report.ok)throw Error('P8 browser acceptance failed');
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
