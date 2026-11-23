"""Read-only PLM/1C evidence contract, not a production approval engine.

No real PLM or 1C connector is configured on the demo workstation. An
administrator may provision an offline Ed25519-signed snapshot and pinned
verification key via environment variables. This proves integrity/authorship
under that key, NOT issuer authority, Russian qualified e-signature, current
revision or production release. No HTTP client or document writeback.
"""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

import dimension_trace as p9
import engineering_corpus as ec

SCHEMA="factory-plm-revision-evidence/v1"
MAX_BYTES=131072
KEY_ID=re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$")
HASH=re.compile(r"^[0-9a-f]{64}$")
PART=re.compile(r"^[A-Za-z0-9_:\-]{2,160}$")
class AuthorityError(ValueError): pass

def canonical(payload:dict)->bytes:
    return json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")

def verify_envelope(envelope, trusted_keys, *, at=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    if not isinstance(envelope,dict) or set(envelope)!={"payload","signature","key_id"}:
        raise AuthorityError("INVALID_ENVELOPE")
    payload=envelope["payload"]
    if not isinstance(payload,dict) or set(payload)!={
        "schema","source_system","issuer","issued_at","expires_at",
        "part_key","revision","drawing_sha256","cad_sha256","release_event_id","status"}:
        raise AuthorityError("INVALID_PAYLOAD_FIELDS")
    if payload["schema"]!=SCHEMA or payload["source_system"] not in ("PLM","1C_ERP"):
        raise AuthorityError("INVALID_SOURCE_SYSTEM")
    for field in ("issuer","release_event_id","revision"):
        if not isinstance(payload[field],str) or not 1<=len(payload[field])<=128:
            raise AuthorityError("INVALID_METADATA")
    if not isinstance(payload["part_key"],str) or not PART.fullmatch(payload["part_key"]):
        raise AuthorityError("INVALID_PART")
    for field in ("drawing_sha256","cad_sha256"):
        if not isinstance(payload[field],str) or not HASH.fullmatch(payload[field]):
            raise AuthorityError("INVALID_DOCUMENT_HASH")
    if payload["status"] not in ("RELEASED","OBSOLETE","DRAFT","REVOKED"):
        raise AuthorityError("INVALID_STATE")
    key_id=envelope["key_id"]
    if not isinstance(key_id,str) or not KEY_ID.fullmatch(key_id):
        raise AuthorityError("INVALID_KEY_ID")
    if key_id not in trusted_keys:raise AuthorityError("UNKNOWN_TRUST_ANCHOR")
    try:
        signature=base64.b64decode(envelope["signature"],validate=True)
        key_bytes=bytes.fromhex(trusted_keys[key_id])
        if len(signature)!=64 or len(key_bytes)!=32:
            raise AuthorityError("INVALID_CRYPTO_LENGTH")
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature,canonical(payload))
    except (ValueError,TypeError,InvalidSignature) as exc:
        raise AuthorityError("SIGNATURE_INVALID") from exc
    def date(value):
        if not isinstance(value,str) or not value.endswith("Z"):
            raise AuthorityError("INVALID_TIMESTAMP")
        try:
            dt=datetime.fromisoformat(value.replace("Z","+00:00"))
        except ValueError as exc:raise AuthorityError("INVALID_TIMESTAMP") from exc
        if dt.tzinfo is None:raise AuthorityError("INVALID_TIMESTAMP")
        return dt
    issued=date(payload["issued_at"])
    expiry=date(payload["expires_at"])
    now=at or datetime.now(timezone.utc)
    if not issued<=now<=expiry or expiry<=issued or (expiry-issued).total_seconds()>86400*14:
        raise AuthorityError("EXPIRED_OR_FUTURE_EVIDENCE")
    return payload

def status(query:str,corpus_index=ec.DEFAULT_INDEX,*,environment=None,at=None):
    env=environment if environment is not None else os.environ
    part=p9.annotations(query,corpus_index)
    if part["state"]!="CANDIDATE_DIMENSIONS_FOR_REVIEW":
        return {"state":"NO_PROVEN_PART_TRIPLET","manufacturing_authorization":False}
    p=part["part_key"]
    fallback={"state":"NO_AUTHORITATIVE_CONNECTOR","part_key":p,
              "revision_status":"NOT_VERIFIED","issuer_authority":"NOT_VERIFIED",
              "manufacturing_authorization":False,
              "note":"No actual enterprise PLM/1C connector or trusted source of released configurations has been connected."}
    snapshot=env.get("P10_PLM_SNAPSHOT_FILE")
    trust=env.get("P10_PLM_PUBLIC_KEYS_JSON")
    if not snapshot or not trust:return fallback
    try:
        keys=json.loads(trust)
        if not isinstance(keys,dict) or len(keys)>16:raise AuthorityError("TRUST_REGISTRY_INVALID")
        path=Path(snapshot)
        if not path.is_file() or path.stat().st_size>MAX_BYTES:raise AuthorityError("SNAPSHOT_MISSING_OR_TOO_LARGE")
        with path.open("rb") as stream:
            raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise AuthorityError("SNAPSHOT_SIZE_LIMIT")
        envelope=json.loads(raw.decode("utf-8","strict"))
        payload=verify_envelope(envelope,keys,at=at)
        if payload["part_key"]!=part["part_key"]:
            raise AuthorityError("PART_MISMATCH")
        if payload["drawing_sha256"]!=part["drawing"]["sha256"] or payload["cad_sha256"]!=part["model"]["sha256"]:
            raise AuthorityError("DOCUMENT_HASH_MISMATCH")
        return {"state":"SIGNED_SNAPSHOT_INTEGRITY_VERIFIED","part_key":p,
                "source_system":payload["source_system"],"issuer":payload["issuer"],
                "claimed_revision":payload["revision"],"claimed_status":payload["status"],
                "release_event_id":payload["release_event_id"],
                "snapshot_sha256":hashlib.sha256(raw).hexdigest(),
                "signature_verification":"VALID_FOR_PINNED_KEY",
                "issuer_authority":"NOT_VERIFIED",
                "revision_status":"NOT_VERIFIED_IN_DRAWING_AND_AUTHORITATIVE_REPOSITORY",
                "legal_e_signature":"NOT_EVALUATED",
                "manufacturing_authorization":False,
                "note":"Ed25519 signature only proves a snapshot is unmodified under an admin-provisioned key; there is no real enterprise trust/certification or current PLM release validation."}
    except (ValueError,TypeError,UnicodeError,OSError,AuthorityError) as exc:
        return {"state":"UNTRUSTED_OR_UNUSABLE_SNAPSHOT","part_key":p,
                "reason":str(exc)[:120] if isinstance(exc,AuthorityError) else "SNAPSHOT_REJECTED",
                "revision_status":"NOT_VERIFIED",
                "manufacturing_authorization":False}
