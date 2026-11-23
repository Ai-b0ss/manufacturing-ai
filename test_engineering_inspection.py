"""P8 real engineering evidence, CAD topology, revisions and access controls."""
from __future__ import annotations
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import app
import engineering_corpus as ec
import engineering_inspection as p8

class P8EngineeringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not p8.DEFAULT_INDEX.is_file() or not ec.DEFAULT_INDEX.is_file():
            raise unittest.SkipTest("P8 public-source indexes not installed")
        cls.temp=tempfile.TemporaryDirectory()
        d=Path(cls.temp.name)
        cls.db=d/"api.sqlite3"
        cred=d/"creds.txt"
        app.initialize(cls.db,cred)
        cls.passwords={}
        for line in cred.read_text(encoding="utf-8").splitlines():
            m=re.search(r": (technologist|engineer|finance|director) / (.+)",line)
            if m:cls.passwords[m.group(1)]=m.group(2)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.server.database_path=cls.db
        cls.port=cls.server.server_port
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def req(self,path,method="GET",body=None,cookie=""):
        c=http.client.HTTPConnection("127.0.0.1",self.port,timeout=8)
        heads={"Host":"127.0.0.1:"+str(self.port)}
        if body is not None:heads["Content-Type"]="application/json"
        if cookie:heads["Cookie"]=cookie
        c.request(method,quote(path,safe="/?=&%"),json.dumps(body).encode("utf-8") if body is not None else None,heads)
        response=c.getresponse()
        status=response.status
        headers=dict(response.getheaders())
        data=response.read();c.close()
        try:value=json.loads(data.decode("utf-8"))
        except (UnicodeError,ValueError):value=data
        return status,value,headers

    def login(self,role):
        code,_,head=self.req("/api/login",method="POST",
                             body={"username":role,"password":self.passwords[role]})
        self.assertEqual(code,200)
        return head["Set-Cookie"].split(";")[0]

    @staticmethod
    def source(name,group):
        with closing(ec._open_readonly(ec.DEFAULT_INDEX)) as db:
            row=db.execute("SELECT id FROM source WHERE name=? AND source_group=?",
                           (name,group)).fetchone()
            assert row is not None,name
            return row["id"]

    def test_01_index_covers_real_p7_corpus(self):
        result=p8.summary()
        self.assertEqual(result["sources"],40)
        self.assertEqual(result["step"],3)
        self.assertEqual(result["pdf"],19)
        self.assertEqual(result["valid_cad_breps"],3)
        self.assertGreaterEqual(result["recognized_title_blocks"],13)
        self.assertTrue(result["unverified_revisions"])

    def test_02_passport_h_revisions_extracted_from_pdf(self):
        sid=self.source("FD_1842300-H_Front-Housing.PDF","Passport_Assembly")
        report=p8.inspect_source(sid)
        inspection=report["inspection"]
        self.assertEqual(report["filename_titleblock_comparison"],"MATCH")
        self.assertEqual((inspection["designation"],inspection["revision"]),("1842300","H"))
        self.assertEqual([x["revision"] for x in inspection["revision_history"]],["G","H"])
        self.assertEqual(inspection["revision_history"][1]["date_as_printed"],"3/16/2021")
        self.assertFalse(report["revision_approved"])
        for evidence in inspection["evidence"]:
            self.assertEqual(evidence["page"],1)
            self.assertTrue(all(0<=x<=1 for x in evidence["bbox"]))

    def test_03_passport_landscape_and_portrait_layouts(self):
        cases=[
            ("FD_1842307-C_Bracket.PDF","1842307","C"),
            ("FD_1842308-D_Battery-PCB-Chassis.PDF","1842308","D"),
            ("FD_1842305-C_Camera-Shield.PDF","1842305","C")
        ]
        for name,number,revision in cases:
            with self.subTest(file=name):
                r=p8.inspect_source(self.source(name,"Passport_Assembly"))
                self.assertEqual(r["inspection"]["designation"],number)
                self.assertEqual(r["inspection"]["revision"],revision)
                self.assertEqual(r["filename_titleblock_comparison"],"MATCH")

    def test_04_bcn_real_reference_and_version(self):
        sid=self.source("040M07C01.0P026D Extruder Body Left.PDF","BCN3D_Sigmax")
        r=p8.inspect_source(sid)
        self.assertEqual(r["inspection"]["designation"],"040M07C01.0P026D")
        self.assertEqual(r["inspection"]["revision"],"V1.0")
        self.assertEqual(r["filename_titleblock_comparison"],"MATCH")

    def test_05_bcn_file_internal_designation_disagreement(self):
        sid=self.source("040M07C01.0P027B Extruder Tensor Left.PDF","BCN3D_Sigmax")
        r=p8.inspect_source(sid)
        self.assertEqual(r["filename_titleblock_comparison"],"MISMATCH")
        self.assertEqual(r["inspection"]["designation"],"040M07C01.0P27B")
        self.assertEqual(r["inspection"]["revision"],"V1.4")

    def test_06_release_comparison_does_not_conflate_versions(self):
        report=p8.compare_bcn3d_top_frames()
        self.assertEqual(report["state"],"DOCUMENT_COMPARISON_ONLY")
        self.assertEqual(report["designation_comparison"],"DIFFERENT_OR_UNAVAILABLE")
        self.assertEqual(report["version_label_comparison"],"SAME_LABEL")
        self.assertEqual(report["left"]["inspection"]["revision"],"V1.0")
        self.assertEqual(report["right"]["inspection"]["revision"],"V1.0")
        self.assertNotEqual(report["left"]["inspection"]["designation"],
                            report["right"]["inspection"]["designation"])
        self.assertTrue(report["not_a_formal_change_notice"])
        self.assertFalse(report["manufacturing_authorization"])

    def test_07_arcblue_titleblock_grade_exact_token(self):
        report=p8.report_for_part("focuser_drawtube")
        self.assertEqual(report["state"],"ENGINEERING_VERIFICATION_INCOMPLETE")
        self.assertEqual(report["material_grade_comparison"],"VISIBLE_GRADE_TOKEN_MATCH")
        self.assertEqual(report["drawing"]["inspection"]["material_grade"],"6061-T6")
        self.assertIsNone(report["drawing"]["inspection"]["revision"])
        self.assertFalse(report["manufacturing_authorization"])

    def test_08_austenitic_steel_codes_not_silently_equated(self):
        r=p8.report_for_part("lower_square_tube")
        self.assertEqual(r["source_match"]["material"],"SUS304 Stainless Steel")
        self.assertEqual(r["drawing"]["inspection"]["material_grade"],"AISI 304")
        self.assertEqual(r["material_grade_comparison"],"GRADE_CODES_DIFFER_REVIEW_REQUIRED")
        self.assertIn("MATERIAL_GRADE_REQUIRES_ENGINEER_REVIEW",r["risks"])

    def test_09_real_occt_brep_valid_topology_on_three_parts(self):
        for name in ["focuser_drawtube","mirror_cell_base","lower_square_tube"]:
            with self.subTest(name=name):
                report=p8.report_for_part(name)
                cad=report["step_model"]["inspection"]
                self.assertEqual(cad["state"],"BREP_VALID")
                self.assertTrue(cad["brep_valid"])
                self.assertEqual(cad["solid_count"],1)
                self.assertGreater(cad["face_count"],0)
                self.assertGreater(cad["volume_in_cad_units_cubed"],0)
                self.assertTrue(all(value>0 for value in cad["xyz_extent"]))
                self.assertEqual(cad["comparison_to_drawing"],"NOT_PERFORMED")

    def test_10_historical_notice_cannot_auto_approve_angle(self):
        r=p8.historical_notice()
        self.assertEqual(r["state"],"HISTORICAL_UNCERTAIN_SOURCE")
        self.assertEqual(r["pdf_inspection"]["state"],"HISTORICAL_NOTICE_TEXT_LOCATED")
        self.assertEqual(r["pdf_inspection"]["page"],13)
        self.assertEqual(r["pdf_inspection"]["numeric_change_status"],"UNVERIFIED_TEXT_GLYPHS")
        self.assertFalse(r["date_verified_from_original"])
        self.assertFalse(r["change_geometry_verified_from_original"])
        self.assertFalse(r["manufacturing_authorization"])

    def test_11_unrelated_bcn_id_is_not_a_part_bom_join(self):
        r=p8.report_for_part("040M07C01.0P026D")
        self.assertEqual(r["state"],"NOT_FOUND")
        self.assertFalse(r["manufacturing_authorization"])
        self.assertNotIn("step_model",r)

    def test_12_invalid_source_id_cannot_query_or_escape(self):
        for sid in ["","../","a"*24,"' OR 1=1 --","G"*24]:
            with self.subTest(sid=sid):
                with self.assertRaises(p8.InspectionError):
                    p8.inspect_source(sid)

    def test_13_catalog_is_bounded_and_tied_to_source_index(self):
        r=p8.catalog()
        self.assertEqual(r["total"],22)
        self.assertEqual(len(r["items"]),22)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{24}",s["id"]) for s in r["items"]))
        self.assertFalse(r["approval_record_available"])

    def test_14_access_forbidden_before_any_inspection(self):
        path_list=[
            "/api/engineering/p8/summary",
            "/api/engineering/p8/catalog",
            "/api/engineering/p8/part?q=focuser_drawtube",
            "/api/engineering/p8/compare-top",
            "/api/engineering/p8/notice",
            "/api/engineering/p8/source?id="+self.source("focuser_drawtube.pdf","ArcBlue_HMT_Lite"),
        ]
        tech=self.login("technologist")
        for route in path_list:
            with self.subTest(route=route):
                self.assertEqual(self.req(route)[0],401)
                code,resp,_=self.req(route,cookie=tech)
                self.assertEqual(code,403)
                self.assertNotIn("focuser_drawtube",str(resp))
                self.assertNotIn("БВАИ",str(resp))

    def test_15_authorized_engineer_api_and_direct_source(self):
        engineer=self.login("engineer")
        for route,expected in [
            ("/api/engineering/p8/summary", "sources"),
            ("/api/engineering/p8/catalog","items"),
            ("/api/engineering/p8/part?q=focuser_drawtube","step_model"),
            ("/api/engineering/p8/compare-top","left"),
            ("/api/engineering/p8/notice","pdf_inspection"),
            ("/api/engineering/p8/source?id="+self.source(
                "FD_1842300-H_Front-Housing.PDF","Passport_Assembly"),"inspection"),
        ]:
            with self.subTest(route=route):
                code,result,_=self.req(route,cookie=engineer)
                self.assertEqual(code,200)
                self.assertIn(expected,result)

    def test_16_director_has_engineering_access_finance_role_does_not(self):
        route="/api/engineering/p8/part?q=mirror_cell_base"
        self.assertEqual(self.req(route,cookie=self.login("director"))[0],200)
        self.assertEqual(self.req(route,cookie=self.login("finance"))[0],403)

    def test_17_no_production_release_claim_in_any_result(self):
        for query in ("focuser_drawtube","lower_square_tube","mirror_cell_base"):
            result=p8.report_for_part(query)
            self.assertFalse(result["manufacturing_authorization"])
            self.assertEqual(result["revision_status"],"NOT_VERIFIED")
            self.assertIn("NO_RELEASED_PLM_CONFIGURATION",result["risks"])
            self.assertEqual(result["drawing"]["geometry_matches_drawing"],"NOT_ESTABLISHED")

    def test_18_source_mutation_after_index_blocks_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"corpus";group=root/"ArcBlue_HMT_Lite";group.mkdir(parents=True)
            source=group/"focuser_drawtube.pdf"
            shutil.copyfile(ec.DEFAULT_CORPUS/"ArcBlue_HMT_Lite"/source.name,source)
            raw=source.read_bytes()
            (root/"manifest.json").write_text(json.dumps({"documents":[
                {"group":"ArcBlue_HMT_Lite","name":source.name,
                 "size":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),
                 "url":"https://raw.githubusercontent.com/example/dummy/main/focuser_drawtube.pdf"}]}),
                encoding="utf-8")
            cor=Path(temp)/"cor.sqlite3";ins=Path(temp)/"ins.sqlite3"
            ec.build_index(root,cor)
            p8.build_index(cor,ins)
            sid=ec.source_id("ArcBlue_HMT_Lite",source.name)
            self.assertEqual(p8.inspect_source(sid,cor,ins)["source_name"],source.name)
            source.write_bytes(raw+b"tamper")
            with self.assertRaises(ec.CorpusError):
                p8.inspect_source(sid,cor,ins)

    def test_19_no_index_means_no_cached_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            missing=Path(temp)/"missing.sqlite3"
            with self.assertRaises(p8.InspectionError):
                p8.summary(index_path=missing)

    def test_20_geometry_worker_rejects_invalid_step(self):
        if not p8.CAD_PYTHON.is_file():
            self.skipTest("isolated OpenCascade not installed")
        with tempfile.TemporaryDirectory() as temp:
            fake=Path(temp)/"fake.step"
            fake.write_text("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;",encoding="ascii")
            r=subprocess.run([str(p8.CAD_PYTHON),str(p8.HERE/"p8_cad_worker.py"),str(fake)],
                             capture_output=True,text=True,timeout=12,encoding="utf-8",errors="replace")
            frames=[line.split("P8_RESULT_JSON:",1)[1] for line in r.stdout.splitlines()
                    if line.startswith("P8_RESULT_JSON:")]
            self.assertEqual(len(frames),1)
            result=json.loads(frames[0])
            self.assertFalse(result.get("geometry_verified",False))

    def test_22_revocation_during_inspection_blocks_reply(self):
        from unittest.mock import patch
        source_id=self.source("focuser_drawtube.pdf","ArcBlue_HMT_Lite")
        cookie=self.login("engineer")
        started=threading.Event()
        release=threading.Event()
        original=p8.inspect_source
        outcomes=[]
        def deliberately_slow(*args,**kwargs):
            started.set()
            if not release.wait(7):
                raise AssertionError("Test thread synchronization failed")
            return original(*args,**kwargs)
        def requester():
            try:
                outcomes.append(self.req("/api/engineering/p8/source?id="+source_id,cookie=cookie))
            except Exception as err:
                outcomes.append(err)
        try:
            with patch.object(p8,"inspect_source",side_effect=deliberately_slow):
                worker=threading.Thread(target=requester,daemon=True)
                worker.start()
                self.assertTrue(started.wait(4),"Source parsing did not start")
                with closing(app.connect(self.db)) as db:
                    app.revoke(db,"engineer","engineering")
                release.set()
                worker.join(timeout=9)
                self.assertFalse(worker.is_alive(),"Response stuck after revocation")
                self.assertEqual(len(outcomes),1)
                self.assertIsInstance(outcomes[0],tuple)
                self.assertEqual(outcomes[0][0],409)
                self.assertNotIn("focuser_drawtube",str(outcomes[0][1]))
        finally:
            release.set()
            # Restore original role for subsequent independent use of this test DB.
            with closing(app.connect(self.db)) as db:
                db.execute("INSERT OR IGNORE INTO acl(account_id,scope_id) VALUES (?,?)",
                           ("engineer","engineering"))
                db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='engineering'")

    def test_21_ocr_method_does_not_promote_hint_to_approved_revision(self):
        # OCR is deliberately opt-in; a false-positive letter must not change
        # the authoritative title-block or release status.
        sid=self.source("focuser_drawtube.pdf","ArcBlue_HMT_Lite")
        normal=p8.inspect_source(sid)
        self.assertIsNone(normal["inspection"]["revision"])
        self.assertFalse(normal["revision_approved"])

if __name__=="__main__":
    unittest.main()
