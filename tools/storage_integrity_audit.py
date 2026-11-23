"""Non-mutating audit of stored PDF/STEP/BOM originals and a PostgreSQL dump.

No document names, source bytes, identities or credentials appear in output.
CAD integrity is not a manufacturing approval or geometrical certification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from core06.db_client import _creds_dir, _pg_port
from core06.private_cad_bundle import unpack_bundle, BundleInvalid

HASH = re.compile(r"^[0-9a-f]{64}$")
REQUIRED = ("TABLE DATA core document_blob ", "TABLE DATA core cad_kit ",
            "TABLE DATA core cad_kit_revision ", "TABLE DATA core cad_kit_request ")
REQUIRED_ACLS = ("ACL - SCHEMA core ", "ACL core TABLE document ")


def verify_cad(package, expected, report, part_key):
    issues = []
    if not isinstance(package, (bytes, memoryview)):
        return ("PACKAGE_TYPE_INVALID",)
    package = bytes(package)
    if not isinstance(expected, str) or not HASH.fullmatch(expected) or hashlib.sha256(package).hexdigest() != expected:
        issues.append("PACKAGE_SHA256_MISMATCH")
    try:
        files, part = unpack_bundle(package)
    except (ValueError, TypeError, BundleInvalid):
        return tuple(issues + ["PACKAGE_STRUCTURE_INVALID"])
    if not isinstance(report, dict):
        return tuple(issues + ["REPORT_NOT_OBJECT"])
    if report.get("schema") != "factory_private_cad_bundle_v1":
        issues.append("REPORT_SCHEMA_INVALID")
    if report.get("state") != "REVIEW_REQUIRED" or report.get("manufacturing_authorization") is not False:
        issues.append("REPORT_APPROVAL_INVALID")
    expected_part = (part or Path(files["model"].name).stem).strip()
    name = report.get("part")
    if not isinstance(name, str) or name != expected_part:
        issues.append("REPORT_PART_MISMATCH")
    if not isinstance(name, str) or not isinstance(part_key, str) or name.strip().lower() != part_key:
        issues.append("KIT_PART_KEY_MISMATCH")
    src = report.get("sources")
    if not isinstance(src, list) or len(src) != 3:
        return tuple(issues + ["REPORT_SOURCES_INVALID"])
    roles = [x.get("role") if isinstance(x, dict) else None for x in src]
    if sorted(map(str, roles)) != ["bom", "drawing", "model"]:
        return tuple(issues + ["REPORT_ROLES_INVALID"])
    for record in src:
        original = files[record["role"]]
        if record.get("name") != original.name:
            issues.append("SOURCE_FILENAME_MISMATCH")
        if type(record.get("size")) is not int or record["size"] != len(original.data):
            issues.append("SOURCE_SIZE_MISMATCH")
        if record.get("sha256") != original.sha256:
            issues.append("SOURCE_SHA256_MISMATCH")
    return tuple(issues)


def check_revisions(latest, records):
    errors, previous = [], {}
    for kit, number in records:
        if kit not in latest:
            errors.append("ORPHAN_REVISION")
            continue
        if type(number) is not int or number != previous.get(kit, 0) + 1:
            errors.append("REVISION_SEQUENCE_BROKEN")
        if type(number) is int:
            previous[kit] = number
    for kit, expected in latest.items():
        if type(expected) is not int or expected < 1 or expected != previous.get(kit, 0):
            errors.append("KIT_LATEST_REVISION_MISMATCH")
    return tuple(errors)


def hash_archive(path):
    digest = hashlib.sha256()
    length = 0
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            length += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), length


def verify_backup(receipt_path, pg_restore):
    result = {"status": "failed", "issues": [], "bytes": 0}
    try:
        receipt = json.loads(Path(receipt_path).read_text("utf-8-sig"))
        archive, expected, length = receipt["backup"], receipt["sha256"], receipt["dump_bytes"]
        if not isinstance(expected, str) or not HASH.fullmatch(expected) or type(length) is not int:
            raise ValueError("INVALID_RECEIPT")
        if not Path(archive).is_file() or not Path(pg_restore).is_file():
            raise FileNotFoundError("MISSING_TOOL_OR_ARCHIVE")
        actual, size = hash_archive(archive)
        result["bytes"] = size
        if (actual, size) != (expected, length):
            result["issues"].append("ARCHIVE_SHA256_MISMATCH")
            return result
        startup, flags = None, 0
        if sys.platform == "win32":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            flags = subprocess.CREATE_NO_WINDOW
        p = subprocess.run([str(pg_restore), "--list", str(archive)],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=45, startupinfo=startup, creationflags=flags)
        if p.returncode:
            result["issues"].append("PG_ARCHIVE_CATALOG_INVALID")
        else:
            listing = p.stdout.decode("utf-8", "replace")
            if any(name not in listing for name in REQUIRED):
                result["issues"].append("PG_ARCHIVE_REQUIRED_TABLES_MISSING")
            if any(acl not in listing for acl in REQUIRED_ACLS):
                result["issues"].append("PG_ARCHIVE_PRIVILEGES_MISSING")
            if not result["issues"]:
                result["status"] = "passed"
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        result["issues"].append("BACKUP_READ_FAILED")
    return result


def inspect_database(database):
    import psycopg
    result = {"status": "failed", "counts": {
        "documents_total": 0, "documents": 0, "cad_kits": 0,
        "cad_revisions": 0, "source_files": 0,
        "idempotency_requests": 0}, "issues": []}
    errors = Counter()
    try:
        password = (_creds_dir() / "admin-password.txt").read_text("ascii")
        # A session-level read-only policy forbids accidental mutations.
        with psycopg.connect(host="127.0.0.1", port=_pg_port(), dbname=database,
                user="factory_pg_admin", password=password, connect_timeout=8,
                options="-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=3000") as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if db.execute("SHOW transaction_read_only").fetchone()[0] != "on":
                raise RuntimeError("READ_ONLY_DISABLED")
            result["counts"]["documents_total"] = db.execute(
                "SELECT count(*) FROM core.document").fetchone()[0]
            for expected, actual in db.execute(
                    "SELECT content_sha256,encode(sha256(original_bytes),'hex') "
                    "FROM core.document_blob"):
                result["counts"]["documents"] += 1
                if expected != actual:
                    errors["DOCUMENT_SHA256_MISMATCH"] += 1
            kits = {}
            for kit, part, key, latest in db.execute(
                    "SELECT id,part,part_key,latest_revision FROM core.cad_kit"):
                kits[kit] = latest
                result["counts"]["cad_kits"] += 1
                if part.strip().lower() != key:
                    errors["KIT_PART_KEY_INVALID"] += 1
            revisions, ids = [], []
            with db.cursor(name="factory_integrity_readonly_revisions") as cursor:
                cursor.execute("SELECT r.id,r.kit_id,r.revision_no,r.package,"
                               "r.package_sha256,r.report,k.part_key "
                               "FROM core.cad_kit_revision r JOIN core.cad_kit k "
                               "ON k.id=r.kit_id ORDER BY r.kit_id,r.revision_no")
                while batch := cursor.fetchmany(1):
                    for identifier, kit, number, blob, sha, report, part_key in batch:
                        ids.append(identifier)
                        revisions.append((kit, number))
                        result["counts"]["cad_revisions"] += 1
                        problems = verify_cad(blob, sha, report, part_key)
                        errors.update(problems)
                        if not problems:
                            result["counts"]["source_files"] += 3
            errors.update(check_revisions(kits, revisions))
            requests = {identifier: count for identifier, count in db.execute(
                "SELECT revision_id,count(*) FROM core.cad_kit_request GROUP BY revision_id")}
            result["counts"]["idempotency_requests"] = sum(requests.values())
            for identifier in ids:
                if requests.get(identifier) != 1:
                    errors["REVISION_REQUEST_MISSING_OR_DUPLICATE"] += 1
            if len(requests) != len(ids):
                errors["REQUEST_COVERAGE_MISMATCH"] += 1
    except Exception as exc:
        errors["READONLY_CHECK_FAILED_" + type(exc).__name__.upper()] += 1
    result["issues"] = [{"code":k, "count":v} for k, v in sorted(errors.items())]
    result["status"] = "passed" if not errors else "failed"
    return result


def compare_snapshot_counts(receipt, current):
    """Counts detect definite backup drift, never claim equal content."""
    mapping = {
        "document": "documents_total",
        "document_blob": "documents",
        "cad_kit": "cad_kits",
        "cad_kit_revision": "cad_revisions",
        "cad_kit_request": "idempotency_requests",
    }
    archived = receipt.get("counts") if isinstance(receipt, dict) else None
    current = current.get("counts") if isinstance(current, dict) else None
    if not isinstance(archived, dict) or not isinstance(current, dict):
        return {"status": "unknown", "reason": "COUNTS_NOT_AVAILABLE"}
    if any(type(archived.get(k)) is not int or type(current.get(v)) is not int
           for k, v in mapping.items()):
        return {"status": "unknown", "reason": "COUNTS_INCOMPLETE"}
    changed = [key for key, mapped in mapping.items()
               if archived[key] != current[mapped]]
    return {"status": "failed" if changed else "passed",
            "tables_checked": len(mapping), "changed_tables": changed,
            "meaning": "ROW_COUNTS_ONLY_NOT_CONTENT_IDENTITY"}


def main(argv=None):
    cli = argparse.ArgumentParser(description="Read-only storage verification.")
    cli.add_argument("--check-database", action="store_true")
    cli.add_argument("--database", default="factory_core_v06")
    cli.add_argument("--allow-live-readonly", action="store_true")
    cli.add_argument("--backup-receipt", type=Path)
    cli.add_argument("--pg-restore", type=Path)
    cli.add_argument("--json-out", type=Path)
    args = cli.parse_args(argv)
    if not args.check_database and not args.backup_receipt:
        cli.error("Choose --check-database and/or --backup-receipt.")
    if args.check_database and args.database == "factory_core_v06" and not args.allow_live_readonly:
        cli.error("Explicit --allow-live-readonly is required for the live DB.")
    if args.check_database and not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", args.database):
        cli.error("Invalid database name.")
    report = {"schema": "storage_integrity_readonly_v1", "checks": {}}
    if args.check_database:
        report["checks"]["database"] = inspect_database(args.database)
    if args.backup_receipt:
        restore = args.pg_restore
        if restore is None:
            from tools.provision_core import BIN
            restore = BIN / "pg_restore.exe"
        report["checks"]["backup"] = verify_backup(args.backup_receipt, restore)
        if args.check_database and report["checks"]["database"]["status"] == "passed":
            try:
                old_counts = json.loads(args.backup_receipt.read_text("utf-8-sig"))
                report["checks"]["snapshot_counts"] = compare_snapshot_counts(
                    old_counts, report["checks"]["database"])
            except (OSError, UnicodeError, ValueError):
                report["checks"]["snapshot_counts"] = {
                    "status": "unknown", "reason": "RECEIPT_COUNTS_UNAVAILABLE"}
    report["status"] = ("passed" if all(v["status"] == "passed" for v in report["checks"].values()) else
                        "incomplete" if any(v["status"] == "unknown" for v in report["checks"].values()) else "failed")
    result = json.dumps(report, ensure_ascii=True, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(result, encoding="utf-8")
    print(result)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
