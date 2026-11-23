// Headless Chrome regression for real CAD DOM, without production services.
'use strict';
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const cp=require('node:child_process'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..');
const CHROME='C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PROFILE=fs.mkdtempSync(path.join(os.tmpdir(),'factory-uxqa-dom-'));
const src=fs.readFileSync(path.join(ROOT,'app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'index.html'),'utf8');
assert(html.includes('id="private-kit-version-target"'));
assert(html.includes('id="private-kit-target-hint"'));
const start=src.indexOf('function normalizedKitPart(part){');
const end=src.indexOf('function privateKitReset(){',start);
assert(start>0&&end>start);
const helpers=src.slice(start,end);
const chrome=cp.spawn(CHROME,[
  '--headless=new','--no-first-run','--no-default-browser-check',
  '--disable-gpu','--disable-extensions','--remote-debugging-port=0',
  '--window-size=1120,720','--user-data-dir='+PROFILE,'about:blank'
],{windowsHide:true,stdio:'ignore'});
let ws,seq=1;
const outstanding=new Map();
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function command(method,params={}){
  return new Promise((resolve,reject)=>{
    const id=seq++;
    const timer=setTimeout(()=>{outstanding.delete(id);reject(new Error('CDP timeout '+method))},13000);
    outstanding.set(id,{resolve,reject,timer});
    ws.send(JSON.stringify({id,method,params}));
  });
}
async function js(expression){
  const r=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  if(r.exceptionDetails)throw new Error('Browser JS exception '+JSON.stringify(r.exceptionDetails).slice(0,550));
  return r.result.value;
}
async function until(expr){
  for(let i=0;i<65;i++){
    try{if(await js(expr))return}catch{}
    await sleep(120);
  }
  throw new Error('Chrome condition timeout '+expr);
}
(async()=>{
  const stages=[];
  let port=0;
  for(let i=0;i<110;i++){
    const f=path.join(PROFILE,'DevToolsActivePort');
    if(fs.existsSync(f)){
      port=Number(fs.readFileSync(f,'utf8').split(/\r?\n/)[0]);
      if(port)break;
    }
    await sleep(100);
  }
  if(!port)throw Error('Chrome DevTools unavailable');
  const tabs=await(await fetch('http://127.0.0.1:'+port+'/json')).json();
  const tab=tabs.find(t=>t.type==='page');
  if(!tab)throw Error('No Chrome page');
  ws=new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{
    ws.addEventListener('open',resolve,{once:true});
    ws.addEventListener('error',reject,{once:true});
  });
  ws.addEventListener('message',ev=>{
    const packet=JSON.parse(ev.data);
    if(packet.id&&outstanding.has(packet.id)){
      const x=outstanding.get(packet.id);outstanding.delete(packet.id);clearTimeout(x.timer);
      packet.error?x.reject(Error(packet.error.message)):x.resolve(packet.result);
    }
  });
  await command('Runtime.enable');
  await command('Page.enable');
  const body='<!doctype html><html lang="ru"><meta charset="utf-8"><title>CAD QA</title>'+
    '<body><label for="private-kit-part">Деталь</label><input id="private-kit-part" value="">'+
    '<label for="private-kit-version-target">Комплект</label>'+
    '<select id="private-kit-version-target"><option value="">Новый комплект</option>'+
    '<option value="kit-a">focuser_drawtube</option><option value="kit-b">lower_square_tube</option>'+
    '</select><p id="private-kit-target-hint"></p></body></html>';
  const navigation=await command('Page.navigate',{url:'data:text/html;charset=utf-8,'+encodeURIComponent(body)});
  await sleep(400);
  const diagnostic=await js("({url:location.href.slice(0,100),ready:document.readyState,tag:document.body?.firstElementChild?.tagName,hasHint:!!document.getElementById('private-kit-target-hint')})");
  console.log('NAVIGATION_DIAGNOSTIC',JSON.stringify({navigation,diagnostic}).slice(0,550));
  await until("document.readyState==='complete' && !!document.getElementById('private-kit-target-hint')");
  stages.push('Actual headless Chrome DOM rendered');
  const init='(() => {const $=id=>document.getElementById(id);'+
    'let privateKitReport=null;'+
    "let savedKits=[{kit_id:'kit-a',part:'focuser_drawtube',latest_revision:1},"+
                  "{kit_id:'kit-b',part:'lower_square_tube',latest_revision:8}];"+
    helpers+
    'window.qa={sync:syncPrivateKitTarget,report:x=>privateKitReport=x};})()';
  await js(init);
  await js("window.qa.report({part:' FOCUSER_DRAWTUBE '});window.qa.sync()");
  let view=await js("({value:document.getElementById('private-kit-version-target').value,hint:document.getElementById('private-kit-target-hint').textContent})");
  assert.equal(view.value,'kit-a');assert(view.hint.includes('№ 2'));
  stages.push('Matching part selects revision 2 instead of duplicate');
  await js("document.getElementById('private-kit-version-target').value='kit-b';window.qa.sync()");
  view=await js("document.getElementById('private-kit-version-target').value");
  assert.equal(view,'kit-a');
  stages.push('Wrong-part kit selection corrected');
  await js("window.qa.report({part:'New_Part'});window.qa.sync()");
  view=await js("({value:document.getElementById('private-kit-version-target').value,hint:document.getElementById('private-kit-target-hint').textContent})");
  assert.equal(view.value,'');assert(view.hint.includes('новый комплект'));
  stages.push('Unknown part proposes new kit');
  await js("document.getElementById('private-kit-version-target').disabled=true;window.qa.sync()");
  view=await js("document.getElementById('private-kit-target-hint').textContent");
  assert(view.includes('право записи'));
  stages.push('Read-only mode explains absence of permission');
  const report={chrome_used:true,passed:stages.length,success:true,stages,backend_touched:false};
  const out=path.join(ROOT,'data','uxqa_dom_report.json');
  fs.mkdirSync(path.dirname(out),{recursive:true});
  fs.writeFileSync(out,JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
})().catch(e=>{console.error(e.stack||e);process.exitCode=1})
.finally(()=>{
  for(const item of outstanding.values())clearTimeout(item.timer);
  if(ws)ws.close();
  chrome.kill();
  try{fs.rmSync(PROFILE,{recursive:true,force:true,maxRetries:4,retryDelay:150})}catch{}
});
