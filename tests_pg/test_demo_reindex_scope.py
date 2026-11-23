"""Real temporary PostgreSQL regression: demo bootstrap must not index uploads/deletions."""
from __future__ import annotations
from contextlib import closing
import secrets
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql

from core06 import hybrid_retrieval as hybrid
from core06.db_client import _creds_dir, _pg_port

ROOT=Path(__file__).resolve().parents[1]
DB_NAME="factory_reindex_test_"+secrets.token_hex(6)


def connect(name="postgres"):
    return psycopg.connect(
        host="127.0.0.1",port=_pg_port(),dbname=name,user="factory_pg_admin",
        password=(_creds_dir()/"admin-password.txt").read_text("ascii"),
        autocommit=True,connect_timeout=7,
    )


class Encoder:
    def embed(self, rows, **kwargs):
        return [[0.0]*383+[1.0] for _ in rows]


class VectorSink:
    def __init__(self):
        self.points=[]
    def upsert(self, collection_name, points, wait):
        assert collection_name==hybrid.COLLECTION and wait
        self.points.extend(points)
    def count(self, collection_name, exact=True):
        assert collection_name==hybrid.COLLECTION and exact
        return type("C", (), {"count":len(self.points)})()


def main(qdrant_exe=None, scratch_root=Path(r"K:\Temp")):
    if not DB_NAME.startswith("factory_reindex_test_") or len(DB_NAME)!=33:
        raise RuntimeError("REFUSE_UNSAFE_STAGING_NAME")
    created=False
    try:
        with connect() as db:
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
        created=True
        with connect(DB_NAME) as db:
            for name in ("schema.sql","ingestion_v1.sql","ingestion_v2.sql"):
                db.execute((ROOT/"core06"/name).read_text("utf-8-sig"))
            db.execute("INSERT INTO core.scope(id) VALUES('engineering')")
            fixtures=[
                ("demo-active","p13-demo",False),
                ("demo-deleted","p13-demo",True),
                ("upload-active","postgres_ingest",False),
                ("upload-deleted","postgres_ingest",True),
            ]
            for document,source,deleted in fixtures:
                db.execute("""INSERT INTO core.document(id,title,scope_id,doc_type,source_sha256,source_kind)
                              VALUES(%s,'Synthetic Fixture','engineering','internal',%s,%s)""",
                           (document,"0"*64,source))
                db.execute("""INSERT INTO core.document_chunk(id,document_id,body,page_no,extraction_method)
                              VALUES(%s,%s,'Synthetic demo sample for index test',1,'text')""",
                           (document+"-chunk-1",document))
                if deleted:
                    db.execute("UPDATE core.document SET deleted_at=now() WHERE id=%s",(document,))
        sink=VectorSink()
        # The real SQL must run against the ephemeral PG database; Qdrant is
        # deliberately replaced by an in-memory sink, no production write.
        with patch.object(hybrid,"database_session",lambda admin=True:connect(DB_NAME)), \
             patch.object(hybrid,"ensure_collection",lambda client:None):
            out=hybrid.index_demo_corpus(Encoder(),sink)
        assert out["indexed"]==1,out
        assert len(sink.points)==1,len(sink.points)
        assert sink.points[0].payload["document_id"]=="demo-active"
        assert frozenset(sink.points[0].payload)==hybrid.PAYLOAD_KEYS
        print("PASS real SQL: only active p13-demo indexed; deleted and uploaded excluded")
        if qdrant_exe is not None:
            # Optional true native Qdrant path. All writes are confined to the
            # short-lived owned process and unique private collection.
            from qdrant_client import QdrantClient
            from tests_pg.native_vector_fixture import native_vector
            vector_collection="factory_demo_reindex_"+secrets.token_hex(6)
            with native_vector(qdrant_exe, scratch_root=scratch_root) as port:
                with closing(QdrantClient(
                    url=f"http://127.0.0.1:{port}", timeout=15,
                    trust_env=False, check_compatibility=False,
                )) as client:
                    with patch.object(hybrid,"database_session",lambda admin=True:connect(DB_NAME)), \
                         patch.object(hybrid,"COLLECTION",vector_collection):
                        result=hybrid.index_demo_corpus(Encoder(),client)
                        actual,offset=client.scroll(
                            vector_collection,with_payload=True,with_vectors=False,
                        )
                        assert offset is None and len(actual)==1,(len(actual),offset)
                        assert actual[0].payload["document_id"]=="demo-active"
                        assert result["indexed"]==1
                        assert client.count(vector_collection,exact=True).count==1
                        client.delete_collection(vector_collection)
            print("PASS real native Qdrant: only active demo point persisted; uploaded and deleted absent")
    finally:
        if created:
            with connect() as db:
                db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB_NAME)))
            print("CLEANUP owned ephemeral PostgreSQL database removed")


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--native-qdrant",type=Path)
    parser.add_argument("--scratch-root",type=Path,default=Path(r"K:\Temp"))
    args=parser.parse_args()
    main(args.native_qdrant,args.scratch_root)
