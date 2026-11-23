"""Keycloak-backed FastAPI Policy Gateway for Factory Knowledge v0.6 P2.

The issuer, audience, permitted OAuth clients and local origins are hard-coded
for an isolated acceptance environment. No trusting X-User / JSON actor_id.
Every request: verify RS256 JWT -> Keycloak introspection -> PostgreSQL
issuer+sub binding -> SET LOCAL actor in one read-only scoped transaction.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import base64,hashlib,hmac,html,json,os,re,secrets,threading,time
from pathlib import Path
from urllib.parse import urlencode,urlparse

import httpx
from fastapi import FastAPI,HTTPException,Query,Request,Response
from fastapi.responses import FileResponse,JSONResponse,RedirectResponse
from p2_gateway.security import (
    ISSUER,WEB_CLIENT,TOKEN_ENDPOINT,END_SESSION_ENDPOINT,verify_access_token,verify_id_token,Principal
)
from core06.db_client import database_session

APP_ORIGIN=os.environ.get("FACTORY_P2_APP_ORIGIN","http://localhost:19136")
if APP_ORIGIN not in ("http://localhost:19136","http://localhost:19137","http://localhost:19139"):
    raise RuntimeError("FACTORY_P2_ORIGIN_MUST_BE_LOCAL_TEST_ORIGIN")
CALLBACK_URL=APP_ORIGIN+"/auth/callback"
KC_AUTH_URL=ISSUER+"/protocol/openid-connect/auth"
COOKIE_SESSION="factory_unified_session" if APP_ORIGIN.endswith(":19139") else "factory_p2_session"
COOKIE_FLOW="factory_unified_flow" if APP_ORIGIN.endswith(":19139") else "factory_p2_flow"
ROOT=Path(__file__).resolve().parent
WEB=ROOT/"web"
app=FastAPI(title="Factory Core v0.6: Authenticated Policy Gateway",
            docs_url=None,redoc_url=None,openapi_url=None)

@dataclass
class WebSession:
    access_token:str
    exp:int
    csrf:str
    id_token:str

@dataclass
class OidcFlow:
    state:str
    verifier:str
    nonce:str
    created_at:float

_SESSIONS:dict[str,WebSession]={}
_FLOWS:dict[str,OidcFlow]={}
_LOCK=threading.RLock()

def _error(code,detail):
    raise HTTPException(status_code=code,detail=detail)

def _browser_host(request):
    # Explicitly fix the public UI hostname so session cookies never reach
    # the separate Keycloak origin 127.0.0.1:18080.
    return request.headers.get("host","").lower()==urlparse(APP_ORIGIN).netloc

def _cleanup(now=None):
    now=now or time.time()
    with _LOCK:
        for handle,entry in list(_FLOWS.items()):
            if now-entry.created_at>300:_FLOWS.pop(handle,None)
        for handle,entry in list(_SESSIONS.items()):
            if entry.exp<=now:_SESSIONS.pop(handle,None)

def _auth(request:Request):
    auth=request.headers.get("authorization","")
    cookie=request.cookies.get(COOKIE_SESSION)
    if auth and cookie:_error(401,"AMBIGUOUS_AUTHENTICATION")
    if auth:
        m=re.fullmatch(r"Bearer ([A-Za-z0-9_.-]{100,8192})",auth)
        if not m:_error(401,"AUTHORIZATION_FORMAT_INVALID")
        token=m.group(1)
        return verify_access_token(token),None
    if cookie:
        if not _browser_host(request):_error(401,"BROWSER_HOST_INVALID")
        with _LOCK:entry=_SESSIONS.get(cookie)
        if entry is None:_error(401,"SESSION_EXPIRED")
        if entry.exp<=time.time():
            with _LOCK:_SESSIONS.pop(cookie,None)
            _error(401,"SESSION_EXPIRED")
        return verify_access_token(entry.access_token),entry
    _error(401,"AUTHENTICATION_REQUIRED")

@contextmanager
def _actor_db(principal:Principal):
    try:
        with database_session() as db:
            with db.transaction():
                # Trust the verified issuer/sub; no request-supplied actor.
                row=db.execute("SELECT core.resolve_oidc_subject(%s,%s)",
                               (principal.issuer,principal.external_sub)).fetchone()
                actor=row[0] if row else None
                if not actor:_error(403,"IDENTITY_NOT_MAPPED")
                db.execute("SELECT set_config('factory.actor_id',%s,true)",(actor,))
                yield db,actor
    except HTTPException:raise
    except Exception:
        # Deny on DB, connection, RLS or authoritative ledger failure.
        _error(503,"AUTHORIZATION_DATABASE_UNAVAILABLE")

@app.middleware("http")
async def secure_headers(request:Request,call_next):
    try:
        response=await call_next(request)
    except Exception:
        raise
    response.headers["Cache-Control"]="no-store, max-age=0"
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["X-Frame-Options"]="DENY"
    response.headers["Referrer-Policy"]="no-referrer"
    response.headers["Permissions-Policy"]="camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Resource-Policy"]="same-origin"
    response.headers["Content-Security-Policy"]=(
        "default-src 'none'; script-src 'self'; style-src 'self';"
        " connect-src 'self'; img-src 'self'; form-action 'self';"
        " base-uri 'none'; frame-ancestors 'none'")
    return response

@app.get("/health")
def health():
    try:
        with database_session() as db:
            rows=db.execute("SELECT 1").fetchone()
        status=rows==(1,)
    except Exception:status=False
    return JSONResponse(status_code=200 if status else 503,
         content={"status":"ok" if status else "degraded","version":"core-v06-p2",
                  "policy_db":status,"authentication":"keycloak-rs256-introspection"})

def _is_unified_frontend():
    return (APP_ORIGIN.endswith(":19139") or (
        APP_ORIGIN=="http://localhost:19137" and
        os.environ.get("FACTORY_V06_UNIFIED_STAGING")=="1"
    ))


@app.get("/")
def home(request:Request):
    if not _browser_host(request):
        _error(400,"USE_CANONICAL_LOCALHOST_ORIGIN")
    if _is_unified_frontend():
        return FileResponse(Path(__file__).resolve().parent.parent/"index.html",media_type="text/html")
    return FileResponse(WEB/"index.html",media_type="text/html")

@app.get("/app.js")
def client_js(request:Request):
    if not _browser_host(request):_error(400,"USE_CANONICAL_LOCALHOST_ORIGIN")
    if _is_unified_frontend():
        return FileResponse(Path(__file__).resolve().parent.parent/"app.js",media_type="application/javascript")
    return FileResponse(WEB/"app.js",media_type="application/javascript")

@app.get("/style.css")
def client_style(request:Request):
    if not _browser_host(request):_error(400,"USE_CANONICAL_LOCALHOST_ORIGIN")
    if _is_unified_frontend():
        return FileResponse(Path(__file__).resolve().parent.parent/"style.css",media_type="text/css")
    return FileResponse(WEB/"style.css",media_type="text/css")

@app.get("/auth/login")
def browser_login(request:Request):
    if not _browser_host(request):
        _error(400,"USE_CANONICAL_LOCALHOST_ORIGIN")
    _cleanup()
    state=secrets.token_urlsafe(28)
    verifier=secrets.token_urlsafe(64)
    nonce=secrets.token_urlsafe(28)
    digest=hashlib.sha256(verifier.encode("ascii")).digest()
    challenge=base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    handle=secrets.token_urlsafe(36)
    with _LOCK:
        if len(_FLOWS)>100:_error(503,"TOO_MANY_PENDING_LOGINS")
        _FLOWS[handle]=OidcFlow(state,verifier,nonce,time.time())
    params={
       "client_id":WEB_CLIENT,"response_type":"code","scope":"openid",
       "redirect_uri":CALLBACK_URL,"state":state,"nonce":nonce,
       "code_challenge":challenge,"code_challenge_method":"S256"}
    response=RedirectResponse(url=KC_AUTH_URL+"?"+urlencode(params),
                              status_code=302)
    response.set_cookie(COOKIE_FLOW,handle,max_age=300,httponly=True,
                        secure=False,samesite="lax",path="/auth")
    return response

@app.get("/auth/callback")
def browser_callback(request:Request,code:str=Query(min_length=10,max_length=2200),
                     state:str=Query(min_length=10,max_length=150)):
    if not _browser_host(request):_error(400,"USE_CANONICAL_LOCALHOST_ORIGIN")
    handle=request.cookies.get(COOKIE_FLOW)
    if not handle:_error(401,"MISSING_OIDC_FLOW_COOKIE")
    with _LOCK:flow=_FLOWS.pop(handle,None)
    if not flow or time.time()-flow.created_at>300:
        _error(401,"OIDC_FLOW_EXPIRED")
    if not hmac.compare_digest(state,flow.state):
        _error(401,"OIDC_STATE_MISMATCH")
    try:
        with httpx.Client(timeout=9,trust_env=False) as client:
            r=client.post(TOKEN_ENDPOINT,data={
                "grant_type":"authorization_code",
                "client_id":WEB_CLIENT,
                "redirect_uri":CALLBACK_URL,
                "code":code,
                "code_verifier":flow.verifier})
        if r.status_code!=200:_error(401,"OIDC_AUTHORIZATION_CODE_INVALID")
        result=r.json()
        id_claims=verify_id_token(result["id_token"],flow.nonce)
        principal=verify_access_token(result["access_token"])
        if id_claims["sub"]!=principal.external_sub or principal.client_id!=WEB_CLIENT:
            _error(401,"OIDC_TOKEN_PAIR_MISMATCH")
        with _actor_db(principal):pass
    except HTTPException:raise
    except Exception:_error(503,"OIDC_CALLBACK_UNAVAILABLE")
    handle=secrets.token_urlsafe(40)
    with _LOCK:
        _cleanup()
        if len(_SESSIONS)>120:_error(503,"SESSION_LIMIT_EXCEEDED")
        _SESSIONS[handle]=WebSession(result["access_token"],principal.exp,
                                     secrets.token_urlsafe(26),result["id_token"])
    response=RedirectResponse(url=APP_ORIGIN+"/",status_code=303)
    response.delete_cookie(COOKIE_FLOW,path="/auth")
    response.set_cookie(COOKIE_SESSION,handle,
                        httponly=True,secure=False,samesite="strict",path="/",
                        max_age=min(600,max(0,int(principal.exp-time.time()))))
    return response

@app.post("/auth/logout")
def browser_logout(request:Request):
    if not _browser_host(request):_error(403,"INVALID_ORIGIN")
    if request.headers.get("origin")!=APP_ORIGIN:
        _error(403,"CSRF_ORIGIN_FAILED")
    handle=request.cookies.get(COOKIE_SESSION)
    if not handle:_error(401,"SESSION_REQUIRED")
    with _LOCK:entry=_SESSIONS.get(handle)
    if not entry:_error(401,"SESSION_EXPIRED")
    given=request.headers.get("x-csrf-token","")
    if not hmac.compare_digest(entry.csrf,given):
        _error(403,"CSRF_TOKEN_FAILED")
    with _LOCK:_SESSIONS.pop(handle,None)
    # Revoke the local session before exposing the IdP signout destination.
    # Otherwise a user can silently reauthenticate using the residual SSO cookie.
    rp_logout=END_SESSION_ENDPOINT+"?"+urlencode({
        "client_id":WEB_CLIENT,
        "id_token_hint":entry.id_token,
        "post_logout_redirect_uri":APP_ORIGIN+"/"
    })
    response=JSONResponse({"status":"local_session_revoked",
                           "identity_logout_url":rp_logout})
    response.delete_cookie(COOKIE_SESSION,path="/")
    return response

@app.get("/api/me")
def me(request:Request):
    principal,web_session=_auth(request)
    with _actor_db(principal) as (db,actor):
        docs=db.execute("SELECT count(*) FROM core.document").fetchone()[0]
        sources=db.execute("SELECT count(*) FROM core.engineering_source").fetchone()[0]
    return {"actor":actor,"identity_provider":"keycloak","subject_hash":hashlib.sha256(principal.external_sub.encode()).hexdigest()[:12],
            "documents":docs,"engineering_sources":sources,
            "csrf":web_session.csrf if web_session else None}

@app.get("/api/documents")
def documents(request:Request,limit:int=Query(default=30,ge=1,le=50)):
    principal,_=_auth(request)
    with _actor_db(principal) as (db,actor):
        rows=db.execute("""SELECT id,title,designation,doc_type,revision
                           FROM core.document ORDER BY id LIMIT %s""",
                        (limit,)).fetchall()
    return {"count":len(rows),"documents":[dict(zip(("id","title","designation","type","revision"),x)) for x in rows]}

@app.get("/api/documents/{doc_id}")
def document(request:Request,doc_id:str):
    if len(doc_id)>128 or len(doc_id)<1:_error(404,"DOCUMENT_NOT_FOUND")
    principal,_=_auth(request)
    with _actor_db(principal) as (db,actor):
        row=db.execute("""SELECT id,title,designation,doc_type,revision
                           FROM core.document WHERE id=%s""",(doc_id,)).fetchone()
        if row is None:_error(404,"DOCUMENT_NOT_FOUND")
        chunks=db.execute("""SELECT id,body,page_no,extraction_method
                             FROM core.document_chunk WHERE document_id=%s
                             ORDER BY id LIMIT 100""",(doc_id,)).fetchall()
    return {"document":dict(zip(("id","title","designation","type","revision"),row)),
            "chunks":[dict(zip(("id","text","page","extraction_method"),c)) for c in chunks]}

@app.get("/api/search")
def search(request:Request,q:str=Query(min_length=1,max_length=250),
           limit:int=Query(default=10,ge=1,le=30)):
    if not q.strip():_error(422,"QUERY_EMPTY")
    principal,_=_auth(request)
    with _actor_db(principal) as (db,actor):
        rows=db.execute("""SELECT c.id,c.document_id,left(c.body,420),
                              ts_rank_cd(c.search_vector,plainto_tsquery('simple',%s))
                         FROM core.document_chunk c
                         WHERE c.search_vector @@ plainto_tsquery('simple',%s)
                         ORDER BY 4 DESC,1 LIMIT %s""",(q,q,limit)).fetchall()
    return {"count":len(rows),"results":[dict(zip(("chunk_id","document_id","excerpt","score"),x)) for x in rows]}

@app.get("/api/finance")
def finance(request:Request,limit:int=Query(default=20,ge=1,le=50)):
    principal,_=_auth(request)
    with _actor_db(principal) as (db,actor):
        rows=db.execute("""SELECT id,title,planned,actual FROM core.finance
                           ORDER BY id LIMIT %s""",(limit,)).fetchall()
    return {"count":len(rows),
            "rows":[dict(zip(("id","title","planned","actual"),[str(x) if i>=2 else x for i,x in enumerate(row)]))
                    for row in rows]}

@app.get("/api/engineering/sources")
def engineering_sources(request:Request,limit:int=Query(default=20,ge=1,le=50)):
    principal,_=_auth(request)
    with _actor_db(principal) as (db,actor):
        rows=db.execute("""SELECT id,name,kind,source_group,original_sha256
                           FROM core.engineering_source ORDER BY name,id LIMIT %s""",
                        (limit,)).fetchall()
    return {"count":len(rows),
            "sources":[dict(zip(("id","name","kind","group","source_sha256"),x)) for x in rows]}

# P3: new isolated retrieval API; not enabled on the canonical P2 service
# until real Qdrant + local model + PostgreSQL acceptance tests pass.
_HYBRID_RETRIEVER=None
_HYBRID_INIT_LOCK=threading.Lock()

def _p3_retriever():
    global _HYBRID_RETRIEVER
    with _HYBRID_INIT_LOCK:
        if _HYBRID_RETRIEVER is None:
            from core06.hybrid_retrieval import COLLECTION, LocalE5, PolicyHybridRetriever, qdrant_client

            client = qdrant_client()
            try:
                client.get_collection(COLLECTION)
                _HYBRID_RETRIEVER = PolicyHybridRetriever(LocalE5(), client)
            except Exception:
                client.close()
                raise
        return _HYBRID_RETRIEVER

@app.get("/api/hybrid-search")
def hybrid_search(request:Request,q:str=Query(min_length=1,max_length=250),
                  limit:int=Query(default=10,ge=1,le=30)):
    if not q.strip():_error(422,"QUERY_EMPTY")
    principal,_=_auth(request)
    with _actor_db(principal) as (_db,actor):
        pass  # Mapping is rechecked on final hydrate in PostgreSQL.
    try:
        found=_p3_retriever().search(actor,q,limit,
              oidc_issuer=principal.issuer,oidc_sub=principal.external_sub)
    except Exception:
        _error(503,"RETRIEVAL_UNAVAILABLE")
    return {"count":len(found),"mode":"qdrant_dense_sparse_postgres_lexical",
            "results":[{"chunk_id":h.chunk_id,"document_id":h.document_id,
                        "title":h.title,"text":h.text,"page":h.page_no,
                        "revision":h.revision,"score":h.score,
                        "extraction_method":h.extraction_method}
                       for h in found]}
