"""Operator-only physical purge for already soft-deleted v0.6 uploads.

Default is DRY RUN. "Physical" means active PostgreSQL rows and Qdrant
vectors; PostgreSQL WAL, backups and snapshots remain subject to retention
policies. Never invoked by public HTTP routes.
"""
from __future__ import annotations
import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime,timezone

from qdrant_client import models
from core06.db_client import database_session
from core06 import hybrid_retrieval as hybrid


DOC_ID = re.compile(r"upload-[0-9a-f]{32}\Z")


class PurgeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class PurgeResult:
    document_id: str
    eligible: bool
    executed: bool
    chunks: int
    referenced_qdrant_points: int
    blockers: tuple[str,...]


def _qdrant_count(client, doc_id):
    # Any error must block purge, even if there is no local vector collection.
    if not client.collection_exists(hybrid.COLLECTION):
        return 0
    flt=models.Filter(must=[models.FieldCondition(
        key="document_id",match=models.MatchValue(value=doc_id)
    )])
    return int(client.count(
        collection_name=hybrid.COLLECTION, count_filter=flt, exact=True
    ).count)


def _inspect(db,client,doc_id):
    row=db.execute(
        """SELECT source_kind,deleted_at FROM core.document WHERE id=%s""",
        (doc_id,),
    ).fetchone()
    if row is None:
        raise PurgeBlocked("DOCUMENT_NOT_FOUND")
    source,deleted_at=row
    statuses=db.execute(
        """SELECT event_kind,status FROM core.ingest_outbox
           WHERE document_id=%s ORDER BY event_kind""",
        (doc_id,),
    ).fetchall()
    chunks=db.execute(
        "SELECT count(*) FROM core.document_chunk WHERE document_id=%s",
        (doc_id,),
    ).fetchone()[0]
    request_row=db.execute(
        "SELECT actor_id,request_key FROM core.ingest_request WHERE document_id=%s",
        (doc_id,),
    ).fetchone()
    blockers=[]
    if request_row is None:blockers.append("IDEMPOTENCY_TOMBSTONE_SOURCE_MISSING")
    if source!="postgres_ingest":blockers.append("NOT_AN_UPLOADED_DOCUMENT")
    if deleted_at is None:blockers.append("NOT_LOGICALLY_DELETED")
    elif (datetime.now(timezone.utc)-deleted_at).total_seconds()<86400:
        blockers.append("RETENTION_24H_NOT_MET")
    dictionary=dict(statuses)
    if dictionary.get("delete")!="indexed":
        blockers.append("QDRANT_DELETE_NOT_ACKNOWLEDGED")
    if any(status=="processing" for _event,status in statuses):
        blockers.append("INDEX_JOB_PROCESSING")
    count=_qdrant_count(client,doc_id)
    if count>0:blockers.append("QDRANT_POINTS_STILL_EXIST")
    return PurgeResult(doc_id,not blockers,False,int(chunks),count,tuple(blockers))


def purge_one(doc_id: str, execute=False, db_factory=database_session, client=None):
    if not isinstance(doc_id,str) or not DOC_ID.fullmatch(doc_id):
        raise PurgeBlocked("DOCUMENT_ID_INVALID")
    if execute and os.environ.get("FACTORY_V06_PURGE_ENABLED")!="1":
        raise PurgeBlocked("PHYSICAL_PURGE_NOT_ENABLED")
    owns_client=client is None
    if owns_client:
        client=hybrid.qdrant_client()
    try:
        with db_factory(admin=True) as db:
            with db.transaction():
                if execute:
                    # MUST match worker's cross-process session advisory lock.
                    db.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        ("purge-upload:"+doc_id,),
                    )
                result=_inspect(db,client,doc_id)
                if not execute:
                    return result
                if not result.eligible:
                    raise PurgeBlocked("PURGE_PRECONDITIONS_FAILED: "+",".join(result.blockers))
                request_key=db.execute(
                    "SELECT actor_id,request_key FROM core.ingest_request WHERE document_id=%s",
                    (doc_id,),
                ).fetchone()
                if request_key is None:
                    raise PurgeBlocked("IDEMPOTENCY_SOURCE_NOT_FOUND")
                confirmed=db.execute(
                    "SELECT core.purge_deleted_upload(%s)",
                    (doc_id,),
                ).fetchone()
                if confirmed != (True,):
                    raise PurgeBlocked("PURGE_DATABASE_NOT_CONFIRMED")
                for table in ("document","document_blob","document_chunk","ingest_outbox"):
                    row=db.execute(
                        # Table names are a fixed trusted allowlist, never caller input.
                        "SELECT count(*) FROM core."+table+
                        " WHERE "+("id" if table=="document" else "document_id")+"=%s",
                        (doc_id,),
                    ).fetchone()
                    if row != (0,):
                        raise PurgeBlocked("PURGE_RESIDUAL_ACTIVE_DATA_"+table)
                tombstone=db.execute(
                    """SELECT document_id FROM core.ingest_request
                       WHERE actor_id=%s AND request_key=%s""",
                    request_key,
                ).fetchone()
                if tombstone!=(None,):
                    raise PurgeBlocked("IDEMPOTENCY_TOMBSTONE_MISSING")
                if _qdrant_count(client,doc_id)!=0:
                    raise PurgeBlocked("QDRANT_REINTRODUCED_POINTS")
                return PurgeResult(doc_id,True,True,result.chunks,0,())
    finally:
        if owns_client and client is not None and hasattr(client, "close"):
            client.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description="Purge a logically deleted document after 24-hour retention")
    parser.add_argument("--document-id",required=True)
    parser.add_argument("--execute",action="store_true",help="Physically remove active DB rows")
    parser.add_argument("--confirm-id",default=None,help="Must equal --document-id when executing")
    args=parser.parse_args(argv)
    if args.execute and args.confirm_id!=args.document_id:
        parser.error("Explicit exact document ID confirmation is required")
    result=purge_one(args.document_id,execute=args.execute)
    print(json.dumps({
        "document_id":result.document_id,
        "eligible":result.eligible,
        "executed":result.executed,
        "chunks":result.chunks,
        "qdrant_points":result.referenced_qdrant_points,
        "blockers":result.blockers,
        "backup_erasure_confirmed":False,
    },ensure_ascii=False))
    return 0 if (result.executed if args.execute else True) else 2


if __name__=="__main__":
    try:raise SystemExit(main())
    except PurgeBlocked as exc:
        print(json.dumps({"executed":False,"error":str(exc)[:250]}))
        raise SystemExit(3)
