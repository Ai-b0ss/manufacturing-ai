"""Source-locked P12 CAD raster preview with explicit unverified mapping.

The worker only sees a temporary copy of the exact SHA-verified public STEP.
Public caller-selected identifiers are checked against currently recomputed
P10 OpenCascade analytic features; no file path comes from HTTP parameters.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re
import struct
import threading

import engineering_corpus as ec
import engineering_inspection as p8
import feature_verification as fv

HERE=Path(__file__).resolve().parent
VIEWS=frozenset(("iso","front","side","top"))
class VisualError(ValueError):pass
RENDER_LIMIT=threading.BoundedSemaphore(2)

def _render_locked(query,feature_id,view="iso",corpus_index=ec.DEFAULT_INDEX,
           inspection_index=p8.DEFAULT_INDEX):
    if not isinstance(feature_id,str) or not re.fullmatch(r"[a-f0-9]{24}",feature_id):
        raise VisualError("FEATURE_ID_INVALID")
    if view not in VIEWS:raise VisualError("CAMERA_VIEW_INVALID")
    report=fv.candidates(query,corpus_index,inspection_index)
    if report.get("state")!="FEATURE_CANDIDATES_FOR_HUMAN_MAPPING":
        raise VisualError("NO_SOURCE_TRIPLET")
    source=report["model"]
    feature=next((item for item in report["features"] if item["feature_id"]==feature_id),None)
    if feature is None:raise VisualError("FEATURE_ID_UNKNOWN")
    raw,suffix=ec.original_file(source["id"],corpus_index)
    if suffix.lower() not in (".step",".stp") or hashlib.sha256(raw).hexdigest()!=source["sha256"]:
        raise VisualError("SOURCE_CHANGED")
    result=p8._run_worker(
        raw,suffix,HERE/"p12_cad_render_worker.py",p8.CAD_PYTHON,
        extra=[feature_id,view],timeout=17)
    if (result.get("state")!="CAD_FEATURE_RENDERED" or
            result.get("selected_feature_id")!=feature_id or
            result.get("source_sha256")!=source["sha256"] or
            result.get("view")!=view or
            result.get("selected_face_count")!=feature["face_occurrences"] or
            not set(feature["sample_face_indices"]).issubset(set(result["selected_face_indices"]))):
        raise VisualError("UNTRUSTED_RENDERER_EVIDENCE")
    try:
        image=base64.b64decode(result["png_base64"],validate=True)
        if not 20<len(image)<=3_000_000 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("image is not PNG")
        width,height=struct.unpack(">II",image[16:24])
        if width!=1000 or height!=660:
            raise ValueError("unexpected dimensions")
    except (ValueError,TypeError,KeyError,struct.error) as exc:
        raise VisualError("UNTRUSTED_RENDERER_PNG") from exc
    return image

def describe(query,feature_id,corpus_index=ec.DEFAULT_INDEX,
             inspection_index=p8.DEFAULT_INDEX):
    if not isinstance(feature_id,str) or not re.fullmatch(r"[a-f0-9]{24}",feature_id):
        raise VisualError("FEATURE_ID_INVALID")
    source=fv.candidates(query,corpus_index,inspection_index)
    if source.get("state")!="FEATURE_CANDIDATES_FOR_HUMAN_MAPPING":
        raise VisualError("NO_SOURCE_TRIPLET")
    feature=next((x for x in source["features"] if x["feature_id"]==feature_id),None)
    if feature is None:raise VisualError("FEATURE_ID_UNKNOWN")
    return {
        "state":"CAD_FEATURE_EVIDENCE",
        "drawing_source_id":source["drawing"]["id"],
        "drawing_sha256":source["drawing"]["sha256"],
        "cad_source_id":source["model"]["id"],
        "cad_sha256":source["model"]["sha256"],
        "feature":feature,
        "available_views":sorted(VIEWS),
        "feature_mapping_verified":False,
        "manufacturing_authorization":False,
        "note":"Highlighted geometry is a real STEP face group; correspondence with a PDF dimension leader has not been established."
    }

def render(query,feature_id,view="iso",corpus_index=ec.DEFAULT_INDEX,
           inspection_index=p8.DEFAULT_INDEX):
    if not RENDER_LIMIT.acquire(blocking=False):
        raise VisualError("RENDER_BUSY")
    try:
        return _render_locked(query,feature_id,view,corpus_index,inspection_index)
    finally:
        RENDER_LIMIT.release()
