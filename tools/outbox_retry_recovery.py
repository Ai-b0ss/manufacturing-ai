"""Read-only by default; guarded, atomic requeue of exhausted upload jobs.

Production application requires explicit operator flags, exact IDs, a ready
Qdrant collection and fresh row-level preconditions. No stored text, source
documents, names or connection errors are returned in operator output.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

from core06.db_client import _creds_dir, _pg_port

LIVE = "factory_core_v06"
STAGE = re.compile(r"factory_ingest_stage_[0-9a-f]{12}\Z")
MAX_SELECTED = 20

@dataclass(frozen=True)
class Job:
    id: int
    event: str
    status: str
    attempts: int
    deleted: bool
    source: str
    age_seconds: int

def parse_ids(value: str | None) -> list[int]:
    if value is None:return []
    raw=value.split(",")
    if not (1<=len(raw)<=MAX_SELECTED) or any(not re.fullmatch(r"[1-9][0-9]{0,17}",x) for x in raw):
        raise ValueError("INVALID_JOB_ID_LIST")
    values=[int(x) for x in raw]
    if len(set(values))!=len(values):raise ValueError("DUPLICATE_JOB_IDS")
    return values

def eligibility(row: Job) -> str:
    if row.source!="postgres_ingest":return "NOT_PROTECTED_UPLOAD"
    if row.event not in ("index","delete"):return "UNSUPPORTED_EVENT"
    if row.status!="failed":return "NOT_FAILED"
    if row.attempts<5:return "AUTOMATIC_RETRY_AVAILABLE"
    if row.age_seconds<5:return "BACKOFF_STILL_ACTIVE"
    if row.event=="index" and row.deleted:return "INDEX_DOCUMENT_DELETED"
    if row.event=="delete" and not row.deleted:return "DELETE_DOCUMENT_ACTIVE"
    return "eligible"

def connect(database,readonly):
    import psycopg
    options="-c statement_timeout=20000 -c lock_timeout=3000"
    if readonly:options+=" -c default_transaction_read_only=on"
    return psycopg.connect(
        host="127.0.0.1",port=_pg_port(),dbname=database,
        user="factory_pg_admin",
        password=(_creds_dir()/"admin-password.txt").read_text("ascii"),
        connect_timeout=8,options=options,autocommit=True)

LOOKUP = """SELECT o.id,o.event_kind,o.status,o.attempts,
            (d.deleted_at IS NOT NULL),d.source_kind,
            GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (now()-o.updated_at)))::integer)
            FROM core.ingest_outbox o JOIN core.document d ON d.id=o.document_id
            WHERE o.id=ANY(%s) ORDER BY o.id"""

def fetch_jobs(db, ids, *, locking=False):
    # Also lock the document row: delete_uploaded_document takes the same
    # document lock before flipping deleted_at, so eligibility cannot change
    # between our verification and the atomic queue update.
    query=LOOKUP+(" FOR UPDATE OF o,d NOWAIT" if locking else "")
    return [Job(*rec) for rec in db.execute(query,(ids,)).fetchall()]

def summary(db):
    rows=db.execute("""SELECT status,event_kind,count(*)
                       FROM core.ingest_outbox WHERE attempts>=5
                       GROUP BY status,event_kind ORDER BY status,event_kind""").fetchall()
    return [{"status":status,"event":event,"count":n} for status,event,n in rows]

def preview(database, ids):
    with connect(database,readonly=True) as db:
        with db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if db.execute("SHOW transaction_read_only").fetchone()[0]!="on":
                raise RuntimeError("NOT_READ_ONLY")
            counts=summary(db)
            rows={job.id:job for job in fetch_jobs(db,ids)} if ids else {}
            selected=[]
            for n in ids:
                item=rows.get(n)
                selected.append({
                    "job_id":n,"event":item.event if item else None,
                    "attempts":item.attempts if item else None,
                    "action":eligibility(item) if item else "JOB_NOT_FOUND"})
    return {"mode":"preview","status":"passed","selected":selected,
            "exhausted_counts":counts,"mutated":False}

def vector_ready():
    from core06 import hybrid_retrieval as hybrid
    client=hybrid.qdrant_client(timeout=3)
    try:
        info=client.get_collection(hybrid.COLLECTION)
        vectors=info.config.params.vectors
        return (getattr(info.status, "value", None) in {"green","yellow"}
                and isinstance(vectors,dict) and "dense" in vectors
                and vectors["dense"].size==hybrid.VECTOR_SIZE
                and "sparse" in (info.config.params.sparse_vectors or {}))
    finally:
        client.close()

def apply(database,ids):
    if not ids:raise ValueError("NO_EXPLICIT_IDS")
    # Internal guard cannot be bypassed through an injected readiness flag.
    if database==LIVE and not vector_ready():raise RuntimeError("VECTOR_NOT_READY")
    with connect(database,readonly=False) as db:
        with db.transaction():
            rows={item.id:item for item in fetch_jobs(db,ids,locking=True)}
            if len(rows)!=len(ids) or any(eligibility(rows[n])!="eligible" for n in ids):
                raise ValueError("INELIGIBLE_OR_MISSING_JOB")
            updated=db.execute("""UPDATE core.ingest_outbox
                  SET status='pending',attempts=0,lease_token=NULL,updated_at=now()
                  WHERE id=ANY(%s) AND status='failed' AND attempts>=5
                    AND updated_at<now()-interval '5 seconds'
                  RETURNING id,document_id,event_kind""",(ids,)).fetchall()
            if len(updated)!=len(ids):
                raise RuntimeError("CONCURRENT_REQUEUE_ABORTED")
            for job_id,document_id,event_kind in updated:
                db.execute(
                    """INSERT INTO core.audit_event(actor,action,target,outcome)
                       VALUES(session_user,'requeue_exhausted_outbox',%s,'requeued_pending')""",
                    (f"{document_id}:{event_kind}:job-{job_id}",),
                )
    return {"mode":"apply","status":"passed","count":len(ids),"mutated":True}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database",default=LIVE)
    parser.add_argument("--job-ids",help="Comma-separated IDs (maximum 20)")
    parser.add_argument("--allow-live-readonly",action="store_true")
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--confirm-database")
    parser.add_argument("--allow-production-requeue",action="store_true")
    parser.add_argument("--json-out",type=Path)
    args=parser.parse_args(argv)
    if args.database!=LIVE and not STAGE.fullmatch(args.database):
        parser.error("Only production or factory_ingest_stage_XXXXXXXXXXXX is permitted.")
    try:ids=parse_ids(args.job_ids)
    except ValueError:parser.error("Invalid or duplicate job IDs.")
    if args.database==LIVE and not args.allow_live_readonly:
        parser.error("Live access requires --allow-live-readonly.")
    if args.apply:
        if not ids or args.confirm_database!=args.database:
            parser.error("Apply requires exact selected IDs and --confirm-database.")
        if args.database==LIVE and not args.allow_production_requeue:
            parser.error("Production apply requires --allow-production-requeue.")
    try:
        result=(apply(args.database,ids)
                if args.apply else preview(args.database,ids))
    except (ValueError,RuntimeError):
        result={"mode":"apply" if args.apply else "preview","status":"failed",
                "issue":"PRECONDITION_OR_CONCURRENT_CHANGE","mutated":False}
    except Exception as exc:
        result={"mode":"apply" if args.apply else "preview","status":"unavailable",
                "issue":"BACKEND_UNAVAILABLE_"+type(exc).__name__.upper(),"mutated":False}
    result["schema"]="factory_outbox_retry_v1"
    output=json.dumps(result,ensure_ascii=True,indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True,exist_ok=True)
        args.json_out.write_text(output,encoding="utf8")
    print(output)
    return 0 if result["status"]=="passed" else 1

if __name__=="__main__":
    sys.exit(main())
