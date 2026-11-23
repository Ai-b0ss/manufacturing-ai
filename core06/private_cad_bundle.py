"""Private, one-shot CAD bundle analysis (CSV BOM + PDF drawing + STEP model).

No customer content persists in the corpus, database, logs or shared indexes.
Existing P9/P8/P10 engineering parsers run as bounded, separate local processes.
Results are *candidates* requiring review, never manufacturing approval.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import csv
import hashlib
import json
import math
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
P9_PYTHON = Path(r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe")
CAD_PYTHON = Path(r"C:\Users\user\Documents\ManufacturingAI-P13-Drawing-CAD-Registration\.p8venv\Scripts\python.exe")

ROLES = {"bom": ((".csv",), 2_000_000),
         "drawing": ((".pdf",), 8_000_000),
         "model": ((".step", ".stp"), 8_000_000)}
MAX_PACKAGE = sum(item[1] for item in ROLES.values()) + 8192
MAGIC = b"CZKIT1\n"
MAX_MANIFEST = 4096
MAX_OUTPUT = 750_000


class BundleInvalid(ValueError):
    """One safe, stable client-visible error code for invalid source content."""


def _require(condition, code):
    if not condition:
        raise BundleInvalid(code)


@dataclass(frozen=True)
class BundleFile:
    role: str
    name: str
    data: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


def unpack_bundle(content: bytes):
    _require(isinstance(content, bytes) and 12 < len(content) <= MAX_PACKAGE,
             "PACKAGE_SIZE_INVALID")
    _require(content.startswith(MAGIC), "PACKAGE_FORMAT_INVALID")
    length = int.from_bytes(content[len(MAGIC):len(MAGIC)+4], "big")
    _require(0 < length <= MAX_MANIFEST, "MANIFEST_TOO_LARGE")
    start = len(MAGIC) + 4
    _require(start+length <= len(content), "MANIFEST_TRUNCATED")
    try:
        manifest = json.loads(content[start:start+length].decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BundleInvalid("MANIFEST_INVALID") from exc
    _require(isinstance(manifest, dict), "MANIFEST_INVALID")
    entries = manifest.get("files")
    _require(isinstance(entries, list) and len(entries) == 3,
             "EXPECTED_THREE_FILES")
    part = manifest.get("part")
    _require(isinstance(part, str) and len(part) <= 150 and
             (not part or re.fullmatch(r"[\w. \-()]{1,150}", part, re.UNICODE)),
             "PART_DESIGNATION_INVALID")
    files = {}
    cursor = start + length
    for entry in entries:
        _require(isinstance(entry, dict), "FILE_MANIFEST_INVALID")
        role, name, count = entry.get("role"), entry.get("name"), entry.get("size")
        _require(role in ROLES and role not in files, "FILE_ROLE_INVALID")
        ext, limit = ROLES[role]
        _require(isinstance(name, str) and 1 <= len(name) <= 180 and
                 not any(x in name for x in ("/", "\\", "\x00", "\r", "\n")) and
                 name.lower().endswith(ext), "FILE_NAME_INVALID")
        _require(type(count) is int and 0 < count <= limit, "FILE_SIZE_INVALID")
        _require(cursor + count <= len(content), "FILE_BODY_TRUNCATED")
        body = content[cursor:cursor+count]
        cursor += count
        if role == "drawing":
            _require(body.startswith(b"%PDF-"), "PDF_SIGNATURE_INVALID")
        elif role == "model":
            _require(body.lstrip().startswith(b"ISO-10303-21;"),
                     "STEP_SIGNATURE_INVALID")
        else:
            _require(b"\x00" not in body, "CSV_BINARY_CONTENT")
        files[role] = BundleFile(role, name, body)
    _require(set(files) == set(ROLES) and cursor == len(content),
             "PACKAGE_TRAILING_BYTES")
    return files, part.strip()


def read_bom(content: bytes, part: str):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("cp1251")
        except UnicodeDecodeError as exc:
            raise BundleInvalid("BOM_ENCODING_UNSUPPORTED") from exc
    _require(len(text) <= 600_000, "BOM_TOO_MANY_CHARACTERS")
    try:
        reader = csv.DictReader(StringIO(text), strict=True)
        original_fields = reader.fieldnames
        _require(isinstance(original_fields, list) and 2 <= len(original_fields) <= 40,
                 "BOM_HEADERS_INVALID")
        fields = {str(k).strip().casefold(): k for k in original_fields}
        key = next((fields[x] for x in
                    ("part_name", "part", "name", "designation", "наименование", "обозначение")
                    if x in fields), None)
        _require(key is not None, "BOM_PART_COLUMN_MISSING")
        target = part.casefold().strip()
        hits = []
        for row_no, row in enumerate(reader, start=2):
            _require(row_no <= 3001, "BOM_ROW_LIMIT")
            _require(isinstance(row, dict) and None not in row,
                     "BOM_ROW_COLUMN_MISMATCH")
            _require(all(isinstance(value, str) and len(value) <= 800
                         for value in row.values()), "BOM_CELL_TOO_LONG")
            if row.get(key, "").strip().casefold() == target:
                hits.append((row_no, row))
                _require(len(hits) <= 1, "BOM_PART_AMBIGUOUS")
    except csv.Error as exc:
        raise BundleInvalid("BOM_CSV_MALFORMED") from exc
    _require(bool(hits), "PART_NOT_FOUND_IN_BOM")
    number, row = hits[0]
    lowered = {str(k).strip().casefold(): v for k, v in row.items()}
    return {
        "part": row[key].strip(), "row": number,
        "item_no": lowered.get("item_no", ""),
        "material": lowered.get("material", ""),
        "quantity": lowered.get("qty", lowered.get("quantity", "")),
        "unit": lowered.get("unit", ""),
        "revision": lowered.get("rev", lowered.get("revision", "")),
    }


def _hidden_startupinfo():
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return info


def _execute_worker(python: Path, script: str, source: Path, prefix: str, timeout=45):
    _require(python.is_file(), "LOCAL_PARSER_UNAVAILABLE")
    try:
        completed = subprocess.run(
            [str(python), "-I", str(ROOT/script), str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=_hidden_startupinfo(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BundleInvalid("CAD_ANALYSIS_TIMEOUT_OR_RUNTIME_ERROR") from exc
    _require(completed.returncode == 0 and len(completed.stdout) <= MAX_OUTPUT,
             "ENGINEERING_PARSER_REJECTED_FILE")
    try:
        stdout = completed.stdout.decode("ascii").strip()
        _require(stdout.startswith(prefix), "ENGINEERING_PARSER_BAD_RESPONSE")
        result = json.loads(stdout[len(prefix):])
    except (ValueError, UnicodeError) as exc:
        raise BundleInvalid("ENGINEERING_PARSER_BAD_RESPONSE") from exc
    _require(isinstance(result, dict) and not result.get("error"),
             "ENGINEERING_PARSER_REJECTED_FILE")
    return result


def analyze_bundle(package: bytes):
    files, requested_part = unpack_bundle(package)
    part = (requested_part or Path(files["model"].name).stem).strip()
    _require(1 <= len(part) <= 150 and re.fullmatch(r"[\w. \-()]{1,150}", part, re.UNICODE),
             "PART_DESIGNATION_INVALID")
    bom = read_bom(files["bom"].data, part)
    with tempfile.TemporaryDirectory(prefix="czkit-private-") as tmp:
        temp = Path(tmp)
        local = {
            "drawing": temp/"drawing.pdf",
            "model": temp/"model.step",
        }
        for role, dest in local.items():
            dest.write_bytes(files[role].data)
        drawing = _execute_worker(P9_PYTHON, "p9_dimension_worker.py",
                                  local["drawing"], "P9_RESULT_JSON:")
        topology = _execute_worker(CAD_PYTHON, "p8_cad_worker.py",
                                   local["model"], "P8_RESULT_JSON:")
        cad = _execute_worker(CAD_PYTHON, "p10_cad_feature_worker.py",
                              local["model"], "P8_RESULT_JSON:")

    _require(drawing.get("source_sha256") == files["drawing"].sha256 and
             cad.get("source_sha256") == files["model"].sha256,
             "SOURCE_INTEGRITY_MISMATCH")
    _require(topology.get("geometry_verified") is True and
             cad.get("state") == "ANALYTIC_CAD_CANDIDATES",
             "CAD_TOPOLOGY_UNVERIFIED")
    annotations = drawing.get("annotations")
    features = cad.get("features")
    _require(isinstance(annotations, list) and isinstance(features, list),
             "ENGINEERING_EVIDENCE_INCOMPLETE")
    units_ok = (topology.get("units") == "millimetre" and
                cad.get("units") == "millimetre")
    stems_equal = (Path(files["drawing"].name).stem.casefold() ==
                   Path(files["model"].name).stem.casefold() ==
                   part.casefold())
    checks = []
    for a in annotations[:120]:
        if not isinstance(a, dict):
            continue
        label, kind, nominal = a.get("label"), a.get("kind"), a.get("nominal")
        if not isinstance(label, str) or not isinstance(nominal, (int, float)):
            continue
        if not math.isfinite(nominal):
            continue
        candidates = []
        if kind == "RADIUS" and a.get("unit") == "mm" and units_ok:
            candidates = sorted(
                [
                    {"feature_id": f["feature_id"], "value": f["value"],
                     "difference_mm": round(abs(f["value"] - nominal), 5),
                     "face_occurrences": f.get("face_occurrences", 0)}
                    for f in features if isinstance(f, dict)
                    and f.get("kind") == "CYLINDER_RADIUS"
                    and isinstance(f.get("value"), (float, int))
                    and abs(f["value"] - nominal) <= 0.02
                ],
                key=lambda x: (x["difference_mm"], x["feature_id"])
            )[:8]
        checks.append({
            "label": label[:90], "page": a.get("page"), "kind": kind,
            "nominal": nominal, "unit": a.get("unit"),
            "numeric_candidates": candidates, "geometry_mapping": "NOT_VERIFIED",
            "tolerance_status": "NOT_ESTABLISHED"
        })

    warnings = [
        "Source relation is selected by the user, not verified by a PLM/ERP revision authority.",
        "A numerical match is not proof of a corresponding PDF leader or STEP surface.",
        "Tolerance, approvals, real revision, and manufacturing applicability are not verified.",
        "Source bytes are processed in a temporary directory and are not added to the common document corpus.",
    ]
    if not stems_equal:
        warnings.append("PDF/STEP filenames and BOM part designation do not match exactly.")
    if not units_ok:
        warnings.append("STEP declared length units are uncertain; numeric radius comparison was skipped.")
    if not checks:
        warnings.append("No machine-readable dimension callouts found; scanned PDF drawings need a separate OCR path.")
    return {
        "schema": "factory_private_cad_bundle_v1",
        "state": "REVIEW_REQUIRED",
        "part": part, "bom": bom,
        "linked_by": "MATCHING_PART_NAMES_UNVERIFIED_REVISION" if stems_equal else "USER_SELECTED_FILES_UNVERIFIED_LINK",
        "sources": [
            {"role": role, "name": files[role].name,
             "size": len(files[role].data), "sha256": files[role].sha256}
            for role in ("bom", "drawing", "model")
        ],
        "model": {"state": topology.get("state"),
                  "bbox_mm": topology.get("xyz_extent") if units_ok else None,
                  "solid_count": topology.get("solid_count"),
                  "face_count": topology.get("face_count"),
                  "feature_count": cad.get("feature_count"),
                  "units": topology.get("units")},
        "drawing": {"dimension_count": len(annotations),
                    "returned_count": len(checks), "more_dimensions": len(annotations) > len(checks)},
        "dimension_checks": checks,
        "warnings": warnings,
        "manufacturing_authorization": False,
        "source_persistence": "NONE",
    }
