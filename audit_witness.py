"""Optional independent Ed25519-signed witness of local demo-review history.

The local hash chain alone cannot detect truncation of its tail. An externally
held checkpoint, signed by an authority that does NOT share the app's secrets,
pins (store_id, seq, event_hash); replayed/truncated/diverged history is
rejected. The app NEVER creates a trusted signing key or claims a legal e-sign.
A witness can cover only a prefix; later records remain explicitly unwitnessed.
"""
from __future__ import annotations

import base64
from datetime import datetime,timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

SCHEMA="factory-review-checkpoint/v1"
GENESIS="0"*64
MAX_FILE=8192

class WitnessError(ValueError):
    pass

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),
                      ensure_ascii=False,allow_nan=False).encode("utf-8")

def ensure_store_identity(db:sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS audit_identity (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
      store_id TEXT NOT NULL UNIQUE
    )""")
    db.execute("INSERT OR IGNORE INTO audit_identity(singleton,store_id) VALUES(1,?)",
               (str(uuid.uuid4()),))
    row=db.execute("SELECT store_id FROM audit_identity WHERE singleton=1").fetchone()
    if row is None:raise WitnessError("STORE_ID_MISSING")
    try:
        value=uuid.UUID(row["store_id"])
        if value.version!=4 or str(value)!=row["store_id"]:
            raise ValueError("invalid UUID v4")
    except (ValueError,TypeError,KeyError) as exc:
        raise WitnessError("STORE_ID_INVALID") from exc
    return str(value)

def proposal(db,entries,last_hash):
    return {
        "schema":SCHEMA,
        "store_id":ensure_store_identity(db),
        "seq":entries[-1]["seq"] if entries else 0,
        "event_hash":last_hash,
        "created_at":datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
    }

def _validate(value):
    if not isinstance(value,dict) or set(value)!={"schema","store_id","seq","event_hash","created_at"}:
        raise WitnessError("WITNESS_FIELDS_INVALID")
    if value["schema"]!=SCHEMA:raise WitnessError("WITNESS_SCHEMA_INVALID")
    try:
        token=uuid.UUID(value["store_id"])
        if token.version!=4 or str(token)!=value["store_id"]:
            raise ValueError("noncanonical ID")
    except (ValueError,TypeError,AttributeError) as exc:raise WitnessError("WITNESS_STORE_ID_INVALID") from exc
    if not isinstance(value["seq"],int) or isinstance(value["seq"],bool) or not 0<=value["seq"]<=50_000:
        raise WitnessError("WITNESS_SEQUENCE_INVALID")
    if not isinstance(value["event_hash"],str) or not re.fullmatch(r"[0-9a-f]{64}",value["event_hash"]):
        raise WitnessError("WITNESS_HASH_INVALID")
    when=value["created_at"]
    if not isinstance(when,str) or not when.endswith("Z"):
        raise WitnessError("WITNESS_TIMESTAMP_INVALID")
    try:
        parsed=datetime.fromisoformat(when.replace("Z","+00:00"))
    except ValueError as exc:raise WitnessError("WITNESS_TIMESTAMP_INVALID") from exc
    if parsed>datetime.now(timezone.utc):
        raise WitnessError("WITNESS_FUTURE_TIMESTAMP")
    return value

def verify_signed_checkpoint(envelope,key_hex):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    if not isinstance(envelope,dict) or set(envelope)!={"payload","signature"}:
        raise WitnessError("WITNESS_ENVELOPE_INVALID")
    payload=_validate(envelope["payload"])
    try:
        key=bytes.fromhex(key_hex)
        sig=base64.b64decode(envelope["signature"],validate=True)
        if len(key)!=32 or len(sig)!=64:raise ValueError("invalid key/sig len")
        Ed25519PublicKey.from_public_bytes(key).verify(sig,canonical(payload))
    except (ValueError,TypeError,InvalidSignature) as exc:
        raise WitnessError("WITNESS_SIGNATURE_INVALID") from exc
    return payload

def check(db,entries,last_hash,environment=None):
    env=os.environ if environment is None else environment
    store_id=ensure_store_identity(db)
    witness_path=env.get("P12_REVIEW_ANCHOR_FILE")
    pinned_key=env.get("P12_REVIEW_ANCHOR_PUBLIC_KEY_HEX")
    if not witness_path and not pinned_key:
        return {
            "state":"NO_EXTERNAL_WITNESS",
            "store_id":store_id,
            "latest_seq":entries[-1]["seq"] if entries else 0,
            "latest_hash":last_hash,
            "externally_witnessed_events":0,
            "unwitnessed_events":len(entries),
            "manufacturing_authorization":False
        }
    if not witness_path or not pinned_key:
        raise WitnessError("WITNESS_CONFIGURATION_INCOMPLETE")
    path=Path(witness_path)
    try:
        if not path.is_absolute() or not path.is_file() or path.stat().st_size>MAX_FILE:
            raise WitnessError("WITNESS_FILE_UNAVAILABLE")
        raw=path.read_bytes()
        if len(raw)>MAX_FILE:raise WitnessError("WITNESS_FILE_TOO_LARGE")
        envelope=json.loads(raw.decode("utf-8","strict"))
    except (OSError,UnicodeError,ValueError) as exc:
        if isinstance(exc,WitnessError):raise
        raise WitnessError("WITNESS_FILE_UNREADABLE") from exc
    payload=verify_signed_checkpoint(envelope,pinned_key)
    if payload["store_id"]!=store_id:
        raise WitnessError("WITNESS_STORE_MISMATCH")
    last=payload["seq"]
    if last==0:
        if payload["event_hash"]!=GENESIS:
            raise WitnessError("WITNESS_INVALID_GENESIS")
    else:
        pinned=next((row for row in entries if row["seq"]==last),None)
        if pinned is None:
            raise WitnessError("WITNESS_HISTORY_TRUNCATED")
        if not hmac.compare_digest(pinned["event_hash"],payload["event_hash"]):
            raise WitnessError("WITNESS_HISTORY_DIVERGED")
    if entries and entries[0]["seq"]!=1:
        raise WitnessError("WITNESS_SEQUENCE_GAP")
    for idx,row in enumerate(entries,1):
        if row["seq"]!=idx:
            raise WitnessError("WITNESS_SEQUENCE_GAP")
    return {
        "state":"SIGNED_PREFIX_VERIFIED",
        "store_id":store_id,
        "witness_seq":last,
        "witness_hash":payload["event_hash"],
        "latest_seq":entries[-1]["seq"] if entries else 0,
        "latest_hash":last_hash,
        "externally_witnessed_events":last,
        "unwitnessed_events":len(entries)-last,
        "witness_snapshot_sha256":hashlib.sha256(raw).hexdigest(),
        "manufacturing_authorization":False,
        "limitation":"This is an offline Ed25519 checkpoint only; issuer authority and legal signatory status are not established."
    }
