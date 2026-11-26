"""Offline stdlib regression tests for storage_index_worker error-loop and chunking fixes."""
from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core06.ingest_service import (
    MAX_CHUNK_CHARS,
    MAX_CHUNKS,
    MAX_TEXT,
    UploadInputError,
    _split_page_text,
    prepare_upload,
)

ROOT = Path(__file__).resolve().parent


def _load_worker_host():
    script = ROOT / "tools" / "storage_index_worker_host.pyw"
    loader = SourceFileLoader("storage_worker_host_regression", str(script))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class _FakeOutcome:
    def __init__(self, document_id, result, chunks=0):
        self.document_id = document_id
        self.result = result
        self.chunks = chunks


class ConnectionTimeout(Exception):
    """Mimics psycopg.errors.ConnectionTimeout without requiring psycopg in CI."""


class StorageWorkerHostRegressionTests(unittest.TestCase):
    def test_01_sustained_backend_timeout_bounds_error_count_and_deduplicates_logs(self):
        host = _load_worker_host()
        polls = iter([False] * 20 + [True])
        stop = SimpleNamespace(exists=lambda: next(polls))
        states = []
        sleeps = []

        def failing_process():
            raise ConnectionTimeout("password=super-secret-value host=127.0.0.1")

        with (
            patch.object(host, "STOPFILE", stop),
            patch.object(host, "state", side_effect=lambda s, **kw: states.append((s, kw))),
            patch.object(host.time, "sleep", side_effect=lambda sec: sleeps.append(sec)),
            self.assertLogs(level="WARNING") as logs,
        ):
            rc = host.run_loop(failing_process)

        self.assertEqual(rc, 0)
        degraded = [kw for s, kw in states if s == "degraded"]
        self.assertEqual(len(degraded), 20)
        # Error count must stay bounded (1) rather than climbing 1..20..1018 forever.
        self.assertEqual([d["error_count"] for d in degraded], [1] * 20)
        self.assertEqual(degraded[-1]["reason"], "backend_unavailable")
        self.assertEqual(degraded[-1]["error_type"], "ConnectionTimeout")
        self.assertEqual(degraded[-1]["outage_checks"], 20)
        self.assertEqual(degraded[-1]["suppressed_logs"], 19)
        # Only 1 warning emitted across 20 identical offline checks; zero secret leaks.
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("super-secret-value", " ".join(logs.output))
        # Exponential backoff: 5, 10, 20, 40, 80, 120, 120...
        self.assertEqual(sleeps[:6], [5.0, 10.0, 20.0, 40.0, 80.0, 120.0])
        self.assertEqual(max(sleeps), 120.0)

    def test_02_backends_ready_precheck_skips_process_one_when_services_stopped_and_recovers(self):
        host = _load_worker_host()
        polls = iter([False, False, False, False, True])
        stop = SimpleNamespace(exists=lambda: next(polls))
        probes = iter([
            {"postgres": False, "qdrant": False},
            {"postgres": False, "qdrant": False},
            {"postgres": True, "qdrant": True},
            {"postgres": True, "qdrant": True},
        ])
        outcomes = iter([
            _FakeOutcome("upload-" + "a" * 32, "indexed", 2),
            _FakeOutcome(None, "empty", 0),
        ])
        process_calls = 0

        def process():
            nonlocal process_calls
            process_calls += 1
            return next(outcomes)

        states = []
        with (
            patch.object(host, "STOPFILE", stop),
            patch.object(host, "state", side_effect=lambda s, **kw: states.append((s, kw))),
            patch.object(host.time, "sleep"),
            self.assertLogs(level="INFO") as logs,
        ):
            rc = host.run_loop(process, backends_ready=lambda: next(probes))

        self.assertEqual(rc, 0)
        # process_one was NEVER invoked while PG/Qdrant were offline.
        self.assertEqual(process_calls, 2)
        self.assertEqual([s for s, _ in states], ["running", "degraded", "degraded", "running", "stopped"])
        degraded = [kw for s, kw in states if s == "degraded"]
        self.assertEqual(degraded[0]["missing_backends"], ["postgres", "qdrant"])
        self.assertEqual(degraded[1]["error_count"], 1)
        self.assertEqual(degraded[1]["outage_checks"], 2)
        self.assertEqual(degraded[1]["suppressed_logs"], 1)
        recovered = [kw for s, kw in states if s == "running"][-1]
        self.assertEqual(recovered["last_result"], "indexed")
        self.assertEqual(recovered["last_chunks"], 2)
        self.assertEqual(recovered["error_count"], 0)
        warn_lines = [line for line in logs.output if "WARNING:" in line]
        self.assertEqual(len(warn_lines), 1)

    def test_03_periodic_outage_reminder_and_signature_change_logging(self):
        host = _load_worker_host()
        polls = iter([False, False, False, False, True])
        stop = SimpleNamespace(exists=lambda: next(polls))
        probes = iter([
            {"postgres": False, "qdrant": False},
            {"postgres": False, "qdrant": False},
            {"postgres": False, "qdrant": False},
            {"postgres": True, "qdrant": False},
        ])
        clock = iter([100.0, 100.0, 110.0, 2000.0, 2010.0])

        with (
            patch.object(host, "STOPFILE", stop),
            patch.object(host, "state"),
            patch.object(host.time, "sleep"),
            patch.object(host.time, "monotonic", side_effect=lambda: next(clock)),
            self.assertLogs(level="WARNING") as logs,
        ):
            rc = host.run_loop(
                lambda: self.fail("process_one should not run while backends are offline"),
                backends_ready=lambda: next(probes),
                outage_log_interval=1800.0,
            )

        self.assertEqual(rc, 0)
        # 1 initial warning + 1 periodic summary after 1900s + 1 signature-change warning (qdrant-only)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("suppressed=2", logs.output[1])
        self.assertIn("missing=qdrant", logs.output[2])

    def test_04_job_failures_and_unexpected_errors_separated_from_backend_outage(self):
        host = _load_worker_host()
        polls = iter([False, False, False, True])
        stop = SimpleNamespace(exists=lambda: next(polls))
        outcomes = iter([
            _FakeOutcome("upload-" + "1" * 32, "failed", 0),
            ValueError("sensitive-payload-must-not-log"),
            _FakeOutcome(None, "empty", 0),
        ])

        def process():
            item = next(outcomes)
            if isinstance(item, Exception):
                raise item
            return item

        states = []
        with (
            patch.object(host, "STOPFILE", stop),
            patch.object(host, "state", side_effect=lambda s, **kw: states.append((s, kw))),
            patch.object(host.time, "sleep"),
            self.assertLogs(level="WARNING") as logs,
        ):
            rc = host.run_loop(process)

        self.assertEqual(rc, 0)
        degraded = [kw for s, kw in states if s == "degraded"]
        self.assertEqual(degraded[0]["reason"], "job_failed")
        self.assertEqual(degraded[0]["error_count"], 1)
        self.assertEqual(degraded[1]["reason"], "unexpected_error")
        self.assertEqual(degraded[1]["error_count"], 2)
        self.assertEqual(degraded[1]["error_type"], "ValueError")
        self.assertNotIn("sensitive-payload-must-not-log", " ".join(logs.output))
        # Recovers cleanly when queue becomes empty.
        self.assertEqual([s for s, _ in states], ["running", "degraded", "degraded", "running", "stopped"])

    def test_05_interruptible_sleep_exits_promptly_on_stopfile(self):
        host = _load_worker_host()
        with TemporaryDirectory() as td:
            stopfile = Path(td) / "worker.stop"
            with patch.object(host, "STOPFILE", stopfile):
                def trigger_stop():
                    time.sleep(0.12)
                    stopfile.write_text("stop", encoding="ascii")

                t = threading.Thread(target=trigger_stop, daemon=True)
                t.start()
                started = time.monotonic()
                host._sleep_interruptible(20.0, stop_poll_seconds=0.05)
                elapsed = time.monotonic() - started
                t.join(timeout=2)
        self.assertLess(elapsed, 2.0)


class IngestChunkingRegressionTests(unittest.TestCase):
    def test_06_oversized_single_page_txt_splits_within_worker_limit_preserving_page_and_tail(self):
        tail_fact = "Клапан КП-440М давление срабатывания 18,5 МПа температура 115 C."
        body = ("Регламент обслуживания узлов насосной станции Н-125.\n" * 450) + tail_fact
        self.assertGreater(len(body), 15_000)
        self.assertLess(len(body), MAX_TEXT)

        prepared = prepare_upload(
            "oversized_protocol.txt",
            "Протокол клапана КП-440М",
            body.encode("utf-8"),
            extractor=lambda _p: {"pages": [{"page": 1, "text": body, "extraction_method": "text"}], "note": ""},
        )
        self.assertGreaterEqual(len(prepared.chunks), 2)
        self.assertLessEqual(len(prepared.chunks), MAX_CHUNKS)
        for ch in prepared.chunks:
            self.assertEqual(ch["page"], 1)
            self.assertEqual(ch["extraction_method"], "text")
            self.assertTrue(1 <= len(ch["text"]) <= MAX_CHUNK_CHARS)
        self.assertIn(tail_fact, " ".join(ch["text"] for ch in prepared.chunks))

    def test_07_short_page_not_split_and_continuous_token_hard_splits_safely(self):
        self.assertEqual(_split_page_text("Краткий текст Н-125 25 бар"), ["Краткий текст Н-125 25 бар"])
        solid = "A" * (MAX_CHUNK_CHARS * 2 + 500)
        parts = _split_page_text(solid)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(1 <= len(p) <= MAX_CHUNK_CHARS for p in parts))
        self.assertEqual("".join(parts), solid)

    def test_08_negative_oversized_total_text_and_excess_pages_rejected(self):
        huge = "X" * (MAX_TEXT + 1)
        with self.assertRaisesRegex(UploadInputError, "EXTRACTED_TEXT_TOO_LARGE"):
            prepare_upload(
                "too_large.txt",
                "Too large",
                b"placeholder",
                extractor=lambda _p: {"pages": [{"page": 1, "text": huge, "extraction_method": "text"}], "note": ""},
            )
        many_pages = [{"page": i + 1, "text": "page text", "extraction_method": "text"} for i in range(MAX_CHUNKS + 1)]
        with self.assertRaisesRegex(UploadInputError, "INVALID_EXTRACTED_PAGES"):
            prepare_upload(
                "too_many_pages.txt",
                "Too many pages",
                b"placeholder",
                extractor=lambda _p: {"pages": many_pages, "note": ""},
            )


if __name__ == "__main__":
    unittest.main()
