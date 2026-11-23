const fs=require('fs'),os=require('os'),path=require('path'),cp=require('child_process');
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'factory-enter-test-'));
const chrome=cp.spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
 ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
  '--remote-debugging-port=19119','--window-size=1450,900','--user-data-dir='+profile,'about:blank'],
 {stdio:'ignore',windowsHide:true});
let ws,seq=1,pending=new Map(),errors=[];
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function send(method,params={}){
 const id=seq++;
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout '+method))},8000);
  pending.set(id,{resolve,reject,timer});
  ws.send(JSON.stringify({id,method,params}));
 });
}
async function evalInPage(expression){
 const m=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
 if(m.result.exceptionDetails)throw Error('Eval failed: '+JSON.stringify(m.result.exceptionDetails));
 return m.result.result.value;
}
async function until(expression,timeout=9000){
 const started=Date.now();
 while(Date.now()-started<timeout){
  if(await evalInPage(expression))return true;
  await sleep(90);
 }
 throw Error('Not ready: '+expression);
}
async function keyDownEnter(modifiers=0,autoRepeat=false){
 await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',
       text:'\r',unmodifiedText:'\r',windowsVirtualKeyCode:13,nativeVirtualKeyCode:13,
       modifiers,autoRepeat});
 await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',
       windowsVirtualKeyCode:13,nativeVirtualKeyCode:13,modifiers});
}
(async()=>{
 const out={};
 try{
  let tabs;
  for(let i=0;i<65;i++){
   try{tabs=await (await fetch('http://127.0.0.1:19119/json')).json();if(tabs.length)break}catch(e){}
   await sleep(110);
  }
  if(!tabs)throw Error('Chrome not reachable');
  ws=new WebSocket(tabs[0].webSocketDebuggerUrl.replace('localhost','127.0.0.1'));
  await new Promise((ok,no)=>{ws.onopen=ok;ws.onerror=no});
  ws.onmessage=e=>{
   const m=JSON.parse(e.data),pend=pending.get(m.id);
   if(m.method==='Runtime.exceptionThrown')errors.push(m.params?.exceptionDetails?.text||'error');
   if(pend){clearTimeout(pend.timer);pending.delete(m.id);m.error?pend.reject(Error(m.error.message)):pend.resolve(m)}
  };
  await send('Runtime.enable');await send('Page.enable');
  await send('Page.navigate',{url:'http://127.0.0.1:19127/'});
  await until("document.readyState==='complete' && !!document.querySelector('[data-demo-role=technologist]')");
  await evalInPage("document.querySelector('[data-demo-role=technologist]').click()");
  await until("!document.getElementById('workspace').hidden");
  await evalInPage("document.querySelector('[data-page=assistant]').click()");
  await until("!document.getElementById('page-assistant').hidden");
  const patch="(()=>{window.__testRequests=[];const original=window.fetch.bind(window);window.fetch=(url,options={})=>{if(new URL(url,location.href).pathname==='/api/ask'){window.__testRequests.push({body:JSON.parse(options.body),at:Date.now()});return new Promise(resolve=>setTimeout(()=>resolve(new Response(JSON.stringify({answer:'Тестовый локальный ответ',mode:'local_llm',sources:[]}),{status:200,headers:{'Content-Type':'application/json'}})),350));}return original(url,options);};return true;})()";
  await evalInPage(patch);
  out.hasKeyboardHint=await evalInPage("document.querySelector('.ai-keyboard-hint')?.innerText?.includes('Shift+Enter')===true");
  await evalInPage("document.getElementById('ai-question').value='Проверка обычного Enter';document.getElementById('ai-question').focus()");
  await keyDownEnter();
  await until("window.__testRequests.length===1");
  out.enterRequest=await evalInPage("window.__testRequests[0].body.question");
  out.enterDidNotNewline=await evalInPage("document.getElementById('ai-question').value==='Проверка обычного Enter'");
  await until("document.getElementById('ai-ask').disabled===false");
  await evalInPage("document.getElementById('ai-question').value='Первая строка';document.getElementById('ai-question').focus()");
  await keyDownEnter(8);
  out.shiftEnterText=await evalInPage("document.getElementById('ai-question').value");
  out.shiftEnterDidNotSubmit=await evalInPage("window.__testRequests.length===1");
  out.composing=await evalInPage("(()=>{const el=document.getElementById('ai-question');const ev=new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true});el.dispatchEvent(ev);return {defaultPrevented:ev.defaultPrevented,requests:window.__testRequests.length};})()");
  await evalInPage("document.getElementById('ai-question').value='Один запрос при удержании Enter';document.getElementById('ai-question').focus()");
  await keyDownEnter();
  await until("window.__testRequests.length===2");
  await keyDownEnter(0,true);
  await sleep(100);
  out.repeatCount=await evalInPage("window.__testRequests.length");
  out.noNewlineAfterRepeat=await evalInPage("document.getElementById('ai-question').value==='Один запрос при удержании Enter'");
  await until("document.getElementById('ai-ask').disabled===false");
  await evalInPage("document.getElementById('ai-question').value='Отправлено кнопкой';document.getElementById('ai-ask').click()");
  await until("window.__testRequests.length===3");
  out.buttonQuestion=await evalInPage("window.__testRequests[2].body.question");
  out.errors=errors;
  out.pass=out.hasKeyboardHint &&
    out.enterRequest==='Проверка обычного Enter' && out.enterDidNotNewline &&
    out.shiftEnterDidNotSubmit && out.shiftEnterText==='Первая строка\n' &&
    out.composing.defaultPrevented===false && out.composing.requests===1 &&
    out.repeatCount===2 && out.noNewlineAfterRepeat &&
    out.buttonQuestion==='Отправлено кнопкой' && errors.length===0;
  if(!out.pass)throw Error('Keyboard behavior assertions failed');
  console.log(JSON.stringify({ok:true,...out}));
 }catch(e){console.error(JSON.stringify({ok:false,error:String(e),...out}));process.exitCode=1}
 finally{
  if(ws)ws.close();
  chrome.kill();
  try{fs.rmSync(profile,{recursive:true,force:true})}catch{}
 }
})();