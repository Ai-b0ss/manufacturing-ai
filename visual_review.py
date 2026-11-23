"""P11 visual source evidence and append-only, demo-only engineering review notes.

Human review is not an authoritative release, an electronic signature, or an
assertion that the drawing callout corresponds to the chosen CAD surface.
All source bytes are rechecked against P7 SHA-256 before each new finding.
"""
from __future__ import annotations

import base64
from contextlib import closing, nullcontext
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid

import dimension_trace as p9
import feature_verification as fv
import audit_witness
import engineering_corpus as ec
import engineering_inspection as p8

HERE=Path(__file__).resolve().parent
DEFAULT_STORE=Path(os.environ.get("FACTORY_P11_REVIEW_DB",str(HERE/"data"/"p11_review.sqlite3")))
DECISIONS=frozenset(("PLAUSIBLE_MAPPING","NOT_SAME_FEATURE","NEEDS_MORE_EVIDENCE"))
ID_RE=re.compile(r"^[0-9a-f]{24}$")
SCHEMA="""
CREATE TABLE IF NOT EXISTS review_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  timestamp_ms INTEGER NOT NULL,
  part_key TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  record_json TEXT NOT NULL,
  previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_event_part ON review_event(part_key,seq);
"""
GENESIS="0"*64

class ReviewError(ValueError):
    pass

def _open_store(store:Path):
    store=Path(store)
    store.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(store,timeout=8,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(SCHEMA)
    return db

def _encode(record):
    return json.dumps(record,sort_keys=True,separators=(",",":"),
                      ensure_ascii=False,allow_nan=False).encode("utf-8")

def _digest(previous_hash:str,record:dict)->str:
    return hashlib.sha256(previous_hash.encode("ascii")+b"\n"+_encode(record)).hexdigest()

def _validated_chain(db):
    prev=GENESIS
    entries=db.execute(
        "SELECT seq,event_id,timestamp_ms,part_key,reviewer,record_json,previous_hash,event_hash FROM review_event ORDER BY seq"
    ).fetchall()
    if len(entries)>50000:
        raise ReviewError("REVIEW_CHAIN_TOO_LARGE")
    for row in entries:
        try:
            obj=json.loads(row["record_json"])
            if not isinstance(obj,dict):
                raise ReviewError("REVIEW_CHAIN_BROKEN")
            digest=_digest(prev,obj)
            if (obj.get("event_id")!=row["event_id"] or
                    obj.get("part_key")!=row["part_key"] or
                    obj.get("reviewer")!=row["reviewer"] or
                    obj.get("created_at_ms")!=row["timestamp_ms"] or
                    not hmac.compare_digest(row["previous_hash"],prev) or
                    not hmac.compare_digest(digest,row["event_hash"])):
                raise ReviewError("REVIEW_CHAIN_BROKEN")
        except (ValueError,UnicodeError,TypeError,AttributeError,KeyError) as error:
            raise ReviewError("REVIEW_CHAIN_BROKEN") from error
        prev=row["event_hash"]
    # Without an external trusted signature this remains local consistency,
    # not protection against administrative truncation or wholesale rewrite.
    try:
        audit_witness.check(db,entries,prev)
    except audit_witness.WitnessError as error:
        raise ReviewError(str(error)) from error
    return entries,prev

def _source_pair(query,corpus_index,inspection_index):
    report=fv.candidates(query,corpus_index,inspection_index)
    if report.get("state")!="FEATURE_CANDIDATES_FOR_HUMAN_MAPPING":
        raise ReviewError("NO_EXACT_PUBLIC_TRIPLET")
    return report

def _checked_ids(report,annotation_id,feature_id):
    if not isinstance(annotation_id,str) or not ID_RE.fullmatch(annotation_id):
        raise ReviewError("INVALID_ANNOTATION_ID")
    if not isinstance(feature_id,str) or not ID_RE.fullmatch(feature_id):
        raise ReviewError("INVALID_FEATURE_ID")
    ann=next((x for x in report["drawing_annotations"] if x["annotation_id"]==annotation_id),None)
    face=next((x for x in report["features"] if x["feature_id"]==feature_id),None)
    if ann is None or face is None:raise ReviewError("EVIDENCE_ID_UNKNOWN")
    compatible=((ann["kind"]=="RADIUS" and face["kind"]=="CYLINDER_RADIUS")
                or (ann["kind"]=="ANGULAR" and face["kind"]=="PLANE_DIRECTION"))
    if not compatible:raise ReviewError("FEATURE_TYPE_MISMATCH")
    return ann,face

def render_preview(query,annotation_id,corpus_index=ec.DEFAULT_INDEX,
                   inspection_index=p8.DEFAULT_INDEX):
    if not isinstance(annotation_id,str) or not ID_RE.fullmatch(annotation_id):
        raise ReviewError("INVALID_ANNOTATION_ID")
    source=p9.annotations(query,corpus_index,inspection_index)
    if source.get("state")!="CANDIDATE_DIMENSIONS_FOR_REVIEW":
        raise ReviewError("NO_EXACT_PUBLIC_TRIPLET")
    ann=next((x for x in source["annotations"] if x["annotation_id"]==annotation_id),None)
    if not ann:raise ReviewError("EVIDENCE_ID_UNKNOWN")
    data,suffix=ec.original_file(source["drawing"]["id"],corpus_index)
    digest=hashlib.sha256(data).hexdigest()
    if suffix.lower()!=".pdf" or digest!=source["drawing"]["sha256"]:
        raise ReviewError("SOURCE_SHA_MISMATCH")
    python=p8.PDF_PYTHON
    if not python.is_file():raise ReviewError("PDF_RENDERER_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="p11-public-preview-") as folder:
        file=Path(folder)/"document.pdf"
        file.write_bytes(data)
        args=[str(python),str(HERE/"p11_preview_worker.py"),str(file),
              str(ann["page"]),json.dumps(ann["bbox"],separators=(",",":"))]
        try:
            execution=subprocess.run(
                args,capture_output=True,timeout=12,check=False,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)
            )
        except (OSError,subprocess.TimeoutExpired) as exc:
            raise ReviewError("PDF_RENDERING_UNAVAILABLE") from exc
        try:
            if execution.returncode:
                raise ReviewError("PDF_RENDERING_FAILED")
            lines=execution.stdout.decode("utf-8","strict").splitlines()
            framed=[line.partition("P11_PREVIEW_JSON:")[2] for line in lines
                    if line.startswith("P11_PREVIEW_JSON:")]
            if len(framed)!=1:raise ValueError("Invalid renderer framing")
            rendered=json.loads(framed[0])
            if rendered.get("source_sha256")!=digest or rendered.get("page")!=ann["page"]:
                raise ValueError("Renderer source digest changed")
            png=base64.b64decode(rendered["image"],validate=True)
            if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png)>2_000_000:
                raise ValueError("Invalid PNG")
            return png
        except (ValueError,UnicodeError,KeyError,TypeError) as exc:
            raise ReviewError("RENDERER_UNTRUSTED_RESULT") from exc

def _validate_note(note):
    if not isinstance(note,str) or not 12<=len(note.strip())<=500:
        raise ReviewError("NOTE_LENGTH_INVALID")
    if any(ord(char)<32 and char not in ("\n","\t") for char in note):
        raise ReviewError("NOTE_CONTROL_CHARACTER")
    return note.strip()

def _validate_event_id(event_id):
    try: parsed=uuid.UUID(event_id)
    except (ValueError,TypeError,AttributeError) as exc:
        raise ReviewError("EVENT_ID_INVALID") from exc
    if parsed.version!=4 or str(parsed)!=event_id:
        raise ReviewError("EVENT_ID_INVALID")
    return event_id

def _current_sources(part_key,drawing_sha,cad_sha,corpus_index):
    # Read current indexed hashes and actual files. Reindexing to new revisions
    # invalidates old records, and mutations without reindexing also invalidate.
    try:
        name=part_key.split(":",1)[1]
        result=ec.answer(name,corpus_index)
        if result.get("state")!="BOM_DRAWING_STEP_LINKED" or result.get("part_key")!=part_key:
            return False
        drawing=next(x for x in result["sources"] if x["role"]=="DRAWING")
        model=next(x for x in result["sources"] if x["role"]=="MODEL")
        if drawing["sha256"]!=drawing_sha or model["sha256"]!=cad_sha:
            return False
        ec.original_file(drawing["id"],corpus_index)
        ec.original_file(model["id"],corpus_index)
        return True
    except (ec.CorpusError,IndexError,KeyError,ValueError):
        return False

def record_finding(query,annotation_id,feature_id,decision,note,reviewer,event_id,
                   corpus_index=ec.DEFAULT_INDEX,inspection_index=p8.DEFAULT_INDEX,
                   store=None,axis="x",access_gate=None):
    event_id=_validate_event_id(event_id)
    note=_validate_note(note)
    if decision not in DECISIONS:
        raise ReviewError("INVALID_DECISION")
    if reviewer!="engineer":
        raise ReviewError("REVIEWER_IS_DEMO_ENGINEER_ONLY")
    report=_source_pair(query,corpus_index,inspection_index)
    ann,feature=_checked_ids(report,annotation_id,feature_id)
    if axis not in ("x","y","z"):
        raise ReviewError("AXIS_INVALID")
    record={
        "version":"p11-review-note/v1",
        "event_id":event_id,
        "part_key":report["part_key"],
        "part":report["part"],
        "drawing_source_id":report["drawing"]["id"],
        "drawing_sha256":report["drawing"]["sha256"],
        "cad_source_id":report["model"]["id"],
        "cad_sha256":report["model"]["sha256"],
        "annotation_id":ann["annotation_id"],
        "annotation_text":ann["label"],
        "annotation_page":ann["page"],
        "annotation_bbox":ann["bbox"],
        "feature_id":feature["feature_id"],
        "feature_kind":feature["kind"],
        "feature_measurement":(feature["value"] if feature["kind"]=="CYLINDER_RADIUS"
                               else feature["angles_to_axes"][axis]),
        "feature_face_indices":feature["sample_face_indices"],
        "comparison_axis":axis if feature["kind"]=="PLANE_DIRECTION" else "NOT_APPLICABLE",
        "decision":decision,
        "note":note,
        "reviewer":"engineer",
        "status":"HUMAN_DEMO_REVIEW_NOTE_NOT_SIGNED",
        "manufacturing_authorization":False
    }
    with access_gate() if access_gate is not None else nullcontext():
      with closing(_open_store(store or DEFAULT_STORE)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _entries,previous=_validated_chain(db)
            existing=db.execute("SELECT record_json FROM review_event WHERE event_id=?",
                                (event_id,)).fetchone()
            if existing:
                before=json.loads(existing["record_json"])
                before.pop("created_at_ms",None)
                if before!=record:raise ReviewError("IDEMPOTENCY_CONFLICT")
                db.execute("COMMIT")
                return {"state":"REVIEW_NOTE_RECORDED","event_id":event_id,
                        "duplicate":True,"manufacturing_authorization":False}
            record["created_at_ms"]=int(time.time()*1000)
            digest=_digest(previous,record)
            db.execute("INSERT INTO review_event(event_id,timestamp_ms,part_key,reviewer,record_json,previous_hash,event_hash) VALUES (?,?,?,?,?,?,?)",
                (event_id,record["created_at_ms"],record["part_key"],reviewer,
                 _encode(record).decode("utf-8"),previous,digest))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"state":"REVIEW_NOTE_RECORDED","event_id":event_id,
            "duplicate":False,"audit_digest":digest,
            "manufacturing_authorization":False,
            "note":"This is a demo review note, not an engineering change approval or production release."}

def audit_status(store=None):
    with closing(_open_store(store or DEFAULT_STORE)) as db:
        entries,last_hash=_validated_chain(db)
        status=audit_witness.check(db,entries,last_hash)
        proposal=audit_witness.proposal(db,entries,last_hash)
    return {
        "state":status["state"],
        "witness":status,
        "unsigned_checkpoint_proposal":proposal,
        "external_signature_required":True,
        "human_identity_verified":False,
        "manufacturing_authorization":False,
        "note":"A checkpoint becomes independent evidence only when signed and kept outside the demo app by a trusted party. The app neither signs nor silently accepts a local unsigned checkpoint."
    }

def list_findings(query,corpus_index=ec.DEFAULT_INDEX,store=None):
    if not isinstance(query,str) or not 2<=len(query)<=150:
        raise ReviewError("QUERY_INVALID")
    item=ec.answer(query,corpus_index)
    if item.get("state")!="BOM_DRAWING_STEP_LINKED":
        return {"state":"NO_EXACT_TRIPLET","items":[],"manufacturing_authorization":False}
    with closing(_open_store(store or DEFAULT_STORE)) as db:
        entries,head=_validated_chain(db)
        witness_status=audit_witness.check(db,entries,head)
        all_for_part=[json.loads(x["record_json"]) for x in entries
                      if json.loads(x["record_json"])["part_key"]==item["part_key"]]
    list_items=[]
    known={}
    for entry in all_for_part[-100:]:
        key=(entry["part_key"],entry["drawing_sha256"],entry["cad_sha256"])
        if key not in known:
            known[key]=_current_sources(*key,corpus_index)
        current=known[key]
        list_items.append({**entry,
                           "evidence_state":"SOURCE_HASHES_VALID" if current else "SOURCE_CHANGED_OR_MISSING",
                           "feature_mapping_verified":False,
                           "manufacturing_authorization":False})
    return {
        "state":"DEMO_REVIEW_AUDIT",
        "part_key":item["part_key"],
        "items":list_items,
        "count":len(all_for_part),
        "chain_state":("SIGNED_PREFIX_VERIFIED" if witness_status["state"]=="SIGNED_PREFIX_VERIFIED"
                       else "CHAIN_LINKS_VALID_NOT_EXTERNALLY_ANCHORED"),
        "external_witness_state":witness_status["state"],
        "externally_witnessed_events":witness_status["externally_witnessed_events"],
        "latest_chain_hash":head,
        "manufacturing_authorization":False,
        "notice":"The local hash chain detects many accidental edits but cannot prevent administrators from rewriting or truncating it; it is NOT an authenticated audit ledger."
    }
