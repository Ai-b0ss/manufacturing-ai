"""Deterministic engineering retrieval checks on exclusively synthetic records.

The aim is to catch misleading numerics, designation collisions and ACL leaks,
not to measure LLM's general competence.
"""
import pathlib
import tempfile
import unittest
from contextlib import closing

import app
import ai_extension


class EngineeringRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir=tempfile.TemporaryDirectory(prefix="factory-eng-")
        cls.dbpath=pathlib.Path(cls.dir.name)/"engineering.sqlite3"
        app.initialize(cls.dbpath,pathlib.Path(cls.dir.name)/"creds")
        examples=[
            ("VALVE-908","Клапан VALVE-908","VALVE-908","public",
             "Клапан VALVE-908. Испытательное давление 25 бар, контроль фланца."),
            ("VALVE-9088","Клапан VALVE-9088","VALVE-9088","public",
             "Клапан VALVE-9088. Испытательное давление 125 бар, контроль седла."),
            ("DRAW-A01","Чертёж корпуса А-01","A-01","public",
             "Корпус А-01 выпущен по редакции 01. Архивный чертёж, не запускать производство."),
            ("DRAW-A02","Чертёж корпуса А-02","A-02","public",
             "Корпус А-02 выпущен по редакции 02. Проверить извещения, актуальность отдельно."),
            ("NOTICE-X077","Извещение X-077","X-077","engineering",
             "Извещение X-077: изменение шероховатости поверхности, требуется решение КБ."),
            ("COST-CONF","Финансовая информация","COST-007","executive",
             "Закрытая калькуляция Н-100: секретная премия 999999 рублей."),
            ("OCR-DEMO","Учебный скан","S-111","public",
             "Учебный скан S-111. Температура 78 градусов. Материал 12Х18Н1ОТ."),
        ]
        with closing(app.connect(cls.dbpath)) as db:
            for ident,title,designation,scope,text in examples:
                db.execute("INSERT INTO document VALUES(?,?,?,?,?,?)",
                           (ident,title,designation,"manual",scope,"учебный"))
                chunk="CHK-"+ident
                db.execute("INSERT INTO chunk VALUES(?,?,?)",(chunk,ident,text))
                db.execute("INSERT INTO chunk_fts(chunk_id,text) VALUES(?,?)",(chunk,text))
                if ident=="OCR-DEMO":
                    db.execute("INSERT INTO chunk_page(chunk_id,page_no,extraction_method) VALUES (?,?,?)",
                               (chunk,3,"ocr"))

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def search(self,account,question):
        with closing(app.connect(self.dbpath)) as db:
            return ai_extension.retrieve(db,account,question)

    def test_01_exact_code_is_not_substring_collision(self):
        items=self.search("technologist","VALVE-908")
        self.assertIn("VALVE-908",[x["doc_id"] for x in items])
        self.assertNotIn("VALVE-9088",[x["doc_id"] for x in items])
        self.assertEqual(next(x for x in items if x["doc_id"]=="VALVE-908")["identifier_match"],"exact")

    def test_02_other_code_stays_distinct(self):
        items=self.search("technologist","VALVE-9088")
        self.assertIn("VALVE-9088",[x["doc_id"] for x in items])
        self.assertNotIn("VALVE-908",[x["doc_id"] for x in items])

    def test_03_numerical_25_is_not_125(self):
        items=self.search("technologist","давление 25 бар")
        self.assertIn("VALVE-908",[x["doc_id"] for x in items])
        self.assertNotIn("VALVE-9088",[x["doc_id"] for x in items])

    def test_04_numerical_125_is_not_25(self):
        items=self.search("technologist","давление 125 бар")
        self.assertIn("VALVE-9088",[x["doc_id"] for x in items])
        self.assertNotIn("VALVE-908",[x["doc_id"] for x in items])

    def test_05_drawing_execution_01_is_not_02(self):
        items=self.search("technologist","Чертёж A-01")
        self.assertIn("DRAW-A01",[x["doc_id"] for x in items])
        self.assertNotIn("DRAW-A02",[x["doc_id"] for x in items])

    def test_06_drawing_execution_02_is_not_01(self):
        items=self.search("technologist","Чертёж A-02")
        self.assertIn("DRAW-A02",[x["doc_id"] for x in items])
        self.assertNotIn("DRAW-A01",[x["doc_id"] for x in items])

    def test_07_unknown_identifier_returns_no_sources(self):
        self.assertEqual(self.search("technologist","VALVE-9999"),[])

    def test_08_engineering_notice_scoped_before_ranking(self):
        self.assertEqual(self.search("technologist","X-077"),[])
        self.assertIn("NOTICE-X077",[x["doc_id"] for x in self.search("engineer","X-077")])

    def test_09_director_salary_never_leaks_via_retrieval(self):
        self.assertNotIn("COST-CONF",[x["doc_id"] for x in
                                     self.search("technologist","секретная премия 999999")])
        self.assertIn("COST-CONF",[x["doc_id"] for x in
                                  self.search("director","секретная премия 999999")])

    def test_10_ocr_provenance_follows_citations(self):
        items=self.search("technologist","S-111")
        item=next(x for x in items if x["doc_id"]=="OCR-DEMO")
        self.assertEqual(item["extraction_method"],"ocr")
        self.assertEqual(item["page"],3)

    def test_11_ocr_confusion_not_invented_as_exact_grade(self):
        # The OCR source says Cyrillic O, not zero, and must not be silently
        # treated as exact 12Х18Н10Т.
        items=self.search("technologist","12Х18Н10Т")
        self.assertNotIn("OCR-DEMO",[x["doc_id"] for x in items])

    def test_12_cross_script_code_is_marked_candidate_not_verified(self):
        items=self.search("technologist","12X18H10T")
        row=next(x for x in items if x["doc_id"]=="DOC-001")
        self.assertEqual(row["identifier_match"],"homoglyph_candidate")

    def test_13_ui_search_does_not_return_numeric_collision(self):
        with closing(app.connect(self.dbpath)) as db:
            found=app.lexical_search(db,"technologist","VALVE-908")
            self.assertIn("VALVE-908",[x["id"] for x in found])
            self.assertNotIn("VALVE-9088",[x["id"] for x in found])

    def test_14_ui_search_respects_25_125_distinction(self):
        with closing(app.connect(self.dbpath)) as db:
            found=app.lexical_search(db,"technologist","давление 25 бар")
            self.assertIn("VALVE-908",[x["id"] for x in found])
            self.assertNotIn("VALVE-9088",[x["id"] for x in found])


if __name__=="__main__":
    unittest.main()
