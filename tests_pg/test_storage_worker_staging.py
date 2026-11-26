"""Isolated real PostgreSQL 18 + native Qdrant + LocalE5 worker staging & recovery test.

Zero touch to live ports (55436/16333/18080/19139), live DB, live worker, or real docs.
Requires FACTORY_PG_BIN and FACTORY_AUDIT_QDRANT_EXE (skips cleanly if unavailable).
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from psycopg import sql
from qdrant_client import QdrantClient

import ai_extension
from core06 import db_client, hybrid_retrieval as hybrid, ingest_index_worker
from core06.ingest_service import persist_upload, prepare_upload
import p2_gateway.app as p2module
from p2_gateway.security import Principal
from tests_pg import run_isolated_v06_e2e as iso
from tests_pg.native_vector_fixture import native_vector
from tools import outbox_retry_recovery, vector_index_integrity_audit as via
import unified_app

ROOT = Path(__file__).resolve().parents[1]


def _load_worker_host():
    script = ROOT / "tools" / "storage_index_worker_host.pyw"
    loader = SourceFileLoader("storage_worker_host_staging", str(script))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _start_ephemeral_pg(tmp: Path, creds: Path, pg_port: int, stage_db: str) -> Path:
    iso._grant_current_user_acl(tmp)
    pgdata = tmp / "pg"
    pglog = tmp / "pg.log"
    pgctl_log = tmp / "pgctl.log"
    admin_pw = secrets.token_urlsafe(48)
    gateway_pw = secrets.token_urlsafe(48)
    (creds / "admin-password.txt").write_text(admin_pw, encoding="ascii")
    (creds / "gateway-password.txt").write_text(gateway_pw, encoding="ascii")

    init = iso.run_cmd(
        [
            str(iso.PG_BIN / "initdb.exe"),
            "-D", str(pgdata),
            "-U", "factory_pg_admin",
            f"--pwfile={creds / 'admin-password.txt'}",
            "-A", "scram-sha-256",
            "-E", "UTF8",
            "--no-locale",
            "--no-sync",
        ],
        env=os.environ.copy(),
        timeout=120,
    )
    if init.returncode != 0:
        raise RuntimeError(f"INITDB_FAILED: {init.stderr[-600:]}")
    (pgdata / "postgresql.auto.conf").write_text(
        "fsync = off\nsynchronous_commit = off\nfull_page_writes = off\n",
        encoding="ascii",
    )
    iso.pg_ctl_start(pgdata, pglog, pgctl_log, pg_port)
    with iso.pg_connect(pg_port, creds, "postgres", admin=True) as db:
        db.execute(
            """
            CREATE ROLE factory_owner NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
            CREATE ROLE factory_gateway LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
            """
        )
        db.execute(sql.SQL("ALTER ROLE factory_gateway WITH PASSWORD {}").format(sql.Literal(gateway_pw)))
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(stage_db)))
        db.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC; GRANT CONNECT ON DATABASE {} TO factory_gateway;").format(
                sql.Identifier(stage_db), sql.Identifier(stage_db)
            )
        )
    with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
        for mig in iso.MIGRATIONS:
            db.execute((ROOT / "core06" / mig).read_text(encoding="utf-8-sig"))
    return pgdata


def _stop_ephemeral_pg(pgdata: Path | None) -> None:
    if pgdata is None or not pgdata.exists():
        return
    iso.run_cmd(
        [str(iso.PG_BIN / "pg_ctl.exe"), "-D", str(pgdata), "-m", "immediate", "stop"],
        env=os.environ.copy(),
        timeout=30,
    )


class IsolatedStorageWorkerStagingTest(unittest.TestCase):
    def test_outage_recovery_indexing_search_source_links_idempotency_and_negatives(self):
        if iso.PG_BIN is None or not (iso.PG_BIN / "initdb.exe").is_file():
            self.skipTest("FACTORY_PG_BIN not configured")
        if iso.QDRANT_EXE is None or not iso.QDRANT_EXE.is_file():
            self.skipTest("FACTORY_AUDIT_QDRANT_EXE not configured")

        scratch_parent = Path(os.environ.get("FACTORY_AUDIT_SCRATCH", "") or Path(os.environ.get("TEMP", ".")))
        scratch_parent.mkdir(parents=True, exist_ok=True)
        tmp = scratch_parent / ("st_" + secrets.token_hex(3))
        tmp.mkdir(parents=True, exist_ok=True)
        creds = tmp / "c"
        creds.mkdir()

        pg_port = iso.free_port(set())
        stage_db = "factory_ingest_stage_" + secrets.token_hex(6)
        col_name = "c_" + secrets.token_hex(4)
        pgdata = None
        old_env = {k: os.environ.get(k) for k in ("FACTORY_PG_PORT", "FACTORY_PG_CREDS_DIR", "FACTORY_V06_INGEST_ENABLED")}
        old_collection = hybrid.COLLECTION
        old_url = hybrid.URL
        old_p2_db = p2module.database_session
        old_hybrid_db = hybrid.database_session
        old_retriever = p2module._HYBRID_RETRIEVER

        try:
            pgdata = _start_ephemeral_pg(tmp, creds, pg_port, stage_db)
            os.environ["FACTORY_PG_PORT"] = str(pg_port)
            os.environ["FACTORY_PG_CREDS_DIR"] = str(creds)
            os.environ["FACTORY_V06_INGEST_ENABLED"] = "1"

            issuer = p2module.ISSUER
            with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                db.execute("INSERT INTO core.subject(id) VALUES ('engineer'), ('finance')")
                db.execute("INSERT INTO core.scope(id,classification) VALUES ('engineering','internal'), ('finance','restricted')")
                db.execute(
                    "INSERT INTO core.scope_grant(subject_id,scope_id) VALUES ('engineer','engineering'), ('finance','finance')"
                )
                db.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES ('engineer','engineering')")
                db.execute(
                    "INSERT INTO core.oidc_identity(issuer,external_sub,subject_id) VALUES (%s,'sub-eng-stg-01','engineer'), (%s,'sub-fin-stg-01','finance')",
                    (issuer, issuer),
                )

            # Synthetic Doc 1: Standard technical passport (.txt)
            doc1_bytes = (
                "Паспорт насосного агрегата Н-125 (изд. АБВГ.712345.004-02).\n"
                "Рабочее давление 25 бар, максимальная температура корпуса 78 C.\n"
                "Уплотнение торцевое тип УТ-125, материал корпуса сталь 12Х18Н10Т.\n"
            ).encode("utf-8")
            up1 = prepare_upload("passport_n125.txt", "Паспорт насоса Н-125", doc1_bytes)

            # Synthetic Doc 2: Oversized valid .txt (>15,000 chars) with critical tail fact (SW-2 regression)
            doc2_tail = "Контрольный факт: Клапан КП-440М давление срабатывания 18,5 МПа температура среды 115 C."
            doc2_text = ("Регламент планового обслуживания линии циркуляции цеха 4.\n" * 390) + doc2_tail + "\n"
            self.assertGreater(len(doc2_text), 20_000)
            up2 = prepare_upload("protocol_kp440m.txt", "Протокол испытаний клапана КП-440М", doc2_text.encode("utf-8"))
            self.assertGreaterEqual(len(up2.chunks), 2)

            # Synthetic Doc 3: Real scanned PDF via Windows WinRT OCR
            sample_pdf = (ROOT / "samples" / "factory_scanned_pump_demo.pdf").read_bytes()
            up3 = prepare_upload("factory_scanned_pump_demo.pdf", "Скан паспорта насоса Н-125", sample_pdf)
            self.assertTrue(any(ch["extraction_method"] == "ocr" for ch in up3.chunks))

            with iso.pg_connect(pg_port, creds, stage_db, admin=False) as gw:
                doc1_id = persist_upload(gw, "engineer", "engineering", up1, "stg-doc1-idem-key-0001")
                doc2_id = persist_upload(gw, "engineer", "engineering", up2, "stg-doc2-idem-key-0002")
                doc3_id = persist_upload(gw, "engineer", "engineering", up3, "stg-doc3-idem-key-0003")

            # Phase 1: Verify worker host outage standby BEFORE Qdrant is running (SW-1 live-equivalent)
            closed_qport = iso.free_port({pg_port})
            hybrid.COLLECTION = col_name
            hybrid.URL = f"http://127.0.0.1:{closed_qport}"
            host = _load_worker_host()
            states_outage = []
            polls_outage = iter([False] * 8 + [True])
            stop_outage = SimpleNamespace(exists=lambda: next(polls_outage))

            with (
                patch.object(host, "STOPFILE", stop_outage),
                patch.object(host, "state", side_effect=lambda s, **kw: states_outage.append((s, kw))),
                patch.object(host.time, "sleep"),
                self.assertLogs(level="WARNING") as outage_logs,
            ):
                rc_out = host.run_loop(
                    lambda: ingest_index_worker.process_one(
                        db_factory=lambda admin=False: db_client.database_session(stage_db, admin=admin),
                    ),
                    backends_ready=host.default_backends_ready,
                )
            self.assertEqual(rc_out, 0)
            degraded_out = [kw for s, kw in states_outage if s == "degraded"]
            self.assertEqual(len(degraded_out), 8)
            self.assertEqual([d["error_count"] for d in degraded_out], [1] * 8)
            self.assertEqual(degraded_out[-1]["missing_backends"], ["qdrant"])
            self.assertEqual(len(outage_logs.output), 1)
            # Zero outbox retries spent while Qdrant was down!
            with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                attempts_after_outage = db.execute("SELECT DISTINCT status, attempts FROM core.ingest_outbox").fetchall()
            self.assertEqual(attempts_after_outage, [("pending", 0)])

            # Phase 2: Bring up native Qdrant + real LocalE5, recover via storage_index_worker_host.run_loop
            encoder = hybrid.LocalE5()
            with native_vector(iso.QDRANT_EXE, scratch_root=scratch_parent) as qport:
                hybrid.URL = f"http://127.0.0.1:{qport}"
                with closing(
                    QdrantClient(url=hybrid.URL, trust_env=False, check_compatibility=False, timeout=20)
                ) as qclient:
                    hybrid.ensure_collection(qclient)
                    db_factory = lambda admin=False: db_client.database_session(stage_db, admin=admin)

                    # Simulate 2 outage checks then immediate recovery & indexing of all 3 docs in one run_loop
                    recovery_probes = iter([
                        {"postgres": True, "qdrant": False},
                        {"postgres": True, "qdrant": False},
                        {"postgres": True, "qdrant": True},
                        {"postgres": True, "qdrant": True},
                        {"postgres": True, "qdrant": True},
                        {"postgres": True, "qdrant": True},
                    ])
                    recovery_polls = iter([False, False, False, False, False, False, True])
                    recovery_states = []
                    with (
                        patch.object(host, "STOPFILE", SimpleNamespace(exists=lambda: next(recovery_polls))),
                        patch.object(host, "state", side_effect=lambda s, **kw: recovery_states.append((s, kw))),
                        patch.object(host.time, "sleep"),
                    ):
                        rc_rec = host.run_loop(
                            lambda: ingest_index_worker.process_one(
                                db_factory=db_factory,
                                encoder=encoder,
                                client=qclient,
                            ),
                            backends_ready=lambda: next(recovery_probes),
                        )
                    self.assertEqual(rc_rec, 0)
                    self.assertEqual([s for s, _ in recovery_states][-2:], ["running", "stopped"])
                    self.assertEqual(recovery_states[-2][1]["error_count"], 0)

                    expected_chunks = len(up1.chunks) + len(up2.chunks) + len(up3.chunks)
                    with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                        outbox_rows = db.execute(
                            "SELECT document_id, status, attempts FROM core.ingest_outbox ORDER BY id"
                        ).fetchall()
                    self.assertEqual(
                        outbox_rows,
                        [(doc1_id, "indexed", 1), (doc2_id, "indexed", 1), (doc3_id, "indexed", 1)],
                    )
                    self.assertEqual(qclient.count(col_name, exact=True).count, expected_chunks)

                    # Phase 3: Re-run idempotency (upload replay + worker replay + explicit re-index = 0 duplicates)
                    with iso.pg_connect(pg_port, creds, stage_db, admin=False) as gw:
                        self.assertEqual(persist_upload(gw, "engineer", "engineering", up1, "stg-doc1-idem-key-0001"), doc1_id)
                        self.assertEqual(persist_upload(gw, "engineer", "engineering", up2, "stg-doc2-idem-key-0002"), doc2_id)
                    empty_again = ingest_index_worker.process_one(db_factory=db_factory, encoder=encoder, client=qclient)
                    self.assertEqual(empty_again.result, "empty")

                    # Force explicit re-index of doc2 and prove deterministic UUID5 upsert produces 0 duplicates
                    with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                        db.execute(
                            "UPDATE core.ingest_outbox SET status='pending', attempts=0 WHERE document_id=%s",
                            (doc2_id,),
                        )
                    reindexed = ingest_index_worker.process_one(db_factory=db_factory, encoder=encoder, client=qclient)
                    self.assertEqual(reindexed.result, "indexed")
                    self.assertEqual(reindexed.chunks, len(up2.chunks))
                    self.assertEqual(qclient.count(col_name, exact=True).count, expected_chunks)

                    docs_audit, chunks_audit = via.load_database(stage_db)
                    pts_audit = via.load_vectors(qclient, col_name)
                    audit = via.compare_index(docs_audit, chunks_audit, pts_audit)
                    self.assertEqual(audit["status"], "passed", audit)
                    self.assertEqual(audit["issues"], [])
                    self.assertEqual(audit["warnings"], [])

                    # Phase 4: Text search + source links + original download + OCR page preview via unified_app
                    p2module.database_session = db_factory
                    hybrid.database_session = db_factory
                    p2module._HYBRID_RETRIEVER = hybrid.PolicyHybridRetriever(encoder, qclient)
                    eng_principal = Principal(issuer, "sub-eng-stg-01", p2module.WEB_CLIENT, int(time.time()) + 300)
                    fin_principal = Principal(issuer, "sub-fin-stg-01", p2module.WEB_CLIENT, int(time.time()) + 300)
                    eng_cookie = "stg-eng-" + uuid.uuid4().hex
                    p2module._SESSIONS[eng_cookie] = p2module.WebSession("mock-jwt", int(time.time()) + 300, "csrf-stg", "id-stg")
                    origin = "http://localhost:19139"
                    try:
                        with (
                            patch.object(p2module, "APP_ORIGIN", origin),
                            patch.object(unified_app, "APP_ORIGIN", origin),
                            patch.object(p2module, "COOKIE_SESSION", "factory_unified_session"),
                            patch.object(p2module, "verify_access_token", return_value=eng_principal),
                        ):
                            with TestClient(unified_app.app, base_url=origin, raise_server_exceptions=False) as client:
                                auth_hdr = {"Authorization": "Bearer " + ("A" * 40) + "." + ("B" * 40) + "." + ("C" * 40)}

                                # Search tail fact from oversized Doc 2 (>15k chars)
                                sr2 = client.get("/unified/api/search?q=клапан КП-440М давление 18,5 МПа", headers=auth_hdr)
                                self.assertEqual(sr2.status_code, 200)
                                sr2_items = sr2.json()["items"]
                                self.assertTrue(any(it["id"] == doc2_id for it in sr2_items), sr2_items)

                                # Search scanned OCR Doc 3 + Doc 1
                                sr_pump = client.get("/unified/api/search?q=насос Н-125 давление 25 бар", headers=auth_hdr)
                                self.assertEqual(sr_pump.status_code, 200)
                                sr_pump_ids = {it["id"] for it in sr_pump.json()["items"]}
                                self.assertIn(doc1_id, sr_pump_ids)
                                self.assertIn(doc3_id, sr_pump_ids)

                                # Source links via /unified/api/ask (grounded citation -> clickable doc_id + page)
                                client.cookies.set("factory_unified_session", eng_cookie)
                                with (
                                    patch.object(ai_extension, "status", return_value={"status": "ready"}),
                                    patch.object(
                                        ai_extension,
                                        "generate",
                                        return_value={
                                            "answer": "Для клапана КП-440М давление срабатывания 18,5 МПа [1].",
                                            "citations": [1],
                                            "filtered_lines": 0,
                                        },
                                    ),
                                ):
                                    ask2 = client.post(
                                        "/unified/api/ask",
                                        json={"question": "Какое давление срабатывания клапана КП-440М?"},
                                        headers={"Origin": origin, "X-CSRF-Token": "csrf-stg"},
                                    )
                                self.assertEqual(ask2.status_code, 200)
                                ask2_json = ask2.json()
                                self.assertEqual(ask2_json["mode"], "local_llm_advisory")
                                self.assertEqual(ask2_json["citations"], [1])
                                self.assertEqual(ask2_json["sources"][0]["doc_id"], doc2_id)
                                self.assertEqual(ask2_json["sources"][0]["page"], 1)
                                self.assertTrue(ask2_json["sources"][0]["cited"])
                                client.cookies.clear()

                                # Source document + original download SHA-256 + OCR PDF JPEG preview
                                det2 = client.get(f"/unified/api/document?id={doc2_id}", headers=auth_hdr)
                                self.assertEqual(det2.status_code, 200)
                                self.assertIn("КП-440М", det2.json()["text"])

                                orig2 = client.get(f"/unified/api/uploads/{doc2_id}/original", headers=auth_hdr)
                                self.assertEqual(orig2.status_code, 200)
                                self.assertEqual(
                                    hashlib.sha256(orig2.content).hexdigest(),
                                    hashlib.sha256(doc2_text.encode("utf-8")).hexdigest(),
                                )

                                prev3 = client.get(f"/unified/api/page-preview?id={doc3_id}&page=2", headers=auth_hdr)
                                self.assertEqual(prev3.status_code, 200)
                                self.assertEqual(prev3.headers.get("content-type"), "image/jpeg")
                                self.assertTrue(prev3.content.startswith(b"\xff\xd8"))

                                # Negative 1: Unknown identifier returns 0 noise hits
                                sr_missing = client.get("/unified/api/search?q=Клапан КП-999Z", headers=auth_hdr)
                                self.assertEqual(sr_missing.status_code, 200)
                                self.assertEqual(sr_missing.json()["items"], [])

                                # Negative 2: Unauthorized role (finance) sees 0 hits and 404 on sources
                                with patch.object(p2module, "verify_access_token", return_value=fin_principal):
                                    self.assertEqual(
                                        client.get("/unified/api/search?q=КП-440М", headers=auth_hdr).json()["items"],
                                        [],
                                    )
                                    self.assertEqual(client.get(f"/unified/api/document?id={doc2_id}", headers=auth_hdr).status_code, 404)
                                    self.assertEqual(client.get(f"/unified/api/uploads/{doc2_id}/original", headers=auth_hdr).status_code, 404)
                                    self.assertEqual(client.get(f"/unified/api/page-preview?id={doc3_id}&page=2", headers=auth_hdr).status_code, 404)

                                # Negative 3: Live ACL revoke immediately hides docs without touching Qdrant
                                pts_before_revoke = qclient.count(col_name, exact=True).count
                                with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                                    db.execute("SELECT core.revoke_scope_access('engineer','engineering','stg_revoke')")
                                self.assertEqual(
                                    client.get("/unified/api/search?q=КП-440М", headers=auth_hdr).json()["items"],
                                    [],
                                )
                                self.assertEqual(qclient.count(col_name, exact=True).count, pts_before_revoke)

                    finally:
                        p2module._SESSIONS.pop(eng_cookie, None)

                    # Negative 4: Unindexable punctuation-only chunk exhausts retries cleanly at 5, then recovers via outbox_retry_recovery
                    bad_doc = "upload-" + uuid.uuid4().hex
                    with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                        db.execute(
                            """INSERT INTO core.document(id,title,scope_id,doc_type,source_sha256,source_kind)
                               VALUES (%s,'!!!','engineering','uploaded',%s,'postgres_ingest')""",
                            (bad_doc, "0" * 64),
                        )
                        db.execute(
                            """INSERT INTO core.document_chunk(id,document_id,body,page_no,extraction_method)
                               VALUES (%s,%s,'!!! --- ...',1,'text')""",
                            (bad_doc + "-chunk-001", bad_doc),
                        )
                        bad_job_id = db.execute(
                            "INSERT INTO core.ingest_outbox(document_id,event_kind) VALUES (%s,'index') RETURNING id",
                            (bad_doc,),
                        ).fetchone()[0]

                    with self.assertLogs("core06.ingest_index_worker", level="WARNING") as job_logs:
                        for _ in range(5):
                            out_bad = ingest_index_worker.process_one(db_factory=db_factory, encoder=encoder, client=qclient)
                            self.assertEqual(out_bad.result, "failed")
                            with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                                db.execute(
                                    "UPDATE core.ingest_outbox SET updated_at = now() - interval '10 seconds' WHERE id=%s",
                                    (bad_job_id,),
                                )
                    self.assertTrue(any("EMPTY_SPARSE_CHUNK" in line for line in job_logs.output), job_logs.output)
                    # 6th poll must NOT keep failing infinitely.
                    self.assertEqual(
                        ingest_index_worker.process_one(db_factory=db_factory, encoder=encoder, client=qclient).result,
                        "empty",
                    )
                    prev_snap = outbox_retry_recovery.preview(stage_db, [bad_job_id])
                    self.assertEqual(prev_snap["selected"][0]["action"], "eligible")

                    # Repair source text in isolated DB, requeue via outbox_retry_recovery, re-index cleanly!
                    with iso.pg_connect(pg_port, creds, stage_db, admin=True) as db:
                        db.execute(
                            "UPDATE core.document SET title='Восстановленный документ' WHERE id=%s",
                            (bad_doc,),
                        )
                        db.execute(
                            "UPDATE core.document_chunk SET body='Восстановленный текст проверки ВД-100' WHERE document_id=%s",
                            (bad_doc,),
                        )
                    requeued = outbox_retry_recovery.apply(stage_db, [bad_job_id])
                    self.assertEqual(requeued["status"], "passed")
                    self.assertEqual(requeued["count"], 1)
                    fixed_out = ingest_index_worker.process_one(db_factory=db_factory, encoder=encoder, client=qclient)
                    self.assertEqual(fixed_out.result, "indexed")
                    self.assertEqual(fixed_out.chunks, 1)

                    # Final PG/Qdrant metadata reconciliation: 0 issues, 0 warnings, 0 duplicates
                    docs_end, chunks_end = via.load_database(stage_db)
                    pts_end = via.load_vectors(qclient, col_name)
                    final_audit = via.compare_index(docs_end, chunks_end, pts_end)
                    self.assertEqual(final_audit["status"], "passed", final_audit)
                    self.assertEqual(final_audit["counts"]["vector_points"], expected_chunks + 1)
        finally:
            hybrid.COLLECTION = old_collection
            hybrid.URL = old_url
            p2module.database_session = old_p2_db
            hybrid.database_session = old_hybrid_db
            p2module._HYBRID_RETRIEVER = old_retriever
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            _stop_ephemeral_pg(pgdata)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
