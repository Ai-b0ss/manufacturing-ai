"""Real OpenCascade analytic-face measurements compared to P9 PDF callouts.

Only explicitly selected matching surface types are comparable. No feature
correspondence, GD&T, approval or manufacturing readiness is established.
"""
from __future__ import annotations
from decimal import Decimal
import hashlib
import re

import dimension_trace as p9
import engineering_corpus as ec
import engineering_inspection as p8

HERE=p9.HERE
class FeatureError(ValueError): pass

def _read_cad(source, corpus_index):
    data,suffix=ec.original_file(source["id"],corpus_index)
    if suffix not in (".step",".stp") or hashlib.sha256(data).hexdigest()!=source["sha256"]:
        raise FeatureError("STEP_SOURCE_VERIFICATION_FAILED")
    return p8._run_worker(data,suffix,HERE/"p10_cad_feature_worker.py",p8.CAD_PYTHON,timeout=20)

def candidates(query,corpus_index=ec.DEFAULT_INDEX,inspection_index=p8.DEFAULT_INDEX):
    source=p9.annotations(query,corpus_index,inspection_index)
    if source["state"]!="CANDIDATE_DIMENSIONS_FOR_REVIEW":
        return {"state":"NOT_COMPARABLE","manufacturing_authorization":False}
    cad=_read_cad(source["model"],corpus_index)
    if cad["source_sha256"]!=source["model"]["sha256"]:
        raise FeatureError("CAD_SOURCE_CHANGED")
    features=cad["features"]
    return {
        "state":"FEATURE_CANDIDATES_FOR_HUMAN_MAPPING",
        "part":source["part"],"part_key":source["part_key"],
        "drawing":source["drawing"],"model":source["model"],
        "drawing_annotations":[a for a in source["annotations"] if a["kind"] in ("RADIUS","ANGULAR")],
        "features":features,"features_count":len(features),
        "cad_source_units":cad["units"],"cad_file_declared_units":cad["file_declared_units"],
        "cad_face_count":cad["face_count"],
        "annotation_to_face_mapping":"NOT_ESTABLISHED",
        "approval_status":"NOT_VERIFIED",
        "manufacturing_authorization":False
    }

def compare(query,annotation_id,feature_id,axis="x",window="0.02",
            corpus_index=ec.DEFAULT_INDEX,inspection_index=p8.DEFAULT_INDEX):
    if not re.fullmatch(r"[0-9a-f]{24}",str(annotation_id)) or not re.fullmatch(r"[0-9a-f]{24}",str(feature_id)):
        raise FeatureError("ID_INVALID")
    if axis not in ("x","y","z"):raise FeatureError("AXIS_INVALID")
    if not isinstance(window,str) or not re.fullmatch(r"(?:0|0\.\d{1,4})",window):
        raise FeatureError("WINDOW_INVALID")
    margin=Decimal(window)
    if margin>Decimal("0.20"):raise FeatureError("WINDOW_LIMIT")
    data=candidates(query,corpus_index,inspection_index)
    if data["state"]!="FEATURE_CANDIDATES_FOR_HUMAN_MAPPING":
        return {"state":"NOT_COMPARABLE","manufacturing_authorization":False}
    annotation=next((a for a in data["drawing_annotations"] if a["annotation_id"]==annotation_id),None)
    feature=next((f for f in data["features"] if f["feature_id"]==feature_id),None)
    if not annotation or not feature:raise FeatureError("UNRECOGNIZED_SOURCE_ELEMENT")
    if annotation["kind"]=="RADIUS" and feature["kind"]=="CYLINDER_RADIUS":
        if annotation["unit"]!="mm" or data["cad_source_units"]!="millimetre":
            return {"state":"UNITS_UNVERIFIED","manufacturing_authorization":False}
        value=feature["value"];unit="mm"
    elif annotation["kind"]=="ANGULAR" and feature["kind"]=="PLANE_DIRECTION":
        value=feature["angles_to_axes"][axis];unit="degree"
    else:
        raise FeatureError("DIMENSION_SURFACE_TYPES_INCOMPATIBLE")
    diff=abs(Decimal(str(annotation["nominal"]))-Decimal(str(value)))
    match=diff<=margin
    return {
        "state":"ANALYTIC_VALUES_WITHIN_SELECTED_WINDOW" if match else "ANALYTIC_VALUES_OUTSIDE_SELECTED_WINDOW",
        "comparison_kind":"USER_SELECTED_PDF_CALLOUT_TO_ANALYTIC_STEP_FACE_HYPOTHESIS",
        "part":data["part"],"drawing":data["drawing"],"model":data["model"],
        "annotation":annotation,"surface":feature,
        "surface_reference_axis":axis if annotation["kind"]=="ANGULAR" else "NOT_APPLICABLE",
        "cad_surface_measurement":str(value),"unit":unit,
        "absolute_difference":str(diff.normalize()),"selected_window":str(margin),
        "numeric_match":match,"feature_mapping_verified":False,
        "engineering_tolerance_verification":"NOT_PERFORMED",
        "revision_status":"UNVERIFIED","manufacturing_authorization":False,
        "explanation":"The numeric values agree/disagree for the selected analytic face; the drawing callout's actual geometric target has not been identified."
    }
