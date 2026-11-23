"""Opt-in Keycloak/RLS raw-file ingress for isolated v0.6 pilot.

Endpoints remain disabled unless FACTORY_V06_INGEST_ENABLED=1 and v2 migration
has been applied. No direct SQL client mutation / legacy SQLite fallback.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
from urllib.parse import quote, unquote
from threading import BoundedSemaphore

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from psycopg.errors import InsufficientPrivilege, UniqueViolation

from core06.ingest_service import (
    MAX_BYTES, MEDIA, UploadInputError, prepare_upload, persist_upload,
)

_UPLOAD_SLOTS = BoundedSemaphore(2)

UPLOAD_ID = re.compile(r"upload-[0-9a-f]{32}\Z")


def attach_ingest_routes(app, authenticate, actor_db, app_origin, deny,
                         prefix="", enforce_host=None):
    if prefix not in ("", "/unified"):
        raise ValueError("UNEXPECTED_UPLOAD_PREFIX")
    def check_host(request: Request):
        if enforce_host is not None:
            enforce_host(request)

    def enabled(request):
        check_host(request)
        if os.environ.get("FACTORY_V06_INGEST_ENABLED") != "1":
            deny(503, "PROTECTED_UPLOAD_NOT_ENABLED")

    def check_csrf(request: Request, web_session):
        origin = request.headers.get("origin")
        if origin is not None and origin != app_origin:
            deny(403, "UPLOAD_ORIGIN_DENIED")
        if web_session is not None:
            if origin != app_origin:
                deny(403, "UPLOAD_ORIGIN_REQUIRED")
            if not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), web_session.csrf
            ):
                deny(403, "CSRF_TOKEN_FAILED")

    @app.get(prefix + "/api/upload-capabilities")
    def upload_capabilities(request: Request):
        check_host(request)
        principal, _session = authenticate(request)
        if os.environ.get("FACTORY_V06_INGEST_ENABLED") != "1":
            return {"enabled": False, "writable_scopes": [], "max_bytes": MAX_BYTES}
        with actor_db(principal) as (db, _actor):
            scopes = [row[0] for row in db.execute(
                "SELECT * FROM core.writable_scopes()"
            ).fetchall()]
        return {"enabled": bool(scopes), "writable_scopes": scopes, "max_bytes": MAX_BYTES}

    @app.post(prefix + "/api/uploads")
    async def protected_upload(request: Request):
        enabled(request)
        principal, web_session = authenticate(request)
        check_csrf(request, web_session)
        try:
            filename = unquote(request.headers.get("x-filename", "")[:650],
                               encoding="utf-8", errors="strict")
            title = unquote(request.headers.get("x-title", "")[:650],
                            encoding="utf-8", errors="strict")
        except UnicodeError:
            deny(422, "UPLOAD_METADATA_ENCODING_INVALID")
        scope_id = request.headers.get("x-scope-id", "")
        idem = request.headers.get("idempotency-key", "")
        declared_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not filename or not title or not scope_id:
            deny(422, "UPLOAD_METADATA_REQUIRED")
        if len(filename) > 180 or len(title) > 200 or len(scope_id) > 128:
            deny(422, "UPLOAD_METADATA_TOO_LONG")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", idem):
            deny(422, "IDEMPOTENCY_KEY_REQUIRED")
        if MEDIA.get(Path(filename).suffix.lower()) != declared_type:
            deny(415, "UPLOAD_MEDIA_TYPE_MISMATCH")
        size_header = request.headers.get("content-length")
        if size_header:
            try:
                if int(size_header) < 0:
                    deny(400, "INVALID_CONTENT_LENGTH")
                if int(size_header) > MAX_BYTES:
                    deny(413, "UPLOAD_TOO_LARGE")
            except ValueError:
                deny(400, "INVALID_CONTENT_LENGTH")
        # Reject read-only actors before expensive OCR, then re-check at commit.
        with actor_db(principal) as (db, _actor):
            if not db.execute("SELECT core.can_write_scope(%s)",(scope_id,)).fetchone()[0]:
                deny(403, "SCOPE_WRITE_DENIED")
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > MAX_BYTES:
                deny(413, "UPLOAD_TOO_LARGE")
            body.extend(part)

        def commit():
            if not _UPLOAD_SLOTS.acquire(blocking=False):
                deny(429, "UPLOAD_PROCESSING_BUSY")
            try:
                return _do_commit()
            finally:
                _UPLOAD_SLOTS.release()

        def _do_commit():
            try:
                document = prepare_upload(filename, title, bytes(body))
            except UploadInputError:
                deny(422, "UPLOAD_EXTRACTION_OR_VALIDATION_FAILED")
            with actor_db(principal) as (db, actor):
                try:
                    doc_id = persist_upload(db, actor, scope_id, document, idem)
                except PermissionError:
                    deny(403, "SCOPE_WRITE_DENIED")
                except InsufficientPrivilege as exc:
                    if "IDEMPOTENT_DOCUMENT_NOT_AVAILABLE" in str(exc):
                        deny(409, "IDEMPOTENT_DOCUMENT_NOT_AVAILABLE")
                    deny(403, "SCOPE_WRITE_DENIED")
                except UniqueViolation:
                    deny(409, "IDEMPOTENCY_KEY_CONFLICT")
                reused = doc_id != document.id
                status = db.execute(
                    "SELECT core.upload_index_status(%s)", (doc_id,)
                ).fetchone()[0]
            return JSONResponse(
                status_code=200 if reused else 201,
                content={
                    "document_id": doc_id, "reused": reused,
                    "sha256": document.sha256,
                    "extracted_pages": len(document.chunks),
                    "index_status": status or "pending",
                    "note": document.note,
                },
            )
        return await run_in_threadpool(commit)

    @app.get(prefix + "/api/uploads/{doc_id}/status")
    def protected_status(request: Request, doc_id: str):
        enabled(request)
        if not UPLOAD_ID.fullmatch(doc_id):
            deny(404, "DOCUMENT_NOT_FOUND")
        principal, _ = authenticate(request)
        with actor_db(principal) as (db, _actor):
            row = db.execute(
                "SELECT core.upload_index_status(%s)", (doc_id,)
            ).fetchone()
            if not row or row[0] is None:
                deny(404, "DOCUMENT_NOT_FOUND")
        return {"document_id": doc_id, "index_status": row[0]}

    @app.delete(prefix + "/api/uploads/{doc_id}")
    def protected_delete(request: Request, doc_id: str):
        enabled(request)
        if not UPLOAD_ID.fullmatch(doc_id):
            deny(404, "DOCUMENT_NOT_FOUND")
        principal, web_session = authenticate(request)
        check_csrf(request, web_session)
        with actor_db(principal) as (db, _actor):
            try:
                row = db.execute(
                    "SELECT core.delete_uploaded_document(%s)", (doc_id,)
                ).fetchone()
            except Exception as exc:
                # PostgreSQL intentionally does not disclose existence of private documents.
                if getattr(exc, "sqlstate", None) == "42501":
                    deny(404, "DOCUMENT_NOT_FOUND")
                raise
        return {"document_id": doc_id, "deleted": bool(row and row[0]),
                "index_cleanup": "pending"}

    @app.get(prefix + "/api/uploads/{doc_id}/original")
    def protected_original(request: Request, doc_id: str):
        enabled(request)
        if not UPLOAD_ID.fullmatch(doc_id):
            deny(404, "DOCUMENT_NOT_FOUND")
        principal, _ = authenticate(request)
        # Fresh RLS transaction; no cache of source bytes between requests.
        with actor_db(principal) as (db, _actor):
            row = db.execute(
                """SELECT filename,media_type,original_bytes,content_sha256
                     FROM core.document_blob WHERE document_id=%s""",
                (doc_id,),
            ).fetchone()
            if row is None:
                deny(404, "DOCUMENT_NOT_FOUND")
        filename, media_type, data, sha = row
        if hashlib.sha256(data).hexdigest() != sha:
            deny(503, "SOURCE_INTEGRITY_FAILED")
        return Response(
            content=bytes(data), media_type=media_type,
            headers={
                "Content-Disposition": "attachment; filename=source; filename*=UTF-8''" + quote(filename, safe=""),
                "X-Content-SHA256": sha,
                "Cache-Control": "private, no-store",
            },
        )
