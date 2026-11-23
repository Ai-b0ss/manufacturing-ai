"""Offline regression tests for read-only CAD and archive verification."""
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core06.private_cad_bundle import MAGIC
from tools.storage_integrity_audit import (
    check_revisions, hash_archive, verify_backup, verify_cad, compare_snapshot_counts,
)


def fixture(part="Part-001"):
    sources = [
        ("bom", "sample.csv", b"part,quantity\nPart-001,1\n"),
        ("drawing", "sample.pdf", b"%PDF-1.4\n%%EOF"),
        ("model", "sample.stp", b"ISO-10303-21;\nENDSEC;\nEND-ISO-10303-21;"),
    ]
    manifest = {"part": part, "files": [
        {"role": role, "name": name, "size": len(body)}
        for role, name, body in sources
    ]}
    header = json.dumps(manifest).encode("utf-8")
    blob = MAGIC + len(header).to_bytes(4, "big") + header + b"".join(
        body for _, _, body in sources
    )
    report = {
        "schema": "factory_private_cad_bundle_v1",
        "part": part,
        "state": "REVIEW_REQUIRED",
        "manufacturing_authorization": False,
        "sources": [
            {"role": role, "name": name, "size": len(body),
             "sha256": hashlib.sha256(body).hexdigest()}
            for role, name, body in sources
        ],
    }
    return blob, report, hashlib.sha256(blob).hexdigest()


class TestStorageAudit(unittest.TestCase):
    def test_untouched_bundle(self):
        blob, report, sha = fixture()
        self.assertEqual(verify_cad(blob, sha, report, "part-001"), ())

    def test_detects_package_mutation(self):
        blob, report, sha = fixture()
        self.assertIn("PACKAGE_SHA256_MISMATCH", verify_cad(blob + b"!", sha, report, "part-001"))
        self.assertIn("PACKAGE_STRUCTURE_INVALID", verify_cad(blob + b"!", sha, report, "part-001"))

    def test_report_source_sha_tamper(self):
        blob, report, sha = fixture()
        report["sources"][1]["sha256"] = "0" * 64
        self.assertIn("SOURCE_SHA256_MISMATCH", verify_cad(blob, sha, report, "part-001"))

    def test_report_source_name_tamper(self):
        blob, report, sha = fixture()
        report["sources"][0]["name"] = "wrong.csv"
        self.assertIn("SOURCE_FILENAME_MISMATCH", verify_cad(blob, sha, report, "part-001"))

    def test_report_source_size_tamper(self):
        blob, report, sha = fixture()
        report["sources"][2]["size"] += 1
        self.assertIn("SOURCE_SIZE_MISMATCH", verify_cad(blob, sha, report, "part-001"))

    def test_report_duplicate_role(self):
        blob, report, sha = fixture()
        report["sources"][1]["role"] = "bom"
        self.assertIn("REPORT_ROLES_INVALID", verify_cad(blob, sha, report, "part-001"))

    def test_report_must_not_approve_manufacture(self):
        blob, report, sha = fixture()
        report["manufacturing_authorization"] = True
        self.assertIn("REPORT_APPROVAL_INVALID", verify_cad(blob, sha, report, "part-001"))

    def test_report_rejects_approval_string(self):
        blob, report, sha = fixture()
        report["manufacturing_authorization"] = "false"
        self.assertIn("REPORT_APPROVAL_INVALID", verify_cad(blob, sha, report, "part-001"))

    def test_source_part_mismatch(self):
        blob, report, sha = fixture()
        report["part"] = "Different"
        findings = verify_cad(blob, sha, report, "part-001")
        self.assertIn("REPORT_PART_MISMATCH", findings)
        self.assertIn("KIT_PART_KEY_MISMATCH", findings)

    def test_untrusted_report_type(self):
        blob, _, sha = fixture()
        self.assertIn("REPORT_NOT_OBJECT", verify_cad(blob, sha, [], "part-001"))

    def test_corrupt_manifest(self):
        blob, report, sha = fixture()
        bad = blob[:7] + b"\xff\xff\xff\xff" + blob[11:]
        self.assertIn("PACKAGE_STRUCTURE_INVALID", verify_cad(bad, sha, report, "part-001"))

    def test_revision_series(self):
        self.assertEqual(check_revisions({"kit-a": 2}, [("kit-a", 1), ("kit-a", 2)]), ())
        self.assertIn("REVISION_SEQUENCE_BROKEN",
            check_revisions({"kit-a": 2}, [("kit-a", 2)]))
        self.assertIn("KIT_LATEST_REVISION_MISMATCH",
            check_revisions({"kit-a": 3}, [("kit-a", 1), ("kit-a", 2)]))
        self.assertIn("ORPHAN_REVISION",
            check_revisions({"kit-a": 1}, [("kit-b", 1), ("kit-a", 1)]))
        self.assertIn("KIT_LATEST_REVISION_MISMATCH", check_revisions({"kit-a": 0}, []))

    def test_backup_row_count_drift_is_flagged(self):
        source = {"counts": {"document": 14, "document_blob": 1,
                             "cad_kit": 1, "cad_kit_revision": 2, "cad_kit_request": 2}}
        current = {"counts": {"documents_total": 14, "documents": 1,
                              "cad_kits": 1, "cad_revisions": 3,
                              "idempotency_requests": 3}}
        mismatch = compare_snapshot_counts(source, current)
        self.assertEqual(mismatch["status"], "failed")
        self.assertEqual(mismatch["changed_tables"], ["cad_kit_revision", "cad_kit_request"])
        current["counts"]["cad_revisions"] = 2
        current["counts"]["idempotency_requests"] = 2
        matched = compare_snapshot_counts(source, current)
        self.assertEqual(matched["status"], "passed")
        self.assertEqual(matched["tables_checked"], 5)
        self.assertEqual(matched["meaning"], "ROW_COUNTS_ONLY_NOT_CONTENT_IDENTITY")

    def test_missing_backup_counts_cannot_be_called_verified(self):
        result = compare_snapshot_counts({}, {"counts": {}})
        self.assertEqual(result["status"], "unknown")

    def test_archive_hash_streams_and_detects_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "sample.dump"
            archive.write_bytes(b"x" * (3 * 1024 * 1024 + 99))
            digest, count = hash_archive(archive)
            self.assertEqual(count, archive.stat().st_size)
            self.assertEqual(digest, hashlib.sha256(archive.read_bytes()).hexdigest())
            receipt = Path(td) / "receipt.json"
            receipt.write_text(json.dumps({
                "backup": str(archive), "sha256": digest, "dump_bytes": count
            }), encoding="utf8")
            archive.write_bytes(b"z" + archive.read_bytes()[1:])
            failure = verify_backup(receipt, Path(__file__))
            self.assertEqual(failure["status"], "failed")
            self.assertIn("ARCHIVE_SHA256_MISMATCH", failure["issues"])

    def test_empty_manifest_part_resolves_to_model_stem(self):
        blob, report, sha = fixture(part="")
        report["part"] = "sample"
        self.assertEqual(verify_cad(blob, sha, report, "sample"), ())
        report["part"] = "other_part"
        self.assertIn("REPORT_PART_MISMATCH", verify_cad(blob, sha, report, "other_part"))

    def test_verify_backup_requires_acl_entries(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "sample.dump"
            archive.write_bytes(b"PGDMP-synthetic")
            digest, count = hash_archive(archive)
            receipt = Path(td) / "receipt.json"
            receipt.write_text(json.dumps({
                "backup": str(archive), "sha256": digest, "dump_bytes": count
            }), encoding="utf8")
            tables_only = (
                "10; 0 0 TABLE DATA core document_blob factory_owner\n"
                "11; 0 0 TABLE DATA core cad_kit factory_owner\n"
                "12; 0 0 TABLE DATA core cad_kit_revision factory_owner\n"
                "13; 0 0 TABLE DATA core cad_kit_request factory_owner\n"
            ).encode("utf-8")
            with patch("tools.storage_integrity_audit.subprocess.run",
                       return_value=subprocess.CompletedProcess(["pg_restore"], 0, tables_only, b"")):
                missing_acl = verify_backup(receipt, Path(__file__))
            self.assertEqual(missing_acl["status"], "failed")
            self.assertIn("PG_ARCHIVE_PRIVILEGES_MISSING", missing_acl["issues"])

            with_acls = tables_only + (
                "14; 0 0 ACL - SCHEMA core factory_owner\n"
                "15; 0 0 ACL core TABLE document factory_owner\n"
            ).encode("utf-8")
            with patch("tools.storage_integrity_audit.subprocess.run",
                       return_value=subprocess.CompletedProcess(["pg_restore"], 0, with_acls, b"")):
                passed = verify_backup(receipt, Path(__file__))
            self.assertEqual(passed["status"], "passed")
            self.assertEqual(passed["issues"], [])


if __name__ == "__main__":
    unittest.main()
