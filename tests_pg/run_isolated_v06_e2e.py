"""Self-contained isolated full-stack v0.6 acceptance runner (ZERO touch to live services).

Starts ephemeral, private instances on non-live loopback ports inside a disposable
TemporaryDirectory:
  1. PostgreSQL 18.7 (initdb + pg_ctl on a fresh free port != 55436)
  2. Native Qdrant 1.20.2 (tests_pg.native_vector_fixture on 19200..19899 != 16333)
  3. Keycloak 26 (kc.bat start-dev --db=dev-mem --config-file=<empty> on fresh port != 18080)
  4. llama-server.exe + MiniCPM5-2B-Q4_K_M.gguf (on fresh port != 19086)
  5. unified_app.py (uvicorn on staging port 19137 != 19139)

Executes:
  - Phase A (--suite): existing isolated PG/Qdrant/Keycloak test modules
  - Phase B (--e2e): real end-to-end P0 document agent scenario (two-role real Keycloak
    PKCE + Bearer auth, scanned PDF + WinRT OCR, outbox -> Qdrant E5 indexing,
    hybrid retrieval + page citations + JPEG page preview, real MiniCPM5-2B answer,
    no-source refusal, full PG + Qdrant + gateway restart persistence & recovery,
    live ACL revocation, RP-initiated logout, pg_dump/pg_restore +
    storage_integrity_audit, outbox retry recovery, soft delete -> worker delete -> purge).

Runtime paths are resolved from environment variables (FACTORY_PG_BIN, FACTORY_KC_BAT,
FACTORY_JAVA_HOME, FACTORY_AUDIT_QDRANT_EXE, FACTORY_AI_SERVER, FACTORY_AI_MODEL) or PATH.
All credentials are freshly generated synthetic tokens in temp dir; all processes
and temporary directories are torn down in finally.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import httpx
import psycopg
from psycopg import sql
from qdrant_client import QdrantClient

ROOT = Path(__file__).resolve().parents[1]


def _resolve_optional_path(env_var: str, which_names: tuple[str, ...] = (), parent_levels: int = 0) -> Path | None:
    raw = os.environ.get(env_var, "").strip()
    if raw:
        return Path(raw)
    for name in which_names:
        found = shutil.which(name)
        if found:
            p = Path(found).resolve()
            for _ in range(parent_levels):
                p = p.parent
            return p
    return None


PG_BIN = _resolve_optional_path("FACTORY_PG_BIN", ("initdb.exe", "initdb"), parent_levels=1)
KC_BAT = _resolve_optional_path("FACTORY_KC_BAT", ("kc.bat", "kc.sh"))
JAVA_HOME = (
    _resolve_optional_path("FACTORY_JAVA_HOME")
    or _resolve_optional_path("JAVA_HOME", ("java.exe", "java"), parent_levels=2)
)
QDRANT_EXE = _resolve_optional_path("FACTORY_AUDIT_QDRANT_EXE", ("qdrant.exe", "qdrant"))
LLM_SERVER = _resolve_optional_path("FACTORY_AI_SERVER", ("llama-server.exe", "llama-server"))
LLM_MODEL = _resolve_optional_path("FACTORY_AI_MODEL")
NODE_EXE = Path(os.environ.get("FACTORY_STAGE_NODE") or shutil.which("node") or "node")

FORBIDDEN_LIVE_PORTS = {55436, 16333, 18080, 19081, 19086, 19134, 19136, 19139}
STAGING_APP_PORT = 19137
STAGING_APP_ORIGIN = f"http://localhost:{STAGING_APP_PORT}"
REALM = "factory-core-v06"
DEMO_USERS = ("engineer", "technologist", "finance", "director")
MIGRATIONS = ("schema.sql", "p2_oidc.sql", "ingestion_v1.sql", "ingestion_v2.sql", "ingestion_v3_purge.sql", "cad_kit_v1.sql")


def _hidden_startupinfo():
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return info


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port(avoid: set[int]) -> int:
    for _ in range(80):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid and port not in FORBIDDEN_LIVE_PORTS and port != STAGING_APP_PORT and not (19200 <= port <= 19899) and 1024 <= port <= 65535:
            return port
    raise RuntimeError("NO_FREE_EPHEMERAL_PORT")


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_port_free(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_free(port):
            return
        time.sleep(0.2)
    raise RuntimeError(f"PORT_STILL_IN_USE_{port}")


def allocate_vector_port() -> int:
    with socket.socket() as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        for _ in range(40):
            port = 19200 + secrets.randbelow(700)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("NO_FREE_VECTOR_TEST_PORT")


def start_ephemeral_qdrant(executable: Path, qdrant_dir: Path, port: int):
    from tests_pg.native_vector_fixture import is_owned_listener

    qdrant_dir.mkdir(parents=True, exist_ok=True)
    config = qdrant_dir / "fixture.yaml"
    if not config.exists():
        config.write_text(
            "service:\n  grpc_port: null\n"
            "storage:\n  optimizers:\n    default_segment_number: 1\n",
            encoding="ascii",
        )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("QDRANT__")}
    env.update(
        {
            "QDRANT__SERVICE__HOST": "127.0.0.1",
            "QDRANT__SERVICE__HTTP_PORT": str(port),
            "QDRANT__TELEMETRY_DISABLED": "true",
            "QDRANT__STORAGE__PERFORMANCE__MAX_SEARCH_THREADS": "2",
            "TEMP": str(qdrant_dir),
            "TMP": str(qdrant_dir),
        }
    )
    log_path = qdrant_dir / "qdrant.log"
    log_fp = log_path.open("ab")
    proc = subprocess.Popen(
        [str(executable.resolve(strict=True)), "--config-path", str(config)],
        cwd=str(qdrant_dir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        creationflags=_no_window(),
        startupinfo=_hidden_startupinfo(),
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    with httpx.Client(timeout=1, trust_env=False) as http:
        while proc.poll() is None and time.monotonic() < deadline:
            if is_owned_listener(port, proc.pid):
                try:
                    if http.get(base + "/readyz").status_code == 200:
                        return proc, log_fp
                except httpx.RequestError:
                    pass
            time.sleep(0.2)
    kill_tree(proc)
    log_fp.close()
    tail = log_path.read_text("utf-8", errors="replace")[-1200:] if log_path.exists() else ""
    raise RuntimeError(f"QDRANT_START_FAILED: {tail}")


def stop_ephemeral_qdrant(proc: subprocess.Popen | None, log_fp) -> None:
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
    finally:
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass


def start_unified_app(app_log_path: Path, env: dict[str, str]):
    app_log = app_log_path.open("ab")
    app_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "unified_app:app", "--host", "127.0.0.1", "--port", str(STAGING_APP_PORT), "--no-access-log"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=app_log,
        stderr=subprocess.STDOUT,
        creationflags=_no_window(),
        startupinfo=_hidden_startupinfo(),
    )
    with httpx.Client(timeout=30, trust_env=False) as http:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if app_proc.poll() is not None:
                app_log.flush()
                app_log.close()
                raise RuntimeError("UNIFIED_APP_EXITED_EARLY: " + app_log_path.read_text("utf-8", errors="replace")[-1000:])
            try:
                r = http.get(STAGING_APP_ORIGIN + "/unified/ready")
                if r.status_code == 200 and r.json().get("ready") is True:
                    return app_proc, app_log, r.json()
            except Exception:
                pass
            time.sleep(0.3)
    kill_tree(app_proc)
    app_log.close()
    raise RuntimeError("UNIFIED_READY_TIMEOUT")


def kill_tree(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=_no_window(),
            startupinfo=_hidden_startupinfo(),
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_cmd(args: list[str], env: dict[str, str], timeout: int = 180, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_no_window(),
        startupinfo=_hidden_startupinfo(),
    )


def pg_connect(port: int, creds_dir: Path, dbname: str = "postgres", admin: bool = True):
    user = "factory_pg_admin" if admin else "factory_gateway"
    pw_file = "admin-password.txt" if admin else "gateway-password.txt"
    return psycopg.connect(
        host="127.0.0.1",
        port=port,
        dbname=dbname,
        user=user,
        password=(creds_dir / pw_file).read_text("ascii").strip(),
        connect_timeout=8,
        autocommit=True,
    )


def _grant_current_user_acl(path: Path) -> None:
    """Ensure the current user's non-elevated SID can access a temp directory.

    Python 3.13 mkdtemp() on elevated Windows tokens assigns ownership/DACL to
    BUILTIN\\Administrators; PostgreSQL initdb/pg_ctl drops Administrators via
    CreateRestrictedToken and accesses files as the user SID.
    """
    if os.name != "nt":
        return
    who = run_cmd(["whoami", "/user", "/fo", "csv", "/nh"], env=os.environ.copy(), timeout=10)
    if who.returncode != 0:
        return
    sid = who.stdout.strip().replace('"', "").split(",")[-1].strip()
    if sid.startswith("S-1-5-"):
        run_cmd(["icacls", str(path), "/grant", f"*{sid}:(OI)(CI)F", "/T"], env=os.environ.copy(), timeout=15)


def pg_ctl_start(pgdata: Path, pglog: Path, pgctl_log: Path, pg_port: int) -> None:
    # Never pass stdout/stderr PIPE to pg_ctl start on Windows: the spawned
    # postgres.exe inherits pipe handles and hangs capture_output=True until timeout.
    with pgctl_log.open("ab") as ctl_out:
        start = subprocess.run(
            [
                str(PG_BIN / "pg_ctl.exe"),
                "-D", str(pgdata),
                "-l", str(pglog),
                "-o", f"-h 127.0.0.1 -p {pg_port} -k \"\"",
                "start",
            ],
            cwd=str(ROOT),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=ctl_out,
            stderr=subprocess.STDOUT,
            timeout=60,
            creationflags=_no_window(),
            startupinfo=_hidden_startupinfo(),
        )
    if start.returncode != 0:
        log_tail = pglog.read_text("utf-8", errors="replace")[-600:] if pglog.exists() else ""
        ctl_tail = pgctl_log.read_text("utf-8", errors="replace")[-400:] if pgctl_log.exists() else ""
        raise RuntimeError(f"PG_CTL_START_FAILED: {ctl_tail} | {log_tail}")


def start_ephemeral_pg(tmp: Path, creds_dir: Path, pg_port: int, stage_db: str) -> tuple[Path, Path, Path]:
    _grant_current_user_acl(tmp)
    pgdata = tmp / "pgdata"
    pglog = tmp / "pg.log"
    pgctl_log = tmp / "pgctl.log"
    admin_pw = secrets.token_urlsafe(48)
    gateway_pw = secrets.token_urlsafe(48)
    (creds_dir / "admin-password.txt").write_text(admin_pw, encoding="ascii")
    (creds_dir / "gateway-password.txt").write_text(gateway_pw, encoding="ascii")

    init = run_cmd(
        [
            str(PG_BIN / "initdb.exe"),
            "-D", str(pgdata),
            "-U", "factory_pg_admin",
            f"--pwfile={creds_dir / 'admin-password.txt'}",
            "-A", "scram-sha-256",
            "-E", "UTF8",
            "--no-locale",
        ],
        env=os.environ.copy(),
        timeout=60,
    )
    if init.returncode != 0:
        raise RuntimeError(f"INITDB_FAILED: {init.stderr[-600:]}")

    pg_ctl_start(pgdata, pglog, pgctl_log, pg_port)

    with pg_connect(pg_port, creds_dir, "postgres", admin=True) as db:
        db.execute(
            """
            CREATE ROLE factory_owner NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
            CREATE ROLE factory_gateway LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
            """
        )
        db.execute(sql.SQL("ALTER ROLE factory_gateway WITH PASSWORD {}").format(sql.Literal(gateway_pw)))
        # Create an empty baseline factory_core_v06 inside the EPHEMERAL cluster for tests that connect to it
        db.execute("CREATE DATABASE factory_core_v06")
        db.execute("REVOKE ALL ON DATABASE factory_core_v06 FROM PUBLIC; GRANT CONNECT ON DATABASE factory_core_v06 TO factory_gateway;")
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(stage_db)))
        db.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC; GRANT CONNECT ON DATABASE {} TO factory_gateway;").format(
                sql.Identifier(stage_db), sql.Identifier(stage_db)
            )
        )

    for dbname in ("factory_core_v06", stage_db):
        with pg_connect(pg_port, creds_dir, dbname, admin=True) as db:
            for mig in MIGRATIONS:
                db.execute((ROOT / "core06" / mig).read_text(encoding="utf-8-sig"))
    return pgdata, pglog, pgctl_log


def stop_ephemeral_pg(pgdata: Path | None) -> None:
    if pgdata is None or not pgdata.exists():
        return
    run_cmd([str(PG_BIN / "pg_ctl.exe"), "-D", str(pgdata), "-m", "fast", "stop"], env=os.environ.copy(), timeout=30)


def start_ephemeral_keycloak(tmp: Path, creds_dir: Path, kc_port: int, kc_mgmt_port: int):
    kc_tmp = tmp / "kc_tmp"
    kc_tmp.mkdir(parents=True, exist_ok=True)
    empty_conf = tmp / "kc_empty.conf"
    empty_conf.write_text("# Isolated ephemeral Keycloak test config\n", encoding="ascii")
    admin_pw = secrets.token_urlsafe(24)
    (creds_dir / "keycloak-bootstrap-admin.txt").write_text(admin_pw, encoding="ascii")

    env = os.environ.copy()
    env.update(
        {
            "JAVA_HOME": str(JAVA_HOME),
            "KC_BOOTSTRAP_ADMIN_USERNAME": "factory_admin",
            "KC_BOOTSTRAP_ADMIN_PASSWORD": admin_pw,
            "JAVA_OPTS_APPEND": f"-Djava.io.tmpdir={kc_tmp} -Xms256m -Xmx768m",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }
    )
    kc_log_path = tmp / "keycloak.log"
    kc_log = kc_log_path.open("wb")
    cmd = [
        "cmd.exe", "/c", str(KC_BAT),
        f"--config-file={empty_conf}",
        "start-dev",
        "--db=dev-mem",
        "--cache=local",
        "--http-host=127.0.0.1",
        f"--http-port={kc_port}",
        f"--http-management-port={kc_mgmt_port}",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(kc_tmp),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=kc_log,
        stderr=subprocess.STDOUT,
        creationflags=_no_window(),
        startupinfo=_hidden_startupinfo(),
    )

    kc_url = f"http://127.0.0.1:{kc_port}"
    deadline = time.monotonic() + 120
    with httpx.Client(timeout=4, trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                kc_log.close()
                tail = kc_log_path.read_text("utf-8", errors="replace")[-1200:]
                raise RuntimeError(f"KEYCLOAK_EXITED_EARLY_{proc.returncode}: {tail}")
            try:
                r = client.get(f"{kc_url}/realms/master/.well-known/openid-configuration")
                if r.status_code == 200 and r.json().get("issuer") == f"{kc_url}/realms/master":
                    return proc, kc_log, kc_url
            except Exception:
                pass
            time.sleep(0.6)
    kill_tree(proc)
    kc_log.close()
    tail = kc_log_path.read_text("utf-8", errors="replace")[-1200:]
    raise RuntimeError(f"KEYCLOAK_START_TIMEOUT: {tail}")


def provision_ephemeral_keycloak(kc_url: str, creds_dir: Path) -> dict[str, str]:
    admin_pw = (creds_dir / "keycloak-bootstrap-admin.txt").read_text("ascii").strip()
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        tok = client.post(
            f"{kc_url}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli", "username": "factory_admin", "password": admin_pw},
        )
        if tok.status_code != 200:
            raise RuntimeError(f"KC_ADMIN_LOGIN_FAILED: {tok.status_code} {tok.text[:200]}")
        hdr = {"Authorization": "Bearer " + tok.json()["access_token"]}

        def kc_req(method: str, path: str, allowed=(200, 201, 204), **kw):
            r = client.request(method, kc_url + path, headers=hdr, **kw)
            if r.status_code not in allowed:
                raise RuntimeError(f"KC_HTTP_{r.status_code} {method} {path}: {r.text[:250]}")
            return r.json() if r.content else None

        kc_req(
            "POST",
            "/admin/realms",
            json={
                "realm": REALM,
                "enabled": True,
                "sslRequired": "NONE",
                "registrationAllowed": False,
                "resetPasswordAllowed": False,
                "loginWithEmailAllowed": False,
                "duplicateEmailsAllowed": False,
                "bruteForceProtected": True,
                "accessTokenLifespan": 600,
                "ssoSessionIdleTimeout": 1800,
                "verifyEmail": False,
            },
        )

        def create_client(cid: str, settings: dict) -> str:
            kc_req("POST", f"/admin/realms/{REALM}/clients", json={"clientId": cid, "enabled": True, "protocol": "openid-connect", **settings})
            rows = kc_req("GET", f"/admin/realms/{REALM}/clients?clientId={cid}")
            matches = [x for x in rows if x.get("clientId") == cid]
            if len(matches) != 1:
                raise RuntimeError(f"KC_CLIENT_NOT_FOUND_{cid}")
            return matches[0]["id"]

        def add_audience(guid: str):
            kc_req(
                "POST",
                f"/admin/realms/{REALM}/clients/{guid}/protocol-mappers/models",
                json={
                    "name": "factory-gateway-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "consentRequired": False,
                    "config": {
                        "included.client.audience": "factory-gateway",
                        "id.token.claim": "false",
                        "access.token.claim": "true",
                        "introspection.token.claim": "true",
                    },
                },
            )

        gw_guid = create_client(
            "factory-gateway",
            {
                "publicClient": False,
                "clientAuthenticatorType": "client-secret",
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False,
            },
        )
        web_guid = create_client(
            "factory-web-audit",
            {
                "publicClient": True,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [STAGING_APP_ORIGIN + "/auth/callback"],
                "webOrigins": [STAGING_APP_ORIGIN],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "post.logout.redirect.uris": STAGING_APP_ORIGIN + "/",
                },
            },
        )
        e2e_guid = create_client(
            "factory-e2e",
            {
                "publicClient": False,
                "clientAuthenticatorType": "client-secret",
                "serviceAccountsEnabled": False,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": True,
                "implicitFlowEnabled": False,
            },
        )
        add_audience(web_guid)
        add_audience(e2e_guid)

        gw_secret = kc_req("GET", f"/admin/realms/{REALM}/clients/{gw_guid}/client-secret")["value"]
        e2e_secret = kc_req("GET", f"/admin/realms/{REALM}/clients/{e2e_guid}/client-secret")["value"]
        (creds_dir / "keycloak-gateway-client-secret.txt").write_text(gw_secret, encoding="ascii")
        (creds_dir / "keycloak-e2e-client-secret.txt").write_text(e2e_secret, encoding="ascii")

        passwords = {u: secrets.token_urlsafe(20) for u in DEMO_USERS}
        (creds_dir / "keycloak-demo-users.json").write_text(json.dumps(passwords), encoding="utf-8")

        subs: dict[str, str] = {}
        for u in DEMO_USERS:
            kc_req(
                "POST",
                f"/admin/realms/{REALM}/users",
                json={
                    "username": u,
                    "enabled": True,
                    "email": f"{u}@example.invalid",
                    "firstName": "Demo",
                    "lastName": u.title(),
                    "emailVerified": True,
                    "requiredActions": [],
                    "credentials": [{"type": "password", "value": passwords[u], "temporary": False}],
                },
            )
            found = kc_req("GET", f"/admin/realms/{REALM}/users?username={u}&exact=true")
            matches = [row for row in found if row.get("username") == u]
            if len(matches) != 1:
                raise RuntimeError(f"KC_USER_NOT_FOUND_{u}")
            subs[u] = matches[0]["id"]
        return subs


def start_ephemeral_llm(tmp: Path, ai_port: int):
    ai_dir = tmp / "ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    key_file = ai_dir / "model_api_key.txt"
    pid_file = ai_dir / "model_process.json"
    log_file = ai_dir / "model_runtime.log"
    api_key = secrets.token_urlsafe(44)
    key_file.write_text(api_key, encoding="ascii")

    params = [
        str(LLM_SERVER),
        "-m", str(LLM_MODEL),
        "--host", "127.0.0.1",
        "--port", str(ai_port),
        "-t", "8",
        "-c", "3072",
        "-b", "128",
        "-ub", "64",
        "--parallel", "1",
        "-ngl", "99",
        "--api-key-file", str(key_file),
        "--cors-origins", STAGING_APP_ORIGIN,
        "--reasoning", "off",
    ]
    log_fp = log_file.open("wb")
    proc = subprocess.Popen(
        params,
        cwd=str(LLM_SERVER.parent),
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        creationflags=_no_window(),
        startupinfo=_hidden_startupinfo(),
    )
    pid_file.write_text(
        json.dumps({"pid": proc.pid, "model": str(LLM_MODEL), "server": str(LLM_SERVER), "port": ai_port}),
        encoding="utf-8",
    )

    deadline = time.monotonic() + 90
    with httpx.Client(timeout=3, trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_fp.close()
                tail = log_file.read_text("utf-8", errors="replace")[-1000:]
                raise RuntimeError(f"LLM_EXITED_EARLY_{proc.returncode}: {tail}")
            try:
                r = client.get(f"http://127.0.0.1:{ai_port}/health", headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code == 200:
                    return proc, log_fp, key_file, pid_file, log_file
            except Exception:
                pass
            time.sleep(0.5)
    kill_tree(proc)
    log_fp.close()
    tail = log_file.read_text("utf-8", errors="replace")[-1000:]
    raise RuntimeError(f"LLM_START_TIMEOUT: {tail}")


def mint_token(kc_url: str, creds_dir: Path, username: str) -> str:
    pw = json.loads((creds_dir / "keycloak-demo-users.json").read_text("utf-8"))[username]
    secret = (creds_dir / "keycloak-e2e-client-secret.txt").read_text("ascii").strip()
    with httpx.Client(timeout=15, trust_env=False) as client:
        r = client.post(
            f"{kc_url}/realms/{REALM}/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "factory-e2e", "client_secret": secret, "username": username, "password": pw},
        )
        if r.status_code != 200:
            raise RuntimeError(f"MINT_FAILED_{username}_{r.status_code}")
        return r.json()["access_token"]


def run_existing_isolated_suites(base_env: dict[str, str], scratch_dir: Path) -> list[dict]:
    py = sys.executable
    # Exclude test_15 from test_policy_rls because it asserts live P13 migrated row counts in factory_core_v06
    policy_tests = [
        f"tests_pg.test_policy_rls.PostgreSQLCoreTests.test_{n:02d}_" + name
        for n, name in [
            (1, "postgres_is_real_and_no_gateway_superuser"),
            (2, "default_denies_all_without_actor"),
            (3, "actor_scoping_and_no_cross_user_exposure"),
            (4, "protected_chunks_and_fts_inherit_document_scope"),
            (5, "finance_isolated_and_absent_from_tech_search"),
            (6, "role_cannot_read_grants_or_audit"),
            (7, "gateway_cannot_mutate_data_or_impersonate_owner"),
            (8, "row_security_off_does_not_bypass_for_gateway"),
            (9, "sql_injection_is_data_not_sql"),
            (10, "transaction_local_actor_does_not_leak_across_calls"),
            (11, "revoke_visible_in_existing_read_committed_transaction"),
            (12, "move_document_hides_both_doc_and_chunk_atomically"),
            (13, "rollback_on_missing_document_preserves_ledger"),
            (14, "literal_bad_subject_and_limit_fail_validation"),
        ]
    ]
    commands = [
        ("test_policy_rls (14 tests)", [py, "-m", "unittest", *policy_tests]),
        ("test_p2_gateway (20 tests)", [py, "-m", "unittest", "tests_pg.test_p2_gateway"]),
        ("test_outbox_retry_recovery", [py, "-m", "unittest", "tests_pg.test_outbox_retry_recovery"]),
        ("test_ingest_isolated_pg", [py, "-m", "tests_pg.test_ingest_isolated_pg"]),
        ("test_cad_kit_isolated_pg", [py, "-m", "tests_pg.test_cad_kit_isolated_pg"]),
        ("test_ingest_races", [py, "-m", "tests_pg.test_ingest_races"]),
        ("test_ingest_physical_purge (PG+Qdrant)", [py, "-m", "tests_pg.test_ingest_physical_purge", "--qdrant-exe", str(QDRANT_EXE), "--scratch-root", str(scratch_dir)]),
        ("test_demo_reindex_scope (PG+Qdrant)", [py, "-m", "tests_pg.test_demo_reindex_scope", "--native-qdrant", str(QDRANT_EXE), "--scratch-root", str(scratch_dir)]),
        ("run_vector_pg_staging_audit (PG+Qdrant)", [py, "-m", "tests_pg.run_vector_pg_staging_audit", "--qdrant-exe", str(QDRANT_EXE), "--scratch", str(scratch_dir)]),
        ("run_storage_recovery_drill (PG+Qdrant snapshot/restore)", [py, "-m", "tests_pg.run_storage_recovery_drill", "--qdrant-exe", str(QDRANT_EXE), "--scratch-root", str(scratch_dir)]),
        ("test_ingest_http_keycloak (real Keycloak+PG)", [py, "-m", "tests_pg.test_ingest_http_keycloak"]),
        ("test_antigravity_audit_regressions.AntigravityIsolatedFullStackTests", [py, "-m", "unittest", "tests_pg.test_antigravity_audit_regressions.AntigravityIsolatedFullStackTests"]),
    ]
    results = []
    suite_env = base_env.copy()
    # Let each existing test manage its own scratch DB/collection (without staging DB pin)
    for k in ("FACTORY_V06_STAGING_DATABASE", "FACTORY_V06_STAGING_COLLECTION", "FACTORY_V06_ISOLATED_VECTOR_PORT"):
        suite_env.pop(k, None)
    suite_env["FACTORY_AUDIT_SCRATCH"] = str(scratch_dir)
    for label, cmd in commands:
        t0 = time.perf_counter()
        p = run_cmd(cmd, env=suite_env, timeout=240)
        ms = round((time.perf_counter() - t0) * 1000)
        ok = p.returncode == 0
        results.append({"suite": label, "ok": ok, "returncode": p.returncode, "ms": ms})
        print(f"[SUITE] {'PASS' if ok else 'FAIL'} {label} ({ms} ms)", flush=True)
        if not ok:
            print("--- STDOUT ---\n" + p.stdout[-1500:], flush=True)
            print("--- STDERR ---\n" + p.stderr[-1500:], flush=True)
            raise RuntimeError(f"ISOLATED_SUITE_FAILED: {label}")
    return results


def run_full_p0_e2e(
    tmp: Path,
    creds_dir: Path,
    pgdata: Path,
    pglog: Path,
    pgctl_log: Path,
    pg_port: int,
    kc_port: int,
    kc_url: str,
    stage_db: str,
    subs: dict[str, str],
    ai_port: int,
    ai_key_file: Path,
    ai_pid_file: Path,
    ai_log_file: Path,
) -> dict:
    if not port_is_free(STAGING_APP_PORT):
        raise RuntimeError(f"STAGING_PORT_{STAGING_APP_PORT}_BUSY")

    issuer = f"{kc_url}/realms/{REALM}"
    with pg_connect(pg_port, creds_dir, stage_db, admin=True) as db:
        db.execute("INSERT INTO core.subject(id) VALUES ('engineer'),('technologist'),('finance'),('director')")
        db.execute("INSERT INTO core.scope(id,classification) VALUES ('public','public'),('engineering','internal'),('finance','restricted')")
        db.execute(
            """INSERT INTO core.scope_grant(subject_id,scope_id) VALUES
               ('engineer','public'),('engineer','engineering'),
               ('technologist','public'),
               ('finance','public'),('finance','finance'),
               ('director','public'),('director','engineering'),('director','finance')"""
        )
        db.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES ('engineer','engineering')")
        for user, sub in subs.items():
            db.execute(
                "INSERT INTO core.oidc_identity(issuer,external_sub,subject_id) VALUES (%s,%s,%s)",
                (issuer, sub, user),
            )

    report: dict = {"stage_db": stage_db, "ports": {"pg": pg_port, "keycloak": kc_port, "ai": ai_port, "app": STAGING_APP_PORT}, "steps": []}

    qdrant_dir = tmp / "qdrant_store"
    qport = allocate_vector_port()
    report["ports"]["qdrant"] = qport
    qurl = f"http://127.0.0.1:{qport}"
    running: dict = {"q_proc": None, "q_log": None, "app_proc": None, "app_log": None}

    def _cleanup_runtime() -> None:
        kill_tree(running["app_proc"])
        if running["app_log"] is not None:
            try:
                running["app_log"].close()
            except Exception:
                pass
        stop_ephemeral_qdrant(running["q_proc"], running["q_log"])

    with ExitStack() as stack:
        stack.callback(_cleanup_runtime)
        running["q_proc"], running["q_log"] = start_ephemeral_qdrant(QDRANT_EXE, qdrant_dir, qport)

        # Initialize empty vector collection before starting unified_app so /unified/ready can verify green status
        env = os.environ.copy()
        env.update(
            {
                "FACTORY_V06_UNIFIED_STAGING": "1",
                "FACTORY_V06_INGEST_ENABLED": "1",
                "FACTORY_V06_INDEX_WORKER_ENABLED": "1",
                "FACTORY_P2_APP_ORIGIN": STAGING_APP_ORIGIN,
                "FACTORY_P2_WEB_CLIENT": "factory-web-audit",
                "FACTORY_P2_KEYCLOAK_PORT": str(kc_port),
                "FACTORY_P2_CREDS_DIR": str(creds_dir),
                "FACTORY_PG_PORT": str(pg_port),
                "FACTORY_PG_CREDS_DIR": str(creds_dir),
                "FACTORY_V06_STAGING_DATABASE": stage_db,
                "FACTORY_V06_STAGING_COLLECTION": stage_db,
                "FACTORY_V06_ISOLATED_VECTOR_PORT": str(qport),
                "FACTORY_AI_PORT": str(ai_port),
                "FACTORY_AI_KEY_FILE": str(ai_key_file),
                "FACTORY_AI_PID_FILE": str(ai_pid_file),
                "FACTORY_AI_LOG_FILE": str(ai_log_file),
                "FACTORY_STAGE_PYTHON": sys.executable,
                "FACTORY_E2E_TMP": str(tmp),
                "TEMP": str(tmp),
                "TMP": str(tmp),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "OMP_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "TOKENIZERS_PARALLELISM": "false",
                "PYTHONUTF8": "1",
                "NO_PROXY": "localhost,127.0.0.1",
                "no_proxy": "localhost,127.0.0.1",
            }
        )

        # Ensure collection exists in isolated Qdrant
        init_col = run_cmd(
            [sys.executable, "-c", "from core06.hybrid_retrieval import ensure_collection, qdrant_client; c=qdrant_client(); ensure_collection(c); c.close()"],
            env=env,
            timeout=30,
        )
        if init_col.returncode != 0:
            raise RuntimeError(f"QDRANT_ENSURE_COLLECTION_FAILED: {init_col.stderr}")

        app_log_path = tmp / "unified_app.log"
        running["app_proc"], running["app_log"], ready_json = start_unified_app(app_log_path, env)
        report["ready"] = ready_json
        report["steps"].append({"step": "unified_ready_200", "ok": True, "dependencies": ready_json["dependencies"]})
        print("[E2E] PASS /unified/ready -> 200 (PG RLS + Qdrant + Keycloak OIDC)", flush=True)

        with httpx.Client(timeout=30, trust_env=False) as http:
            # Step 2-6: Real Headless Chrome PKCE Engineer flow (upload scanned PDF + OCR + Qdrant index + hybrid search + page preview + real MiniCPM5-2B ask + unknown query refusal + RP logout)
            eng_browser = run_cmd([str(NODE_EXE), str(ROOT / "tests_pg" / "browser_isolated_v06_e2e.cjs"), "engineer"], env=env, timeout=240)
            if eng_browser.returncode != 0:
                raise RuntimeError(f"CHROME_ENGINEER_E2E_FAILED:\nSTDOUT: {eng_browser.stdout[-1200:]}\nSTDERR: {eng_browser.stderr[-1200:]}")
            eng_data = json.loads(eng_browser.stdout.strip().splitlines()[-1])
            doc_id = eng_data["document_id"]
            report["engineer_browser"] = eng_data
            report["steps"].append({"step": "engineer_browser_pkce_ocr_llm_logout", "ok": True, "doc_id": doc_id})
            print(
                f"[E2E] PASS Engineer Chrome PKCE -> PDF/OCR -> Qdrant -> MiniCPM5-2B cited answer -> unknown refusal -> RP logout "
                f"(doc={doc_id}, llm_mode={eng_data['ask_answered']['mode']}, citations={eng_data['ask_answered']['citations']}, "
                f"unknown_mode={eng_data['ask_unknown_with_docs']['mode']})",
                flush=True,
            )

            # Step 7: Real vector index integrity audit (PG vs native Qdrant)
            vec_audit = run_cmd([sys.executable, "-m", "tools.vector_index_integrity_audit", "--database", stage_db], env=env, timeout=45)
            if vec_audit.returncode != 0:
                raise RuntimeError(f"VECTOR_INTEGRITY_AUDIT_FAILED: {vec_audit.stdout} {vec_audit.stderr}")
            vec_json = json.loads(vec_audit.stdout)
            if vec_json.get("status") != "passed":
                raise RuntimeError(f"VECTOR_INTEGRITY_NOT_PASSED: {vec_json}")
            report["vector_audit"] = vec_json
            report["steps"].append({"step": "vector_index_integrity_audit", "ok": True})
            print("[E2E] PASS tools.vector_index_integrity_audit -> passed (0 drift)", flush=True)

            # Step 7b: Full-stack service restart (unified_app + Qdrant + PostgreSQL) -> persistence & recovery verification
            print("[E2E] Restarting unified_app, Qdrant, and PostgreSQL on persisted storage...", flush=True)
            kill_tree(running["app_proc"])
            running["app_proc"] = None
            if running["app_log"] is not None:
                running["app_log"].close()
                running["app_log"] = None
            wait_port_free(STAGING_APP_PORT)

            stop_ephemeral_qdrant(running["q_proc"], running["q_log"])
            running["q_proc"] = None
            running["q_log"] = None
            wait_port_free(qport)

            stop_ephemeral_pg(pgdata)
            wait_port_free(pg_port)

            pg_ctl_start(pgdata, pglog, pgctl_log, pg_port)
            running["q_proc"], running["q_log"] = start_ephemeral_qdrant(QDRANT_EXE, qdrant_dir, qport)
            running["app_proc"], running["app_log"], restart_ready = start_unified_app(app_log_path, env)

            stale_cookie = http.get(
                STAGING_APP_ORIGIN + "/unified/api/me",
                headers={"Cookie": "factory_p2_session=stale-pre-restart-cookie"},
            )
            if stale_cookie.status_code != 401:
                raise RuntimeError(f"STALE_SESSION_NOT_REJECTED_AFTER_RESTART: {stale_cookie.status_code}")

            eng_tok_post_restart = mint_token(kc_url, creds_dir, "engineer")
            sr_post = http.get(
                STAGING_APP_ORIGIN + "/unified/api/search",
                params={"q": "Н-125"},
                headers={"Authorization": "Bearer " + eng_tok_post_restart},
                timeout=90,
            )
            if sr_post.status_code != 200 or not any(
                item.get("id") == doc_id and item.get("has_ocr") is True for item in sr_post.json().get("items", [])
            ):
                raise RuntimeError(f"POST_RESTART_SEARCH_FAILED: {sr_post.status_code} {sr_post.text[:400]}")
            doc_post = http.get(
                STAGING_APP_ORIGIN + "/unified/api/document",
                params={"id": doc_id},
                headers={"Authorization": "Bearer " + eng_tok_post_restart},
            )
            if doc_post.status_code != 200 or "25 бар" not in doc_post.json().get("text", ""):
                raise RuntimeError(f"POST_RESTART_DOCUMENT_FAILED: {doc_post.status_code}")
            prev_post = http.get(
                STAGING_APP_ORIGIN + "/unified/api/page-preview",
                params={"id": doc_id, "page": 2},
                headers={"Authorization": "Bearer " + eng_tok_post_restart},
            )
            if prev_post.status_code != 200 or prev_post.headers.get("content-type") != "image/jpeg" or not prev_post.content.startswith(b"\xff\xd8"):
                raise RuntimeError(f"POST_RESTART_PREVIEW_FAILED: {prev_post.status_code}")
            orig_post = http.get(
                STAGING_APP_ORIGIN + f"/unified/api/uploads/{doc_id}/original",
                headers={"Authorization": "Bearer " + eng_tok_post_restart},
            )
            expected_sample_sha = hashlib.sha256((ROOT / "samples" / "factory_scanned_pump_demo.pdf").read_bytes()).hexdigest()
            if orig_post.status_code != 200 or hashlib.sha256(orig_post.content).hexdigest() != expected_sample_sha:
                raise RuntimeError(f"POST_RESTART_ORIGINAL_SHA_MISMATCH: {orig_post.status_code}")
            vec_post = run_cmd([sys.executable, "-m", "tools.vector_index_integrity_audit", "--database", stage_db], env=env, timeout=45)
            if vec_post.returncode != 0 or json.loads(vec_post.stdout).get("status") != "passed":
                raise RuntimeError(f"POST_RESTART_VECTOR_AUDIT_FAILED: {vec_post.stdout} {vec_post.stderr}")
            report["post_restart_ready"] = restart_ready
            report["steps"].append({"step": "full_stack_restart_pg_qdrant_gateway_persistence", "ok": True})
            print("[E2E] PASS Full restart (PG + Qdrant + unified_app) -> /unified/ready 200, stale cookie 401, OCR search/doc/preview/original SHA + 0-drift audit persisted", flush=True)

            # Step 8: Second role (Technologist) via real Headless Chrome PKCE + Bearer spoof check
            tech_browser = run_cmd([str(NODE_EXE), str(ROOT / "tests_pg" / "browser_isolated_v06_e2e.cjs"), "technologist", doc_id], env=env, timeout=180)
            if tech_browser.returncode != 0:
                raise RuntimeError(f"CHROME_TECH_E2E_FAILED:\nSTDOUT: {tech_browser.stdout[-1200:]}\nSTDERR: {tech_browser.stderr[-1200:]}")
            tech_data = json.loads(tech_browser.stdout.strip().splitlines()[-1])
            report["technologist_browser"] = tech_data
            report["steps"].append({"step": "technologist_browser_pkce_isolation_refusal_logout", "ok": True})
            print("[E2E] PASS Technologist Chrome PKCE -> 0 leaks, 404 on doc/original/preview, no-source refusal, RP logout", flush=True)

            # Bearer token spoof check + write denial for technologist
            eng_tok = mint_token(kc_url, creds_dir, "engineer")
            tech_tok = mint_token(kc_url, creds_dir, "technologist")
            spoof_write = http.post(
                STAGING_APP_ORIGIN + "/unified/api/uploads",
                content=b"Unauthorized spoof attempt",
                headers={
                    "Authorization": "Bearer " + tech_tok,
                    "X-Actor-Id": "engineer",
                    "X-Filename": "spoof.txt",
                    "X-Title": "Spoof",
                    "X-Scope-Id": "engineering",
                    "Content-Type": "text/plain",
                    "Idempotency-Key": "spoof-write-attempt-00001",
                },
            )
            if spoof_write.status_code != 403:
                raise RuntimeError(f"TECH_WRITE_NOT_DENIED: {spoof_write.status_code}")
            report["steps"].append({"step": "technologist_write_and_spoof_denied_403", "ok": True})

            # Step 9: Live ACL revocation (PG only; Qdrant untouched!) -> immediate fail-closed across search/doc/original/preview
            with closing(QdrantClient(url=qurl, trust_env=False, check_compatibility=False, timeout=15)) as qclient:
                pts_before = qclient.count(stage_db, exact=True).count
                if pts_before < 1:
                    raise RuntimeError("NO_QDRANT_POINTS_BEFORE_REVOKE")
                with pg_connect(pg_port, creds_dir, stage_db, admin=True) as db:
                    db.execute("SELECT core.revoke_scope_access('engineer','engineering','isolated_e2e_revoke')")
                sr_rev = http.get(STAGING_APP_ORIGIN + "/unified/api/search", params={"q": "Н-125"}, headers={"Authorization": "Bearer " + eng_tok})
                doc_rev = http.get(STAGING_APP_ORIGIN + "/unified/api/document", params={"id": doc_id}, headers={"Authorization": "Bearer " + eng_tok})
                orig_rev = http.get(STAGING_APP_ORIGIN + f"/unified/api/uploads/{doc_id}/original", headers={"Authorization": "Bearer " + eng_tok})
                prev_rev = http.get(STAGING_APP_ORIGIN + "/unified/api/page-preview", params={"id": doc_id, "page": 2}, headers={"Authorization": "Bearer " + eng_tok})
                pts_after = qclient.count(stage_db, exact=True).count
                if (sr_rev.status_code, sr_rev.json().get("total"), doc_rev.status_code, orig_rev.status_code, prev_rev.status_code) != (200, 0, 404, 404, 404):
                    raise RuntimeError(f"REVOKE_NOT_FAIL_CLOSED: {(sr_rev.status_code, sr_rev.json(), doc_rev.status_code, orig_rev.status_code, prev_rev.status_code)}")
                if pts_after != pts_before:
                    raise RuntimeError("QDRANT_MUTATED_DURING_REVOKE")
                # Restore grant and verify access resumes
                with pg_connect(pg_port, creds_dir, stage_db, admin=True) as db:
                    db.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES ('engineer','engineering')")
                doc_restored = http.get(STAGING_APP_ORIGIN + "/unified/api/document", params={"id": doc_id}, headers={"Authorization": "Bearer " + eng_tok})
                if doc_restored.status_code != 200:
                    raise RuntimeError("ACCESS_DID_NOT_RESUME_AFTER_GRANT_RESTORE")
                report["steps"].append({"step": "live_acl_revocation_immediate_without_qdrant_write", "ok": True, "qdrant_points": pts_after})
                print("[E2E] PASS Live ACL revocation -> immediate 0 search / 404 doc+original+preview with Qdrant untouched", flush=True)

            # Step 10: Real pg_dump -> pg_restore -> storage_integrity_audit (on isolated temp dir & restored DB!)
            backup_dir = tmp / "backup"
            backup_dir.mkdir(exist_ok=True)
            dump_file = backup_dir / f"{stage_db}.dump"
            pg_env = env.copy()
            pg_env["PGPASSWORD"] = (creds_dir / "admin-password.txt").read_text("ascii").strip()
            dump_res = run_cmd(
                [str(PG_BIN / "pg_dump.exe"), "-h", "127.0.0.1", "-p", str(pg_port), "-U", "factory_pg_admin", "-d", stage_db, "--format=custom", "--no-password", "--file", str(dump_file)],
                env=pg_env,
                timeout=60,
            )
            if dump_res.returncode != 0:
                raise RuntimeError(f"PG_DUMP_FAILED: {dump_res.stderr}")
            dump_sha = hashlib.sha256(dump_file.read_bytes()).hexdigest()
            restored_db = stage_db + "_restored"
            with pg_connect(pg_port, creds_dir, "postgres", admin=True) as db:
                db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_db)))
            try:
                rst_res = run_cmd(
                    [str(PG_BIN / "pg_restore.exe"), "-h", "127.0.0.1", "-p", str(pg_port), "-U", "factory_pg_admin", "-d", restored_db, "--no-password", "--exit-on-error", str(dump_file)],
                    env=pg_env,
                    timeout=60,
                )
                if rst_res.returncode != 0:
                    raise RuntimeError(f"PG_RESTORE_FAILED: {rst_res.stderr}")
                with pg_connect(pg_port, creds_dir, stage_db, admin=True) as db:
                    src_counts = {
                        tbl: db.execute(sql.SQL("SELECT count(*) FROM core.{}").format(sql.Identifier(tbl))).fetchone()[0]
                        for tbl in ("document", "document_blob", "cad_kit", "cad_kit_revision", "cad_kit_request")
                    }
                receipt_path = backup_dir / "receipt.json"
                receipt_path.write_text(
                    json.dumps({"backup": str(dump_file), "sha256": dump_sha, "dump_bytes": dump_file.stat().st_size, "counts": src_counts}),
                    encoding="utf-8",
                )
                audit_res = run_cmd(
                    [
                        sys.executable, "-m", "tools.storage_integrity_audit",
                        "--check-database", "--database", restored_db,
                        "--backup-receipt", str(receipt_path),
                        "--pg-restore", str(PG_BIN / "pg_restore.exe"),
                    ],
                    env=env,
                    timeout=45,
                )
                if audit_res.returncode != 0:
                    raise RuntimeError(f"STORAGE_INTEGRITY_AUDIT_FAILED: {audit_res.stdout} {audit_res.stderr}")
                audit_json = json.loads(audit_res.stdout)
                if audit_json.get("checks", {}).get("database", {}).get("status") != "passed" or audit_json.get("checks", {}).get("backup", {}).get("status") != "passed":
                    raise RuntimeError(f"STORAGE_AUDIT_UNEXPECTED: {audit_json}")
                # Verify RLS active on restored DB
                with pg_connect(pg_port, creds_dir, restored_db, admin=False) as gw, gw.transaction():
                    gw.execute("SELECT set_config('factory.actor_id','',true)")
                    rls_active, anon_cnt = gw.execute("SELECT row_security_active('core.document'), (SELECT count(*) FROM core.document)").fetchone()
                    if (rls_active, anon_cnt) != (True, 0):
                        raise RuntimeError("RESTORED_DB_RLS_MISMATCH")
                report["backup_audit"] = {"dump_bytes": dump_file.stat().st_size, "database": audit_json["checks"]["database"], "backup": audit_json["checks"]["backup"]}
                report["steps"].append({"step": "backup_restore_and_storage_integrity_audit", "ok": True})
                print("[E2E] PASS pg_dump -> pg_restore -> storage_integrity_audit + restored RLS verified", flush=True)
            finally:
                with pg_connect(pg_port, creds_dir, "postgres", admin=True) as db:
                    db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(restored_db)))

            # Step 11: Soft delete -> worker Qdrant delete -> outbox retry recovery check -> physical purge (with 24h retention)
            del_resp = http.delete(STAGING_APP_ORIGIN + f"/unified/api/uploads/{doc_id}", headers={"Authorization": "Bearer " + eng_tok})
            if del_resp.status_code != 200 or not del_resp.json().get("deleted"):
                raise RuntimeError(f"SOFT_DELETE_FAILED: {del_resp.status_code} {del_resp.text}")
            # Simulate exhausted delete job (5 attempts), recover via tools.outbox_retry_recovery, then run worker!
            with pg_connect(pg_port, creds_dir, stage_db, admin=True) as db:
                del_job_id = db.execute(
                    "SELECT id FROM core.ingest_outbox WHERE document_id=%s AND event_kind='delete'",
                    (doc_id,),
                ).fetchone()[0]
                db.execute(
                    "UPDATE core.ingest_outbox SET status='failed', attempts=5, updated_at=now()-interval '60 seconds' WHERE id=%s",
                    (del_job_id,),
                )
            rec_res = run_cmd(
                [sys.executable, "-m", "tools.outbox_retry_recovery", "--database", stage_db, "--job-ids", str(del_job_id), "--apply", "--confirm-database", stage_db],
                env=env,
                timeout=30,
            )
            if rec_res.returncode != 0:
                raise RuntimeError(f"OUTBOX_RECOVERY_FAILED: {rec_res.stdout} {rec_res.stderr}")
            del_worker = run_cmd([sys.executable, "-m", "tools.run_ingest_indexer", "--once"], env=env, timeout=60)
            if del_worker.returncode != 0 or "result=deleted" not in del_worker.stdout:
                raise RuntimeError(f"DELETE_WORKER_FAILED: {del_worker.stdout} {del_worker.stderr}")
            with closing(QdrantClient(url=qurl, trust_env=False, check_compatibility=False, timeout=15)) as qclient:
                if qclient.count(stage_db, exact=True).count != 0:
                    raise RuntimeError("QDRANT_POINTS_NOT_DELETED")
            with pg_connect(pg_port, creds_dir, stage_db, admin=True) as db:
                db.execute("UPDATE core.document SET deleted_at = now() - interval '25 hours' WHERE id=%s", (doc_id,))
            purge_env = env.copy()
            purge_env["FACTORY_V06_PURGE_ENABLED"] = "1"
            purge_res = run_cmd(
                [sys.executable, "-m", "tools.purge_deleted_upload", "--document-id", doc_id, "--execute", "--confirm-id", doc_id],
                env=purge_env,
                timeout=30,
            )
            if purge_res.returncode != 0 or not json.loads(purge_res.stdout).get("executed"):
                raise RuntimeError(f"PHYSICAL_PURGE_FAILED: {purge_res.stdout} {purge_res.stderr}")
            report["steps"].append({"step": "soft_delete_outbox_recovery_qdrant_delete_physical_purge", "ok": True})
            print("[E2E] PASS Soft delete -> outbox retry recovery -> worker Qdrant cleanup -> 24h physical purge", flush=True)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-only", action="store_true", help="Run only existing isolated PG/Qdrant/Keycloak test suites")
    parser.add_argument("--e2e-only", action="store_true", help="Run only full live P0 Document Agent E2E")
    default_scratch = os.environ.get("FACTORY_E2E_SCRATCH_ROOT", "").strip()
    parser.add_argument("--scratch-root", type=Path, default=Path(default_scratch) if default_scratch else None)
    args = parser.parse_args()

    required: list[tuple[str, str, Path | None]] = [
        ("PG bin (initdb.exe)", "FACTORY_PG_BIN", (PG_BIN / "initdb.exe") if PG_BIN else None),
        ("Keycloak", "FACTORY_KC_BAT", KC_BAT),
        ("JDK (java.exe)", "FACTORY_JAVA_HOME", (JAVA_HOME / "bin" / "java.exe") if JAVA_HOME else None),
        ("Qdrant", "FACTORY_AUDIT_QDRANT_EXE", QDRANT_EXE),
    ]
    if not args.suite_only:
        required.extend(
            [
                ("llama-server", "FACTORY_AI_SERVER", LLM_SERVER),
                ("GGUF model", "FACTORY_AI_MODEL", LLM_MODEL),
            ]
        )
    for label, env_var, p in required:
        if p is None or not p.is_file():
            raise RuntimeError(f"REQUIRED_LOCAL_RUNTIME_MISSING: {label} (configure {env_var}; resolved={p})")

    if args.scratch_root is not None:
        args.scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="factory-v06-e2e-", dir=args.scratch_root) as td:
        tmp = Path(td)
        creds_dir = tmp / "creds"
        creds_dir.mkdir(parents=True, exist_ok=True)
        used: set[int] = set()
        pg_port = free_port(used); used.add(pg_port)
        kc_port = free_port(used); used.add(kc_port)
        kc_mgmt_port = free_port(used); used.add(kc_mgmt_port)
        ai_port = free_port(used); used.add(ai_port)
        stage_db = "factory_ingest_stage_" + secrets.token_hex(6)

        pgdata = None
        kc_proc = None
        kc_log = None
        llm_proc = None
        llm_log = None
        summary: dict = {"status": "starting", "ephemeral_ports": {"pg": pg_port, "keycloak": kc_port, "ai": ai_port, "app": STAGING_APP_PORT}}
        try:
            print(f"[BOOT] Starting ephemeral PG 18.7 on 127.0.0.1:{pg_port}...", flush=True)
            pgdata, pglog, pgctl_log = start_ephemeral_pg(tmp, creds_dir, pg_port, stage_db)
            print(f"[BOOT] Starting ephemeral Keycloak 26 (dev-mem) on 127.0.0.1:{kc_port}...", flush=True)
            kc_proc, kc_log, kc_url = start_ephemeral_keycloak(tmp, creds_dir, kc_port, kc_mgmt_port)
            subs = provision_ephemeral_keycloak(kc_url, creds_dir)
            print(f"[BOOT] Provisioned realm {REALM} with {len(subs)} synthetic users.", flush=True)

            base_env = os.environ.copy()
            base_env.update(
                {
                    "FACTORY_V06_UNIFIED_STAGING": "1",
                    "FACTORY_PG_PORT": str(pg_port),
                    "FACTORY_PG_CREDS_DIR": str(creds_dir),
                    "FACTORY_P2_KEYCLOAK_PORT": str(kc_port),
                    "FACTORY_P2_CREDS_DIR": str(creds_dir),
                    "TEMP": str(tmp),
                    "TMP": str(tmp),
                    "PYTHONUTF8": "1",
                    "NO_PROXY": "localhost,127.0.0.1",
                    "no_proxy": "localhost,127.0.0.1",
                }
            )

            if not args.e2e_only:
                scratch_suites = tmp / "suites_scratch"
                scratch_suites.mkdir(exist_ok=True)
                summary["suites"] = run_existing_isolated_suites(base_env, scratch_suites)

            if not args.suite_only:
                print(f"[BOOT] Starting ephemeral llama-server.exe (MiniCPM5-2B) on 127.0.0.1:{ai_port}...", flush=True)
                llm_proc, llm_log, ai_key_file, ai_pid_file, ai_log_file = start_ephemeral_llm(tmp, ai_port)
                print(f"[BOOT] llama-server ready on 127.0.0.1:{ai_port}.", flush=True)
                summary["e2e"] = run_full_p0_e2e(
                    tmp, creds_dir, pgdata, pglog, pgctl_log, pg_port, kc_port, kc_url, stage_db, subs, ai_port, ai_key_file, ai_pid_file, ai_log_file
                )

            summary["status"] = "passed"
            print("ISOLATED_V06_ACCEPTANCE_SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)
            return 0
        finally:
            print("[TEARDOWN] Stopping ephemeral LLM, Keycloak, and PostgreSQL...", flush=True)
            kill_tree(llm_proc)
            if llm_log is not None:
                llm_log.close()
            kill_tree(kc_proc)
            if kc_log is not None:
                kc_log.close()
            stop_ephemeral_pg(pgdata)


if __name__ == "__main__":
    raise SystemExit(main())
