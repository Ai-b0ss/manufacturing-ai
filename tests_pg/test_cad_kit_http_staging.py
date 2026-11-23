"""Real Keycloak-signed HTTP + PostgreSQL RLS + local STEP/PDF parser.
Creates unique scratch DB; never changes live factory_core_v06.
"""
from __future__ import annotations
import os,json,uuid,hashlib
from pathlib import Path
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
os.environ["FACTORY_P2_APP_ORIGIN"]="http://localhost:19139"
import p2_gateway.app as p2module
import unified_app
from tools.p3_gateway_smoke import mint
from p2_gateway.security import verify_access_token
from tests_pg.test_private_cad_bundle import package
from core06.private_cad_bundle import unpack_bundle
from core06.db_client import _creds_dir, _pg_port

ROOT=Path(__file__).resolve().parents[1]
DB="factory_cad_http_"+uuid.uuid4().hex[:12]

def db(name=DB,admin=True):
 return psycopg.connect(host="127.0.0.1",port=_pg_port(),dbname=name,
    user="factory_pg_admin" if admin else "factory_gateway",
    password=(_creds_dir()/("admin-password.txt" if admin else "gateway-password.txt")).read_text("ascii"),
    autocommit=True,connect_timeout=8)

def test():
    engineer_token=mint("engineer")
    finance_token=mint("finance")
    principal=verify_access_token(engineer_token)
    with db("factory_core_v06") as con:
        con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB)))
    prev=p2module.database_session
    try:
        with db() as con:
            for fn in ("schema.sql","p2_oidc.sql","ingestion_v1.sql",
                       "ingestion_v2.sql","ingestion_v3_purge.sql","cad_kit_v1.sql"):
                con.execute((ROOT/"core06"/fn).read_text("utf-8-sig"))
            con.execute("INSERT INTO core.subject(id) VALUES('engineer'),('finance')")
            con.execute("INSERT INTO core.scope(id) VALUES('engineering')")
            con.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES('engineer','engineering')")
            con.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES('engineer','engineering')")
            con.execute("INSERT INTO core.oidc_identity(issuer,external_sub,subject_id) VALUES(%s,%s,'engineer')",
                        (principal.issuer,principal.external_sub))
            finance=verify_access_token(finance_token)
            con.execute("INSERT INTO core.oidc_identity(issuer,external_sub,subject_id) VALUES(%s,%s,'finance')",
                        (finance.issuer,finance.external_sub))
        p2module.database_session=lambda admin=False:db(DB,admin)
        base="http://localhost:19139"
        eng={"Authorization":"Bearer "+engineer_token,
             "Content-Type":"application/octet-stream"}
        fin={"Authorization":"Bearer "+finance_token,
             "Content-Type":"application/octet-stream"}
        kit=package()
        original_files,_=unpack_bundle(kit)
        with TestClient(unified_app.app,base_url=base) as c:
            assert c.get("/unified/api/engineering/kits").status_code==401
            assert c.post("/unified/api/engineering/kits",content=kit).status_code==401
            r=c.post("/unified/api/engineering/kits",content=kit,
                headers={**fin,"Idempotency-Key":"cad-finance-upload-001"})
            assert r.status_code==403,r.text[:220]
            r=c.post("/unified/api/engineering/kits",content=kit,
                headers={**eng,"Idempotency-Key":"cad-real-stage-first-01","X-Actor-Id":"director"})
            assert r.status_code==201,r.text[:260]
            first=r.json()
            assert first["revision_no"]==1 and first["manufacturing_authorization"] is False
            kid=first["kit_id"]
            assert c.get("/unified/api/engineering/kits",headers=fin).json()["items"]==[]
            overview=c.get("/unified/api/engineering/kits",headers=eng)
            assert overview.status_code==200 and len(overview.json()["items"])==1
            revisions=c.get(f"/unified/api/engineering/kits/{kid}/revisions",headers=eng)
            assert revisions.status_code==200 and len(revisions.json()["revisions"])==1
            for role,file in original_files.items():
                r=c.get(f"/unified/api/engineering/kits/{kid}/revisions/1/source/{role}",headers=eng)
                assert r.status_code==200 and r.content==file.data
                assert hashlib.sha256(r.content).hexdigest()==file.sha256
                assert "attachment;" in r.headers.get("Content-Disposition","")
                assert "no-store" in r.headers.get("Cache-Control","")
                assert c.get(f"/unified/api/engineering/kits/{kid}/revisions/1/source/{role}",headers=fin).status_code==404
            dup=c.post("/unified/api/engineering/kits",content=kit,
                headers={**eng,"Idempotency-Key":"cad-real-stage-first-01"})
            assert dup.status_code==200 and dup.json()["reused"] is True
            stale=c.post("/unified/api/engineering/kits",content=kit,
                headers={**eng,"Idempotency-Key":"cad-real-stage-stale-01"})
            assert stale.status_code==409,stale.text[:250]
            v2=c.post("/unified/api/engineering/kits",content=kit,
                headers={**eng,"Idempotency-Key":"cad-real-stage-second-01",
                         "X-Kit-Id":kid,"X-Expected-Revision":"1"})
            assert v2.status_code==201,v2.text[:250]
            assert v2.json()["revision_no"]==2
            assert len(c.get(f"/unified/api/engineering/kits/{kid}/revisions",headers=eng).json()["revisions"])==2
            with db() as admin:
                assert admin.execute("SELECT count(*) FROM core.cad_kit_revision").fetchone()==(2,)
                admin.execute("DELETE FROM core.scope_grant WHERE subject_id='engineer' AND scope_id='engineering'")
            assert c.get("/unified/api/engineering/kits",headers=eng).json()["items"]==[]
            assert c.get(f"/unified/api/engineering/kits/{kid}/revisions/1/source/bom",headers=eng).status_code==404
            blocked=c.post("/unified/api/engineering/kits",content=kit,
                headers={**eng,"Idempotency-Key":"cad-real-stage-denied-01",
                         "X-Kit-Id":kid,"X-Expected-Revision":"2"})
            assert blocked.status_code==403
        print("PASS CAD HTTP: signed OIDC, real parsers, 2 revisions, 3 byte-exact originals, idempotency, stale reject, finance deny, revoke")
    finally:
        p2module.database_session=prev
        with db("factory_core_v06") as con:
            con.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB)))

if __name__=="__main__":test()
