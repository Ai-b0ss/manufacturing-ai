"""Evidence-chain tests using synthetic data and isolated in-memory DB snapshots."""
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import closing

import app


PART="АБВГ.712345.004-02"


class EngineeringEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        base=pathlib.Path(cls.temp.name)
        cls.path=base/"engineering.sqlite3"
        app.initialize(cls.path,base/"creds.txt")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.db=sqlite3.connect(":memory:",isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        with closing(sqlite3.connect(self.path)) as source:
            source.backup(self.db)

    def tearDown(self):
        self.db.close()

    def report(self,role="engineer",lot="LOT-2026-10"):
        return app.part_report(self.db,role,PART,lot)

    def test_01_explicit_source_fk_is_seeded_and_not_fuzzy(self):
        row=self.db.execute("SELECT notice_id,document_id FROM change_notice_source").fetchall()
        self.assertEqual([(r["notice_id"],r["document_id"]) for r in row],
                         [("ИИ-017","DOC-003")])

    def test_02_evidence_chain_links_real_accessible_documents(self):
        result=self.report()
        self.assertEqual(result["state"],"VERIFIED_FOR_DEMO")
        self.assertEqual([x["doc_id"] for x in result["evidence"]],["DOC-001","DOC-003"])
        self.assertEqual(result["evidence"][1]["effective_from"],"2026-01-01")
        self.assertEqual(result["timeline"][0]["new_revision"],"02")
        for x in result["evidence"]:
            self.assertIsNotNone(app.get_document(self.db,"engineer",x["doc_id"]))

    def test_03_no_source_link_means_unverified(self):
        self.db.execute("DELETE FROM change_notice_source WHERE notice_id='ИИ-017'")
        result=self.report()
        self.assertEqual(result["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(result["issue"],"NOTICE_SOURCE_NOT_LINKED")

    def test_04_changed_or_mislabelled_source_rejected(self):
        self.db.execute("UPDATE document SET revision='99' WHERE id='DOC-003'")
        result=self.report()
        self.assertEqual(result["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(result["issue"],"NOTICE_SOURCE_MISMATCH")

    def test_05_bad_current_revision_rejected(self):
        self.db.execute("UPDATE part SET current_revision='04' WHERE id=?",(PART,))
        result=self.report()
        self.assertEqual(result["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(result["issue"],"CURRENT_REVISION_MISMATCH")

    def test_06_hidden_notice_source_cannot_leak_to_engineer(self):
        self.db.execute("UPDATE document SET scope_id='executive' WHERE id='DOC-003'")
        engineer=self.report("engineer")
        public=self.report("technologist")
        self.assertEqual(engineer["state"],"CURRENTNESS_UNVERIFIED")
        self.assertNotIn("evidence",engineer)
        self.assertNotIn("notice",engineer)
        self.assertEqual(public["state"],"CURRENTNESS_UNVERIFIED")
        director=self.report("director")
        self.assertEqual(director["state"],"VERIFIED_FOR_DEMO")

    def test_07_hidden_notice_itself_cannot_be_an_oracle(self):
        self.db.execute("UPDATE change_notice SET scope_id='executive' WHERE id='ИИ-017'")
        for lot_id in ["LOT-2025-12","LOT-2026-10","NON-EXISTENT",""]:
            result=app.part_report(self.db,"engineer",PART,lot_id)
            self.assertEqual(result["state"],"CURRENTNESS_UNVERIFIED")
            self.assertNotIn("notice",result)
            self.assertNotIn("evidence",result)
            self.assertNotIn("lot",result)

    def test_08_no_notices_never_yields_proven_currentness(self):
        self.db.execute("DELETE FROM change_notice_source")
        self.db.execute("DELETE FROM change_notice")
        r=self.report()
        self.assertEqual(r["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(r["issue"],"NO_NOTICE_CHAIN")

    def test_09_rework_for_legacy_lot_is_not_auto_approval(self):
        old=self.report(lot="LOT-2025-12")
        new=self.report(lot="LOT-2026-10")
        self.assertEqual(old["lot"]["built_revision"],"01")
        self.assertEqual(old["lot"]["expected_at_manufacture"],"01")
        self.assertEqual(old["lot"]["status"],"REQUIRES_ENGINEER_REVIEW")
        self.assertEqual(old["lot"]["directive"],"ДОРАБОТАТЬ")
        self.assertEqual(new["lot"]["built_revision"],"02")
        self.assertEqual(new["lot"]["expected_at_manufacture"],"02")
        self.assertEqual(new["lot"]["status"],"REVISION_MATCHES")

    def test_10_late_production_under_old_revision_is_flagged(self):
        self.db.execute("UPDATE lot SET revision_id='01' WHERE id='LOT-2026-10'")
        r=self.report()
        self.assertEqual(r["lot"]["expected_at_manufacture"],"02")
        self.assertFalse(r["lot"]["matches_at_manufacture"])
        self.assertEqual(r["lot"]["status"],"REQUIRES_ENGINEER_REVIEW")

    def test_11_early_production_claiming_future_revision_is_flagged(self):
        self.db.execute("UPDATE lot SET revision_id='02' WHERE id='LOT-2025-12'")
        r=self.report(lot="LOT-2025-12")
        self.assertEqual(r["lot"]["expected_at_manufacture"],"01")
        self.assertEqual(r["lot"]["status"],"REQUIRES_ENGINEER_REVIEW")

    def test_12_date_and_notice_sequence_integrity(self):
        self.db.execute("UPDATE change_notice SET valid_from='not-a-date' WHERE id='ИИ-017'")
        r=self.report()
        self.assertEqual(r["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(r["issue"],"INVALID_EFFECTIVE_DATE")

    def test_13_two_notice_timeline_and_batch_cutover(self):
        self.db.execute("INSERT INTO document VALUES(?,?,?,?,?,?)",
                        ("DOC-099","Учебное извещение ИИ-099","ИИ-099","change","engineering","03"))
        self.db.execute("INSERT INTO change_notice VALUES(?,?,?,?,?,?,?)",
                        ("ИИ-099",PART,"engineering","03","2026-07-01","ДОРАБОТАТЬ","Новый режим"))
        self.db.execute("INSERT INTO change_notice_source VALUES (?,?)",("ИИ-099","DOC-099"))
        self.db.execute("UPDATE part SET current_revision='03' WHERE id=?",(PART,))
        r=self.report()
        self.assertEqual([e["doc_id"] for e in r["evidence"]],["DOC-001","DOC-003","DOC-099"])
        self.assertEqual(r["state"],"VERIFIED_FOR_DEMO")
        self.assertEqual(r["lot"]["expected_at_manufacture"],"03")
        self.assertEqual(r["lot"]["status"],"REQUIRES_ENGINEER_REVIEW")

    def test_14_public_cannot_observe_any_lot_metadata(self):
        outputs=[app.part_report(self.db,"technologist",PART,lot)
                 for lot in ("LOT-2025-12","LOT-2026-10","unknown","")]
        self.assertTrue(all(x==outputs[0] for x in outputs))
        self.assertNotIn("notice",outputs[0])
        self.assertNotIn("timeline",outputs[0])

    def test_15_invalid_batch_date_is_not_treated_as_verified(self):
        self.db.execute("UPDATE lot SET manufactured_at='XXXX' WHERE id='LOT-2026-10'")
        r=self.report()
        self.assertEqual(r["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(r["issue"],"INVALID_BATCH_DATE")

    def test_17_migration_never_silently_recovers_revoked_evidence(self):
        self.db.execute("DELETE FROM change_notice_source WHERE notice_id='ИИ-017'")
        app.ensure_demo_notice_link(self.db)
        self.assertIsNone(self.db.execute(
            "SELECT document_id FROM change_notice_source WHERE notice_id='ИИ-017'"
        ).fetchone())
        self.assertEqual(self.report()["state"],"CURRENTNESS_UNVERIFIED")

    def test_18_lookalike_latin_character_not_auto_verified(self):
        mixed="AБВГ.712345.004-02"  # Latin A, remaining Cyrillic.
        r=app.part_report(self.db,"engineer",mixed,"LOT-2026-10")
        self.assertEqual(r["state"],"NOT_FOUND")

    def test_19_migration_marker_seeded_once(self):
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM app_schema_migration WHERE id='link_demo_notice_v1'"
        ).fetchone()[0],1)

    def test_20_future_notice_does_not_claim_current_revision(self):
        self.db.execute("UPDATE change_notice SET valid_from='2999-01-01' WHERE id='ИИ-017'")
        r=self.report()
        self.assertEqual(r["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(r["issue"],"NOTICE_NOT_YET_EFFECTIVE")

    def test_21_future_batch_date_is_not_a_manufactured_lot(self):
        self.db.execute("UPDATE lot SET manufactured_at='2999-01-01' WHERE id='LOT-2026-10'")
        r=self.report()
        self.assertEqual(r["state"],"CURRENTNESS_UNVERIFIED")
        self.assertEqual(r["issue"],"FUTURE_BATCH_DATE")

    def test_16_reinitialized_database_retains_all_original_rows(self):
        import sqlite3
        before={t:self.db.execute("SELECT COUNT(*) FROM "+t).fetchone()[0]
                for t in ("document","part","change_notice","lot","finance","change_notice_source")}
        self.db.executescript(app.SCHEMA)
        app.ensure_demo_notice_link(self.db)
        after={t:self.db.execute("SELECT COUNT(*) FROM "+t).fetchone()[0] for t in before}
        self.assertEqual(before,after)
