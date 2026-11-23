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

  await until("document.getElementById('p9-annotation').options.length>=10 && document.getElementById('p9-comparison-result').innerText.includes('0.00086')",14000);
  report.p9=await js("({numberOfAnnotations:document.getElementById('p9-annotation').options.length,comparison:document.getElementById('p9-comparison-result').innerText.slice(0,1200),selection:document.querySelector('#p9-annotation option:checked').textContent,unit:document.getElementById('p9-source-info').innerText.includes('мм')})");
  await js("document.getElementById('p9-axis').value='x';document.getElementById('p9-axis').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#p9-comparison-form button').click()");
  await until("document.getElementById('p9-comparison-result').innerText.includes('Числа отличаются')");
  report.p9WrongAxis=await js("document.getElementById('p9-comparison-result').innerText.slice(0,550)");
  await js("document.getElementById('p9-axis').value='y';document.getElementById('p9-axis').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#p9-comparison-form button').click()");
  await until("document.getElementById('p9-comparison-result').innerText.includes('Числа находятся в выбранном интервале')");
  await js("document.getElementById('p9-part-panel').scrollIntoView({block:'start'})");

  await until("document.getElementById('p10-feature-result').innerText.includes('41.5') && document.getElementById('p10-feature-result').innerText.includes('Числа аналитической поверхности совпадают')",20000);
  report.p10Radius=await js("({callout:document.querySelector('#p10-callout option:checked')?.textContent,face:document.querySelector('#p10-surface option:checked')?.textContent,value:document.getElementById('p10-feature-result').innerText.slice(0,850),registry:document.getElementById('p10-revision-result').innerText.slice(0,380)})");
  await js("document.getElementById('p10-feature-panel').scrollIntoView({block:'start'})");


  await until("document.querySelector('#p12-cad-image img')?.naturalWidth===1000",18000);
  await until("document.querySelector('#p13-vector-preview img')?.naturalWidth>200 && document.getElementById('p13-match-summary').innerText.includes('один численно совместимый вариант')",23000);
  report.p13R41=await js("({width:document.querySelector('#p13-vector-preview img').naturalWidth,proof:document.querySelector('#p13-vector-proof').innerText.slice(0,430),summary:document.getElementById('p13-match-summary').innerText.slice(0,275),count:document.querySelectorAll('#p13-candidates .p13-candidate').length})");
  report.p12CAD=await js("(()=>{const img=document.querySelector('#p12-cad-image img');const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);const rgb=ctx.getImageData(0,0,canvas.width,canvas.height).data;let orange=0,blue=0;for(let i=0;i<rgb.length;i+=4){const r=rgb[i],g=rgb[i+1],b=rgb[i+2];if(r>145&&r>g*1.55&&r>b*1.65&&g>25)orange++;if(b>r*1.04&&b>g*.90&&r<215)blue++;}return {width:canvas.width,height:canvas.height,orange,blue,src:img.getAttribute('src').slice(-160)}})()");
  await js("document.getElementById('p12-camera').value='side';document.getElementById('p12-camera').dispatchEvent(new Event('change',{bubbles:true}))");
  await until("document.querySelector('#p12-cad-image img')?.getAttribute('src').includes('view=side') && document.querySelector('#p12-cad-image img')?.naturalWidth===1000",18000);
  report.p12Side=await js("({src:document.querySelector('#p12-cad-image img').getAttribute('src'),width:document.querySelector('#p12-cad-image img').naturalWidth})");
  await js("document.getElementById('p12-camera').value='iso';document.getElementById('p12-camera').dispatchEvent(new Event('change',{bubbles:true}))");
  await until("document.querySelector('#p12-cad-image img')?.getAttribute('src').includes('view=iso') && document.querySelector('#p12-cad-image img')?.naturalWidth===1000",18000);
  await until("document.querySelector('#p11-preview-area img')?.naturalWidth>50",15000);
  report.p11Crop=await js("({width:document.querySelector('#p11-preview-area img').naturalWidth,height:document.querySelector('#p11-preview-area img').naturalHeight,source:document.getElementById('p11-current-pair').innerText.slice(0,260)})");
  await js("document.getElementById('p11-decision').value='NEEDS_MORE_EVIDENCE';document.getElementById('p11-note').value='Тест: сверена область R41.5 с цилиндрической гранью CAD, но линия-выноска и допуск требуют дополнительной экспертизы.';document.getElementById('p11-save').click()");
  await until("document.getElementById('p11-save-result').innerText.includes('Замечание сохранено') && document.querySelectorAll('#p11-history .p11-history-item').length===1",18000);
  report.p11Recorded=await js("({status:document.getElementById('p11-save-result').innerText.slice(0,380),history:document.getElementById('p11-history').innerText.slice(0,380),count:document.querySelectorAll('#p11-history .p11-history-item').length})");
  await until("document.getElementById('p12-witness-status').innerText.includes('НЕ ПОДКЛЮЧЕНА') && document.getElementById('p12-witness-status').innerText.includes('1')",10000);
  report.p12Witness=await js("document.getElementById('p12-witness-status').innerText.slice(0,520)");
  await js("document.getElementById('p11-save').click()");
  await until("document.getElementById('p11-save-result').innerText.includes('Такая запись уже была сохранена')",16000);
  await until("document.querySelectorAll('#p11-history .p11-history-item').length===1",12000);
  report.p11Idempotent=await js("({status:document.getElementById('p11-save-result').innerText.slice(0,180),count:document.querySelectorAll('#p11-history .p11-history-item').length})");
  await js("document.getElementById('p10-callout').value='e1a9fb555c7e8e7491ddd820';document.getElementById('p10-callout').dispatchEvent(new Event('change',{bubbles:true}))");
  await until("document.querySelectorAll('#p13-candidates .p13-candidate').length===2 && document.getElementById('p13-match-summary').innerText.includes('несколько численно совместимых')",23000);
  report.p13R4=await js("({count:document.querySelectorAll('#p13-candidates .p13-candidate').length,blocked:document.getElementById('p10-surface').value==='',warning:document.getElementById('p13-candidates').innerText.includes('Автоматический выбор запрещён'),ids:[...document.querySelectorAll('#p13-candidates .p13-candidate p')].filter(x=>x.textContent.includes('ID поверхности:')).map(x=>x.textContent)})");
  await js("document.querySelector('#p13-candidates .p13-candidate button').click()");
  await until("document.getElementById('p10-surface').value==='083588fefad986d245b87466' && document.querySelector('#p12-cad-image img')?.naturalWidth===1000",28000);
  report.p13R4Manual=await js("({feature:document.getElementById('p10-surface').value,imageWidth:document.querySelector('#p12-cad-image img').naturalWidth,view:document.getElementById('p12-camera').value})");
  await js("document.getElementById('p10-callout').value='cd2e9bca78adda13b1322aba';document.getElementById('p10-callout').dispatchEvent(new Event('change',{bubbles:true}))");
  await until("document.querySelector('#p13-vector-preview img')?.naturalWidth>200 && document.getElementById('p13-match-summary').innerText.includes('один численно совместимый')",23000);
  await js("document.getElementById('p13-registration-panel').scrollIntoView({block:'start',behavior:'instant'})");
  await sleep(300);
  await screenshot('p13_engineering_desktop.jpg');
  await sleep(280);
  await screenshot('p13_engineering_desktop.jpg');





  await js("document.querySelector('[data-engineering-example=\"040M07C01.0P026D\"]').click()");
  await until("document.querySelectorAll('#engineering-result .engineering-source').length===0 && document.getElementById('engineering-result').innerText.includes('Невозможно подтвердить связь')");
  report.negative=await js("document.getElementById('engineering-result').innerText.slice(0,200)");
  await js("document.querySelector('[data-engineering-example=\"lower_square_tube\"]').click()");
  await until("document.getElementById('engineering-result').innerText.includes('SUS304') && document.querySelectorAll('#engineering-result .engineering-source').length===3");
  await until("document.getElementById('p9-source-info').innerText.includes('lower_square_tube.pdf') && document.getElementById('p9-comparison-result').innerText.includes('437')",12000);
  report.p9Tube=await js("({label:document.querySelector('#p9-annotation option:checked').textContent,result:document.getElementById('p9-comparison-result').innerText.slice(0,700)})");
  await until("document.getElementById('p10-feature-result').innerText.includes('0.00015') && document.getElementById('p10-feature-result').innerText.includes('совпадают')",20000);
  report.p10Angle=await js("({callout:document.querySelector('#p10-callout option:checked')?.textContent,face:document.querySelector('#p10-surface option:checked')?.textContent,result:document.getElementById('p10-feature-result').innerText.slice(0,750)})");
  await until("document.querySelector('#p12-cad-image img')?.naturalWidth===1000",18000);
  report.p12Tube=await js("({view:document.getElementById('p12-camera').value,feature:document.querySelector('#p10-surface option:checked').textContent,width:document.querySelector('#p12-cad-image img').naturalWidth})");
  await until("document.querySelector('#p13-vector-preview img')?.naturalWidth>200 && document.querySelector('#p13-vector-proof').innerText.includes('Отрезков реальной дуги')",24000);
  report.p13Angle=await js("({width:document.querySelector('#p13-vector-preview img').naturalWidth,proof:document.getElementById('p13-vector-proof').innerText.slice(0,430),summary:document.getElementById('p13-match-summary').innerText.slice(0,260),count:document.querySelectorAll('#p13-candidates .p13-candidate').length})");
  await js("document.getElementById('p10-reference-axis').value='y';document.getElementById('p10-reference-axis').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#p10-compare-form button').click()");
  await until("document.getElementById('p10-feature-result').innerText.includes('Числа аналитической поверхности отличаются')",14000);
  report.p10WrongAxis=await js("document.getElementById('p10-feature-result').innerText.slice(0,380)");
  await until("document.getElementById('p13-match-summary').innerText.includes('не найдено')",23000);
  report.p13WrongAxis=await js("({summary:document.getElementById('p13-match-summary').innerText.slice(0,260),count:document.querySelectorAll('#p13-candidates .p13-candidate').length})");
  await js("document.getElementById('p10-reference-axis').value='x';document.getElementById('p10-reference-axis').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#p10-compare-form button').click()");
  await until("document.getElementById('p10-feature-result').innerText.includes('0.00015')",14000);


  for(const width of [1440,1024,768,390]){
    await command('Emulation.setDeviceMetricsOverride',{width,height:950,deviceScaleFactor:1,mobile:width<=768});
    await sleep(250);
    report['w'+width]=await js("({viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,sources:document.querySelectorAll('#engineering-result .engineering-source').length})");
    if(width===390){
      report.mobileOverflow=await js("([...document.querySelectorAll('*')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.right>innerWidth+3&&getComputedStyle(e).visibility!=='hidden'}).slice(0,20).map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,65),right:Math.round(e.getBoundingClientRect().right),scroll:e.scrollWidth})))");
      report.mobileLayout=await js("(['html','body','.app-shell','.sidebar','.nav','.app-body','.main-content','#page-engineering','.p8-panel','#p8-source-select'].map(q=>{const e=document.querySelector(q);if(!e)return null;const r=e.getBoundingClientRect();return {q,width:Math.round(r.width),left:Math.round(r.left),right:Math.round(r.right),scroll:e.scrollWidth,overflow:getComputedStyle(e).overflowX}}))");
      report.mobileIntrinsicOverflow=await js("([...document.querySelectorAll('#page-engineering *')].filter(e=>e.clientWidth>0&&e.scrollWidth>e.clientWidth+10).map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,60),client:e.clientWidth,scroll:e.scrollWidth,excess:e.scrollWidth-e.clientWidth})).sort((a,b)=>b.excess-a.excess).slice(0,25))");
      await js("document.getElementById('p13-registration-panel').scrollIntoView({block:'start',behavior:'instant'})");
      await sleep(280);
      await screenshot('p13_engineering_mobile.jpg');
    }
  }
  report.exceptions=exceptions;
  report.assertionFailures=[
    {name:"#0: report.p12CAD.width===1000",pass:Boolean(report.p12CAD.width===1000)},
    {name:"#1: report.p12CAD.orange>500",pass:Boolean(report.p12CAD.orange>500)},
    {name:"#2: report.p12CAD.blue>1000",pass:Boolean(report.p12CAD.blue>1000)},
    {name:"#3: report.p12Side.width===1000",pass:Boolean(report.p12Side.width===1000)},
    {name:"#4: report.p12Tube.width===1000",pass:Boolean(report.p12Tube.width===1000)},
    {name:"#5: report.p12Witness.includes('НЕ ПОДКЛЮЧЕНА')",pass:Boolean(report.p12Witness.includes('НЕ ПОДКЛЮЧЕНА'))},
    {name:"#6: report.p11Crop.width>50",pass:Boolean(report.p11Crop.width>50)},
    {name:"#7: report.p11Crop.height>20",pass:Boolean(report.p11Crop.height>20)},
    {name:"#8: report.p11Recorded.count===1",pass:Boolean(report.p11Recorded.count===1)},
    {name:"#9: report.p11Idempotent.count===1",pass:Boolean(report.p11Idempotent.count===1)},
    {name:"#10: report.p11Idempotent.status.includes('Такая запись')",pass:Boolean(report.p11Idempotent.status.includes('Такая запись'))},
    {name:"#11: report.p10Radius.callout.includes('R41.5')",pass:Boolean(report.p10Radius.callout.includes('R41.5'))},
    {name:"#12: report.p10Radius.value.includes('41.5')",pass:Boolean(report.p10Radius.value.includes('41.5'))},
    {name:"#13: report.p10Radius.registry.includes('не подключена')",pass:Boolean(report.p10Radius.registry.includes('не подключена'))},
    {name:"#14: report.p10Angle.callout.includes('67.38')",pass:Boolean(report.p10Angle.callout.includes('67.38'))},
    {name:"#15: report.p10Angle.result.includes('0.00015')",pass:Boolean(report.p10Angle.result.includes('0.00015'))},
    {name:"#16: report.p10WrongAxis.includes('отличаются')",pass:Boolean(report.p10WrongAxis.includes('отличаются'))},
    {name:"#17: report.p9.numberOfAnnotations>=10",pass:Boolean(report.p9.numberOfAnnotations>=10)},
    {name:"#18: report.p9.comparison.includes('0.00086')",pass:Boolean(report.p9.comparison.includes('0.00086'))},
    {name:"#19: report.p9.comparison.includes('НЕ УСТАНОВЛЕНО')",pass:Boolean(report.p9.comparison.includes('НЕ УСТАНОВЛЕНО'))},
    {name:"#20: report.p9WrongAxis.includes('Числа отличаются')",pass:Boolean(report.p9WrongAxis.includes('Числа отличаются'))},
    {name:"#21: report.p9Tube.label.includes('437')",pass:Boolean(report.p9Tube.label.includes('437'))},
    {name:"#22: report.p9Tube.result.includes('0 мм')",pass:Boolean(report.p9Tube.result.includes('0 мм'))},
    {name:"#23: report.p8Initial.topology",pass:Boolean(report.p8Initial.topology)},
    {name:"#24: report.p8Initial.catalog>=19",pass:Boolean(report.p8Initial.catalog>=19)},
    {name:"#25: report.passport.history>=2",pass:Boolean(report.passport.history>=2)},
    {name:"#26: report.releaseComparison.sides===2",pass:Boolean(report.releaseComparison.sides===2)},
    {name:"#27: report.releaseComparison.codes.some(Boolean)",pass:Boolean(report.releaseComparison.codes.some(Boolean))},
    {name:"#28: report.notice.toLowerCase().includes('не подтверждены')",pass:Boolean(report.notice.toLowerCase().includes('не подтверждены'))},
    {name:"#29: report.engineer.downloads===3",pass:Boolean(report.engineer.downloads===3)},
    {name:"#30: PDF download",pass:Boolean(report.engineer.filenames.includes('focuser_drawtube.pdf'))},
    {name:"#31: STEP download",pass:Boolean(report.engineer.filenames.includes('focuser_drawtube.STEP'))},
    {name:"#32: historical citation",pass:Boolean(report.engineer.historical.includes('318.03-2009'))},
    {name:"#33: material text",pass:Boolean(report.engineer.text.includes('6061-T6'))},
    {name:"#34: responsive viewport",pass:Boolean([1440,1024,768,390].every(width=>{
      const item=report['w'+width];return item.scroll<=item.viewport+2 && item.sources===3;
    }))},
    {name:"#35: no browser exceptions",pass:Boolean(exceptions.length===0)},
    {name:"#36: genuine PDF vector R41 image",pass:Boolean(report.p13R41.width>200)},
    {name:"#37: unique R41 CAD numeric value",pass:Boolean(report.p13R41.count===1)},
    {name:"#38: R41 arrow touches real document contour",pass:Boolean(report.p13R41.proof.includes('0.011'))},
    {name:"#39: R4 two distinct CAD alternatives",pass:Boolean(report.p13R4.count===2 && new Set(report.p13R4.ids).size===2)},
    {name:"#40: R4 no implicit first-match selection",pass:Boolean(report.p13R4.blocked)},
    {name:"#41: R4 requires manual selection",pass:Boolean(report.p13R4.warning)},
    {name:"#42: original angular PDF arc",pass:Boolean(report.p13Angle.width>200 && report.p13Angle.proof.includes('45'))},
    {name:"#43: angle CAD candidate",pass:Boolean(report.p13Angle.count===1)},
    {name:"#44: wrong axis produces no candidates",pass:Boolean(report.p13WrongAxis.count===0)},
    {name:"#45: explicit unverified CAD mapping",pass:Boolean(report.p13R41.summary.includes('не установлено'))},
    {name:"#46: explicit R4 candidate selection",pass:Boolean(report.p13R4Manual.feature==='083588fefad986d245b87466')},
    {name:"#47: selected real R4 CAD renders in 3D",pass:Boolean(report.p13R4Manual.imageWidth===1000)}
  ].filter(item=>!item.pass).map(item=>item.name);
  report.ok=report.assertionFailures.length===0;
  if(!report.ok){console.error('FAILED_CONDITIONS '+JSON.stringify(report.assertionFailures));throw Error('P13 browser acceptance failed');}
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
