"""P9 deterministic source-anchored dimensional hypotheses regressions."""
from __future__ import annotations
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import quote

import app
import engineering_corpus as ec
import engineering_inspection as p8
import dimension_trace as p9

class P9DimensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ec.DEFAULT_INDEX.exists() or not p8.DEFAULT_INDEX.exists():
            raise unittest.SkipTest("P9 source indexes are not installed")
        cls.tdir=tempfile.TemporaryDirectory()
        root=Path(cls.tdir.name)
        cls.dbpath=root/"app.sqlite3"
        auth=root/"passwords.txt"
        app.initialize(cls.dbpath,auth)
        cls.passwords={}
        for line in auth.read_text(encoding="utf-8").splitlines():
            m=re.search(r": (technologist|engineer|finance|director) / (.+)",line)
            if m:cls.passwords[m.group(1)]=m.group(2)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.server.database_path=cls.dbpath
        cls.port=cls.server.server_port
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.tdir.cleanup()

    def req(self,path,method="GET",body=None,cookie=""):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=8)
        header={"Host":"127.0.0.1:"+str(self.port)}
        if cookie:header["Cookie"]=cookie
        if body is not None:header["Content-Type"]="application/json"
        conn.request(method,quote(path,safe="/?=&%"),json.dumps(body).encode() if body is not None else None,header)
        response=conn.getresponse()
        status=response.status;headers=dict(response.getheaders());data=response.read();conn.close()
        try:parsed=json.loads(data.decode("utf-8"))
        except (UnicodeError,ValueError):parsed=data
        return status,parsed,headers

    def login(self,role):
        status,_,header=self.req("/api/login",method="POST",
                                 body={"username":role,"password":self.passwords[role]})
        self.assertEqual(status,200)
        return header["Set-Cookie"].split(";")[0]

    @staticmethod
    def find(result,token):
        matches=[x for x in result["annotations"] if x["label"]==token]
        assert len(matches)>0,token
        return matches[0]

    def test_01_three_exact_public_source_triplets(self):
        result=p9.catalog()
        self.assertEqual(result["total"],3)
        self.assertEqual([x["part"] for x in result["items"]],
                         ["focuser_drawtube","mirror_cell_base","lower_square_tube"])
        self.assertFalse(result["manufacturing_authorization"])

    def test_02_real_pdf_callouts_retained_with_page_and_box(self):
        for name,token in (("focuser_drawtube","80"),
                           ("lower_square_tube","437"),
                           ("mirror_cell_base","24")):
            with self.subTest(part=name):
                result=p9.annotations(name)
                self.assertEqual(result["state"],"CANDIDATE_DIMENSIONS_FOR_REVIEW")
                ann=self.find(result,token)
                self.assertEqual(ann["unit"],"mm")
                self.assertEqual(ann["kind"],"LINEAR")
                self.assertEqual(ann["page"],1)
                self.assertEqual(len(ann["bbox"]),4)
                self.assertTrue(all(0<x<1 for x in ann["bbox"]))
                self.assertEqual(ann["tolerance_status"],"NOT_ESTABLISHED")
                self.assertFalse(result["manufacturing_authorization"])

    def test_03_file_units_read_from_step_context(self):
        for name in ("focuser_drawtube","lower_square_tube","mirror_cell_base"):
            with self.subTest(part=name):
                r=p9.annotations(name)
                self.assertEqual(r["cad"]["units"],"millimetre")
                self.assertEqual(r["cad"]["file_length_units"],["millimetre"])
                self.assertTrue(r["cad"]["brep_valid"])
                self.assertEqual(r["cad"]["solid_count"],1)

    def test_04_real_80_equals_y_extent_with_narrow_window(self):
        r=p9.annotations("focuser_drawtube")
        ann=self.find(r,"80")
        result=p9.compare("focuser_drawtube",ann["annotation_id"],"y","0.02")
        self.assertEqual(result["state"],"NUMERIC_WITHIN_SELECTED_WINDOW")
        self.assertEqual(result["cad_bbox_extent"],"80.00086")
        self.assertEqual(result["absolute_difference_mm"],"0.00086")
        self.assertFalse(result["feature_mapping_verified"])
        self.assertEqual(result["geometric_compliance"],"NOT_ESTABLISHED")
        self.assertFalse(result["manufacturing_authorization"])

    def test_05_zero_window_is_not_rounded_to_success(self):
        r=p9.annotations("focuser_drawtube")
        id=self.find(r,"80")["annotation_id"]
        result=p9.compare("focuser_drawtube",id,"y","0")
        self.assertEqual(result["state"],"NUMERIC_OUTSIDE_SELECTED_WINDOW")

    def test_06_real_437_matches_tube_y_extent(self):
        r=p9.annotations("lower_square_tube")
        id=self.find(r,"437")["annotation_id"]
        result=p9.compare("lower_square_tube",id,"y","0")
        self.assertEqual(result["state"],"NUMERIC_WITHIN_SELECTED_WINDOW")
        self.assertEqual(result["absolute_difference_mm"],"0")
        self.assertFalse(result["revision_approved"])

    def test_07_real_35_matches_tube_z_extent(self):
        r=p9.annotations("lower_square_tube")
        id=self.find(r,"35")["annotation_id"]
        result=p9.compare("lower_square_tube",id,"z","0")
        self.assertEqual(result["state"],"NUMERIC_WITHIN_SELECTED_WINDOW")

    def test_08_wrong_axis_is_numerically_outside_not_defect(self):
        r=p9.annotations("lower_square_tube")
        id=self.find(r,"437")["annotation_id"]
        result=p9.compare("lower_square_tube",id,"x","0.02")
        self.assertEqual(result["state"],"NUMERIC_OUTSIDE_SELECTED_WINDOW")
        self.assertEqual(result["cad_bbox_extent"],"220.0")
        self.assertFalse(result["manufacturing_authorization"])
        self.assertIn("not prove a part defect",result["message"])

    def test_09_radius_is_not_implicitly_doubled(self):
        r=p9.annotations("focuser_drawtube")
        radius=self.find(r,"R41.5")
        self.assertEqual(radius["kind"],"RADIUS")
        with self.assertRaisesRegex(p9.DimensionError,"ONLY_LINEAR"):
            p9.compare("focuser_drawtube",radius["annotation_id"],"z")

    def test_10_angle_is_not_linear_dimension(self):
        r=p9.annotations("lower_square_tube")
        ang=self.find(r,"67.38°")
        self.assertEqual(ang["kind"],"ANGULAR")
        with self.assertRaises(p9.DimensionError):
            p9.compare("lower_square_tube",ang["annotation_id"],"x")

    def test_11_wrong_or_forged_id_rejected(self):
        for fake in ["", "../", "0"*24, "g"*24, "A"*24]:
            with self.subTest(fake=fake):
                r=p9.annotations("focuser_drawtube")
                if fake=="0"*24:
                    with self.assertRaisesRegex(p9.DimensionError,"ANNOTATION_NOT_FOUND"):
                        p9.compare("focuser_drawtube",fake,"y")
                else:
                    with self.assertRaisesRegex(p9.DimensionError,"ANNOTATION_ID_INVALID"):
                        p9.compare("focuser_drawtube",fake,"y")

    def test_12_window_validation_rejects_nans_and_unsafe_values(self):
        r=p9.annotations("focuser_drawtube")
        id=self.find(r,"80")["annotation_id"]
        for invalid in ("nan","inf","1e-4","-1","0.201","0.2001","0.9999",".02"):
            with self.subTest(window=invalid):
                with self.assertRaises(p9.DimensionError):
                    p9.compare("focuser_drawtube",id,"y",invalid)

    def test_13_axis_validation(self):
        r=p9.annotations("focuser_drawtube")
        id=self.find(r,"80")["annotation_id"]
        for invalid in ("X","w","__class__","",0):
            with self.subTest(axis=invalid):
                with self.assertRaises(p9.DimensionError):
                    p9.compare("focuser_drawtube",id,invalid)

    def test_14_ambiguous_or_unknown_parts_never_create_cad_links(self):
        for query in ("040M07C01.0P026D","wrongpart","fоcuser_drawtube",
                      "focuser_drawtube mirror_cell_base"):
            with self.subTest(query=query):
                r=p9.annotations(query)
                self.assertIn(r["state"],{"NOT_FOUND","AMBIGUOUS"})
                self.assertNotIn("cad",r)
                self.assertFalse(r["manufacturing_authorization"])

    def test_15_mutation_of_verified_pdf_blocks_result(self):
        # Isolated temporary corpus with exactly three real sources.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"sources";group=root/"ArcBlue_HMT_Lite"
            group.mkdir(parents=True)
            manifest=[]
            for name in ("materials_bom.csv","focuser_drawtube.pdf","focuser_drawtube.STEP"):
                dest=group/name
                shutil.copyfile(ec.DEFAULT_CORPUS/"ArcBlue_HMT_Lite"/name,dest)
                raw=dest.read_bytes()
                manifest.append({"group":"ArcBlue_HMT_Lite","name":name,
                                 "size":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),
                                 "url":"https://raw.githubusercontent.com/example/demo/main/"+name})
            (root/"manifest.json").write_text(json.dumps({"documents":manifest}),encoding="utf-8")
            index=Path(tmp)/"p7.sqlite3";p8index=Path(tmp)/"p8.sqlite3"
            ec.build_index(root,index)
            p8.build_index(index,p8index)
            self.assertEqual(p9.annotations("focuser_drawtube",index,p8index)["state"],
                             "CANDIDATE_DIMENSIONS_FOR_REVIEW")
            with (group/"focuser_drawtube.pdf").open("ab") as stream:
                stream.write(b"#tamper")
            with self.assertRaises(ec.CorpusError):
                p9.annotations("focuser_drawtube",index,p8index)

    def test_16_unknown_units_produce_unverified_result(self):
        real=p9.annotations("focuser_drawtube")
        real["cad"]["units"]="UNKNOWN_OR_MIXED"
        id=self.find(real,"80")["annotation_id"]
        with patch.object(p9,"annotations",return_value=real):
            outcome=p9.compare("focuser_drawtube",id,"y")
        self.assertEqual(outcome["state"],"UNITS_NOT_VERIFIED")
        self.assertFalse(outcome["manufacturing_authorization"])

    def test_17_anonymous_and_technologist_cannot_access_any_p9_path(self):
        tech=self.login("technologist")
        for path in ("/api/engineering/p9/catalog",
                     "/api/engineering/p9/annotations?q=focuser_drawtube",
                     "/api/engineering/p9/compare?q=focuser_drawtube&annotation="+"0"*24+"&axis=y"):
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0],401)
                code,data,_=self.req(path,cookie=tech)
                self.assertEqual(code,403)
                self.assertNotIn("focuser_drawtube",str(data))

    def test_18_engineer_gets_actual_traceable_result(self):
        cookie=self.login("engineer")
        code,annotations,_=self.req("/api/engineering/p9/annotations?q=focuser_drawtube",cookie=cookie)
        self.assertEqual(code,200)
        id=self.find(annotations,"80")["annotation_id"]
        uri="/api/engineering/p9/compare?q=focuser_drawtube&annotation="+id+"&axis=y&window=0.02"
        code,comparison,_=self.req(uri,cookie=cookie)
        self.assertEqual(code,200)
        self.assertEqual(comparison["state"],"NUMERIC_WITHIN_SELECTED_WINDOW")
        self.assertFalse(comparison["manufacturing_authorization"])
        self.assertEqual(len(comparison["drawing"]["sha256"]),64)
        self.assertEqual(len(comparison["model"]["sha256"]),64)

    def test_19_invalid_arguments_get_422(self):
        cookie=self.login("engineer")
        for route in ("/api/engineering/p9/annotations?q=a",
                      "/api/engineering/p9/compare?q=focuser_drawtube&annotation="+"z"*24+"&axis=y",
                      "/api/engineering/p9/compare?q=focuser_drawtube&annotation="+"0"*24+"&axis=DOESNOTEXIST"):
            with self.subTest(route=route):
                self.assertEqual(self.req(route,cookie=cookie)[0],422)

    def test_20_revocation_during_annotation_parse_hides_result(self):
        cookie=self.login("engineer")
        started=threading.Event();release=threading.Event()
        outputs=[]
        true_read=p9._read_worker
        def delayed(*args,**kwargs):
            started.set()
            if not release.wait(7):raise RuntimeError("Test synchronization timed out")
            return true_read(*args,**kwargs)
        def waiter():
            try:outputs.append(self.req("/api/engineering/p9/annotations?q=focuser_drawtube",cookie=cookie))
            except Exception as error:outputs.append(error)
        try:
            with patch.object(p9,"_read_worker",side_effect=delayed):
                job=threading.Thread(target=waiter,daemon=True);job.start()
                self.assertTrue(started.wait(4))
                with closing(app.connect(self.dbpath)) as db:
                    app.revoke(db,"engineer","engineering")
                release.set();job.join(timeout=9)
                self.assertFalse(job.is_alive())
                self.assertEqual(len(outputs),1)
                self.assertIsInstance(outputs[0],tuple)
                self.assertEqual(outputs[0][0],409)
                self.assertNotIn("focuser_drawtube",str(outputs[0][1]))
        finally:
            release.set()
            with closing(app.connect(self.dbpath)) as db:
                db.execute("INSERT OR IGNORE INTO acl(account_id,scope_id) VALUES (?,?)",
                           ("engineer","engineering"))
                db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='engineering'")

if __name__=="__main__":
    unittest.main()
