/* Цех.Знания — учебная демонстрация с локальной MiniCPM. Без облачных запросов. */
'use strict';
const $ = id => document.getElementById(id);
function uiIcon(name){
  // One coherent Lucide icon registry. All source SVGs bundled locally.
  const ns='http://www.w3.org/2000/svg';
  const svg=document.createElementNS(ns,'svg');
  svg.setAttribute('class','ui-icon');
  svg.setAttribute('aria-hidden','true');
  const use=document.createElementNS(ns,'use');
  use.setAttribute('href','/assets/icons.svg#icon-'+name);
  svg.append(use);
  return svg;
}
const state = {me:null,page:'home',query:'',aiPoll:null,aiBusy:false,aiStatus:'stopped',sessionSerial:0,uploadCaps:null,uploadKey:null,uploadDocId:null,uploadBusy:false,backendMode:'unified',demoUiReady:false};
const titles = {home:'Главная',documents:'Документы',assistant:'Вопросы по файлам',part:'Детали и партии',engineering:'Инженерный корпус',metrics:'Затраты'};
const types = {drawing:'Чертёж',routing:'Технологическая карта',change:'Извещение об изменении',finance:'Отчёт о затратах',internal:'Внутренний документ',manual:'Инструкция',uploaded:'Загруженный файл'};
const fmt = new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0});
const money = value => fmt.format(value)+' ₽';
function el(tag,className='',text=''){
  const n=document.createElement(tag);
  if(className)n.className=className;
  n.textContent=String(text ?? '');
  return n;
}
function isDemoMode(){
  return state.backendMode==='demo';
}
async function detectBackendMode(){
  if(location.hostname!=='127.0.0.1' && location.hostname!=='localhost')return;
  try{
    const r=await fetch('/api/health',{credentials:'same-origin',cache:'no-store'});
    if(!r.ok)return;
    const data=await r.json();
    if(data && data.status==='ok' && data.mode==='synthetic-local-demo'){
      state.backendMode='demo';
      applyDemoModeUI();
    }
  }catch{}
}
function applyDemoModeUI(){
  if(state.demoUiReady)return;
  state.demoUiReady=true;
  const authInner=document.querySelector('#auth .auth-inner');
  const truthBox=authInner?.querySelector('.truth-note > div');
  if(truthBox){
    truthBox.replaceChildren(
      el('strong','','Это демонстрационная версия.'),
      document.createTextNode(' Локальный стенд на учебных данных без Keycloak. Не загружайте рабочие документы, персональные данные и коммерческую тайну.')
    );
  }
  const roleTitle=authInner?.querySelector('.role-title');
  if(roleTitle){
    roleTitle.textContent='Выберите учебную роль';
    if(roleTitle.nextElementSibling?.tagName==='P'){
      roleTitle.nextElementSibling.textContent='У каждой роли свой доступ к учебным документам. Проверка прав выполняется на локальном сервере.';
    }
  }
  const unifiedBtn=$('unified-login');
  if(unifiedBtn)unifiedBtn.hidden=true;
  const loginError=$('login-error');
  if(authInner && loginError && !$('demo-role-list')){
    const roles=[
      {id:'technologist',primary:true,icon:'wrench',title:'Технолог цеха',desc:'Базовые инструкции, поиск по архиву и загрузка своих файлов.'},
      {id:'engineer',primary:false,icon:'file-check-2',title:'Инженер-конструктор',desc:'Извещения КБ, проверка редакций и инженерный корпус.'},
      {id:'finance',primary:false,icon:'chart-no-axes-column',title:'Финансовый аналитик',desc:'План-факт затрат по учебному заказу и открытые документы.'},
      {id:'director',primary:false,icon:'files',title:'Руководитель',desc:'Полный учебный архив, включая закрытый пример отчёта дирекции.'}
    ];
    const list=el('div','role-list');
    list.id='demo-role-list';
    for(const item of roles){
      const btn=el('button',item.primary?'role-card primary-role':'role-card');
      btn.type='button';
      btn.dataset.demoRole=item.id;
      const iconWrap=el('span','role-icon');
      iconWrap.append(uiIcon(item.icon));
      const textWrap=el('span','role-text');
      textWrap.append(el('strong','',item.title),el('small','',item.desc));
      const arrow=el('span','role-arrow');
      arrow.append(uiIcon('arrow-up-right'));
      btn.append(iconWrap,textWrap,arrow);
      btn.addEventListener('click',()=>demoLogin(item.id,btn));
      list.append(btn);
    }
    authInner.insertBefore(list,loginError);
  }
  const loginForm=$('login-form');
  if(authInner && loginForm && !loginForm.closest('details.technical-login')){
    const details=el('details','technical-login');
    details.append(el('summary','','Вход по паролю (demo_credentials.txt)'));
    const userInput=$('username'),passInput=$('password');
    if(userInput && !loginForm.querySelector('label[for="username"]')){
      const userLabel=el('label','','Логин роли');
      userLabel.htmlFor='username';
      userInput.autocomplete='username';
      userInput.required=true;
      loginForm.insertBefore(userLabel,userInput);
    }
    if(passInput && !loginForm.querySelector('label[for="password"]')){
      const passLabel=el('label','','Пароль');
      passLabel.htmlFor='password';
      passInput.autocomplete='current-password';
      passInput.required=true;
      loginForm.insertBefore(passLabel,passInput);
    }
    const submitBtn=loginForm.querySelector('button[type="submit"]');
    if(submitBtn)submitBtn.className='secondary-button';
    loginForm.hidden=false;
    details.append(loginForm);
    authInner.append(details);
  }
  const partNav=document.querySelector('.nav-item[data-page="part"]');
  if(partNav){partNav.disabled=false;partNav.removeAttribute('title')}
  const partTile=document.querySelector('.task-tile[data-action="part"]');
  if(partTile){partTile.disabled=false;partTile.removeAttribute('title')}
  const chipSub=document.querySelector('.user-chip small');
  if(chipSub)chipSub.textContent='Локальный стенд · учебная роль';
  const switchBtn=$('switch-demo');
  if(switchBtn){
    const iconSpan=el('span');
    iconSpan.append(uiIcon('arrow-up-right'));
    switchBtn.replaceChildren(document.createTextNode('Сменить роль '),iconSpan);
  }
  const homeLead=document.querySelector('#page-home .page-lead p');
  if(homeLead)homeLead.textContent='Выберите задачу. Учебные документы с проверкой прав на локальном сервере.';
  const uploadNote=document.querySelector('#page-assistant .upload-card p');
  if(uploadNote)uploadNote.textContent='После загрузки текст файла станет доступен только в личном пространстве выбранной роли. До 5 МБ.';
  const titleLabel=document.querySelector('label[for="ai-title"]');
  if(titleLabel)titleLabel.hidden=true;
  if($('ai-title'))$('ai-title').hidden=true;
  const privateKit=document.querySelector('.private-kit');
  if(privateKit)privateKit.hidden=true;
  const kitReport=document.querySelector('.kit-report:not(.private-kit)');
  if(kitReport)kitReport.hidden=true;
  const p11Save=$('p11-save');
  if(p11Save){p11Save.disabled=false;p11Save.removeAttribute('title')}
}
async function request(path,opts={}){
  const mapped=(!isDemoMode() && path.startsWith('/api/'))?'/unified'+path:path;
  const r=await fetch(mapped,{credentials:'same-origin',cache:'no-store',...opts});
  let data;
  try{data=await r.json()}catch{throw Error('Не удалось прочитать ответ сервера')}
  if(!r.ok){
    const err=Error(data.error||data.detail||('Ошибка '+r.status));
    err.status=r.status;
    throw err;
  }
  return data;
}
function showAuth(){
  state.sessionSerial++;
  state.me=null;state.uploadCaps=null;state.uploadKey=null;state.uploadDocId=null;$('workspace').hidden=true;$('auth').hidden=false;
  if(state.aiPoll){clearInterval(state.aiPoll);state.aiPoll=null}
  if($('private-kit-form')){
    $('private-kit-form').reset();
    $('private-kit-result').textContent='Выберите три файла, затем нажмите «Проверить комплект».';
    privateKitReset();
  }
  $('ai-answer').replaceChildren();$('ai-answer').hidden=true;
  $('ai-sources').replaceChildren();$('ai-sources').hidden=true;
  $('ai-upload-result').textContent='';$('ai-question').value='';$('ai-file').value='';
  if($('document-modal').open)$('document-modal').close();
  if($('scan-preview-modal').open)$('scan-preview-modal').close();
  $('scan-preview-image').removeAttribute('src');
  $('login-error').textContent='';
  document.title='Цех.Знания — поиск по документам';
  document.querySelectorAll('[data-demo-role]').forEach(b=>b.disabled=false);
}
function setRoleView(){
  const canEngineering=(state.me.scopes||[]).includes('engineering');
  $('engineering-navigation').hidden=!canEngineering;
  const canFinance=(state.me.scopes||[]).includes('finance');
  $('finance-navigation').hidden=!canFinance;
  $('finance-tile').hidden=!canFinance;
  $('profile-role').textContent=state.me.role;
  $('avatar').textContent=state.me.role.substring(0,1);
  if(!canFinance&&state.page==='metrics')state.page='home';
  if(!canEngineering&&state.page==='engineering')state.page='home';
}
async function refresh(){
  try{
    const next=await request('/api/me');
    if(!state.me || state.me.id!==next.id)state.sessionSerial++;
    state.me=next;
    $('auth').hidden=true;$('workspace').hidden=false;
    setRoleView();await navigate(state.page);
  }catch{showAuth()}
}
async function demoLogin(role,button){
  if(!isDemoMode()){
    window.location.assign('/auth/login');
    return;
  }
  $('login-error').textContent='';
  const buttons=document.querySelectorAll('[data-demo-role]');
  buttons.forEach(b=>b.disabled=true);
  try{
    await request('/api/demo-login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({role})
    });
    await refresh();
  }catch(error){
    $('login-error').textContent=error.message||'Не удалось выполнить вход';
    buttons.forEach(b=>b.disabled=false);
    if(button)button.focus();
  }
}
async function logout(){
  if(!state.me)return showAuth();
  if(isDemoMode()){
    try{
      await request('/api/logout',{
        method:'POST',
        headers:{'X-CSRF-Token':state.me.csrf,'Content-Type':'application/json'},
        body:'{}'
      });
    }catch{}
    showAuth();
    return;
  }
  try{
    const response=await fetch('/auth/logout',{method:'POST',credentials:'same-origin',
       headers:{'X-CSRF-Token':state.me.csrf,'Content-Type':'application/json'}});
    if(!response.ok)throw Error('Выход не подтверждён');
    const data=await response.json();
    const url=new URL(data.identity_logout_url);
    const logoutPath='/realms/factory-core-v06/protocol/openid-connect/logout';
    const allowedCanonical=url.origin+url.pathname==='http://127.0.0.1:18080'+logoutPath;
    const portNum=Number(url.port);
    const allowedStaging=location.origin==='http://localhost:19137'
      && url.protocol==='http:' && url.hostname==='127.0.0.1' && !url.username && !url.password
      && url.pathname===logoutPath && Number.isInteger(portNum) && portNum>=1024 && portNum<=65535;
    if(!allowedCanonical && !allowedStaging)
      throw Error('Недопустимый адрес SSO');
    showAuth();window.location.assign(url.href);
  }catch(error){$('login-error').textContent=error.message}
}
function errorIn(box,message){
  box.replaceChildren(el('div','message-error','Не удалось получить данные: '+message));
}
async function navigate(page){
  if(!Object.prototype.hasOwnProperty.call(titles,page))page='home';
  if(page==='metrics'&&!state.me?.scopes?.includes('finance'))page='home';
  if(page==='engineering'&&!state.me?.scopes?.includes('engineering'))page='home';
  state.page=page;
  if(state.aiPoll){clearInterval(state.aiPoll);state.aiPoll=null}
  document.querySelectorAll('.page').forEach(s=>s.hidden=s.id!=='page-'+page);
  document.querySelectorAll('.nav-item').forEach(b=>{
    const active=b.dataset.page===page;
    b.classList.toggle('active',active);
    if(active)b.setAttribute('aria-current','page');
    else b.removeAttribute('aria-current');
  });
  $('page-label').textContent=titles[page];
  document.title=titles[page]+' — Цех.Знания';
  window.scrollTo({top:0,behavior:'instant'});
  if(page==='documents')await loadDocuments(state.query);
  if(page==='assistant'){
    await loadUploadCapabilities();
    await loadAiStatus();
    state.aiPoll=setInterval(()=>{if(state.me && state.page==='assistant'){loadAiStatus();if(!isDemoMode())loadUploadJobStatus()}},2500);
  }
  if(page==='part'){
    if(isDemoMode())await loadPart();
    else $('part-result').replaceChildren(el('div','message-error','Проверка применимости партий к PostgreSQL ещё не перенесена. Используйте инженерный корпус в отдельном пилоте.'));
  }
  if(page==='engineering'){
    await loadEngineering();
    if(!isDemoMode()){await loadUploadCapabilities();await loadSavedKits()}
  }
  if(page==='metrics')await loadMetrics();
}
function card(doc){
  const box=el('article','doc-card');
  const top=el('div','doc-top');
  top.append(el('span','doc-type',types[doc.type]||'Документ'));
  if(doc.revision&&!['учебный'].includes(doc.revision))top.append(el('span','doc-revision','Редакция '+doc.revision));
  if(doc.has_ocr)top.append(el('span','doc-revision','Распознано со скана'));
  box.append(top,el('h3','',doc.title));
  const preview=String(doc.snippet||'').replace(/^УЧЕБН(ЫЙ ПРИМЕР|АЯ ПАМЯТКА|АЯ ИНСТРУКЦИЯ)\.\s*/i,'');
  box.append(el('p','doc-preview',preview));
  const bottom=el('div','doc-foot');
  bottom.append(el('span','doc-code',doc.designation));
  const button=el('button','doc-open','Открыть');button.type='button';button.append(uiIcon('arrow-up-right'));
  button.addEventListener('click',()=>openDocument(doc.id));
  bottom.append(button);
  box.append(bottom);return box;
}
async function loadDocuments(query=''){
  const out=$('search-results');out.replaceChildren(el('div','loading','Идёт поиск…'));
  $('search-input').value=query;
  state.query=query;
  $('docs-heading').textContent=query?'Результаты по запросу «'+query+'»':'Все доступные документы';
  try{
    const url=query?'/api/search?q='+encodeURIComponent(query):'/api/documents';
    const data=await request(url);
    $('result-count').textContent='Документов: '+data.items.length;
    if(data.items.length)out.replaceChildren(...data.items.map(card));
    else{
      const empty=el('div','empty-state');
      empty.append(el('h3','','Ничего не найдено'),
        el('p','','Уточните обозначение или попробуйте более короткий запрос.'));
      const button=el('button','secondary-button','Показать все документы');
      button.addEventListener('click',()=>loadDocuments(''));
      empty.append(button);out.replaceChildren(empty);
    }
  }catch(e){errorIn(out,e.message)}
}
async function openDocument(id){
  const modal=$('document-modal'),session=state.sessionSerial;
  try{
    const d=await request('/api/document?id='+encodeURIComponent(id));
    if(!state.me || state.sessionSerial!==session)return;
    $('modal-kind').textContent=types[d.type]||'Документ';
    $('modal-title').textContent=d.title;
    $('modal-meta').textContent='Обозначение: '+d.designation+' · Редакция: '+d.revision;
    $('modal-text').textContent=d.text;
    const downloaded=d.type==='uploaded';
    const canWrite=Boolean(state.uploadCaps?.enabled&&Array.isArray(state.uploadCaps.writable_scopes)&&state.uploadCaps.writable_scopes.length>0);
    const canDelete=downloaded&&(isDemoMode()?true:(d.can_delete===undefined?(!state.uploadCaps||canWrite):Boolean(d.can_delete)));
    $('modal-download').hidden=!downloaded;
    $('modal-delete').hidden=!canDelete;
    $('modal-delete').dataset.docId=canDelete?id:'';
    if(downloaded){
      $('modal-download').href=isDemoMode()
        ? '/api/source-file?id='+encodeURIComponent(id)
        : '/unified/api/uploads/'+encodeURIComponent(id)+'/original';
      $('modal-download').setAttribute('download',d.title);
    }else{
      $('modal-download').removeAttribute('href');
      $('modal-download').removeAttribute('download');
    }
    if(!modal.open)modal.showModal();
  }catch(e){alert('Не удалось открыть документ: '+e.message)}
}
const labels={
  VERIFIED_FOR_DEMO:'Документы сопоставлены',
  CURRENTNESS_UNVERIFIED:'Редакция не подтверждена',
  NOT_FOUND:'Деталь не найдена',
  LOT_NOT_FOUND:'Партия не найдена'
};
const partIssues={
  INVALID_BASE_DRAWING:'Базовый чертёж не соответствует обозначению детали.',
  NO_NOTICE_CHAIN:'В учётной базе нет полной цепочки извещений об изменении.',
  NOTICE_SOURCE_NOT_LINKED:'Не указан исходный файл, подтверждающий извещение.',
  NOTICE_SOURCE_MISMATCH:'Номер или редакция файла расходятся с данными извещения.',
  INVALID_EFFECTIVE_DATE:'У извещения некорректная дата вступления в силу.',
  INCONSISTENT_NOTICE_SEQUENCE:'Нарушен порядок редакций или дат извещений.',
  UNKNOWN_STOCK_DIRECTIVE:'У извещения отсутствует понятное указание о старых партиях.',
  CURRENT_REVISION_MISMATCH:'Последнее извещение и учётная редакция детали не совпадают.',
  INVALID_BATCH_DATE:'Некорректная дата выпуска партии.',
  NOTICE_NOT_YET_EFFECTIVE:'Извещение ещё не вступило в силу.',
  FUTURE_BATCH_DATE:'В учётных данных указана будущая дата выпуска партии.'
};
function datum(label,value){
  const row=el('div','datum');row.append(el('span','datum-label',label),el('strong','datum-value',value));return row;
}
function showDate(iso){
  if(typeof iso!=='string' || !/^\d{4}-\d{2}-\d{2}$/.test(iso))return String(iso||'');
  return iso.split('-').reverse().join('.');
}
function evidenceSection(evidence){
  const section=el('section','part-evidence');
  section.append(el('h3','','Документы, использованные при сравнении'));
  for(const doc of evidence){
    const item=el('div','part-evidence-item');
    const info=el('div','part-evidence-info');
    info.append(el('strong','',doc.title),
      el('span','',doc.kind==='drawing'?'Базовый чертёж · ред. '+doc.revision:
        'Извещение '+doc.notice_id+' · ред. '+doc.revision+
        ' · с '+showDate(doc.effective_from)));
    const open=el('button','secondary-button part-evidence-open','Открыть');
    open.type='button';open.append(uiIcon('arrow-up-right'));
    open.addEventListener('click',()=>openDocument(doc.doc_id));
    item.append(info,open);section.append(item);
  }
  return section;
}
async function loadPart(){
  const out=$('part-result');out.replaceChildren(el('div','loading','Сопоставляю партию с документами…'));
  try{
    const code=$('part-code').value.trim(),lot=$('lot-id').value;
    if(!code){out.replaceChildren(el('div','message-error','Введите обозначение детали.'));return}
    const r=await request('/api/part?code='+encodeURIComponent(code)+'&lot='+encodeURIComponent(lot));
    const known=r.state==='VERIFIED_FOR_DEMO';
    const matches=known&&r.lot?.status==='REVISION_MATCHES';
    const needsReview=known&&r.lot?.status==='REQUIRES_ENGINEER_REVIEW';
    const box=el('div','part-result-card '+(matches?'good':'attention'));
    box.append(el('span','result-marker',matches?'СООТВЕТСТВИЕ УЧЕБНЫХ ЗАПИСЕЙ':
      needsReview?'ТРЕБУЕТ РЕШЕНИЯ КОНСТРУКТОРА':'НЕ ПОДТВЕРЖДЕНО'));
    box.append(el('h2','',needsReview?'Партию нужно проверить':labels[r.state]||'Недостаточно данных'));
    let intro='';
    if(r.state==='CURRENTNESS_UNVERIFIED')
      intro=r.issue?(partIssues[r.issue]||'В учётных данных есть несоответствие.'):
        'Для выбранной роли недостаточно сведений, чтобы подтвердить редакцию.';
    else if(known)
      intro=needsReview?'Для партии указана редакция, отличающаяся от текущей, либо не действовавшая на дату выпуска.':
        'В учебной базе доступны согласованные сведения о чертеже и изменениях.';
    else intro='Проверьте обозначение детали и номер партии.';
    box.append(el('p','part-intro',intro));
    if(r.part)box.append(datum('Обозначение',r.part));
    if(known){
      box.append(datum('Базовая редакция',r.base_revision),
                 datum('Редакция по последнему извещению',r.current_revision));
      if(r.lot){
        box.append(datum('Партия',r.lot.id),
                   datum('Дата выпуска',showDate(r.lot.manufactured_at)),
                   datum('По какой редакции изготовлена',r.lot.built_revision),
                   datum('Редакция на дату выпуска',r.lot.expected_at_manufacture));
        if(r.lot.directive)
          box.append(datum('Указание по старому заделу',r.lot.directive));
        if(needsReview){
          box.append(el('p','part-decision-note',
            'Не определяйте пригодность партии по этому результату. Нужна проверка конструктором.'));
        }
      }
      if(r.timeline?.length){
        const timeline=el('section','part-timeline');
        timeline.append(el('h3','','История изменений'));
        for(const notice of r.timeline){
          timeline.append(el('p','',
            'С '+showDate(notice.effective_from)+' · '+notice.notice_id+
            ' · редакция '+notice.new_revision+
            ' · '+notice.directive.toLowerCase()));
        }
        box.append(timeline);
      }
    }else if(r.state==='CURRENTNESS_UNVERIFIED'){
      box.append(el('p','part-decision-note',
        'Не используйте эту проверку для решения о выпуске партии. Уточните комплект документов у конструктора.'));
    }
    if(r.evidence?.length)box.append(evidenceSection(r.evidence));
    box.append(el('p','synthetic-warning','Только учебная проверка. Даже совпадение редакций не подтверждает качество и не разрешает выпуск.'));
    out.replaceChildren(box);
  }catch(e){errorIn(out,e.message)}
}

function engineeringDatum(label,value){
  const item=el('div','engineering-datum');
  item.append(el('span','engineering-label',label),el('strong','engineering-value',value ?? 'Не указано'));
  return item;
}
function engineeringSource(source){
  const item=el('div','engineering-source');
  const title=el('strong','',source.name);
  const kind={BOM:'Спецификация',DRAWING:'PDF-чертёж',MODEL:'STEP-модель'}[source.role] || source.kind;
  const head=el('div','engineering-source-head');
  head.append(el('span','engineering-source-kind',kind),title);
  item.append(head);
  item.append(el('p','','Основание связи: '+source.matching+' · '+source.evidence));
  item.append(el('code','','SHA-256: '+source.sha256));
  const actions=el('div','engineering-source-actions');
  const download=el('a','secondary-button','Скачать проверенный оригинал');
  download.href='/api/engineering/file?id='+encodeURIComponent(source.id);
  download.setAttribute('download','');
  const original=el('a','source-link','Открыть публикацию');
  original.href=source.url;
  original.target='_blank';original.rel='noopener noreferrer';
  actions.append(download,original);
  item.append(actions);
  return item;
}
async function loadEngineeringQuery(){
  const out=$('engineering-result');
  out.replaceChildren(el('div','loading','Сопоставляю реальные источники…'));
  const serial=state.sessionSerial;
  try{
    const data=await request('/api/engineering/ask?q='+encodeURIComponent($('engineering-query').value.trim()));
    if(serial!==state.sessionSerial || state.page!=='engineering')return;
    const box=el('div','part-result-card attention');
    const matched=data.state==='BOM_DRAWING_STEP_LINKED';
    box.append(el('span','result-marker',matched?'СВЯЗИ ПО ИМЕНИ ФАЙЛА НАЙДЕНЫ':'НЕ ПОДТВЕРЖДЕНО'));
    box.append(el('h2','',matched?'Найден комплект из трёх источников':
      data.state==='AMBIGUOUS'?'Несколько совпадений':data.state==='NOT_FOUND'?
      'Невозможно подтвердить связь':'Неполный комплект'));
    box.append(el('p','part-intro',data.message||'Для надёжного вывода недостаточно сведений.'));
    if(data.part){
      const details=el('div','engineering-details');
      details.append(engineeringDatum('Проект',data.group),
        engineeringDatum('Деталь',data.part),
        engineeringDatum('Позиция BOM',data.item_no),
        engineeringDatum('Материал',data.material),
        engineeringDatum('Количество',data.quantity?data.quantity+' '+(data.unit||''):'Не указано'),
        engineeringDatum('Строка исходной BOM',data.bom_row_number));
      box.append(details);
    }
    if(data.sources?.length){
      box.append(el('h3','engineering-source-title','Проверяемые оригиналы'));
      const sources=el('div','engineering-sources');
      for(const item of data.sources)sources.append(engineeringSource(item));
      box.append(sources);
    }
    if(data.candidates?.length){
      const group=el('div','engineering-candidates');
      for(const candidate of data.candidates)group.append(el('p','',candidate.part_key));
      box.append(group);
    }
    box.append(el('p','synthetic-warning','Статус редакции: НЕ ПОДТВЕРЖДЕНА. Статус производственного допуска: НЕ УСТАНОВЛЕН. Совпадение имён файлов не заменяет инженерную экспертизу.'));
    out.replaceChildren(box);
    void loadP8PartQuery();
    void loadP9PartQuery();
    void loadP10PartQuery();
  }catch(error){
    if(serial===state.sessionSerial)errorIn(out,error.message);
  }
}
async function loadEngineering(){
  const serial=state.sessionSerial;
  $('engineering-summary').replaceChildren(el('div','loading','Читаю индекс корпуса…'));
  $('engineering-historical').replaceChildren();
  try{
    const [summary,notice]=await Promise.all([
      request('/api/engineering/summary'),
      request('/api/engineering/notice?id='+encodeURIComponent('БВАИ.107-2012'))
    ]);
    if(serial!==state.sessionSerial||state.page!=='engineering')return;
    const stats=el('div','engineering-stats');
    for(const [label,value] of [
      ['Оригинальных файлов',summary.sources],
      ['Строк спецификаций',summary.bom_lines],
      ['Точных файловых связей',summary.links],
      ['Групп проектов',summary.groups.length]
    ])stats.append(engineeringDatum(label,String(value)));
    $('engineering-summary').replaceChildren(stats);
    if(notice.state==='HISTORICAL_REFERENCE_ONLY'){
      const node=el('div','engineering-history-details');
      node.append(el('p','','Источник: '+notice.document_id+
        ' · страница '+notice.page+' · '+notice.change_description));
      node.append(el('p','','Дата изменения указана по справочной карточке: '+notice.effective_date_reference+'. Применимость к текущей продукции не установлена.'));
      const ref=el('a','source-link','Открыть исходную публикацию');
      ref.href=notice.source_url;ref.target='_blank';ref.rel='noopener noreferrer';
      node.append(ref);
      $('engineering-historical').replaceChildren(node);
    }
    await loadP8Summary();
    await loadEngineeringQuery();
  }catch(error){
    if(serial===state.sessionSerial)errorIn($('engineering-summary'),error.message);
  }
}

const p8Descriptions={
  MATCH:'Обозначение совпадает',
  MISMATCH:'Обнаружено различие обозначений',
  NOT_CHECKABLE:'Сопоставление не выполнено',
  CANDIDATE_FIELDS_EXTRACTED:'Поля прочитаны, но требуют инженерного подтверждения',
  NOT_CONFIRMED:'Штамп не удалось надёжно распознать',
  NOT_APPLICABLE:'К этому виду файла проверка не применяется',
  BREP_VALID:'BREP-топология корректна',
  BREP_REVIEW_REQUIRED:'Геометрия требует дополнительной проверки',
  ENGINEERING_VERIFICATION_INCOMPLETE:'Проверка актуальности не завершена',
  VISIBLE_GRADE_TOKEN_MATCH:'В чертеже и BOM совпадает обозначение марки',
  GRADE_CODES_DIFFER_REVIEW_REQUIRED:'Марки записаны по-разному, требуется экспертная сверка',
  GRADE_NOT_COMPARABLE:'Недостаточно данных для сравнения марки',
  ARCBLUE_REVISION_NOT_CONFIRMABLE_FROM_TEXT_LAYER:'В текстовом слое чертежа не установлена достоверная редакция',
  TITLE_REVISION_ABSENT_FROM_VISIBLE_HISTORY:'Редакция штампа не найдена в видимой таблице изменений',
  NO_PASSPORT_TITLE_BLOCK_FOUND:'Штамп Passport в данном PDF не найден',
  NO_BCN3D_TITLE_BLOCK_FOUND:'Штамп BCN3D в данном PDF не найден',
  OCR_REQUIRES_VISUAL_REVIEW:'Результат OCR необходимо сравнить с изображением',
  OCR_UNAVAILABLE_OR_FAILED:'OCR не удалось выполнить',
  NO_KNOWN_TITLE_BLOCK_PARSER:'Для данного формата штампа нет проверенного парсера',
  NO_AUTHORITATIVE_REVISION_RECORD:'Нет удостоверенной учётной редакции',
  NO_RELEASED_PLM_CONFIGURATION:'Нет зарегистрированного статуса выпуска в PLM',
  DRAWING_MODEL_GEOMETRY_NOT_COMPARED:'Геометрия модели не сопоставлена с размерами чертежа',
  MATERIAL_GRADE_REQUIRES_ENGINEER_REVIEW:'Сопоставление марки материала требует конструктора',
  STEP_BREP_NOT_VALIDATED:'Топология STEP не подтверждена',
  SAME_LABEL:'Подписи версии совпадают',
  DIFFERENT_OR_UNAVAILABLE:'Обозначения отличаются либо отсутствуют'
};
function p8Human(value){
  return typeof value==='string'?(p8Descriptions[value]||value):value;
}
function p8Field(label,value){
  return engineeringDatum(label,value===null||value===undefined||value===''?'Не установлено':String(p8Human(value)));
}
function p8Notice(message){
  return el('p','p8-review-warning',message);
}
function p8SourceInspection(report){
  const inspect=report.inspection||{};
  const box=el('section','p8-inspection-card');
  box.append(el('h3','',report.source_name||'Инженерный источник'));
  const details=el('div','engineering-details');
  details.append(p8Field('Тип источника',report.source_kind),
    p8Field('Обозначение в чертеже',inspect.designation),
    p8Field('Редакция из штампа',inspect.revision),
    p8Field('Марка материала в чертеже',inspect.material_grade),
    p8Field('Сверка с именем файла',report.filename_titleblock_comparison),
    p8Field('Статус распознавания',inspect.recognition||inspect.state));
  box.append(details);
  if(inspect.revision_history?.length){
    const history=el('div','p8-history');
    history.append(el('h4','','Записи в таблице изменений на чертеже'));
    for(const item of inspect.revision_history){
      history.append(el('p','',item.revision+' — '+(item.date_as_printed||'дата не прочитана')));
    }
    box.append(history);
  }
  if(inspect.geometry_loaded){
    const block=el('div','p8-geometry');
    block.append(el('h4','','OpenCascade: топология STEP'));
    const values=el('div','engineering-details');
    values.append(p8Field('Проверка BREP',inspect.state),
      p8Field('Твёрдых тел',inspect.solid_count),
      p8Field('Граней',inspect.face_count),
      p8Field('Габариты по осям CAD',inspect.xyz_extent?.join(' × ')),
      p8Field('Объём, ед. CAD³',inspect.volume_in_cad_units_cubed));
    block.append(values);
    block.append(p8Notice('Единицы и соответствие размеров PDF не подтверждены. Корректная BREP-топология не доказывает идентичность чертежу.'));
    box.append(block);
  }
  if(inspect.evidence?.length){
    const list=el('div','p8-evidence');
    list.append(el('h4','','Извлечённые значения с координатами'));
    for(const field of inspect.evidence){
      const position=Array.isArray(field.bbox)?field.bbox.slice(0,2).join(', '):'?';
      list.append(el('p','',field.field+': '+field.value+' · страница '+field.page+
        ' · относительные координаты '+position));
    }
    box.append(list);
  }
  for(const warning of inspect.warnings||[]){
    box.append(p8Notice('Требует проверки: '+p8Human(warning)));
  }
  if(inspect.ocr_candidate){
    box.append(p8Notice('OCR: НЕПОДТВЕРЖДЁННАЯ ПОДСКАЗКА. Значения нельзя применять без сверки изображения.'));
    box.append(el('pre','p8-ocr-preview',inspect.ocr_candidate.text||'Надпись не распознана'));
  }
  const link=el('a','secondary-button','Оригинал для сверки');
  link.href='/api/engineering/file?id='+encodeURIComponent(report.source_id);
  link.setAttribute('download','');
  box.append(link);
  box.append(p8Notice('Подлинная редакция, утверждение и применимость к партии по этому файлу не подтверждены.'));
  return box;
}
async function loadP8Summary(){
  const serial=state.sessionSerial;
  const target=$('p8-summary');
  target.replaceChildren(el('div','loading','Загружаю результаты инженерных проверок…'));
  try{
    const [summary,catalog]=await Promise.all([
      request('/api/engineering/p8/summary'),
      request('/api/engineering/p8/catalog')
    ]);
    if(serial!==state.sessionSerial||state.page!=='engineering')return;
    const stats=el('div','p8-stats');
    for(const [name,value] of [
      ['Чертежи PDF',summary.pdf],
      ['Штампы с извлечёнными полями',summary.recognized_title_blocks],
      ['BREP-топологии проверены',summary.valid_cad_breps],
      ['CAD-модели STEP',summary.step]
    ])stats.append(p8Field(name,value));
    target.replaceChildren(stats);
    const selector=$('p8-source-select');
    selector.replaceChildren();
    for(const source of catalog.items){
      const option=el('option','',source.group+' · '+source.name);
      option.value=source.id;
      selector.append(option);
    }
    const passport=catalog.items.find(item=>item.name==='FD_1842300-H_Front-Housing.PDF');
    if(passport)selector.value=passport.id;
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
async function loadP8PartQuery(){
  const serial=state.sessionSerial, query=$('engineering-query').value.trim();
  const target=$('p8-part-result');
  target.replaceChildren(el('div','loading','Проверяю штамп PDF и геометрию CAD…'));
  try{
    const report=await request('/api/engineering/p8/part?q='+encodeURIComponent(query));
    if(serial!==state.sessionSerial||state.page!=='engineering'||
       query!==$('engineering-query').value.trim())return;
    const result=el('div','p8-result');
    if(report.state!=='ENGINEERING_VERIFICATION_INCOMPLETE'){
      result.append(p8Notice('Для этого обозначения нет полного подтверждённого файлового комплекта. Проверка чертежа и CAD не выполнялась.'));
      target.replaceChildren(result);return;
    }
    result.append(el('h3','','Деталь '+report.part));
    result.append(p8Notice('РЕДАКЦИЯ НЕ ПОДТВЕРЖДЕНА. Связь со штампом, PLM и производственным допуском не установлена.'));
    result.append(p8Field('Марка материала: сравнение',report.material_grade_comparison));
    result.append(p8Field('Состояние проверки',report.state));
    if(report.drawing)result.append(p8SourceInspection(report.drawing));
    if(report.step_model)result.append(p8SourceInspection(report.step_model));
    if(report.risks?.length){
      const warning=el('div','p8-risk-list');
      warning.append(el('h4','','Причины, по которым нельзя подтвердить актуальность'));
      for(const issue of report.risks)warning.append(el('p','',p8Human(issue)));
      result.append(warning);
    }
    target.replaceChildren(result);
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
async function p8InspectSelected(ocr=false){
  const selected=$('p8-source-select').value;
  const target=$('p8-source-result'),serial=state.sessionSerial;
  if(!selected){target.replaceChildren(p8Notice('Выберите документ.'));return}
  target.replaceChildren(el('div','loading',
    ocr?'OCR ограниченной области штампа…':'Читаю проверенный оригинал…'));
  try{
    const kind=ocr?'ocr-hint':'source';
    const report=await request('/api/engineering/p8/'+kind+'?id='+encodeURIComponent(selected));
    if(serial!==state.sessionSerial||state.page!=='engineering'||
       selected!==$('p8-source-select').value)return;
    target.replaceChildren(p8SourceInspection(report));
    $('p8-source-ocr').hidden=report.source_kind!=='PDF';
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
async function p8CompareTopFrames(){
  const target=$('p8-compare-result'),serial=state.sessionSerial;
  target.replaceChildren(el('div','loading','Сравниваю две версии чертежа…'));
  try{
    const report=await request('/api/engineering/p8/compare-top');
    if(serial!==state.sessionSerial||state.page!=='engineering')return;
    const box=el('div','p8-compare-grid');
    for(const [title,side] of [['BCN3D Sigmax',report.left],['BCN3D R19',report.right]]){
      const item=el('article','p8-compare-side');
      item.append(el('h3','',title));
      item.append(p8Field('Обозначение',side.inspection.designation),
                  p8Field('Версия, написанная в чертеже',side.inspection.revision));
      item.append(p8SourceInspection(side));
      box.append(item);
    }
    const notes=el('div','p8-review-warning');
    notes.textContent='Результат: '+p8Human(report.designation_comparison)+' · '+p8Human(report.version_label_comparison)+
      '. Общая метка V1.0 НЕ означает одинаковый чертёж. Оформленное извещение не обнаружено.';
    target.replaceChildren(notes,box);
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
async function p8InspectHistoricalNotice(){
  const target=$('p8-notice-result'),serial=state.sessionSerial;
  target.replaceChildren(el('div','loading','Извлекаю реквизиты исходного извещения…'));
  try{
    const report=await request('/api/engineering/p8/notice');
    if(serial!==state.sessionSerial||state.page!=='engineering')return;
    const box=el('div','p8-historical-result');
    const pdf=report.pdf_inspection||{};
    box.append(p8Field('Идентификация исходного PDF',pdf.state),
      p8Field('Номер извещения',pdf.notice_id),
      p8Field('Номер документа',pdf.document_id),
      p8Field('Страница оригинала',pdf.page));
    if(pdf.change_excerpt)box.append(el('p','p8-excerpt','Как извлёк PDF-парсер: '+pdf.change_excerpt));
    box.append(p8Notice('Числа и знаки допусков в тексте PDF искажены. Сведения каталога об изменении и дате НЕ подтверждены независимым чтением оригинала; требуется визуальная экспертиза.'));
    target.replaceChildren(box);
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}

let p9ActiveQuery='';
let p9ActiveResult=null;
function p9OriginCard(kind,source){
  const card=el('div','p9-provenance');
  card.append(el('strong','',kind+': '+source.name));
  card.append(el('code','','SHA-256: '+source.sha256));
  const actions=el('div','p9-origin-actions');
  const local=el('a','secondary-button','Проверенный оригинал');
  local.href='/api/engineering/file?id='+encodeURIComponent(source.id);
  local.setAttribute('download','');
  const published=el('a','source-link','Первоисточник');
  published.href=source.original_url;
  published.target='_blank';published.rel='noopener noreferrer';
  actions.append(local,published);
  card.append(actions);
  return card;
}
async function loadP9PartQuery(){
  const serial=state.sessionSerial;
  const query=$('engineering-query').value.trim();
  p9ActiveResult=null;p9ActiveQuery='';
  const target=$('p9-source-info'),answer=$('p9-comparison-result');
  target.replaceChildren(el('div','loading','Извлекаю значения с координатами PDF…'));
  answer.replaceChildren();
  $('p9-annotation').replaceChildren();
  try{
    const result=await request('/api/engineering/p9/annotations?q='+encodeURIComponent(query));
    if(serial!==state.sessionSerial||state.page!=='engineering'||
      $('engineering-query').value.trim()!==query)return;
    if(result.state!=='CANDIDATE_DIMENSIONS_FOR_REVIEW'){
      target.replaceChildren(p8Notice('Для этого обозначения нет подтверждённой P7-связки BOM → PDF → STEP. Сравнение недоступно.'));
      return;
    }
    p9ActiveQuery=query;p9ActiveResult=result;
    const head=el('div','p9-summary');
    head.append(p9OriginCard('Чертёж',result.drawing),
      p9OriginCard('STEP-модель',result.model));
    const measures=el('div','engineering-details p9-axes');
    for(const [axis,index] of [['X',0],['Y',1],['Z',2]]){
      measures.append(p9Field('Габарит CAD '+axis,
        result.cad?.xyz_extent?.[index]+' '+(result.cad.units==='millimetre'?'мм':'единицы не установлены')));
    }
    target.replaceChildren(head,measures,
      p8Notice('Список содержит только кандидаты из текстового слоя PDF. Значение на чертеже может относиться к локальной детали, отверстию или сечению, а не к общему габариту CAD.'));
    const select=$('p9-annotation');
    for(const item of result.annotations){
      const types={LINEAR:'число',RADIUS:'радиус (без сопоставления)',ANGULAR:'угол (без сопоставления)',DIAMETER:'диаметр (без сопоставления)',COMPOSITE:'составной размер (без сопоставления)'};
      const option=el('option','',item.label+' '+(item.unit==='mm'?'мм':item.unit||'')+
        ' · стр. '+item.page+' · '+(types[item.kind]||item.kind));
      option.value=item.annotation_id;option.disabled=item.kind!=='LINEAR';
      select.append(option);
    }
    const preferred=result.part==='focuser_drawtube'?'80':
      result.part==='lower_square_tube'?'437':'24';
    const recommended=result.annotations.find(x=>x.label===preferred&&x.kind==='LINEAR')||
      result.annotations.find(x=>x.kind==='LINEAR');
    if(recommended){
      select.value=recommended.annotation_id;
      $('p9-axis').value='y';
      $('p9-window').value='0.02';
      await runP9Compare();
    }else{
      answer.replaceChildren(p8Notice('Числовых размерных кандидатов не обнаружено.'));
    }
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
function p9Field(label,value){return engineeringDatum(label,String(value))}
async function runP9Compare(){
  const serial=state.sessionSerial;
  const query=p9ActiveQuery;
  const target=$('p9-comparison-result');
  if(!p9ActiveResult||query!==$('engineering-query').value.trim()){
    target.replaceChildren(p8Notice('Сначала выполните поиск инженерной детали.'));
    return;
  }
  const annotation=$('p9-annotation').value;
  const axis=$('p9-axis').value;
  const windowValue=$('p9-window').value;
  target.replaceChildren(el('div','loading','Перепроверяю источники и сравниваю выбранные числа…'));
  try{
    const url='/api/engineering/p9/compare?q='+encodeURIComponent(query)+
      '&annotation='+encodeURIComponent(annotation)+
      '&axis='+encodeURIComponent(axis)+
      '&window='+encodeURIComponent(windowValue);
    const result=await request(url);
    if(serial!==state.sessionSerial||state.page!=='engineering'||
       query!==$('engineering-query').value.trim()||
       annotation!==$('p9-annotation').value||axis!==$('p9-axis').value||
       windowValue!==$('p9-window').value)return;
    const section=el('section','p9-comparison-card');
    const near=result.state==='NUMERIC_WITHIN_SELECTED_WINDOW';
    section.append(el('h3','',near?'Числа находятся в выбранном интервале':
      result.state==='NUMERIC_OUTSIDE_SELECTED_WINDOW'?'Числа отличаются':'Проверка невозможна'));
    if(result.annotation){
      const facts=el('div','engineering-details p9-facts');
      facts.append(p9Field('Надпись PDF',result.annotation.label+
        ' '+result.annotation.unit),
        p9Field('Габарит модели, ось '+result.chosen_axis?.toUpperCase(),
                result.cad_bbox_extent+' мм'),
        p9Field('Абсолютная разница',result.absolute_difference_mm+' мм'),
        p9Field('Выбранное окно',result.selected_window_mm+' мм'),
        p9Field('Лист PDF',result.annotation.page));
      section.append(facts);
      const bounds=result.annotation.bbox||[];
      if(bounds.length===4){
        section.append(el('p','p9-evidence-location',
          'Относительные координаты надписи: ['+bounds.join(', ')+'].'));
      }
    }
    section.append(p8Notice(near?
      'Совпадают только числовые значения в пределах указанного вами окна. Какая конструктивная особенность изображена и соответствует ли она габариту CAD, не установлено.':
      'Расхождение не является доказательством дефекта: выбранная надпись может обозначать другой элемент конструкции.'));
    section.append(el('p','p9-denial','Соответствие геометрии: НЕ УСТАНОВЛЕНО. Чертёжные допуски: НЕ ПРОВЕРЕНЫ. Редакция: НЕ ПОДТВЕРЖДЕНА. Производственный допуск: НЕ ПОДТВЕРЖДЁН.'));
    target.replaceChildren(section);
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
let p10Evidence=null;
let p10Query='';
function p10SelectSurface(){
  const selected=$('p10-callout').value;
  const ann=p10Evidence?.drawing_annotations.find(x=>x.annotation_id===selected);
  const list=$('p10-surface');
  list.replaceChildren();
  if(!ann)return;
  const compatible=p10Evidence.features.filter(f=>
    (ann.kind==='RADIUS'&&f.kind==='CYLINDER_RADIUS')||
    (ann.kind==='ANGULAR'&&f.kind==='PLANE_DIRECTION'));
  for(const surface of compatible){
    const name=surface.kind==='CYLINDER_RADIUS'?
      'Цилиндр R'+surface.value+' мм · граней '+surface.face_occurrences:
      'Плоскость · угол к X '+surface.angles_to_axes.x+'° · граней '+surface.face_occurrences;
    const option=el('option','',name);
    option.value=surface.feature_id;
    list.append(option);
  }
  const numericallyMatching=compatible.filter(x=>{
    if(ann.kind==='RADIUS')return Math.abs(x.value-ann.nominal)<0.03;
    return Math.abs(x.angles_to_axes.x-ann.nominal)<0.03;
  });
  if(numericallyMatching.length===1){
    list.value=numericallyMatching[0].feature_id;
  }else{
    const placeholder=el('option','',
      numericallyMatching.length>1?
      'Несколько совпадений: выберите грань для проверки':
      'Числового совпадения нет: выберите гипотезу вручную');
    placeholder.value='';placeholder.disabled=true;
    list.prepend(placeholder);
    list.value='';
  }
  $('p10-reference-axis').disabled=ann.kind!=='ANGULAR';
  $('p10-feature-result').replaceChildren();
  void p11PreviewSelection();
}
async function loadP10PartQuery(){
  const serial=state.sessionSerial, query=$('engineering-query').value.trim();
  p10Evidence=null;p10Query='';p11Pending=null;
  $('p11-preview-area').replaceChildren();
  $('p11-current-pair').replaceChildren();
  $('p11-save-result').replaceChildren();
  $('p11-history').replaceChildren();
  $('p12-cad-image').replaceChildren();
  $('p12-cad-proof').replaceChildren();
  $('p12-witness-status').replaceChildren();
  p13Key='';p13Sequence++;
  $('p13-vector-preview').replaceChildren();
  $('p13-vector-proof').replaceChildren();
  $('p13-match-summary').replaceChildren();
  $('p13-candidates').replaceChildren();
  $('p11-review-form').hidden=state.me?.id!=='engineer';
  const target=$('p10-feature-summary');
  target.replaceChildren(el('div','loading','Извлекаю аналитические поверхности STEP…'));
  $('p10-callout').replaceChildren();
  $('p10-surface').replaceChildren();
  $('p10-feature-result').replaceChildren();
  $('p10-revision-result').replaceChildren(el('div','loading','Проверяю подключение к реестру редакций…'));
  try{
    const [features,revision]=await Promise.all([
      request('/api/engineering/p10/features?q='+encodeURIComponent(query)),
      request('/api/engineering/p10/revisions?q='+encodeURIComponent(query))
    ]);
    if(serial!==state.sessionSerial||state.page!=='engineering'||query!==$('engineering-query').value.trim())return;
    const registry=el('div','p10-revision-status');
    registry.append(p10Value('Источник редакции',revision.state==='NO_AUTHORITATIVE_CONNECTOR'?
      'PLM/1С не подключена':revision.state==='SIGNED_SNAPSHOT_INTEGRITY_VERIFIED'?
      'Подпись технического снимка проверена, полномочия неизвестны':revision.state),
      p10Value('Действующая утверждённая редакция','НЕ УСТАНОВЛЕНА'));
    registry.append(p8Notice(revision.note||'Без настоящего корпоративного источника, подтверждения полномочий и контроля действующей редакции выдавать разрешения нельзя.'));
    $('p10-revision-result').replaceChildren(registry);
    if(features.state!=='FEATURE_CANDIDATES_FOR_HUMAN_MAPPING'){
      target.replaceChildren(p8Notice('Нет точного комплекта BOM + PDF + STEP для проверки аналитических поверхностей.'));
      return;
    }
    p10Evidence=features;p10Query=query;
    const stats=el('div','engineering-details p10-stat-grid');
    stats.append(p10Value('Аналитических элементов STEP',features.features_count),
      p10Value('Всего граней',features.cad_face_count),
      p10Value('Размеченных радиусов и углов PDF',features.drawing_annotations.length),
      p10Value('Единицы STEP',features.cad_source_units));
    target.replaceChildren(stats);
    const select=$('p10-callout');
    for(const ann of features.drawing_annotations){
      const opt=el('option','',ann.label+' · лист '+ann.page+
        ' · '+(ann.kind==='RADIUS'?'радиус':'угол'));
      opt.value=ann.annotation_id;select.append(opt);
    }
    const desired=features.part==='focuser_drawtube'?'R41.5':
      features.part==='lower_square_tube'?'67.38°':'R128.8';
    const found=features.drawing_annotations.find(a=>a.label===desired);
    if(found)select.value=found.annotation_id;
    p10SelectSurface();
    void loadP11History();
    void loadP12Audit();
    if($('p10-surface').value)await runP10Compare();
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
function p10Value(label,value){return engineeringDatum(label,String(value))}
async function runP10Compare(){
  const target=$('p10-feature-result');
  if(!p10Evidence||!$('p10-callout').value||!$('p10-surface').value){
    target.replaceChildren(p8Notice('Нет пары подходящих аналитических элементов.'));
    return;
  }
  const serial=state.sessionSerial;
  const query=p10Query;
  const annotation=$('p10-callout').value;
  const feature=$('p10-surface').value;
  const axis=$('p10-reference-axis').value;
  target.replaceChildren(el('div','loading','Повторно проверяю исходники и вычисляю значение поверхности…'));
  try{
    const data=await request('/api/engineering/p10/compare?q='+encodeURIComponent(query)+
      '&annotation='+encodeURIComponent(annotation)+'&feature='+encodeURIComponent(feature)+
      '&axis='+encodeURIComponent(axis));
    if(serial!==state.sessionSerial||state.page!=='engineering'||query!==p10Query||
       annotation!==$('p10-callout').value||feature!==$('p10-surface').value||
       axis!==$('p10-reference-axis').value)return;
    const box=el('div','p10-comparison-card');
    const agrees=data.state==='ANALYTIC_VALUES_WITHIN_SELECTED_WINDOW';
    box.append(el('h3','',agrees?'Числа аналитической поверхности совпадают':
      data.state==='ANALYTIC_VALUES_OUTSIDE_SELECTED_WINDOW'?'Числа аналитической поверхности отличаются':
      'Сравнение не выполнено'));
    if(data.annotation&&data.surface){
      const details=el('div','engineering-details p10-result-grid');
      details.append(p10Value('Надпись PDF',data.annotation.label),
        p10Value('Измеренное значение STEP',data.cad_surface_measurement+' '+(data.unit==='degree'?'°':'мм')),
        p10Value('Абсолютная разница',data.absolute_difference+' '+(data.unit==='degree'?'°':'мм')),
        p10Value('Числовой интервал',data.selected_window+' '+(data.unit==='degree'?'°':'мм')),
        p10Value('Связанных CAD-граней',data.surface.face_occurrences));
      box.append(details);
      box.append(el('p','p10-coordinate','PDF: лист '+data.annotation.page+' · координаты надписи '+
        data.annotation.bbox.join(', ')+'; STEP: грани №'+data.surface.sample_face_indices.join(', ')));
    }
    box.append(p8Notice('Соответствие выноски PDF выбранной поверхности не подтверждено. Проверка допусков и действующей редакции не выполнена. Разрешения на изготовление нет.'));
    target.replaceChildren(box);
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
let p11Pending=null;
function p11HumanDecision(decision){
  return {PLAUSIBLE_MAPPING:'Визуально похоже на соответствие',
    NOT_SAME_FEATURE:'Выбранная поверхность не соответствует обозначению',
    NEEDS_MORE_EVIDENCE:'Нужна дополнительная проверка'}[decision]||decision;
}
function p11PreviewSelection(){
  const area=$('p11-preview-area');
  const pair=$('p11-current-pair');
  $('p11-save-result').replaceChildren();
  const annotation=$('p10-callout').value,feature=$('p10-surface').value;
  void p13LoadVectors();
  if(!p10Evidence||!annotation||!feature){
    area.replaceChildren(p8Notice('Выберите надпись и CAD-поверхность в блоке P10.'));
    pair.replaceChildren();
    $('p12-cad-image').replaceChildren();
    $('p12-cad-proof').replaceChildren();
    return;
  }
  const actual=p10Evidence.drawing_annotations.find(x=>x.annotation_id===annotation);
  const surface=p10Evidence.features.find(x=>x.feature_id===feature);
  if(!actual||!surface){
    area.replaceChildren(p8Notice('Доказательство из выбранного источника не найдено.'));
    pair.replaceChildren();return;
  }
  const section=el('div','p11-visual-evidence');
  section.append(el('h3','','Фрагмент оригинального PDF с выделенной надписью'));
  const image=el('img','p11-crop');
  image.alt='Оригинальный чертёж: область обозначения '+actual.label+
    ', красная рамка выделяет текстовую надпись';
  image.loading='eager';
  image.decoding='async';
  image.onerror=()=>{if(section.isConnected)section.append(p8Notice('Не удалось визуализировать подтверждённый фрагмент PDF.'));};
  image.src='/api/engineering/p11/preview?q='+encodeURIComponent(p10Query)+
    '&annotation='+encodeURIComponent(annotation);
  section.append(image);
  const footer=el('div','p11-source-detail');
  footer.append(el('span','',actual.label+' · PDF, лист '+actual.page),
    el('span','','Координаты '+actual.bbox.join(', ')));
  const original=el('a','source-link','Сверить полный PDF');
  original.href='/api/engineering/file?id='+encodeURIComponent(p10Evidence.drawing.id);
  original.setAttribute('download','');
  footer.append(original);
  section.append(footer);
  area.replaceChildren(section);
  const details=el('div','engineering-details p11-selection-details');
  details.append(p10Value('Предполагаемая CAD-поверхность',
    surface.kind==='CYLINDER_RADIUS'?'Цилиндрический радиус '+surface.value+' мм':
      'Плоскость, угол к X '+surface.angles_to_axes.x+'°'),
    p10Value('Исходные CAD-грани',surface.sample_face_indices.join(', ')),
    p10Value('SHA-256 STEP',p10Evidence.model.sha256));
  pair.replaceChildren(details,
    p8Notice('Сходство чисел не подтверждает, что надпись чертежа указывает именно на эту поверхность.'));
  void p12LoadCAD();
}
async function loadP11History(){
  const serial=state.sessionSerial,query=p10Query;
  const target=$('p11-history');
  target.replaceChildren(el('div','loading','Загружаю журнал проверок…'));
  try{
    const result=await request('/api/engineering/p11/findings?q='+encodeURIComponent(query));
    if(serial!==state.sessionSerial||query!==p10Query||
       state.page!=='engineering')return;
    if(!result.items?.length){
      target.replaceChildren(el('p','source-note','Пока нет замечаний по этому комплекту исходных файлов.'));
      return;
    }
    const container=el('div','p11-history-items');
    for(const record of [...result.items].reverse().slice(0,15)){
      const item=el('article','p11-history-item');
      item.append(el('strong','',p11HumanDecision(record.decision)),
        el('p','',record.note));
      const metadata=el('div','p11-history-meta');
      metadata.append(el('span','','Учебная роль: инженер'),
        el('span','','Отметка '+new Date(record.created_at_ms).toLocaleString('ru-RU')),
        el('span','',record.evidence_state==='SOURCE_HASHES_VALID'?
          'Исходные файлы по SHA-256 не изменились':
          'ИСХОДНЫЕ ДОКУМЕНТЫ ИЗМЕНЕНЫ ИЛИ НЕДОСТУПНЫ'));
      item.append(metadata);
      container.append(item);
    }
    target.replaceChildren(container,p8Notice(
      'Цепочка SHA-256 журнала проверена локально, но не имеет внешнего доверенного якоря. Записи не являются юридически значимыми утверждениями.'));
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
async function p11SaveFinding(){
  const serial=state.sessionSerial;
  const status=$('p11-save-result');
  if(state.me?.id!=='engineer'){
    status.replaceChildren(p8Notice('Для записи необходима учебная роль инженера.'));
    return;
  }
  if(!p10Evidence||!$('p10-callout').value||!$('p10-surface').value){
    status.replaceChildren(p8Notice('Не выбрана пара исходных инженерных элементов.'));
    return;
  }
  const input={
    q:p10Query,
    annotation:$('p10-callout').value,
    feature:$('p10-surface').value,
    axis:$('p10-reference-axis').value,
    decision:$('p11-decision').value,
    note:$('p11-note').value.trim()
  };
  const fingerprint=JSON.stringify(input);
  if(!p11Pending||p11Pending.fingerprint!==fingerprint){
    p11Pending={fingerprint,event_id:crypto.randomUUID()};
  }
  const btn=$('p11-save');
  btn.disabled=true;
  status.replaceChildren(el('div','loading','Проверяю контрольные суммы и сохраняю запись…'));
  try{
    const response=await request('/api/engineering/p11/finding',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':state.me.csrf},
      body:JSON.stringify({...input,event_id:p11Pending.event_id})
    });
    if(serial!==state.sessionSerial||p10Query!==input.q)return;
    const box=el('div','p11-confirmation');
    box.append(el('strong','',response.duplicate?
      'Такая запись уже была сохранена':'Замечание сохранено в деможурнале'));
    box.append(el('p','','ID события: '+response.event_id));
    box.append(p8Notice('Статус: заметка учебного проверяющего. Утверждённая редакция и производственный допуск отсутствуют.'));
    status.replaceChildren(box);
    await loadP11History();
    void loadP12Audit();
  }catch(error){
    if(serial===state.sessionSerial)errorIn(status,error.message);
  }finally{btn.disabled=false;}
}
let p13Sequence=0,p13Key='';
function p13Detail(label,value){
  const entry=el('div','p13-proof-row');
  entry.append(el('dt','',label),el('dd','',String(value)));
  return entry;
}
async function p13LoadVectors(){
  const annotation=$('p10-callout').value,axis=$('p10-reference-axis').value;
  const serial=state.sessionSerial,query=p10Query;
  const key=[serial,query,annotation,axis].join('|');
  const display=$('p13-vector-preview'),detail=$('p13-vector-proof');
  const summary=$('p13-match-summary'),candidates=$('p13-candidates');
  if(!p10Evidence||!annotation||!query){
    p13Key='';p13Sequence++;
    display.replaceChildren(p8Notice('Выберите исходный размер PDF и связанную модель STEP.'));
    detail.replaceChildren();summary.replaceChildren();candidates.replaceChildren();
    return;
  }
  if(key===p13Key)return;
  p13Key=key;
  const seq=++p13Sequence;
  display.replaceChildren(el('div','loading','Считываю реальные векторные линии, стрелки и контуры из PDF…'));
  detail.replaceChildren();
  summary.replaceChildren(el('div','loading','Проверяю числа исходных CAD-граней…'));
  candidates.replaceChildren();
  const url=new URLSearchParams({q:query,annotation,axis}).toString();
  // Pixels and JSON have the same authoritative original-file SHA binding.
  const img=el('img','p13-vector-image');
  img.alt='Реальный векторный чертёж: текст выделен рамкой, линия-выноска или дуга обведена оранжевым, концы стрелок выделены жёлтыми маркерами';
  img.loading='eager';img.decoding='async';
  img.onload=()=>{
    if(seq===p13Sequence&&key===p13Key&&serial===state.sessionSerial){
      display.replaceChildren(img);
    }
  };
  img.onerror=()=>{
    if(seq===p13Sequence&&key===p13Key){
      display.replaceChildren(p8Notice('Не удалось отрисовать векторы PDF из проверенного оригинала.'));
    }
  };
  img.src='/api/engineering/p13/vectors?'+url;
  try{
    const report=await request('/api/engineering/p13/associations?'+url);
    if(seq!==p13Sequence||key!==p13Key||serial!==state.sessionSerial||
       query!==p10Query||state.page!=='engineering')return;
    const vector=report.vector_evidence;
    const one=vector.vector_witnesses?.[0];
    const stateText={
      LEADER_ARROW_TOUCHES_DRAWING_CONTOUR:'Стрелка привязана к линии исходного чертежа',
      LEADER_ARROW_OBSERVED_NO_CONTOUR_CONTACT:'Найдена стрелка, контакт с контуром не подтверждён',
      ANGLE_ARC_BETWEEN_TWO_ARROWS_OBSERVED:'Найдена настоящая дуга с двумя наконечниками',
      DOCUMENT_LEADER_AMBIGUOUS:'Векторная выноска однозначно не распознана'
    }[report.document_geometry_state]||'Данные графики требуют проверки';
    const facts=el('dl','p13-proof');
    facts.append(p13Detail('Графическая проверка',stateText),
      p13Detail('Выноска, лист',report.annotation.page),
      p13Detail('SHA-256 PDF',report.drawing.sha256));
    if(one){
      facts.append(p13Detail('Координаты стрелки, pt',one.arrow_tip_page_points.join('; ')),
        p13Detail('Векторный объект PDF',one.document_path_index));
      if(one.vector_contour_contact_pt!=null){
        facts.append(p13Detail('Расстояние до чёрного контура, pt',
          one.vector_contour_contact_pt));
      }
      if(one.arc_segment_count){
        facts.append(p13Detail('Отрезков реальной дуги',one.arc_segment_count));
      }
    }
    detail.replaceChildren(facts);
    const matches=report.cad_candidates;
    const summaryText={
      SINGLE_NUMERIC_CANDIDATE:'Найден один численно совместимый вариант',
      MULTIPLE_NUMERIC_CANDIDATES:'Найдено несколько численно совместимых вариантов',
      NO_NUMERIC_CANDIDATE:'Численно совместимых CAD-поверхностей не найдено'
    }[report.numeric_state]||report.numeric_state;
    const title=el('div','p13-status',summaryText);
    const qualification=el('p','source-note',
      'Окно сравнения: ±'+report.numeric_window+
      ' '+(report.annotation.kind==='RADIUS'?'мм':'°')+
      '. Основание: оригинальная STEP, SHA-256 '+report.model.sha256.slice(0,15)+'… . '+
      'Соответствие стрелки CAD-грани не установлено.');
    summary.replaceChildren(title,qualification);
    if(matches.length===0){
      candidates.replaceChildren(p8Notice('Не выбираю геометрию: по указанному радиусу или углу совпадений STEP нет.'));
    }else{
      const cards=matches.map((item,index)=>{
        const card=el('div','p13-candidate');
        card.append(el('span','p13-tag','Кандидат '+(index+1)+' · не утверждён'),
          el('strong','',item.measured_value+' '+item.unit+
                      ' · '+item.face_occurrences+' CAD-граней'),
          el('p','','Абсолютное расхождение с чертежом: '+item.absolute_difference+' '+item.unit),
          el('p','','Грани из STEP: '+item.sample_face_indices.join(', ')),
          el('p','','ID поверхности: '+item.feature_id));
        const button=el('button','btn btn-secondary','Проверить эту гипотезу в 3D');
        button.type='button';
        button.addEventListener('click',()=>{
          if(serial!==state.sessionSerial||p10Query!==query||
             $('p10-callout').value!==annotation)return;
          const surface=$('p10-surface');
          if(![...surface.options].some(option=>option.value===item.feature_id))return;
          surface.value=item.feature_id;
          surface.dispatchEvent(new Event('change',{bubbles:true}));
          $('p12-cad-preview')?.scrollIntoView({block:'center',behavior:'smooth'});
        });
        card.append(button);
        return card;
      });
      candidates.replaceChildren(...cards);
    }
    if(report.numeric_state==='MULTIPLE_NUMERIC_CANDIDATES'){
      candidates.append(p8Notice('Одно и то же значение встречается у разных CAD-поверхностей. Автоматический выбор запрещён: инженер должен проверить, какая из них соответствует реальной выноске.'));
    }
  }catch(error){
    if(seq===p13Sequence&&key===p13Key&&serial===state.sessionSerial){
      errorIn(summary,error.message);
    }
  }
}
let p12Seq=0;
async function p12LoadCAD(){
  const serial=state.sessionSerial,seq=++p12Seq;
  const feature=$('p10-surface').value,query=p10Query;
  const camera=$('p12-camera').value;
  const imageBox=$('p12-cad-image'),proof=$('p12-cad-proof');
  imageBox.replaceChildren();proof.replaceChildren();
  if(!p10Evidence||!feature||!query){
    imageBox.replaceChildren(p8Notice('Выберите конкретную CAD-грань в блоке P10.'));
    return;
  }
  imageBox.replaceChildren(el('div','loading','Преобразую оригинальный STEP в треугольники и строю изображение…'));
  const img=el('img','p12-cad-render');
  img.alt='Трёхмерная модель из исходного STEP: выбранные аналитические CAD-грани оранжевые, остальные серо-голубые. Вид '+camera;
  img.loading='eager';img.decoding='async';
  img.onload=()=>{
    if(seq===p12Seq&&serial===state.sessionSerial&&
       query===p10Query&&feature===$('p10-surface').value&&camera===$('p12-camera').value){
      imageBox.replaceChildren(img);
    }
  };
  img.onerror=()=>{
    if(seq===p12Seq&&serial===state.sessionSerial){
      imageBox.replaceChildren(p8Notice('Не удалось отобразить STEP. Проверьте доступ к оригиналу и статус CAD-обработчика.'));
    }
  };
  img.src='/api/engineering/p12/render?q='+encodeURIComponent(query)+
    '&feature='+encodeURIComponent(feature)+'&view='+encodeURIComponent(camera);
  try{
    const evidence=await request('/api/engineering/p12/feature?q='+encodeURIComponent(query)+
      '&feature='+encodeURIComponent(feature));
    if(seq!==p12Seq||serial!==state.sessionSerial||query!==p10Query||
       feature!==$('p10-surface').value||camera!==$('p12-camera').value)return;
    const selected=evidence.feature;
    const grid=el('div','engineering-details p12-proof-list');
    grid.append(p10Value('Выделенные грани BREP',selected.face_occurrences),
      p10Value('Пример индексов граней',selected.sample_face_indices.join(', ')),
      p10Value('SHA-256 модели',evidence.cad_sha256));
    proof.replaceChildren(grid,p8Notice('Кадр получен из настоящей STEP-геометрии. Соответствие стрелки чертежа конкретной поверхности НЕ ДОКАЗАНО.'));
  }catch(error){
    if(seq===p12Seq&&serial===state.sessionSerial)errorIn(proof,error.message);
  }
}
async function loadP12Audit(){
  const serial=state.sessionSerial,target=$('p12-witness-status');
  target.replaceChildren(el('div','loading','Проверяю локальный журнал и наличие независимого подписанного якоря…'));
  try{
    const report=await request('/api/engineering/p12/audit');
    if(serial!==state.sessionSerial||state.page!=='engineering')return;
    const signed=report.state==='SIGNED_PREFIX_VERIFIED';
    const status=el('div','engineering-details p12-witness-grid');
    const w=report.witness;
    status.append(p10Value('Внешняя подпись',signed?'Подтверждена по настроенному публичному ключу':'НЕ ПОДКЛЮЧЕНА'),
      p10Value('Событий в журнале',w.latest_seq),
      p10Value('Подписанных событий',w.externally_witnessed_events),
      p10Value('Событий после контрольной подписи',w.unwitnessed_events));
    const expl=el('p','source-note',signed?
      'Достоверность подписи проверена, но полномочия подписанта в организации не установлены. Последующие неподписанные события требуют нового якоря.':
      'Без внешнего подписанного якоря невозможно обнаружить все случаи удаления последних записей человеком с доступом к базе.');
    target.replaceChildren(status,expl);
  }catch(error){
    if(serial===state.sessionSerial)errorIn(target,error.message);
  }
}
async function loadMetrics(){
  const out=$('metric-results');out.replaceChildren(el('div','loading','Рассчитываю план и факт…'));
  try{
    const data=await request('/api/metrics');
    out.replaceChildren();
    for(const row of data.items){
      const name=el('h2','metric-order','Заказ '+row.order);
      const cards=el('div','metric-grid');
      const values=[
        ['Плановая себестоимость',row.planned],
        ['Фактические затраты',row.actual],
        ['Отклонение от плана',row.variance]
      ];
      for(const [label,value] of values){
        const c=el('div','metric-card');c.append(el('span','metric-label',label),el('strong','metric-number',money(value)));
        cards.append(c);
      }
      const explanation=el('div','metric-explainer');
      explanation.append(el('h3','','Отклонение от плана'),
        el('p','','Фактические затраты по учебному заказу на '+money(row.variance)+
          ' выше плановых ('+(row.variance/row.planned*100).toFixed(1).replace('.',',')+
          '%). Причины отклонения в примере не определяются автоматически.'));
      const bars=el('div','metric-bars');
      for(const [label,value] of [['План',row.planned],['Факт',row.actual]]){
        const pair=el('div','bar-pair');pair.append(el('div','bar-label',label+' · '+money(value)));
        const track=el('div','bar-track'),fill=el('div','bar-fill');
        fill.style.width=Math.max(1,Math.round(value/Math.max(row.planned,row.actual)*100))+'%';
        track.append(fill);pair.append(track);bars.append(pair);
      }
      explanation.append(bars);out.append(name,cards,explanation);
    }
  }catch(e){errorIn(out,e.message)}
}
async function loadAiStatus(){
  if(!state.me)return;
  const label=$('ai-status'),note=$('ai-model-note');
  try{
    const info=await request('/api/ai/status');
    state.aiStatus=info.status;
    const labels={ready:'Готова к работе',starting:'Запускается',stopped:'Выключена',
      error:'Ошибка',conflict:'Конфликт порта',missing:'Не установлена'};
    label.textContent=labels[info.status]||info.status;
    label.dataset.status=info.status;
    note.textContent=info.message;
    // This preview shares the primary local model; never expose controls that
    // would start/stop the same inference process from two applications.
    const canOperateModel=['engineer','director'].includes(state.me.id);
    $('ai-start').disabled=state.aiBusy||!canOperateModel||info.status==='ready'||info.status==='starting'||info.status==='conflict'||info.status==='missing';
    $('ai-stop').disabled=state.aiBusy||!canOperateModel||!(info.status==='ready'||info.status==='starting');
    if(!canOperateModel)note.textContent+=' Запуском и остановкой управляет инженер или руководитель.';
  }catch(e){
    label.textContent='Сервер недоступен';note.textContent=e.message;
  }
}
async function aiStartStop(action){
  const btn=$(action==='start'?'ai-start':'ai-stop');
  btn.disabled=true;$('ai-model-note').textContent=action==='start'?'Запускаю обработку вопросов…':'Останавливаю обработку…';
  try{
    const response=await request('/api/ai/'+action,
      {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.me.csrf},body:'{}'});
    $('ai-model-note').textContent=response.message;
  }catch(e){$('ai-model-note').textContent='Не получилось: '+e.message}
  finally{await loadAiStatus()}
}
function encodeFile(file){
  return file.arrayBuffer().then(buffer=>{
    const bytes=new Uint8Array(buffer);
    const chunks=[];
    for(let i=0;i<bytes.length;i+=8192)
      chunks.push(String.fromCharCode(...bytes.subarray(i,i+8192)));
    return btoa(chunks.join(''));
  });
}
let demoRunBusy=false;
function canUploadFiles(){
  return Boolean(state.uploadCaps?.enabled&&Array.isArray(state.uploadCaps.writable_scopes)&&state.uploadCaps.writable_scopes.length>0);
}
function syncUploadButtons(){
  const allowed=canUploadFiles();
  $('ai-upload').disabled=!allowed||state.uploadBusy||demoRunBusy;
  $('ai-try-sample').disabled=!allowed||state.uploadBusy||demoRunBusy;
  $('ai-demo-run').disabled=!allowed||state.uploadBusy||state.aiBusy||demoRunBusy;
}
async function loadUploadCapabilities(){
  if(!state.me)return;
  if(isDemoMode()){
    const personalScope='personal-'+state.me.id;
    state.uploadCaps={enabled:true,writable_scopes:[personalScope],max_bytes:5*1024*1024};
    const scope=$('ai-scope');
    const opt=document.createElement('option');
    opt.value=personalScope;
    opt.textContent='Личное пространство ('+state.me.role+')';
    scope.replaceChildren(opt);
    scope.disabled=true;
    $('ai-file').disabled=false;
    syncUploadButtons();
    return;
  }
  let caps={enabled:false,writable_scopes:[],max_bytes:8*1024*1024};
  try{caps=await request('/api/upload-capabilities')}catch{}
  state.uploadCaps=caps;
  const allowed=canUploadFiles();
  const scope=$('ai-scope'),previous=scope.value;
  scope.replaceChildren(...(allowed?caps.writable_scopes:[]).map(x=>{
    const o=document.createElement('option');o.value=x;o.textContent=x;return o;
  }));
  if(allowed&&caps.writable_scopes.includes(previous))scope.value=previous;
  scope.disabled=!allowed;$('ai-file').disabled=!allowed;
  syncUploadButtons();
  if(!allowed)$('ai-upload-result').textContent='Загрузка выключена или для вашей роли нет разрешённого раздела.';
}
async function loadUploadJobStatus(){
  if(!state.me||!state.uploadDocId)return;
  const docId=state.uploadDocId,session=state.sessionSerial;
  try{
    const d=await request('/api/uploads/'+encodeURIComponent(docId)+'/status');
    // An earlier poll must never update the status of a newer upload,
    // or disclose the previous session's result after logout/login.
    if(!state.me||state.sessionSerial!==session||state.uploadDocId!==docId)return;
    const note=$('ai-upload-result');
    if(d.index_status==='indexed'){
      note.append(el('p','ai-hint','Документ проиндексирован и доступен для поиска по вашим правам.'));
      state.uploadDocId=null;
    } else if(d.index_status==='failed'){
      note.append(el('p','ai-warning-line','Индексация отложена; очередь повторит задачу. Оригинал уже сохранён.'));
      state.uploadDocId=null;
    }
  }catch(err){
    // Terminal auth/not-found errors stop polling; transient failures retry.
    if(state.sessionSerial===session&&state.uploadDocId===docId&&
       (err?.status===401||err?.status===403||err?.status===404)){
      state.uploadDocId=null;
    }
  }
}
async function aiUpload(fileOverride=null){
  const file=fileOverride||$('ai-file').files[0],
    out=$('ai-upload-result'),session=state.sessionSerial;
  if(!state.uploadCaps?.enabled){out.textContent='Нет разрешённого раздела для загрузки.';return null}
  const maxMb=Math.round((state.uploadCaps.max_bytes||5*1024*1024)/(1024*1024));
  if(!file||!/\.(pdf|docx|txt)$/i.test(file.name)||file.size<1||
     file.size>state.uploadCaps.max_bytes){out.textContent='Нужен PDF, DOCX или TXT не больше '+maxMb+' МБ.';return null}
  if(isDemoMode()){
    if(state.uploadBusy)return null;
    state.uploadBusy=true;syncUploadButtons();
    out.textContent='Извлечение текста и сохранение документа в локальной учебной базе…';
    try{
      const encoded=await encodeFile(file);
      if(session!==state.sessionSerial||!state.me)return null;
      const result=await request('/api/upload',{
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRF-Token':state.me.csrf||''},
        body:JSON.stringify({filename:file.name,base64:encoded})
      });
      if(session!==state.sessionSerial||!state.me)return null;
      out.replaceChildren(el('strong','',result.duplicate
        ?'Этот файл уже загружен (дубликат не создан).'
        :'Оригинал и текст сохранены.'));
      if(!result.duplicate){
        const summary='Страниц: '+result.pages+' · фрагментов: '+result.chunks;
        out.append(el('p','ai-hint',summary));
      }
      if(result.warning)out.append(el('p','ai-warning-line',result.warning));
      const original=el('a','secondary-button ai-source-open','Скачать оригинал');
      original.href='/api/source-file?id='+encodeURIComponent(result.id);
      original.download=file.name;out.append(original);
      const open=el('button','secondary-button ai-source-open','Открыть документ');
      open.type='button';open.addEventListener('click',()=>openDocument(result.id));out.append(open);
      $('ai-file').value='';
      return result;
    }catch(e){
      if(session===state.sessionSerial)out.textContent='Ошибка загрузки: '+e.message;
      return null;
    }finally{
      state.uploadBusy=false;
      syncUploadButtons();
    }
  }
  const scope=$('ai-scope').value;
  if(!state.uploadCaps.writable_scopes.includes(scope)){out.textContent='Нет права записи в выбранный раздел.';return null}
  if(state.uploadBusy)return null;
  const title=$('ai-title').value.trim()||file.name;
  const fingerprint=[file.name,file.size,file.lastModified,scope,title].join('|');
  const key=state.uploadKey?.fingerprint===fingerprint?state.uploadKey.key:crypto.randomUUID().replace(/-/g,'');
  state.uploadKey={key,fingerprint};
  state.uploadBusy=true;syncUploadButtons();
  out.textContent='Извлечение текста и сохранение оригинала в защищённом PostgreSQL…';
  try{
    const media=file.name.toLowerCase().endsWith('.pdf')?'application/pdf':
      file.name.toLowerCase().endsWith('.docx')?
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'text/plain';
    const result=await request('/api/uploads',{
      method:'POST',
      headers:{
        'Content-Type':media,'X-Filename':encodeURIComponent(file.name),
        'X-Title':encodeURIComponent(title),'X-Scope-Id':scope,
        'Idempotency-Key':key,'X-CSRF-Token':state.me.csrf||''
      },
      body:file
    });
    if(session!==state.sessionSerial||!state.me)return null;
    state.uploadKey=null;state.uploadDocId=result.document_id;
    out.replaceChildren(el('strong','',result.reused?'Повторная отправка — без дубля.':'Оригинал и текст сохранены.'));
    out.append(el('p','ai-hint','SHA-256: '+result.sha256+' · индекс: '+result.index_status));
    const original=el('a','secondary-button ai-source-open','Скачать оригинал');
    original.href='/unified/api/uploads/'+encodeURIComponent(result.document_id)+'/original';
    original.download=file.name;out.append(original);
    const open=el('button','secondary-button ai-source-open','Открыть документ');
    open.type='button';open.addEventListener('click',()=>openDocument(result.document_id));out.append(open);
    $('ai-file').value='';
    loadUploadJobStatus();
    return result;
  }catch(e){
    if(session===state.sessionSerial)out.textContent='Ошибка загрузки: '+e.message+
      '. Повторная попытка сохранит тот же ключ запроса.';
    return null;
  }finally{
    state.uploadBusy=false;
    syncUploadButtons();
  }
}

function openScanPreview(docId,page,title){
  const modal=$('scan-preview-modal');
  $('scan-preview-title').textContent=title+' · стр. '+page;
  $('scan-preview-error').hidden=true;
  $('scan-preview-image').hidden=false;
  const previewBase=String(docId).startsWith('upload-')?'/unified/api/page-preview':'/api/page-preview';
  $('scan-preview-image').src=previewBase+'?id='+encodeURIComponent(docId)+'&page='+encodeURIComponent(page);
  if(!modal.open)modal.showModal();
}
$('scan-preview-image').addEventListener('error',()=>{
  $('scan-preview-image').hidden=true;
  $('scan-preview-error').textContent='Изображение страницы не открылось. Откройте исходный PDF из карточки документа.';
  $('scan-preview-error').hidden=false;
});
$('scan-preview-close').addEventListener('click',()=>$('scan-preview-modal').close());
$('scan-preview-modal').addEventListener('close',()=>{
  $('scan-preview-image').removeAttribute('src');
});
async function aiAsk(){
  const session=state.sessionSerial;
  const q=$('ai-question').value.trim();
  const output=$('ai-answer'),src=$('ai-sources');
  if(!q){output.hidden=false;output.textContent='Введите вопрос.';return}
  if(state.aiBusy)return;
  state.aiBusy=true;$('ai-ask').disabled=true;$('ai-demo-run').disabled=true;
  output.hidden=false;src.hidden=true;
  output.replaceChildren(el('div','ai-thinking','Ищу подходящие фрагменты. Затем модель подготовит ответ…'));
  src.replaceChildren();
  try{
    const result=await request('/api/ask',{method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':state.me.csrf},
      body:JSON.stringify({question:q})});
    if(!state.me||state.sessionSerial!==session)return;
    output.replaceChildren(el('div','ai-answer-label',(result.mode==='local_llm'||result.mode==='local_llm_advisory')?'Ответ по найденным документам':'Результат поиска'),
      el('p','ai-answer-text',result.answer));
    if(result.filtered_lines)output.append(el('p','ai-warning-line',
      'Из текста документов пропущены '+result.filtered_lines+' строк, похожих на команды.'));
    if(result.citation_warning){
      output.append(el('p','ai-warning-line',
        result.mode==='retrieval_only'
          ?'Показаны найденные исходные фрагменты без нейросетевой генерации. Сверьте данные с оригиналом.'
          :'Модель не указала, какие документы использовала. Сверьте ответ с найденными файлами.'));
    }
    if(result.verification_required || result.sources?.some(s=>s.extraction_method==='ocr')){
      const hasHomoglyph=result.sources?.some(s=>s.identifier_match==='homoglyph_candidate');
      const hasOcr=result.sources?.some(s=>s.extraction_method==='ocr');
      if(hasHomoglyph)output.append(el('p','ai-warning-line',
        'Обозначение совпало только после замены похожих букв кириллицы и латиницы. Сверьте номер с оригиналом.'));
      if(hasOcr)output.append(el('p','ai-warning-line',
        'Часть ответа основана на OCR. Проверьте цифры и обозначения на оригинальной странице.'));
      if(!hasHomoglyph && !hasOcr && result.mode!=='retrieval_only')output.append(el('p','ai-warning-line',
        'ИИ дал справочный ответ на основе найденных документов. Проверьте его по источникам: это не разрешение на изготовление или эксплуатацию.'));
    }
    if(result.sources?.length){
      src.hidden=false;src.replaceChildren(el('h3','',
        result.mode==='retrieval_only'?'Найденные документы и страницы':
        result.citation_warning?'Найденные документы — ссылка на источник не подтверждена':
        'Документы и страницы'));
      for(const source of result.sources){
        const card=el('article',source.cited?'ai-source ai-source-cited':'ai-source');
        const heading=el('div','ai-source-head');
        heading.append(el('strong','', '['+source.number+'] '+source.title+
          (source.cited?' · указан в ответе':' · дополнительный документ')),
          el('span','', 'Страница '+source.page+(source.extraction_method==='ocr'?' · OCR':'')+
            (source.identifier_match==='homoglyph_candidate'?' · сходство символов':'')));
        card.append(heading,el('p','',source.excerpt));
        const button=el('button','ai-source-open','Открыть документ');button.append(uiIcon('arrow-up-right'));
        button.type='button';button.addEventListener('click',()=>openDocument(source.doc_id));
        card.append(button);
        if(source.extraction_method==='ocr' && (source.doc_id.startsWith('UPL-') || source.doc_id.startsWith('upload-'))){
          const scan=el('button','ai-source-open scan-open','Открыть страницу PDF');scan.append(uiIcon('arrow-up-right'));
          scan.type='button';
          scan.addEventListener('click',()=>openScanPreview(source.doc_id,source.page,source.title));
          card.append(scan);
        }
        src.append(card);
      }
    }
  }catch(e){
    if(state.me&&state.sessionSerial===session)
      output.replaceChildren(el('div','message-error','Не удалось получить ответ: '+e.message));
  }finally{
    state.aiBusy=false;$('ai-ask').disabled=false;syncUploadButtons();
    if(state.page==='assistant')loadAiStatus();
  }
}

document.querySelectorAll('[data-demo-role]').forEach(b=>b.addEventListener('click',()=>demoLogin(b.dataset.demoRole,b)));
document.querySelectorAll('.nav-item').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.page)));
document.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',async()=>{
  if(b.dataset.action==='search-pump'){state.query='насос';await navigate('documents')}
  if(b.dataset.action==='search-heat'){state.query='перегрев';await navigate('documents')}
  if(b.dataset.action==='part')await navigate('part');
  if(b.dataset.action==='assistant')await navigate('assistant');
  if(b.dataset.action==='metrics')await navigate('metrics');
}));
document.querySelectorAll('[data-query]').forEach(b=>b.addEventListener('click',()=>loadDocuments(b.dataset.query)));
document.querySelectorAll('[data-ai-query]').forEach(b=>b.addEventListener('click',()=>{
  $('ai-question').value=b.dataset.aiQuery;$('ai-question').focus();
}));
$('ai-start').addEventListener('click',()=>aiStartStop('start'));
$('ai-stop').addEventListener('click',()=>aiStartStop('stop'));
// Private engineering kit: no fixture registry or database write, only
// source-bound results from newly selected PDF, STEP and CSV originals.
let privateKitReport=null,privateKitPackage=null,privateKitSaveKey=null,savedKits=[];
function privateKitMarkdown(report){
  const clean=v=>String(v??'Не установлено').replace(/[\r\n]/g,' ').slice(0,260);
  const lines=[
    '# Предварительное инженерное заключение: '+clean(report.part),
    '',
    '**ТРЕБУЕТ ПРОВЕРКИ ИНЖЕНЕРОМ. НЕ ЯВЛЯЕТСЯ РАЗРЕШЕНИЕМ НА ИЗГОТОВЛЕНИЕ.**',
    '',
    '## Спецификация',
    'Деталь: '+clean(report.bom.part),
    'Строка BOM: '+clean(report.bom.row),
    'Позиция: '+clean(report.bom.item_no),
    'Материал: '+clean(report.bom.material),
    'Количество: '+clean(report.bom.quantity)+' '+clean(report.bom.unit),
    '',
    '## Исходные файлы и контрольные суммы',
  ];
  for(const file of report.sources)
    lines.push('- '+file.role+': '+clean(file.name)+'; SHA-256: '+file.sha256);
  lines.push('','## STEP: геометрия');
  lines.push('Габариты в мм: '+(report.model.bbox_mm||[]).join(' × '));
  lines.push('Тел: '+clean(report.model.solid_count)+', граней: '+clean(report.model.face_count)+
    ', аналитических признаков: '+clean(report.model.feature_count));
  lines.push('','## Обозначения размеров на PDF');
  for(const entry of report.dimension_checks){
    const c=entry.numeric_candidates;
    const matches=c.length ? c.map(x=>x.value+' мм (разница '+x.difference_mm+' мм)').join('; ') : 'нет обоснованного численного сопоставления';
    lines.push('- '+clean(entry.label)+' (страница '+clean(entry.page)+'): '+matches+
      '; геометрическая привязка к конкретной грани НЕ ПОДТВЕРЖДЕНА');
  }
  lines.push('','## Ограничения и предупреждения');
  for(const warning of report.warnings)lines.push('- '+clean(warning));
  lines.push('','Анализ выполняется временно. При отдельном сохранении редакции три исходника сохраняются в защищённой PostgreSQL; без сохранения не добавляются в хранилище.');
  return lines.join('\n');
}
function renderPrivateKit(report){
  const root=$('private-kit-result');
  const content=el('div','private-kit-output');
  content.append(
    el('h3','kit-report-part',report.part+' · результаты анализа нового комплекта'),
    el('p','kit-report-status','Предварительная проверка · производство не разрешено')
  );
  const meta=el('p','source-note','BOM, строка '+report.bom.row+' · материал: '+
    (report.bom.material||'не указан')+' · STEP: '+report.model.face_count+
    ' граней · на PDF: '+report.drawing.dimension_count+' обозначений');
  content.append(meta);
  const files=el('div','kit-report-sources');
  for(const file of report.sources){
    const item=el('div','kit-report-file');
    item.append(
      el('strong','',{bom:'Спецификация CSV',drawing:'Чертёж PDF',model:'Модель STEP'}[file.role]),
      el('span','',file.name),
      el('small','','SHA-256: '+file.sha256)
    );
    files.append(item);
  }
  content.append(files);
  if(report.model.bbox_mm)
    content.append(el('p','kit-report-check','Габариты модели: '+
      report.model.bbox_mm.join(' × ')+' мм. Связь размеров чертежа с осями модели не установлена.'));
  else
    content.append(el('p','kit-report-check','Единицы измерения STEP не подтверждены; сравнение размеров ограничено.'));
  content.append(el('h4','kit-report-section-title','Размеры и возможные CAD-совпадения'));
  if(!report.dimension_checks.length)
    content.append(el('p','source-note','Текстовых обозначений не найдено. Для сканированного чертежа требуется OCR.'));
  for(const entry of report.dimension_checks){
    const hits=entry.numeric_candidates.length;
    content.append(el('p','kit-report-check',
      entry.label+' · стр. '+entry.page+' · '+
      (hits?('Численных совпадений: '+hits+
        '; значения: '+entry.numeric_candidates.map(x=>x.value).join(', ')):
        'Подтверждённых численных совпадений нет')+
      '. Грань, допуск и применимость НЕ подтверждены.'));
  }
  if(report.drawing.more_dimensions)
    content.append(el('p','source-note','Показаны первые 120 обозначений. Остальные требуют отдельной проверки.'));
  content.append(el('p','kit-report-warning',
    'Связь трёх файлов указана пользователем. Настоящая редакция, проектная геометрическая привязка и разрешение на производство не подтверждены. Исходники не сохранялись.'));
  root.replaceChildren(content);
}
// Choose a unique per-part CAD target from the authoritative server listing.
// The backend forbids duplicate (scope, part); defaulting to "new" for a
// previously stored part would otherwise produce an unnecessary HTTP 409.
function normalizedKitPart(part){
  return String(part??'').trim().toLocaleLowerCase('ru');
}
function savedKitTargetForPart(part,kits,current=''){
  const items=Array.isArray(kits)?kits:[];
  const normalized=normalizedKitPart(part);
  if(!normalized)return items.some(k=>k.kit_id===current)?current:'';
  const selected=items.find(k=>k.kit_id===current);
  if(selected&&normalizedKitPart(selected.part)===normalized)return current;
  return items.find(k=>normalizedKitPart(k.part)===normalized)?.kit_id||'';
}
function syncPrivateKitTarget(){
  const select=$('private-kit-version-target'),hint=$('private-kit-target-hint');
  const stepStem=($('private-kit-step')?.files?.[0]?.name||'').replace(/\.(?:step|stp)$/i,'').trim();
  const part=privateKitReport?.part||$('private-kit-part')?.value.trim()||stepStem;
  const chosen=savedKitTargetForPart(part,savedKits,select.value);
  select.value=chosen;
  const kit=savedKits.find(x=>x.kit_id===chosen);
  if(!select.disabled&&(normalizedKitPart(part)||kit)){
    hint.textContent=kit
      ? 'Найден комплект «'+kit.part+'»: при сохранении будет создана редакция № '+(kit.latest_revision+1)+'.'
      : 'Совпадающий комплект не найден: при сохранении будет создан новый комплект.';
  }else if(select.disabled){
    hint.textContent='Для сохранения требуется право записи в инженерный раздел.';
  }else{
    hint.textContent='После анализа файлов система найдёт существующий комплект или предложит создать новый.';
  }
}
function privateKitReset(){
  privateKitReport=null;privateKitPackage=null;privateKitSaveKey=null;
  $('private-kit-export').disabled=true;
  $('private-kit-save').disabled=true;
  if($('private-kit-version-target')&&$('private-kit-target-hint'))syncPrivateKitTarget();
}
async function privateKitAnalyze(event){
  event.preventDefault();
  const form=$('private-kit-form');
  if(!form.reportValidity())return;
  const bom=$('private-kit-bom').files[0],
    drawing=$('private-kit-pdf').files[0],model=$('private-kit-step').files[0];
  const out=$('private-kit-result'),submit=$('private-kit-analyze');
  const session=state.sessionSerial;
  const selections=[
    {role:'bom',file:bom,ext:'.csv',max:2000000},
    {role:'drawing',file:drawing,ext:'.pdf',max:8000000},
    {role:'model',file:model,ext:'.step',max:8000000}
  ];
  privateKitReset();
  for(const {role,file,ext,max} of selections){
    if(!file || !(file.name.toLowerCase().endsWith(ext) || (role==='model' && file.name.toLowerCase().endsWith('.stp'))) ||
       file.size<1||file.size>max || /[/\\\r\n]/.test(file.name)){
      out.textContent='Некорректный '+role+': проверьте расширение и размер файла.';
      return;
    }
  }
  const part=$('private-kit-part').value.trim();
  if(part && !/^[\p{L}\p{N}_. ()-]{1,150}$/u.test(part)){
    out.textContent='Название детали содержит неподдерживаемые символы.';
    return;
  }
  const header={
    part,
    files:selections.map(({role,file})=>({role,name:file.name,size:file.size}))
  };
  const json=new TextEncoder().encode(JSON.stringify(header));
  if(json.length>4096){
    out.textContent='Слишком длинные имена файлов.';return;
  }
  const length=new Uint8Array(4);
  new DataView(length.buffer).setUint32(0,json.length,false);
  const payload=new Blob([
    new TextEncoder().encode('CZKIT1\n'),length,json,
    ...selections.map(x=>x.file)
  ],{type:'application/octet-stream'});
  submit.disabled=true;
  out.replaceChildren(el('div','loading','Анализирую предоставленные документы и реальную геометрию STEP…'));
  try{
    const result=await request('/api/engineering/bundle-check',{
      method:'POST',
      headers:{
        'Content-Type':'application/octet-stream',
        ...(state.me?.csrf?{'X-CSRF-Token':state.me.csrf}:{})
      },
      body:payload
    });
    if(session!==state.sessionSerial||state.page!=='engineering')return;
    if(result.schema!=='factory_private_cad_bundle_v1'||
       result.manufacturing_authorization!==false ||
       result.source_persistence!=='NONE')throw Error('Сервер вернул некорректный результат');
    privateKitReport=result;privateKitPackage=payload;
    syncPrivateKitTarget();
    renderPrivateKit(result);
    $('private-kit-export').disabled=false;
    $('private-kit-save').disabled=$('private-kit-version-target').disabled;
  }catch(error){
    if(session===state.sessionSerial)
      out.textContent='Не удалось проверить комплект: '+error.message+
        '. Убедитесь, что деталь присутствует в CSV и файлы не повреждены.';
  }finally{submit.disabled=false;}
}
function privateKitExport(){
  if(!privateKitReport)return;
  const blob=new Blob([privateKitMarkdown(privateKitReport)],{type:'text/markdown;charset=utf-8'});
  const href=URL.createObjectURL(blob);
  const anchor=document.createElement('a');
  anchor.href=href;
  anchor.download='cad-check-'+privateKitReport.part.replace(/[^a-zA-Z0-9_-]/g,'_')+'.md';
  document.body.append(anchor);anchor.click();anchor.remove();
  setTimeout(()=>URL.revokeObjectURL(href),600);
}
$('private-kit-form').addEventListener('submit',privateKitAnalyze);
$('private-kit-export').addEventListener('click',privateKitExport);

async function privateKitSave(){
 const button=$('private-kit-save'),note=$('private-kit-save-state');
 if(!privateKitReport||!privateKitPackage||!state.me||!state.uploadCaps?.writable_scopes?.includes('engineering'))return;
 const target=$('private-kit-version-target').value;
 const kit=savedKits.find(x=>x.kit_id===target);
 if(target&&!kit){note.textContent='Целевой комплект недоступен.';return}
 if(kit&&normalizedKitPart(kit.part)!==normalizedKitPart(privateKitReport.part)){
  note.textContent='Выбран другой идентификатор детали. Выберите правильный комплект.';return
 }
 const fingerprint=privateKitReport.sources.map(x=>x.sha256+':'+x.name).join('|')+
   '|'+target+'|'+(kit?.latest_revision||0);
 if(!privateKitSaveKey||privateKitSaveKey.fingerprint!==fingerprint)
   privateKitSaveKey={fingerprint,key:crypto.randomUUID().replace(/-/g,'')};
 button.disabled=true;
 note.textContent='Сохраняю три оригинала и инженерные свидетельства в PostgreSQL…';
 try{
  const headers={'Content-Type':'application/octet-stream',
   'Idempotency-Key':privateKitSaveKey.key,
   'X-Expected-Revision':String(kit?.latest_revision||0),
   'X-CSRF-Token':state.me.csrf||''};
  if(kit)headers['X-Kit-Id']=kit.kit_id;
  const result=await request('/api/engineering/kits',{method:'POST',
   headers,body:privateKitPackage});
  note.textContent='Сохранено: '+result.part+' · редакция '+result.revision_no+
   (result.reused?' (без дубля)':'')+
   '. Исходники сохранены, изготовление не разрешено.';
  privateKitSaveKey=null;
  await loadSavedKits(result.kit_id);
 }catch(e){note.textContent='Ошибка сохранения: '+e.message+'. Повторная попытка не создаст дубль.'}
 finally{button.disabled=false}
}
$('private-kit-save').addEventListener('click',privateKitSave);
$('private-kit-version-target').addEventListener('change',()=>{privateKitSaveKey=null;syncPrivateKitTarget()});


function savedKitOriginalLink(kit,revision,source){
 const a=el('a','secondary-button ai-source-open',source.name||source.role);
 a.href='/unified/api/engineering/kits/'+encodeURIComponent(kit)+'/revisions/'+revision+
   '/source/'+encodeURIComponent(source.role);
 a.download=source.name;a.rel='noopener';return a;
}
async function loadSavedKitRevisions(kit){
 const out=$('saved-kits-list');
 try{
  const data=await request('/api/engineering/kits/'+encodeURIComponent(kit.kit_id)+'/revisions');
  const wrapper=el('div','kit-report-content');
  wrapper.append(el('h4','',data.part+' · '+data.revisions.length+' редакций'));
  for(const version of data.revisions){
   const row=el('div','kit-report-file');
   row.append(el('strong','','Редакция '+version.revision_no+' · Требует проверки инженером'));
   const files=el('div','ai-actions');
   for(const src of version.report.sources||[])files.append(savedKitOriginalLink(kit.kit_id,version.revision_no,src));
   row.append(files);wrapper.append(row);
  }
  out.replaceChildren(wrapper);
  const back=el('button','secondary-button','Все комплекты');
  back.addEventListener('click',loadSavedKits);out.append(back);
 }catch(e){out.textContent='Редакции недоступны: '+e.message}
}
async function loadSavedKits(preferredKitId=null){
 const out=$('saved-kits-list'),target=$('private-kit-version-target');
 if(!state.me)return;
 try{
  const data=await request('/api/engineering/kits');
  savedKits=data.items||[];
  const selected=preferredKitId||target.value;
  target.replaceChildren();
  const empty=document.createElement('option');empty.value='';empty.textContent='Новый комплект';target.append(empty);
  for(const kit of savedKits){
   const o=document.createElement('option');o.value=kit.kit_id;
   o.textContent=kit.part+' · редакция '+kit.latest_revision;target.append(o);
  }
  target.value=savedKits.some(k=>k.kit_id===selected)?selected:'';
  target.disabled=!state.uploadCaps?.writable_scopes?.includes('engineering');
  syncPrivateKitTarget();
  out.replaceChildren();
  if(!savedKits.length){out.append(el('p','source-note','Сохранённых комплектов пока нет.'));return}
  for(const kit of savedKits){
   const row=el('div','kit-report-file');
   row.append(el('strong','',kit.part+' · редакция '+kit.latest_revision));
   const open=el('button','secondary-button','Редакции и оригиналы');
   open.type='button';open.addEventListener('click',()=>loadSavedKitRevisions(kit));
   row.append(open);out.append(row);
  }
 }catch(e){target.disabled=true;out.textContent='Список комплектов недоступен: '+e.message}
}

for(const id of ['private-kit-bom','private-kit-pdf','private-kit-step','private-kit-part'])
  $(id).addEventListener('change',privateKitReset);
$('private-kit-part').addEventListener('input',privateKitReset);

// A complete, source-bound engineering kit report for the known public CAD
// corpus. Never present numerical CAD candidates as geometric certification.
let activeKitReport=null;
function kitReportMarkdown(report){
  const clean=value=>String(value??'Не установлено').replace(/[\r\n]+/g,' ').slice(0,300);
  const lines=[
    '# Инженерное заключение — '+clean(report.part),
    '',
    '**Статус: ТРЕБУЕТ ПРОВЕРКИ ИНЖЕНЕРОМ. Изготовление не разрешено.**',
    '',
    'Комплект: '+clean(report.group)+' / '+clean(report.part_key),
    'Позиция BOM: '+clean(report.item_no),
    'Материал: '+clean(report.material),
    'Редакция: '+clean(report.revision_status),
    '',
    '## Исходные файлы',
  ];
  for(const source of report.sources){
    lines.push('- '+source.role+': '+clean(source.name)+' (SHA-256: '+source.sha256+')');
  }
  lines.push('','## Размеры и CAD-кандидаты');
  if(!report.dimension_checks.length)lines.push('Размеры для автоматической сверки не обнаружены.');
  for(const d of report.dimension_checks){
    const result=d.numeric_candidates==null?'требуется выбор оси и ручная проверка':
      String(d.numeric_candidates)+' численно подходящих поверхностей; принадлежность грани НЕ установлена';
    lines.push('- '+clean(d.annotation)+' · '+clean(d.nominal)+': '+result);
  }
  lines.push('','## Нерешённые риски');
  for(const item of report.risks)lines.push('- '+clean(item));
  lines.push('','## Ограничения','- Вывод сформирован по открытому учебному комплекту, не по произвольной загрузке.');
  lines.push('- Геометрическая привязка PDF к STEP и разрешённая редакция не подтверждены.');
  lines.push('- Численное совпадение НЕ означает соответствия конкретной 3D-грани.');
  lines.push('- Не является утверждением конструкторской документации или разрешением на изготовление.','');
  return lines.join('\n');
}
function renderKitReport(report){
  const out=$('kit-report-content');
  const body=el('div','kit-report-body');
  const heading=el('h3','kit-report-part',report.part+' · проверка комплекта');
  const status=el('p','kit-report-status','Требуется проверка инженером · изготовление не разрешено');
  const summary=el('p','source-note','Материал: '+(report.material||'не указан')+
    ' · позиция BOM: '+(report.item_no||'не указана')+
    ' · размеров: '+report.drawing_annotations+' · поверхностей CAD: '+report.cad_features);
  body.append(heading,status,summary);
  const files=el('div','kit-report-sources');
  for(const source of report.sources){
    const item=el('div','kit-report-file');
    const role=el('strong','',source.role==='BOM'?'Спецификация':source.role==='DRAWING'?'Чертёж PDF':'Модель STEP');
    const a=el('a','',source.name);
    a.href=source.download_path;a.rel='noopener';
    const sha=el('small','', 'SHA-256: '+source.sha256.slice(0,20)+'…');
    item.append(role,a,sha);files.append(item);
  }
  body.append(files);
  const title=el('h4','kit-report-section-title','Размеры чертежа и числовые соответствия STEP');
  body.append(title);
  if(!report.dimension_checks.length)body.append(el('p','source-note','Нет размеров, пригодных для сопоставления.'));
  for(const dim of report.dimension_checks){
    const count=dim.numeric_candidates;
    const detail=count==null?'Требуется выбор оси и ручная проверка':
      count===0?'Численного совпадения не найдено':
      'Найдено численных кандидатов: '+count;
    body.append(el('p','kit-report-check',dim.annotation+' · '+detail+
       '. Принадлежность конкретной 3D-грани не подтверждена.'));
  }
  const warning=el('p','kit-report-warning',
    'Это предварительное заключение по открытому учебному корпусу. Редакция, допуски и применимость к производству не подтверждены.');
  body.append(warning);out.replaceChildren(body);
}
async function generateKitReport(){
  const button=$('kit-report-generate');
  const out=$('kit-report-content');
  const session=state.sessionSerial;
  const q=$('engineering-query').value.trim();
  activeKitReport=null;
  $('kit-report-save').disabled=true;
  if(!/^[A-Za-z0-9_. -]{3,120}$/.test(q)){
    out.textContent='Введите точное обозначение детали из инженерного каталога, например focuser_drawtube.';
    return;
  }
  button.disabled=true;
  out.replaceChildren(el('div','loading','Сверяю связанные BOM, PDF и STEP и формирую заключение…'));
  try{
    const report=await request('/api/engineering/kit-report?q='+encodeURIComponent(q));
    if(session!==state.sessionSerial || state.page!=='engineering' || q!==$('engineering-query').value.trim())return;
    if(report.schema!=='factory_engineering_kit_report_v1'||report.manufacturing_authorization!==false){
      throw Error('Некорректный статус инженерного заключения');
    }
    activeKitReport=report;renderKitReport(report);
    $('kit-report-save').disabled=false;
  }catch(error){
    if(session===state.sessionSerial)out.textContent='Заключение не сформировано: '+error.message+
      '. Для неподтверждённых или отсутствующих комплектов результат не выдаётся.';
  }finally{button.disabled=false;}
}
function saveKitReport(){
  const report=activeKitReport;
  if(!report)return;
  const text=kitReportMarkdown(report);
  const blob=new Blob([text],{type:'text/markdown;charset=utf-8'});
  const href=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=href;
  a.download='engineering-report-'+report.part.replace(/[^a-zA-Z0-9_-]/g,'_')+'.md';
  document.body.append(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(href),600);
}
$('kit-report-generate').addEventListener('click',generateKitReport);
$('kit-report-save').addEventListener('click',saveKitReport);
$('engineering-query').addEventListener('input',()=>{
  activeKitReport=null;
  $('kit-report-save').disabled=true;
  $('kit-report-content').textContent='Выберите деталь и сформируйте заключение по её исходным файлам.';
});

$('ai-upload').addEventListener('click',()=>aiUpload());
const DEMO_QUESTION='Какое испытательное давление указано для насоса Н-125 в учебном паспорте?';
async function trySampleDemo(autoAsk=false){
  if(demoRunBusy || !state.me || state.aiBusy || !canUploadFiles())return;
  demoRunBusy=true;
  const session=state.sessionSerial;
  const status=$('ai-upload-result'),hero=$('ai-demo-run');
  syncUploadButtons();
  hero.classList.add('is-running');
  const resetHero=()=>{
    hero.replaceChildren(uiIcon('play'),el('span','','Запустить учебный пример'));
    hero.classList.remove('is-running');
  };
  try{
    hero.textContent='1 из 3 · Загружаю PDF…';
    const response=await fetch('/sample-scan.pdf',{credentials:'same-origin',cache:'no-store'});
    if(!response.ok)throw Error('Не удалось открыть пример.');
    const blob=await response.blob();
    if(!state.me || session!==state.sessionSerial)return;
    const file=new File([blob],'Учебный_паспорт_насоса_скан.pdf',{type:'application/pdf'});
    hero.textContent='2 из 3 · Читаю страницы…';
    const uploaded=await aiUpload(file);
    if(!state.me || session!==state.sessionSerial)return;
    if(!uploaded)throw Error('PDF не загружен. Проверьте сообщение в разделе загрузки.');
    $('ai-question').value=DEMO_QUESTION;
    if(autoAsk){
      hero.textContent='3 из 3 · Получаю ответ…';
      await aiAsk();
      if(state.me&&session===state.sessionSerial){
        const answer=$('ai-answer');
        if(!answer.hidden)answer.scrollIntoView({behavior:'smooth',block:'center'});
      }
    }else{
      status.append(el('p','ai-hint','Вопрос подставлен. Нажмите «Получить ответ».'));
      $('ai-question').focus();
    }
  }catch(error){
    if(state.me&&session===state.sessionSerial)
      status.textContent='Не получилось запустить пример: '+error.message;
  }finally{
    demoRunBusy=false;resetHero();
    syncUploadButtons();
  }
}
$('ai-try-sample').addEventListener('click',()=>trySampleDemo(false));
$('ai-demo-run').addEventListener('click',()=>trySampleDemo(true));

$('ai-question').addEventListener('keydown',event=>{
  // A textarea normally inserts a newline on Enter. Chat-style behavior:
  // Enter submits, Shift+Enter inserts a newline, IME composition stays untouched.
  if(event.key!=='Enter' || event.shiftKey || event.isComposing || event.keyCode===229)return;
  event.preventDefault();
  // Holding Enter must not queue multiple submissions.
  if(event.repeat || state.aiBusy)return;
  $('ai-question-form').requestSubmit();
});
$('ai-question-form').addEventListener('submit',event=>{event.preventDefault();aiAsk()});
$('search-form').addEventListener('submit',event=>{event.preventDefault();loadDocuments($('search-input').value.trim())});
$('part-form').addEventListener('submit',event=>{event.preventDefault();loadPart()});
$('engineering-form').addEventListener('submit',event=>{event.preventDefault();loadEngineeringQuery()});
$('p9-comparison-form').addEventListener('submit',event=>{event.preventDefault();runP9Compare()});
$('p10-compare-form').addEventListener('submit',event=>{event.preventDefault();runP10Compare()});
$('p11-review-form').addEventListener('submit',event=>{event.preventDefault();p11SaveFinding()});
$('p10-surface').addEventListener('change',p11PreviewSelection);
$('p10-reference-axis').addEventListener('change',p11PreviewSelection);
$('p12-camera').addEventListener('change',p12LoadCAD);
$('p10-callout').addEventListener('change',p10SelectSurface);
$('p10-surface').addEventListener('change',()=>{$('p10-feature-result').replaceChildren()});
$('p10-reference-axis').addEventListener('change',()=>{$('p10-feature-result').replaceChildren()});
$('p9-annotation').addEventListener('change',()=>{$('p9-comparison-result').replaceChildren()});
$('p9-axis').addEventListener('change',()=>{$('p9-comparison-result').replaceChildren()});
$('p9-window').addEventListener('change',()=>{$('p9-comparison-result').replaceChildren()});
$('p8-source-check').addEventListener('click',()=>p8InspectSelected(false));
$('p8-source-ocr').addEventListener('click',()=>p8InspectSelected(true));
$('p8-compare-button').addEventListener('click',p8CompareTopFrames);
$('p8-notice-button').addEventListener('click',p8InspectHistoricalNotice);
$('p8-source-select').addEventListener('change',()=>{
  $('p8-source-result').replaceChildren();
  $('p8-source-ocr').hidden=true;
});
document.querySelectorAll('[data-engineering-example]').forEach(button=>{
  button.addEventListener('click',()=>{
    $('engineering-query').value=button.dataset.engineeringExample;
    loadEngineeringQuery();
  });
});
$('switch-demo').addEventListener('click',logout);
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();
  $('login-error').textContent='';
  if(!isDemoMode()){
    window.location.assign('/auth/login');
    return;
  }
  try{
    await request('/api/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:$('username').value.trim(),password:$('password').value})
    });
    $('password').value='';
    await refresh();
  }catch(e){$('login-error').textContent=e.message}
});
$('modal-delete').addEventListener('click',async()=>{
  const id=$('modal-delete').dataset.docId,session=state.sessionSerial;
  if(!id || !state.me)return;
  if(!confirm('Удалить файл и извлечённый из него текст из базы выбранной роли?'))return;
  $('modal-delete').disabled=true;
  try{
    if(isDemoMode()){
      await request('/api/delete-upload',{method:'POST',
        headers:{'Content-Type':'application/json','X-CSRF-Token':state.me.csrf},
        body:JSON.stringify({id})});
    }else{
      await request('/api/uploads/'+encodeURIComponent(id),{method:'DELETE',
        headers:{'X-CSRF-Token':state.me.csrf}});
    }
    if(!state.me||state.sessionSerial!==session)return;
    if(state.uploadDocId===id)state.uploadDocId=null;
    $('document-modal').close();
    $('ai-answer').replaceChildren();$('ai-answer').hidden=true;
    $('ai-sources').replaceChildren();$('ai-sources').hidden=true;
    $('ai-upload-result').textContent='Файл и его текст удалены.';
    if(state.page==='documents')loadDocuments(state.query);
  }catch(e){
    if(state.me&&state.sessionSerial===session){
      const reason=(e?.status===403||e?.status===404)
        ?'Удаление разрешено только автору загрузки документа (либо файл уже удалён).'
        :e.message;
      alert('Не удалось удалить файл: '+reason);
    }
  }
  finally{$('modal-delete').disabled=false}
});
$('close-modal').addEventListener('click',()=>$('document-modal').close());
$('close-modal-bottom').addEventListener('click',()=>$('document-modal').close());
const unifiedLoginButton=$('unified-login');unifiedLoginButton.addEventListener('click',()=>window.location.assign('/auth/login'));unifiedLoginButton.dataset.ready='1';

detectBackendMode().finally(refresh);
