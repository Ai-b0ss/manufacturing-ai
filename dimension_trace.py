"""P9: check a *user-selected numeric hypothesis* against a real CAD bbox.

A visible drawing number is NOT automatically an overall dimension. Neither
numeric agreement nor a selected axis can approve a manufacturing revision.
Every call is bound to P7 exact part joins, source SHA-256 and P8 BREP evidence.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from contextlib import closing
import json
import pathlib
import re
import subprocess
import tempfile
import hashlib
from typing import Any

import engineering_corpus as ec
import engineering_inspection as p8

HERE=pathlib.Path(__file__).resolve().parent
DEFAULT_PYTHON=p8.PDF_PYTHON
MAX_PDF_BYTES=9_000_000
MAX_PDF_WAIT=13

class DimensionError(ValueError):
    pass

def _read_worker(data:bytes,expected_sha:str)->dict[str,Any]:
    if len(data)>MAX_PDF_BYTES:
        raise DimensionError("PDF_SIZE_LIMIT")
    if hashlib.sha256(data).hexdigest()!=expected_sha:
        raise DimensionError("SOURCE_SHA_MISMATCH")
    if not DEFAULT_PYTHON.is_file():
        raise DimensionError("DIMENSION_PARSER_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="p9-annotation-") as tmp:
        copy=pathlib.Path(tmp)/"drawing.pdf"
        copy.write_bytes(data)
        try:
            result=subprocess.run(
                [str(DEFAULT_PYTHON),str(HERE/"p9_dimension_worker.py"),str(copy)],
                capture_output=True,check=False,timeout=MAX_PDF_WAIT,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except (OSError,subprocess.TimeoutExpired) as exc:
            raise DimensionError("DIMENSION_PARSER_TIMEOUT") from exc
        if result.returncode:
            raise DimensionError("DIMENSION_PARSER_FAILED")
        try:
            lines=result.stdout.decode("utf-8","strict").splitlines()
            framed=[line.partition("P9_RESULT_JSON:")[2] for line in lines
                    if line.startswith("P9_RESULT_JSON:")]
            if len(framed)!=1:raise ValueError("Missing unique frame")
            parsed=json.loads(framed[0])
            if parsed.get("error") or parsed.get("source_sha256")!=expected_sha:
                raise ValueError("Parser source mismatch")
            return parsed
        except (ValueError,UnicodeError,KeyError) as exc:
            raise DimensionError("DIMENSION_PARSER_UNTRUSTED_OUTPUT") from exc

def _sources(query:str, corpus_index:pathlib.Path, inspection_index:pathlib.Path):
    if not isinstance(query,str) or len(query)>150 or len(query.strip())<2:
        raise DimensionError("QUERY_INVALID")
    item=ec.answer(query,corpus_index)
    if item.get("state")!="BOM_DRAWING_STEP_LINKED":
        return item,None,None
    drawing=next((x for x in item["sources"] if x["role"]=="DRAWING"),None)
    model=next((x for x in item["sources"] if x["role"]=="MODEL"),None)
    if not drawing or not model:raise DimensionError("UNVERIFIED_PAIR")
    # Both must belong to the same exact P7 part and group, not fuzzy names.
    with closing(ec._open_readonly(corpus_index)) as corpus:
        group_rows=corpus.execute(
            "SELECT id,source_group FROM source WHERE id IN (?,?)",
            (drawing["id"],model["id"])).fetchall()
    if len(group_rows)!=2 or any(x["source_group"]!=item["group"] for x in group_rows):
        raise DimensionError("CROSS_GROUP_PAIR_FORBIDDEN")
    cad=p8.inspect_source(model["id"],corpus_index,inspection_index)
    geo=cad["inspection"]
    original,pdf_ext=ec.original_file(drawing["id"],corpus_index)
    if pdf_ext!=".pdf":raise DimensionError("DRAWING_NOT_PDF")
    tokens=_read_worker(original,drawing["sha256"])
    return item,(drawing,tokens),(model,geo)

def annotations(query:str,corpus_index:pathlib.Path=ec.DEFAULT_INDEX,
                inspection_index:pathlib.Path=p8.DEFAULT_INDEX)->dict[str,Any]:
    item,drawing,model=_sources(query,corpus_index,inspection_index)
    if drawing is None:
        return {"state":item.get("state","NOT_FOUND"),
                "message":"No exact P7 drawing + STEP + BOM join",
                "manufacturing_authorization":False}
    source,parsed=drawing
    modelsrc,geometry=model
    return {
        "state":"CANDIDATE_DIMENSIONS_FOR_REVIEW",
        "part":item["part"],"group":item["group"],"part_key":item["part_key"],
        "drawing":{"id":source["id"],"name":source["name"],
                   "sha256":source["sha256"],"original_url":source["url"]},
        "model":{"id":modelsrc["id"],"name":modelsrc["name"],
                 "sha256":modelsrc["sha256"],"original_url":modelsrc["url"]},
        "annotations":parsed["annotations"],
        "annotations_count":parsed["count"],
        "cad":{
            "state":geometry.get("state"),
            "xyz_extent":geometry.get("xyz_extent"),
            "file_length_units":geometry.get("file_declared_length_units"),
            "units":geometry.get("units"),
            "solid_count":geometry.get("solid_count"),
            "brep_valid":geometry.get("brep_valid")
        },
        "bbox_to_drawing_mapping":"NOT_ESTABLISHED",
        "drawing_tolerances":"NOT_ESTABLISHED",
        "revision_status":"NOT_VERIFIED",
        "manufacturing_authorization":False,
        "notice":"Select a source callout and a hypothetical CAD bbox axis. Exact numeric equality is NOT proof that they describe the same feature."
    }

def compare(query:str, annotation_id:str, axis:str, margin:str="0.02",
            corpus_index:pathlib.Path=ec.DEFAULT_INDEX,
            inspection_index:pathlib.Path=p8.DEFAULT_INDEX)->dict[str,Any]:
    if not re.fullmatch(r"[a-f0-9]{24}",str(annotation_id)):
        raise DimensionError("ANNOTATION_ID_INVALID")
    if axis not in {"x","y","z"}:
        raise DimensionError("CAD_AXIS_INVALID")
    if not isinstance(margin,str) or not re.fullmatch(r"(?:0|0\.\d{1,4})",margin):
        raise DimensionError("WINDOW_INVALID")
    try:
        window=Decimal(margin)
    except InvalidOperation as exc:
        raise DimensionError("WINDOW_INVALID") from exc
    if window<0 or window>Decimal("0.20"):
        raise DimensionError("WINDOW_OUT_OF_RANGE")
    result=annotations(query,corpus_index,inspection_index)
    if result["state"]!="CANDIDATE_DIMENSIONS_FOR_REVIEW":
        return {"state":"NOT_COMPARABLE","reason":"NO_EXACT_PART_SOURCES",
                "manufacturing_authorization":False}
    ann=next((x for x in result["annotations"]
              if x["annotation_id"]==annotation_id),None)
    if not ann:raise DimensionError("ANNOTATION_NOT_FOUND")
    if ann["kind"]!="LINEAR":
        raise DimensionError("ONLY_LINEAR_CALLOUTS_SUPPORTED")
    if ann["unit"]!="mm" or result["cad"]["units"]!="millimetre":
        return {"state":"UNITS_NOT_VERIFIED","annotation":ann,
                "cad_units":result["cad"]["units"],
                "manufacturing_authorization":False}
    if result["cad"]["state"]!="BREP_VALID" or not result["cad"]["brep_valid"]:
        return {"state":"GEOMETRY_NOT_VERIFIED","manufacturing_authorization":False}
    axis_index={"x":0,"y":1,"z":2}[axis]
    xyz=result["cad"]["xyz_extent"]
    if not isinstance(xyz,list) or len(xyz)!=3:
        raise DimensionError("CAD_BBOX_INVALID")
    numeric=Decimal(str(ann["nominal"]))
    observed=Decimal(str(xyz[axis_index]))
    delta=abs(observed-numeric)
    within=delta<=window
    return {
        "state":"NUMERIC_WITHIN_SELECTED_WINDOW" if within else "NUMERIC_OUTSIDE_SELECTED_WINDOW",
        "comparison_kind":"UNVERIFIED_USER_SELECTED_DIMENSION_TO_CAD_BBOX_HYPOTHESIS",
        "part":result["part"],
        "drawing":result["drawing"],"model":result["model"],
        "annotation":ann,
        "chosen_axis":axis,
        "cad_bbox_extent":str(observed),
        "cad_length_unit":"mm",
        "selected_window_mm":str(window),
        "absolute_difference_mm":str(delta.normalize()),
        "numerical_coincidence":within,
        "feature_mapping_verified":False,
        "geometric_compliance":"NOT_ESTABLISHED",
        "drawing_tolerance_approved":False,
        "revision_approved":False,
        "manufacturing_authorization":False,
        "message":("Values are numerically close within the user's selected window, "
                   "but feature identity, tolerances and approved revision are unknown."
                   if within else
                   "Values differ more than the selected window. This does not prove a part defect: "
                   "the chosen drawing callout may not be a CAD bounding-box dimension.")
    }

def catalog(corpus_index:pathlib.Path=ec.DEFAULT_INDEX)->dict[str,Any]:
    # Only the three real, exact triads that P7 proved are eligible.
    names=["focuser_drawtube","mirror_cell_base","lower_square_tube"]
    results=[]
    for name in names:
        answer=ec.answer(name,corpus_index)
        if answer.get("state")=="BOM_DRAWING_STEP_LINKED":
            results.append({"part":answer["part"],"group":answer["group"]})
    return {"items":results,"total":len(results),
            "manufacturing_authorization":False}
