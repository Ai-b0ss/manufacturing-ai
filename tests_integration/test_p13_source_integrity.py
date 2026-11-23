"""End-to-end integrity/authorization of original P13 BOM/PDF/STEP bytes.

All tests read only. Use synthetic source corpus, verified Keycloak JWT and
the actual unified HTTP endpoint. Do not print file contents or tokens.
"""
from __future__ import annotations
import hashlib,unittest,httpx
from tools.p3_gateway_smoke import mint

BASE="http://localhost:19139"

class EngineeringSourceIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=httpx.Client(base_url=BASE,timeout=70,trust_env=False)
        cls.tokens={r:mint(r) for r in ("engineer","technologist","finance","director")}
        resp=cls.client.get("/unified/api/engineering/ask",params={"q":"focuser_drawtube"},
             headers={"Authorization":"Bearer "+cls.tokens["engineer"]})
        assert resp.status_code==200, "Engineering linked source request unavailable"
        cls.sources=resp.json().get("sources",[])
    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tokens.clear()
    def req(self,role,source):
        headers={} if role=="anonymous" else {"Authorization":"Bearer "+self.tokens[role]}
        return self.client.get("/api/engineering/file",
                               params={"id":source["id"]},headers=headers)

    def test_01_exact_linked_bom_pdf_step_evidence(self):
        self.assertEqual([x["role"] for x in self.sources],
                         ["BOM","DRAWING","MODEL"])
        self.assertEqual(len(set(x["id"] for x in self.sources)),3)
        self.assertEqual(len(set(x["sha256"] for x in self.sources)),3)

    def test_02_bytes_match_original_sha256(self):
        for x in self.sources:
            with self.subTest(source=x["name"]):
                response=self.req("engineer",x)
                self.assertEqual(response.status_code,200)
                self.assertGreater(len(response.content),100)
                self.assertEqual(hashlib.sha256(response.content).hexdigest(),x["sha256"])
                self.assertEqual(response.headers.get("x-legacy-synthetic-data"),"true")
                self.assertEqual(response.headers.get("content-type"),"application/octet-stream")
                self.assertNotIn("set-cookie",response.headers)
                self.assertEqual(response.headers.get("cache-control"),"no-store, max-age=0")

    def test_03_denied_roles_get_no_source_bytes(self):
        for role in ("technologist","finance"):
            for x in self.sources:
                with self.subTest(role=role,file=x["name"]):
                    r=self.req(role,x)
                    self.assertEqual(r.status_code,403)
                    self.assertNotEqual(hashlib.sha256(r.content).hexdigest(),x["sha256"])

    def test_04_anonymous_cannot_download(self):
        for x in self.sources:
            with self.subTest(file=x["name"]):
                self.assertEqual(self.req("anonymous",x).status_code,401)

    def test_05_director_can_read_exact_originals(self):
        for x in self.sources:
            with self.subTest(file=x["name"]):
                r=self.req("director",x)
                self.assertEqual(r.status_code,200)
                self.assertEqual(hashlib.sha256(r.content).hexdigest(),x["sha256"])

    def test_06_nonexistent_ids_fail_closed(self):
        for role in ("engineer","director"):
            r=self.client.get("/api/engineering/file",params={"id":"not-a-real-source"},
                headers={"Authorization":"Bearer "+self.tokens[role]})
            self.assertEqual(r.status_code,404)

    def test_07_no_query_parameter_duplication(self):
        x=self.sources[0]
        r=self.client.get("/api/engineering/file",params=[("id",x["id"]),("id",self.sources[1]["id"])],
            headers={"Authorization":"Bearer "+self.tokens["engineer"]})
        self.assertEqual(r.status_code,422)

    def test_08_no_unlisted_paths_or_unsafe_query(self):
        headers={"Authorization":"Bearer "+self.tokens["engineer"]}
        for path in ("internal","p13/private","file/index"):
            with self.subTest(path=path):
                r=self.client.get("/api/engineering/"+path,headers=headers)
                self.assertEqual(r.status_code,404)
        r=self.client.get("/api/engineering/ask",params={"extra":"x"},
                          headers=headers)
        self.assertEqual(r.status_code,422)

if __name__=="__main__":
    unittest.main(verbosity=2)
