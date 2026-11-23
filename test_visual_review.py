"""P11 real PDF crop, review evidence chain, CSRF/ACL/revocation acceptance."""
from __future__ import annotations

from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote
import uuid

import app
import engineering_corpus as ec
import feature_verification as fv
import visual_review as rv

from http.server import ThreadingHTTPServer

NOTE="Проверил исходный PDF и радиус поверхности CAD; требуется независимая инженерная сверка."

class P11VisualReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ec.DEFAULT_INDEX.is_file():
            raise unittest.SkipTest("Real public engineering corpus unavailable")
        cls.folder=tempfile.TemporaryDirectory()
        root=Path(cls.folder.name)
        cls.database=root/"testdemo.sqlite3"
        cls.reviewstore=root/"p11_review.sqlite3"
        cls.previous_store=rv.DEFAULT_STORE
        rv.DEFAULT_STORE=cls.reviewstore
        passwords=root/"credentials.txt"
        app.initialize(cls.database,passwords)
        cls.passwords={}
        for line in passwords.read_text(encoding="utf-8").splitlines():
            match=re.search(r": (technologist|engineer|finance|director) / (.+)",line)
            if match:cls.passwords[match.group(1)]=match.group(2)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.server.database_path=cls.database
        cls.server_thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.server_thread.start()
        cls.port=cls.server.server_port
        cls.real=fv.candidates("focuser_drawtube")
        cls.callout=next(x for x in cls.real["drawing_annotations"] if x["label"]=="R41.5")
        cls.surface=next(x for x in cls.real["features"]
                         if x["kind"]=="CYLINDER_RADIUS" and x["value"]==41.5)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close()
        cls.server_thread.join(timeout=3)
        rv.DEFAULT_STORE=cls.previous_store
        cls.folder.cleanup()

    def req(self,path,method="GET",body=None,cookie="",csrf="",origin=True):
        connection=http.client.HTTPConnection("127.0.0.1",self.port,timeout=12)
        heads={"Host":f"127.0.0.1:{self.port}"}
        if cookie:heads["Cookie"]=cookie
        if body is not None:
            heads["Content-Type"]="application/json"
            if origin:heads["Origin"]=f"http://127.0.0.1:{self.port}"
        if csrf:heads["X-CSRF-Token"]=csrf
        connection.request(method,quote(path,safe="/?=&%"),
                          json.dumps(body).encode("utf-8") if body is not None else None,heads)
        response=connection.getresponse()
        code=response.status;response_headers=dict(response.getheaders())
        data=response.read();connection.close()
        try:payload=json.loads(data.decode("utf-8"))
        except (UnicodeError,ValueError):payload=data
        return code,payload,response_headers

    def login(self,role="engineer"):
        code,_,headers=self.req("/api/login",method="POST",
                                body={"username":role,"password":self.passwords[role]})
        self.assertEqual(code,200)
        cookie=headers["Set-Cookie"].split(";")[0]
        code,person,_=self.req("/api/me",cookie=cookie)
        self.assertEqual(code,200)
        return cookie,person["csrf"]

    def event(self,**changes):
        payload={
            "q":"focuser_drawtube",
            "annotation":self.callout["annotation_id"],
            "feature":self.surface["feature_id"],
            "axis":"x",
            "decision":"NEEDS_MORE_EVIDENCE",
            "note":NOTE,
            "event_id":str(uuid.uuid4())
        }
        return {**payload,**changes}

    def submit(self,payload=None,role="engineer",csrf=True):
        cookie,token=self.login(role)
        return self.req("/api/engineering/p11/finding","POST",payload or self.event(),
                        cookie=cookie,csrf=token if csrf else "")

    def test_01_real_pdf_png_preview_contains_marker(self):
        url="/api/engineering/p11/preview?q=focuser_drawtube&annotation="+self.callout["annotation_id"]
        cookie,_=self.login()
        before=ec.original_file(self.real["drawing"]["id"])[0]
        code,image,headers=self.req(url,cookie=cookie)
        after=ec.original_file(self.real["drawing"]["id"])[0]
        self.assertEqual(code,200)
        self.assertEqual(headers["Content-Type"],"image/png")
        self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertEqual(image[:8],b"\x89PNG\r\n\x1a\n")
        self.assertGreater(len(image),2500)
        self.assertEqual(hashlib.sha256(before).digest(),hashlib.sha256(after).digest())

    def test_02_untrusted_annotation_and_unsupported_part_cannot_render(self):
        cookie,_=self.login()
        for fragment,expected in [
            ("/api/engineering/p11/preview?q=focuser_drawtube&annotation=../../../windows",422),
            ("/api/engineering/p11/preview?q=focuser_drawtube&annotation="+"0"*24,404),
            ("/api/engineering/p11/preview?q=040M07C01.0P026D&annotation="+self.callout["annotation_id"],404),
        ]:
            with self.subTest(url=fragment):
                self.assertEqual(self.req(fragment,cookie=cookie)[0],expected)

    def test_03_saved_note_links_source_sha_and_pdf_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            result=rv.record_finding(**self._arguments(self.event()),
                reviewer="engineer",store=Path(temp)/"notes.sqlite3")
            self.assertEqual(result["state"],"REVIEW_NOTE_RECORDED")
            self.assertFalse(result["manufacturing_authorization"])
            notes=rv.list_findings("focuser_drawtube",store=Path(temp)/"notes.sqlite3")
            self.assertEqual(notes["count"],1)
            note=notes["items"][0]
            self.assertEqual(note["drawing_sha256"],self.real["drawing"]["sha256"])
            self.assertEqual(note["cad_sha256"],self.real["model"]["sha256"])
            self.assertEqual(note["annotation_bbox"],self.callout["bbox"])
            self.assertEqual(note["feature_face_indices"],self.surface["sample_face_indices"])
            self.assertFalse(note["feature_mapping_verified"])
            self.assertEqual(note["evidence_state"],"SOURCE_HASHES_VALID")
            self.assertEqual(notes["chain_state"],"CHAIN_LINKS_VALID_NOT_EXTERNALLY_ANCHORED")

    @staticmethod
    def _arguments(event):
        return {"query":event["q"],"annotation_id":event["annotation"],
                "feature_id":event["feature"],"decision":event["decision"],
                "note":event["note"],"event_id":event["event_id"],
                "axis":event["axis"]}

    def test_04_repeat_event_id_is_idempotent(self):
        payload=self.event()
        first=self.submit(payload)
        second=self.submit(payload)
        self.assertEqual(first[0],201)
        self.assertEqual(second[0],200)
        self.assertTrue(second[1]["duplicate"])
        self.assertEqual(first[1]["event_id"],second[1]["event_id"])

    def test_05_modified_retry_rejected_with_conflict(self):
        payload=self.event()
        self.assertEqual(self.submit(payload)[0],201)
        altered={**payload,"decision":"PLAUSIBLE_MAPPING"}
        code,data,_=self.submit(altered)
        self.assertEqual(code,409)
        self.assertNotIn("manufacturing_authorization",data)

    def test_06_no_acceptance_decision_is_permitted(self):
        for decision in ("APPROVED","MANUFACTURING_APPROVED","RELEASED","PLM_APPROVED",""):
            with self.subTest(decision=decision):
                code,_,_=self.submit(self.event(decision=decision))
                self.assertEqual(code,422)

    def test_07_csrf_required_for_write(self):
        self.assertEqual(self.submit(self.event(),csrf=False)[0],403)
        self.assertEqual(self.submit(self.event())[0],201)

    def test_08_cross_origin_post_rejected(self):
        cookie,token=self.login()
        route="/api/engineering/p11/finding"
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=8)
        headers={"Cookie":cookie,"X-CSRF-Token":token,
                 "Content-Type":"application/json",
                 "Origin":"http://evil.example"}
        conn.request("POST",route,json.dumps(self.event()).encode("utf-8"),headers)
        response=conn.getresponse()
        self.assertEqual(response.status,403)
        response.read();conn.close()

    def test_09_demo_roles_and_anon_blocked(self):
        route="/api/engineering/p11/findings?q=focuser_drawtube"
        code,_,_=self.req(route)
        self.assertEqual(code,401)
        tech,_=self.login("technologist")
        code,data,_=self.req(route,cookie=tech)
        self.assertEqual(code,403)
        self.assertNotIn("focuser_drawtube",str(data))
        self.assertEqual(self.submit(self.event(),role="director")[0],403)
        self.assertEqual(self.submit(self.event(),role="finance")[0],403)

    def test_10_engineer_can_view_history_director_can_read(self):
        self.assertEqual(self.submit(self.event())[0],201)
        for role in ("engineer","director"):
            with self.subTest(role=role):
                cookie,_=self.login(role)
                code,data,_=self.req("/api/engineering/p11/findings?q=focuser_drawtube",cookie=cookie)
                self.assertEqual(code,200)
                self.assertGreaterEqual(data["count"],1)
                self.assertFalse(data["manufacturing_authorization"])

    def test_11_altered_journal_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Path(folder)/"record.sqlite3"
            rv.record_finding(**self._arguments(self.event()),reviewer="engineer",store=store)
            with closing(sqlite3.connect(store)) as db:
                db.execute("UPDATE review_event SET record_json=? WHERE seq=1",
                           ('{"event_id":"forged","decision":"APPROVED"}',))
                db.commit()
            with self.assertRaisesRegex(rv.ReviewError,"REVIEW_CHAIN_BROKEN"):
                rv.list_findings("focuser_drawtube",store=store)
            with self.assertRaisesRegex(rv.ReviewError,"REVIEW_CHAIN_BROKEN"):
                rv.record_finding(**self._arguments(self.event()),reviewer="engineer",store=store)

    def test_12_changed_file_makes_review_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Path(folder)/"review.sqlite3"
            rv.record_finding(**self._arguments(self.event()),reviewer="engineer",store=store)
            original=rv._current_sources
            with patch.object(rv,"_current_sources",return_value=False):
                listed=rv.list_findings("focuser_drawtube",store=store)
            self.assertEqual(listed["items"][0]["evidence_state"],"SOURCE_CHANGED_OR_MISSING")
            self.assertFalse(listed["items"][0]["manufacturing_authorization"])
            self.assertIsNotNone(original)

    def test_13_real_source_hash_change_blocks_save(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Path(folder)/"review.sqlite3"
            with patch.object(ec,"original_file",side_effect=ec.CorpusError("SOURCE_SHA_MISMATCH")):
                with self.assertRaises(ec.CorpusError):
                    rv.record_finding(**self._arguments(self.event()),reviewer="engineer",store=store)
            self.assertFalse(store.exists())

    def test_14_invalid_feature_pair_rejected(self):
        payload=self.event(feature="0"*24)
        self.assertEqual(self.submit(payload)[0],409)
        plane=next(x for x in self.real["features"] if x["kind"]=="PLANE_DIRECTION")
        self.assertEqual(self.submit(self.event(feature=plane["feature_id"]))[0],422)

    def test_15_reviewer_note_escaping_and_bounded_length(self):
        for note in ("a","x"*501,"abc\x00def",""):
            with self.subTest(note_length=len(note)):
                self.assertEqual(self.submit(self.event(note=note))[0],422)
        html="<script>alert('xss')</script> <img src=x onerror=alert('xss')>"
        payload=self.event(note=html)
        self.assertEqual(self.submit(payload)[0],201)
        cookie,_=self.login()
        history=self.req("/api/engineering/p11/findings?q=focuser_drawtube",cookie=cookie)[1]
        self.assertTrue(any(x["note"]==html for x in history["items"]))

    def test_16_invalid_event_id_rejected(self):
        for id in ("",str(uuid.uuid1()),"../../../","0"*36):
            with self.subTest(value=id):
                self.assertEqual(self.submit(self.event(event_id=id))[0],422)

    def test_17_revoked_scope_during_processing_blocks_write(self):
        cookie,csrf=self.login()
        started=threading.Event();release=threading.Event()
        previous=rv._source_pair
        outcomes=[]
        payload=self.event()
        def delayed(*args,**kwargs):
            started.set()
            if not release.wait(9):raise AssertionError("test coordination failure")
            return previous(*args,**kwargs)
        def requester():
            try:
                outcomes.append(self.req("/api/engineering/p11/finding","POST",payload,
                                         cookie=cookie,csrf=csrf))
            except Exception as error:outcomes.append(error)
        try:
            with patch.object(rv,"_source_pair",side_effect=delayed):
                request_thread=threading.Thread(target=requester,daemon=True)
                request_thread.start()
                self.assertTrue(started.wait(4))
                with closing(app.connect(self.database)) as db:
                    app.revoke(db,"engineer","engineering")
                release.set()
                request_thread.join(timeout=10)
                self.assertFalse(request_thread.is_alive())
                self.assertEqual(outcomes[0][0],409)
                with closing(rv._open_store(self.reviewstore)) as db:
                    self.assertIsNone(db.execute("SELECT 1 FROM review_event WHERE event_id=?",
                                                 (payload["event_id"],)).fetchone())
        finally:
            release.set()
            with closing(app.connect(self.database)) as db:
                db.execute("INSERT OR IGNORE INTO acl(account_id,scope_id) VALUES (?,?)",
                           ("engineer","engineering"))
                db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='engineering'")

    def test_18_two_writers_produce_valid_hash_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Path(folder)/"audit.sqlite3"
            payloads=[self.event() for _ in range(2)]
            done=[]
            def save(data):
                try:
                    done.append(rv.record_finding(**self._arguments(data),
                                  reviewer="engineer",store=store))
                except Exception as error:done.append(error)
            t1=threading.Thread(target=save,args=(payloads[0],))
            t2=threading.Thread(target=save,args=(payloads[1],))
            t1.start();t2.start();t1.join(timeout=13);t2.join(timeout=13)
            self.assertEqual(len(done),2)
            self.assertTrue(all(isinstance(x,dict) for x in done),done)
            listed=rv.list_findings("focuser_drawtube",store=store)
            self.assertEqual(listed["count"],2)
            self.assertEqual(len(set(x["event_id"] for x in listed["items"])),2)

    def test_19_preview_revoke_during_render_blocks_pixels(self):
        cookie,_=self.login()
        started=threading.Event();release=threading.Event()
        result=[]
        real=rv.render_preview
        def paused(*args,**kwargs):
            started.set()
            if not release.wait(8):raise RuntimeError("wait expired")
            return real(*args,**kwargs)
        def run():
            try:
                result.append(self.req("/api/engineering/p11/preview?q=focuser_drawtube&annotation="+
                                       self.callout["annotation_id"],cookie=cookie))
            except Exception as error:result.append(error)
        try:
            with patch.object(rv,"render_preview",side_effect=paused):
                t=threading.Thread(target=run,daemon=True);t.start()
                self.assertTrue(started.wait(4))
                with closing(app.connect(self.database)) as db:
                    app.revoke(db,"engineer","engineering")
                release.set();t.join(timeout=10)
                self.assertFalse(t.is_alive())
                self.assertEqual(result[0][0],409)
                self.assertNotIn(b"PNG",str(result[0][1]).encode())
        finally:
            release.set()
            with closing(app.connect(self.database)) as db:
                db.execute("INSERT OR IGNORE INTO acl(account_id,scope_id) VALUES (?,?)",
                           ("engineer","engineering"))
                db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='engineering'")

    def test_20_altered_database_metadata_breaks_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Path(directory)/"audit.sqlite3"
            rv.record_finding(**self._arguments(self.event()),reviewer="engineer",store=store)
            with closing(sqlite3.connect(store)) as db:
                db.execute("UPDATE review_event SET reviewer='director', timestamp_ms=123 WHERE seq=1")
                db.commit()
            with self.assertRaisesRegex(rv.ReviewError,"REVIEW_CHAIN_BROKEN"):
                rv.list_findings("focuser_drawtube",store=store)

if __name__=="__main__":
    unittest.main()
