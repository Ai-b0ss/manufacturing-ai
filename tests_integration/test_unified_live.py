"""Real Keycloak + unified P13 + PostgreSQL RLS cross-role integration.

Read-only synthetic corpus tests. The original P13 and Keycloak remain
unmodified. A temporary bearer token is never logged.
"""
from __future__ import annotations
import unittest
import httpx
from core06.db_client import database_session
from tools.p3_gateway_smoke import mint

API="http://localhost:19139"
ROLES=("engineer","technologist","finance","director")
EXPECTED_DOCUMENTS={"engineer":11,"technologist":9,"finance":10,"director":13}
ENG_ALLOWED={"engineer","director"}
FIN_ALLOWED={"finance","director"}

class UnifiedLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=httpx.Client(base_url=API,trust_env=False,timeout=70)
        cls.tokens={role:mint(role) for role in ROLES}
        with database_session(admin=True) as con:
            row=con.execute("""SELECT id FROM core.document
                  WHERE scope_id='executive' ORDER BY id LIMIT 1""").fetchone()
            cls.secret_doc=row[0]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tokens.clear()

    def get(self,role,path,params=None,headers=None):
        hdr={"Authorization":"Bearer "+self.tokens[role]}
        if headers:hdr.update(headers)
        return self.client.get(path,params=params,headers=hdr)

    def test_01_reject_anonymous_on_all_data_routes(self):
        for url in ["/unified/api/me","/unified/api/documents",
                    "/unified/api/metrics","/unified/api/engineering/summary"]:
            with self.subTest(url=url):
                response=self.client.get(url)
                self.assertEqual(response.status_code,401)

    def test_02_all_role_subjects_verified(self):
        for role in ROLES:
            with self.subTest(role=role):
                response=self.get(role,"/unified/api/me")
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()["id"],role)
                self.assertEqual(response.json()["identity_provider"],"keycloak")

    def test_03_postgres_document_acl_for_each_role(self):
        for role,count in EXPECTED_DOCUMENTS.items():
            with self.subTest(role=role):
                r=self.get(role,"/unified/api/documents")
                self.assertEqual(r.status_code,200)
                self.assertEqual(len(r.json()["items"]),count)

    def test_04_executive_document_non_interference(self):
        for role in ROLES:
            r=self.get(role,"/unified/api/document",{"id":self.secret_doc})
            self.assertEqual(r.status_code,200 if role=="director" else 404,
                             role+" leaked or lost executive doc")

    def test_05_finance_acl(self):
        for role in ROLES:
            r=self.get(role,"/unified/api/metrics")
            self.assertEqual(r.status_code,200)
            self.assertEqual(len(r.json()["items"])>0,role in FIN_ALLOWED)

    def test_06_engineering_requires_complete_pg_scope(self):
        for role in ROLES:
            r=self.get(role,"/unified/api/engineering/summary")
            self.assertEqual(r.status_code,200 if role in ENG_ALLOWED else 403,
                             role+" engineering policy mismatch")
            if r.status_code==200:self.assertEqual(r.json().get("sources"),40)

    def test_07_p8_p9_and_p12_reuse_real_p13_readonly_engine(self):
        for route in ("p8/catalog","p9/catalog","p12/audit"):
            with self.subTest(route=route):
                r=self.get("engineer","/unified/api/engineering/"+route)
                self.assertEqual(r.status_code,200,r.text[:150])
                self.assertEqual(r.headers.get("x-legacy-synthetic-data"),"true")

    def test_08_cad_file_id_without_pg_grant_fails_closed(self):
        for role in ROLES:
            r=self.get(role,"/api/engineering/file",{"id":"injected-secret-id"})
            self.assertIn(r.status_code,(403,404))

    def test_09_unknown_engineering_route_denied(self):
        r=self.get("engineer","/unified/api/engineering/private-config")
        self.assertEqual(r.status_code,404)
        traversal=self.get("engineer","/unified/api/engineering/../settings")
        # HTTP clients normalize dot-segments before routing; a 405 means no
        # GET handler exists at the normalized path, which is fail-closed.
        self.assertIn(traversal.status_code,(404,405,422))

    def test_10_forged_identity_header_does_not_escalate(self):
        r=self.get("engineer","/unified/api/document",{"id":self.secret_doc},
            headers={"X-Actor-Id":"director","X-Scope":"executive"})
        self.assertEqual(r.status_code,404)

    def test_11_legacy_mutations_disabled(self):
        for route in ("/unified/api/demo-login","/unified/api/upload",
                      "/unified/api/engineering/p11/finding"):
            with self.subTest(route=route):
                r=self.client.post(route,json={"role":"director"},
                    headers={"Authorization":"Bearer "+self.tokens["engineer"]})
                self.assertEqual(r.status_code,501)

    def test_12_question_without_csrf_denied_even_with_token(self):
        r=self.client.post("/unified/api/ask",json={"question":"чертёж"},
          headers={"Authorization":"Bearer "+self.tokens["engineer"]})
        self.assertEqual(r.status_code,403)

    def test_13_bad_query_values_are_rejected(self):
        q=self.get("engineer","/unified/api/engineering/p8/part",
                   params={"q":"a"*251})
        self.assertEqual(q.status_code,422)
        dup=self.get("engineer","/unified/api/engineering/p8/part",
                     params=[("q","a"),("q","b")])
        self.assertEqual(dup.status_code,422)

if __name__=="__main__":unittest.main(verbosity=2)
