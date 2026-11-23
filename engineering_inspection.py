"""P8: evidence-backed drawing/title-block and STEP topology inspection.

This sits beside P7's immutable, SHA-256-checked public engineering corpus.
No retrieved data grants manufacturing approval, PLM release or ESKD compliance.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import engineering_corpus as ec

HERE=Path(__file__).resolve().parent
DEFAULT_INDEX=HERE/"data"/"engineering_inspection.sqlite3"
PDF_PYTHON=ec.DEFAULT_PYTHON
CAD_PYTHON=HERE/".p8venv"/"Scripts"/"python.exe"
HISTORICAL_PATH=(HERE.parent/"ManufacturingAI-Design-Preview"/".preview"
                 /"external_historical_change_reference"/"STO_CKTI_318_03_2009_with_change.pdf")
PARSER_VERSION="P8-20261117-3"
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE context (
  singleton INTEGER PRIMARY KEY CHECK (singleton=1),
  corpus_manifest_sha256 TEXT NOT NULL,
  importer_version TEXT NOT NULL,
  built_at INTEGER NOT NULL
);
CREATE TABLE inspection (
  source_id TEXT PRIMARY KEY,
  original_sha256 TEXT NOT NULL,
  source_group TEXT NOT NULL,
  kind TEXT NOT NULL,
  report_json TEXT NOT NULL
);
CREATE TABLE historic_reference (
  singleton INTEGER PRIMARY KEY CHECK (singleton=1),
  original_sha256 TEXT NOT NULL,
  report_json TEXT NOT NULL
);
"""

class InspectionError(ValueError):
    pass

def _strict_json(stdout: bytes)->dict:
    try:
        lines=stdout.decode("utf-8",errors="strict").splitlines()
        framed=[line.split("P8_RESULT_JSON:",1)[1] for line in lines
                if line.startswith("P8_RESULT_JSON:")]
        if len(framed)!=1: raise ValueError("Worker result frame missing or ambiguous")
        result=json.loads(framed[0])
        if not isinstance(result,dict): raise ValueError("Worker response not object")
        return result
    except (UnicodeError,ValueError) as error:
        raise InspectionError("WORKER_INVALID_JSON") from error

def _run_worker(data: bytes, suffix: str, worker: Path, python: Path,
                extra: list[str]|None=None, timeout: int=22)->dict:
    if not python.is_file():
        return {"state":"WORKER_UNAVAILABLE","reason":worker.name,
                "geometry_verified":False}
    # Immutable local copy closes the gap between SHA verification and parsing.
    # Worker never receives an external path, URL, shell command or token.
    if len(data)>10_000_000: raise InspectionError("SOURCE_TOO_LARGE")
    with tempfile.TemporaryDirectory(prefix="factory-p8-") as directory:
        path=Path(directory)/("source"+suffix)
        path.write_bytes(data)
        flags=getattr(subprocess,"CREATE_NO_WINDOW",0)
        try:
            completed=subprocess.run([str(python),str(worker),str(path),*(extra or [])],
                capture_output=True,timeout=timeout,check=False,
                creationflags=flags)
        except (OSError,subprocess.TimeoutExpired) as error:
            raise InspectionError("WORKER_FAILED_OR_TIMED_OUT") from error
        result=_strict_json(completed.stdout)
        if completed.returncode!=0 or "error" in result:
            raise InspectionError("WORKER_REJECTED_SOURCE")
        return result

def _get_source(db: sqlite3.Connection, source_id: str):
    if not isinstance(source_id,str) or not re.fullmatch(r"[0-9a-f]{24}",source_id):
        raise InspectionError("SOURCE_NOT_FOUND")
    row=db.execute(
        "SELECT id,source_group,name,kind,original_sha256,original_url FROM source WHERE id=?",
        (source_id,)).fetchone()
    if row is None: raise InspectionError("SOURCE_NOT_FOUND")
    return row

def _inspect_original(row, corpus_index: Path, *, ocr=False)->dict:
    data,suffix=ec.original_file(row["id"],corpus_index)
    kind=row["kind"]
    if kind=="PDF":
        return _run_worker(data,suffix,HERE/"p8_pdf_worker.py",PDF_PYTHON,
                           [row["source_group"],*(["--ocr"] if ocr else [])],
                           timeout=24 if ocr else 12)
    if kind=="STEP":
        return _run_worker(data,suffix,HERE/"p8_cad_worker.py",CAD_PYTHON,
                           timeout=22)
    return {"state":"NOT_APPLICABLE","geometry_verified":False,
            "reason":"This file format has no P8 drawing or CAD inspector"}

def build_index(corpus_index: Path=ec.DEFAULT_INDEX,
                index_path: Path=DEFAULT_INDEX)->dict:
    with closing(ec._open_readonly(corpus_index)) as corpus:
        src_rows=corpus.execute("SELECT id,source_group,name,kind,original_sha256,original_url FROM source ORDER BY source_group,name").fetchall()
        manifest=corpus.execute("SELECT manifest_sha256 FROM dataset WHERE singleton=1").fetchone()
        if not manifest: raise InspectionError("CORPUS_INDEX_UNVERIFIED")
        manifest_sha=manifest[0]
    index_path=Path(index_path).resolve()
    index_path.parent.mkdir(parents=True,exist_ok=True)
    tmp=index_path.with_name(index_path.name+"."+uuid.uuid4().hex+".tmp")
    counts={"sources":0,"pdf":0,"step":0,"recognized_title_blocks":0,
            "valid_cad_breps":0,"unavailable_inspectors":0,
            "historical_reference":False}
    try:
        db=sqlite3.connect(tmp)
        db.executescript(SCHEMA)
        db.execute("INSERT INTO context VALUES(1,?,?,?)",
                   (manifest_sha,PARSER_VERSION,int(time.time())))
        with closing(ec._open_readonly(corpus_index)) as corpus:
            for row in src_rows:
                if row["kind"] in ("PDF","STEP"):
                    report=_inspect_original(row,corpus_index)
                    if row["kind"]=="PDF":
                        counts["pdf"]+=1
                        if report.get("recognition")=="CANDIDATE_FIELDS_EXTRACTED":
                            counts["recognized_title_blocks"]+=1
                    else:
                        counts["step"]+=1
                        if report.get("state")=="BREP_VALID":
                            counts["valid_cad_breps"]+=1
                    if report.get("state")=="WORKER_UNAVAILABLE":
                        counts["unavailable_inspectors"]+=1
                else:
                    report={"state":"NOT_APPLICABLE","geometry_verified":False}
                db.execute("INSERT INTO inspection VALUES (?,?,?,?,?)",
                           (row["id"],row["original_sha256"],row["source_group"],
                            row["kind"],json.dumps(report,ensure_ascii=False)))
                counts["sources"]+=1
        if HISTORICAL_PATH.is_file():
            data=HISTORICAL_PATH.read_bytes()
            if len(data)>10_000_000:
                raise InspectionError("HISTORICAL_REFERENCE_TOO_LARGE")
            historic=_run_worker(data,".pdf",HERE/"p8_notice_worker.py",PDF_PYTHON,timeout=12)
            db.execute("INSERT INTO historic_reference VALUES (1,?,?)",
                       (hashlib.sha256(data).hexdigest(),json.dumps(historic,ensure_ascii=False)))
            counts["historical_reference"]=True
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise InspectionError("INDEX_INTEGRITY_CHECK_FAILED")
        db.close()
        os.replace(tmp,index_path)
        return counts
    except BaseException:
        try: db.close()
        except (UnboundLocalError,sqlite3.Error):pass
        tmp.unlink(missing_ok=True)
        raise

def _read_cache(index_path:Path, corpus_index:Path):
    path=Path(index_path)
    if not path.is_file():raise InspectionError("INSPECTION_INDEX_MISSING")
    db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=2)
    db.row_factory=sqlite3.Row
    try:
        with closing(ec._open_readonly(corpus_index)) as corpus:
            manifest=corpus.execute(
                "SELECT manifest_sha256 FROM dataset WHERE singleton=1").fetchone()
        current=db.execute(
            "SELECT corpus_manifest_sha256,importer_version FROM context WHERE singleton=1").fetchone()
        if not current or not manifest or tuple(current)!=(manifest[0],PARSER_VERSION):
            raise InspectionError("INSPECTION_INDEX_STALE")
        return db
    except BaseException:
        db.close()
        raise

def summary(corpus_index:Path=ec.DEFAULT_INDEX,index_path:Path=DEFAULT_INDEX)->dict:
    with closing(_read_cache(index_path,corpus_index)) as db:
        rows=db.execute("SELECT kind,report_json FROM inspection").fetchall()
        result={"sources":len(rows),"pdf":0,"step":0,"recognized_title_blocks":0,
                "valid_cad_breps":0,"unverified_revisions":True,
                "historical_reference":bool(db.execute("SELECT 1 FROM historic_reference").fetchone())}
        for row in rows:
            report=json.loads(row["report_json"])
            if row["kind"]=="PDF":
                result["pdf"]+=1
                if report.get("recognition")=="CANDIDATE_FIELDS_EXTRACTED":
                    result["recognized_title_blocks"]+=1
            elif row["kind"]=="STEP":
                result["step"]+=1
                if report.get("state")=="BREP_VALID":
                    result["valid_cad_breps"]+=1
        return result

def catalog(corpus_index:Path=ec.DEFAULT_INDEX,index_path:Path=DEFAULT_INDEX)->dict:
    with closing(_read_cache(index_path,corpus_index)) as cache:
        rows=cache.execute(
            "SELECT source_id,source_group,kind,report_json FROM inspection "
            "WHERE kind IN ('PDF','STEP') ORDER BY source_group,source_id").fetchall()
    with closing(ec._open_readonly(corpus_index)) as corpus:
        items=[]
        for row in rows:
            original=_get_source(corpus,row["source_id"])
            report=json.loads(row["report_json"])
            items.append({"id":original["id"],"group":original["source_group"],
                          "name":original["name"],"kind":original["kind"],
                          "titleblock_recognized":bool(report.get("designation") or report.get("revision")),
                          "cad_loaded":bool(report.get("geometry_loaded"))})
        return {"items":items,"total":len(items),"approval_record_available":False}

def _report(source_id:str,corpus_index:Path,index_path:Path,*,ocr:bool=False)->dict:
    with closing(ec._open_readonly(corpus_index)) as corpus:
        row=_get_source(corpus,source_id)
    # Always re-verify source data before returning cached evidence. A renamed,
    # tampered or removed file cannot retain a trustworthy inspection status.
    ec.original_file(source_id,corpus_index)
    if ocr:
        if row["kind"]!="PDF":raise InspectionError("OCR_PDF_ONLY")
        inspection=_inspect_original(row,corpus_index,ocr=True)
    else:
        with closing(_read_cache(index_path,corpus_index)) as cache:
            entry=cache.execute("SELECT original_sha256,report_json FROM inspection WHERE source_id=?",
                                (source_id,)).fetchone()
        if not entry or entry["original_sha256"]!=row["original_sha256"]:
            raise InspectionError("SOURCE_INSPECTION_STALE")
        inspection=json.loads(entry["report_json"])
    file_match="NOT_CHECKABLE"
    if row["kind"]=="PDF" and inspection.get("designation") and inspection.get("revision"):
        match=re.match(r"FD_(\d{7})-([A-Z])",row["name"],re.I)
        if match:
            file_match=("MATCH" if (match.group(1)==inspection["designation"]
                        and match.group(2)==inspection["revision"]) else "MISMATCH")
        else:
            named=re.match(r"(\d{3}M\d{2}C\d{2}\.\dP\d+[A-Z])",row["name"],re.I)
            if named:
                file_match="MATCH" if named.group(1)==inspection["designation"] else "MISMATCH"
    return {
        "source_id":row["id"],"source_group":row["source_group"],"source_name":row["name"],
        "source_kind":row["kind"],"source_sha256":row["original_sha256"],
        "source_url":row["original_url"],
        "inspection":inspection,
        "filename_titleblock_comparison":file_match,
        "revision_approved":False,
        "geometry_matches_drawing":"NOT_ESTABLISHED",
        "manufacturing_authorization":False
    }

def inspect_source(source_id:str, corpus_index:Path=ec.DEFAULT_INDEX,
                   index_path:Path=DEFAULT_INDEX,ocr:bool=False)->dict:
    return _report(source_id,corpus_index,index_path,ocr=ocr)

def _grade_compare(bom:str,grade:str|None)->str:
    if not bom or not grade:return "GRADE_NOT_COMPARABLE"
    def key(v):
        tokens=re.findall(r"(?:6061[- ]?T6|AISI\s*304|SUS\s*304)",v,re.I)
        return re.sub(r"[\s-]","",tokens[0]).upper() if tokens else None
    bomcode=key(bom);gradecode=key(grade)
    if not bomcode or not gradecode:return "GRADE_NOT_COMPARABLE"
    if bomcode==gradecode:return "VISIBLE_GRADE_TOKEN_MATCH"
    # AISI 304 and SUS304 may be related, but this parser is not a certified
    # material-grade equivalence engine; do not guess acceptance.
    return "GRADE_CODES_DIFFER_REVIEW_REQUIRED"

def report_for_part(query:str,corpus_index:Path=ec.DEFAULT_INDEX,
                    index_path:Path=DEFAULT_INDEX)->dict:
    base=ec.answer(query,corpus_index)
    if base.get("state")!="BOM_DRAWING_STEP_LINKED":
        return {"state":base.get("state","UNVERIFIED"),"source_match":base,
                "manufacturing_authorization":False}
    reports={}
    for src in base["sources"]:
        if src["role"] in {"DRAWING","MODEL"}:
            reports[src["role"]]=inspect_source(src["id"],corpus_index,index_path)
    drawing=reports.get("DRAWING",{}).get("inspection",{})
    model=reports.get("MODEL",{}).get("inspection",{})
    grade=_grade_compare(base.get("material",""),drawing.get("material_grade"))
    risk=["NO_AUTHORITATIVE_REVISION_RECORD",
          "NO_RELEASED_PLM_CONFIGURATION",
          "DRAWING_MODEL_GEOMETRY_NOT_COMPARED"]
    if grade=="GRADE_CODES_DIFFER_REVIEW_REQUIRED":
        risk.append("MATERIAL_GRADE_REQUIRES_ENGINEER_REVIEW")
    if model.get("state")!="BREP_VALID":
        risk.append("STEP_BREP_NOT_VALIDATED")
    return {
        "state":"ENGINEERING_VERIFICATION_INCOMPLETE",
        "part":base["part"],"source_match":base,
        "drawing":reports.get("DRAWING"),
        "step_model":reports.get("MODEL"),
        "material_grade_comparison":grade,
        "revision_status":"NOT_VERIFIED",
        "manufacturing_authorization":False,
        "risks":risk,
        "message":"Title-block and STEP evidence collected; released revision, CAD/drawing equivalence and production applicability remain unverified."
    }

def compare_sources(left_id:str,right_id:str,corpus_index:Path=ec.DEFAULT_INDEX,
                    index_path:Path=DEFAULT_INDEX)->dict:
    left=inspect_source(left_id,corpus_index,index_path)
    right=inspect_source(right_id,corpus_index,index_path)
    if left["source_kind"]!="PDF" or right["source_kind"]!="PDF":
        raise InspectionError("DRAWING_PDF_ONLY")
    if left_id==right_id:
        raise InspectionError("DISTINCT_SOURCES_REQUIRED")
    a=left["inspection"];b=right["inspection"]
    same_ref=bool(a.get("designation") and b.get("designation") and a["designation"]==b["designation"])
    same_version=bool(a.get("revision") and b.get("revision") and a["revision"]==b["revision"])
    return {
        "state":"DOCUMENT_COMPARISON_ONLY",
        "left":left,"right":right,
        "designation_comparison":"SAME_IDENTIFIER" if same_ref else
                                 "DIFFERENT_OR_UNAVAILABLE",
        "version_label_comparison":"SAME_LABEL" if same_version else
                                   "DIFFERENT_OR_UNAVAILABLE",
        "not_a_formal_change_notice":True,
        "manufacturing_authorization":False,
        "message":"Distinct source documents compared. Version labels cannot establish an approved engineering change or chronological applicability."
    }

def compare_bcn3d_top_frames(corpus_index:Path=ec.DEFAULT_INDEX,
                            index_path:Path=DEFAULT_INDEX)->dict:
    with closing(ec._open_readonly(corpus_index)) as corpus:
        pair=[]
        for group,name in [("BCN3D_Sigmax","Top_frame_Sigmax.pdf"),
                           ("BCN3D_R19","Top_frame_R19.pdf")]:
            row=corpus.execute("SELECT id FROM source WHERE source_group=? AND name=?",
                               (group,name)).fetchone()
            if not row:raise InspectionError("RELEASE_PAIR_NOT_FOUND")
            pair.append(row["id"])
    return compare_sources(*pair,corpus_index,index_path)

def historical_notice(corpus_index:Path=ec.DEFAULT_INDEX,
                      index_path:Path=DEFAULT_INDEX)->dict:
    with closing(_read_cache(index_path,corpus_index)) as db:
        row=db.execute("SELECT * FROM historic_reference WHERE singleton=1").fetchone()
        if not row: raise InspectionError("HISTORICAL_REFERENCE_UNAVAILABLE")
        saved=json.loads(row["report_json"])
    if not HISTORICAL_PATH.is_file() or HISTORICAL_PATH.stat().st_size>10_000_000:
        raise InspectionError("HISTORICAL_REFERENCE_UNAVAILABLE")
    with HISTORICAL_PATH.open("rb") as source:
        if hashlib.file_digest(source,"sha256").hexdigest()!=row["original_sha256"]:
            raise InspectionError("HISTORICAL_REFERENCE_CHANGED")
    ref=ec.notice("БВАИ.107-2012",corpus_index)
    if ref.get("state")!="HISTORICAL_REFERENCE_ONLY":
        raise InspectionError("HISTORICAL_REFERENCE_NOT_IN_PROVENANCE")
    return {"state":"HISTORICAL_UNCERTAIN_SOURCE",
            "pdf_inspection":saved,
            "publication_url":ref["source_url"],
            "effective_date_from_external_catalog":ref["effective_date_reference"],
            "change_claim_in_external_catalog":ref["change_description"],
            "date_verified_from_original":False,
            "change_geometry_verified_from_original":False,
            "manufacturing_authorization":False}

def main():
    parser=argparse.ArgumentParser(description="P8 drawing + CAD inspection")
    parser.add_argument("action",choices=["import","summary","part","source","compare-top","notice"])
    parser.add_argument("--query",default="")
    parser.add_argument("--source-id",default="")
    args=parser.parse_args()
    if args.action=="import":res=build_index()
    elif args.action=="summary":res=summary()
    elif args.action=="part":res=report_for_part(args.query)
    elif args.action=="source":res=inspect_source(args.source_id)
    elif args.action=="notice":res=historical_notice()
    else:res=compare_bcn3d_top_frames()
    print(json.dumps(res,ensure_ascii=True,indent=2))

if __name__=="__main__":
    main()
