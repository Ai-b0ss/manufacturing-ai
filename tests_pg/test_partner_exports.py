import base64
from datetime import datetime, timezone
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from core06.partner_exports import AppliedLot, correlate_lot, preview_finance
from revision_gateway import SCHEMA, canonical


class PartnerExportTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
        self.key = Ed25519PrivateKey.generate()
        self.keys = {"plm-fixture": self.key.public_key().public_bytes_raw().hex()}
        self.release = {
            "schema": SCHEMA, "issuer": "Fixture PDM", "source_system": "PLM",
            "issued_at": "2026-11-18T10:00:00Z", "expires_at": "2026-11-20T10:00:00Z",
            "part_key": "drawtube", "revision": "B", "status": "RELEASED",
            "drawing_sha256": "a" * 64, "cad_sha256": "b" * 64, "release_event_id": "PLM-R17",
        }
        self.lot = {
            "schema_version": "factory-1c-applied-lot/v1", "source_event_id": "ERP-A51",
            "part_key": "drawtube", "lot_id": "LOT-005", "recorded_on": "2026-11-18",
            "applied_revision": "B", "plm_release_event_id": "PLM-R17",
        }

    def envelope(self):
        return {"key_id": "plm-fixture", "payload": dict(self.release), "signature": (
            base64.b64encode(self.key.sign(canonical(self.release))).decode("ascii")
        )}

    def assess(self, envelope, **lot_overrides):
        lot = AppliedLot.model_validate_json(json.dumps(self.lot | lot_overrides))
        return correlate_lot(lot, envelope, self.keys, "a" * 64, "b" * 64, at=self.at)

    def test_release_matches_explicit_application_but_never_authorizes(self):
        result = self.assess(self.envelope())
        self.assertEqual(result["state"], "EXPORTS_MATCH")
        self.assertEqual(result["erp_event_id"], "ERP-A51")
        self.assertFalse(result["manufacturing_authorization"])

    def test_posting_date_does_not_imply_revision(self):
        result = self.assess(self.envelope(), applied_revision=None)
        self.assertEqual(result["reason"], "APPLIED_REVISION_MISSING")

    def test_revision_mismatch_and_tampering_require_review(self):
        env = self.envelope()
        self.assertEqual(self.assess(env, applied_revision="A")["reason"], "APPLIED_REVISION_MISMATCH")
        env["payload"]["release_event_id"] = "FORGED"
        self.assertEqual(self.assess(env)["reason"], "UNTRUSTED_RELEASE")

    def test_revoked_wrong_system_and_wrong_hash_fail_closed(self):
        self.release["status"] = "REVOKED"
        self.assertEqual(self.assess(self.envelope())["reason"], "RELEASE_NOT_CONFIRMED_IN_EXPORT")
        self.release.update(status="RELEASED", source_system="1C_ERP")
        self.assertEqual(self.assess(self.envelope())["reason"], "RELEASE_NOT_CONFIRMED_IN_EXPORT")
        self.release.update(source_system="PLM", cad_sha256="c" * 64)
        self.assertEqual(self.assess(self.envelope())["reason"], "PART_OR_SOURCE_HASH_MISMATCH")

    def test_finance_groups_by_month_and_currency_and_excludes_order_ids(self):
        row = dict(order_id="ORDER-A", posting_date="2026-11-01", currency="RUB",
                   planned_minor=101, actual_minor=120)
        batch = {"schema_version": "factory-1c-order-actuals/v1", "batch_id": "batch-001",
                 "records": [row, row | {"order_id": "ORDER-B", "currency": "USD"},
                             row | {"order_id": "ORDER-C", "actual_minor": 94}]}
        projected = json.loads(preview_finance(json.dumps(batch).encode()))
        self.assertEqual(projected["rows"], [
            {"month": "2026-11", "currency": "RUB", "planned_minor": 202,
             "actual_minor": 214, "variance_minor": 12},
            {"month": "2026-11", "currency": "USD", "planned_minor": 101,
             "actual_minor": 120, "variance_minor": 19},
        ])
        batch["records"] = [row, row]
        with self.assertRaisesRegex(ValueError, "DUPLICATE_ORDER"):
            preview_finance(json.dumps(batch).encode())
        batch["records"] = [row | {"actual_minor": "120", "private_note": "x"}]
        with self.assertRaises(ValidationError):
            preview_finance(json.dumps(batch).encode())
