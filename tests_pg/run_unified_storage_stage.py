"""Ephemeral real-Chrome staging acceptance (never writes live v0.6).

Creates one unique PostgreSQL DB and Qdrant collection, one carefully scoped
temporary Keycloak public client if missing, and binds temporary uvicorn to
127.0.0.1:19137. Always cleans up resources; never restarts 19139.
"""
from __future__ import annotations

import json
import argparse
from contextlib import nullcontext
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
HIDDEN_STARTUP=subprocess.STARTUPINFO()
HIDDEN_STARTUP.dwFlags |= subprocess.STARTF_USESHOWWINDOW
HIDDEN_STARTUP.wShowWindow=subprocess.SW_HIDE
import sys
import time
import urllib.request

import psycopg
from psycopg import sql
from core06.db_client import _creds_dir, _pg_port

ROOT=Path(__file__).resolve().parents[1]
PY=Path(r"C:\Users\user\Documents\ManufacturingAI-Core-V06-P3-Hybrid-Retrieval\.p3venv\Scripts\python.exe")
_NODE_RESOLVED=os.environ.get("FACTORY_STAGE_NODE") or shutil.which("node") or "node"
NODE=Path(_NODE_RESOLVED)
STAMP=secrets.token_hex(6)
DB_NAME="factory_ingest_stage_"+STAMP
COLLECTION="factory_ingest_stage_"+STAMP
BASE="http://localhost:19137"
TIMEOUT=360


def db(admin=True,database="factory_core_v06"):
    return psycopg.connect(
        host="127.0.0.1",port=_pg_port(),dbname=database,
        user="factory_pg_admin" if admin else "factory_gateway",
        password=(_creds_dir()/("admin-password.txt" if admin else "gateway-password.txt")).read_text("ascii"),
        autocommit=True,connect_timeout=8,
    )


def run(command, env=None, timeout=40):
    p=subprocess.run(command,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                     capture_output=True,text=True,timeout=timeout,
                      creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=HIDDEN_STARTUP)
    if p.returncode:
        raise RuntimeError("command_failed_"+str(p.returncode)+": "+p.stderr[-450:])
    return p.stdout


def port_is_free():
    with socket.socket() as sock:
        sock.settimeout(.5)
        return sock.connect_ex(("127.0.0.1",19137))!=0


_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy():
    try:
        with _LOOPBACK_OPENER.open(BASE+"/health",timeout=2) as response:
            return response.status==200 and response.headers.get("content-type","").startswith("application/json")
    except Exception:
        return False


def stage_vector_client(vector_port):
    if vector_port is None:
        from core06.hybrid_retrieval import qdrant_client
        return qdrant_client()
    from qdrant_client import QdrantClient
    return QdrantClient(url=f"http://127.0.0.1:{vector_port}", timeout=10,
                        trust_env=False, check_compatibility=False)


def main(vector_port=None, scratch_root=None):
    if not port_is_free():
        raise RuntimeError("STAGING_PORT_19137_ALREADY_IN_USE")
    if not PY.is_file() or not NODE.is_file():
        raise RuntimeError("PINNED_STAGING_RUNTIME_MISSING")
    import tempfile
    stage_tmp_ctx = (
        tempfile.TemporaryDirectory(prefix="stage-tmp-", dir=scratch_root)
        if scratch_root is not None else nullcontext(None)
    )
    with stage_tmp_ctx as stage_tmp:
        return _run_stage(vector_port, stage_tmp)


def _run_stage(vector_port=None, stage_tmp=None):
    report={"staging_db":DB_NAME,"staging_collection":COLLECTION,
            "staging_origin":BASE,"status":"starting"}
    out=ROOT/"data"/"unified_storage_stage_execution.json"
    out.parent.mkdir(exist_ok=True)
    chrome_record=ROOT/"data"/"unified_storage_chrome_acceptance.json"
    if chrome_record.exists():
        chrome_record.unlink()
    child=None
    created_db=False
    created_client=False
    client=None
    try:
        # Snapshot current Keycloak client status without changing live clients.
        status=json.loads(run([str(PY),"-m","tests_pg.ephemeral_keycloak_client","verify"]))
        if status.get("exists") and not status.get("valid_config"):
            raise RuntimeError("FOREIGN_KEYCLOAK_CLIENT_CONFIGURATION")
        if not status.get("exists"):
            response=json.loads(run([str(PY),"-m","tests_pg.ephemeral_keycloak_client","create"]))
            if response.get("action")!="created":
                raise RuntimeError("STAGING_KEYCLOAK_CLIENT_NOT_CREATED")
            created_client=True
        with db() as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
        created_db=True
        with db(database=DB_NAME) as connection:
            for f in ("schema.sql","p2_oidc.sql","ingestion_v1.sql","ingestion_v2.sql","ingestion_v3_purge.sql","cad_kit_v1.sql"):
                connection.execute((ROOT/"core06"/f).read_text(encoding="utf-8-sig"))
        # The token's signed OIDC issuer/sub, not a caller-chosen actor ID.
        from tools.p3_gateway_smoke import mint
        from p2_gateway.security import verify_access_token
        principal=verify_access_token(mint("engineer"))
        with db(database=DB_NAME) as connection:
            connection.execute("INSERT INTO core.subject(id) VALUES('engineer')")
            connection.execute("INSERT INTO core.scope(id) VALUES('ingest_scope')")
            connection.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES('engineer','ingest_scope')")
            connection.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES('engineer','ingest_scope')")

            connection.execute("INSERT INTO core.scope(id) VALUES('engineering')")
            connection.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES('engineer','engineering')")
            connection.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES('engineer','engineering')")
            for n in range(40):
                connection.execute("""INSERT INTO core.engineering_source(
                    id,source_group,name,kind,relative_path,original_sha256,
                    original_size,parse_status,scope_id)
                    VALUES(%s,'TEST-ISOLATED',%s,'pdf',%s,%s,1,'text','engineering')""",
                    (f'stage-{n:03d}',f'isolated_{n:03d}',f'stage/{n:03d}.pdf','0'*64))

            connection.execute(
                "INSERT INTO core.oidc_identity(issuer,external_sub,subject_id) VALUES(%s,%s,'engineer')",
                (principal.issuer,principal.external_sub),
            )
        env=os.environ.copy()
        env.update({
            "FACTORY_V06_UNIFIED_STAGING":"1",
            "FACTORY_PG_PORT":str(_pg_port()),
            "FACTORY_V06_STAGING_DATABASE":DB_NAME,
            "FACTORY_V06_STAGING_COLLECTION":COLLECTION,
            "FACTORY_V06_INGEST_ENABLED":"1",
            "FACTORY_V06_INDEX_WORKER_ENABLED":"1",
            "FACTORY_P2_APP_ORIGIN":BASE,
            "FACTORY_P2_WEB_CLIENT":"factory-web-audit",
            "FACTORY_STAGE_PYTHON":str(PY),
            "HF_HUB_OFFLINE":"1",
            "TRANSFORMERS_OFFLINE":"1",
            "OMP_NUM_THREADS":"2",
            "MKL_NUM_THREADS":"2",
            "TOKENIZERS_PARALLELISM":"false",
            "NO_PROXY":"localhost,127.0.0.1",
            "no_proxy":"localhost,127.0.0.1",
        })
        live_data = ROOT.parent / "ManufacturingAI-Unified-V06-P3-P13" / "data"
        own_pid = ROOT / "data" / "model_process.json"
        if (
            "FACTORY_AI_PID_FILE" not in env
            and not own_pid.is_file()
            and (live_data / "model_api_key.txt").is_file()
            and (live_data / "model_process.json").is_file()
        ):
            env["FACTORY_AI_KEY_FILE"] = str(live_data / "model_api_key.txt")
            env["FACTORY_AI_PID_FILE"] = str(live_data / "model_process.json")
        if vector_port is not None:
            env["FACTORY_V06_ISOLATED_VECTOR_PORT"] = str(vector_port)
        if stage_tmp is not None:
            env["TEMP"] = str(stage_tmp)
            env["TMP"] = str(stage_tmp)
        server_log=(ROOT/"data"/"unified_storage_stage_server.log").open("wb")
        try:
            child=subprocess.Popen(
                [str(PY),"-m","uvicorn","unified_app:app",
                 "--host","127.0.0.1","--port","19137","--no-access-log"],
                cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=server_log,
                stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW,
                startupinfo=HIDDEN_STARTUP,
            )
            for i in range(90):
                if child.poll() is not None:
                    raise RuntimeError("STAGING_SERVER_EXITED_EARLY_"+str(child.returncode))
                if healthy():break
                time.sleep(.25)
            else:
                raise RuntimeError("STAGING_SERVER_HEALTH_TIMEOUT")
            print("STAGING_READY isolated db="+DB_NAME+"  port=19137",flush=True)
            report["status"]="running_chrome"
            result=subprocess.run([str(NODE),str(ROOT/"tests_pg"/"browser_unified_storage_stage.cjs")],
                                  cwd=ROOT,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=TIMEOUT,
                                  creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=HIDDEN_STARTUP)
            report["browser_returncode"]=result.returncode
            report["chrome_stdout_tail"]=result.stdout[-2800:]
            report["chrome_stderr_tail"]=result.stderr[-700:]
            chrome_report=ROOT/"data"/"unified_storage_chrome_acceptance.json"
            if chrome_report.exists():
                report["chrome"]=json.loads(chrome_report.read_text("utf-8"))
            if result.returncode!=0:
                raise RuntimeError("BROWSER_STAGE_ACCEPTANCE_FAILED "+result.stdout[-300:])
            with db(database=DB_NAME) as connection:
                events = connection.execute("""
                    SELECT event_kind, status, attempts FROM core.ingest_outbox
                    WHERE document_id=%s ORDER BY event_kind
                """, (report["chrome"]["document_id"],)).fetchall()
            if events != [("delete", "indexed", 1), ("index", "indexed", 1)]:
                raise RuntimeError(f"BROWSER_QUEUE_NOT_DRAINED {events!r}")
            client = stage_vector_client(vector_port)
            if client.count(COLLECTION, exact=True).count != 0:
                raise RuntimeError("BROWSER_VECTOR_POINT_NOT_REMOVED")
            report["queue"] = [
                {"event": event, "status": status, "attempts": attempts}
                for event, status, attempts in events
            ]
            report["vector_points_after_delete"] = 0
            # Clean-up SQL cannot race indexer: browser worker is synchronous.
            report["status"]="passed"
            return report
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait(timeout=8)
            server_log.close()
    finally:
        # Fail closed on failed cleanup; isolate original services regardless.
        cleanup=[]
        if created_db:
            try:
                with db() as connection:
                    connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB_NAME)))
                cleanup.append("db_dropped")
            except Exception as exc:cleanup.append("DB_CLEANUP_FAILED_"+type(exc).__name__)
        try:
            if client is None:
                client=stage_vector_client(vector_port)
            if client.collection_exists(COLLECTION):
                client.delete_collection(COLLECTION)
            cleanup.append("qdrant_removed" if not client.collection_exists(COLLECTION) else "QDRANT_CLEANUP_FAILED")
        except Exception as exc:cleanup.append("QDRANT_CLEANUP_FAILED_"+type(exc).__name__)
        finally:
            if client is not None:
                client.close()
        if created_client:
            try:
                response=json.loads(run([str(PY),"-m","tests_pg.ephemeral_keycloak_client","delete"]))
                cleanup.append("client_deleted" if response.get("action")=="deleted" else "CLIENT_CLEANUP_FAILED")
            except Exception as exc:cleanup.append("CLIENT_CLEANUP_FAILED_"+type(exc).__name__)
        report["cleanup"]=cleanup
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("CLEANUP "+json.dumps(cleanup),flush=True)
        if any("FAILED" in x for x in cleanup):
            raise RuntimeError("STAGING_CLEANUP_INCOMPLETE")


if __name__=="__main__":
    from tests_pg.native_vector_fixture import native_vector

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-exe", type=Path, help="Start an owned native loopback Qdrant fixture")
    parser.add_argument("--scratch-root", type=Path, help="Existing directory for temporary Qdrant storage")
    options = parser.parse_args()
    try:
        vector = native_vector(options.qdrant_exe, options.scratch_root) if options.qdrant_exe else nullcontext()
        with vector as port:
            print("STAGING_RESULT "+json.dumps(main(port, options.scratch_root),ensure_ascii=True),flush=True)
    except Exception as exc:
        print("STAGING_FAILED "+type(exc).__name__+": "+str(exc)[:700],flush=True)
        raise SystemExit(1)
