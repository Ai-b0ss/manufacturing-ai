"""Read-only commissioning and post-login diagnostics for the Windows pilot."""
import argparse
import json
from pathlib import Path
import time

from core06.db_client import database_session
from core06.readiness import check_dependencies
from tools import bringup_factory


_ALLOWED_REASONS = frozenset({"backend_unavailable", "job_failed", "unexpected_error"})
_ALLOWED_BACKENDS = frozenset({"postgres", "qdrant"})


def indexer_state(path, now=None):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - float(state["timestamp"])
        status = state["status"]
        if status not in {"running", "degraded", "stopped", "failed"} or not 0 <= age < float("inf"):
            raise ValueError("invalid heartbeat")
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unavailable"}
    is_stale = (status == "running" and age > 180) or (status == "degraded" and age > 300)
    result = {
        "state": "stale" if is_stale else status,
        "last_reported_state": status,
        "age_seconds": round(age, 1),
    }
    reason = state.get("reason")
    if isinstance(reason, str) and reason in _ALLOWED_REASONS:
        result["reason"] = reason
    error_count = state.get("error_count")
    if type(error_count) is int and error_count >= 0:
        result["error_count"] = error_count
    outage_checks = state.get("outage_checks")
    if type(outage_checks) is int and outage_checks >= 0:
        result["outage_checks"] = outage_checks
    missing = state.get("missing_backends")
    if isinstance(missing, list) and all(isinstance(x, str) and x in _ALLOWED_BACKENDS for x in missing):
        result["missing_backends"] = sorted(set(missing))
    return result


def queue_status():
    try:
        with database_session(admin=True) as db, db.transaction():
            db.execute("SET TRANSACTION READ ONLY")
            db.execute("SET LOCAL statement_timeout = '2s'")
            rows = db.execute("""
                SELECT status, count(*), count(*) FILTER (
                    WHERE attempts >= 5 AND (status = 'failed' OR
                        (status = 'processing' AND updated_at < now() - interval '10 minutes'))
                ) FROM core.ingest_outbox GROUP BY status
            """).fetchall()
        return {"counts": {status: count for status, count, _ in rows},
                "exhausted_jobs": sum(exhausted for _, _, exhausted in rows)}
    except Exception:
        return {"state": "unavailable"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, default=bringup_factory.MAIN)
    args = parser.parse_args()
    checks = check_dependencies()
    services = bringup_factory.snapshot()
    services["local_model_http"] = bringup_factory.http(
        "http://127.0.0.1:19086/health", lambda body: json.loads(body).get("status") == "ok",
    )
    indexer = indexer_state(args.deployment / "data" / "storage_index_worker_state.json")
    queue = queue_status()
    ready = (all(checks.values()) and services["unified"] and indexer["state"] == "running"
             and queue.get("exhausted_jobs") == 0)
    print(json.dumps({"ready": ready, "services": services, "dependencies": checks,
                      "indexer": indexer, "queue": queue}, indent=2))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
