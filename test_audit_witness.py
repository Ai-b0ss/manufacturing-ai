"""P12 externally signed audit anchor: truncation, divergence and forgery."""
from __future__ import annotations

import base64
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

import audit_witness as witness
import visual_review as review

class SignedWitnessTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/"audit.sqlite3"
        disable=patch.dict(os.environ,{
            "P12_REVIEW_ANCHOR_FILE":"",
            "P12_REVIEW_ANCHOR_PUBLIC_KEY_HEX":""})
        disable.start();self.addCleanup(disable.stop)
        with closing(review._open_store(self.path)) as db:
            for i in (1,2):
                self._append(db,i)
        self.private=Ed25519PrivateKey.generate()
        self.public=self.private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        self.anchor=Path(self.folder.name)/"witness.json"

    @staticmethod
    def _append(db,i):
        with db:
            current=db.execute("SELECT event_hash FROM review_event ORDER BY seq DESC LIMIT 1").fetchone()
            prev=current["event_hash"] if current else review.GENESIS
            event_id=str(uuid.uuid4())
            record={
                "version":"p11-review-note/v1",
                "event_id":event_id,
                "part_key":"arcblue:focuser_drawtube",
                "created_at_ms":int(time.time()*1000)+i,
                "reviewer":"engineer",
                "decision":"NEEDS_MORE_EVIDENCE",
                "manufacturing_authorization":False
            }
            digest=review._digest(prev,record)
            db.execute(
                "INSERT INTO review_event(event_id,timestamp_ms,part_key,reviewer,record_json,previous_hash,event_hash) VALUES (?,?,?,?,?,?,?)",
                (event_id,record["created_at_ms"],record["part_key"],record["reviewer"],
                 review._encode(record).decode("utf-8"),prev,digest)
            )

    def create_anchor(self,seq=None,corrupt_signature=False):
        with closing(review._open_store(self.path)) as db:
            entries,head=review._validated_chain(db)
            payload=witness.proposal(db,entries,head)
        if seq is not None:
            payload["seq"]=seq
            if seq==0:payload["event_hash"]=witness.GENESIS
            else:payload["event_hash"]=entries[seq-1]["event_hash"]
        signature=self.private.sign(witness.canonical(payload))
        if corrupt_signature:
            signature=bytes([signature[0]^1])+signature[1:]
        signed={"payload":payload,"signature":base64.b64encode(signature).decode("ascii")}
        self.anchor.write_text(json.dumps(signed),encoding="utf-8")
        return {
            "P12_REVIEW_ANCHOR_FILE":str(self.anchor),
            "P12_REVIEW_ANCHOR_PUBLIC_KEY_HEX":self.public
        }

    def status(self):
        with closing(review._open_store(self.path)) as db:
            entries,head=review._validated_chain(db)
            return witness.check(db,entries,head)

    def test_01_without_external_witness_only_local_hashes(self):
        state=self.status()
        self.assertEqual(state["state"],"NO_EXTERNAL_WITNESS")
        self.assertEqual(state["latest_seq"],2)
        self.assertEqual(state["externally_witnessed_events"],0)

    def test_02_signed_head_is_valid_and_complete(self):
        env=self.create_anchor()
        with patch.dict(os.environ,env):
            state=self.status()
        self.assertEqual(state["state"],"SIGNED_PREFIX_VERIFIED")
        self.assertEqual(state["externally_witnessed_events"],2)
        self.assertEqual(state["unwitnessed_events"],0)
        self.assertFalse(state["manufacturing_authorization"])

    def test_03_signed_prefix_allows_later_unwitnessed_entries(self):
        env=self.create_anchor(seq=1)
        with patch.dict(os.environ,env):
            state=self.status()
        self.assertEqual(state["externally_witnessed_events"],1)
        self.assertEqual(state["unwitnessed_events"],1)

    def test_04_signed_head_detects_deleted_last_event(self):
        env=self.create_anchor()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM review_event WHERE seq=2")
            db.commit()
        with patch.dict(os.environ,env):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_HISTORY_TRUNCATED"):
                self.status()
        # Without anchor, the remaining chain is mathematically consistent.
        self.assertEqual(self.status()["latest_seq"],1)

    def test_05_signed_head_detects_rewritten_last_event(self):
        env=self.create_anchor()
        with closing(review._open_store(self.path)) as db:
            db.execute("DELETE FROM review_event WHERE seq=2")
            # Replace at same SQLite sequence to show local chain is self-consistent.
            prior=db.execute("SELECT event_hash FROM review_event WHERE seq=1").fetchone()["event_hash"]
            event_id=str(uuid.uuid4())
            note={"event_id":event_id,"part_key":"arcblue:focuser_drawtube",
                  "reviewer":"engineer","created_at_ms":123456,
                  "decision":"PLAUSIBLE_MAPPING"}
            digest=review._digest(prior,note)
            db.execute("INSERT INTO review_event(seq,event_id,timestamp_ms,part_key,reviewer,record_json,previous_hash,event_hash) VALUES (?,?,?,?,?,?,?,?)",
                (2,event_id,123456,note["part_key"],"engineer",
                 review._encode(note).decode("utf-8"),prior,digest))
        self.assertEqual(self.status()["latest_seq"],2)
        with patch.dict(os.environ,env):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_HISTORY_DIVERGED"):
                self.status()

    def test_06_unsigned_or_mutated_checkpoint_rejected(self):
        env=self.create_anchor()
        original=json.loads(self.anchor.read_text(encoding="utf-8"))
        original["payload"]["event_hash"]="0"*64
        self.anchor.write_text(json.dumps(original),encoding="utf-8")
        with patch.dict(os.environ,env):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_SIGNATURE_INVALID"):
                self.status()

    def test_07_different_trust_key_rejected(self):
        env=self.create_anchor()
        other=Ed25519PrivateKey.generate()
        env["P12_REVIEW_ANCHOR_PUBLIC_KEY_HEX"]=other.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        with patch.dict(os.environ,env):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_SIGNATURE_INVALID"):
                self.status()

    def test_08_deleted_anchor_fails_closed_if_configured(self):
        env=self.create_anchor()
        self.anchor.unlink()
        with patch.dict(os.environ,env):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_FILE_UNAVAILABLE"):
                self.status()

    def test_09_only_path_or_only_key_fails_closed(self):
        env=self.create_anchor()
        with patch.dict(os.environ,{"P12_REVIEW_ANCHOR_FILE":env["P12_REVIEW_ANCHOR_FILE"],
                                     "P12_REVIEW_ANCHOR_PUBLIC_KEY_HEX":""}):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_CONFIGURATION_INCOMPLETE"):
                self.status()
        with patch.dict(os.environ,{"P12_REVIEW_ANCHOR_FILE":"",
                                     "P12_REVIEW_ANCHOR_PUBLIC_KEY_HEX":self.public}):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_CONFIGURATION_INCOMPLETE"):
                self.status()

    def test_10_replacement_database_identity_fails(self):
        env=self.create_anchor()
        with closing(review._open_store(self.path)) as db:
            db.execute("DELETE FROM audit_identity")
            db.execute("INSERT INTO audit_identity(singleton,store_id) VALUES (1,?)",
                       (str(uuid.uuid4()),))
        with patch.dict(os.environ,env):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_STORE_MISMATCH"):
                self.status()

    def test_11_canonical_signed_genesis_allows_future_production_of_notes_only(self):
        env=self.create_anchor(seq=0)
        with patch.dict(os.environ,env):
            state=self.status()
        self.assertEqual(state["externally_witnessed_events"],0)
        self.assertEqual(state["unwitnessed_events"],2)
        self.assertFalse(state["manufacturing_authorization"])

    def test_12_invalid_gap_or_noncanonical_rows_denied(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE review_event SET seq=4 WHERE seq=2")
            db.commit()
        env=self.create_anchor(seq=0)
        with patch.dict(os.environ,env):
            with self.assertRaisesRegex(review.ReviewError,"WITNESS_SEQUENCE_GAP"):
                self.status()

    def test_13_anchor_status_offers_only_unsigned_proposal(self):
        state=review.audit_status(store=self.path)
        self.assertEqual(state["state"],"NO_EXTERNAL_WITNESS")
        self.assertEqual(state["unsigned_checkpoint_proposal"]["schema"],witness.SCHEMA)
        self.assertTrue(state["external_signature_required"])
        self.assertFalse(state["human_identity_verified"])

if __name__=="__main__":
    unittest.main()
