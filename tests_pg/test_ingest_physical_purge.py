"""Real isolated PostgreSQL+Qdrant physical purge acceptance.

Tests irreversible deletion ONLY on synthetic fixtures in a disposable DB.
Never deletes or alters active documents in factory_core_v06.
"""
from __future__ import annotations
from contextlib import ExitStack, closing
from concurrent.futures import ThreadPoolExecutor
import argparse
import os
from pathlib import Path
import threading
import time
import uuid

import psycopg
from psycopg import sql
from qdrant_client import QdrantClient, models

from core06.db_client import _creds_dir, _pg_port
from core06.ingest_service import prepare_upload,persist_upload
from core06.ingest_index_worker import process_one
from core06 import hybrid_retrieval as hybrid
from tests_pg.native_vector_fixture import native_vector
from tools.purge_deleted_upload import PurgeBlocked,purge_one

ROOT=Path(__file__).resolve().parents[1]
TEST_DB="factory_ingest_purge_"+uuid.uuid4().hex[:12]
TEST_COLLECTION="factory_ingest_purge_"+uuid.uuid4().hex[:12]


def db_session(admin=False,database=TEST_DB):
    return psycopg.connect(
        host="127.0.0.1",port=_pg_port(),dbname=database,
        user="factory_pg_admin" if admin else "factory_gateway",
        password=(_creds_dir()/("admin-password.txt" if admin else "gateway-password.txt")).read_text("ascii"),
        autocommit=True,connect_timeout=8,
    )


def assert_blocked(fn,reason):
    try:fn()
    except PurgeBlocked as exc:
        assert reason in str(exc),str(exc)
        return
    raise AssertionError("expected purge denial: "+reason)


def main(qdrant_exe=None, scratch_root=None):
    old_collection=hybrid.COLLECTION
    old_flag=os.environ.get("FACTORY_V06_PURGE_ENABLED")
    created_db=False
    client=None
    stack=ExitStack()
    with db_session(True,"postgres") as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
        created_db=True
    try:
        with db_session(True) as db:
            for name in ("schema.sql","ingestion_v1.sql","ingestion_v2.sql","ingestion_v3_purge.sql"):
                db.execute((ROOT/"core06"/name).read_text("utf-8-sig"))
            db.execute("INSERT INTO core.subject(id) VALUES('ingest_tester')")
            db.execute("INSERT INTO core.scope(id) VALUES('ingest_scope')")
            db.execute("INSERT INTO core.scope_grant VALUES('ingest_tester','ingest_scope',now())")
            db.execute("INSERT INTO core.scope_write_grant VALUES('ingest_tester','ingest_scope')")
        hybrid.COLLECTION=TEST_COLLECTION
        if qdrant_exe is not None:
            vport=stack.enter_context(native_vector(Path(qdrant_exe),scratch_root=scratch_root))
            client=stack.enter_context(closing(QdrantClient(
                url=f"http://127.0.0.1:{vport}",timeout=15,trust_env=False,check_compatibility=False,
            )))
        else:
            client=stack.enter_context(closing(hybrid.qdrant_client()))
        hybrid.ensure_collection(client)

        document=prepare_upload(
            "source.txt","Synthetic deleted upload",
            b"Purge fixture DOC-881 assembly STAGE-441 no real personal data",
        )
        key="purge-fixture-request-0001"
        with db_session() as gateway:
            persist_upload(gateway,"ingest_tester","ingest_scope",document,key)
            with gateway.transaction():
                gateway.execute("SELECT set_config('factory.actor_id','ingest_tester',true)")
                try:
                    with gateway.transaction():
                        gateway.execute(
                            "SELECT core.purge_deleted_upload(%s)",(document.id,),
                        )
                    raise AssertionError("Gateway could run restricted purge")
                except psycopg.errors.InsufficientPrivilege:
                    pass

        docid=document.id
        # A deliberately nonzero 384-dim synthetic vector: Qdrant is real,
        # no external model required for this deletion-only acceptance.
        point_id=str(uuid.uuid5(hybrid.NAMESPACE,docid+"-chunk-001"))
        client.upsert(
            collection_name=TEST_COLLECTION,
            points=[models.PointStruct(
                id=point_id,
                vector={"dense":[0.1]*384,
                        "sparse":hybrid.sparse_tokens(document.chunks[0]["text"])},
                payload={
                    "tenant_id":hybrid.TENANT,"document_id":docid,
                    "chunk_id":docid+"-chunk-001","acl_scope_id":"ingest_scope",
                },
            )],wait=True,
        )
        kwargs={"db_factory":db_session,"client":client}
        os.environ.pop("FACTORY_V06_PURGE_ENABLED",None)
        assert_blocked(lambda:purge_one(docid,execute=True,**kwargs),"PHYSICAL_PURGE_NOT_ENABLED")
        initial=purge_one(docid,**kwargs)
        assert not initial.eligible and "NOT_LOGICALLY_DELETED" in initial.blockers
        assert initial.referenced_qdrant_points==1
        os.environ["FACTORY_V06_PURGE_ENABLED"]="1"
        assert_blocked(lambda:purge_one(docid,execute=True,**kwargs),"PURGE_PRECONDITIONS_FAILED")

        with db_session() as gateway:
            with gateway.transaction():
                gateway.execute("SELECT set_config('factory.actor_id','ingest_tester',true)")
                assert gateway.execute("SELECT core.delete_uploaded_document(%s)",(docid,)).fetchone()==(True,)
        deleted=purge_one(docid,**kwargs)
        assert "QDRANT_DELETE_NOT_ACKNOWLEDGED" in deleted.blockers
        assert "RETENTION_24H_NOT_MET" in deleted.blockers
        assert "QDRANT_POINTS_STILL_EXIST" in deleted.blockers

        result=process_one(db_factory=db_session,client=client)
        assert result.result=="deleted" and result.document_id==docid,result
        assert client.retrieve(TEST_COLLECTION,[point_id],with_payload=False)==[]
        too_early=purge_one(docid,**kwargs)
        assert too_early.blockers==("RETENTION_24H_NOT_MET",),too_early
        assert_blocked(lambda:purge_one(docid,execute=True,**kwargs),"PURGE_PRECONDITIONS_FAILED")

        # Simulate a retention-expired fixture. ONLY isolated test DB is altered.
        with db_session(True) as admin:
            admin.execute(
                "UPDATE core.document SET deleted_at=now()-interval '48 hours' WHERE id=%s",
                (docid,),
            )
        assert purge_one(docid,**kwargs).eligible

        # Operator's purge must wait for the same advisory lock as a live worker.
        holding=db_session(True)
        holding.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("purge-upload:"+docid,),
        )
        started=threading.Event()
        def execute():
            started.set()
            return purge_one(docid,execute=True,**kwargs)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(execute)
                assert started.wait(4)
                time.sleep(.35)
                assert not pending.done(),"purge bypassed a worker's advisory lock"
                assert holding.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    ("purge-upload:"+docid,),
                ).fetchone()==(True,)
                final=pending.result(timeout=12)
            assert final.executed and final.eligible,final
        finally:
            holding.close()

        with db_session(True) as admin:
            for table in ("document","document_chunk","document_blob","ingest_outbox"):
                column="id" if table=="document" else "document_id"
                assert admin.execute(
                    "SELECT count(*) FROM core."+table+" WHERE "+column+"=%s",(docid,)
                ).fetchone()==(0,),table
            tombstone=admin.execute(
                "SELECT document_id FROM core.ingest_request WHERE actor_id='ingest_tester' AND request_key=%s",
                (key,),
            ).fetchone()
            assert tombstone==(None,),tombstone
            assert admin.execute(
                "SELECT count(*) FROM core.audit_event WHERE action='purge_uploaded_document' AND target=%s",
                (docid,),
            ).fetchone()==(1,)
        # Reusing the historical request key cannot recreate a deleted document.
        duplicate=prepare_upload("source.txt","Synthetic deleted upload",document.original_bytes)
        with db_session() as gateway:
            try:
                persist_upload(gateway,"ingest_tester","ingest_scope",duplicate,key)
                raise AssertionError("Purged idempotency key was accepted")
            except psycopg.errors.InsufficientPrivilege:
                pass
        print("PASS real PG + Qdrant: dry-run, 24h retention, no points, worker lock, purge, durable idempotency tombstone")
    finally:
        hybrid.COLLECTION=old_collection
        if old_flag is None:os.environ.pop("FACTORY_V06_PURGE_ENABLED",None)
        else:os.environ["FACTORY_V06_PURGE_ENABLED"]=old_flag
        cleanup_issues=[]
        try:
            if client is not None:
                cleared=False
                for attempt in range(5):
                    try:
                        if client.collection_exists(TEST_COLLECTION):
                            client.delete_collection(TEST_COLLECTION)
                        if not client.collection_exists(TEST_COLLECTION):
                            cleared=True
                            break
                    except Exception:pass
                    time.sleep(.4*(attempt+1))
                if not cleared:
                    cleanup_issues.append("PURGE_TEST_QDRANT_COLLECTION_CLEANUP_FAILED")
        finally:
            stack.close()
            if created_db:
                try:
                    with db_session(True,"postgres") as admin:
                        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))
                except Exception:
                    cleanup_issues.append("PURGE_TEST_POSTGRES_DATABASE_CLEANUP_FAILED")
        if cleanup_issues:
            raise RuntimeError(",".join(cleanup_issues))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--qdrant-exe",type=Path,default=None)
    parser.add_argument("--scratch-root",type=Path,default=None)
    args=parser.parse_args()
    main(args.qdrant_exe,args.scratch_root)
