"use strict";
const element = (id) => document.getElementById(id);
let activeMe = null;
function say(message, error=false) {
  const container=element("results");
  container.textContent=message;
  container.classList.toggle("error",error);
}
async function requestJson(url, options={}) {
  const response=await fetch(url,{credentials:"same-origin",cache:"no-store",...options});
  const body=await response.json();
  if(!response.ok) throw new Error(body.detail||"Ошибка доступа");
  return body;
}
function renderCards(items, kind) {
  const root=element("results");root.replaceChildren();root.classList.remove("error");
  if(!items.length){root.textContent="В вашем контуре подходящие документы не найдены.";return;}
  for(const item of items){
    const card=document.createElement("article");
    const title=document.createElement("strong");
    title.textContent=kind==="search"?(item.document_id||"Документ"):(item.title||item.id);
    const secondary=document.createElement("small");
    secondary.textContent=kind==="search"?(item.excerpt||""):(item.designation||"Без обозначения")+" · "+(item.type||"Документ")+" · "+(item.revision||"Без ревизии");
    card.append(title,secondary);root.append(card);
  }
}
async function refreshMe() {
  try{
    activeMe=await requestJson("/api/me");
    element("workspace").hidden=false;
    element("login-button").hidden=true;
    element("logout-button").hidden=false;
    element("welcome").textContent="Доступ подтверждён: "+activeMe.actor;
    element("doc-count").textContent=String(activeMe.documents);
    element("source-count").textContent=String(activeMe.engineering_sources);
    element("system-status").textContent="OIDC · доступ подтверждён";
  }catch(e){
    activeMe=null;element("workspace").hidden=true;
    element("login-button").hidden=false;element("logout-button").hidden=true;
    element("system-status").textContent="Вход необходим";
  }
}
async function listDocuments(){
  try{say("Чтение…");const response=await requestJson("/api/documents?limit=50");renderCards(response.documents,"documents");}
  catch(e){say(e.message,true);}
}
async function search(){
  const term=element("query").value.trim();
  if(!term){say("Введите запрос для поиска.");return;}
  try{say("Поиск…");const response=await requestJson("/api/search?q="+encodeURIComponent(term)+"&limit=20");renderCards(response.results,"search");}
  catch(e){say(e.message,true);}
}
element("documents-button").addEventListener("click",listDocuments);
element("search-button").addEventListener("click",search);
element("query").addEventListener("keydown",(event)=>{if(event.key==="Enter")search();});
element("logout-button").addEventListener("click",async()=>{
  if(!activeMe)return;
  try{
    const result=await requestJson("/auth/logout",{method:"POST",headers:{"X-CSRF-Token":activeMe.csrf}});
    activeMe=null;
    const target=new URL(result.identity_logout_url);
    const expected="http://127.0.0.1:18080/realms/factory-core-v06/protocol/openid-connect/logout";
    if(target.origin+target.pathname!==expected)throw Error("Недопустимый адрес выхода SSO");
    window.location.assign(target.href);
  }catch(e){say("Выход не выполнен: "+e.message,true);}
});
refreshMe();
