"""Provision the isolated v0.6 database and least-privilege gateway role.

Only touches the private PostgreSQL 18.7 instance on 127.0.0.1:55436.
All secrets are in K:\\ManufacturingAI-CoreRuntime\\credentials, never Git.
"""
from __future__ import annotations
import json,os,re,secrets,subprocess,sys
from pathlib import Path

from core06.db_client import _creds_dir, _pg_port

ROOT=Path(r"K:\ManufacturingAI-CoreRuntime")
BIN=ROOT/"pg18"/"bin"
CREDS=ROOT/"credentials"
DB="factory_core_v06"
HOST="127.0.0.1"
PORT=_pg_port()
HERE=Path(__file__).resolve().parent.parent

def _hidden_startupinfo():
    if not hasattr(subprocess,"STARTUPINFO"):
        return None
    info=subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess,"STARTF_USESHOWWINDOW",0)
    info.wShowWindow=getattr(subprocess,"SW_HIDE",0)
    return info

def protect(path):
    no_win=getattr(subprocess,"CREATE_NO_WINDOW",0)
    sid=subprocess.run(["whoami","/user","/fo","csv","/nh"],capture_output=True,text=True,
                        encoding="utf-8",errors="replace",check=True,timeout=9,
                        creationflags=no_win,startupinfo=_hidden_startupinfo()).stdout.strip().replace('"',"").split(",")[-1]
    if not re.fullmatch(r"S-1-5-[\d-]+",sid):raise RuntimeError("INVALID_CURRENT_SID")
    r=subprocess.run(["icacls",str(path),"/inheritance:r","/grant:r",
                      f"*{sid}:F","*S-1-5-18:F","*S-1-5-32-544:F"],
                     capture_output=True,timeout=10,
                     creationflags=no_win,startupinfo=_hidden_startupinfo())
    if r.returncode!=0:raise RuntimeError("SECRET_ACL_ERROR")

def psql(db,query=None,file=None,as_gateway=False,timeout=35):
    if (query is None)==(file is None):raise ValueError("QUERY_XOR_FILE")
    env=os.environ.copy()
    env["PGPASSWORD"]=(_creds_dir()/("gateway-password.txt" if as_gateway else "admin-password.txt")).read_text(encoding="ascii")
    args=[str(BIN/"psql.exe"),"-X","-v","ON_ERROR_STOP=1","-A","-t",
          "-h",HOST,"-p",str(_pg_port()),"-U","factory_gateway" if as_gateway else "factory_pg_admin",
          "-d",db]
    args+=["-c",query] if query is not None else ["-f",str(file)]
    run=subprocess.run(args,capture_output=True,text=True,encoding="utf-8",errors="replace",
                       env=env,timeout=timeout,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
                       startupinfo=_hidden_startupinfo())
    if run.returncode:
        raise RuntimeError("PSQL_ERROR: "+run.stderr[-950:])
    return run.stdout.strip()

def main():
    if not (BIN/"psql.exe").exists() or not (CREDS/"admin-password.txt").exists():
        raise RuntimeError("PG_BOOTSTRAP_NOT_READY")
    gateway_pwd=CREDS/"gateway-password.txt"
    if not gateway_pwd.exists():gateway_pwd.write_text(secrets.token_urlsafe(50),encoding="ascii")
    protect(gateway_pwd)
    pwd=gateway_pwd.read_text(encoding="ascii")
    if not re.fullmatch(r"[A-Za-z0-9_-]{60,120}",pwd):
        raise RuntimeError("INVALID_STORED_GATEWAY_PASSWORD")
    setup=f"""
DO $$
BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='factory_owner') THEN
 CREATE ROLE factory_owner NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
END IF;
IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='factory_gateway') THEN
 CREATE ROLE factory_gateway LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
END IF;
END$$;
ALTER ROLE factory_gateway WITH LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB
 NOREPLICATION NOBYPASSRLS PASSWORD '{pwd}';
"""
    psql("postgres",query=setup)
    exists=psql("postgres",query="SELECT COUNT(*) FROM pg_database WHERE datname='"+DB+"'")
    if exists=="0":psql("postgres",query="CREATE DATABASE "+DB)
    elif exists!="1":raise RuntimeError("INVALID_DATABASE_STATE")
    psql("postgres",query="REVOKE ALL ON DATABASE "+DB+" FROM PUBLIC; GRANT CONNECT ON DATABASE "+DB+" TO factory_gateway;")
    sql_file=HERE/"core06"/"schema.sql"
    psql(DB,file=sql_file)
    info=psql(DB,query="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='core' AND c.relkind='r'")
    rls=psql(DB,query="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='core' AND c.relrowsecurity AND c.relforcerowsecurity")
    mode=psql(DB,query="SELECT current_user,current_database(),inet_server_addr(),inet_server_port()")
    gateway_test=psql(DB,query="SELECT current_user,current_setting('is_superuser'), (SELECT count(*) FROM core.document)",as_gateway=True)
    print(json.dumps({"result":"ready","schema_tables":int(info),"rls_forced_tables":int(rls),
                      "database":mode,"gateway_test":gateway_test,
                      "secret_file":str(gateway_pwd),"credentials_in_repo":False},ensure_ascii=True))

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(json.dumps({"result":"error","detail":str(exc)[:1100]},ensure_ascii=True))
        sys.exit(1)
