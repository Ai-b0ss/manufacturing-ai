"""P7 engineering corpus: real-source, provenance, negative and ACL regressions."""
from __future__ import annotations
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import app
import engineering_corpus as ec

class EngineeringCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ec.DEFAULT_INDEX.is_file():
            raise unittest.SkipTest("Real engineering corpus not imported in this environment")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dbpath=Path(cls.tmp.name)/"test_app.sqlite3"
        cls.creds=Path(cls.tmp.name)/"cred.txt"
        app.initialize(cls.dbpath,cls.creds)
        cls.passwords={}
        import re
        for text in cls.creds.read_text(encoding="utf-8").splitlines():
            m=re.search(r": (technologist|engineer|finance|director) / (.+)",text)
            if m:cls.passwords[m.group(1)] = m.group(2)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.server.database_path=cls.dbpath
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.port=cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def req(self, path, cookie="", method="GET", body=None, content_type="application/json"):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=8)
        head={"Host":"127.0.0.1:"+str(self.port)}
        if cookie:head["Cookie"]=cookie
        if body is not None:head["Content-Type"]=content_type
        conn.request(method,quote(path,safe="/?=&%"),json.dumps(body).encode() if body is not None else None,head)
        resp=conn.getresponse()
        status=resp.status;headers=dict(resp.getheaders());data=resp.read();conn.close()
        try:decoded=json.loads(data.decode("utf-8"))
        except (UnicodeError,ValueError):decoded=data
        return status,decoded,headers

    def login(self,role):
        code,body,headers=self.req("/api/login",method="POST",
                                   body={"username":role,"password":self.passwords[role]})
        self.assertEqual(code,200,body)
        return headers["Set-Cookie"].split(";")[0]

    def test_01_full_original_manifest_indexed(self):
        result=ec.summary()
        self.assertEqual(result["sources"],40)
        self.assertGreaterEqual(result["bom_lines"],300)
        self.assertEqual(result["links"],6)
        self.assertEqual(result["revision_approval"],"NOT_VERIFIED")

    def test_02_all_40_files_sha256_and_length_independent(self):
        with closing(ec._open_readonly(ec.DEFAULT_INDEX)) as db:
            rows=db.execute("SELECT * FROM source").fetchall()
            root=Path(db.execute("SELECT source_root FROM dataset").fetchone()[0])
        self.assertEqual(len(rows),40)
        for row in rows:
            path=ec._safe_path(root,row["source_group"],row["name"])
            self.assertEqual(path.stat().st_size,row["original_size"])
            with path.open("rb") as stream:
                self.assertEqual(hashlib.file_digest(stream,"sha256").hexdigest(),row["original_sha256"])

    def test_03_three_real_bom_drawing_step_joins(self):
        expectations=[
            ("focuser_drawtube","4","6061-T6 Aluminum Alloy","1"),
            ("mirror_cell_base","2","6061-T6 Aluminum Alloy","1"),
            ("lower_square_tube","23","SUS304 Stainless Steel","6")]
        for name,item,material,quantity in expectations:
            with self.subTest(part=name):
                result=ec.answer("какой материал у "+name+"?")
                self.assertEqual(result["state"],"BOM_DRAWING_STEP_LINKED")
                self.assertEqual((result["item_no"],result["material"],result["quantity"]),
                                 (item,material,quantity))
                self.assertEqual({s["role"] for s in result["sources"]},
                                 {"BOM","DRAWING","MODEL"})
                self.assertEqual({s["matching"] for s in result["sources"]},
                                 {"BOM_ROW","EXACT_FILENAME"})
                self.assertFalse(result["manufacturing_authorization"])
                self.assertEqual(result["revision_status"],"UNVERIFIED")

    def test_04_unknown_bcn3d_does_not_infer_bom(self):
        result=ec.answer("040M07C01.0P026D")
        self.assertEqual(result["state"],"NOT_FOUND")
        self.assertNotIn("sources",result)

    def test_05_passport_skuid_not_guess_revision(self):
        result=ec.answer("FD_1842300-H")
        self.assertEqual(result["state"],"NOT_FOUND")

    def test_06_prefix_homoglyphs_not_joined(self):
        for value in ["focuser_drawtube99","x_focuser_drawtube",
                      "fоcuser_drawtube", "mirror_cell_base9"]:
            with self.subTest(value=value):
                self.assertEqual(ec.answer(value)["state"],"NOT_FOUND")

    def test_07_multi_part_question_not_silently_combined(self):
        result=ec.answer("focuser_drawtube mirror_cell_base")
        self.assertEqual(result["state"],"AMBIGUOUS")
        self.assertEqual(len(result["candidates"]),2)

    def test_08_alias_spaces_only_for_deterministic_name(self):
        self.assertEqual(ec.answer("focuser drawtube")["state"],"BOM_DRAWING_STEP_LINKED")

    def test_09_historical_change_is_separate_reference(self):
        result=ec.notice("БВАИ.107-2012")
        self.assertEqual(result["state"],"HISTORICAL_REFERENCE_ONLY")
        self.assertEqual(result["page"],13)
        self.assertFalse(result["manufacturing_authorization"])
        self.assertNotIn("source_id",result)
        self.assertEqual(ec.notice("ИИ-017")["state"],"NOT_FOUND")

    def test_10_original_download_matches_index_and_hash(self):
        sources=ec.answer("focuser_drawtube")["sources"]
        for source in sources:
            with self.subTest(source=source["name"]):
                data,suffix=ec.original_file(source["id"])
                self.assertEqual(hashlib.sha256(data).hexdigest(),source["sha256"])
                self.assertIn(suffix,{".pdf",".step",".csv"})

    def test_11_http_authentication_and_engineering_scope(self):
        paths=["/api/engineering/summary","/api/engineering/ask?q=focuser_drawtube",
               "/api/engineering/notice?id="+quote("БВАИ.107-2012"),
               "/api/engineering/file?id="+ec.answer("focuser_drawtube")["sources"][0]["id"]]
        tech=self.login("technologist")
        for path in paths:
            self.assertEqual(self.req(path)[0],401)
            status,data,_=self.req(path,cookie=tech)
            self.assertEqual(status,403)
            self.assertNotIn("focuser_drawtube",str(data))
            self.assertNotIn("БВАИ",str(data))

    def test_12_http_engineer_and_director(self):
        sid=next(x["id"] for x in ec.answer("focuser_drawtube")["sources"] if x["kind"]=="PDF")
        for role in ["engineer","director"]:
            with self.subTest(role=role):
                cookie=self.login(role)
                code,result,_=self.req("/api/engineering/ask?q=focuser_drawtube",cookie)
                self.assertEqual(code,200)
                self.assertEqual(result["state"],"BOM_DRAWING_STEP_LINKED")
                code,file_bytes,head=self.req("/api/engineering/file?id="+sid,cookie)
                self.assertEqual(code,200)
                self.assertEqual(file_bytes[:4],b"%PDF")
                self.assertEqual(head.get("Content-Type"),"application/octet-stream")
                self.assertEqual(head.get("X-Content-Type-Options"),"nosniff")

    def test_13_path_traversal_or_unknown_file_cannot_fetch(self):
        cookie=self.login("engineer")
        for sid in ["../../Windows","%2e%2e","a"*24,"' OR 1=1 --"]:
            self.assertEqual(self.req("/api/engineering/file?id="+sid,cookie)[0],404)

    def test_14_missing_index_returns_failure_not_fake_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ec.CorpusError,"CORPUS_NOT_IMPORTED"):
                ec.summary(Path(tmp)/"missing.sqlite3")

    def test_15_bad_checksum_rejected_and_previous_index_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"corpus";group=root/"Project";group.mkdir(parents=True)
            source=group/"sample.txt";source.write_text("original",encoding="utf-8")
            manifest={"documents":[{"group":"Project","name":"sample.txt",
                                     "size":source.stat().st_size,
                                     "sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
                                     "url":"https://raw.githubusercontent.com/example/demo/main/sample.txt"}]}
            (root/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
            index=Path(tmp)/"copy.sqlite3"
            ec.build_index(root,index)
            original_summary=ec.summary(index)
            source.write_text("modified",encoding="utf-8")
            with self.assertRaises(ec.CorpusError):
                ec.build_index(root,index)
            self.assertEqual(original_summary,ec.summary(index))
            sid=ec.source_id("Project","sample.txt")
            with self.assertRaises(ec.CorpusError):
                ec.original_file(sid,index)

    def test_16_malicious_manifest_paths_not_ingested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"corpus";root.mkdir()
            manifest={"documents":[{"group":"Project","name":"../secret.txt","size":0,
                                     "sha256":"0"*64,
                                     "url":"https://raw.githubusercontent.com/example/demo/main/x"}]}
            (root/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
            with self.assertRaises(ec.CorpusError):
                ec.build_index(root,Path(tmp)/"db.sqlite3")

    def test_19_changed_source_invalidates_existing_answer(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"corpus"
            real=ec.DEFAULT_CORPUS
            entries=[]
            for filename in ("materials_bom.csv","focuser_drawtube.pdf","focuser_drawtube.STEP"):
                group="ArcBlue_HMT_Lite"
                destination=root/group/filename
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(real/group/filename,destination)
                data=destination.read_bytes()
                entries.append({"group":group,"name":filename,"size":len(data),
                                "sha256":hashlib.sha256(data).hexdigest(),
                                "url":"https://raw.githubusercontent.com/example/demo/main/"+filename})
            (root/"manifest.json").write_text(json.dumps({"documents":entries}),encoding="utf-8")
            idx=Path(tmp)/"index.sqlite3"
            ec.build_index(root,idx)
            self.assertEqual(ec.answer("focuser_drawtube",idx)["state"],"BOM_DRAWING_STEP_LINKED")
            with (root/"ArcBlue_HMT_Lite"/"focuser_drawtube.STEP").open("ab") as file:
                file.write(b"#unexpected-change")
            with self.assertRaisesRegex(ec.CorpusError,"SOURCE_CHANGED_AFTER_IMPORT"):
                ec.answer("focuser_drawtube",idx)

    def test_17_content_never_claims_approved_revision(self):
        source=Path(app.BASE/"index.html").read_text(encoding="utf-8")
        self.assertIn('id="engineering-query"',source)
        self.assertIn('id="engineering-navigation"',source)
        for item in ("редакцию","изготовление"):
            self.assertIn(item,source.lower())

    def test_18_pdf_text_extraction_is_bounded_and_complete(self):
        with closing(ec._open_readonly(ec.DEFAULT_INDEX)) as db:
            failed=db.execute("SELECT count(*) FROM source WHERE kind='PDF' AND parse_status LIKE '%FAILED%'").fetchone()[0]
            pages=db.execute("SELECT count(*) FROM source_page").fetchone()[0]
            empty=db.execute("SELECT count(*) FROM source_page WHERE status='NO_TEXT_LAYER'").fetchone()[0]
        self.assertEqual(failed,0)
        self.assertEqual(pages,36)
        self.assertEqual(empty,0)

if __name__=="__main__":
    unittest.main()
