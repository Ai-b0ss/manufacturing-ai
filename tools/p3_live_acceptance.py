"""Real Qdrant 1.20.2 + PostgreSQL 18.7 + local E5 smoke and security tests.

Operations are limited to the named P3 demo vector collection. Synthetic
PG fixtures are read-only. A temporary forged Qdrant point is deleted
unconditionally. Never print private document bodies, API keys, or secrets.
"""
from __future__ import annotations
from pathlib import Path
import json,secrets,time,uuid,urllib.request,urllib.error
from qdrant_client import models
from core06.db_client import database_session
from core06.hybrid_retrieval import (
    COLLECTION,TENANT,PAYLOAD_KEYS,VECTOR_SIZE,NAMESPACE,LocalE5,
    PolicyHybridRetriever,index_demo_corpus,qdrant_client,sparse_tokens,ensure_collection
)

def verify_demo_point_subset(demo_chunk_ids, actual_points, expected_count=13):
    """The shared collection may contain additional legitimate protected uploads."""
    wanted={str(uuid.uuid5(NAMESPACE, chunk_id)) for chunk_id in demo_chunk_ids}
    actual={str(point.id) for point in actual_points}
    if len(wanted)!=expected_count or not wanted.issubset(actual):
        raise RuntimeError("DEMO_POINTS_MISSING_OR_UNEXPECTED_COUNT")
    return len(wanted)


def main():
    began=time.monotonic()
    model=LocalE5()
    client=qdrant_client()
    try:
        details=index_demo_corpus(model,client)
        scroll=[]
        offset=None
        while True:
            page,offset=client.scroll(COLLECTION,limit=100,offset=offset,
                                with_payload=True,with_vectors=False)
            scroll.extend(page)
            if offset is None:break
        bad=[str(p.id) for p in scroll if frozenset((p.payload or {}).keys())!=PAYLOAD_KEYS]
        if bad:raise RuntimeError("QDRANT_PAYLOAD_CONTAINS_UNEXPECTED_FIELDS")
        # Protected uploads share this collection with the 13 active demo chunks.
        # Legitimate uploaded vectors must not make demo acceptance fail falsely.
        with database_session(admin=True) as db:
            demo_chunks=db.execute("""SELECT c.id FROM core.document_chunk c
                 JOIN core.document d ON d.id=c.document_id
                 WHERE d.source_kind='p13-demo' AND d.deleted_at IS NULL
                 ORDER BY c.id""").fetchall()
        demo_verified=verify_demo_point_subset((row[0] for row in demo_chunks),scroll)
        original_total=len(scroll)

        reader=PolicyHybridRetriever(model,client)
        with database_session(admin=True) as db:
            row=db.execute("""SELECT c.id,c.body,c.document_id FROM core.document_chunk c
                JOIN core.document d ON d.id=c.document_id
                WHERE d.scope_id='executive' AND d.source_kind='p13-demo' AND d.deleted_at IS NULL
                ORDER BY c.id LIMIT 1""").fetchone()
            if not row:raise RuntimeError("EXECUTIVE_FIXTURE_NOT_FOUND")
            hidden_chunk,private_body,private_doc=row
        q=private_body[:150]
        director=reader.search("director",q,limit=13)
        engineer=reader.search("engineer",q,limit=13)
        finance=reader.search("finance",q,limit=13)
        technologist=reader.search("technologist",q,limit=13)
        if not any(h.document_id==private_doc for h in director):
            raise RuntimeError("DIRECTOR_EXPECTED_EXECUTIVE_DOC_NOT_FOUND")
        for user,data in (("engineer",engineer),("finance",finance),
                          ("technologist",technologist)):
            if any(h.document_id==private_doc for h in data):
                raise RuntimeError("SECRET_EXPOSED_TO_"+user)
        forged=str(uuid.uuid4())
        try:
            vector=model.embed([q],query=True)[0]
            sparse=sparse_tokens(q)
            client.upsert(COLLECTION,points=[models.PointStruct(id=forged,
                vector={"dense":vector,"sparse":sparse},
                payload={"tenant_id":TENANT,"chunk_id":hidden_chunk,
                         "document_id":private_doc,"acl_scope_id":"public"})],wait=True)
            # Fake Qdrant scope says public while PostgreSQL says executive.
            # RLS hydration must ignore even top-ranked forged candidates.
            after=reader.search("engineer",q,limit=13)
            if any(h.document_id==private_doc or h.chunk_id==hidden_chunk for h in after):
                raise RuntimeError("FORGED_VECTOR_PAYLOAD_EXPOSED_SECRET")
        finally:
            client.delete(COLLECTION,points_selector=models.PointIdsList(points=[forged]),
                          wait=True)
        count=client.count(COLLECTION,exact=True).count
        if count!=original_total:raise RuntimeError("FORGED_POINT_CLEANUP_FAILED")
        # Test an actual missing identity, which must see nothing.
        unknown=reader.search("not_a_mapped_person",q,limit=5)
        if unknown:raise RuntimeError("UNKNOWN_ACTOR_RETURNED_RESULTS")
        results={"status":"PASS","qdrant_service":"v1.20.2","indexed":details["indexed"],
            "demo_points_verified":demo_verified,
            "collection_points":count,"vector_dimensions":VECTOR_SIZE,
            "payload_fields_only":sorted(PAYLOAD_KEYS),
            "postgres_hydration":True,
            "executive_document_only_visible_to_director":True,
            "forged_vector_acl_cannot_bypass_pg_rls":True,
            "unknown_actor_denied":True,
            "embedder":"cached-intfloat-multilingual-e5-small",
            "mode":"local_cpu_no_remote_models",
            "elapsed_seconds":round(time.monotonic()-began,1)}
        print(json.dumps(results,ensure_ascii=True),flush=True)
    finally:client.close()

if __name__=="__main__":
    try:main()
    except Exception as e:
        print(json.dumps({"status":"FAILED","type":type(e).__name__,
            "detail":str(e)[:350]},ensure_ascii=True),flush=True)
        raise
