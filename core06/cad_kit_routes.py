"""Authenticated, original-preserving CAD revision API for the Unified app.

Original PDF/STEP/BOM bytes remain in a single bounded, SHA-256 verified CZKIT1
package in PostgreSQL. Stored analyses are advisory, not manufacturing approval.
"""
from __future__ import annotations
import hashlib,hmac,json,re
from urllib.parse import quote
from threading import BoundedSemaphore
from fastapi import Request,Response
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from psycopg import Error as PgError
from core06.private_cad_bundle import MAX_PACKAGE,BundleInvalid,analyze_bundle,unpack_bundle

_SLOTS=BoundedSemaphore(1)
_KIT_ID=re.compile(r"kit-[0-9a-f]{32}\Z")
_IDEM=re.compile(r"[A-Za-z0-9_-]{16,100}\Z")

def attach_cad_routes(app,authenticate,actor_db,app_origin,deny,require_host):
    def secure(request,write=False):
        require_host(request)
        principal,session=authenticate(request)
        if write:
            origin=request.headers.get("origin")
            if origin is not None and origin!=app_origin:
                deny(403,"CAD_ORIGIN_DENIED")
            if session is not None:
                if origin!=app_origin or not hmac.compare_digest(
                    request.headers.get("x-csrf-token",""),session.csrf):
                    deny(403,"CAD_CSRF_DENIED")
        return principal

    @app.get("/unified/api/engineering/kits")
    def list_kits(request:Request):
        principal=secure(request)
        with actor_db(principal) as (db,actor):
            rows=db.execute("""SELECT id,part,scope_id,latest_revision,created_at
                FROM core.cad_kit ORDER BY created_at DESC,id LIMIT 60""").fetchall()
        return {"items":[{"kit_id":r[0],"part":r[1],"scope_id":r[2],
             "latest_revision":r[3],"created_at":r[4].isoformat()} for r in rows],
             "manufacturing_authorization":False}

    @app.get("/unified/api/engineering/kits/{kit_id}/revisions")
    def list_revisions(request:Request,kit_id:str):
        principal=secure(request)
        if not _KIT_ID.fullmatch(kit_id):deny(404,"CAD_KIT_NOT_FOUND")
        with actor_db(principal) as (db,actor):
            # PostgreSQL RLS limits both table and revision contents.
            kit=db.execute("SELECT part,scope_id,latest_revision FROM core.cad_kit WHERE id=%s",
                           (kit_id,)).fetchone()
            if kit is None:deny(404,"CAD_KIT_NOT_FOUND")
            rows=db.execute("""SELECT revision_no,id,package_sha256,created_at,created_by,report
                FROM core.cad_kit_revision WHERE kit_id=%s ORDER BY revision_no DESC LIMIT 100""",
                (kit_id,)).fetchall()
        return {"kit_id":kit_id,"part":kit[0],"scope_id":kit[1],
             "latest_revision":kit[2],
             "revisions":[{"revision_no":v[0],"revision_id":v[1],"package_sha256":v[2],
                "created_at":v[3].isoformat(),"created_by":v[4],
                "report":v[5],"manufacturing_authorization":False} for v in rows]}

    @app.get("/unified/api/engineering/kits/{kit_id}/revisions/{number}/source/{role}")
    def original_source(request:Request,kit_id:str,number:int,role:str):
        principal=secure(request)
        if not _KIT_ID.fullmatch(kit_id) or number<1 or number>10000 or role not in ("bom","drawing","model"):
            deny(404,"CAD_ORIGINAL_NOT_FOUND")
        with actor_db(principal) as (db,actor):
            row=db.execute("""SELECT r.package,r.package_sha256,r.report
               FROM core.cad_kit_revision r WHERE r.kit_id=%s AND r.revision_no=%s""",
               (kit_id,number)).fetchone()
            if row is None:deny(404,"CAD_ORIGINAL_NOT_FOUND")
            pkg=bytes(row[0])
        if hashlib.sha256(pkg).hexdigest()!=row[1]:
            deny(503,"CAD_STORAGE_INTEGRITY_FAILURE")
        try:
            files,_=unpack_bundle(pkg)
        except BundleInvalid:
            deny(503,"CAD_STORAGE_INTEGRITY_FAILURE")
        source=files[role]
        matched=[x for x in row[2].get("sources",[]) if
            x.get("role")==role and x.get("name")==source.name and
            x.get("sha256")==source.sha256]
        if len(matched)!=1:
            deny(503,"CAD_SOURCE_INTEGRITY_FAILURE")
        typ={"bom":"text/csv","drawing":"application/pdf","model":"application/step"}[role]
        return Response(content=source.data,media_type=typ,headers={
            "Cache-Control":"no-store, private","X-Content-Type-Options":"nosniff",
            "Content-Disposition":"attachment; filename*=UTF-8''"+quote(source.name,safe="")})

    @app.post("/unified/api/engineering/kits")
    async def save_kit(request:Request):
        principal=secure(request,write=True)
        # Verify write entitlement before inspecting even malformed upload metadata.
        with actor_db(principal) as (db,actor):
            writable=db.execute("SELECT core.can_write_scope('engineering')").fetchone()[0]
            if not writable:deny(403,"CAD_WRITE_FORBIDDEN")
        if request.headers.get("content-type","").split(";",1)[0].strip().lower()!="application/octet-stream":
            deny(415,"CAD_MEDIA_UNSUPPORTED")
        key=request.headers.get("idempotency-key","")
        if not _IDEM.fullmatch(key):deny(422,"CAD_IDEMPOTENCY_REQUIRED")
        kit_id=request.headers.get("x-kit-id") or None
        if kit_id is not None and not _KIT_ID.fullmatch(kit_id):
            deny(422,"CAD_KIT_ID_INVALID")
        expected_raw=request.headers.get("x-expected-revision","0")
        if not expected_raw.isascii() or not expected_raw.isdigit() or len(expected_raw)>4:
            deny(422,"CAD_EXPECTED_REVISION_INVALID")
        expected=int(expected_raw)
        if expected>9999:deny(422,"CAD_EXPECTED_REVISION_INVALID")
        length=request.headers.get("content-length")
        if length is not None:
            try:n=int(length)
            except ValueError:deny(400,"CAD_LENGTH_INVALID")
            if n<0:deny(400,"CAD_LENGTH_INVALID")
            if n>MAX_PACKAGE:deny(413,"CAD_PACKAGE_TOO_LARGE")
        if not _SLOTS.acquire(False):deny(429,"CAD_SAVE_BUSY")
        try:
            payload=bytearray()
            async for data in request.stream():
                if len(payload)+len(data)>MAX_PACKAGE:
                    deny(413,"CAD_PACKAGE_TOO_LARGE")
                payload.extend(data)
            try:
                report=await run_in_threadpool(analyze_bundle,bytes(payload))
            except BundleInvalid as exc:
                deny(422,str(exc))
            # Re-resolve verified issuer/sub and check writer role after heavy parsing.
            with actor_db(principal) as (db,final_actor):
                if actor!=final_actor:deny(409,"CAD_IDENTITY_CHANGED")
                try:
                    saved=db.execute("""SELECT core.save_cad_kit_revision(
                        %s,'engineering',%s,%s,%s,%s,%s::jsonb)""",
                        (key,report["part"],kit_id,expected,bytes(payload),
                         json.dumps(report,ensure_ascii=False))).fetchone()[0]
                except PgError as e:
                    if e.sqlstate=="42501":deny(403,"CAD_WRITE_FORBIDDEN")
                    if e.sqlstate in ("23505","23503"):deny(409,"CAD_REVISION_CONFLICT")
                    if e.sqlstate=="22023":deny(422,"CAD_SOURCE_INVALID")
                    deny(503,"CAD_DATABASE_WRITE_FAILED")
            return JSONResponse({
                **saved,"part":report["part"],"sources":report["sources"],
                "state":"REVIEW_REQUIRED","manufacturing_authorization":False,
                "persistence":"POSTGRESQL_RLS_VERSIONED",
            },status_code=200 if saved["reused"] else 201)
        finally:
            _SLOTS.release()
