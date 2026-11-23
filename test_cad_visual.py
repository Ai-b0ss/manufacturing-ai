"""P12 real on-disk CAD geometry, renderer authenticity, imaging and ACL."""
from __future__ import annotations
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import app
import cad_visual
import engineering_corpus as ec
import feature_verification as fv

FOCUSER="focuser_drawtube"
CYLINDER="59f93ecd4d13bec23d1133a6"
TUBE="lower_square_tube"
PLANE="27a1dea11cff2d06aedca694"

class P12CADVisualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ec.DEFAULT_INDEX.is_file():
            raise unittest.SkipTest("P7 real corpus unavailable")
        cls.tmp=tempfile.TemporaryDirectory()
        temp=Path(cls.tmp.name)
        cls.db=temp/"demo.sqlite3"
        creds=temp/"credentials.txt"
        app.initialize(cls.db,creds)
        cls.credentials={}
        for line in creds.read_text(encoding="utf-8").splitlines():
            m=re.search(r": (technologist|engineer|finance|director) / (.+)",line)
            if m:cls.credentials[m.group(1)]=m.group(2)
        cls.srv=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.srv.database_path=cls.db
        cls.thread=threading.Thread(target=cls.srv.serve_forever,daemon=True)
        cls.thread.start()
        cls.port=cls.srv.server_port
        cls.cylinder_report=fv.candidates(FOCUSER)
        cls.tube_report=fv.candidates(TUBE)
        cls.focuser_render=cad_visual.render(FOCUSER,CYLINDER,"iso")
        cls.tube_render=cad_visual.render(TUBE,PLANE,"iso")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown();cls.srv.server_close();cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def req(self,path,method="GET",body=None,cookie=""):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=12)
        headers={"Host":f"127.0.0.1:{self.port}"}
        if body is not None:headers["Content-Type"]="application/json"
        if cookie:headers["Cookie"]=cookie
        conn.request(method,quote(path,safe="/?=&%"),
                     json.dumps(body).encode("utf-8") if body is not None else None,
                     headers)
        response=conn.getresponse()
        status=response.status;headers=dict(response.getheaders())
        raw=response.read();conn.close()
        try:content=json.loads(raw.decode("utf-8"))
        except (ValueError,UnicodeError):content=raw
        return status,content,headers

    def login(self,role):
        code,_,head=self.req("/api/login",method="POST",
                             body={"username":role,"password":self.credentials[role]})
        self.assertEqual(code,200)
        return head["Set-Cookie"].split(";")[0]

    def test_01_real_focuser_brep_face_indices_match_p10(self):
        selected=next(f for f in self.cylinder_report["features"] if f["feature_id"]==CYLINDER)
        self.assertEqual(selected["sample_face_indices"],[17,18,63])
        self.assertEqual(selected["face_occurrences"],3)
        self.assertEqual(self.focuser_render[:8],b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II",self.focuser_render[16:24]),(1000,660))

    def test_02_real_tube_angled_planes_match_p10(self):
        selected=next(f for f in self.tube_report["features"] if f["feature_id"]==PLANE)
        self.assertEqual(selected["sample_face_indices"],[3,4,7,8])
        self.assertEqual(selected["face_occurrences"],4)
        self.assertEqual(struct.unpack(">II",self.tube_render[16:24]),(1000,660))
        self.assertNotEqual(hashlib.sha256(self.focuser_render).digest(),
                            hashlib.sha256(self.tube_render).digest())

    def test_03_raster_contains_genuine_orange_feature(self):
        # The OCP worker environment has Pillow; analyze genuine rendered pixels,
        # not just a valid-looking PNG header.
        script="""from PIL import Image
import io,sys,json
im=Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('RGB')
pix=im.getdata()
orange=sum(r>145 and r>g*1.55 and r>b*1.65 and g>25 for r,g,b in pix)
blue=sum(b>r*1.04 and b>g*.90 and r<215 for r,g,b in pix)
print(json.dumps({"orange":orange,"blue":blue}))
"""
        for blob in (self.focuser_render,self.tube_render):
            with self.subTest(bytes=len(blob)):
                check=subprocess.run([str(cad_visual.p8.CAD_PYTHON),"-c",script],
                    input=blob,capture_output=True,timeout=10)
                self.assertEqual(check.returncode,0,check.stderr)
                result=json.loads(check.stdout)
                self.assertGreater(result["orange"],200)
                self.assertGreater(result["blue"],1000)

    def test_04_camera_views_affect_real_image(self):
        front=cad_visual.render(FOCUSER,CYLINDER,"front")
        self.assertNotEqual(front,self.focuser_render)
        self.assertEqual(struct.unpack(">II",front[16:24]),(1000,660))

    def test_05_http_engineer_binary_png_and_provenance(self):
        cookie=self.login("engineer")
        route="/api/engineering/p12/render?q="+FOCUSER+"&feature="+CYLINDER+"&view=iso"
        code,raw,headers=self.req(route,cookie=cookie)
        self.assertEqual(code,200)
        self.assertEqual(headers["Content-Type"],"image/png")
        self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertEqual(raw[:8],b"\x89PNG\r\n\x1a\n")
        code,item,_=self.req("/api/engineering/p12/feature?q="+FOCUSER+
                            "&feature="+CYLINDER,cookie=cookie)
        self.assertEqual(code,200)
        self.assertEqual(item["feature"]["sample_face_indices"],[17,18,63])
        self.assertFalse(item["feature_mapping_verified"])
        self.assertFalse(item["manufacturing_authorization"])

    def test_06_anonymous_and_technologist_cannot_discover_geometry(self):
        route="/api/engineering/p12/render?q="+FOCUSER+"&feature="+CYLINDER
        self.assertEqual(self.req(route)[0],401)
        cookie=self.login("technologist")
        code,data,_=self.req(route,cookie=cookie)
        self.assertEqual(code,403)
        self.assertNotIn("focuser",str(data))
        self.assertEqual(self.req("/api/engineering/p12/feature?q="+FOCUSER+
                                  "&feature="+CYLINDER,cookie=cookie)[0],403)

    def test_07_wrong_camera_or_feature_is_denied(self):
        cookie=self.login("engineer")
        for feature,view,expected in [
            ("../../../etc/passwd","iso",422),
            ("0"*24,"iso",404),
            (PLANE,"iso",404), # foreign source
            (CYLINDER,"evil",422),
        ]:
            with self.subTest(feature=feature,view=view):
                code,_,_=self.req("/api/engineering/p12/render?q="+FOCUSER+
                                  "&feature="+feature+"&view="+view,cookie=cookie)
                self.assertEqual(code,expected)

    def test_08_tampered_step_fails_before_rendering(self):
        with patch.object(cad_visual.fv,"candidates",return_value=self.cylinder_report):
            real=ec.original_file(self.cylinder_report["model"]["id"])
            with patch.object(cad_visual.ec,"original_file",
                              return_value=(real[0]+b"modified",real[1])):
                with self.assertRaisesRegex(cad_visual.VisualError,"SOURCE_CHANGED"):
                    cad_visual.render(FOCUSER,CYLINDER)

    def test_09_renderer_mismatched_sha_or_face_count_rejected(self):
        fake={
            "state":"CAD_FEATURE_RENDERED",
            "source_sha256":"0"*64,
            "selected_feature_id":CYLINDER,
            "selected_face_indices":[17,18,63],
            "selected_face_count":3,
            "view":"iso",
            "png_base64":"YQ=="
        }
        with patch.object(cad_visual.fv,"candidates",return_value=self.cylinder_report):
            with patch.object(cad_visual.p8,"_run_worker",return_value=fake):
                with self.assertRaisesRegex(cad_visual.VisualError,"UNTRUSTED_RENDERER_EVIDENCE"):
                    cad_visual.render(FOCUSER,CYLINDER)

    def test_10_renderer_fake_png_rejected(self):
        item=next(x for x in self.cylinder_report["features"] if x["feature_id"]==CYLINDER)
        fake={
            "state":"CAD_FEATURE_RENDERED",
            "source_sha256":self.cylinder_report["model"]["sha256"],
            "selected_feature_id":CYLINDER,
            "selected_face_indices":item["sample_face_indices"],
            "selected_face_count":item["face_occurrences"],
            "view":"iso",
            "png_base64":"YQ=="
        }
        with patch.object(cad_visual.fv,"candidates",return_value=self.cylinder_report):
            with patch.object(cad_visual.p8,"_run_worker",return_value=fake):
                with self.assertRaisesRegex(cad_visual.VisualError,"UNTRUSTED_RENDERER_PNG"):
                    cad_visual.render(FOCUSER,CYLINDER)

    def test_11_worker_limit_blocks_third_parallel_render(self):
        self.assertTrue(cad_visual.RENDER_LIMIT.acquire(False))
        self.assertTrue(cad_visual.RENDER_LIMIT.acquire(False))
        try:
            with self.assertRaisesRegex(cad_visual.VisualError,"RENDER_BUSY"):
                cad_visual.render(FOCUSER,CYLINDER)
        finally:
            cad_visual.RENDER_LIMIT.release()
            cad_visual.RENDER_LIMIT.release()

    def test_12_revoked_engineering_access_blocks_png_after_render(self):
        cookie=self.login("engineer")
        started=threading.Event();release=threading.Event()
        real=cad_visual.render
        observed=[]
        def delayed(*args,**kwargs):
            started.set()
            if not release.wait(7):raise RuntimeError("coordinator failed")
            return self.focuser_render
        def run():
            try:
                observed.append(self.req("/api/engineering/p12/render?q="+FOCUSER+
                    "&feature="+CYLINDER,cookie=cookie))
            except Exception as error:observed.append(error)
        try:
            with patch.object(cad_visual,"render",side_effect=delayed):
                thread=threading.Thread(target=run,daemon=True)
                thread.start();self.assertTrue(started.wait(5))
                with closing(app.connect(self.db)) as db:
                    app.revoke(db,"engineer","engineering")
                release.set();thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(observed[0][0],409)
                self.assertNotIn(b"\x89PNG",str(observed[0][1]).encode())
        finally:
            release.set()
            with closing(app.connect(self.db)) as db:
                db.execute("INSERT OR IGNORE INTO acl(account_id,scope_id) VALUES(?,?)",
                           ("engineer","engineering"))
                db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='engineering'")

if __name__=="__main__":
    unittest.main()
