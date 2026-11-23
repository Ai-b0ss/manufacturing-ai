"""No production files touched: synthetic Chrome temporary-download fixtures."""
from __future__ import annotations
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4
import zipfile

from tools.disk_pressure_diagnostics import (
    GB, health, stale_crx, installed_browser_guard_versions,
    extension_metadata, inspect,
)


def sample_crx(label="Malwarebytes Browser Guard",version="3.3.10"):
    binary = BytesIO()
    with zipfile.ZipFile(binary,"w",compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json",json.dumps({
            "manifest_version":3,"name":label,"version":version
        }))
        archive.writestr("sample.js","// offline synthetic fixture")
    return b"Cr24" + (3).to_bytes(4,"little") + (0).to_bytes(4,"little") + binary.getvalue()


class TestDiskPressure(unittest.TestCase):
    def test_space_thresholds(self):
        self.assertEqual(health(0,100*GB),"critical")
        self.assertEqual(health(600*1024*1024,100*GB),"critical")
        self.assertEqual(health(2*GB,100*GB),"low")
        self.assertEqual(health(4*GB,80*GB),"low")
        self.assertEqual(health(20*GB,100*GB),"ok")
        self.assertEqual(health(1,0),"unavailable")

    def test_valid_browser_guard_extension_crx(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"test.crx"
            p.write_bytes(sample_crx())
            self.assertEqual(extension_metadata(p),("Malwarebytes Browser Guard","3.3.10"))

    def test_valid_avira_password_manager_extension(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"test.crx"
            p.write_bytes(sample_crx("Avira Password Manager","2.21.0.5052"))
            self.assertEqual(extension_metadata(p),("Avira Password Manager","2.21.0.5052"))

    def test_disallows_arbitrary_extension_names_from_report(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"test.crx"
            p.write_bytes(sample_crx("My Personal Extension with Username"))
            self.assertEqual(extension_metadata(p)[0],"other-or-unidentified-extension")

    def test_bad_archive_classified_without_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"incomplete.crx"
            p.write_bytes(b"Cr24broken")
            self.assertEqual(extension_metadata(p)[0],"incomplete-or-unreadable")

    def test_two_identical_old_downloads_are_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            data=sample_crx()
            now=time.time()
            for x in range(2):
                p=root/(str(uuid4())+".tmp")
                p.write_bytes(data)
                os.utime(p,(now-5000,now-5000))
            result=stale_crx(root,now=now,min_age_seconds=3600,min_size=1)
            self.assertEqual(result["status"],"passed")
            self.assertEqual(result["stale_crx_files"],2)
            self.assertEqual(len(result["duplicate_groups"]),1)
            self.assertEqual(result["duplicate_groups"][0]["copies"],2)
            self.assertTrue(result["files_unchanged"])
            self.assertEqual(sum(root.is_file() for root in root.iterdir()),2)

    def test_partial_download_is_not_counted_as_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            now=time.time()
            p=root/(str(uuid4())+".tmp")
            p.write_bytes(b"Cr24"+b"unfinished")
            os.utime(p,(now-8000,now-8000))
            result=stale_crx(root,now=now,min_age_seconds=3600,min_size=1)
            self.assertEqual(result["incomplete_archive_count"],1)
            self.assertEqual(result["duplicate_groups"],[])

    def test_recent_download_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            p=root/(str(uuid4())+".tmp")
            p.write_bytes(sample_crx())
            result=stale_crx(root,now=time.time(),min_age_seconds=3600,min_size=1)
            self.assertEqual(result["stale_crx_files"],0)

    def test_scan_limit_fails_as_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for i in range(3):
                (root/(str(uuid4())+".tmp")).write_bytes(sample_crx())
            result=stale_crx(root,cap=2,min_age_seconds=0,min_size=1)
            self.assertEqual(result["status"],"incomplete")
            self.assertEqual(result["reason"],"TEMP_ENTRY_SCAN_CAP_EXCEEDED")

    def test_avira_installation_version_from_synthetic_profile(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            p=root/"Profile 7"/"Extensions"/"caljgklbbfbcjjanaijlacgncafpegll"/"2.21.0.5015"
            p.mkdir(parents=True)
            (p/"manifest.json").write_text(json.dumps({
                "name":"Avira Password Manager","version":"2.21.0.5015"
            }),encoding="utf8")
            report=installed_browser_guard_versions(root)
            self.assertEqual(report["chrome_profile_count"],1)
            self.assertEqual(report["avira_version_folders"],{"2.21.0.5015":1})

    def test_browser_guard_versions_from_synthetic_profile(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            p=root/"Profile 3"/"Extensions"/"ihcjicgdanjaechkgeegckofjjedodee"/"3.3.10"
            p.mkdir(parents=True)
            (p/"manifest.json").write_text(json.dumps({
                "name":"Malwarebytes Browser Guard","version":"3.3.10"
            }),encoding="utf8")
            result=installed_browser_guard_versions(root)
            self.assertEqual(result["chrome_profile_count"],1)
            self.assertEqual(result["browser_guard_version_folders"],{"3.3.10":1})


if __name__=="__main__":
    unittest.main()
