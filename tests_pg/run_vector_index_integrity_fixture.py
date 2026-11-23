"""Real Qdrant 1.20+ metadata reconciliation on an isolated Windows process.

Does NOT connect to or alter production PostgreSQL, Qdrant or Keycloak.
"""
from __future__ import annotations
import argparse
import json
import secrets
from contextlib import closing
from pathlib import Path

from qdrant_client import QdrantClient, models
from tests_pg.native_vector_fixture import native_vector
from tools.vector_index_integrity_audit import (
    Chunk, Document, compare_index, load_vectors, point_id,
)


def run(executable: Path, scratch: Path):
    results = []
    docs = [
        Document("fixture-alpha", "engineering", "postgres_ingest", False, "indexed", None, 1),
        Document("fixture-beta", "engineering", "postgres_ingest", False, "pending", None, 0),
    ]
    chunks = [
        Chunk("fixture-chunk-alpha", "fixture-alpha"),
        Chunk("fixture-chunk-beta", "fixture-beta"),
    ]
    collection = "factory_vector_audit_" + secrets.token_hex(6)
    with native_vector(executable, scratch_root=scratch) as port:
        with closing(QdrantClient(url="http://127.0.0.1:" + str(port), timeout=10, trust_env=False,
                                  check_compatibility=False)) as client:
            client.create_collection(
                collection_name=collection,
                vectors_config={"dense": models.VectorParams(
                    size=384, distance=models.Distance.COSINE)},
            )
            def payload(doc, chunk):
                return {
                    "tenant_id": "synthetic-demo",
                    "document_id": doc,
                    "chunk_id": chunk,
                    "acl_scope_id": "engineering",
                }
            def add(doc, chunk, scope="engineering"):
                fields = payload(doc, chunk)
                fields["acl_scope_id"] = scope
                client.upsert(collection_name=collection,
                              points=[models.PointStruct(
                                  id=point_id(chunk),
                                  vector={"dense": [0.0] * 383 + [1.0]},
                                  payload=fields,
                              )],wait=True)
            def check(label, status, must_issue=None):
                live = load_vectors(client, collection)
                report = compare_index(docs, chunks, live)
                if report["status"] != status:
                    raise AssertionError((label, status, report))
                if must_issue and must_issue not in {
                    x["code"] for x in report["issues"] + report["warnings"]
                }:
                    raise AssertionError((label, must_issue, report))
                results.append({
                    "scenario": label, "result": "passed",
                    "status": report["status"],
                    "vector_points": report["counts"].get("vector_points", 0),
                    "issues": [x["code"] for x in report["issues"]],
                    "warnings": [x["code"] for x in report["warnings"]],
                })
            add("fixture-alpha", "fixture-chunk-alpha")
            check("consistent_index_with_pending_upload", "incomplete", "WORK_PENDING")
            add("fixture-beta", "fixture-chunk-beta")
            check("inflight_point_before_index_ack", "incomplete", "VECTOR_PRESENT_BEFORE_INDEX_ACK")
            docs[1] = Document("fixture-beta","engineering","postgres_ingest",False,"indexed",None,1)
            check("indexed_two_documents", "passed")
            client.set_payload(
                collection_name=collection,
                payload={"acl_scope_id":"finance"},
                points=[point_id("fixture-chunk-alpha")], wait=True)
            check("real_qdrant_payload_tamper", "failed", "VECTOR_PAYLOAD_INCONSISTENT")
            client.set_payload(
                collection_name=collection,
                payload={"acl_scope_id":"engineering"},
                points=[point_id("fixture-chunk-alpha")], wait=True)
            docs[1] = Document("fixture-beta","engineering","postgres_ingest",True,"indexed","indexed",1)
            check("deleted_but_vector_retained", "failed", "VECTOR_STALE_AFTER_DELETE_ACK")
            client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=[point_id("fixture-chunk-beta")]),
                wait=True)
            check("deleted_point_removed", "passed")
            client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=[point_id("fixture-chunk-alpha")]),
                wait=True)
            check("missing_indexed_point", "failed", "INDEXED_CHUNK_MISSING_FROM_VECTOR")
            add("untracked", "untracked-chunk")
            check("orphan_point", "failed", "VECTOR_ORPHAN_POINT")
            client.delete_collection(collection)
    return {"schema":"factory_vector_audit_fixture_v1",
            "result":"passed", "checked_scenarios":len(results),
            "synthetic_only":True, "production_touched":False, "scenarios":results}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--qdrant-exe",type=Path,required=True)
    parser.add_argument("--scratch",type=Path,default=Path(r"K:\Temp"))
    parser.add_argument("--json-out",type=Path)
    args=parser.parse_args()
    output=run(args.qdrant_exe,args.scratch)
    text=json.dumps(output,ensure_ascii=True,indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True,exist_ok=True)
        args.json_out.write_text(text,encoding="utf8")
    print(text)

if __name__=="__main__":
    main()
