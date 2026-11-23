"""Actual PostgreSQL RLS, immutable CAD originals and optimistic version tests.
Creates a scratch database and drops it in finally. No live DB mutation.
"""
from __future__ import annotations
import hashlib,json,uuid
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from core06.private_cad_bundle import unpack_bundle
from core06.db_client import _creds_dir, _pg_port
from tests_pg.test_private_cad_bundle import package

ROOT=Path(__file__).resolve().parents[1]
DB="factory_cad_stage_"+uuid.uuid4().hex[:12]

def conn(name="factory_core_v06",admin=True):
    return psycopg.connect(host="127.0.0.1",port=_pg_port(),dbname=name,
       user="factory_pg_admin" if admin else "factory_gateway",
       password=(_creds_dir()/("admin-password.txt" if admin else "gateway-password.txt")).read_text("ascii"),
       connect_timeout=8,autocommit=True)

def save(db,actor,key,payload,report,kit=None,expected=0):
    with db.transaction():
        db.execute("SELECT set_config('factory.actor_id',%s,true)",(actor,))
        return db.execute("""SELECT core.save_cad_kit_revision(
            %s,'engineering','focuser_drawtube',%s,%s,%s,%s::jsonb)""",
            (key,kit,expected,payload,json.dumps(report,ensure_ascii=False))).fetchone()[0]

def test():
    payload=package()
    files,part=unpack_bundle(payload)
    report={"schema":"factory_private_cad_bundle_v1","part":part,
       "state":"REVIEW_REQUIRED","manufacturing_authorization":False,
       "sources":[{"role":f.role,"name":f.name,"sha256":f.sha256,
                   "size":len(f.data)} for f in files.values()]}
    with conn() as db:
        before=db.execute("SELECT count(*) FROM core.document").fetchone()[0]
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB)))
    try:
        with conn(DB) as db:
            for file in ("schema.sql","ingestion_v1.sql","ingestion_v2.sql",
                         "ingestion_v3_purge.sql","cad_kit_v1.sql"):
                db.execute((ROOT/"core06"/file).read_text("utf-8-sig"))
            db.execute("INSERT INTO core.subject(id) VALUES('engineer'),('finance')")
            db.execute("INSERT INTO core.scope(id) VALUES('engineering')")
            db.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES('engineer','engineering')")
            db.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES('engineer','engineering')")
            policies=db.execute("""SELECT relname,relrowsecurity,relforcerowsecurity
                FROM pg_class WHERE relname IN ('cad_kit','cad_kit_revision','cad_kit_request')""").fetchall()
            assert len(policies)==3 and all(row[1:] == (True,True) for row in policies),policies
        with conn(DB,admin=False) as db:
            try:
                db.execute("INSERT INTO core.cad_kit(id,scope_id,part,part_key,created_by) VALUES('x','engineering','X','x','engineer')")
                raise AssertionError("gateway had direct INSERT")
            except psycopg.errors.InsufficientPrivilege:
                pass
            try:
                save(db,"finance","idemp-finance-attempt-01",payload,report)
                raise AssertionError("finance wrote CAD")
            except psycopg.errors.InsufficientPrivilege:
                pass
            first=save(db,"engineer","idemp-cad-first-0001",payload,report)
            assert first["revision_no"]==1 and first["reused"] is False
            repeated=save(db,"engineer","idemp-cad-first-0001",payload,report)
            assert repeated["revision_id"]==first["revision_id"] and repeated["reused"] is True
            try:
                save(db,"engineer","idemp-cad-first-0001",payload+b'x',report)
                raise AssertionError("changed payload accepted for same key")
            except psycopg.errors.UniqueViolation:pass
            try:
                save(db,"engineer","idemp-cad-other-0001",payload,report)
                raise AssertionError("duplicate initial revision accepted")
            except psycopg.errors.UniqueViolation:pass
            second=save(db,"engineer","idemp-cad-second-0001",payload,report,
                        kit=first["kit_id"],expected=1)
            assert second["revision_no"]==2 and second["revision_id"]!=first["revision_id"]
            try:
                save(db,"engineer","idemp-cad-stale-000001",payload,report,
                     kit=first["kit_id"],expected=1)
                raise AssertionError("stale write accepted")
            except psycopg.errors.UniqueViolation:pass
            with db.transaction():
                db.execute("SELECT set_config('factory.actor_id','engineer',true)")
                original=db.execute("SELECT package,package_sha256,report FROM core.cad_kit_revision WHERE id=%s",
                                    (first["revision_id"],)).fetchone()
                assert original and bytes(original[0])==payload
                assert original[1]==hashlib.sha256(payload).hexdigest()
                src,_=unpack_bundle(bytes(original[0]))
                assert {k:v.sha256 for k,v in src.items()}=={k:v.sha256 for k,v in files.items()}
                assert db.execute("SELECT count(*) FROM core.cad_kit_revision").fetchone()[0]==2
            with db.transaction():
                db.execute("SELECT set_config('factory.actor_id','finance',true)")
                assert db.execute("SELECT count(*) FROM core.cad_kit_revision").fetchone()[0]==0
            # Abort an insert within a transaction: revision counter is unchanged.
            try:
                with db.transaction():
                    db.execute("SELECT set_config('factory.actor_id','engineer',true)")
                    db.execute("SELECT core.save_cad_kit_revision(%s,%s,%s,%s,%s,%s,%s::jsonb)",
                        ("idemp-cad-rollback-0001","engineering",part,first["kit_id"],2,
                         payload,json.dumps(report)))
                    raise RuntimeError("force transaction abort")
            except RuntimeError:pass
        with conn(DB) as db:
            assert db.execute("SELECT latest_revision FROM core.cad_kit WHERE id=%s",
                              (first["kit_id"],)).fetchone()==(2,)
            assert db.execute("SELECT count(*) FROM core.cad_kit_request").fetchone()[0]==2
            db.execute("DELETE FROM core.scope_grant WHERE subject_id='engineer'")
        with conn(DB,admin=False) as db:
            with db.transaction():
                db.execute("SELECT set_config('factory.actor_id','engineer',true)")
                assert db.execute("SELECT count(*) FROM core.cad_kit_revision").fetchone()==(0,)
            try:
                save(db,"engineer","idemp-cad-denied-0001",payload,report,
                     kit=first["kit_id"],expected=2)
                raise AssertionError("revoked actor wrote")
            except psycopg.errors.InsufficientPrivilege:pass
        with conn() as db:
            assert db.execute("SELECT count(*) FROM core.document").fetchone()[0]==before
        print("PASS isolated CAD: RLS deny, immutable package and three SHA, 2 revisions, OCC conflict, idempotency, rollback, revoke")
    finally:
        with conn() as db:
            db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB)))

if __name__=="__main__":test()
