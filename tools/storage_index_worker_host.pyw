"""Silent, single-instance, stoppable background indexer for protected uploads.

Run via the virtual environment's pythonw.exe under the logged-in user's Scheduled Task.
No console window, no interactive prompts, no credentials in logs.
"""
from __future__ import annotations
import json
import logging
from logging.handlers import RotatingFileHandler
try:
    import msvcrt
except ImportError:
    msvcrt = None
import os
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
LOGS=ROOT/"data"
LOGFILE=LOGS/"storage_index_worker.log"
STATE=LOGS/"storage_index_worker_state.json"
LOCKFILE=LOGS/"storage_index_worker.lock"
STOPFILE=LOGS/"storage_index_worker.stop"
_REAL_SLEEP = time.sleep

_OUTAGE_CLASS_NAMES = frozenset({
    "ConnectionTimeout",
    "OperationalError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "ResponseHandlingException",
    "NetworkError",
    "TimeoutException",
})

WORKER_ENV = {
    "FACTORY_V06_INGEST_ENABLED":"1",
    "FACTORY_V06_INDEX_WORKER_ENABLED":"1",
    "HF_HUB_OFFLINE":"1",
    "TRANSFORMERS_OFFLINE":"1",
    "TOKENIZERS_PARALLELISM":"false",
    "OMP_NUM_THREADS":"2",
    "MKL_NUM_THREADS":"2",
}


def _tcp_reachable(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False


def default_backends_ready(timeout: float = 0.4) -> dict[str, bool]:
    """Fast loopback TCP pre-check without reading credentials or spending retries."""
    from core06 import db_client, hybrid_retrieval

    parsed = urlparse(hybrid_retrieval.URL)
    q_host = parsed.hostname or "127.0.0.1"
    q_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return {
        "postgres": _tcp_reachable(db_client.HOST, db_client._pg_port(), timeout=timeout),
        "qdrant": _tcp_reachable(q_host, q_port, timeout=timeout),
    }


def _is_backend_outage(exc: BaseException) -> bool:
    """Classify transient DB/Qdrant connectivity outages without inspecting message text."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError, OSError)):
            return True
        name = type(current).__name__
        if name in _OUTAGE_CLASS_NAMES:
            return True
        if name == "UnexpectedResponse":
            status_code = getattr(current, "status_code", 0)
            if isinstance(status_code, int) and status_code >= 500:
                return True
        current = current.__cause__ or current.__context__
    return False


def _sleep_interruptible(seconds: float, stop_poll_seconds: float = 1.0) -> None:
    if seconds <= 0:
        return
    if time.sleep is not _REAL_SLEEP or seconds <= stop_poll_seconds:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or STOPFILE.exists():
            break
        time.sleep(min(stop_poll_seconds, remaining))


def state(status,**kwargs):
    payload={"status":status,"pid":os.getpid(),"timestamp":time.time(),**kwargs}
    temp=STATE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload,ensure_ascii=True),encoding="utf8")
    os.replace(temp,STATE)

def main():
    if msvcrt is None:
        raise RuntimeError("WINDOWS_LOCKING_REQUIRED")
    LOGS.mkdir(exist_ok=True)
    with LOCKFILE.open("a+b") as lock:
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            return 0
        try:
            sys.path.insert(0,str(ROOT))
            os.environ.update(WORKER_ENV)
            logging.basicConfig(level=logging.INFO,handlers=[
                RotatingFileHandler(LOGFILE,maxBytes=1_500_000,backupCount=2,encoding="utf8"),
            ],format="%(asctime)s %(levelname)s %(message)s")
            from core06.ingest_index_worker import process_one
            return run_loop(process_one, backends_ready=default_backends_ready)
        except Exception as exc:
            logging.error("Fatal worker exit: %s", type(exc).__name__)
            state("failed")
            return 1
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)


def run_loop(
    process_one,
    *,
    backends_ready=None,
    outage_base_seconds: float = 5.0,
    outage_max_seconds: float = 120.0,
    outage_log_interval: float = 1800.0,
    stop_poll_seconds: float = 1.0,
):
    job_errors=0
    outage_active=False
    outage_checks=0
    outage_signature=None
    suppressed_logs=0
    last_outage_log=0.0
    heartbeat = time.monotonic()
    last_result = None
    state("running")
    logging.info("Started protected storage indexer, pid=%s",os.getpid())

    def record_outage(error_type: str, missing: list[str] | None = None) -> None:
        nonlocal outage_active, outage_checks, outage_signature, suppressed_logs, last_outage_log
        now = time.monotonic()
        missing_list = sorted(missing) if missing else []
        sig = (error_type, tuple(missing_list))
        outage_checks += 1
        delay = min(outage_max_seconds, outage_base_seconds * (2 ** min(outage_checks - 1, 6)))
        if not outage_active or sig != outage_signature:
            outage_active = True
            outage_signature = sig
            suppressed_logs = 0
            last_outage_log = now
            if missing_list:
                logging.warning(
                    "Backend unavailable (missing=%s); entering bounded backoff %.0fs",
                    ",".join(missing_list),
                    delay,
                )
            else:
                logging.warning(
                    "Worker backend unavailable: %s; entering bounded backoff %.0fs",
                    error_type,
                    delay,
                )
        else:
            suppressed_logs += 1
            if now - last_outage_log >= outage_log_interval:
                logging.warning(
                    "Backend still unavailable (%s); suppressed=%d outage_checks=%d",
                    error_type,
                    suppressed_logs,
                    outage_checks,
                )
                suppressed_logs = 0
                last_outage_log = now
        extra = {
            "reason": "backend_unavailable",
            "error_type": error_type,
            "outage_checks": outage_checks,
            "suppressed_logs": suppressed_logs,
            "next_retry_seconds": int(delay),
        }
        if missing_list:
            extra["missing_backends"] = missing_list
        # Keep error_count bounded during an ongoing backend outage instead of
        # accumulating unbounded counters while local services are stopped.
        state("degraded", last_result=last_result, error_count=max(1, job_errors), **extra)
        _sleep_interruptible(delay, stop_poll_seconds)

    def clear_degraded() -> None:
        nonlocal outage_active, outage_checks, outage_signature, suppressed_logs, job_errors
        outage_active = False
        outage_checks = 0
        outage_signature = None
        suppressed_logs = 0
        job_errors = 0

    while not STOPFILE.exists():
        if backends_ready is not None:
            try:
                probe = backends_ready()
            except Exception as exc:
                record_outage(type(exc).__name__)
                continue
            missing = sorted(name for name, ok in probe.items() if not ok)
            if missing:
                record_outage("BackendOffline", missing=missing)
                continue
        try:
            result=process_one()
            if result.result=="empty":
                if outage_active or job_errors:
                    logging.info(
                        "Worker connectivity restored after outage_checks=%d job_errors=%d",
                        outage_checks,
                        job_errors,
                    )
                    state("running", last_result=last_result, error_count=0)
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 30:
                    state("running", last_result=last_result, error_count=0)
                    heartbeat = time.monotonic()
                clear_degraded()
                _sleep_interruptible(5.0, stop_poll_seconds)
                continue
            last_result = result.result
            heartbeat = time.monotonic()
            if result.result=="failed":
                outage_active = False
                outage_checks = 0
                outage_signature = None
                suppressed_logs = 0
                job_errors += 1
                logging.warning(
                    "outcome=%s document=%s chunks=%s job_errors=%d",
                    result.result,
                    result.document_id,
                    result.chunks,
                    job_errors,
                )
                state("degraded", reason="job_failed", last_result=last_result, error_count=job_errors)
                _sleep_interruptible(min(60, 5 * job_errors), stop_poll_seconds)
            else:
                if outage_active or job_errors:
                    logging.info(
                        "Worker recovered after outage_checks=%d job_errors=%d",
                        outage_checks,
                        job_errors,
                    )
                logging.info("outcome=%s document=%s chunks=%s",
                             result.result,result.document_id,result.chunks)
                state("running", last_result=last_result, last_chunks=result.chunks, error_count=0)
                clear_degraded()
        except Exception as exc:
            if _is_backend_outage(exc):
                record_outage(type(exc).__name__)
            else:
                outage_active = False
                outage_checks = 0
                outage_signature = None
                suppressed_logs = 0
                job_errors += 1
                logging.warning("Worker iteration failed: %s", type(exc).__name__)
                state(
                    "degraded",
                    reason="unexpected_error",
                    error_type=type(exc).__name__,
                    last_result=last_result,
                    error_count=job_errors,
                )
                _sleep_interruptible(min(60, 5 * job_errors), stop_poll_seconds)
    logging.info("Stop marker seen: shutting down")
    state("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
