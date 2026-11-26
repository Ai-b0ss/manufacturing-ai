"""Targeted regressions and negative tests for subject-matter quality defects D1-D5.

Pure stdlib + SQLite + local synthetic fixtures (runs offline in CI without
PostgreSQL, Qdrant, Keycloak, or external model weights).
"""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import ai_extension
import app
from core06 import text_grounding as tg
from core06 import unified_ai

SYNTH_OCR_PUMP_TEXT = (
    "УЧЕБНЫЙ ПАСПОРТ ОБОРУДОВАНИЯ Насос Н-125, корпус АБВГ.712З45.ОО4-О2. "
    "Испытательное давление 25 бар. Температура 78 градусов. "
    "Марка материала 12Х18Н10Т. Перед пуском проверить фланец и смазку."
)

SYNTH_N125M_TEXT = (
    "Насос Н-125М, корпус АБВГ.712345.004-03, ревизия 02 от 15.02.2026. "
    "Испытательное давление 40 бар. Рабочая температура 95 градусов. "
    "Марка материала 08Х18Н10Т. Момент затяжки шпилек фланца 140 Нм. "
    "Не смешивать с базовым насосом Н-125 исполнения -02."
)


class QualityGroundingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="factory-d1-d5-")
        cls.dir = pathlib.Path(cls.tmp.name)
        cls.dbpath = cls.dir / "quality.sqlite3"
        app.initialize(cls.dbpath, cls.dir / "creds.txt")
        with contextlib.closing(app.connect(cls.dbpath)) as db:
            # Seed synthetic OCR pump passport (filename without Н-125, mirroring standalone upload)
            db.execute(
                "INSERT INTO document VALUES(?,?,?,?,?,?)",
                ("UPL-OCR-PUMP", "factory_scanned_pump_demo.pdf", "factory_scanned_pump_demo.pdf",
                 "uploaded", "personal-engineer", "загружен"),
            )
            db.execute(
                "INSERT INTO chunk VALUES(?,?,?)",
                ("UPL-OCR-PUMP-0001", "UPL-OCR-PUMP", "DOCUMENT INTRODUCTION, TRAINING ONLY. PAGE ONE."),
            )
            db.execute(
                "INSERT INTO chunk_page VALUES(?,?,?)",
                ("UPL-OCR-PUMP-0001", 1, "text"),
            )
            db.execute(
                "INSERT INTO chunk VALUES(?,?,?)",
                ("UPL-OCR-PUMP-0002", "UPL-OCR-PUMP", SYNTH_OCR_PUMP_TEXT),
            )
            db.execute(
                "INSERT INTO chunk_page VALUES(?,?,?)",
                ("UPL-OCR-PUMP-0002", 2, "ocr"),
            )
            # Seed synthetic distractor Н-125М with negative reference to Н-125
            db.execute(
                "INSERT INTO document VALUES(?,?,?,?,?,?)",
                ("UPL-SYNTH-N125M", "synth_n125m_rev02.txt", "synth_n125m_rev02.txt",
                 "uploaded", "personal-engineer", "загружен"),
            )
            db.execute(
                "INSERT INTO chunk VALUES(?,?,?)",
                ("UPL-SYNTH-N125M-0001", "UPL-SYNTH-N125M", SYNTH_N125M_TEXT),
            )
            db.execute(
                "INSERT INTO chunk_page VALUES(?,?,?)",
                ("UPL-SYNTH-N125M-0001", 1, "text"),
            )
            # Negative control: non-OCR chunk with OCR-corrupted code must NOT match via OCR-fuzzy
            db.execute(
                "INSERT INTO document VALUES(?,?,?,?,?,?)",
                ("TXT-CORRUPT-CODE", "Текстовый паспорт оборудования", "TXT-BAD",
                 "manual", "public", "учебный"),
            )
            db.execute(
                "INSERT INTO chunk VALUES(?,?,?)",
                ("CHK-TXT-CORRUPT-CODE", "TXT-CORRUPT-CODE",
                 "Паспорт оборудования корпуса АБВГ.712З45.ОО4-О2. Перед пуском проверить фланец и смазку."),
            )
            db.execute(
                "INSERT INTO chunk_page VALUES(?,?,?)",
                ("CHK-TXT-CORRUPT-CODE", 1, "text"),
            )
            # Negative control: OCR chunk with DIFFERENT execution -03 (О3) must NOT match -02
            db.execute(
                "INSERT INTO document VALUES(?,?,?,?,?,?)",
                ("OCR-WRONG-EXEC", "Скан паспорта исполнения -03", "OCR-03",
                 "manual", "personal-engineer", "учебный"),
            )
            db.execute(
                "INSERT INTO chunk VALUES(?,?,?)",
                ("CHK-OCR-WRONG-EXEC", "OCR-WRONG-EXEC",
                 "УЧЕБНЫЙ ПАСПОРТ ОБОРУДОВАНИЯ Насос Н-125М, корпус АБВГ.712З45.ОО4-О3. Перед пуском проверить фланец и смазку."),
            )
            db.execute(
                "INSERT INTO chunk_page VALUES(?,?,?)",
                ("CHK-OCR-WRONG-EXEC", 1, "ocr"),
            )
            # Negative control mirroring test_11: bare OCR grade confusion with no context match
            db.execute(
                "INSERT INTO document VALUES(?,?,?,?,?,?)",
                ("OCR-GRADE-ONLY", "Учебный скан", "S-111", "manual", "public", "учебный"),
            )
            db.execute(
                "INSERT INTO chunk VALUES(?,?,?)",
                ("CHK-OCR-GRADE-ONLY", "OCR-GRADE-ONLY",
                 "Учебный скан S-111. Температура 78 градусов. Материал 12Х18Н1ОТ."),
            )
            db.execute(
                "INSERT INTO chunk_page VALUES(?,?,?)",
                ("CHK-OCR-GRADE-ONLY", 3, "ocr"),
            )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _retrieve(self, subject, question):
        with contextlib.closing(app.connect(self.dbpath)) as db:
            return ai_extension.retrieve(db, subject, question)

    def _mock_generate(self, question, evidence, payload):
        response = {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"completion_tokens": 16},
        }
        buf = io.BytesIO(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        with (
            mock.patch.object(ai_extension, "_health", return_value=True),
            mock.patch.object(ai_extension, "_key", return_value="test-key"),
            mock.patch.object(ai_extension.urllib.request, "urlopen", return_value=buf),
        ):
            return ai_extension.generate(question, evidence)

    # --- D1: N-125 vs N-125M disambiguation & negative-mention suppression ---

    def test_d1_q03_ranks_n125_passport_first_and_excludes_negative_distractor(self):
        q03 = "Какое испытательное давление и рабочая температура указаны в паспорте насоса Н-125?"
        hits = self._retrieve("engineer", q03)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["doc_id"], "UPL-OCR-PUMP")
        self.assertEqual(hits[0]["page"], 2)
        self.assertIn("25 бар", hits[0]["body"])
        self.assertNotIn("UPL-SYNTH-N125M", [h["doc_id"] for h in hits])

    def test_d1_q04_still_retrieves_n125m_when_explicitly_requested(self):
        q04 = "Какое испытательное давление и момент затяжки фланца для насоса Н-125М (АБВГ.712345.004-03)?"
        hits = self._retrieve("engineer", q04)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["doc_id"], "UPL-SYNTH-N125M")
        self.assertIn("40 бар", hits[0]["body"])
        self.assertNotIn("UPL-OCR-PUMP", [h["doc_id"] for h in hits])

    def test_d1_negative_only_distractor_returns_empty_when_target_missing(self):
        # Even if only Н-125М exists, asking for Н-125 must NOT return Н-125М's 40 bar.
        with tempfile.TemporaryDirectory(prefix="factory-d1-neg-") as td:
            p = pathlib.Path(td) / "neg.sqlite3"
            app.initialize(p, pathlib.Path(td) / "c.txt")
            with contextlib.closing(app.connect(p)) as db:
                db.execute(
                    "INSERT INTO document VALUES(?,?,?,?,?,?)",
                    ("ONLY-N125M", "synth_n125m_rev02.txt", "Н-125М", "uploaded", "personal-engineer", "02"),
                )
                db.execute("INSERT INTO chunk VALUES(?,?,?)", ("CHK-ONLY-N125M", "ONLY-N125M", SYNTH_N125M_TEXT))
                self.assertEqual(
                    ai_extension.retrieve(db, "engineer", "Какое испытательное давление у насоса Н-125?"),
                    [],
                )
                self.assertEqual(
                    app.lexical_search(db, "engineer", "Какое испытательное давление у насоса Н-125?"),
                    [],
                )

    def test_d1_comparison_query_allows_both_variants(self):
        hits = self._retrieve("engineer", "Чем отличается насос Н-125М от Н-125?")
        ids = {h["doc_id"] for h in hits}
        self.assertIn("UPL-OCR-PUMP", ids)
        self.assertIn("UPL-SYNTH-N125M", ids)

    # --- D2: Missing identifier fail-closed & citation grounding post-check ---

    def test_d2_missing_revoked_identifier_returns_empty(self):
        # technologist lacks 'engineering' scope (mirrors post-revoke Q12b)
        q12b = "Что указано в извещении ИИ-017 для детали АБВГ.712345.004-02?"
        self.assertEqual(self._retrieve("technologist", q12b), [])
        with contextlib.closing(app.connect(self.dbpath)) as db:
            self.assertEqual(app.lexical_search(db, "technologist", q12b), [])

    def test_d2_generate_rejects_hallucinated_citation_missing_identifier(self):
        q12b = "Что указано в извещении ИИ-017 для детали АБВГ.712345.004-02?"
        doc1_evidence = [{
            "doc_id": "DOC-001",
            "title": "Корпус насоса Н-100 — архивный чертёж детали",
            "designation": "АБВГ.712345.004-02",
            "body": "Деталь АБВГ.712345.004-02. Материал 12Х18Н10Т. Базовый чертёж ревизии 01 от 2024 года.",
            "page": 1,
            "extraction_method": "text",
            "identifier_match": "exact",
        }]
        res = self._mock_generate(
            q12b,
            doc1_evidence,
            {"answer": "Извещение ИИ-017 указано в тексте [1].", "citations": [1]},
        )
        self.assertEqual(res["citations"], [])
        self.assertTrue(res["citation_warning"])
        self.assertNotIn("[1]", res["answer"])

    def test_d2_unified_ai_generated_response_blocks_ungrounded_citation(self):
        q12b = "Что указано в извещении ИИ-017 для детали АБВГ.712345.004-02?"
        doc1 = SimpleNamespace(
            chunk_id="c1", document_id="DOC-001", title="Чертёж детали",
            text="Деталь АБВГ.712345.004-02. Материал 12Х18Н10Т. Ревизия 01.",
            page_no=1, revision="01", score=0.86, extraction_method="text",
            designation="АБВГ.712345.004-02", doc_type="drawing", identifier_match="exact",
        )
        doc3 = SimpleNamespace(
            chunk_id="c3", document_id="DOC-003", title="Извещение ИИ-017",
            text="ИИ-017 для АБВГ.712345.004-02: ревизия 02 с 01.01.2026. Для старого задела указание: доработать.",
            page_no=1, revision="02", score=0.93, extraction_method="text",
            designation="ИИ-017", doc_type="change", identifier_match="exact",
        )
        # Hallucinating ИИ-017 while citing DOC-001 -> must fail closed (None)
        self.assertIsNone(
            unified_ai.generated_response(
                [doc1],
                {"answer": "Извещение ИИ-017 требует уточнения [1].", "citations": [1]},
                question=q12b,
            )
        )
        # Even without question param, mentioning ИИ-017 in answer while citing DOC-001 -> None
        self.assertIsNone(
            unified_ai.generated_response(
                [doc1],
                {"answer": "По извещению ИИ-017 требуется доработка [1].", "citations": [1]},
            )
        )
        # Even if answer omits ИИ-017 text, question asks about ИИ-017 and only DOC-001 is cited -> None
        self.assertIsNone(
            unified_ai.generated_response(
                [doc1, doc3],
                {"answer": "Требуется уточнение параметров [1].", "citations": [1]},
                question=q12b,
            )
        )
        # Citing DOC-003 (which grounds BOTH ИИ-017 and АБВГ.712345.004-02) -> accepted
        ok = unified_ai.generated_response(
            [doc1, doc3],
            {"answer": "По ИИ-017 новая ревизия 02, старый задел доработать [2].", "citations": [2]},
            question=q12b,
        )
        self.assertIsNotNone(ok)
        self.assertEqual(ok["mode"], "local_llm_advisory")
        self.assertEqual(ok["citations"], [2])

    # --- D3: Safe PostgreSQL Russian/simple OR tsquery builder ---

    def test_d3_pg_tsquery_builder_handles_russian_questions_and_sql_injection(self):
        q03 = "Какое испытательное давление и рабочая температура указаны в паспорте насоса Н-125?"
        ru_tsq, simple_tsq = tg.build_pg_tsqueries(q03)
        self.assertIn("|", ru_tsq)
        self.assertIn("давление", ru_tsq)
        self.assertIn("паспорте", ru_tsq)
        self.assertNotIn("какое", ru_tsq)
        self.assertNotIn("указаны", ru_tsq)
        self.assertIn("(н <-> 125)", simple_tsq)

        # Latin homoglyph code emits Cyrillic candidate into simple tsquery
        _, s_homo = tg.build_pg_tsqueries("В каких документах указан материал 12X18H10T?")
        self.assertIn("12х18н10т", s_homo)

        # SQL injection / tsquery metacharacters never leak raw operators
        inj = "' OR 1=1; DROP TABLE core.document;-- !&|():*'"
        ru_inj, simple_inj = tg.build_pg_tsqueries(inj)
        for expr in (ru_inj, simple_inj):
            self.assertNotIn("'", expr)
            self.assertNotIn(";", expr)
            self.assertNotIn("!", expr)
            self.assertNotIn(":*", expr)

    # --- D4: OCR fuzzy recall (О/0, З/3) with strict safety boundaries ---

    def test_d4_q07_recovers_winrt_ocr_pump_with_explicit_fuzzy_flag(self):
        q07 = "Что нужно проверить перед пуском корпуса АБВГ.712345.004-02 согласно паспорту оборудования?"
        hits = self._retrieve("engineer", q07)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["doc_id"], "UPL-OCR-PUMP")
        self.assertEqual(hits[0]["page"], 2)
        self.assertEqual(hits[0]["extraction_method"], "ocr")
        self.assertEqual(hits[0]["identifier_match"], "ocr_fuzzy_candidate")
        # Negative controls must NOT appear
        hit_ids = [h["doc_id"] for h in hits]
        self.assertNotIn("TXT-CORRUPT-CODE", hit_ids)
        self.assertNotIn("OCR-WRONG-EXEC", hit_ids)

    def test_d4_bare_material_grade_never_matches_ocr_confusion(self):
        hits = self._retrieve("technologist", "12Х18Н10Т")
        self.assertIn("DOC-001", [h["doc_id"] for h in hits])
        self.assertNotIn("OCR-GRADE-ONLY", [h["doc_id"] for h in hits])

    # --- D5: Inline citation preservation & v0.6 homoglyph/OCR-fuzzy propagation ---

    def test_d5_inline_citations_preserved_and_forged_stripped_cleanly(self):
        evidence = [
            {"doc_id": "DOC-001", "title": "Чертёж", "body": "Материал 12Х18Н10Т.",
             "page": 1, "extraction_method": "text", "identifier_match": "homoglyph_candidate"},
            {"doc_id": "UPL-OCR-PUMP", "title": "Паспорт", "body": "Марка материала 12Х18Н10Т.",
             "page": 2, "extraction_method": "ocr", "identifier_match": "homoglyph_candidate"},
        ]
        res = self._mock_generate(
            "В каких документах указан материал 12X18H10T?",
            evidence,
            {"answer": "В документах [1] и [2] указан материал 12X18H10T.", "citations": [1, 2]},
        )
        self.assertEqual(res["answer"], "В документах [1] и [2] указан материал 12X18H10T.")
        self.assertNotIn("  ", res["answer"])
        self.assertEqual(res["citations"], [1, 2])
        self.assertFalse(res["citation_warning"])

        # Forged [99] and uncited [2] are removed without leaving double spaces or broken punctuation
        formatted = tg.format_verified_citations("В документе [1] и [99] указано давление [2] .", [1])
        self.assertEqual(formatted, "В документе [1] и указано давление.")
        self.assertNotIn("[99]", formatted)
        self.assertNotIn("[2]", formatted)

    def test_d5_unified_ai_propagates_homoglyph_and_ocr_fuzzy_flags(self):
        h_homo = SimpleNamespace(
            chunk_id="c1", document_id="DOC-001", title="Чертёж", text="Материал 12Х18Н10Т",
            page_no=1, revision="01", score=0.84, extraction_method="text",
            designation="АБВГ.712345.004-02", doc_type="drawing", identifier_match="homoglyph_candidate",
        )
        h_ocr = SimpleNamespace(
            chunk_id="c2", document_id="UPL-OCR", title="Паспорт", text=SYNTH_OCR_PUMP_TEXT,
            page_no=2, revision="01", score=0.89, extraction_method="ocr",
            designation="", doc_type="uploaded", identifier_match="ocr_fuzzy_candidate",
        )
        ev = unified_ai.evidence_from_hits([h_homo, h_ocr])
        self.assertEqual(ev[0]["identifier_match"], "homoglyph_candidate")
        self.assertEqual(ev[1]["identifier_match"], "ocr_fuzzy_candidate")
        cards = unified_ai.source_cards([h_homo, h_ocr], citations=[1, 2])
        self.assertEqual(cards[0]["identifier_match"], "homoglyph_candidate")
        self.assertEqual(cards[1]["identifier_match"], "ocr_fuzzy_candidate")

    def test_d1_negative_mention_preserves_dotted_codes_and_abbreviations(self):
        # Dots inside ESKD codes or technical abbreviations must NOT break exclusion clauses
        for body in (
            "Не смешивать с АБВГ.712345.004-02 и базовым насосом Н-125.",
            "Не смешивать с изд. Н-125 исполнения -02.",
            "В отличие от п. 2, насос Н-125 рассчитан на 25 бар.",
        ):
            ev = tg.evaluate_identifier_match("Какое давление у Н-125?", "Насос Н-125М", "SYNTH_N125M", body)
            self.assertEqual(ev.matched_keys, frozenset(), msg=body)
            self.assertIn("Н125", ev.negative_only_keys, msg=body)

        # True sentence boundary still starts a fresh positive clause
        pos_body = "Не смешивать с базовым насосом. Насос Н-125 работает при 25 бар."
        ev_pos = tg.evaluate_identifier_match("Какое давление у Н-125?", "Паспорт", "", pos_body)
        self.assertIn("Н125", ev_pos.matched_keys)
        self.assertEqual(ev_pos.negative_only_keys, frozenset())

    def test_d3_fts_where_clauses_match_single_table_gin_indexes(self):
        root = pathlib.Path(__file__).resolve().parent
        schema = (root / "core06" / "schema.sql").read_text(encoding="utf-8")
        for expected_idx in (
            "chunk_fts_ru_gin ON core.document_chunk USING gin(to_tsvector('russian'::regconfig,body))",
            "doc_fts_ru_gin ON core.document USING gin(to_tsvector('russian'::regconfig,title||' '||designation))",
            "chunk_fts_simple_code_gin ON core.document_chunk USING gin(to_tsvector('simple'::regconfig,translate(body,'.-','  ')))",
            "doc_fts_simple_code_gin ON core.document USING gin(to_tsvector('simple'::regconfig,translate(title||' '||designation,'.-','  ')))",
        ):
            self.assertIn(expected_idx, schema)
        for rel in ("core06/db_client.py", "core06/hybrid_retrieval.py", "p2_gateway/app.py"):
            src = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("concat_ws(' ',d.title,d.designation,c.body)", src, msg=rel)
            self.assertIn("to_tsvector('russian'::regconfig,c.body) @@ to_tsquery('russian',%s)", src, msg=rel)
            self.assertIn("to_tsvector('russian'::regconfig,d.title||' '||d.designation) @@ to_tsquery('russian',%s)", src, msg=rel)
            self.assertIn("to_tsvector('simple'::regconfig,translate(c.body,'.-','  ')) @@ to_tsquery('simple',%s)", src, msg=rel)
            self.assertIn("to_tsvector('simple'::regconfig,translate(d.title||' '||d.designation,'.-','  ')) @@ to_tsquery('simple',%s)", src, msg=rel)


if __name__ == "__main__":
    unittest.main()
