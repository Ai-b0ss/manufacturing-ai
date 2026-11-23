"""P7: deterministic, isolated engineering-document corpus and provenance index.

No LLM is allowed to approve revisions or invent join keys. Source files stay in
their public reference directory and are never modified by ingestion.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

HERE = Path(__file__).resolve().parent
DEFAULT_INDEX = HERE / "data" / "engineering_corpus.sqlite3"
DEFAULT_CORPUS = HERE.parent / "ManufacturingAI-Design-Preview" / ".preview" / "public_engineering_corpus_20261117"
DEFAULT_PYTHON = Path(os.environ.get("FACTORY_AI_PARSER_PY",
    r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"))
SUPPORTED = {".pdf": "PDF", ".csv": "CSV", ".xlsx": "XLSX",
             ".step": "STEP", ".stp": "STEP", ".png": "IMAGE",
             ".txt": "TEXT", ".md": "TEXT"}
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE dataset (
  singleton INTEGER PRIMARY KEY CHECK (singleton=1),
  source_root TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  indexed_at INTEGER NOT NULL,
  source_count INTEGER NOT NULL
);
CREATE TABLE source (
  id TEXT PRIMARY KEY,
  source_group TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL UNIQUE,
  original_sha256 TEXT NOT NULL,
  original_size INTEGER NOT NULL,
  original_url TEXT NOT NULL,
  parse_status TEXT NOT NULL,
  page_count INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE source_page (
  source_id TEXT NOT NULL REFERENCES source(id),
  page INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (source_id,page)
);
CREATE TABLE bom_line (
  source_id TEXT NOT NULL REFERENCES source(id),
  row_number INTEGER NOT NULL,
  part_key TEXT NOT NULL,
  part_name TEXT NOT NULL,
  item_no TEXT NOT NULL,
  material TEXT NOT NULL,
  quantity TEXT NOT NULL,
  unit TEXT NOT NULL,
  revision TEXT NOT NULL,
  row_json TEXT NOT NULL,
  PRIMARY KEY (source_id,row_number)
);
CREATE INDEX bom_by_part ON bom_line(part_key);
CREATE TABLE relation (
  part_key TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES source(id),
  relation_type TEXT NOT NULL,
  rule TEXT NOT NULL,
  evidence TEXT NOT NULL,
  PRIMARY KEY (part_key,source_id,relation_type)
);
CREATE INDEX relation_by_part ON relation(part_key);
CREATE TABLE reference_notice (
  notice_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  url TEXT NOT NULL,
  page INTEGER NOT NULL,
  documented_change TEXT NOT NULL,
  effective_date_reference TEXT NOT NULL,
  evidence_status TEXT NOT NULL
);
"""

class CorpusError(ValueError):
    pass


def source_id(group: str, name: str) -> str:
    return hashlib.sha256((group + "/" + name).encode("utf-8")).hexdigest()[:24]


def part_key(group: str, value: str) -> str:
    # Preserve original Unicode code points; never equate Latin and Cyrillic.
    return group + ":" + value.strip().casefold()


def _safe_path(root: Path, group: str, name: str) -> Path:
    if not group or not name or "/" in name or "\\" in name or ":" in name:
        raise CorpusError("Invalid manifest file name")
    if any(ch in group for ch in "/\\:") or group in (".", "..") or name in (".", ".."):
        raise CorpusError("Unsafe manifest group")
    path = root / group / name
    if path.is_symlink() or (root / group).is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise CorpusError("Unsafe path or symlink")
    if not path.is_file():
        raise CorpusError("Source missing: " + group + "/" + name)
    return path


def _verify_file(path: Path, expected_size: int, expected_hash: str) -> None:
    if path.stat().st_size != expected_size or expected_size > 10_000_000:
        raise CorpusError("Source size mismatch or too large: " + path.name)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected_hash.lower():
        raise CorpusError("SHA-256 mismatch: " + path.name)


def _pdf_pages(path: Path, parser_python: Path) -> dict:
    if not parser_python.is_file():
        return {"pages": [], "page_count": 0, "error": "PARSER_UNAVAILABLE"}
    try:
        proc = subprocess.run(
            [str(parser_python), str(HERE / "engineering_pdf_worker.py"), str(path)],
            capture_output=True, timeout=25, check=False, creationflags=0x08000000 if os.name == "nt" else 0)
        if proc.returncode != 0:
            return {"pages": [], "page_count": 0, "error": "PDF_EXTRACTION_FAILED"}
        data = json.loads(proc.stdout.decode("utf-8"))
        if not isinstance(data.get("pages"), list):
            return {"pages": [], "page_count": 0, "error": "INVALID_WORKER_RESPONSE"}
        return data
    except (subprocess.TimeoutExpired, UnicodeError, ValueError, OSError):
        return {"pages": [], "page_count": 0, "error": "PDF_EXTRACTION_FAILED"}


def _read_csv(path: Path) -> list[tuple[int, dict]]:
    # CSV may contain quotes and commas; use real CSV parser, not split(',').
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or len(reader.fieldnames) > 80:
        raise CorpusError("Invalid CSV columns")
    return [(index, dict(row)) for index, row in enumerate(reader, start=2)
            if any(str(v or "").strip() for v in row.values())]


def _read_xlsx(path: Path) -> list[tuple[int, dict]]:
    # Minimal read-only OOXML extractor. No formulas are evaluated or executed.
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 300 or sum(x.file_size for x in infos) > 18_000_000:
            raise CorpusError("XLSX decompressed content exceeds limits")
        for item in infos:
            if item.file_size > 5_000_000:
                raise CorpusError("XLSX member too large")
        def xml(name: str):
            data = archive.read(name)
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise CorpusError("XML entities not allowed")
            return ET.fromstring(data)
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(node.itertext())
                       for node in xml("xl/sharedStrings.xml").findall("m:si", ns)]
        if "xl/worksheets/sheet1.xml" not in archive.namelist():
            raise CorpusError("XLSX first worksheet missing")
        sheet = xml("xl/worksheets/sheet1.xml")
        output = []
        for row in sheet.findall(".//m:sheetData/m:row", ns):
            number = int(row.get("r", "0"))
            fields = {}
            for cell in row.findall("m:c", ns):
                key = re.match(r"^[A-Z]+", cell.attrib.get("r", ""))
                if not key:
                    continue
                val = cell.find("m:v", ns)
                inline = cell.find("m:is", ns)
                if val is not None and val.text is not None:
                    value = strings[int(val.text)] if cell.attrib.get("t") == "s" else val.text
                elif inline is not None:
                    value = "".join(inline.itertext())
                else:
                    value = ""
                fields[key.group()] = value
            if any(str(v).strip() for v in fields.values()):
                output.append((number, fields))
        return output


def _bom_lines(group: str, kind: str, path: Path) -> list[tuple[int, str, str, str, str, str, str, dict]]:
    lines = []
    if kind == "CSV" and path.name.lower().endswith("bom.csv"):
        for row_no, row in _read_csv(path):
            name = (row.get("part_name") or row.get("SKU") or "").strip()
            if not name:
                continue
            lines.append((row_no, name, str(row.get("item_no") or ""),
                          str(row.get("material") or ""),
                          str(row.get("qty") or row.get("Qty") or ""),
                          str(row.get("unit") or "pc"),
                          str(row.get("REV") or ""), row))
    elif kind == "XLSX" and "bom" in path.name.casefold():
        # OLSK stores names in column A and quantity in B, with category rows.
        for row_no, row in _read_xlsx(path):
            name = str(row.get("A") or "").strip()
            qty = str(row.get("B") or "").strip()
            if not name or not qty or not re.fullmatch(r"\d+(\.0+)?", qty):
                continue
            lines.append((row_no, name, "", "", qty, "pc", "", row))
    return lines


def build_index(corpus_root: Path = DEFAULT_CORPUS,
                index_path: Path = DEFAULT_INDEX,
                parser_python: Path = DEFAULT_PYTHON) -> dict:
    root = Path(corpus_root).resolve(strict=True)
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise CorpusError("Manifest has no documents")
    if len(documents) > 200:
        raise CorpusError("Manifest exceeds bounded import size")

    index_path = Path(index_path).resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_name(index_path.name + "." + uuid.uuid4().hex + ".tmp")
    counts = {"sources": 0, "bom_lines": 0, "relations": 0,
              "pdf_pages": 0, "pdf_unreadable_pages": 0, "pdf_parser_failures": 0}
    try:
        db = sqlite3.connect(temporary)
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        seen = set()
        for entry in documents:
            group = entry.get("group")
            name = entry.get("name")
            if not isinstance(group, str) or not isinstance(name, str):
                raise CorpusError("Missing group/name")
            path = _safe_path(root, group, name)
            sid = source_id(group, name)
            if sid in seen:
                raise CorpusError("Repeated source identifier")
            seen.add(sid)
            expected_size = entry.get("size")
            digest = entry.get("sha256")
            if type(expected_size) is not int or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise CorpusError("Missing manifest size/digest")
            _verify_file(path, expected_size, digest)
            suffix = path.suffix.casefold()
            kind = SUPPORTED.get(suffix) or ("TEXT" if name.upper().startswith("LICENSE") else None)
            if not kind:
                raise CorpusError("Unsupported manifest file: " + name)
            url = entry.get("url", "")
            if not isinstance(url, str) or not url.startswith("https://raw.githubusercontent.com/"):
                raise CorpusError("Untrusted source URL")
            page_count = 0
            parse_status = "METADATA_ONLY"
            metadata = {}
            extracted_pages = []
            if kind == "PDF":
                parsed = _pdf_pages(path, parser_python)
                page_count = parsed.get("page_count", 0)
                if "error" in parsed:
                    parse_status = parsed["error"]
                    counts["pdf_parser_failures"] += 1
                else:
                    parse_status = "TEXT_EXTRACTED" if any(p["text"] for p in parsed["pages"]) else "NO_TEXT_LAYER"
                    # Insert pages after parent source row, for SQLite foreign keys.
                    extracted_pages = parsed["pages"]
            elif kind == "STEP":
                with path.open("rb") as file:
                    header = file.read(3000).decode("latin1", "replace")
                    file.seek(max(0, path.stat().st_size - 300))
                    tail = file.read().decode("latin1", "replace")
                if not header.lstrip().startswith("ISO-10303-21;") or "HEADER;" not in header or "DATA;" not in header or "END-ISO-10303-21;" not in tail:
                    raise CorpusError("Invalid STEP envelope: " + name)
                schema = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", header)
                metadata = {"step_schema": schema.group(1) if schema else "UNKNOWN",
                            "geometry_verified": False}
                parse_status = "STEP_HEADER_ONLY"
            elif kind == "CSV":
                parse_status = "CSV_ROWS_EXTRACTED"
            elif kind == "XLSX":
                parse_status = "XLSX_ROWS_EXTRACTED"
            elif kind == "IMAGE":
                parse_status = "IMAGE_NOT_OCR"
            elif kind == "TEXT":
                parse_status = "TEXT_METADATA_ONLY"
            relative = group + "/" + name
            db.execute("INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (sid, group, name, kind, relative, digest.lower(), expected_size,
                        url, parse_status, page_count, json.dumps(metadata, ensure_ascii=False)))
            counts["sources"] += 1
            for page in extracted_pages:
                db.execute("INSERT INTO source_page VALUES (?,?,?,?)",
                           (sid, page["page"], page["text"], page["status"]))
                counts["pdf_pages"] += 1
                if page["status"] == "NO_TEXT_LAYER":
                    counts["pdf_unreadable_pages"] += 1
            if kind in {"CSV", "XLSX"}:
                for row_no, name_, item_no, material, qty, unit, rev, row in _bom_lines(group, kind, path):
                    key = part_key(group, name_)
                    db.execute("INSERT INTO bom_line VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (sid, row_no, key, name_, item_no, material, qty,
                                unit, rev, json.dumps(row, ensure_ascii=False)))
                    db.execute("INSERT OR IGNORE INTO relation VALUES (?,?,?,?,?)",
                               (key, sid, "BOM", "BOM_ROW", "Строка " + str(row_no)))
                    counts["bom_lines"] += 1

        # Exact filename joins only, inside one project. Similar strings or numeric
        # prefixes do not count as verified relations.
        bom_parts = db.execute("SELECT DISTINCT part_key,part_name FROM bom_line").fetchall()
        for key, name in bom_parts:
            group = key.split(":", 1)[0]
            source_rows = db.execute(
                "SELECT id,name,kind FROM source WHERE source_group=? AND kind IN ('PDF','STEP')",
                (group,)).fetchall()
            for sid, original_name, kind in source_rows:
                if Path(original_name).stem != name:
                    continue
                db.execute("INSERT OR IGNORE INTO relation VALUES (?,?,?,?,?)",
                           (key, sid, "DRAWING" if kind == "PDF" else "MODEL",
                            "EXACT_FILENAME",
                            "Название детали в BOM и полное имя файла без расширения совпадают"))
                counts["relations"] += 1

        # Separate historical reference. It is NOT a notice for open-hardware
        # projects; its document isn't bundled because redistribution is unclear.
        refs = root / "VERIFIED_RELATIONS.json"
        if refs.is_file():
            historical = json.loads(refs.read_text(encoding="utf-8-sig")).get("historical_change_notice")
            if historical:
                db.execute("INSERT INTO reference_notice VALUES (?,?,?,?,?,?,?)",
                           (historical["notice"], historical["source_document"],
                            historical["URL"], historical["notice_page"],
                            historical["modification"], historical["date"],
                            "EXTERNAL_HISTORICAL_REFERENCE_NOT_PRODUCTION_APPROVAL"))
        db.execute("INSERT INTO dataset VALUES (1,?,?,?,?)",
                   (str(root), hashlib.sha256(manifest_bytes).hexdigest(), int(time.time()),
                    counts["sources"]))
        db.commit()
        integrity=db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CorpusError("SQLite index integrity failed")
        db.close()
        # Atomic publish; no partial index should be visible to readers.
        os.replace(temporary, index_path)
        return counts
    except BaseException:
        try:
            db.close()
        except (UnboundLocalError, sqlite3.Error):
            pass
        temporary.unlink(missing_ok=True)
        raise


def _open_readonly(index_path: Path):
    path = Path(index_path)
    if not path.is_file():
        raise CorpusError("CORPUS_NOT_IMPORTED")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return db


def summary(index_path: Path = DEFAULT_INDEX) -> dict:
    with closing(_open_readonly(index_path)) as db:
        group_rows = db.execute(
            "SELECT source_group, COUNT(*) n FROM source GROUP BY source_group ORDER BY source_group").fetchall()
        return {"sources": db.execute("SELECT COUNT(*) FROM source").fetchone()[0],
                "bom_lines": db.execute("SELECT COUNT(*) FROM bom_line").fetchone()[0],
                "links": db.execute("SELECT COUNT(*) FROM relation WHERE rule='EXACT_FILENAME'").fetchone()[0],
                "groups": [dict(x) for x in group_rows],
                "history_reference_count": db.execute("SELECT COUNT(*) FROM reference_notice").fetchone()[0],
                "revision_approval": "NOT_VERIFIED"}


def _search_names(query: str, candidate: str) -> bool:
    # No generic fuzzy matching, confusable conversion or substring-by-prefix.
    normalized = query.casefold()
    name = candidate.casefold()
    variants = {name}
    if "_" in name:
        variants.add(name.replace("_", " "))
    return any(re.search(r"(?<![\w])" + re.escape(v) + r"(?![\w])", normalized) for v in variants)


def answer(query: str, index_path: Path = DEFAULT_INDEX, group: str = "") -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        return {"state": "INVALID_QUERY", "message": "Введите обозначение или название детали."}
    with closing(_open_readonly(index_path)) as db:
        candidates = db.execute("SELECT DISTINCT part_key,part_name FROM bom_line").fetchall()
        matches = [r for r in candidates
                   if (not group or r["part_key"].split(":",1)[0] == group)
                   and _search_names(query.strip(), r["part_name"])]
        if not matches:
            return {"state": "NOT_FOUND", "message": "Точное совпадение детали в доступной спецификации не найдено. Это не доказывает отсутствие детали в документах."}
        if len(matches) != 1:
            return {"state": "AMBIGUOUS", "message": "Найдено несколько точных обозначений. Уточните изделие.",
                    "candidates": [{"part_key": r["part_key"],"name":r["part_name"]} for r in matches[:15]]}
        item = matches[0]
        key = item["part_key"]
        lines = db.execute(
            "SELECT b.*,s.name source_name,s.original_sha256 sha FROM bom_line b JOIN source s ON s.id=b.source_id WHERE b.part_key=? ORDER BY b.row_number",
            (key,)).fetchall()
        links = db.execute(
            "SELECT r.relation_type,r.rule,r.evidence,s.id,s.source_group,s.name,s.kind,s.original_sha256,s.original_url,s.parse_status FROM relation r JOIN source s ON s.id=r.source_id WHERE r.part_key=? ORDER BY r.relation_type,s.name",
            (key,)).fetchall()
        root = Path(db.execute("SELECT source_root FROM dataset").fetchone()[0])
        # Evidence was verified at import, but can be changed on disk afterwards.
        # Fail closed rather than return a stale "linked" engineering answer.
        for relation in links:
            source_path = _safe_path(root, relation["source_group"], relation["name"])
            with source_path.open("rb") as verified_stream:
                fingerprint = hashlib.file_digest(verified_stream, "sha256").hexdigest()
            if fingerprint != relation["original_sha256"]:
                raise CorpusError("SOURCE_CHANGED_AFTER_IMPORT")
        sources = [{"role":r["relation_type"],"id":r["id"],"name":r["name"],
                    "kind":r["kind"],"sha256":r["original_sha256"],
                    "url":r["original_url"],"parse_status":r["parse_status"],
                    "matching":r["rule"],"evidence":r["evidence"]} for r in links]
        complete = {"BOM","DRAWING","MODEL"}.issubset({s["role"] for s in sources})
        consistent = len({(r["material"],r["quantity"],r["revision"]) for r in lines}) == 1
        row = lines[0]
        status = ("BOM_DRAWING_STEP_LINKED" if complete else "DOCUMENTS_INCOMPLETE")
        if not consistent:
            status = "CONFLICTING_BOM_ROWS"
        return {
            "state": status,
            "part_key": key,
            "part": item["part_name"],
            "group": key.split(":",1)[0],
            "item_no": row["item_no"] if consistent else None,
            "material": row["material"] if consistent else None,
            "quantity": row["quantity"] if consistent else None,
            "unit": row["unit"] if consistent else None,
            "bom_row_number": row["row_number"],
            "bom_rows": len(lines),
            "sources": sources,
            "revision_status": "UNVERIFIED",
            "approval_status": "UNVERIFIED",
            "manufacturing_authorization": False,
            "message": ("Найдены спецификация, PDF-чертёж и STEP-модель по точным именам; их редакции и утверждение не подтверждены."
                        if complete else "Есть строка спецификации, но полный комплект PDF и STEP по точному имени не подтверждён.")
        }


def notice(notice_id: str, index_path: Path = DEFAULT_INDEX) -> dict:
    if not isinstance(notice_id, str) or len(notice_id) > 100:
        return {"state": "NOT_FOUND"}
    with closing(_open_readonly(index_path)) as db:
        row = db.execute("SELECT * FROM reference_notice WHERE notice_id=?",
                         (notice_id,)).fetchone()
        if row is None:
            return {"state":"NOT_FOUND"}
        return {"state":"HISTORICAL_REFERENCE_ONLY",
                "notice_id":row["notice_id"],"document_id":row["document_id"],
                "source_url":row["url"],"page":row["page"],
                "change_description":row["documented_change"],
                "effective_date_reference":row["effective_date_reference"],
                "evidence_status":row["evidence_status"],
                "manufacturing_authorization":False}


def original_file(source_id_: str, index_path: Path = DEFAULT_INDEX) -> tuple[bytes,str]:
    if not isinstance(source_id_, str) or not re.fullmatch(r"[0-9a-f]{24}", source_id_):
        raise CorpusError("SOURCE_NOT_FOUND")
    with closing(_open_readonly(index_path)) as db:
        row = db.execute("SELECT * FROM source WHERE id=?", (source_id_,)).fetchone()
        if not row:
            raise CorpusError("SOURCE_NOT_FOUND")
        root = Path(db.execute("SELECT source_root FROM dataset").fetchone()[0])
        path = _safe_path(root, row["source_group"], row["name"])
        data = path.read_bytes()
        if len(data) != row["original_size"] or hashlib.sha256(data).hexdigest() != row["original_sha256"]:
            raise CorpusError("SOURCE_CHANGED_AFTER_IMPORT")
        return data, path.suffix.casefold()


def main():
    parser = argparse.ArgumentParser(description="P7 engineering corpus index")
    parser.add_argument("command", choices=["import", "summary", "ask", "notice"])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--db", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--query", default="")
    parser.add_argument("--group", default="")
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    args = parser.parse_args()
    if args.command == "import":
        result = build_index(args.corpus, args.db, args.python)
    elif args.command == "summary":
        result = summary(args.db)
    elif args.command == "notice":
        result = notice(args.query,args.db)
    else:
        result = answer(args.query,args.db,args.group)
    print(json.dumps(result, ensure_ascii=True, indent=2))

if __name__ == "__main__":
    main()
