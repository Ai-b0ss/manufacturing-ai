"""Temporary real PostgreSQL 18 test for v0.6 upload permission and atomic RLS.

Creates a uniquely named DB, uses synthetic text only and drops it in finally.
Does not change the live factory_core_v06 database or active Gateway services.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import uuid

import psycopg
from psycopg import sql

from core06.ingest_service import prepare_upload, persist_upload
from core06.db_client import _creds_dir, _pg_port

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "factory_ingest_test_" + uuid.uuid4().hex[:12]


def connect(database, admin):
    return psycopg.connect(
        host="127.0.0.1", port=_pg_port(), dbname=database,
        user="factory_pg_admin" if admin else "factory_gateway",
        password=(_creds_dir() / ("admin-password.txt" if admin else "gateway-password.txt")).read_text(encoding="ascii"),
        connect_timeout=6, autocommit=True,
    )


def test():
    with connect("postgres", True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    try:
        with connect(TEST_DB, True) as c:
            for path in (ROOT / "core06/schema.sql", ROOT / "core06/ingestion_v1.sql", ROOT / "core06/ingestion_v2.sql"):
                c.execute(path.read_text(encoding="utf-8-sig"))
            c.execute("INSERT INTO core.subject(id) VALUES('ingest_tester'),('read_only')")
            c.execute("INSERT INTO core.scope(id) VALUES('ingest_lab')")
            c.execute("""INSERT INTO core.scope_grant(subject_id,scope_id)
                        VALUES('ingest_tester','ingest_lab'),('read_only','ingest_lab')""")
            c.execute("""INSERT INTO core.scope_write_grant(subject_id,scope_id)
                        VALUES('ingest_tester','ingest_lab')""")
        with connect(TEST_DB, False) as gateway:
            with gateway.transaction():
                gateway.execute("SELECT set_config('factory.actor_id','read_only',true)")
                assert gateway.execute("SELECT core.can_write_scope('ingest_lab')").fetchone() == (False,)
                try:
                    with gateway.transaction():
                        gateway.execute("""INSERT INTO core.document(id,title,scope_id)
                                           VALUES('direct-write','forbidden','ingest_lab')""")
                    raise AssertionError("gateway had direct INSERT rights")
                except psycopg.errors.InsufficientPrivilege:
                    pass
            prepared = prepare_upload(
                "test.txt", "PostgreSQL verified ingest", b"Secure manufacturing reference DOC-789 year2026",
                extractor=lambda p: {"pages":[{"page":1,"text":p.read_text(encoding="utf-8"),"extraction_method":"text"}], "note":""},
            )
            try:
                persist_upload(gateway, "read_only", "ingest_lab", prepared, "readonlyattempt-00001")
                raise AssertionError("read-only actor accepted for upload")
            except PermissionError:
                pass
            docid = persist_upload(gateway, "ingest_tester", "ingest_lab", prepared, "isolated-test-key-0001")
            assert docid == prepared.id
            replay_id = persist_upload(gateway, "ingest_tester", "ingest_lab", prepared, "isolated-test-key-0001")
            assert replay_id == docid
            with gateway.transaction():
                gateway.execute("SELECT set_config('factory.actor_id','ingest_tester',true)")
                rec = gateway.execute(
                    "SELECT original_bytes,content_sha256 FROM core.document_blob WHERE document_id=%s",
                    (docid,),
                ).fetchone()
                assert rec[0] == prepared.original_bytes
                assert rec[1] == hashlib.sha256(rec[0]).hexdigest()
                chunks = gateway.execute(
                    "SELECT body FROM core.document_chunk WHERE document_id=%s",
                    (docid,),
                ).fetchall()
                assert len(chunks) == 1 and "DOC-789" in chunks[0][0]
                assert gateway.execute("SELECT count(*) FROM core.document WHERE id=%s", (docid,)).fetchone()[0] == 1
            with gateway.transaction():
                gateway.execute("SELECT set_config('factory.actor_id','read_only',true)")
                assert gateway.execute("SELECT count(*) FROM core.document_blob WHERE document_id=%s", (docid,)).fetchone() == (1,)
                assert gateway.execute("SELECT core.can_write_scope('ingest_lab')").fetchone() == (False,)
            with gateway.transaction():
                gateway.execute("SELECT set_config('factory.actor_id','ingest_tester',true)")
                count_before = gateway.execute("SELECT count(*) FROM core.document").fetchone()[0]
                try:
                    with gateway.transaction():
                        gateway.execute(
                            "SELECT core.ingest_document_once(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                            ("invalid-sha-000000001", "upload-" + uuid.uuid4().hex, "ingest_lab", "invalid sha", "test.txt",
                             "text/plain", b"content", "0"*64,
                             json.dumps([{"page":1,"text":"content","extraction_method":"text"}])),
                        )
                    raise AssertionError("invalid sha was accepted")
                except psycopg.errors.InvalidParameterValue:
                    pass
                assert gateway.execute("SELECT count(*) FROM core.document").fetchone()[0] == count_before
                # Failure after document + original bytes insertion MUST roll back all rows.
                malformed_doc = "upload-" + uuid.uuid4().hex
                bad_bytes = b"synthetic post-insert rollback"
                try:
                    with gateway.transaction():
                        gateway.execute(
                            "SELECT core.ingest_document_once(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                            ("postinsert-rollback-0001", malformed_doc, "ingest_lab",
                             "Rollback test", "rollback.txt", "text/plain", bad_bytes,
                             hashlib.sha256(bad_bytes).hexdigest(),
                             json.dumps([{"page":1,"text":"","extraction_method":"text"}])),
                        )
                    raise AssertionError("empty extracted text was accepted")
                except psycopg.errors.InvalidParameterValue:
                    pass
                assert gateway.execute("SELECT count(*) FROM core.document").fetchone()[0] == count_before
                assert gateway.execute(
                    "SELECT count(*) FROM core.document_blob WHERE document_id=%s",
                    (malformed_doc,),
                ).fetchone() == (0,)
        with connect(TEST_DB, True) as admin:
            assert admin.execute("SELECT count(*) FROM core.ingest_outbox WHERE document_id=%s", (docid,)).fetchone() == (1,)
            assert admin.execute("SELECT count(*) FROM core.audit_event WHERE target=%s", (docid,)).fetchone() == (1,)
            admin.execute("SELECT core.revoke_scope_access('ingest_tester','ingest_lab','ingest_test')")
        with connect(TEST_DB, False) as gateway:
            with gateway.transaction():
                gateway.execute("SELECT set_config('factory.actor_id','ingest_tester',true)")
                assert gateway.execute("SELECT count(*) FROM core.document WHERE id=%s", (docid,)).fetchone() == (0,)
                assert gateway.execute("SELECT count(*) FROM core.document_blob WHERE document_id=%s", (docid,)).fetchone() == (0,)
                assert gateway.execute("SELECT count(*) FROM core.document_chunk WHERE document_id=%s", (docid,)).fetchone() == (0,)
                assert gateway.execute("SELECT core.can_write_scope('ingest_lab')").fetchone() == (False,)
            try:
                persist_upload(gateway, "ingest_tester", "ingest_lab", prepared, "isolated-test-key-0001")
                raise AssertionError("revoked actor still accepted")
            except PermissionError:
                pass
        print("PASS isolated real PostgreSQL: migration, least-privilege, sha, atomic rollback, OCR text chunks, outbox, revoke")
    finally:
        with connect("postgres", True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))


if __name__ == "__main__":
    test()
