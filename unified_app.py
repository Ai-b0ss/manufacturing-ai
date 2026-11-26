"""Unified P13 UI + P2 Keycloak + P3 Qdrant/PostgreSQL pilot.

This is a compatibility boundary, not a replacement for the P13 CAD modules.
The original P13 UI is served unchanged in structure with SSO login instead
of insecure demo-role login. No original P13 SQLite credentials or sessions are
accepted for document/finance/search APIs.

Engineering P7-P13 *read-only synthetic demos* can be accessed through a
strict allowlist to the preexisting local P13 service, only after PostgreSQL
RLS confirms the user can see the entire synthetic engineering collection.
The read-only legacy adapter never serves enterprise files. An independent
one-shot private CAD endpoint runs on fresh user-selected files in short-lived
local sandboxes without indexing or persisting originals.
"""
from __future__ import annotations
import hashlib,json,logging,re,time,os
from decimal import Decimal
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlencode
import httpx
from fastapi import HTTPException,Request,Query
from fastapi.responses import FileResponse,JSONResponse,Response
from p2_gateway.app import (
    app,_auth,_actor_db,_browser_host,_error,_p3_retriever,
    APP_ORIGIN
)

ROOT=Path(__file__).resolve().parent
LEGACY="http://127.0.0.1:19134"
PUBLIC_DEMO_CORPUS_COUNT=40
ROLE_LABELS={"engineer":"Инженер-конструктор","technologist":"Технолог",
             "finance":"Финансист","director":"Руководитель"}
ALLOWED_ENG={
 "summary","ask","notice","file",
 "p8/summary","p8/catalog","p8/part","p8/source","p8/ocr-hint",
 "p8/compare-top","p8/notice",
 "p9/catalog","p9/annotations","p9/compare",
 "p10/features","p10/compare","p10/revisions",
 "p11/preview","p11/findings",
 "p12/render","p12/feature","p12/audit",
 "p13/vectors","p13/associations"}
BINARY_ENDPOINTS={"p11/preview","p12/render","p13/vectors","file"}
SAFE_PARAMS={"id","q","group","annotation","feature","axis","view","window"}

def auth_actor(request):
    principal,session=_auth(request)
    with _actor_db(principal) as (db,actor):
        return principal,session,actor

def require_unified_host(request):
    primary=APP_ORIGIN=="http://localhost:19139"
    isolated=(APP_ORIGIN=="http://localhost:19137" and
              os.environ.get("FACTORY_V06_UNIFIED_STAGING")=="1")
    if not (primary or isolated) or not _browser_host(request):
        _error(400,"UNIFIED_HOST_REQUIRED")

def authorization_context(principal,required_scope=None,source_id=None):
    # Rechecks issuer/sub mapping and RLS in a fresh PostgreSQL transaction.
    with _actor_db(principal) as (db,actor):
        if required_scope=="engineering":
            count=db.execute("""SELECT count(*) FROM core.engineering_source
                                WHERE scope_id='engineering'""").fetchone()[0]
            if count!=PUBLIC_DEMO_CORPUS_COUNT:
                _error(403,"ENGINEERING_DEMO_CORPUS_NOT_FULLY_AUTHORIZED")
        if source_id is not None:
            doc=db.execute("""SELECT id FROM core.engineering_source
                             WHERE id=%s AND scope_id='engineering'""",
                           (source_id,)).fetchone()
            if doc is None:_error(404,"SOURCE_NOT_FOUND")
        # Gateway cannot directly read core.policy_state by design. Compare
        # the effective RLS-visible source fingerprint on a fresh transaction.
        rows=db.execute("""SELECT id,original_sha256 FROM core.engineering_source
                          WHERE scope_id='engineering' ORDER BY id""").fetchall()
        marker=hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
        return actor,marker

@app.get("/design-preview.css")
def unified_design_style(request:Request):
    require_unified_host(request)
    return FileResponse(ROOT/"design-preview.css",media_type="text/css")

@app.get("/assets/brand-mark.svg")
def unified_brand(request:Request):
    require_unified_host(request)
    return FileResponse(ROOT/"assets"/"brand-mark.svg",media_type="image/svg+xml")

@app.get("/assets/icons.svg")
def unified_icons(request:Request):
    require_unified_host(request)
    return FileResponse(ROOT/"assets"/"icons.svg",media_type="image/svg+xml")

@app.get("/assets/font-face.css")
def unified_font_css(request:Request):
    require_unified_host(request)
    return FileResponse(ROOT/"assets"/"font-face.css",media_type="text/css")

@app.get("/assets/fonts/{filename}")
def unified_font(request:Request,filename:str):
    require_unified_host(request)
    if not re.fullmatch(r"[a-z0-9-]{1,100}\.woff2",filename):
        _error(404,"FONT_NOT_FOUND")
    file=ROOT/"assets"/"fonts"/filename
    if not file.is_file():_error(404,"FONT_NOT_FOUND")
    return FileResponse(file,media_type="font/woff2")

@app.get("/unified/health")
def unified_health():
    return {"status":"up","ui":"p13","auth":"keycloak-p2","documents":"postgresql",
            "search":"qdrant-postgresql","legacy_engineering":"read_only_synthetic",
            "production_ready":False}

@app.get("/unified/ready")
def unified_ready():
    from core06.readiness import check_dependencies

    checks = check_dependencies()
    ready = all(checks.values())
    return JSONResponse(
        {"ready": ready, "dependencies": checks},
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/unified/api/me")
def unified_me(request:Request):
    require_unified_host(request)
    principal,session=_auth(request)
    with _actor_db(principal) as (db,actor):
        rows=db.execute("SELECT DISTINCT scope_id FROM core.document").fetchall()
        scopes={r[0] for r in rows}
        engineering=db.execute("""SELECT count(*) FROM core.engineering_source
                                WHERE scope_id='engineering'""").fetchone()[0]
        finance=db.execute("SELECT count(*) FROM core.finance").fetchone()[0]
        if engineering==PUBLIC_DEMO_CORPUS_COUNT:scopes.add("engineering")
        if finance:scopes.add("finance")
        return {"id":actor,"role":ROLE_LABELS.get(actor,actor),
                "scopes":sorted(scopes),"csrf":session.csrf if session else None,
                "identity_provider":"keycloak",
                "fingerprint":hashlib.sha256((principal.issuer+principal.external_sub)
                                  .encode()).hexdigest()[:12]}

@app.get("/unified/api/documents")
def unified_documents(request:Request):
    require_unified_host(request)
    principal,_=_auth(request)
    with _actor_db(principal) as (db,_):
        rows=db.execute("""SELECT d.id,d.title,d.designation,d.doc_type,d.revision,
                           COALESCE((SELECT left(c.body,240) FROM core.document_chunk c
                            WHERE c.document_id=d.id ORDER BY c.id LIMIT 1),''),
                           EXISTS(SELECT 1 FROM core.document_chunk oc
                            WHERE oc.document_id=d.id AND oc.extraction_method='ocr')
                           FROM core.document d ORDER BY d.id LIMIT 100""").fetchall()
    return {"items":[{"id":r[0],"title":r[1],"designation":r[2],
                     "type":r[3],"revision":r[4],"snippet":r[5],
                     "has_ocr":bool(r[6])} for r in rows],"total":len(rows)}

@app.get("/unified/api/search")
def unified_search(request:Request,q:str=Query(min_length=1,max_length=250)):
    require_unified_host(request)
    principal,_=_auth(request)
    with _actor_db(principal) as (db,actor):
        pass
    try:
        results=_p3_retriever().search(actor,q,50,
                       oidc_issuer=principal.issuer,oidc_sub=principal.external_sub)
    except Exception as exc:
        logging.getLogger(__name__).warning("Unified search unavailable: %s", type(exc).__name__)
        _error(503,"SEARCH_BACKEND_UNAVAILABLE")
    docs={}
    for hit in results:
        is_ocr = getattr(hit, "extraction_method", "text") == "ocr"
        if hit.document_id not in docs:
            docs[hit.document_id]={"id":hit.document_id,"title":hit.title,
               "designation":getattr(hit,"designation",""),
               "type":getattr(hit,"doc_type","internal") or "internal",
               "revision":hit.revision,
               "snippet":hit.text[:240],"has_ocr":is_ocr}
        elif is_ocr:
            docs[hit.document_id]["has_ocr"]=True
    return {"items":list(docs.values()),"total":len(docs)}

@app.get("/unified/api/document")
def unified_document(request:Request,id:str=Query(min_length=1,max_length=128)):
    require_unified_host(request)
    principal,_=_auth(request)
    can_delete=False
    with _actor_db(principal) as (db,_):
        row=db.execute("""SELECT id,title,designation,doc_type,revision,scope_id,source_kind
                      FROM core.document WHERE id=%s""",(id,)).fetchone()
        if row is None:_error(404,"DOCUMENT_NOT_FOUND")
        chunks=db.execute("""SELECT body FROM core.document_chunk
                          WHERE document_id=%s ORDER BY id LIMIT 100""",(id,)).fetchall()
        if os.environ.get("FACTORY_V06_INGEST_ENABLED")=="1" and row[6]=="postgres_ingest":
            can_delete=bool(db.execute("SELECT core.can_write_scope(%s)",(row[5],)).fetchone()[0])
    return {"id":row[0],"title":row[1],"designation":row[2],
            "type":row[3],"revision":row[4],
            "text":"\n\n".join(x[0] for x in chunks),
            "can_delete":can_delete}

_PAGE_PREVIEW_SLOTS=BoundedSemaphore(2)

@app.get("/unified/api/page-preview")
def unified_page_preview(request:Request,id:str=Query(min_length=1,max_length=128),
                         page:int=Query(ge=1,le=80)):
    require_unified_host(request)
    principal,_=_auth(request)
    if os.environ.get("FACTORY_V06_INGEST_ENABLED")!="1":
        _error(503,"PROTECTED_UPLOAD_NOT_ENABLED")
    if not re.fullmatch(r"upload-[0-9a-f]{32}",id):
        _error(404,"DOCUMENT_NOT_FOUND")
    # Check RLS visibility and media_type BEFORE acquiring a rasterization slot
    # or pulling up to 8 MB of TOAST bytes into memory.
    with _actor_db(principal) as (db,_):
        meta=db.execute("""SELECT media_type FROM core.document_blob
                           WHERE document_id=%s""",(id,)).fetchone()
        if meta is None:_error(404,"DOCUMENT_NOT_FOUND")
    if meta[0]!="application/pdf":
        _error(415,"PAGE_PREVIEW_PDF_REQUIRED")
    if not _PAGE_PREVIEW_SLOTS.acquire(blocking=False):
        _error(429,"PAGE_PREVIEW_BUSY")
    try:
        with _actor_db(principal) as (db,_):
            row=db.execute("""SELECT media_type,original_bytes,content_sha256
                              FROM core.document_blob WHERE document_id=%s""",(id,)).fetchone()
            if row is None:_error(404,"DOCUMENT_NOT_FOUND")
        media_type,data,sha=row
        if media_type!="application/pdf":
            _error(415,"PAGE_PREVIEW_PDF_REQUIRED")
        raw=bytes(data)
        if hashlib.sha256(raw).hexdigest()!=sha:
            _error(503,"SOURCE_INTEGRITY_FAILED")
        from ai_extension import preview_pdf_page
        from core06.ingest_service import MAX_BYTES as UPLOAD_MAX_BYTES
        try:
            jpg=preview_pdf_page(raw,page,max_bytes=UPLOAD_MAX_BYTES)
        except ValueError:
            _error(422,"PAGE_PREVIEW_UNAVAILABLE")
        return Response(content=jpg,media_type="image/jpeg",headers={
            "Cache-Control":"private, no-store",
            "X-Content-Type-Options":"nosniff",
        })
    finally:
        _PAGE_PREVIEW_SLOTS.release()

@app.get("/unified/api/metrics")
def unified_metrics(request:Request):
    require_unified_host(request)
    principal,_=_auth(request)
    with _actor_db(principal) as (db,_):
        rows=db.execute("""SELECT id,title,planned,actual FROM core.finance
                        ORDER BY id LIMIT 50""").fetchall()
    return {"items":[{"order":r[1],"planned":float(r[2]),"actual":float(r[3]),
              "variance":float(r[3]-r[2])} for r in rows]}

@app.get("/unified/api/ai/status")
def unified_ai_status(request:Request):
    require_unified_host(request)
    auth_actor(request)
    from ai_extension import status as ai_status
    return ai_status()

def csrf_for_browser(request,session):
    if session is None or request.headers.get("origin")!=APP_ORIGIN:
        _error(403,"CSRF_ORIGIN_REQUIRED")
    from hmac import compare_digest
    if not compare_digest(request.headers.get("x-csrf-token",""),session.csrf):
        _error(403,"CSRF_TOKEN_INVALID")

@app.post("/unified/api/ask")
async def unified_ask(request:Request):
    require_unified_host(request)
    principal,session=_auth(request)
    csrf_for_browser(request,session)
    size_header=request.headers.get("content-length")
    if size_header is not None:
        try:
            declared_len=int(size_header)
        except ValueError:
            _error(400,"INVALID_CONTENT_LENGTH")
        if declared_len<0:
            _error(400,"INVALID_CONTENT_LENGTH")
        if declared_len>4096:
            _error(413,"QUESTION_TOO_LARGE")
    raw=bytearray()
    async for fragment in request.stream():
        if len(raw)+len(fragment)>4096:
            _error(413,"QUESTION_TOO_LARGE")
        raw.extend(fragment)
    try:
        payload=json.loads(raw.decode("utf-8"))
        if not isinstance(payload,dict):
            raise ValueError("expected JSON object")
        question=payload.get("question")
    except Exception:_error(422,"INVALID_QUESTION_JSON")
    if not isinstance(question,str) or not 1<=len(question.strip())<=250:
        _error(422,"QUESTION_INVALID")
    from core06.unified_ai import (
        MAX_EVIDENCE,evidence_from_hits,unchanged_and_allowed,
        search_only_response,generated_response)
    from fastapi.concurrency import run_in_threadpool
    import ai_extension
    with _actor_db(principal) as (db,actor):pass
    try:
        # P3 does a fresh OIDC/RLS hydration AFTER vector retrieval. Do not
        # send Qdrant payloads or legacy demo SQLite content to the model.
        hits=_p3_retriever().search(actor,question,limit=8,
                    oidc_issuer=principal.issuer,oidc_sub=principal.external_sub)
    except Exception:_error(503,"RETRIEVAL_UNAVAILABLE")
    if not hits:
        return search_only_response([],
            "В доступных документах нет подтверждённого ответа на этот вопрос.")
    selected=hits[:MAX_EVIDENCE]
    state=await run_in_threadpool(ai_extension.status)
    generated = None
    fallback_reason = None
    if state.get("status")!="ready":
        fallback_reason = (
            "Найдены разрешённые фрагменты документов. Локальная модель сейчас не запущена; "
            "для генерации ответа нажмите «Запустить ИИ».")
    else:
        try:
            generated=await run_in_threadpool(
                ai_extension.generate,question,evidence_from_hits(selected))
        except ai_extension.BusyError:
            _error(429,"LOCAL_MODEL_BUSY")
        except ValueError:
            fallback_reason = (
                "Локальная модель не смогла подготовить проверяемый ответ. "
                "Ниже показаны исходные фрагменты для самостоятельной проверки.")
        except Exception:
            _error(503,"LOCAL_MODEL_UNAVAILABLE")
    # Every response with excerpts needs fresh authoritative PostgreSQL RLS recheck
    # of the exact selected chunks after model work (without depending on Qdrant rank).
    from core06.hybrid_retrieval import Hit
    try:
        with _actor_db(principal) as (db,after_actor):
            if after_actor!=actor:_error(409,"ACCESS_CHANGED_DURING_GENERATION")
            chunk_ids=[h.chunk_id for h in selected]
            rows=db.execute("""SELECT c.id,c.document_id,d.title,c.body,c.page_no,
                                      d.revision,c.extraction_method,d.designation,d.doc_type
                               FROM core.document_chunk c
                               JOIN core.document d ON d.id=c.document_id
                               WHERE c.id=ANY(%s)""",(chunk_ids,)).fetchall()
    except HTTPException:
        raise
    except Exception:
        _error(503,"RECHECK_UNAVAILABLE")
    refreshed=[Hit(r[0],r[1],r[2],r[3],r[4],r[5],1.0,
                   "ocr" if r[6]=="ocr" else "text",
                   r[7] or "",r[8] or "internal") for r in rows]
    if not unchanged_and_allowed(selected,refreshed):
        _error(409,"SOURCES_CHANGED_DURING_GENERATION")
    if fallback_reason is not None:
        return search_only_response(selected, fallback_reason)
    answer=generated_response(selected,generated,question=question)
    if answer is None:
        return search_only_response(selected,
            "Модель не подтвердила ответ ссылками на источники. "
            "Показаны исходные фрагменты; утверждения модели скрыты.")
    return answer


@app.post("/unified/api/ai/start")
def unified_ai_start(request:Request):
    require_unified_host(request)
    principal,session=_auth(request)
    csrf_for_browser(request,session)
    with _actor_db(principal) as (db,actor):
        if actor not in ("engineer","director"):
            _error(403,"MODEL_OPERATOR_REQUIRED")
    from ai_extension import start as ai_start
    try:return ai_start()
    except ValueError:_error(409,"LOCAL_MODEL_START_FAILED")


@app.post("/unified/api/ai/stop")
def unified_ai_stop(request:Request):
    require_unified_host(request)
    principal,session=_auth(request)
    csrf_for_browser(request,session)
    with _actor_db(principal) as (db,actor):
        if actor not in ("engineer","director"):
            _error(403,"MODEL_OPERATOR_REQUIRED")
    from ai_extension import stop as ai_stop
    try:return ai_stop()
    except ValueError:_error(409,"LOCAL_MODEL_STOP_FAILED")

# Ephemeral user-selected engineering bundle. Never route this through the
# legacy demo service or store customer files in its shared SQLite corpus.
from threading import BoundedSemaphore
import hmac
from fastapi.concurrency import run_in_threadpool
from core06.private_cad_bundle import (
    MAX_PACKAGE as PRIVATE_KIT_MAX_BYTES,
    BundleInvalid, analyze_bundle,
)
_PRIVATE_KIT_SLOTS=BoundedSemaphore(1)


@app.post("/unified/api/engineering/bundle-check")
async def analyze_private_engineering_bundle(request:Request):
    require_unified_host(request)
    principal,session=_auth(request)
    initial_actor,initial_policy=authorization_context(principal,"engineering")
    if request.headers.get("content-type","").split(";",1)[0].strip().lower()!="application/octet-stream":
        _error(415,"CAD_PACKAGE_MEDIA_UNSUPPORTED")
    origin=request.headers.get("origin")
    if origin is not None and origin!=APP_ORIGIN:
        _error(403,"CAD_PACKAGE_ORIGIN_DENIED")
    if session is not None:
        if origin!=APP_ORIGIN:
            _error(403,"CAD_PACKAGE_ORIGIN_REQUIRED")
        if not hmac.compare_digest(request.headers.get("x-csrf-token",""),session.csrf):
            _error(403,"CAD_PACKAGE_CSRF_FAILED")
    size=request.headers.get("content-length")
    if size is not None:
        try:
            n=int(size)
        except ValueError:
            _error(400,"CAD_PACKAGE_SIZE_INVALID")
        if n<0:
            _error(400,"CAD_PACKAGE_SIZE_INVALID")
        if n>PRIVATE_KIT_MAX_BYTES:
            _error(413,"CAD_PACKAGE_TOO_LARGE")
    if not _PRIVATE_KIT_SLOTS.acquire(blocking=False):
        _error(429,"CAD_PACKAGE_PROCESSING_BUSY")
    try:
        incoming=bytearray()
        async for fragment in request.stream():
            if len(incoming)+len(fragment)>PRIVATE_KIT_MAX_BYTES:
                _error(413,"CAD_PACKAGE_TOO_LARGE")
            incoming.extend(fragment)
        try:
            result=await run_in_threadpool(analyze_bundle,bytes(incoming))
        except BundleInvalid as exc:
            _error(422,str(exc))
        final_actor,final_policy=authorization_context(principal,"engineering")
        if initial_actor!=final_actor or initial_policy!=final_policy:
            _error(409,"CAD_PACKAGE_POLICY_CHANGED")
        return JSONResponse(result,headers={
            "Cache-Control":"no-store",
            "X-Content-Type-Options":"nosniff",
        })
    finally:
        _PRIVATE_KIT_SLOTS.release()


# Reuse the tested P5 ingestion API, behind Unified host, OIDC and RLS.
# Disabled by default until versioned migrations and write grants are verified.
from p2_gateway.ingest_routes import attach_ingest_routes
attach_ingest_routes(app,_auth,_actor_db,APP_ORIGIN,_error,
                     prefix="/unified",enforce_host=require_unified_host)

from core06.cad_kit_routes import attach_cad_routes
attach_cad_routes(app,_auth,_actor_db,APP_ORIGIN,_error,require_unified_host)

@app.post("/unified/api/{unsupported:path}")
def denied_legacy_mutation(request:Request,unsupported:str):
    require_unified_host(request)
    _auth(request)
    _error(501,"LEGACY_MUTATION_NOT_INTEGRATED")

@app.get("/unified/api/part")
def missing_part_applicability(request:Request):
    require_unified_host(request)
    _auth(request)
    return {"state":"CURRENTNESS_UNVERIFIED","issue":"ENGINEERING_APPLICABILITY_NOT_MIGRATED"}

def authorized_engineering(request,subpath):
    require_unified_host(request)
    if subpath not in ALLOWED_ENG:_error(404,"ENGINEERING_ROUTE_NOT_FOUND")
    principal,_=_auth(request)
    source_id=request.query_params.get("id") if subpath=="file" else None
    # Permission to see all synthetic public engineering records is required,
    # otherwise the legacy P13 engine cannot guarantee complete source filtering.
    actor,epoch=authorization_context(principal,"engineering",source_id)
    params=list(request.query_params.multi_items())
    if len(params)>8 or any(k not in SAFE_PARAMS or len(v)>250 for k,v in params):
        _error(422,"ENGINEERING_QUERY_INVALID")
    if len({k for k,_ in params})!=len(params):
        _error(422,"DUPLICATE_QUERY_PARAMETERS")
    with httpx.Client(timeout=35,trust_env=False,follow_redirects=False) as client:
        login=client.post(LEGACY+"/api/demo-login",json={"role":"engineer"},
                          headers={"Origin":LEGACY})
        if login.status_code!=200:_error(503,"LEGACY_ENGINEERING_NOT_READY")
        target=LEGACY+"/api/engineering/"+subpath
        response=client.get(target,params=params,headers={"Accept":"application/json"})
        if response.status_code not in (200,201):
            _error(503 if response.status_code>=500 else response.status_code,
                   "ENGINEERING_DEMO_SOURCE_UNAVAILABLE")
        # Force a fresh authorization check AFTER even slow CAD image rendering.
        actor_after,generation_after=authorization_context(principal,"engineering",source_id)
        if actor_after!=actor or epoch!=generation_after:
            _error(409,"POLICY_CHANGED_DURING_ENGINEERING_REQUEST")
        binary=subpath in BINARY_ENDPOINTS
        if len(response.content)>(15*1024*1024 if binary else 1024*1024):
            _error(502,"LEGACY_RESPONSE_LIMIT")
        if binary:
            allowed_types={"file":"application/octet-stream",
                           "p11/preview":"image/png","p12/render":"image/png",
                           "p13/vectors":"image/png"}
            # Never forward upstream Content-Disposition or any cookies.
            return Response(content=response.content,
                media_type=allowed_types[subpath],
                headers={"X-Legacy-Synthetic-Data":"true"})
        try:
            result=response.json()
        except Exception:_error(502,"LEGACY_JSON_INVALID")
        return JSONResponse(content=result,headers={"X-Legacy-Synthetic-Data":"true"})

@app.get("/unified/api/engineering/kit-report")
def unified_engineering_kit_report(
    request:Request,q:str=Query(min_length=3,max_length=120)
):
    """One user-visible result from existing P8/P10 evidence.

    Protected by the SAME engineering authorization on every source request.
    No old demo credentials or direct legacy endpoint are exposed to browser.
    """
    from core06.engineering_kit_report import (
        InvalidEngineeringKit, build_engineering_kit_report,
    )
    require_unified_host(request)
    if not re.fullmatch(r"[a-zA-Z0-9_. -]{3,120}",q):
        _error(422,"ENGINEERING_PART_KEY_INVALID")
    part_response=authorized_engineering(request,"p8/part")
    geometry_response=authorized_engineering(request,"p10/features")
    try:
        return build_engineering_kit_report(
            json.loads(part_response.body),
            json.loads(geometry_response.body),
        )
    except InvalidEngineeringKit:
        _error(422,"ENGINEERING_KIT_EVIDENCE_INCOMPLETE")


@app.get("/unified/api/engineering/{subpath:path}")
def unified_engineering(request:Request,subpath:str):
    return authorized_engineering(request,subpath)

# P13 UI loads engineering images/files directly rather than via request().
@app.get("/api/engineering/{subpath:path}")
def unified_engineering_asset(request:Request,subpath:str):
    return authorized_engineering(request,subpath)

@app.get("/sample-scan.pdf")
def public_demo_sample(request:Request):
    require_unified_host(request)
    _auth(request)
    return FileResponse(ROOT/"samples"/"factory_scanned_pump_demo.pdf",
                        media_type="application/octet-stream",
                        filename="factory_scanned_pump_demo.pdf")
