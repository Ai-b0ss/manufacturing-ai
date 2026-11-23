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
  for(let i=0;i<60;i++){
   try{tabs=await(await fetch('http://127.0.0.1:19149/json')).json();if(tabs.length)break}catch(e){}
   await sleep(140)
  }
  if(!tabs)throw Error('Chrome CDP failed');
  ws=new WebSocket(tabs[0].webSocketDebuggerUrl.replace('localhost','127.0.0.1'));
  await new Promise((res,rej)=>{ws.onopen=res;ws.onerror=rej});
  ws.onmessage=e=>{
   const m=JSON.parse(e.data);
   if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params?.exceptionDetails?.text||'runtime error');
   const p=pending.get(m.id);
   if(p){clearTimeout(p.timer);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m)}
  };
  await send('Page.enable');await send('Runtime.enable');
  await send('Page.navigate',{url:'http://127.0.0.1:19127/'});
  await until("document.readyState==='complete' && !!document.querySelector('[data-demo-role=technologist]')");
  await snap('login-1440.jpg');
  await js("document.querySelector('[data-demo-role=technologist]').click()");
  await until("!document.getElementById('workspace').hidden");
  await js("document.querySelector('[data-page=assistant]').click()");
  await until("!document.getElementById('page-assistant').hidden");
  await js("document.getElementById('ai-demo-run').click()");
  await until("document.querySelector('#ai-answer .ai-answer-text')!==null && document.querySelector('#ai-sources .ai-source')!==null",65000);
  await sleep(150);
  await js("window.scrollTo(0,0)");
  await sleep(150);
  await snap('assistant-top-1440.jpg');
  await snap('assistant-1440.jpg');
  await js("document.getElementById('ai-answer').scrollIntoView({block:'start'})");
  await sleep(180);
  await snap('answer-1440.jpg');
  result.answer=await js("document.querySelector('.ai-answer-text').innerText");
  result.uploadFeedback=await js("document.getElementById('ai-upload-result').innerText");
  result.autoQuestion=await js("document.getElementById('ai-question').value");
  result.ocrSource=await js("[...document.querySelectorAll('#ai-sources .ai-source-head')].some(x=>x.innerText.includes('OCR') && x.innerText.includes('Страница 2'))");

  result.sourceCount=await js("document.querySelectorAll('#ai-sources .ai-source').length");
  result.fontFace=await js(`(async()=>{await document.fonts.ready;return {family:getComputedStyle(document.body).fontFamily,sansLoaded:document.fonts.check("700 17px 'IBM Plex Sans'",'Цех.Знания'),monoLoaded:document.fonts.check("500 14px 'IBM Plex Mono'",'123-456')}})()`);
  result.iconRender=await js("(()=>{let icons=[...document.querySelectorAll('#workspace .ui-icon')];let visible=icons.filter(x=>x.getClientRects().length>0 && getComputedStyle(x).display!=='none');let measurements=visible.map(x=>({bbox:x.getBBox().width,href:x.querySelector('use')?.getAttribute('href')}));return {total:icons.length,visible:visible.length,successful:measurements.filter(x=>x.bbox>1).length,failed:measurements.filter(x=>x.bbox<=1)}})()");

  result.stopDisabled=await js("document.getElementById('ai-stop').disabled");
  await js("window.scrollTo(0,0)");
  result.desktop=await js("({width:innerWidth,doc:document.documentElement.scrollWidth,viewport:document.documentElement.clientWidth,hero:document.querySelector('.assistant-head').getBoundingClientRect().height,bodyFont:getComputedStyle(document.body).fontSize,answerFont:getComputedStyle(document.querySelector('.ai-answer-text')||document.body).fontSize,ctaBg:getComputedStyle(document.getElementById('ai-ask')).backgroundColor})");
  for(const width of [1280,1024,768,390]){
    await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<=768});
    await sleep(400);
    await snap('assistant-'+width+'.jpg');
    result[width]=await js("({viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,chatWidth:Math.round(document.querySelector('.ai-chat').getBoundingClientRect().width),aiVisible:!document.querySelector('.ai-chat').hidden,buttonHeight:Math.round(document.getElementById('ai-ask').getBoundingClientRect().height),sidebarVisible:!!document.querySelector('.sidebar')})");
    if(width===1024)result.overflow=await js("(()=>{const w=document.documentElement.clientWidth;return [...document.querySelectorAll('body *')].filter(x=>{const v=x.getBoundingClientRect();return v.width>0 && v.right>w+1 && getComputedStyle(x).position!=='fixed'}).slice(0,16).map(x=>({tag:x.tagName,cls:x.className?.baseVal??x.className,rect:Math.round(x.getBoundingClientRect().right)}))})()");
    if(width===1024)result.scrollOffenders=await js("(()=>{return [...document.querySelectorAll('body,body *')].filter(x=>x.clientWidth>0 && x.scrollWidth>x.clientWidth+2).slice(0,18).map(x=>({tag:x.tagName,id:x.id,className:typeof x.className==='string'?x.className.slice(0,70):'',client:x.clientWidth,scroll:x.scrollWidth}))})()");


  }
  await send('Emulation.clearDeviceMetricsOverride');
  await js("window.scrollTo(0,0);document.getElementById('ai-upload-result').replaceChildren();document.getElementById('ai-answer').replaceChildren();document.getElementById('ai-answer').hidden=true;document.getElementById('ai-demo-run').click()");
  await until("document.getElementById('ai-upload-result').innerText.includes('Этот файл уже загружен')",55000);
  await until("document.querySelector('#ai-answer .ai-answer-text')!==null && document.querySelector('#ai-answer .ai-answer-text').innerText.includes('25 бар')",55000);
  result.duplicateFeedback=await js("document.getElementById('ai-upload-result').innerText");
  result.repeatAnswer=await js("document.querySelector('#ai-answer .ai-answer-text').innerText");
  result.logoAudit=await js("({marks:[...document.querySelectorAll('.brand-mark')].map(x=>({loaded:x.complete&&x.naturalWidth>0,src:x.getAttribute('src')})),favicon:document.querySelector('link[rel=icon]')?.getAttribute('href')})");
  result.assetAudit=await js("({external:performance.getEntriesByType('resource').filter(x=>new URL(x.name).origin!==location.origin).map(x=>x.name),fontStylesheets:[...document.styleSheets].some(x=>x.href?.endsWith('/assets/font-face.css')),iconSource:!!document.querySelector('use[href^=\"/assets/icons.svg#\"]')})");
  await js("document.querySelector('[data-page=documents]').click()");
  await until("!document.getElementById('page-documents').hidden && document.getElementById('search-results').children.length>0",11000);
  result.documentsNavigation=await js("!document.getElementById('page-documents').hidden && document.getElementById('search-results').children.length>0");
  await js("document.querySelector('[data-page=part]').click()");
  await until("!document.getElementById('page-part').hidden");
  result.partNavigation=await js("!document.getElementById('page-part').hidden");
  await js("document.querySelector('[data-page=home]').click()");
  await until("!document.getElementById('page-home').hidden");
  result.homeNavigation=await js("!document.getElementById('page-home').hidden");
  result.errors=exceptions;
  const all=[{scroll:result.desktop.doc,viewport:result.desktop.viewport},...[1280,1024,768,390].map(k=>result[k])];
  result.ok=all.every(r=>r.scroll<=r.viewport+2)&&exceptions.length===0&&result.sourceCount>=1&&result.stopDisabled&&result.ocrSource&&result.autoQuestion.includes('Н-125')&&result.duplicateFeedback.includes('Этот файл уже загружен')&&result.repeatAnswer.includes('25 бар')&&result.iconRender.successful>=6 && result.iconRender.successful===result.iconRender.visible&&result.fontFace.sansLoaded&&result.fontFace.monoLoaded&&result.assetAudit.external.length===0&&result.assetAudit.fontStylesheets&&result.assetAudit.iconSource&&result.documentsNavigation&&result.partNavigation&&result.homeNavigation&&result.logoAudit.marks.length===2&&result.logoAudit.marks.every(x=>x.loaded)&&result.logoAudit.favicon==='/assets/brand-mark.svg';
  if(!result.ok)throw Error('visual checks failed');
  console.log(JSON.stringify(result));
 }catch(err){console.error(JSON.stringify({...result,error:String(err)}));process.exitCode=1}
 finally{
  if(ws)ws.close();
  chrome.kill();try{fs.rmSync(profile,{recursive:true,force:true})}catch(e){}
 }
})();
