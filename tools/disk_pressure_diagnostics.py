"""Read-only Windows disk pressure and stale Chrome CRX3 download diagnosis.

It never deletes, moves, truncates, or installs anything. The report contains
aggregate counts only: no browser history, document paths, tokens, or user IDs.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import zipfile

GUID_TMP = re.compile(
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.tmp\Z", re.ASCII
)
MB = 1024 * 1024
GB = 1024 ** 3
BROWSER_GUARD_ID = "ihcjicgdanjaechkgeegckofjjedodee"
AVIRA_ID = "caljgklbbfbcjjanaijlacgncafpegll"
PUBLIC_EXTENSION_NAMES = {
    "malwarebytes browser guard": "Malwarebytes Browser Guard",
    "avira password manager": "Avira Password Manager",
}


def health(free: int, total: int) -> str:
    if total <= 0:
        return "unavailable"
    if free < GB:
        return "critical"
    if free < 5 * GB or free / total < 0.05:
        return "low"
    return "ok"


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(MB), b""):
            digest.update(block)
    return digest.hexdigest()


def extension_metadata(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            name = "manifest.json"
            info = archive.getinfo(name)
            if info.file_size > 256 * 1024:
                return "unknown", "unknown"
            manifest = json.loads(archive.read(info).decode("utf-8-sig"))
        if not isinstance(manifest, dict):
            return "unknown", "unknown"
        label = manifest.get("name")
        version = manifest.get("version")
        if not isinstance(label, str) or not 1 <= len(label) <= 90:
            label = "unknown"
        if not isinstance(version, str) or not re.fullmatch(r"[0-9][0-9.]{0,30}", version):
            version = "unknown"
        # Only identify the documented public extension; avoid exposing
        # arbitrary extension metadata or locally installed private names.
        label = PUBLIC_EXTENSION_NAMES.get(
            label.casefold(), "other-or-unidentified-extension"
        )
        return label, version
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError, ValueError):
        return "incomplete-or-unreadable", "unknown"


def stale_crx(temp_dir: Path, *, now=None, min_age_seconds=3600,
              min_size=1024 * 1024, max_size=80 * MB, cap=1200) -> dict:
    now = time.time() if now is None else now
    if not temp_dir.is_dir():
        return {"status": "unavailable", "reason": "TEMP_DIR_MISSING"}
    paths = sorted(temp_dir.glob("*.tmp"), key=lambda x: x.name)
    if len(paths) > cap:
        return {"status": "incomplete", "reason": "TEMP_ENTRY_SCAN_CAP_EXCEEDED",
                "matching_tmp_files": len(paths)}
    found = []
    for path in paths:
        if not GUID_TMP.fullmatch(path.name):
            continue
        try:
            info = path.stat()
            if (info.st_size < min_size or info.st_size > max_size
                    or now - info.st_mtime < min_age_seconds):
                continue
            with path.open("rb") as stream:
                if stream.read(4) != b"Cr24":
                    continue
            digest = hash_file(path)
            name, version = extension_metadata(path)
            found.append((digest, info.st_size, name, version))
        except (OSError,PermissionError):
            continue
    groups = defaultdict(list)
    for digest, size, name, version in found:
        groups[(digest, name, version)].append(size)
    duplicates = []
    for (digest, name, version), sizes in groups.items():
        if len(sizes) >= 2:
            duplicates.append({
                "identity": name, "version": version,
                "copies": len(sizes),
                "duplicate_bytes": sum(sizes),
                "sha256_prefix": digest[:12],
            })
    duplicates.sort(key=lambda item: -item["duplicate_bytes"])
    names = Counter(name for _, _, name, _ in found)
    return {
        "status": "passed",
        "stale_crx_files": len(found),
        "stale_crx_bytes": sum(size for _, size, _, _ in found),
        "incomplete_archive_count": names["incomplete-or-unreadable"],
        "duplicate_groups": duplicates,
        "files_unchanged": True,
    }


def installed_browser_guard_versions(user_data: Path) -> dict:
    profiles = []
    if user_data.is_dir():
        profiles = [
            p for p in user_data.iterdir()
            if p.is_dir() and (p.name == "Default" or re.fullmatch(r"Profile [0-9]+", p.name))
        ]
    kinds = (
        ("browser_guard_version_folders", BROWSER_GUARD_ID, "malwarebytes browser guard"),
        ("avira_version_folders", AVIRA_ID, "avira password manager"),
    )
    result = {"chrome_profile_count": len(profiles)}
    for output_key, extension_id, expected_name in kinds:
        counts = Counter()
        for profile in profiles[:60]:
            ext = profile / "Extensions" / extension_id
            if not ext.is_dir():
                continue
            for version_dir in ext.iterdir():
                manifest = version_dir / "manifest.json"
                if not manifest.is_file():
                    continue
                try:
                    data = json.loads(manifest.read_text("utf-8-sig"))
                    if str(data.get("name", "")).casefold() == expected_name:
                        version = str(data.get("version", "unknown"))
                        counts[version] += 1
                except (OSError, UnicodeError, ValueError):
                    continue
        result[output_key] = dict(sorted(counts.items()))
    return result


def inspect(disk: str, temp_dir: Path, chrome_data: Path, *, age_hours=2) -> dict:
    try:
        usage = shutil.disk_usage(disk)
        available = {
            "status": health(usage.free, usage.total),
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "free_percent": round(100 * usage.free / usage.total, 2),
        }
    except OSError:
        available = {"status": "unavailable"}
    return {
        "schema": "factory_disk_pressure_readonly_v1",
        "disk": available,
        "chrome_temp": stale_crx(temp_dir, min_age_seconds=age_hours*3600),
        "browser_guard": installed_browser_guard_versions(chrome_data),
        "mutated": False,
        "explanation": "Repeated downloads are observed; the responsible process is not identified.",
    }


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--disk", default="C:\\")
    cli.add_argument("--temp-dir", type=Path, default=Path(os.environ.get("TEMP", "C:\\Windows\\Temp")))
    cli.add_argument("--chrome-data", type=Path, default=Path(os.environ.get(
        "LOCALAPPDATA", "C:\\Users\\Public")) / "Google" / "Chrome" / "User Data")
    cli.add_argument("--min-age-hours", type=int, default=2)
    cli.add_argument("--json-out", type=Path)
    args = cli.parse_args(argv)
    if not 1 <= args.min_age_hours <= 720:
        cli.error("--min-age-hours outside permitted range.")
    result = inspect(args.disk, args.temp_dir, args.chrome_data, age_hours=args.min_age_hours)
    output = json.dumps(result, ensure_ascii=True, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(output, encoding="utf-8")
    print(output)
    return 2 if result["disk"]["status"] == "critical" else 0


if __name__ == "__main__":
    sys.exit(main())
