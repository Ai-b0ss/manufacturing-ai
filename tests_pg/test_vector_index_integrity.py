"""Pure reconciler regression tests, requiring no running services."""
from __future__ import annotations
import unittest

from tools.vector_index_integrity_audit import (
    Chunk, Document, Point, classify, compare_index, point_id,
)


def doc(name="A", scope="engineering", kind="postgres_ingest",
        deleted=False, index="indexed", delete=None, attempts=1):
    return Document(name, scope, kind, deleted, index, delete, attempts)


def chunk(docname="A", name="A-chunk-1"):
    return Chunk(name, docname)


def point(document="A", part="A-chunk-1", scope="engineering"):
    return Point(point_id(part), {
        "tenant_id": "synthetic-demo", "document_id": document,
        "chunk_id": part, "acl_scope_id": scope,
    })


def codes(report, field="issues"):
    return {item["code"] for item in report[field]}


class TestVectorIntegrity(unittest.TestCase):
    def test_indexed_matches(self):
        report = compare_index([doc()], [chunk()], [point()])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["counts"]["vector_points"], 1)
        self.assertEqual(report["issues"], [])

    def test_missing_indexed_point(self):
        report = compare_index([doc()], [chunk()], [])
        self.assertIn("INDEXED_CHUNK_MISSING_FROM_VECTOR", codes(report))
        self.assertEqual(report["status"], "failed")

    def test_orphan_point(self):
        report = compare_index([doc()], [chunk()], [point(), point("missing", "other")])
        self.assertIn("VECTOR_ORPHAN_POINT", codes(report))

    def test_wrong_scope(self):
        report = compare_index([doc()], [chunk()], [point(scope="finance")])
        self.assertIn("VECTOR_PAYLOAD_INCONSISTENT", codes(report))

    def test_forged_tenant(self):
        payload = point().payload.copy()
        payload["tenant_id"] = "attacker"
        report = compare_index([doc()], [chunk()], [Point(point_id("A-chunk-1"), payload)])
        self.assertIn("VECTOR_PAYLOAD_INCONSISTENT", codes(report))

    def test_source_text_in_payload_is_rejected(self):
        payload = point().payload.copy()
        payload["body"] = "TOP SECRET - must not enter Qdrant"
        report = compare_index([doc()], [chunk()], [Point(point_id("A-chunk-1"), payload)])
        self.assertIn("VECTOR_PAYLOAD_KEYS_INVALID", codes(report))
        self.assertNotIn("TOP SECRET", str(report))

    def test_missing_index_job(self):
        report = compare_index([doc(index=None)], [chunk()], [])
        self.assertIn("INGEST_INDEX_EVENT_MISSING", codes(report))

    def test_failed_outbox_retries_exhausted(self):
        report = compare_index([doc(index="failed", attempts=5)], [chunk()], [])
        self.assertIn("INGEST_RETRY_EXHAUSTED", codes(report))

    def test_pending_index_not_falsely_missing(self):
        report = compare_index([doc(index="pending")], [chunk()], [])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"]["chunks_awaiting_index_or_delete"], 1)
        self.assertIn("WORK_PENDING", codes(report, "warnings"))

    def test_pending_with_point_not_falsely_corrupt(self):
        report = compare_index([doc(index="processing")], [chunk()], [point()])
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("VECTOR_PRESENT_BEFORE_INDEX_ACK", codes(report, "warnings"))

    def test_delete_ack_must_leave_no_vector(self):
        report = compare_index([doc(deleted=True, delete="indexed")], [chunk()], [point()])
        self.assertIn("VECTOR_STALE_AFTER_DELETE_ACK", codes(report))

    def test_deleting_index_is_transient(self):
        report = compare_index([doc(deleted=True, delete="pending")], [chunk()], [point()])
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("VECTOR_PENDING_DELETE", codes(report, "warnings"))

    def test_deleted_final_without_point(self):
        report = compare_index([doc(deleted=True, delete="indexed")], [chunk()], [])
        self.assertEqual(report["status"], "passed")

    def test_dangling_chunk(self):
        report = compare_index([], [chunk()], [])
        self.assertIn("CHUNK_DOCUMENT_MISSING", codes(report))

    def test_active_document_missing_chunk(self):
        report = compare_index([doc()], [], [])
        self.assertIn("ACTIVE_DOCUMENT_HAS_NO_CHUNKS", codes(report))

    def test_seed_demo_corpus_has_no_outbox(self):
        report = compare_index([doc(kind="seed", index=None)], [chunk()], [point()])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(classify(doc(kind="seed", index=None)), "required")

    def test_duplicate_point(self):
        report = compare_index([doc()], [chunk()], [point(), point()])
        self.assertIn("VECTOR_POINT_DUPLICATED", codes(report))

    def test_point_id_is_deterministic_uuid5(self):
        self.assertEqual(point_id("A-chunk-1"), point_id("A-chunk-1"))
        self.assertNotEqual(point_id("A-chunk-1"), point_id("A-chunk-2"))

    def test_no_source_content_in_result(self):
        report = compare_index(
            [doc(name="private-document-a")],
            [chunk("private-document-a", "private-chunk-1")],
            [point(document="private-document-a", part="private-chunk-1", scope="other")]
        )
        self.assertNotIn("private-document-a", str(report))
        self.assertNotIn("private-chunk-1", str(report))

    def test_row_cap_fail_closed(self):
        report = compare_index([], [], [Point("no", {}) for _ in range(10001)])
        self.assertEqual(report["status"], "failed")
        self.assertIn("AUDIT_ROW_CAP_EXCEEDED", codes(report))

    def test_empty_collection_with_empty_db(self):
        report = compare_index([], [], [])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["counts"], {})


if __name__ == "__main__":
    unittest.main()
