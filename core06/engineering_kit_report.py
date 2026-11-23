"""Deterministic engineering kit report from linked BOM, PDF drawing, STEP.

This is an evidence SUMMARY, not a CAD approval, not LLM-generated.
Both source responses must come from the existing authenticated read-only
engineering adapter. Never assume equal numbers imply same geometric face.
"""
from __future__ import annotations

import re


class InvalidEngineeringKit(ValueError):
    pass


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def build_engineering_kit_report(part_data: dict, geometry_data: dict) -> dict:
    if not isinstance(part_data, dict) or not isinstance(geometry_data, dict):
        raise InvalidEngineeringKit("INVALID_SOURCE_RESPONSE")
    match = part_data.get("source_match")
    if not isinstance(match, dict) or match.get("state") != "BOM_DRAWING_STEP_LINKED":
        raise InvalidEngineeringKit("BOM_PDF_STEP_NOT_LINKED")
    part = match.get("part")
    if not isinstance(part, str) or not part or part != geometry_data.get("part"):
        raise InvalidEngineeringKit("PART_IDENTITY_MISMATCH")
    rows = match.get("sources")
    if not isinstance(rows, list) or len(rows) != 3:
        raise InvalidEngineeringKit("EXPECTED_THREE_SOURCE_FILES")
    sources = {}
    for item in rows:
        if not isinstance(item, dict):
            raise InvalidEngineeringKit("INVALID_SOURCE_METADATA")
        role = item.get("role")
        identifier = item.get("id")
        if (role not in ("BOM", "DRAWING", "MODEL") or role in sources
            or not isinstance(identifier, str)
            or re.fullmatch(r"[a-f0-9]{24}", identifier) is None
            or not _hash(item.get("sha256"))
            or not isinstance(item.get("name"), str)
            or not 1 <= len(item["name"]) <= 180):
            raise InvalidEngineeringKit("SOURCE_ID_OR_SHA_INVALID")
        sources[role] = {
            "role": role,
            "id": identifier,
            "name": item["name"],
            "sha256": item["sha256"],
            "download_path": "/api/engineering/file?id=" + identifier,
        }
    if set(sources) != {"BOM", "DRAWING", "MODEL"}:
        raise InvalidEngineeringKit("BOM_PDF_STEP_NOT_COMPLETE")
    for role, key in (("DRAWING", "drawing"), ("MODEL", "model")):
        other = geometry_data.get(key)
        if (not isinstance(other, dict)
            or other.get("id") != sources[role]["id"]
            or other.get("sha256") != sources[role]["sha256"]):
            raise InvalidEngineeringKit("SOURCE_INTEGRITY_DISAGREEMENT")

    annotations = geometry_data.get("drawing_annotations")
    features = geometry_data.get("features")
    if not isinstance(annotations, list) or not isinstance(features, list):
        raise InvalidEngineeringKit("CAD_GEOMETRY_UNAVAILABLE")
    if len(annotations) > 100 or len(features) > 10000:
        raise InvalidEngineeringKit("UNEXPECTED_CAD_RESULT_SIZE")

    checks = []
    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise InvalidEngineeringKit("INVALID_ANNOTATION")
        kind, nominal = annotation.get("kind"), annotation.get("nominal")
        label = annotation.get("label")
        if kind not in ("RADIUS", "ANGULAR") or not isinstance(label, str):
            continue
        if not isinstance(nominal, (int, float)) or isinstance(nominal, bool):
            continue
        # Radius-only numerical matching. Angle comparisons require choosing
        # a reference axis and are explicitly not inferred by this report.
        count = 0
        if kind == "RADIUS":
            count = sum(
                1 for face in features
                if isinstance(face, dict)
                and face.get("kind") == "CYLINDER_RADIUS"
                and isinstance(face.get("value"), (int, float))
                and not isinstance(face.get("value"), bool)
                and abs(face["value"] - nominal) <= 0.02
            )
        checks.append({
            "annotation": label[:90],
            "kind": kind,
            "nominal": nominal,
            "page": annotation.get("page") if isinstance(annotation.get("page"), int) else None,
            "numeric_candidates": count if kind == "RADIUS" else None,
            "result": "NUMERIC_CANDIDATES_ONLY" if count else "REQUIRES_MANUAL_MAPPING",
        })

    # A report cannot approve manufacture even with one matching number.
    risks = part_data.get("risks")
    if not isinstance(risks, list):
        risks = []
    risks = [x for x in risks if isinstance(x, str) and len(x) <= 100][:30]
    return {
        "schema": "factory_engineering_kit_report_v1",
        "part": part,
        "part_key": match.get("part_key"),
        "group": match.get("group"),
        "material": match.get("material"),
        "item_no": match.get("item_no"),
        "quantity": match.get("quantity"),
        "sources": [sources[k] for k in ("BOM", "DRAWING", "MODEL")],
        "drawing_annotations": len(annotations),
        "cad_features": len(features),
        "dimension_checks": checks,
        "revision_status": str(part_data.get("revision_status", "UNVERIFIED")),
        "manufacturing_authorization": False,
        "state": "ENGINEERING_REVIEW_REQUIRED",
        "risks": risks,
        "limitations": [
            "This is a read-only analysis of the approved public pilot corpus, not newly uploaded STEP files.",
            "Matching dimensions are numerical CAD candidates, not a proved PDF-to-3D face registration.",
            "Drawing tolerances, revisions, approvals and production applicability are not certified.",
            "An engineer must verify CAD surfaces, actual release status and manufacturing applicability.",
        ],
    }
