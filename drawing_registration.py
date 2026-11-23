"""P13: true drawing vector witnesses + bounded numeric STEP candidates.

This is *not* an automatic PDF/STEP spatial registration. Numeric similarity,
valid vector leaders, and contour contact are independent pieces of evidence.
The full association remains a human engineering hypothesis until geometry
registration, projection and revision have been independently established.
"""
from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import threading

import engineering_corpus as ec
import engineering_inspection as p8
import feature_verification as fv

HERE=Path(__file__).resolve().parent
WINDOW=Decimal("0.02")
VECTOR_WAIT=13
PNG_LIMIT=2_000_000
RENDER_LIMIT=threading.BoundedSemaphore(2)

class RegistrationError(ValueError):pass

def _vector_from_source(source,annotation,corpus_index):
    data,suffix=ec.original_file(source["id"],corpus_index)
    if suffix!=".pdf" or hashlib.sha256(data).hexdigest()!=source["sha256"]:
        raise RegistrationError("DRAWING_SOURCE_CHANGED")
    if len(data)>9_000_000:raise RegistrationError("DRAWING_TOO_LARGE")
    with tempfile.TemporaryDirectory(prefix="factory-p13-vectors-") as tmp:
        pdf=Path(tmp)/"source.pdf"
        pdf.write_bytes(data)
        args=[str(p8.PDF_PYTHON),str(HERE/"p13_pdf_vector_worker.py"),str(pdf),
              str(annotation["page"]),json.dumps(annotation["bbox"],separators=(",",":")),
              annotation["kind"]]
        try:
            r=subprocess.run(args,capture_output=True,timeout=VECTOR_WAIT,
                             check=False,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except (subprocess.TimeoutExpired,OSError) as exc:
            raise RegistrationError("DRAWING_VECTOR_WORKER_UNAVAILABLE") from exc
    try:
        lines=r.stdout.decode("utf-8","strict").splitlines()
        frames=[line[len("P13_VECTOR_JSON:"):] for line in lines
                if line.startswith("P13_VECTOR_JSON:")]
        if len(frames)!=1 or r.returncode!=0:raise ValueError("untrusted worker result")
        parsed=json.loads(frames[0])
        if (parsed.get("source_sha256")!=source["sha256"] or
            parsed.get("page")!=annotation["page"] or
            parsed.get("annotation_bbox")!=annotation["bbox"] or
            parsed.get("drawing_to_cad_registration")!="NOT_ESTABLISHED" or
            parsed.get("manufacturing_authorization") is not False or
            not isinstance(parsed.get("vector_witnesses"),list) or
            len(parsed["vector_witnesses"])>4):
            raise ValueError("untrusted PDF source or worker framing")
        png=base64.b64decode(parsed.pop("preview_png_base64"),validate=True)
        width,height=struct.unpack(">II",png[16:24])
        if (not 60<len(png)<=PNG_LIMIT or not png.startswith(b"\x89PNG\r\n\x1a\n") or
            width!=parsed["preview_width"] or height!=parsed["preview_height"] or
            width*height>1_150_000):
            raise ValueError("untrusted raster")
        return parsed,png
    except (KeyError,ValueError,TypeError,UnicodeError,struct.error) as exc:
        raise RegistrationError("UNTRUSTED_VECTOR_EVIDENCE") from exc

def _resolve(query,annotation_id,axis,corpus_index,inspection_index):
    if not isinstance(annotation_id,str) or not re.fullmatch(r"[0-9a-f]{24}",annotation_id):
        raise RegistrationError("ANNOTATION_ID_INVALID")
    if axis not in ("x","y","z"):
        raise RegistrationError("REFERENCE_AXIS_INVALID")
    features=fv.candidates(query,corpus_index,inspection_index)
    if features.get("state")!="FEATURE_CANDIDATES_FOR_HUMAN_MAPPING":
        raise RegistrationError("EXACT_DRAWING_CAD_PAIR_REQUIRED")
    annotation=next((a for a in features["drawing_annotations"]
                     if a["annotation_id"]==annotation_id),None)
    if annotation is None:raise RegistrationError("ANNOTATION_NOT_FOUND")
    return features,annotation

def _rank(features,annotation,axis,vector):
    matches=[]
    alternatives=[]
    for surface in features["features"]:
        value=None
        if annotation["kind"]=="RADIUS" and surface["kind"]=="CYLINDER_RADIUS":
            if annotation["unit"]=="mm" and features["cad_source_units"]=="millimetre":
                value=surface["value"]
        elif annotation["kind"]=="ANGULAR" and surface["kind"]=="PLANE_DIRECTION":
            if annotation["unit"]=="degree":
                value=surface["angles_to_axes"][axis]
        if value is None:continue
        nominal=Decimal(str(annotation["nominal"]))
        delta=abs(nominal-Decimal(str(value)))
        within=delta<=WINDOW
        item={
            "feature_id":surface["feature_id"],
            "surface_kind":surface["kind"],
            "measured_value":str(value),
            "absolute_difference":str(delta.normalize()),
            "unit":annotation["unit"],
            "face_occurrences":surface["face_occurrences"],
            "sample_face_indices":surface["sample_face_indices"],
            "within_numeric_window":within,
            "correspondence_verified":False
        }
        (matches if within else alternatives).append((delta,item))
    matches.sort(key=lambda x:(x[0],x[1]["feature_id"]))
    alternatives.sort(key=lambda x:(x[0],x[1]["feature_id"]))
    if len(matches)==1:numeric_state="SINGLE_NUMERIC_CANDIDATE"
    elif len(matches)>1:numeric_state="MULTIPLE_NUMERIC_CANDIDATES"
    else:numeric_state="NO_NUMERIC_CANDIDATE"
    vector_state=vector["evidence_state"]
    if vector_state=="VECTOR_LEADER_OBSERVED":
        witness=vector["vector_witnesses"][0]
        if witness.get("targets_drawing_outline"):
            pdf_state="LEADER_ARROW_TOUCHES_DRAWING_CONTOUR"
        else:pdf_state="LEADER_ARROW_OBSERVED_NO_CONTOUR_CONTACT"
    elif vector_state=="VECTOR_ANGLE_ARC_OBSERVED":
        pdf_state="ANGLE_ARC_BETWEEN_TWO_ARROWS_OBSERVED"
    else:pdf_state="DOCUMENT_LEADER_AMBIGUOUS"
    return {
        "numeric_state":numeric_state,
        "document_geometry_state":pdf_state,
        "cad_candidates":[item for _,item in matches],
        "rejected_numeric_candidates":[item for _,item in alternatives[:6]],
        "other_numeric_candidates_count":len(alternatives),
        "numeric_window":str(WINDOW),
        "surface_reference_axis":axis if annotation["kind"]=="ANGULAR" else "NOT_APPLICABLE",
        "correspondence_state":"NOT_GEOMETRICALLY_REGISTERED",
        "projection_alignment":"NOT_ESTABLISHED",
        "human_verification_required":True,
        "manufacturing_authorization":False
    }

def inspect(query,annotation_id,axis="x",
            corpus_index=ec.DEFAULT_INDEX,inspection_index=p8.DEFAULT_INDEX):
    if not RENDER_LIMIT.acquire(blocking=False):
        raise RegistrationError("VECTOR_WORKERS_BUSY")
    try:
        features,annotation=_resolve(query,annotation_id,axis,corpus_index,inspection_index)
        vector,png=_vector_from_source(features["drawing"],annotation,corpus_index)
        matches=_rank(features,annotation,axis,vector)
        return {
            "state":"DRAWING_VECTOR_AND_CAD_CANDIDATES",
            "part":features["part"],
            "part_key":features["part_key"],
            "drawing":features["drawing"],
            "model":features["model"],
            "annotation":annotation,
            "vector_evidence":vector,
            "original_step_face_count":features["cad_face_count"],
            **matches,
            "note":"Observed PDF leaders/contours and numeric OpenCascade surfaces are grounded in the same public part's original files. No projection, arrow-target-to-CAD-face identity, revision approval or manufacturing tolerance has been independently established."
        },png
    finally:
        RENDER_LIMIT.release()

def describe(query,annotation_id,axis="x",
             corpus_index=ec.DEFAULT_INDEX,inspection_index=p8.DEFAULT_INDEX):
    body,_=inspect(query,annotation_id,axis,corpus_index,inspection_index)
    return body

def preview(query,annotation_id,axis="x",
            corpus_index=ec.DEFAULT_INDEX,inspection_index=p8.DEFAULT_INDEX):
    _,png=inspect(query,annotation_id,axis,corpus_index,inspection_index)
    return png
