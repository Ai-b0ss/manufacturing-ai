"""Real PostgreSQL concurrency: idempotent double-submit and revoke between reads.

Unique disposable DB, synthetic bytes. Does not modify production database.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import uuid

import psycopg
from psycopg import sql

from core06.db_client import _creds_dir, _pg_port
from core06.ingest_service import prepare_upload, persist_upload

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "factory_ingest_race_" + uuid.uuid4().hex[:12]


def connect(admin=False, database=TEST_DB):
    return psycopg.connect(
        host="127.0.0.1", port=_pg_port(), dbname=database,
        user="factory_pg_admin" if admin else "factory_gateway",
        password=(_creds_dir() / ("admin-password.txt" if admin else "gateway-password.txt")).read_text("ascii"),
        connect_timeout=8, autocommit=True,
    )


def main():
    with connect(True, "postgres") as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    try:
        with connect(True) as db:
            for f in ("schema.sql", "ingestion_v1.sql", "ingestion_v2.sql"):
                db.execute((ROOT / "core06" / f).read_text("utf-8-sig"))
            db.execute("INSERT INTO core.subject(id) VALUES('ingest_tester')")
            db.execute("INSERT INTO core.scope(id) VALUES('ingest_scope')")
            db.execute("INSERT INTO core.scope_grant VALUES('ingest_tester','ingest_scope',now())")
            db.execute("INSERT INTO core.scope_write_grant VALUES('ingest_tester','ingest_scope')")

        gate = Barrier(2)
        def writer():
            document = prepare_upload(
                "fixture.txt", "Synthetic fixture", b"Fixture X-481 tolerance and assembly procedure"
            )
            gate.wait(timeout=6)
            with connect() as db:
                return persist_upload(
                    db, "ingest_tester", "ingest_scope", document,
                    "simultaneous-request-key-0001",
                )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(writer)
            second = pool.submit(writer)
            doc_ids = [first.result(timeout=16), second.result(timeout=16)]
        assert doc_ids[0] == doc_ids[1], doc_ids
        doc_id = doc_ids[0]
        with connect(True) as db:
            assert db.execute("SELECT count(*) FROM core.document WHERE id=%s",(doc_id,)).fetchone()==(1,)
            assert db.execute("SELECT count(*) FROM core.ingest_request WHERE document_id=%s",(doc_id,)).fetchone()==(1,)
            assert db.execute("SELECT count(*) FROM core.ingest_outbox WHERE document_id=%s",(doc_id,)).fetchone()==(1,)
            assert db.execute("SELECT count(*) FROM core.audit_event WHERE target=%s",(doc_id,)).fetchone()==(1,)

        with connect() as reader:
            with reader.transaction():  # READ COMMITTED: each statement sees fresh policy.
                reader.execute("SELECT set_config('factory.actor_id','ingest_tester',true)")
                before = reader.execute(
                    "SELECT count(*) FROM core.document_blob WHERE document_id=%s",(doc_id,)
                ).fetchone()[0]
                assert before == 1
                with connect(True) as admin:
                    admin.execute("SELECT core.revoke_scope_access('ingest_tester','ingest_scope','race_test')")
                after = reader.execute(
                    "SELECT count(*) FROM core.document_blob WHERE document_id=%s",(doc_id,)
                ).fetchone()[0]
                assert after == 0, "existing transaction read stale revoked source"
        print("PASS real PostgreSQL: concurrent identical uploads single document, revoke blocks next read")
    finally:
        with connect(True, "postgres") as db:
            db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))


if __name__ == "__main__":
    main()
