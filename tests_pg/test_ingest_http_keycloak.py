"""Real signed Keycloak tokens -> isolated FastAPI app -> scratch PostgreSQL upload.

No live service restarted. All input data and the temporary DB are synthetic.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
import uuid
from unittest.mock import patch

import ai_extension
import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
from core06.hybrid_retrieval import Hit
from tools.p3_gateway_smoke import mint
from p2_gateway.security import verify_access_token
os.environ["FACTORY_P2_APP_ORIGIN"]="http://localhost:19139"
import p2_gateway.app as p2module
import unified_app as unified
import p2_gateway.ingest_routes as ingest_routes
from core06.db_client import _creds_dir, _pg_port

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "factory_ingest_http_" + uuid.uuid4().hex[:12]


def connect(admin=False, name=TEST_DB):
    return psycopg.connect(
        host="127.0.0.1", port=_pg_port(), dbname=name,
        user="factory_pg_admin" if admin else "factory_gateway",
        password=(_creds_dir() / ("admin-password.txt" if admin else "gateway-password.txt")).read_text("ascii"),
        connect_timeout=6, autocommit=True,
    )


def check_ask_revocation(client, host, csrf, principal, document_id, marker):
    class PolicyOnlyRetriever:
        def search(self, actor, question, limit, **identity):
            with unified._actor_db(principal) as (db, actual_actor):
                assert actor == actual_actor
                row = db.execute("""
                    SELECT c.id, d.id, d.title, c.body, c.page_no, d.revision
                    FROM core.document_chunk c JOIN core.document d ON d.id=c.document_id
                    WHERE d.id=%s ORDER BY c.id LIMIT 1
                """, (document_id,)).fetchone()
            return [Hit(*row, 1.0)] if row else []

    def revoke():
        with connect(admin=True) as db:
            db.execute("SELECT core.revoke_scope_access('engineer','ingest_lab','test_runner')")

    def model_status(mode):
        if mode == "offline":
            revoke()
            return {"status": "stopped"}
        return {"status": "ready"}

    def generate(mode, question, evidence):
        assert marker in evidence[0]["body"]
        revoke()
        if mode == "invalid":
            raise ValueError("fixture produced invalid output")
        return {"answer": marker, "citations": [1]}

    for mode in ("generated", "invalid", "offline"):
        with (patch.object(unified, "_p3_retriever", return_value=PolicyOnlyRetriever()),
              patch.object(ai_extension, "status", side_effect=lambda: model_status(mode)),
              patch.object(ai_extension, "generate", side_effect=lambda *args: generate(mode, *args))):
            response = client.post("/unified/api/ask", json={"question": marker},
                                   headers={"Origin": host, "X-CSRF-Token": csrf})
        assert response.status_code == 409, (mode, response.status_code)
        assert marker not in response.text and "SOURCES_CHANGED_DURING_GENERATION" in response.text
        with connect(admin=True) as db:
            db.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES ('engineer','ingest_lab')")
    print("PASS Unified ask: revoke during generation, parse error and offline check hides all excerpts")


def main():
    engineertoken = mint("engineer")
    technotoken = mint("technologist")
    engineer = verify_access_token(engineertoken)
    techno = verify_access_token(technotoken)
    with connect(True, "factory_core_v06") as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    old_db = p2module.database_session
    old_flag = os.environ.get("FACTORY_V06_INGEST_ENABLED")
    try:
        with connect(True) as db:
            for f in ("schema.sql", "p2_oidc.sql", "ingestion_v1.sql", "ingestion_v2.sql"):
                db.execute((ROOT / "core06" / f).read_text(encoding="utf-8-sig"))
            db.execute("INSERT INTO core.subject(id) VALUES('engineer'),('technologist')")
            db.execute("INSERT INTO core.scope(id) VALUES('ingest_lab')")
            db.execute("""INSERT INTO core.scope_grant(subject_id,scope_id)
                         VALUES('engineer','ingest_lab'),('technologist','ingest_lab')""")
            db.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES('engineer','ingest_lab')")
            db.execute(
                """INSERT INTO core.oidc_identity(issuer,external_sub,subject_id)
                   VALUES(%s,%s,'engineer'),(%s,%s,'technologist')""",
                (engineer.issuer, engineer.external_sub, techno.issuer, techno.external_sub),
            )
        p2module.database_session = lambda admin=False: connect(admin)
        os.environ["FACTORY_V06_INGEST_ENABLED"] = "1"
        host = p2module.APP_ORIGIN
        payload = b"Real Keycloak protected local upload document TOL-995"
        common = {
            "X-Filename": "source.txt", "X-Title": "Manufacturing test artifact",
            "X-Scope-Id": "ingest_lab", "Content-Type": "text/plain",
            "Idempotency-Key": "http-keycloak-ingestion-000001",
        }
        with TestClient(unified.app, base_url=host) as client:
            assert client.post("/unified/api/uploads", content=payload, headers=common).status_code == 401
            engineer_caps = client.get("/unified/api/upload-capabilities",headers={"Authorization":"Bearer "+engineertoken})
            assert engineer_caps.status_code == 200 and engineer_caps.json()["writable_scopes"] == ["ingest_lab"]
            techno_caps = client.get("/unified/api/upload-capabilities",headers={"Authorization":"Bearer "+technotoken})
            assert techno_caps.status_code == 200 and not techno_caps.json()["enabled"]
            unauthorized = client.post(
                "/unified/api/uploads", content=payload,
                headers={**common, "Authorization": "Bearer " + technotoken,
                         "X-Actor-Id": "engineer"},
            )
            assert unauthorized.status_code == 403, unauthorized.text[:200]
            bad_type = client.post(
                "/unified/api/uploads", content=payload,
                headers={**common, "X-Filename": "bad.pdf",
                         "Authorization": "Bearer " + engineertoken},
            )
            assert bad_type.status_code == 415
            with connect(admin=True) as db:
                before = db.execute("SELECT count(*) FROM core.ingest_outbox").fetchone()
            corrupt = client.post(
                "/unified/api/uploads", content=b"%PDF-1.7\ncorrupt fixture",
                headers={**common, "X-Filename": "corrupt.pdf", "Content-Type": "application/pdf",
                         "Authorization": "Bearer " + engineertoken},
            )
            assert corrupt.status_code == 422, corrupt.status_code
            with connect(admin=True) as db:
                assert db.execute("SELECT count(*) FROM core.ingest_outbox").fetchone() == before
            # Busy OCR workers fail quickly; a read-only user must fail before OCR.
            assert ingest_routes._UPLOAD_SLOTS.acquire(blocking=False)
            assert ingest_routes._UPLOAD_SLOTS.acquire(blocking=False)
            try:
                busy = client.post(
                    "/unified/api/uploads", content=payload,
                    headers={**common, "Authorization": "Bearer " + engineertoken,
                             "Idempotency-Key": "busy-concurrency-000001"},
                )
                assert busy.status_code == 429, busy.text[:200]
                denied_while_busy = client.post(
                    "/unified/api/uploads", content=payload,
                    headers={**common, "Authorization": "Bearer " + technotoken,
                             "Idempotency-Key": "busy-readonly-00000001"},
                )
                assert denied_while_busy.status_code == 403, denied_while_busy.text[:200]
            finally:
                ingest_routes._UPLOAD_SLOTS.release()
                ingest_routes._UPLOAD_SLOTS.release()
            ok = client.post(
                "/unified/api/uploads", content=payload,
                headers={**common, "Authorization": "Bearer " + engineertoken},
            )
            assert ok.status_code == 201, (ok.status_code, ok.text[:200])
            doc_id = ok.json()["document_id"]
            replay = client.post(
                "/unified/api/uploads",content=payload,
                headers={**common,"Authorization":"Bearer "+engineertoken},
            )
            assert replay.status_code == 200 and replay.json()["document_id"] == doc_id,(replay.status_code,replay.text[:280])
            assert replay.json()["reused"] is True
            conflict = client.post(
                "/unified/api/uploads",content=b"different payload",
                headers={**common,"Authorization":"Bearer "+engineertoken},
            )
            assert conflict.status_code == 409, conflict.text[:250]
            status = client.get(
                "/unified/api/uploads/" + doc_id + "/status",
                headers={"Authorization":"Bearer "+engineertoken},
            )
            assert status.status_code == 200 and status.json()["index_status"] == "pending"
            assert ok.json()["sha256"] == hashlib.sha256(payload).hexdigest()
            eng_original = client.get(
                "/unified/api/uploads/" + doc_id + "/original",
                headers={"Authorization": "Bearer " + engineertoken},
            )
            assert eng_original.status_code == 200 and eng_original.content == payload
            techno_read = client.get(
                "/unified/api/uploads/" + doc_id + "/original",
                headers={"Authorization": "Bearer " + technotoken},
            )
            assert techno_read.status_code == 200  # explicit read-only scope grant

            # Real signed Keycloak access token stored in an isolated in-memory
            # web session to exercise the cookie CSRF boundary.
            cookie_value = "isolated-ingest-test-" + uuid.uuid4().hex
            p2module._SESSIONS[cookie_value] = p2module.WebSession(
                access_token=engineertoken, exp=int(time.time())+300,
                csrf="csrf-"+uuid.uuid4().hex, id_token="not-used-for-upload",
            )
            try:
                cookie_client = TestClient(p2module.app, base_url=host)
                cookie_client.cookies.set(p2module.COOKIE_SESSION, cookie_value)
                no_csrf = cookie_client.post(
                    "/unified/api/uploads", content=payload, headers=common)
                assert no_csrf.status_code == 403, no_csrf.text[:180]
                wrong_origin = cookie_client.post(
                    "/unified/api/uploads", content=payload,
                    headers={**common, "Origin": "http://malicious.test",
                             "X-CSRF-Token": p2module._SESSIONS[cookie_value].csrf},
                )
                assert wrong_origin.status_code == 403
                valid = cookie_client.post(
                    "/unified/api/uploads", content=payload,
                    headers={**common, "Idempotency-Key": "cookie-ingestion-000000001", "Origin": host,
                             "X-CSRF-Token": p2module._SESSIONS[cookie_value].csrf},
                )
                assert valid.status_code == 201, valid.text[:200]
                check_ask_revocation(cookie_client, host, p2module._SESSIONS[cookie_value].csrf,
                                     engineer, doc_id, "TOL-995")
                cookie_client.close()
            finally:
                p2module._SESSIONS.pop(cookie_value, None)

            no_delete = client.delete(
                "/unified/api/uploads/" + doc_id,
                headers={"Authorization":"Bearer "+technotoken},
            )
            assert no_delete.status_code == 404, no_delete.text[:180]
            removed = client.delete(
                "/unified/api/uploads/" + doc_id,
                headers={"Authorization":"Bearer "+engineertoken},
            )
            assert removed.status_code == 200 and removed.json()["deleted"]
            removed_again = client.delete(
                "/unified/api/uploads/" + doc_id,
                headers={"Authorization":"Bearer "+engineertoken},
            )
            assert removed_again.status_code == 200 and not removed_again.json()["deleted"]
            assert client.get(
                "/unified/api/uploads/" + doc_id + "/status",
                headers={"Authorization":"Bearer "+engineertoken},
            ).status_code == 404
            with connect(True) as db:
                assert db.execute(
                    "SELECT count(*) FROM core.ingest_outbox WHERE document_id=%s AND event_kind='delete'",
                    (doc_id,),
                ).fetchone()[0] == 1
                db.execute("SELECT core.revoke_scope_access('engineer','ingest_lab','http_test')")
            blocked = client.get(
                "/unified/api/uploads/" + doc_id + "/original",
                headers={"Authorization": "Bearer " + engineertoken},
            )
            assert blocked.status_code == 404, blocked.text[:200]
            assert client.get(
                "/unified/api/uploads/" + doc_id + "/original").status_code == 401
        print("PASS Keycloak-signed HTTP: no auth, read-vs-write, raw upload, original, CSRF, forged actor, revoke")
    finally:
        p2module.database_session = old_db
        if old_flag is None: os.environ.pop("FACTORY_V06_INGEST_ENABLED", None)
        else: os.environ["FACTORY_V06_INGEST_ENABLED"] = old_flag
        with connect(True, "factory_core_v06") as db:
            db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))


if __name__ == "__main__":
    main()
