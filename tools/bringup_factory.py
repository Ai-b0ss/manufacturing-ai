"""Idempotent, owner-checked startup for the local v0.6 pilot.

Never stops, deletes, resets, updates, or migrates any process/database.
No model/GPU autostart. No service startup is scheduled automatically.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import msvcrt
import os
from pathlib import Path
import socket
import subprocess
HIDDEN_STARTUP=subprocess.STARTUPINFO()
HIDDEN_STARTUP.dwFlags |= subprocess.STARTF_USESHOWWINDOW
HIDDEN_STARTUP.wShowWindow=subprocess.SW_HIDE
import sys
import time
import urllib.request

HOME = Path(r"C:\Users\user\Documents")
MAIN = HOME / "ManufacturingAI-Unified-V06-P3-P13"
LEGACY = HOME / "ManufacturingAI-P13-Drawing-CAD-Registration"
P2 = HOME / "ManufacturingAI-Core-P2-OIDC"
PY = Path(r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe")
DESKTOP = Path(r"K:\DockerDesktop\Docker Desktop.exe")
CONTAINER = "factory-core-v06-qdrant-p3"
SERVICE = "FactoryCoreV06Pg"
def _pg_port():
    raw = os.environ.get("FACTORY_PG_PORT", "55436").strip()
    if not raw.isascii() or not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise RuntimeError("INVALID_FACTORY_PG_PORT")
    return int(raw)
PG_PORT = _pg_port()
NO_WINDOW = subprocess.CREATE_NO_WINDOW

def command(argv, timeout=10):
    p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                       creationflags=NO_WINDOW,startupinfo=HIDDEN_STARTUP)
    return p.returncode, p.stdout.decode("utf-8","replace").strip(), p.stderr.decode("utf-8","replace").strip()

def reachable(port):
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1",port)) == 0

_ORIGINAL_URLOPEN = urllib.request.urlopen
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(url, checker):
    try:
        opener = urllib.request.urlopen if urllib.request.urlopen is not _ORIGINAL_URLOPEN else _LOOPBACK_OPENER.open
        with opener(url, timeout=3) as resp:
            body=resp.read(16384)
            if resp.status != 200:return False
            return checker(body)
    except Exception:return False

def pg_service_running():
    rc,out,_=command(["sc.exe","query",SERVICE],8)
    if rc:return False
    return "RUNNING" in out

def pg_ready():
    return pg_service_running() and reachable(_pg_port())

def engine_ready():
    rc,txt,_=command(["docker","info","--format","{{.ServerVersion}}"],12)
    return rc == 0 and bool(txt)

def container_owner():
    rc,out,_=command(["docker","inspect",CONTAINER,"--format",
                '{{.Config.Image}}|{{index .Config.Labels "factory.core.stage"}}|{{.State.Status}}'],9)
    if rc:raise RuntimeError("QDRANT_CONTAINER_NOT_FOUND")
    parts=out.split("|")
    if len(parts)!=3 or parts[:2]!=["qdrant/qdrant:v1.20.2","v06-p3"]:
        raise RuntimeError("QDRANT_CONTAINER_OWNER_MISMATCH")
    return parts[2]

def qdrant_ready(docker_ok=None):
    if not (engine_ready() if docker_ok is None else docker_ok):return False
    try:state=container_owner()
    except Exception:return False
    return state=="running" and http("http://127.0.0.1:16333/healthz",
            lambda body: b"healthz check passed" in body)

def keycloak_ready():
    return http("http://127.0.0.1:18080/realms/factory-core-v06/.well-known/openid-configuration",
        lambda b: json.loads(b).get("issuer")=="http://127.0.0.1:18080/realms/factory-core-v06")

def legacy_ready():
    return http("http://127.0.0.1:19134/api/health",
        lambda b: (lambda d:d.get("status")=="ok" and d.get("mode")=="synthetic-local-demo")(json.loads(b)))

def main_ready():
    return http("http://localhost:19139/unified/health",
        lambda b: (lambda d:d.get("status")=="up" and d.get("auth")=="keycloak-p2" and
           d.get("search")=="qdrant-postgresql")(json.loads(b)))

def wait_ready(name, predicate, seconds):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(1)
    raise RuntimeError(name+"_NOT_READY")

def spawn(name,argv,cwd,log):
    log.parent.mkdir(parents=True,exist_ok=True)
    if log.exists() and log.stat().st_size >= 5_000_000:
        log.replace(log.with_suffix(".log.1"))
    with log.open("ab") as out:
        proc=subprocess.Popen([str(arg) for arg in argv],cwd=str(cwd),stdin=subprocess.DEVNULL,
            stdout=out,stderr=subprocess.STDOUT,creationflags=NO_WINDOW,
            startupinfo=HIDDEN_STARTUP,close_fds=True)
    return {"name":name,"pid":proc.pid}

def ensure_pg(actions):
    if pg_ready():return
    if reachable(_pg_port()):raise RuntimeError("POSTGRES_PORT_OCCUPIED_BY_UNVERIFIED_PROCESS")
    rc,out,err=command(["sc.exe","start",SERVICE],15)
    if rc:raise RuntimeError("PG_SERVICE_START_FAILED")
    actions.append({"started":SERVICE})
    wait_ready("POSTGRES",pg_ready,45)

def ensure_docker(actions):
    if engine_ready():return
    if not DESKTOP.is_file():raise RuntimeError("KNOWN_DOCKER_DESKTOP_NOT_FOUND")
    rc,out,err=command(["powershell.exe","-NoProfile","-NonInteractive","-WindowStyle","Hidden","-Command",
        "(Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id) -join ','"],9)
    if not out:
        raise RuntimeError("DOCKER_DESKTOP_NOT_RUNNING: GUI auto-launch blocked; start Docker Desktop intentionally")
    wait_ready("DOCKER_ENGINE",engine_ready,130)

def ensure_qdrant(actions):
    if qdrant_ready():return
    state=container_owner()
    if reachable(16333):raise RuntimeError("QDRANT_PORT_OWNED_BUT_WRONG_HEALTH")
    if state!="exited":raise RuntimeError("QDRANT_UNEXPECTED_STATE_"+state)
    rc,_,_=command(["docker","start",CONTAINER],35)
    if rc:raise RuntimeError("QDRANT_START_FAILED")
    actions.append({"started":CONTAINER})
    wait_ready("QDRANT",qdrant_ready,65)

def ensure_keycloak(actions):
    if keycloak_ready():return
    if reachable(18080):raise RuntimeError("KEYCLOAK_PORT_PRESENT_BUT_IDENTITY_NOT_VALID")
    src=P2/"tools"/"run_keycloak.py"
    if not PY.is_file() or not src.is_file():raise RuntimeError("KEYCLOAK_LAUNCHER_MISSING")
    actions.append(spawn("Keycloak",[PY,"-m","tools.run_keycloak"],P2,
                          MAIN/"data"/"keycloak_service_launcher.log"))
    wait_ready("KEYCLOAK_OIDC",keycloak_ready,130)

def ensure_legacy(actions):
    if legacy_ready():return
    if reachable(19134):raise RuntimeError("P13_PORT_PRESENT_BUT_HEALTH_INVALID")
    for fn in ("data/engineering_corpus.sqlite3","data/engineering_inspection.sqlite3","app.py"):
        if not (LEGACY/fn).is_file():raise RuntimeError("P13_REQUIRED_FILE_MISSING")
    actions.append(spawn("P13 read-only demo",[PY,"app.py","--host","127.0.0.1",
                  "--port","19134"],LEGACY,LEGACY/"data"/"p13_service_launcher.log"))
    wait_ready("P13",legacy_ready,65)

def ensure_main(actions):
    if main_ready():return
    if reachable(19139):raise RuntimeError("UNIFIED_PORT_PRESENT_BUT_HEALTH_INVALID")
    cmd=MAIN/"START_UNIFIED_V06.cmd"
    if not cmd.is_file():raise RuntimeError("UNIFIED_START_CMD_MISSING")
    actions.append(spawn("Unified",[Path(os.environ.get("SystemRoot",r"C:\Windows"))/"System32"/"cmd.exe",
              "/d","/c",cmd],MAIN,MAIN/"data"/"unified_start_launcher.log"))
    wait_ready("UNIFIED",main_ready,65)

def snapshot():
    status = {}
    probes = {
        "postgres": pg_ready,
        "docker": engine_ready,
        "qdrant": lambda: qdrant_ready(status.get("docker", False)),
        "keycloak": keycloak_ready,
        "legacy_p13": legacy_ready,
        "unified": main_ready,
        "local_model_tcp": lambda: reachable(19086),
    }
    for name, probe in probes.items():
        try:
            status[name] = probe()
        except (OSError, subprocess.TimeoutExpired):
            status[name] = False
    return status


@contextmanager
def startup_lock(path):
    with path.open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("STACK_START_ALREADY_RUNNING") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--start",action="store_true",help="Start only verified missing services")
    opts=parser.parse_args()
    actions=[]
    try:
        if opts.start:
            directory = MAIN / "data"
            directory.mkdir(exist_ok=True)
            with startup_lock(directory / "stack_start.lock"):
                for step in [ensure_pg,ensure_keycloak,ensure_legacy,
                             ensure_main,ensure_docker,ensure_qdrant]:
                    step(actions)
        status=snapshot()
        ok=all(status[k] for k in ("postgres","docker","qdrant","keycloak","legacy_p13","unified"))
        print(json.dumps({"ok":ok,"mode":"start" if opts.start else "check",
              "actions":actions,"services":status},ensure_ascii=True))
        return 0 if ok else 2
    except Exception as exc:
        print(json.dumps({"ok":False,"mode":"start" if opts.start else "check",
            "actions":actions,"failure":str(exc)[:170]},ensure_ascii=True))
        return 1

if __name__=="__main__":
    sys.exit(main())
