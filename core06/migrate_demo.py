"""Transactional read-only migration of P13 demo material into PostgreSQL v0.6.

Strict source and table allowlist. No authentication salts, passwords, sessions,
cookies, uploaded binary attachments, or secrets are copied into PostgreSQL.
SQLite online backups are temporary and removed after the operation.
"""
from __future__ import annotations
from contextlib import ExitStack,closing
from datetime import datetime,timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import json,os,re,sqlite3,sys

import psycopg
from psycopg.types.json import Jsonb

HERE=Path(__file__).resolve().parent.parent
SOURCE=Path(r"C:\Users\user\Documents\ManufacturingAI-P13-Drawing-CAD-Registration\data")
RUNTIME=Path(r"K:\ManufacturingAI-CoreRuntime")
DB_NAME="factory_core_v06"
WHITELIST={
 "demo.sqlite3":("account","scope","acl","document","chunk","chunk_page","finance",
                 "change_notice","lot","audit_event"),
 "engineering_corpus.sqlite3":("source","source_page","bom_line")
}
CLASSIFICATIONS={
 "public":"public","engineering":"internal","finance":"restricted",
 "executive":"secret","personal-director":"restricted","personal-engineer":"restricted",
 "personal-finance":"restricted","personal-technologist":"restricted"
}
COUNTS_EXPECTED={"document":13,"chunk":13,"account":4,"scope":8,"acl":13,
                 "source":40,"source_page":36,"bom_line":351}

def read_only_snapshot(name,destination):
    path=SOURCE/name
    if not path.is_file():raise RuntimeError("SOURCE_NOT_FOUND_"+name)
    if path.stat().st_size>25_000_000:raise RuntimeError("SOURCE_SIZE_LIMIT")
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=8)) as con:
        with closing(sqlite3.connect(destination)) as clone:
            con.backup(clone,pages=100,sleep=.03)
    digest=sha256(destination.read_bytes()).hexdigest()
    snap=sqlite3.connect(destination)
    snap.row_factory=sqlite3.Row
    return snap,{"source_name":name,"source_sha256":digest,"source_bytes":destination.stat().st_size}

def getrows(db,table):
    if not re.fullmatch(r"[a-z_]{1,32}",table):raise ValueError("INVALID_TABLE")
    return [dict(r) for r in db.execute('SELECT * FROM "'+table+'"')]

def import_data(con,demo,corpus,source_meta):
    counts={}
    def record(key,rows):counts[key]=len(rows);return rows
    for r in record("account",getrows(demo,"account")):
        con.execute("""INSERT INTO core.subject(id,source) VALUES(%s,%s)
                       ON CONFLICT(id) DO NOTHING""",(r["id"],"synthetic_p13_demo"))
    for r in record("scope",getrows(demo,"scope")):
        label=CLASSIFICATIONS.get(r["id"])
        if label is None:raise RuntimeError("UNKNOWN_SCOPE_CLASSIFICATION")
        con.execute("""INSERT INTO core.scope(id,classification,epoch) VALUES(%s,%s,%s)
                       ON CONFLICT(id) DO NOTHING""",(r["id"],label,r["epoch"]))
    for r in record("acl",getrows(demo,"acl")):
        con.execute("""INSERT INTO core.scope_grant(subject_id,scope_id) VALUES(%s,%s)
                       ON CONFLICT DO NOTHING""",(r["account_id"],r["scope_id"]))
    for r in record("document",getrows(demo,"document")):
        con.execute("""INSERT INTO core.document(id,title,designation,doc_type,scope_id,revision,source_kind)
                       VALUES(%s,%s,%s,%s,%s,%s,'p13-demo') ON CONFLICT(id) DO NOTHING""",
                    (r["id"],r["title"],r["designation"],r["type"],r["scope_id"],r["revision"]))
    pages={r["chunk_id"]:r for r in getrows(demo,"chunk_page")}
    for r in record("chunk",getrows(demo,"chunk")):
        meta=pages.get(r["id"],{})
        con.execute("""INSERT INTO core.document_chunk(id,document_id,body,page_no,extraction_method)
                       VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (r["id"],r["doc_id"],r["body"],meta.get("page_no"),meta.get("extraction_method","text")))
    for r in record("finance",getrows(demo,"finance")):
        con.execute("""INSERT INTO core.finance(id,title,planned,actual,scope_id)
                       VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (r["id"],r["title"],r["planned"],r["actual"],r["scope_id"]))
    for r in record("change_notice",getrows(demo,"change_notice")):
        con.execute("""INSERT INTO core.legacy_change_notice
                       (id,part_id,scope_id,new_revision,valid_from,directive,summary)
                       VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (r["id"],r["part_id"],r["scope_id"],r["new_revision"],
                     r["valid_from"],r["directive"],r["summary"]))
    for r in record("lot",getrows(demo,"lot")):
        con.execute("""INSERT INTO core.legacy_lot(id,part_id,revision_id,manufactured_at)
                       VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (r["id"],r["part_id"],r["revision_id"],r["manufactured_at"]))
    for r in record("source",getrows(corpus,"source")):
        parsed=json.loads(r["metadata_json"])
        if not isinstance(parsed,dict):raise RuntimeError("INVALID_SOURCE_METADATA")
        con.execute("""INSERT INTO core.engineering_source
            (id,source_group,name,kind,relative_path,original_sha256,original_size,
             original_url,parse_status,page_count,metadata_json,scope_id)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'engineering')
             ON CONFLICT(id) DO NOTHING""",
             (r["id"],r["source_group"],r["name"],r["kind"],r["relative_path"],
              r["original_sha256"],r["original_size"],r["original_url"],
              r["parse_status"],r["page_count"],Jsonb(parsed)))
    for r in record("source_page",getrows(corpus,"source_page")):
        con.execute("""INSERT INTO core.engineering_source_page(source_id,page,body,status)
                       VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (r["source_id"],r["page"],r["text"],r["status"]))
    for r in record("bom_line",getrows(corpus,"bom_line")):
        parsed=json.loads(r["row_json"])
        if not isinstance(parsed,dict):raise RuntimeError("INVALID_BOM_JSON")
        con.execute("""INSERT INTO core.engineering_bom_line
            (source_id,row_number,part_key,part_name,item_no,material,quantity,unit,revision,row_json)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (r["source_id"],r["row_number"],r["part_key"],r["part_name"],
             r["item_no"],r["material"],r["quantity"],r["unit"],r["revision"],Jsonb(parsed)))
    for r in record("audit_event",getrows(demo,"audit_event")):
        stamp=datetime.fromtimestamp(float(r["ts"]),tz=timezone.utc)
        con.execute("""INSERT INTO core.audit_event(occurred_at,actor,action,target,outcome)
                       VALUES(%s,%s,%s,%s,%s)""",
                    (stamp,r["actor"],"import_p13:"+r["action"],r["target"],r["outcome"]))
    for name,meta in source_meta.items():
        con.execute("""INSERT INTO core.migration_manifest(source_name,source_sha256,source_bytes,rows_json)
                       VALUES(%s,%s,%s,%s)""",
                    (name,meta["source_sha256"],meta["source_bytes"],Jsonb(counts)))
    return counts

def migrate():
    from tools.provision_core import CREDS,HOST,PORT
    RUNTIME.mkdir(exist_ok=True)
    # The transient directory is explicitly secured; backed-up SQLite may
    # contain demo passwords but none are persisted in PostgreSQL.
    with TemporaryDirectory(prefix="factory-core-sqlite-",dir=RUNTIME/"credentials") as td:
        temp=Path(td)
        # TemporaryDirectory inherits ACL from parent credentials directory.
        snaps={}
        with ExitStack() as stack:
            meta={}
            for name in WHITELIST:
                con,info=read_only_snapshot(name,temp/name)
                stack.enter_context(closing(con))
                snaps[name]=con;meta[name]=info
            password=(CREDS/"admin-password.txt").read_text(encoding="ascii")
            with psycopg.connect(host=HOST,port=PORT,dbname=DB_NAME,user="factory_pg_admin",
                                 password=password,connect_timeout=8,autocommit=True) as pg:
                current=pg.execute("""SELECT source_name,source_sha256
                        FROM core.migration_manifest""").fetchall()
                found={name:digest for name,digest in current}
                if found:
                    if found=={n:v["source_sha256"] for n,v in meta.items()}:
                        return {"status":"already_migrated","source_count":len(meta),
                                "source_sha256":found,"counts":verify_counts(pg)}
                    raise RuntimeError("PREVIOUS_MIGRATION_DIFFERS_ABORT")
                with pg.transaction():
                    counts=import_data(pg,snaps["demo.sqlite3"],
                                       snaps["engineering_corpus.sqlite3"],meta)
                    for name,n in COUNTS_EXPECTED.items():
                        if counts.get(name)!=n:raise RuntimeError("UNEXPECTED_SOURCE_COUNT_"+name)
                return {"status":"imported","source_count":len(meta),
                        "source_sha256":{k:v["source_sha256"] for k,v in meta.items()},
                        "counts":verify_counts(pg)}

def verify_counts(pg):
    keys=["subject","scope","scope_grant","document","document_chunk",
          "finance","legacy_change_notice","legacy_lot",
          "engineering_source","engineering_source_page","engineering_bom_line",
          "audit_event","migration_manifest"]
    return {key:pg.execute("SELECT COUNT(*) FROM core."+key).fetchone()[0] for key in keys}

if __name__=="__main__":
    try:print(json.dumps(migrate(),ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"status":"failed","type":type(e).__name__,"detail":str(e)[:750]},ensure_ascii=True))
        sys.exit(1)
