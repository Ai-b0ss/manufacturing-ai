"""Real PostgreSQL + cryptographically signed JWT + HTTP gateway integration tests."""
from __future__ import annotations
import json,secrets,time,unittest
from pathlib import Path
from unittest.mock import patch
import jwt
from jwt.utils import base64url_encode
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from psycopg import sql

from core06.oidc import OIDCConfig,OIDCVerifier,AuthenticationError
from core06.gateway import create_app
from core06.db_client import database_session
from tools.provision_core import psql

ISSUER="https://sso.example.invalid/realms/factory"
AUDIENCE="factory-core-api"
SUB="6c7c02b5-8e93-4f12-bd4b-8a6a92812aa3"
def keys():
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    public=private.public_key().public_numbers()
    return private,{"kty":"RSA","alg":"RS256","use":"sig","kid":"test-key-1",
                    "n":base64url_encode(public.n.to_bytes((public.n.bit_length()+7)//8,'big')).decode(),
                    "e":base64url_encode(public.e.to_bytes((public.e.bit_length()+7)//8,'big')).decode()}

class P2GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database="factory_core_p2_test_"+secrets.token_hex(4)
        with database_session("postgres",admin=True) as con:
            con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        try:
            root=Path(__file__).resolve().parent.parent
            psql(cls.database,file=root/"core06"/"schema.sql")
            psql(cls.database,file=root/"core06"/"p2_oidc.sql")
            with database_session(cls.database,admin=True) as con:
                for scope in ("open","secret"):
                    con.execute("INSERT INTO core.scope(id) VALUES(%s)",(scope,))
                for name in ("alice","bob"):
                    con.execute("INSERT INTO core.subject(id) VALUES(%s)",(name,))
                con.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES('alice','open')")
                con.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES('bob','secret')")
                for name,scope in (("public-doc","open"),("secret-doc","secret")):
                    con.execute("INSERT INTO core.document(id,title,scope_id) VALUES(%s,%s,%s)",
                                (name,name,scope))
                    con.execute("INSERT INTO core.document_chunk(id,document_id,body) VALUES(%s,%s,%s)",
                                (name+"-c",name,name+" hidden details"))
                con.execute("""INSERT INTO core.finance(id,title,planned,actual,scope_id)
                               VALUES('finance1','Budget',100,120,'secret')""")
                con.execute("""INSERT INTO core.oidc_identity(issuer,external_sub,subject_id)
                               VALUES(%s,%s,'alice')""",(ISSUER,SUB))
            cls.private,cls.public=keys()
            cls.verifier=OIDCVerifier(OIDCConfig(ISSUER,AUDIENCE,
                "https://sso.example.invalid/realms/factory/protocol/openid-connect/certs",
                required_azp="factory-web"),fetch_jwks=lambda:{"keys":[cls.public]})
            cls.app=create_app(verifier=cls.verifier,
                db_factory=lambda:database_session(cls.database))
            cls.client=TestClient(cls.app)
        except:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls,"database"):return
        with database_session("postgres",admin=True) as con:
            con.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(cls.database)))

    def token(self,**override):
        now=int(time.time())
        claims={"iss":ISSUER,"aud":AUDIENCE,"sub":SUB,"iat":now,"nbf":now-1,
                "exp":now+180,"azp":"factory-web"}
        claims.update(override)
        return jwt.encode(claims,self.private,algorithm="RS256",headers={"kid":"test-key-1"})

    def get(self,path,token=None):
        headers={"Authorization":"Bearer "+(token or self.token())}
        return self.client.get(path,headers=headers)

    def test_01_missing_bearer_denied(self):
        self.assertEqual(self.client.get("/api/v06/documents").status_code,401)
        self.assertEqual(self.client.get("/api/v06/documents",headers={"Authorization":"Basic x"}).status_code,401)

    def test_02_signed_jwt_maps_to_internal_subject(self):
        self.assertEqual(self.get("/api/v06/me").json(),{"subject":"alice"})

    def test_03_document_and_chunk_scoped(self):
        docs=self.get("/api/v06/documents")
        self.assertEqual(docs.status_code,200)
        self.assertEqual([x["id"] for x in docs.json()["documents"]],["public-doc"])
        self.assertEqual(self.get("/api/v06/documents/secret-doc").status_code,404)
        self.assertEqual(self.get("/api/v06/documents/public-doc").status_code,200)

    def test_04_search_does_not_leak_secret(self):
        self.assertEqual(self.get("/api/v06/search?q=secret-doc").json()["hits"],[])
        self.assertEqual(len(self.get("/api/v06/search?q=public-doc").json()["hits"]),1)
        self.assertEqual(self.get("/api/v06/finance").json()["metrics"],[])

    def test_05_wrong_issuer_rejected(self):
        self.assertEqual(self.get("/api/v06/me",self.token(iss="https://evil.invalid")).status_code,401)

    def test_06_wrong_audience_rejected(self):
        self.assertEqual(self.get("/api/v06/me",self.token(aud="another-api")).status_code,401)

    def test_07_expired_and_future_tokens_rejected(self):
        now=int(time.time())
        self.assertEqual(self.get("/api/v06/me",self.token(exp=now-100)).status_code,401)
        self.assertEqual(self.get("/api/v06/me",self.token(nbf=now+100)).status_code,401)
        self.assertEqual(self.get("/api/v06/me",self.token(iat=now+100)).status_code,401)

    def test_08_unsigned_token_rejected(self):
        t=jwt.encode({"sub":SUB,"iss":ISSUER},key="",algorithm="none")
        self.assertEqual(self.get("/api/v06/me",t).status_code,401)

    def test_09_wrong_signature_rejected(self):
        other,_=keys()
        t=jwt.encode({"iss":ISSUER,"aud":AUDIENCE,"sub":SUB,
                      "iat":int(time.time()),"nbf":int(time.time())-1,
                      "exp":int(time.time())+100,"azp":"factory-web"},
                     other,algorithm="RS256",headers={"kid":"test-key-1"})
        self.assertEqual(self.get("/api/v06/me",t).status_code,401)

    def test_10_azp_required(self):
        self.assertEqual(self.get("/api/v06/me",self.token(azp="untrusted-client")).status_code,401)

    def test_11_unmapped_subject_denied(self):
        self.assertEqual(self.get("/api/v06/me",self.token(sub="unmapped-uuid")).status_code,403)

    def test_12_request_identity_spoofing_ignored(self):
        response=self.client.get("/api/v06/documents?actor_id=bob&scope=secret",
                                 headers={"Authorization":"Bearer "+self.token(),
                                          "X-Actor-Id":"bob","X-Scope":"secret"})
        self.assertEqual(response.status_code,200)
        self.assertEqual([x["id"] for x in response.json()["documents"]],["public-doc"])

    def test_13_disabled_identity_denied_immediately(self):
        with database_session(self.database,admin=True) as con:
            con.execute("UPDATE core.oidc_identity SET enabled=false WHERE external_sub=%s",(SUB,))
        try:
            self.assertEqual(self.get("/api/v06/me").status_code,403)
        finally:
            with database_session(self.database,admin=True) as con:
                con.execute("UPDATE core.oidc_identity SET enabled=true WHERE external_sub=%s",(SUB,))

    def test_14_scope_revoke_denies_immediately(self):
        with database_session(self.database,admin=True) as con:
            con.execute("DELETE FROM core.scope_grant WHERE subject_id='alice' AND scope_id='open'")
        try:
            self.assertEqual(self.get("/api/v06/documents").json()["documents"],[])
            self.assertEqual(self.get("/api/v06/documents/public-doc").status_code,404)
        finally:
            with database_session(self.database,admin=True) as con:
                con.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES('alice','open')")

    def test_15_fail_closed_when_jwks_unavailable(self):
        bad=OIDCVerifier(self.verifier.config,fetch_jwks=lambda:(_ for _ in ()).throw(OSError("down")))
        client=TestClient(create_app(verifier=bad,db_factory=lambda:database_session(self.database)))
        self.assertEqual(client.get("/api/v06/me",headers={"Authorization":"Bearer "+self.token()}).status_code,401)

    def test_16_config_rejects_insecure_issuer(self):
        from core06.oidc import ConfigurationError
        with self.assertRaises(ConfigurationError):
            OIDCConfig("http://evil.invalid","api","http://evil.invalid/jwks").validate()

    def test_17_unknown_kid_rejected(self):
        t=jwt.encode({"iss":ISSUER,"aud":AUDIENCE,"sub":SUB,
                      "iat":int(time.time()),"nbf":int(time.time())-1,
                      "exp":int(time.time())+100,"azp":"factory-web"},
                     self.private,algorithm="RS256",headers={"kid":"unknown"})
        self.assertEqual(self.get("/api/v06/me",t).status_code,401)

    def test_18_no_cache_on_http_responses(self):
        r=self.get("/api/v06/documents")
        self.assertEqual(r.headers["cache-control"],"no-store, private")
        self.assertEqual(r.headers["x-content-type-options"],"nosniff")

    def test_19_query_limit_validation(self):
        self.assertEqual(self.get("/api/v06/documents?limit=10000").status_code,422)
        self.assertEqual(self.get("/api/v06/search?q=a&limit=-1").status_code,422)

    def test_20_malformed_jwt_rejected(self):
        self.assertEqual(self.get("/api/v06/me","a.b.c").status_code,401)

if __name__=="__main__":unittest.main(verbosity=2)
