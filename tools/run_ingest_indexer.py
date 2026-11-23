"""Opt-in standalone worker for the PostgreSQL ingestion outbox.

Never started automatically. Requires BOTH independent enable flags.
  FACTORY_V06_INGEST_ENABLED=1
  FACTORY_V06_INDEX_WORKER_ENABLED=1
Use --once or --loop, with --max-jobs to bound work. No daemon installer.
"""
from __future__ import annotations
import argparse
import os
import time

from core06.ingest_index_worker import process_one


def main(argv=None):
    parser = argparse.ArgumentParser(description="Factory v0.6 outbox worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Process at most one job")
    mode.add_argument("--loop", action="store_true", help="Poll for jobs until stopped")
    parser.add_argument("--max-jobs", type=int, default=100, help="Max jobs in loop (1..10000)")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="Idle delay (1..60)")
    options = parser.parse_args(argv)
    if os.environ.get("FACTORY_V06_INGEST_ENABLED") != "1" or (
        os.environ.get("FACTORY_V06_INDEX_WORKER_ENABLED") != "1"
    ):
        parser.error("Index worker is disabled; both feature flags must be set explicitly")
    if not 1 <= options.max_jobs <= 10000 or not 1 <= options.poll_seconds <= 60:
        parser.error("max-jobs or poll-seconds is outside permitted bounds")
    processed = 0
    failed = 0
    try:
        while processed < (1 if options.once else options.max_jobs):
            if os.environ.get("FACTORY_V06_INDEX_WORKER_ENABLED") != "1":
                break
            outcome = process_one()
            if outcome.result == "empty":
                if options.once:
                    break
                time.sleep(options.poll_seconds)
                continue
            processed += 1
            failed += outcome.result == "failed"
            print(
                f"job={outcome.document_id} result={outcome.result} chunks={outcome.chunks}",
                flush=True,
            )
            if options.once:
                break
    except KeyboardInterrupt:
        print("worker stopped by operator", flush=True)
    print(f"summary jobs={processed} failed={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
