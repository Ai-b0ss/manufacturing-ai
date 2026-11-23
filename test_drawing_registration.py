"""P13 tests: real public vectors, ambiguity, original STEP and ACL."""
from __future__ import annotations
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import re
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import app
import drawing_registration as dr
import feature_verification as fv
import engineering_corpus as ec

FOCUSER="focuser_drawtube"
TUBE="lower_square_tube"
R41="cd2e9bca78adda13b1322aba"
R4="e1a9fb555c7e8e7491ddd820"
ANGLE="871dbd0ab432a7a88437ccd0"

class P13RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ec.DEFAULT_INDEX.is_file():
            raise unittest.SkipTest("Real source corpus unavailable")
        cls.temp=tempfile.TemporaryDirectory()
        cls.db=Path(cls.temp.name)/"test.sqlite3"
        creds=Path(cls.temp.name)/"credentials.txt"
        app.initialize(cls.db,creds)
        cls.credentials={}
        for line in creds.read_text(encoding="utf-8").splitlines():
            m=re.search(r": (technologist|engineer|finance|director) / (.+)",line)
            if m:cls.credentials[m.group(1)]=m.group(2)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.server.database_path=cls.db
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.port=cls.server.server_port
        cls.r41,cls.r41_image=dr.inspect(FOCUSER,R41)
        cls.r4,cls.r4_image=dr.inspect(FOCUSER,R4)
        cls.angular,cls.angular_image=dr.inspect(TUBE,ANGLE)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def request(self,path,method="GET",body=None,cookie=""):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=15)
        headers={"Host":f"127.0.0.1:{self.port}"}
        if cookie:headers["Cookie"]=cookie
        if body is not None:headers["Content-Type"]="application/json"
        conn.request(method,quote(path,safe="/?=&%"),
                     json.dumps(body).encode("utf-8") if body is not None else None,headers)
        response=conn.getresponse()
        code=response.status;heads=dict(response.getheaders())
        raw=response.read();conn.close()
        try:data=json.loads(raw.decode("utf-8"))
        except (ValueError,UnicodeError):data=raw
        return code,data,heads

    def login(self,role):
        code,_,headers=self.request("/api/login",method="POST",
                                    body={"username":role,"password":self.credentials[role]})
        self.assertEqual(code,200)
        return headers["Set-Cookie"].split(";")[0]

    def test_01_real_radius_leader_tip_is_original_vector(self):
        report=self.r41
        self.assertEqual(report["state"],"DRAWING_VECTOR_AND_CAD_CANDIDATES")
        self.assertEqual(report["document_geometry_state"],"LEADER_ARROW_TOUCHES_DRAWING_CONTOUR")
        self.assertEqual(report["vector_evidence"]["evidence_state"],"VECTOR_LEADER_OBSERVED")
        witness=report["vector_evidence"]["vector_witnesses"][0]
        self.assertEqual(witness["kind"],"LINE_LEADER")
        self.assertAlmostEqual(witness["arrow_tip_page_points"][0],420.9,places=1)
        self.assertAlmostEqual(witness["arrow_tip_page_points"][1],127.89,places=1)
        self.assertLess(witness["vector_contour_contact_pt"],0.05)
        self.assertTrue(witness["targets_drawing_outline"])
        self.assertNotEqual(report["drawing"]["sha256"],report["model"]["sha256"])

    def test_02_real_r415_has_only_one_numeric_candidate_but_no_mapping(self):
        report=self.r41
        self.assertEqual(report["numeric_state"],"SINGLE_NUMERIC_CANDIDATE")
        self.assertEqual(len(report["cad_candidates"]),1)
        self.assertEqual(report["cad_candidates"][0]["feature_id"],"59f93ecd4d13bec23d1133a6")
        self.assertEqual(report["cad_candidates"][0]["sample_face_indices"],[17,18,63])
        self.assertEqual(report["cad_candidates"][0]["absolute_difference"],"0")
        self.assertFalse(report["cad_candidates"][0]["correspondence_verified"])
        self.assertEqual(report["correspondence_state"],"NOT_GEOMETRICALLY_REGISTERED")
        self.assertFalse(report["manufacturing_authorization"])

    def test_03_real_r4_is_ambiguous_do_not_pick_one(self):
        report=self.r4
        self.assertEqual(report["numeric_state"],"MULTIPLE_NUMERIC_CANDIDATES")
        self.assertEqual(len(report["cad_candidates"]),2)
        self.assertEqual(set(x["measured_value"] for x in report["cad_candidates"]),{"4.0"})
        self.assertEqual(len(set(x["feature_id"] for x in report["cad_candidates"])),2)
        self.assertTrue(all(x["correspondence_verified"] is False for x in report["cad_candidates"]))
        self.assertEqual(report["document_geometry_state"],"LEADER_ARROW_TOUCHES_DRAWING_CONTOUR")
        self.assertFalse(report["manufacturing_authorization"])

    def test_04_real_angle_arc_has_two_distinct_termini_and_45_segments(self):
        report=self.angular
        self.assertEqual(report["document_geometry_state"],"ANGLE_ARC_BETWEEN_TWO_ARROWS_OBSERVED")
        self.assertEqual(report["vector_evidence"]["evidence_state"],"VECTOR_ANGLE_ARC_OBSERVED")
        arc=report["vector_evidence"]["vector_witnesses"][0]
        self.assertEqual(arc["kind"],"ANGULAR_ARC")
        self.assertEqual(arc["arc_segment_count"],45)
        self.assertNotEqual(arc["arrow_tip_normalized"],arc["second_arrow_tip_normalized"])
        self.assertEqual(report["numeric_state"],"SINGLE_NUMERIC_CANDIDATE")
        self.assertEqual(report["cad_candidates"][0]["feature_id"],"27a1dea11cff2d06aedca694")
        self.assertEqual(report["cad_candidates"][0]["absolute_difference"],"0.00015")

    def test_05_wrong_axis_produces_no_false_angle_candidate(self):
        report,_=dr.inspect(TUBE,ANGLE,"y")
        self.assertEqual(report["numeric_state"],"NO_NUMERIC_CANDIDATE")
        self.assertEqual(report["cad_candidates"],[])
        self.assertEqual(report["document_geometry_state"],"ANGLE_ARC_BETWEEN_TWO_ARROWS_OBSERVED")

    def test_06_valid_png_previews_are_distinct_and_bounded(self):
        images=(self.r41_image,self.r4_image,self.angular_image)
        hashes={hashlib.sha256(data).hexdigest() for data in images}
        self.assertEqual(len(hashes),3)
        for data in images:
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            width,height=struct.unpack(">II",data[16:24])
            self.assertTrue(100<width<2000 and 100<height<1600)
            self.assertLess(width*height,1_150_001)

    def test_07_http_engineer_can_read_original_vector_and_candidate_data(self):
        cookie=self.login("engineer")
        path="/api/engineering/p13/associations?q="+FOCUSER+"&annotation="+R41
        code,item,headers=self.request(path,cookie=cookie)
        self.assertEqual(code,200)
        self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertEqual(item["numeric_state"],"SINGLE_NUMERIC_CANDIDATE")
        self.assertFalse(item["manufacturing_authorization"])
        pngpath="/api/engineering/p13/vectors?q="+TUBE+"&annotation="+ANGLE
        code,image,headers=self.request(pngpath,cookie=cookie)
        self.assertEqual(code,200)
        self.assertEqual(headers["Content-Type"],"image/png")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_08_http_technologist_and_anonymous_denied(self):
        path="/api/engineering/p13/associations?q="+FOCUSER+"&annotation="+R41
        self.assertEqual(self.request(path)[0],401)
        cookie=self.login("technologist")
        for route in (path,"/api/engineering/p13/vectors?q="+TUBE+"&annotation="+ANGLE):
            with self.subTest(route=route):
                code,body,_=self.request(route,cookie=cookie)
                self.assertEqual(code,403)
                self.assertNotIn("source_sha256",str(body))

    def test_09_unknown_id_rejected_without_other_part_cross_join(self):
        cookie=self.login("engineer")
        for raw,code in (("../etc/passwd",422),("0"*24,404),(ANGLE,404)):
            with self.subTest(annotation=raw):
                route="/api/engineering/p13/associations?q="+FOCUSER+"&annotation="+raw
                status,_,_=self.request(route,cookie=cookie)
                self.assertEqual(status,code)

    def test_10_invalid_axis_and_query_denied(self):
        cookie=self.login("engineer")
        self.assertEqual(self.request("/api/engineering/p13/vectors?q="+TUBE+
                                      "&annotation="+ANGLE+"&axis=../x",
                                      cookie=cookie)[0],422)
        self.assertEqual(self.request("/api/engineering/p13/vectors?q=nonsense-part-zzzz"+
                                      "&annotation="+ANGLE,cookie=cookie)[0],404)

    def test_11_changed_pdf_source_fails_before_worker(self):
        actual=fv.candidates(FOCUSER)
        with patch.object(dr.fv,"candidates",return_value=actual):
            raw,suffix=ec.original_file(actual["drawing"]["id"])
            with patch.object(dr.ec,"original_file",return_value=(raw+b"tampered",suffix)):
                with self.assertRaisesRegex(dr.RegistrationError,"DRAWING_SOURCE_CHANGED"):
                    dr.inspect(FOCUSER,R41)

    def test_12_wrong_source_hash_in_worker_output_fail_closed(self):
        report=fv.candidates(FOCUSER)
        annotated=report["drawing_annotations"][0]
        pdf=report["drawing"]
        with patch.object(dr.subprocess,"run") as fake:
            fake.return_value.returncode=0
            fake.return_value.stdout=b'P13_VECTOR_JSON:{"source_sha256":"0"}\n'
            with self.assertRaisesRegex(dr.RegistrationError,"UNTRUSTED_VECTOR_EVIDENCE"):
                dr._vector_from_source(pdf,annotated,ec.DEFAULT_INDEX)

    def test_13_parallel_vector_worker_limit(self):
        self.assertTrue(dr.RENDER_LIMIT.acquire(False))
        self.assertTrue(dr.RENDER_LIMIT.acquire(False))
        try:
            with self.assertRaisesRegex(dr.RegistrationError,"VECTOR_WORKERS_BUSY"):
                dr.inspect(FOCUSER,R41)
        finally:
            dr.RENDER_LIMIT.release()
            dr.RENDER_LIMIT.release()

    def test_14_revoked_scope_during_vector_render_denies_late_pixels(self):
        cookie=self.login("engineer")
        started=threading.Event();release=threading.Event()
        observed=[]
        def delayed(*args,**kwargs):
            started.set()
            if not release.wait(8):raise RuntimeError("coordinator wait failed")
            return self.r41_image
        def request():
            try:
                observed.append(self.request("/api/engineering/p13/vectors?q="+FOCUSER+
                    "&annotation="+R41,cookie=cookie))
            except Exception as exc:observed.append(exc)
        try:
            with patch.object(dr,"preview",side_effect=delayed):
                t=threading.Thread(target=request,daemon=True)
                t.start();self.assertTrue(started.wait(5))
                with closing(app.connect(self.db)) as db:
                    app.revoke(db,"engineer","engineering")
                release.set();t.join(timeout=10)
                self.assertFalse(t.is_alive())
                self.assertEqual(observed[0][0],409)
        finally:
            release.set()
            with closing(app.connect(self.db)) as db:
                db.execute("INSERT OR IGNORE INTO acl(account_id,scope_id) VALUES(?,?)",
                           ("engineer","engineering"))
                db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='engineering'")

    def test_15_original_pdf_and_step_hashes_survive_api(self):
        self.assertEqual(len(self.r41["drawing"]["sha256"]),64)
        self.assertEqual(len(self.r41["model"]["sha256"]),64)
        self.assertEqual(self.r41["vector_evidence"]["source_sha256"],
                         self.r41["drawing"]["sha256"])
        self.assertFalse(self.r41["human_verification_required"] is False)
        self.assertEqual(self.r41["projection_alignment"],"NOT_ESTABLISHED")

    def test_16_real_vector_worker_does_not_modify_pdf_bytes(self):
        original=fv.candidates(FOCUSER)
        source_id=original["drawing"]["id"]
        raw,_=ec.original_file(source_id)
        before=hashlib.sha256(raw).digest()
        dr.preview(FOCUSER,R41)
        raw_after,_=ec.original_file(source_id)
        self.assertEqual(before,hashlib.sha256(raw_after).digest())

if __name__=="__main__":
    unittest.main()
