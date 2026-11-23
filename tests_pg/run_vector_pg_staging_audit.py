"""Isolated real PostgreSQL + real native Qdrant acceptance of the read-only auditor."""
from __future__ import annotations
from contextlib import closing
import hashlib,json,os,secrets,subprocess
from pathlib import Path
import psycopg
from psycopg import sql
from qdrant_client import QdrantClient,models
from core06.db_client import _creds_dir,_pg_port
from tests_pg.native_vector_fixture import native_vector
from tools.vector_index_integrity_audit import point_id

ROOT=Path(__file__).resolve().parents[1]
PY=Path(r"C:\Users\user\Documents\ManufacturingAI-Core-V06-P3-Hybrid-Retrieval\.p3venv\Scripts\python.exe")
NAME="factory_ingest_stage_"+secrets.token_hex(6)
DOC="upload-"+secrets.token_hex(16)
CHUNK=DOC+"-chunk-001"
CONTENT=b"synthetic isolated regression reference"
SHA=hashlib.sha256(CONTENT).hexdigest()
STARTUP=subprocess.STARTUPINFO()
STARTUP.dwFlags|=subprocess.STARTF_USESHOWWINDOW
STARTUP.wShowWindow=subprocess.SW_HIDE

def connection(name="postgres"):
    return psycopg.connect(host="127.0.0.1",port=_pg_port(),dbname=name,
        user="factory_pg_admin",password=(_creds_dir()/"admin-password.txt").read_text("ascii"),
        autocommit=True,connect_timeout=8)

def auditor(port):
    env=os.environ.copy()
    env.update({"FACTORY_V06_UNIFIED_STAGING":"1",
                "FACTORY_PG_PORT":str(_pg_port()),
                "FACTORY_V06_STAGING_DATABASE":NAME,
                "FACTORY_V06_STAGING_COLLECTION":NAME,
                "FACTORY_V06_ISOLATED_VECTOR_PORT":str(port),
                "PYTHONUTF8":"1",
                "NO_PROXY":"localhost,127.0.0.1",
                "no_proxy":"localhost,127.0.0.1"})
    p=subprocess.run([str(PY),"-m","tools.vector_index_integrity_audit",
        "--database",NAME],env=env,cwd=ROOT,
        capture_output=True,text=True,encoding="utf8",errors="replace",
        timeout=25,stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=STARTUP)
    return p.returncode,json.loads(p.stdout)

def verify(report,label,port,code,status,issue=None):
    rc,data=auditor(port)
    actual={x["code"] for x in data.get("issues",[])}
    if rc!=code or data["status"]!=status or issue and issue not in actual:
        raise RuntimeError("ISOLATED_AUDIT_EXPECTATION_FAILED_"+label)
    report["tests"].append({"name":label,"result":"passed",
                            "auditor_exit":rc,"auditor_status":status})

def main(executable,scratch):
    result={"schema":"vector_pg_staging_e2e_v1","result":"running",
            "tests":[],"cleanup":[],"production_data_mutated":False}
    created=False
    try:
        with connection() as db:
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(NAME)))
        created=True
        with connection(NAME) as db:
            for name in ("schema.sql","ingestion_v1.sql","ingestion_v2.sql","ingestion_v3_purge.sql"):
                db.execute((ROOT/"core06"/name).read_text("utf-8-sig"))
            db.execute("INSERT INTO core.subject(id) VALUES('audit_engineer')")
            db.execute("INSERT INTO core.scope(id) VALUES('engineering')")
            db.execute("""INSERT INTO core.document(id,title,scope_id,doc_type,source_sha256,source_kind)
                          VALUES(%s,'Synthetic Fixture','engineering','uploaded',%s,'postgres_ingest')""",(DOC,SHA))
            db.execute("""INSERT INTO core.document_blob(document_id,filename,media_type,content_sha256,original_bytes)
                          VALUES(%s,'fixture.txt','text/plain',%s,%s)""",(DOC,SHA,CONTENT))
            db.execute("""INSERT INTO core.document_chunk(id,document_id,body,page_no,extraction_method)
                          VALUES(%s,%s,'Synthetic data only',1,'text')""",(CHUNK,DOC))
            db.execute("""INSERT INTO core.ingest_outbox(document_id,event_kind,status,attempts)
                          VALUES(%s,'index','indexed',1)""",(DOC,))
        with native_vector(executable,scratch_root=scratch) as port:
            with closing(QdrantClient(url="http://127.0.0.1:"+str(port),timeout=10,
                       trust_env=False,check_compatibility=False)) as vec:
                vec.create_collection(collection_name=NAME,vectors_config={
                    "dense":models.VectorParams(size=384,distance=models.Distance.COSINE)})
                def upsert(scope="engineering"):
                    vec.upsert(collection_name=NAME,wait=True,points=[models.PointStruct(
                        id=point_id(CHUNK),vector={"dense":[0.0]*383+[1.0]},
                        payload={"tenant_id":"synthetic-demo","document_id":DOC,
                                 "chunk_id":CHUNK,"acl_scope_id":scope})])
                def remove():
                    vec.delete(collection_name=NAME,
                        points_selector=models.PointIdsList(points=[point_id(CHUNK)]),wait=True)
                upsert()
                verify(result,"real_postgres_and_qdrant_match",port,0,"passed")
                upsert("finance")
                verify(result,"tampered_scope_detected",port,1,"failed","VECTOR_PAYLOAD_INCONSISTENT")
                upsert()
                verify(result,"scope_repaired",port,0,"passed")
                remove()
                verify(result,"missing_vector_detected",port,1,"failed","INDEXED_CHUNK_MISSING_FROM_VECTOR")
                upsert()
                with connection(NAME) as db:
                    db.execute("UPDATE core.document SET deleted_at=now() WHERE id=%s",(DOC,))
                    db.execute("""INSERT INTO core.ingest_outbox(document_id,event_kind,status)
                                  VALUES(%s,'delete','indexed')""",(DOC,))
                verify(result,"deleted_stale_point_detected",port,1,"failed","VECTOR_STALE_AFTER_DELETE_ACK")
                remove()
                verify(result,"completed_delete_accepted",port,0,"passed")
                vec.delete_collection(NAME)
        result["result"]="passed"
    finally:
        if created:
            if len(NAME)!=33 or not NAME.startswith("factory_ingest_stage_"):
                raise RuntimeError("REFUSE_UNSAFE_DATABASE_CLEANUP")
            with connection() as db:
                db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(NAME)))
            result["cleanup"].append("OWNED_STAGING_POSTGRES_REMOVED")
        if result["result"]=="passed":
            result["cleanup"].append("ISOLATED_QDRANT_PROCESS_STOPPED")
        out=ROOT/"data"/"vector_pg_staging_receipt.json"
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(result,ensure_ascii=True,indent=2),encoding="utf8")
    return result

if __name__=="__main__":
    import argparse
    cli=argparse.ArgumentParser()
    cli.add_argument("--qdrant-exe",type=Path,required=True)
    cli.add_argument("--scratch",type=Path,default=Path(r"K:\Temp"))
    args=cli.parse_args()
    report=main(args.qdrant_exe,args.scratch)
    print(json.dumps({"result":report["result"],"passed":len(report["tests"]),
                      "cleanup":report["cleanup"]},ensure_ascii=True))
