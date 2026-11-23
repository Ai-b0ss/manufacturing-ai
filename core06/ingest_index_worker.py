"""Bounded and retryable PostgreSQL outbox -> Qdrant lifecycle.

No untrusted text appears in Qdrant payload; PostgreSQL RLS is authoritative.
Only operates on collections configured in core06.hybrid_retrieval.
This module does NOT launch a daemon or change any service by importing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from uuid import uuid4, uuid5

from qdrant_client import models
from core06.db_client import database_session
from core06 import hybrid_retrieval as hybrid


@dataclass(frozen=True)
class IndexOutcome:
    document_id: str | None
    result: str
    chunks: int


@lru_cache(maxsize=1)
def _cached_encoder():
    return hybrid.LocalE5()


def _claim(db_factory):
    lease = uuid4().hex
    with db_factory(admin=True) as db:
        with db.transaction():
            item = db.execute(
                """SELECT id,document_id,event_kind FROM core.ingest_outbox
                   WHERE (status='pending'
                     OR (status='failed' AND updated_at < now()-interval '5 seconds')
                     OR (status='processing' AND updated_at < now()-interval '10 minutes'))
                     AND attempts<5
                   ORDER BY CASE WHEN event_kind='delete' THEN 0 ELSE 1 END,id
                   FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if item is None:
                return None
            task_id, doc_id, event_kind = item
            db.execute(
                """UPDATE core.ingest_outbox SET status='processing',
                       attempts=attempts+1,updated_at=now(),lease_token=%s
                   WHERE id=%s""",
                (lease, task_id),
            )
            return task_id, doc_id, event_kind, lease


def _mark(db_factory, task_id: int, lease: str, status: str):
    with db_factory(admin=True) as db:
        row = db.execute(
            """UPDATE core.ingest_outbox
               SET status=%s,updated_at=now(),lease_token=NULL
               WHERE id=%s AND lease_token=%s AND status='processing'
               RETURNING id""",
            (status, task_id, lease),
        ).fetchone()
        return row is not None


def _delete_points(client, doc_id: str, chunks):
    if not client.collection_exists(hybrid.COLLECTION):
        return 0
    ids = [str(uuid5(hybrid.NAMESPACE, chunk_id)) for chunk_id in chunks]
    if ids:
        client.delete(
            collection_name=hybrid.COLLECTION,
            points_selector=models.PointIdsList(points=ids),
            wait=True,
        )
    return len(ids)


def process_one(db_factory=database_session, encoder=None, client=None) -> IndexOutcome:
    """One job and its independent DB/Qdrant postcondition, no service restarts."""
    if client is not None:
        return _process_available(db_factory, encoder, client)
    client = hybrid.qdrant_client()
    try:
        return _process_available(db_factory, encoder, client)
    finally:
        client.close()


def _process_available(db_factory, encoder, client):
    # A known service outage must not spend a retry on every queued document.
    client.get_collections()
    claimed = _claim(db_factory)
    if claimed is None:
        return IndexOutcome(None, "empty", 0)
    task_id, doc_id, _event_kind, lease = claimed
    # Session advisory lock serializes any Qdrant mutation of this document
    # with SQL+Qdrant verified physical purge. Do not release mid-upsert.
    try:
        with db_factory(admin=True) as lock_db:
            locked = False
            lock_db.execute("SET lock_timeout = '15s'")
            lock_db.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s,0))",
                ("purge-upload:"+doc_id,),
            )
            locked = True
            try:
                return _process_claimed(claimed, db_factory, encoder, client)
            finally:
                if locked:
                    row = lock_db.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                        ("purge-upload:"+doc_id,),
                    ).fetchone()
                    if row != (True,):
                        raise RuntimeError("INDEX_ADVISORY_LOCK_RELEASE_FAILED")
    except Exception as exc:
        logging.getLogger(__name__).warning("Index lock or session failed: %s", type(exc).__name__)
        try:
            _mark(db_factory, task_id, lease, "failed")
        except Exception:
            pass
        return IndexOutcome(doc_id, "failed", 0)


def _process_claimed(claimed, db_factory, encoder, client):
    task_id, doc_id, event_kind, lease = claimed
    count = 0
    try:
        with db_factory(admin=True) as db:
            doc = db.execute(
                "SELECT scope_id,deleted_at FROM core.document WHERE id=%s", (doc_id,)
            ).fetchone()
            chunks = db.execute(
                """SELECT id,body FROM core.document_chunk
                   WHERE document_id=%s ORDER BY id""", (doc_id,)
            ).fetchall()
        if doc is None:
            raise RuntimeError("OUTBOX_MISSING_DOCUMENT")
        scope_id, deleted_at = doc
        if len(chunks) > 100:
            raise RuntimeError("TOO_MANY_INDEX_CHUNKS")
        if event_kind == "delete" or deleted_at is not None:
            count = _delete_points(client, doc_id, [row[0] for row in chunks])
            if not _mark(db_factory, task_id, lease, "indexed"):
                raise RuntimeError("INDEX_LEASE_WAS_LOST")
            return IndexOutcome(doc_id, "deleted", count)
        if event_kind != "index":
            raise RuntimeError("UNEXPECTED_JOB_TYPE")
        if not chunks:
            raise RuntimeError("EMPTY_INDEX_SOURCE")
        if any(
            not isinstance(body, str) or not 1 <= len(body) <= 15000
            for _, body in chunks
        ):
            raise RuntimeError("INVALID_INDEX_CHUNK_LENGTH")

        encoder = encoder or _cached_encoder()
        hybrid.ensure_collection(client)
        vectors = encoder.embed([body for _, body in chunks])
        if len(vectors) != len(chunks):
            raise RuntimeError("INVALID_EMBEDDING_COUNT")
        points = []
        for (chunk_id, body), dense in zip(chunks, vectors):
            sparse = hybrid.sparse_tokens(body)
            if sparse is None:
                raise RuntimeError("EMPTY_SPARSE_CHUNK")
            payload = {
                "tenant_id": hybrid.TENANT,
                "document_id": doc_id,
                "chunk_id": chunk_id,
                "acl_scope_id": scope_id,
            }
            if frozenset(payload) != hybrid.PAYLOAD_KEYS:
                raise RuntimeError("UNEXPECTED_PAYLOAD")
            points.append(models.PointStruct(
                id=str(uuid5(hybrid.NAMESPACE, chunk_id)),
                vector={"dense": dense, "sparse": sparse},
                payload=payload,
            ))

        # A scope move/deletion while embedding was running invalidates this job.
        with db_factory(admin=True) as db:
            recent = db.execute(
                "SELECT scope_id,deleted_at FROM core.document WHERE id=%s", (doc_id,)
            ).fetchone()
        if recent is None or recent[1] is not None or recent[0] != scope_id:
            _delete_points(client, doc_id, [row[0] for row in chunks])
            raise RuntimeError("INDEX_SOURCE_CHANGED_DURING_EMBEDDING")

        client.upsert(collection_name=hybrid.COLLECTION, points=points, wait=True)
        count = len(points)

        # If deletion races the Qdrant write, remove even newly inserted points.
        with db_factory(admin=True) as db:
            recent = db.execute(
                "SELECT scope_id,deleted_at FROM core.document WHERE id=%s", (doc_id,)
            ).fetchone()
        if recent is None or recent[1] is not None or recent[0] != scope_id:
            _delete_points(client, doc_id, [row[0] for row in chunks])
            raise RuntimeError("INDEX_SOURCE_CHANGED_DURING_UPSERT")
        if not _mark(db_factory, task_id, lease, "indexed"):
            raise RuntimeError("INDEX_LEASE_WAS_LOST")
        return IndexOutcome(doc_id, "indexed", count)
    except Exception as exc:
        logging.getLogger(__name__).warning("Index job failed: %s", type(exc).__name__)
        _mark(db_factory, task_id, lease, "failed")
        return IndexOutcome(doc_id, "failed", 0)
