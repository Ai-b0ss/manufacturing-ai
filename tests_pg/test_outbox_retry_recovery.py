"""Recovery tests: pure guards and real temporary PostgreSQL row locking.

No production rows are modified. Actual DB writes stay in a private uniquely
named fixture database, removed even after assertion or SQL errors.
"""
from __future__ import annotations
from contextlib import redirect_stderr,redirect_stdout
from io import StringIO
import json
import secrets
import unittest
from unittest.mock import patch
from pathlib import Path

import psycopg
from psycopg import sql

from core06.db_client import _creds_dir, _pg_port
from tools.outbox_retry_recovery import (
    Job, parse_ids, eligibility, preview, apply, main, LIVE,
)

ROOT=Path(__file__).resolve().parents[1]
STAGE="factory_ingest_stage_"+secrets.token_hex(6)


def connect(name="postgres"):
    return psycopg.connect(host="127.0.0.1",port=_pg_port(),dbname=name,
        user="factory_pg_admin",
        password=(_creds_dir()/"admin-password.txt").read_text("ascii"),
        autocommit=True,connect_timeout=7)


def make_doc(db,n,deleted=False,kind="postgres_ingest"):
    id_="upload-"+n.zfill(32)
    db.execute("""INSERT INTO core.document(id,title,scope_id,doc_type,source_sha256,source_kind)
       VALUES(%s,'Synthetic Requeue Test','engineering','uploaded',%s,%s)""",
       (id_,"0"*64,kind))
    if deleted:
        db.execute("UPDATE core.document SET deleted_at=now() WHERE id=%s",(id_,))
    return id_


def add_job(db,document,event,status,attempts,age=30,lease="stale"):
    return db.execute("""INSERT INTO core.ingest_outbox
       (document_id,event_kind,status,attempts,updated_at,lease_token)
       VALUES(%s,%s,%s,%s,now()-(%s * interval '1 second'),%s)
       RETURNING id""",(document,event,status,attempts,age,lease)).fetchone()[0]


class TestParseAndEligibility(unittest.TestCase):
    def test_numeric_id_list(self):
        self.assertEqual(parse_ids("3,15,209"),[3,15,209])
        self.assertEqual(parse_ids(None),[])

    def test_reject_unsafe_values(self):
        for name in ("","0","-1","1,2,1","1, 2","1;DROP","99999999999999999999",
                     ",".join(str(x) for x in range(1,22))):
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_ids(name)

    def test_eligible_index(self):
        job=Job(12,"index","failed",5,False,"postgres_ingest",30)
        self.assertEqual(eligibility(job),"eligible")

    def test_eligible_delete(self):
        job=Job(12,"delete","failed",5,True,"postgres_ingest",30)
        self.assertEqual(eligibility(job),"eligible")

    def test_reject_auto_retry_not_exhausted(self):
        job=Job(12,"index","failed",4,False,"postgres_ingest",30)
        self.assertEqual(eligibility(job),"AUTOMATIC_RETRY_AVAILABLE")

    def test_reject_processing_job(self):
        job=Job(12,"index","processing",5,False,"postgres_ingest",3600)
        self.assertEqual(eligibility(job),"NOT_FAILED")

    def test_reject_deleted_index_job(self):
        job=Job(12,"index","failed",5,True,"postgres_ingest",30)
        self.assertEqual(eligibility(job),"INDEX_DOCUMENT_DELETED")

    def test_reject_active_delete_job(self):
        job=Job(12,"delete","failed",5,False,"postgres_ingest",30)
        self.assertEqual(eligibility(job),"DELETE_DOCUMENT_ACTIVE")

    def test_backoff_must_expire(self):
        job=Job(12,"delete","failed",5,True,"postgres_ingest",1)
        self.assertEqual(eligibility(job),"BACKOFF_STILL_ACTIVE")

    def test_cli_requires_live_flags_and_exact_confirm(self):
        cases=[
            ["--database",LIVE],
            ["--database",LIVE,"--apply","--job-ids","12",
             "--confirm-database",LIVE],
            ["--database",STAGE,"--apply","--job-ids","12"],
            ["--database",STAGE,"--apply","--confirm-database",STAGE],
        ]
        for args in cases:
            with self.subTest(args=args):
                with redirect_stderr(StringIO()),self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code,2)

    def test_production_direct_apply_rejects_vector_down(self):
        with patch("tools.outbox_retry_recovery.vector_ready",return_value=False):
            with self.assertRaisesRegex(RuntimeError,"VECTOR_NOT_READY"):
                apply(LIVE,[1])


class TestStageDatabase(unittest.TestCase):
    def test_atomic_retry_recovery(self):
        self.assertTrue(STAGE.startswith("factory_ingest_stage_"))
        self.assertEqual(len(STAGE),33)
        created=False
        try:
            with connect() as admin:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(STAGE)))
            created=True
            with connect(STAGE) as db:
                for filename in ("schema.sql","ingestion_v1.sql","ingestion_v2.sql",
                                 "ingestion_v3_purge.sql"):
                    db.execute((ROOT/"core06"/filename).read_text("utf-8-sig"))
                db.execute("INSERT INTO core.scope(id) VALUES('engineering')")
                valid=add_job(db,make_doc(db,"1"),"index","failed",5)
                pending=add_job(db,make_doc(db,"2"),"index","pending",0)
                deleted=add_job(db,make_doc(db,"3",deleted=True),"delete","failed",5)
                bad=add_job(db,make_doc(db,"4",deleted=True),"index","failed",5)
                stale=add_job(db,make_doc(db,"5"),"index","processing",5,700)
                automatic=add_job(db,make_doc(db,"6"),"index","failed",4)
                locked_doc=make_doc(db,"7")
                locked_job=add_job(db,locked_doc,"index","failed",5)
                cli_job=add_job(db,make_doc(db,"8"),"index","failed",5)
            snap=preview(STAGE,[valid,deleted,pending,bad,stale,automatic,999999])
            self.assertEqual(snap["status"],"passed")
            self.assertFalse(snap["mutated"])
            expected={
                valid:"eligible",deleted:"eligible",pending:"NOT_FAILED",
                bad:"INDEX_DOCUMENT_DELETED",stale:"NOT_FAILED",
                automatic:"AUTOMATIC_RETRY_AVAILABLE",999999:"JOB_NOT_FOUND",
            }
            self.assertEqual({x["job_id"]:x["action"] for x in snap["selected"]},expected)
            self.assertTrue(any(x["count"]>=2 for x in snap["exhausted_counts"]))
            # A mixed batch MUST NOT update any selected row or write audit rows.
            with self.assertRaisesRegex(ValueError,"INELIGIBLE"):
                apply(STAGE,[valid,pending])
            with connect(STAGE) as db:
                self.assertEqual(db.execute(
                    "SELECT status,attempts FROM core.ingest_outbox WHERE id=%s",
                    (valid,)).fetchone(),("failed",5))
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM core.audit_event WHERE action='requeue_exhausted_outbox'",
                    ).fetchone(),(0,))
            change=apply(STAGE,[valid,deleted])
            self.assertEqual(change["status"],"passed")
            self.assertEqual(change["count"],2)
            with connect(STAGE) as db:
                self.assertEqual(db.execute(
                    """SELECT status,attempts,lease_token
                       FROM core.ingest_outbox WHERE id=%s""",
                    (valid,)).fetchone(),("pending",0,None))
                self.assertEqual(db.execute(
                    """SELECT status,attempts,lease_token
                       FROM core.ingest_outbox WHERE id=%s""",
                    (deleted,)).fetchone(),("pending",0,None))
                self.assertEqual(db.execute(
                    "SELECT status,attempts FROM core.ingest_outbox WHERE id=%s",
                    (bad,)).fetchone(),("failed",5))
                audit_rows=db.execute(
                    """SELECT actor,action,target,outcome FROM core.audit_event
                       WHERE action='requeue_exhausted_outbox' ORDER BY id"""
                ).fetchall()
                self.assertEqual(len(audit_rows),2)
                self.assertTrue(all(r[0]=="factory_pg_admin" and r[3]=="requeued_pending" for r in audit_rows))
                self.assertIn(f"job-{valid}",audit_rows[0][2])
                self.assertIn(f"job-{deleted}",audit_rows[1][2])
            # A concurrent delete holds the document row: requeue must
            # fail immediately and leave the exhausted job unchanged.
            with connect(STAGE) as blocking:
                with blocking.transaction():
                    blocking.execute(
                        "SELECT id FROM core.document WHERE id=%s FOR UPDATE",
                        (locked_doc,))
                    with self.assertRaises(psycopg.errors.LockNotAvailable):
                        apply(STAGE,[locked_job])
            with connect(STAGE) as db:
                self.assertEqual(db.execute(
                    "SELECT status,attempts FROM core.ingest_outbox WHERE id=%s",
                    (locked_job,)).fetchone(),("failed",5))
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM core.audit_event WHERE action='requeue_exhausted_outbox'",
                    ).fetchone(),(2,))
            # Real CLI integration: precise confirmation and atomic apply
            # modify ONLY a selected row in the owned scratch DB.
            stdout=StringIO()
            with redirect_stdout(stdout):
                rc=main(["--database",STAGE,"--job-ids",str(cli_job),
                         "--apply","--confirm-database",STAGE])
            self.assertEqual(rc,0)
            self.assertEqual(json.loads(stdout.getvalue())["count"],1)
            with connect(STAGE) as db:
                self.assertEqual(db.execute(
                    "SELECT status,attempts FROM core.ingest_outbox WHERE id=%s",
                    (cli_job,)).fetchone(),("pending",0))
                self.assertEqual(db.execute(
                    "SELECT status,attempts FROM core.ingest_outbox WHERE id=%s",
                    (locked_job,)).fetchone(),("failed",5))
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM core.audit_event WHERE action='requeue_exhausted_outbox'",
                    ).fetchone(),(3,))
            # A completed requeue must not be reusable by accident.
            with self.assertRaises(ValueError):
                apply(STAGE,[valid])
            print("PASS isolated PostgreSQL outbox recovery: preview, CLI apply, atomic requeue, lock contention, no double-use")
        finally:
            if created:
                with connect() as admin:
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(STAGE)))
                print("CLEANUP owned ephemeral outbox PostgreSQL database dropped")


if __name__=="__main__":
    unittest.main()
