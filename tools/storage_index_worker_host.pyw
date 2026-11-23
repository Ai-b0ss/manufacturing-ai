"""Silent, single-instance, stoppable background indexer for protected uploads.

Run via the virtual environment's pythonw.exe under the logged-in user's Scheduled Task.
No console window, no interactive prompts, no credentials in logs.
"""
from __future__ import annotations
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
LOGS=ROOT/"data"
LOGFILE=LOGS/"storage_index_worker.log"
STATE=LOGS/"storage_index_worker_state.json"
LOCKFILE=LOGS/"storage_index_worker.lock"
STOPFILE=LOGS/"storage_index_worker.stop"

WORKER_ENV = {
    "FACTORY_V06_INGEST_ENABLED":"1",
    "FACTORY_V06_INDEX_WORKER_ENABLED":"1",
    "HF_HUB_OFFLINE":"1",
    "TRANSFORMERS_OFFLINE":"1",
    "TOKENIZERS_PARALLELISM":"false",
    "OMP_NUM_THREADS":"2",
    "MKL_NUM_THREADS":"2",
}

def state(status,**kwargs):
    payload={"status":status,"pid":os.getpid(),"timestamp":time.time(),**kwargs}
    temp=STATE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload,ensure_ascii=True),encoding="utf8")
    os.replace(temp,STATE)

def main():
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
            return run_loop(process_one)
        except Exception as exc:
            logging.error("Fatal worker exit: %s", type(exc).__name__)
            state("failed")
            return 1
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)


def run_loop(process_one):
    consecutive_errors=0
    heartbeat = time.monotonic()
    last_result = None
    state("running")
    logging.info("Started protected storage indexer, pid=%s",os.getpid())
    while not STOPFILE.exists():
        try:
            result=process_one()
            if result.result=="empty":
                if consecutive_errors or time.monotonic() - heartbeat >= 30:
                    state("running", last_result=last_result)
                    heartbeat = time.monotonic()
                consecutive_errors=0
                time.sleep(5)
                continue
            logging.info("outcome=%s document=%s chunks=%s",
                         result.result,result.document_id,result.chunks)
            last_result = result.result
            heartbeat = time.monotonic()
            if result.result=="failed":
                consecutive_errors+=1
                state("degraded", last_result=last_result, error_count=consecutive_errors)
                time.sleep(min(60,5*consecutive_errors))
            else:
                state("running", last_result=last_result, last_chunks=result.chunks)
                consecutive_errors=0
        except Exception as exc:
            consecutive_errors+=1
            logging.warning("Worker iteration failed: %s", type(exc).__name__)
            state("degraded",last_result=last_result,error_count=consecutive_errors)
            time.sleep(min(60,5*consecutive_errors))
    logging.info("Stop marker seen: shutting down")
    state("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
