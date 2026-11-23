"""Read-only PostgreSQL/Qdrant index consistency diagnostic.

Qdrant payloads are untrusted pointers: PostgreSQL RLS is authoritative.
This tool checks identifiers and metadata only; never reads source document
text, vector values, credentials into logs, or modifies either service.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
from uuid import uuid5

from core06 import hybrid_retrieval as hybrid
from core06.db_client import _creds_dir, _pg_port

COLLECTION = hybrid.COLLECTION
MAX_ROWS = 10000
DB_PATTERN = re.compile(r"(?:factory_core_v06|factory_ingest_stage_[0-9a-f]{12})\Z")
KEYS = frozenset(("tenant_id", "document_id", "chunk_id", "acl_scope_id"))


@dataclass(frozen=True)
class Document:
    document_id: str
    scope_id: str
    source_kind: str
    deleted: bool
    index_status: str | None
    delete_status: str | None
    index_attempts: int = 0


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str


@dataclass(frozen=True)
class Point:
    point_id: str
    payload: object


def point_id(chunk_id: str) -> str:
    return str(uuid5(hybrid.NAMESPACE, chunk_id))


def classify(doc: Document) -> str:
    if doc.deleted:
        return "deleted_final" if doc.delete_status == "indexed" else "deleted_pending"
    if doc.source_kind == "postgres_ingest":
        if doc.index_status == "indexed":
            return "required"
        if doc.index_status is None:
            return "index_job_missing"
        if doc.index_status == "failed" and doc.index_attempts >= 5:
            return "index_retry_exhausted"
        return "index_pending"
    return "required"


def compare_index(documents: list[Document], chunks: list[Chunk],
                  points: list[Point]) -> dict:
    """Pure deterministic reconciliation. No network access or mutations.

    Cross-system reads are not transactional; run twice during a quiet period
    before treating a discrepancy as durable corruption.
    """
    if max(len(documents), len(chunks), len(points)) > MAX_ROWS:
        return {"status": "failed", "issues": [{"code": "AUDIT_ROW_CAP_EXCEEDED", "count": 1}]}
    docs = {}
    errors, warnings, counts = Counter(), Counter(), Counter()
    for doc in documents:
        if not doc.document_id or doc.document_id in docs:
            errors["DUPLICATE_OR_INVALID_DOCUMENT_ID"] += 1
        docs[doc.document_id] = doc
        counts["documents"] += 1
        state = classify(doc)
        counts["documents_" + state] += 1
        if state == "index_job_missing":
            errors["INGEST_INDEX_EVENT_MISSING"] += 1
        if state == "index_retry_exhausted":
            errors["INGEST_RETRY_EXHAUSTED"] += 1
        if state in ("index_pending", "deleted_pending"):
            warnings["WORK_PENDING"] += 1
    expected = {}
    per_doc = Counter()
    for chunk in chunks:
        counts["chunks"] += 1
        doc = docs.get(chunk.document_id)
        if doc is None:
            errors["CHUNK_DOCUMENT_MISSING"] += 1
            continue
        per_doc[chunk.document_id] += 1
        identifier = point_id(chunk.chunk_id)
        if identifier in expected:
            errors["CHUNK_POINT_ID_DUPLICATED"] += 1
        expected[identifier] = (doc, chunk)
    for doc in documents:
        if not doc.deleted and not per_doc[doc.document_id]:
            errors["ACTIVE_DOCUMENT_HAS_NO_CHUNKS"] += 1
    observed = set()
    for point in points:
        counts["vector_points"] += 1
        if point.point_id in observed:
            errors["VECTOR_POINT_DUPLICATED"] += 1
        observed.add(point.point_id)
        match = expected.get(point.point_id)
        if match is None:
            errors["VECTOR_ORPHAN_POINT"] += 1
            continue
        doc, chunk = match
        state = classify(doc)
        if state == "deleted_final":
            errors["VECTOR_STALE_AFTER_DELETE_ACK"] += 1
        elif state == "deleted_pending":
            warnings["VECTOR_PENDING_DELETE"] += 1
        elif state in ("index_job_missing", "index_retry_exhausted"):
            warnings["VECTOR_PRESENT_WITH_INVALID_INDEX_JOB"] += 1
        elif state == "index_pending":
            warnings["VECTOR_PRESENT_BEFORE_INDEX_ACK"] += 1
        payload = point.payload
        if not isinstance(payload, dict) or frozenset(payload) != KEYS:
            errors["VECTOR_PAYLOAD_KEYS_INVALID"] += 1
            continue
        if (payload["tenant_id"] != hybrid.TENANT
            or payload["document_id"] != doc.document_id
            or payload["chunk_id"] != chunk.chunk_id
            or payload["acl_scope_id"] != doc.scope_id):
            errors["VECTOR_PAYLOAD_INCONSISTENT"] += 1
    for identifier, (doc, chunk) in expected.items():
        if identifier not in observed:
            state = classify(doc)
            if state == "required":
                errors["INDEXED_CHUNK_MISSING_FROM_VECTOR"] += 1
            elif state in ("index_pending", "deleted_pending"):
                counts["chunks_awaiting_index_or_delete"] += 1
    return {
        "status": "failed" if errors else "incomplete" if warnings else "passed",
        "counts": dict(sorted(counts.items())),
        "issues": [{"code": key, "count": count} for key, count in sorted(errors.items())],
        "warnings": [{"code": key, "count": count} for key, count in sorted(warnings.items())],
        "evidence_scope": "NON_ATOMIC_READ_ONLY_METADATA_RECONCILIATION",
    }


def load_database(database: str):
    """Read a consistent PG snapshot; no text, binaries or PII are selected."""
    import psycopg
    credentials = _creds_dir() / "admin-password.txt"
    with psycopg.connect(
        host="127.0.0.1", port=_pg_port(), dbname=database,
        user="factory_pg_admin", password=credentials.read_text("ascii"),
        connect_timeout=8,
        options="-c default_transaction_read_only=on -c statement_timeout=20000 -c lock_timeout=2000",
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if connection.execute("SHOW transaction_read_only").fetchone()[0] != "on":
            raise RuntimeError("READ_ONLY_NOT_ACTIVE")
        document_rows = connection.execute(
            "SELECT d.id,d.scope_id,d.source_kind,(d.deleted_at IS NOT NULL),"
            " oi.status,od.status,COALESCE(oi.attempts,0) "
            "FROM core.document d "
            "LEFT JOIN core.ingest_outbox oi "
            " ON oi.document_id=d.id AND oi.event_kind='index' "
            "LEFT JOIN core.ingest_outbox od "
            " ON od.document_id=d.id AND od.event_kind='delete' "
            "ORDER BY d.id LIMIT %s", (MAX_ROWS + 1,)
        ).fetchall()
        chunk_rows = connection.execute(
            "SELECT c.id,c.document_id FROM core.document_chunk c "
            "ORDER BY c.id LIMIT %s", (MAX_ROWS + 1,)
        ).fetchall()
    if max(len(document_rows), len(chunk_rows)) > MAX_ROWS:
        raise RuntimeError("PG_AUDIT_ROW_LIMIT_EXCEEDED")
    return [Document(*row) for row in document_rows], [Chunk(*row) for row in chunk_rows]


def load_vectors(client, collection: str):
    """Stream bounded Qdrant pages without vector values or embedded text."""
    from qdrant_client import models
    if not client.collection_exists(collection):
        raise RuntimeError("VECTOR_COLLECTION_UNAVAILABLE")
    points, offset = [], None
    for _ in range(1 + MAX_ROWS // 128):
        page, offset = client.scroll(
            collection_name=collection, offset=offset, limit=128,
            with_payload=True, with_vectors=False,
        )
        points.extend(Point(str(item.id), item.payload) for item in page)
        if len(points) > MAX_ROWS:
            raise RuntimeError("VECTOR_AUDIT_ROW_LIMIT_EXCEEDED")
        if offset is None:
            break
    else:
        raise RuntimeError("VECTOR_PAGINATION_DID_NOT_COMPLETE")
    count = client.count(collection, exact=True).count
    if count != len(points):
        raise RuntimeError("VECTOR_COUNT_CHANGED_DURING_SCAN")
    return points


def run_check(database: str, collection: str):
    # No collection creation, payload indexing or writes.
    docs, chunks = load_database(database)
    client = hybrid.qdrant_client(timeout=5)
    try:
        points = load_vectors(client, collection)
    finally:
        client.close()
    return compare_index(docs, chunks, points)


def main(argv=None):
    cli = argparse.ArgumentParser(description="Metadata-only Qdrant/PG index audit.")
    cli.add_argument("--database", default="factory_core_v06")
    cli.add_argument("--collection", default=COLLECTION)
    cli.add_argument("--allow-live-readonly", action="store_true")
    cli.add_argument("--json-out", type=Path)
    args = cli.parse_args(argv)
    if not DB_PATTERN.fullmatch(args.database):
        cli.error("Database must be the product DB or a known isolated staging DB.")
    if args.database == "factory_core_v06" and not args.allow_live_readonly:
        cli.error("Live read requires explicit --allow-live-readonly.")
    if args.collection != COLLECTION:
        cli.error("Collection must match the configured product collection.")
    try:
        result = run_check(args.database, args.collection)
    except Exception as exc:
        # No exception content: backend errors can include private connection info.
        result = {"status": "unavailable", "issues": [{
            "code": "INDEX_AUDIT_UNAVAILABLE_" + type(exc).__name__.upper(),
            "count": 1,
        }]}
    result["schema"] = "factory_vector_pg_integrity_readonly_v1"
    data = json.dumps(result, ensure_ascii=True, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(data, encoding="utf-8")
    print(data)
    return {"passed": 0, "failed": 1, "incomplete": 2, "unavailable": 3}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
