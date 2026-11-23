"""P10 real analytic CAD surfaces, source provenance, ACL and signed snapshot tests."""
from __future__ import annotations
from contextlib import closing
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import app
import feature_verification as fv
import revision_gateway as rev
import dimension_trace as p9

class P10Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not p9.ec.DEFAULT_INDEX.is_file():raise unittest.SkipTest("Corpus unimported")
        cls.temp=tempfile.TemporaryDirectory()
        root=Path(cls.temp.name)
        cls.db=root/"test.sqlite3";credentials=root/"demo-creds.txt"
        app.initialize(cls.db,credentials)
        cls.passwords={}
        for line in credentials.read_text(encoding='utf-8').splitlines():
            m=re.search(r": (technologist|engineer|finance|director) / (.+)",line)
            if m:cls.passwords[m.group(1)]=m.group(2)
        cls.srv=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.srv.database_path=cls.db
        cls.thread=threading.Thread(target=cls.srv.serve_forever,daemon=True)
        cls.thread.start()
        cls.port=cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown();cls.srv.server_close();cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def req(self,path,method="GET",body=None,cookie=""):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=9)
        heads={"Host":f"127.0.0.1:{self.port}"}
        if body is not None:heads["Content-Type"]="application/json"
        if cookie:heads["Cookie"]=cookie
        conn.request(method,quote(path,safe="/?=&%"),json.dumps(body).encode() if body is not None else None,heads)
        response=conn.getresponse()
        status=response.status;header=dict(response.getheaders());raw=response.read();conn.close()
        try:value=json.loads(raw.decode('utf-8'))
        except (UnicodeError,ValueError):value=raw
        return status,value,header

    def login(self,role):
        code,_,head=self.req("/api/login",method="POST",
                             body={"username":role,"password":self.passwords[role]})
        self.assertEqual(code,200)
        return head["Set-Cookie"].split(";")[0]

    @staticmethod
    def pick(what,label,kind,value=None):
        record=next(x for x in what["drawing_annotations"] if x["label"]==label)
        options=[x for x in what["features"] if x["kind"]==kind]
        feat=next((x for x in options if value is None or
                  abs(x.get("value",x.get("angles_to_axes",{}).get("x",-999))-value)<.03),None)
        assert feat is not None
        return record,feat

    def test_01_real_focuser_cylinder_41_5(self):
        record=fv.candidates("focuser_drawtube")
        self.assertEqual(record["state"],"FEATURE_CANDIDATES_FOR_HUMAN_MAPPING")
        self.assertEqual(record["cad_source_units"],"millimetre")
        ann,surface=self.pick(record,"R41.5","CYLINDER_RADIUS",41.5)
        self.assertEqual(surface["value"],41.5)
        self.assertGreaterEqual(surface["face_occurrences"],2)
        result=fv.compare("focuser_drawtube",ann["annotation_id"],surface["feature_id"],"x","0")
        self.assertEqual(result["state"],"ANALYTIC_VALUES_WITHIN_SELECTED_WINDOW")
        self.assertEqual(result["absolute_difference"],"0")
        self.assertFalse(result["feature_mapping_verified"])
        self.assertFalse(result["manufacturing_authorization"])

    def test_02_real_tube_67_38_plane_angle(self):
        data=fv.candidates("lower_square_tube")
        ann,surface=self.pick(data,"67.38°","PLANE_DIRECTION",67.38)
        self.assertEqual(len(surface["absolute_normal"]),3)
        self.assertEqual(surface["angles_to_axes"]["x"],67.38015)
        value=fv.compare("lower_square_tube",ann["annotation_id"],surface["feature_id"],"x","0.02")
        self.assertEqual(value["state"],"ANALYTIC_VALUES_WITHIN_SELECTED_WINDOW")
        self.assertEqual(value["absolute_difference"],"0.00015")
        self.assertEqual(value["surface_reference_axis"],"x")
        self.assertEqual(value["engineering_tolerance_verification"],"NOT_PERFORMED")

    def test_03_angle_different_axis_is_negative_only(self):
        data=fv.candidates("lower_square_tube")
        ann,surface=self.pick(data,"67.38°","PLANE_DIRECTION",67.38)
        result=fv.compare("lower_square_tube",ann["annotation_id"],surface["feature_id"],"y","0.02")
        self.assertEqual(result["state"],"ANALYTIC_VALUES_OUTSIDE_SELECTED_WINDOW")
        self.assertFalse(result["manufacturing_authorization"])

    def test_04_radius_is_not_a_plane_angle(self):
        data=fv.candidates("focuser_drawtube")
        ann=next(x for x in data["drawing_annotations"] if x["label"]=="R41.5")
        surface=next(x for x in data["features"] if x["kind"]=="PLANE_DIRECTION")
        with self.assertRaisesRegex(fv.FeatureError,"INCOMPATIBLE"):
            fv.compare("focuser_drawtube",ann["annotation_id"],surface["feature_id"])

    def test_05_angle_cannot_use_cylinder_from_another_part(self):
        tube=fv.candidates("lower_square_tube")
        focuser=fv.candidates("focuser_drawtube")
        ann=next(x for x in tube["drawing_annotations"] if x["kind"]=="ANGULAR")
        cylinder=next(x for x in focuser["features"] if x["kind"]=="CYLINDER_RADIUS")
        # Foreign STEP feature ID cannot be referenced from this part.
        with self.assertRaisesRegex(fv.FeatureError,"UNRECOGNIZED_SOURCE_ELEMENT"):
            fv.compare("lower_square_tube",ann["annotation_id"],cylinder["feature_id"])

    def test_06_forged_feature_ids_and_wrong_windows(self):
        data=fv.candidates("focuser_drawtube")
        ann,surface=self.pick(data,"R41.5","CYLINDER_RADIUS",41.5)
        for value in ("",-1,"z"*24,"a"*30):
            with self.subTest(id=value):
                with self.assertRaises(fv.FeatureError):
                    fv.compare("focuser_drawtube",ann["annotation_id"],value)
        with self.assertRaisesRegex(fv.FeatureError,"UNRECOGNIZED"):
            fv.compare("focuser_drawtube",ann["annotation_id"],"0"*24)
        for value in ("nan","1e5","0.201","-0.1",".05"):
            with self.subTest(window=value):
                with self.assertRaises(fv.FeatureError):
                    fv.compare("focuser_drawtube",ann["annotation_id"],surface["feature_id"],window=value)

    def test_07_false_part_query_cannot_generate_features(self):
        for query in ("focuser_drawtube99","040M07C01.0P026D","fоcuser_drawtube"):
            with self.subTest(query=query):
                self.assertEqual(fv.candidates(query)["state"],"NOT_COMPARABLE")

    def test_08_no_plm_connection_claims_authority(self):
        data=rev.status("focuser_drawtube",environment={})
        self.assertEqual(data["state"],"NO_AUTHORITATIVE_CONNECTOR")
        self.assertFalse(data["manufacturing_authorization"])

    def signed_fixture(self):
        private=Ed25519PrivateKey.generate()
        pub=private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        source=p9.annotations("focuser_drawtube")
        now=datetime.now(timezone.utc)
        payload={
            "schema":rev.SCHEMA,"source_system":"PLM","issuer":"TEST DEMO SYSTEM",
            "issued_at":(now-timedelta(minutes=2)).isoformat(timespec="seconds").replace("+00:00","Z"),
            "expires_at":(now+timedelta(minutes=30)).isoformat(timespec="seconds").replace("+00:00","Z"),
            "part_key":source["part_key"],"revision":"A","drawing_sha256":source["drawing"]["sha256"],
            "cad_sha256":source["model"]["sha256"],"release_event_id":"MOCK-001",
            "status":"RELEASED"
        }
        sig=base64.b64encode(private.sign(rev.canonical(payload))).decode("ascii")
        return {"payload":payload,"signature":sig,"key_id":"test-issuer"},pub

    def test_09_signed_snapshot_does_not_grant_manufacturing(self):
        snapshot,key=self.signed_fixture()
        with tempfile.TemporaryDirectory() as directory:
            f=Path(directory)/"snapshot.json"
            f.write_text(json.dumps(snapshot),encoding="utf-8")
            environment={"P10_PLM_SNAPSHOT_FILE":str(f),
                         "P10_PLM_PUBLIC_KEYS_JSON":json.dumps({"test-issuer":key})}
            result=rev.status("focuser_drawtube",environment=environment)
        self.assertEqual(result["state"],"SIGNED_SNAPSHOT_INTEGRITY_VERIFIED")
        self.assertEqual(result["claimed_status"],"RELEASED")
        self.assertEqual(result["issuer_authority"],"NOT_VERIFIED")
        self.assertFalse(result["manufacturing_authorization"])

    def test_10_snapshot_tampering_rejected(self):
        snapshot,key=self.signed_fixture()
        snapshot["payload"]["revision"]="B"
        with self.assertRaisesRegex(rev.AuthorityError,"SIGNATURE_INVALID"):
            rev.verify_envelope(snapshot,{"test-issuer":key})

    def test_11_unknown_key_and_expired_record_rejected(self):
        snap,key=self.signed_fixture()
        with self.assertRaises(rev.AuthorityError):
            rev.verify_envelope(snap,{"elsewhere":key})
        now=datetime.now(timezone.utc)+timedelta(days=1)
        with self.assertRaisesRegex(rev.AuthorityError,"EXPIRED"):
            rev.verify_envelope(snap,{"test-issuer":key},at=now)

    def test_12_snapshot_wrong_part_and_hash_rejected(self):
        snapshot,key=self.signed_fixture()
        private=Ed25519PrivateKey.generate()
        # Re-sign under a different key to exercise data-integrity/path binding
        # independent of cryptographic mutation checks.
        newkey=private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        snapshot["payload"]["cad_sha256"]="0"*64
        snapshot["signature"]=base64.b64encode(private.sign(rev.canonical(snapshot["payload"]))).decode()
        with tempfile.TemporaryDirectory() as directory:
            f=Path(directory)/"record.json";f.write_text(json.dumps(snapshot),encoding="utf-8")
            result=rev.status("focuser_drawtube",environment={
                "P10_PLM_SNAPSHOT_FILE":str(f),
                "P10_PLM_PUBLIC_KEYS_JSON":json.dumps({"test-issuer":newkey})})
            self.assertEqual(result["state"],"UNTRUSTED_OR_UNUSABLE_SNAPSHOT")
            self.assertEqual(result["reason"],"DOCUMENT_HASH_MISMATCH")
            self.assertFalse(result["manufacturing_authorization"])

    def test_13_missing_snapshot_fails_closed(self):
        result=rev.status("focuser_drawtube",environment={
            "P10_PLM_SNAPSHOT_FILE":r"C:\nonexistent\plm.json",
            "P10_PLM_PUBLIC_KEYS_JSON":json.dumps({"test-issuer":"00"*32})})
        self.assertEqual(result["state"],"UNTRUSTED_OR_UNUSABLE_SNAPSHOT")
        self.assertFalse(result["manufacturing_authorization"])

    def test_14_engineering_scope_restricts_p10_routes(self):
        routes=("/api/engineering/p10/features?q=focuser_drawtube",
                "/api/engineering/p10/revisions?q=focuser_drawtube",
                "/api/engineering/p10/compare?q=focuser_drawtube&annotation="+"0"*24+"&feature="+"0"*24)
        tech=self.login("technologist")
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(self.req(route)[0],401)
                code,data,_=self.req(route,cookie=tech)
                self.assertEqual(code,403)
                self.assertNotIn("focuser_drawtube",str(data))

    def test_15_http_engineer_real_radius_and_plm_absence(self):
        cookie=self.login("engineer")
        status,data,_=self.req("/api/engineering/p10/features?q=focuser_drawtube",cookie=cookie)
        self.assertEqual(status,200)
        ann,surf=self.pick(data,"R41.5","CYLINDER_RADIUS",41.5)
        route="/api/engineering/p10/compare?q=focuser_drawtube&annotation="+ann["annotation_id"]+"&feature="+surf["feature_id"]
        code,value,_=self.req(route,cookie=cookie)
        self.assertEqual(code,200)
        self.assertEqual(value["state"],"ANALYTIC_VALUES_WITHIN_SELECTED_WINDOW")
        self.assertFalse(value["manufacturing_authorization"])
        code,registry,_=self.req("/api/engineering/p10/revisions?q=focuser_drawtube",cookie=cookie)
        self.assertEqual(code,200)
        self.assertEqual(registry["state"],"NO_AUTHORITATIVE_CONNECTOR")

    def test_16_revoke_access_during_cad_processing(self):
        cookie=self.login("engineer")
        started=threading.Event();resume=threading.Event();outputs=[]
        original=fv._read_cad
        def delayed(*args,**kwargs):
            started.set()
            if not resume.wait(7):raise RuntimeError("Test coordination timeout")
            return original(*args,**kwargs)
        def requester():
            try:outputs.append(self.req("/api/engineering/p10/features?q=focuser_drawtube",cookie=cookie))
            except Exception as err:outputs.append(err)
        try:
            with patch.object(fv,"_read_cad",side_effect=delayed):
                worker=threading.Thread(target=requester,daemon=True)
                worker.start();self.assertTrue(started.wait(5))
                with closing(app.connect(self.db)) as db:app.revoke(db,"engineer","engineering")
                resume.set();worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outputs[0][0],409)
        finally:
            resume.set()
            with closing(app.connect(self.db)) as db:
                db.execute("INSERT OR IGNORE INTO acl(account_id,scope_id) VALUES (?,?)",
                           ("engineer","engineering"))
                db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='engineering'")

if __name__=="__main__":
    unittest.main()
