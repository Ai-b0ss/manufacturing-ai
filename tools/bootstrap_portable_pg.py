"""Verified portable PostgreSQL bootstrap for the v0.6 engineering test stack.

Not a system-wide installation. No Docker, service registration, or PATH changes.
Downloads only official EnterpriseDB PostgreSQL binaries and official .sha256.
Separately initializes a private cluster on loopback port 55436.
Never emits credentials in stdout/logs. No restart of a running foreign cluster.
"""
from __future__ import annotations
import hashlib,json,os,re,secrets,subprocess,sys,time,urllib.request,zipfile
from pathlib import Path

URL="https://get.enterprisedb.com/postgresql/postgresql-18.7-1-windows-x64-binaries.zip"
ROOT=Path(os.environ.get("FACTORY_CORE_RUNTIME",r"K:\ManufacturingAI-CoreRuntime"))
_RAW_PG_PORT=os.environ.get("FACTORY_PG_PORT","55436").strip()
if not _RAW_PG_PORT.isascii() or not _RAW_PG_PORT.isdigit() or not 1024<=int(_RAW_PG_PORT)<=65535:
    raise RuntimeError("INVALID_FACTORY_PG_PORT")
PORT=int(_RAW_PG_PORT)
DOWNLOAD=ROOT/"downloads"/"postgresql-18.7-1-windows-x64-binaries.zip"
INSTALL=ROOT/"pg18"
DATA=ROOT/"pgdata"
SECRETS=ROOT/"credentials"
LOG=ROOT/"bootstrap-events.jsonl"

def log(event,**detail):
    ROOT.mkdir(parents=True,exist_ok=True)
    item={"time":time.strftime("%Y-%m-%dT%H:%M:%S"),"event":event,**detail}
    with LOG.open("a",encoding="utf-8") as f:f.write(json.dumps(item,ensure_ascii=True)+"\n")
    print(json.dumps(item,ensure_ascii=True),flush=True)

def _hidden_startupinfo():
    if not hasattr(subprocess,"STARTUPINFO"):
        return None
    info=subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess,"STARTF_USESHOWWINDOW",0)
    info.wShowWindow=getattr(subprocess,"SW_HIDE",0)
    return info

def restrict(path,is_dir=False):
    # Use well-known Windows SIDs: localized names such as Administrators
    # cannot be resolved on Russian-language Windows.
    no_win=getattr(subprocess,"CREATE_NO_WINDOW",0)
    u=subprocess.run(["whoami","/user","/fo","csv","/nh"],
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=10,
        creationflags=no_win,startupinfo=_hidden_startupinfo())
    if u.returncode!=0:raise RuntimeError("CANNOT_RESOLVE_USER_SID")
    sid=u.stdout.strip().replace('"',"").split(",")[-1]
    if not re.fullmatch(r"S-1-5-[0-9-]+",sid):
        raise RuntimeError("INVALID_USER_SID")
    suffix=":(OI)(CI)F" if is_dir else ":F"
    grants=[f"*{sid}{suffix}",f"*S-1-5-18{suffix}",f"*S-1-5-32-544{suffix}"]
    r=subprocess.run(["icacls",str(path),"/inheritance:r","/grant:r",*grants],
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=15,
        creationflags=no_win,startupinfo=_hidden_startupinfo())
    if r.returncode!=0:raise RuntimeError("CANNOT_RESTRICT_SECRETS_ACCESS")

def run(args,timeout=90):
    r=subprocess.run([str(a) for a in args],capture_output=True,text=True,encoding="utf-8",errors="replace",
        timeout=timeout,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),startupinfo=_hidden_startupinfo())
    if r.returncode!=0:
        log("subprocess_failure",command=Path(str(args[0])).name,rc=r.returncode,detail=(r.stderr or r.stdout)[-450:])
        raise RuntimeError("COMMAND_FAILED:"+Path(str(args[0])).name)
    return r.stdout.strip()

def port_open():
    import socket
    with socket.socket() as s:
        s.settimeout(.6)
        return s.connect_ex(("127.0.0.1",PORT))==0

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    SECRETS.mkdir(parents=True,exist_ok=True)
    restrict(SECRETS,is_dir=True)
    DOWNLOAD.parent.mkdir(parents=True,exist_ok=True)
    if port_open() and not (DATA/"postmaster.pid").exists():
        raise RuntimeError("PORT_55436_OWNED_BY_UNKNOWN_PROCESS")
    req=urllib.request.Request(URL+".sha256",headers={"User-Agent":"FactoryCore-v0.6-checksums"})
    with urllib.request.urlopen(req,timeout=15) as f:
        content=f.read(256).decode("ascii")
    m=re.fullmatch(r"([a-f0-9]{64})\s+\S+\.zip\s*",content)
    if not m:raise RuntimeError("INVALID_OFFICIAL_CHECKSUM")
    expected=m.group(1)
    log("official_checksum",sha256=expected)
    hashobj=hashlib.sha256()
    if DOWNLOAD.is_file():
        with DOWNLOAD.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""):hashobj.update(chunk)
        if hashobj.hexdigest()!=expected:
            DOWNLOAD.unlink();log("stale_download_deleted")
    if not DOWNLOAD.is_file():
        tmp=DOWNLOAD.with_suffix(".partial")
        if tmp.exists():tmp.unlink()
        req=urllib.request.Request(URL,headers={"User-Agent":"FactoryCore-v0.6-postgres"})
        log("download_begin",url=URL)
        try:
            with urllib.request.urlopen(req,timeout=60) as response, tmp.open("wb") as f:
                if response.status!=200:raise RuntimeError("DOWNLOAD_HTTP_ERROR")
                limit=700_000_000
                bytes_read=0
                digest=hashlib.sha256()
                last=time.monotonic()
                while chunk:=response.read(1024*1024):
                    bytes_read+=len(chunk)
                    if bytes_read>limit:raise RuntimeError("DOWNLOAD_TOO_LARGE")
                    f.write(chunk);digest.update(chunk)
                    if time.monotonic()-last>15:
                        log("download_progress",mb=round(bytes_read/1e6,1))
                        last=time.monotonic()
                if digest.hexdigest()!=expected:raise RuntimeError("DOWNLOAD_HASH_MISMATCH")
                if bytes_read<15_000_000:raise RuntimeError("DOWNLOAD_INCOMPLETE")
            tmp.replace(DOWNLOAD)
            log("download_verified",bytes=bytes_read)
        except:
            if tmp.exists():tmp.unlink()
            raise
    else:log("archive_verified",bytes=DOWNLOAD.stat().st_size)
    bin=INSTALL/"bin"
    if not (bin/"initdb.exe").exists() or not (bin/"postgres.exe").exists() or not (bin/"pg_ctl.exe").exists():
        import shutil
        stage=ROOT/"pg18-extracting"
        if stage.exists():shutil.rmtree(stage)
        stage.mkdir()
        target_folders=("pgsql/bin/","pgsql/lib/","pgsql/share/")
        n=0;size=0
        with zipfile.ZipFile(DOWNLOAD) as z:
            approved=[x for x in z.infolist() if x.filename.startswith(target_folders) and not x.is_dir()]
            if len(approved)>24000:raise RuntimeError("ARCHIVE_TOO_MANY_FILES")
            for info in approved:
                rel=Path(*Path(info.filename).parts[1:])
                if rel.is_absolute() or ".." in rel.parts:raise RuntimeError("ZIP_PATH_TRAVERSAL")
                dest=stage/rel
                if not dest.resolve().is_relative_to(stage.resolve()):raise RuntimeError("UNSAFE_ARCHIVE_PATH")
                size+=info.file_size
                if size>3_800_000_000:raise RuntimeError("ARCHIVE_UNPACK_BUDGET")
                dest.parent.mkdir(parents=True,exist_ok=True)
                with z.open(info) as src,dest.open("wb") as dst:shutil.copyfileobj(src,dst)
                n+=1
        if not (stage/"bin"/"initdb.exe").is_file():raise RuntimeError("UNEXPECTED_BINARY_ARCHIVE_LAYOUT")
        if INSTALL.exists():shutil.rmtree(INSTALL)
        stage.replace(INSTALL)
        log("binaries_extracted",files=n,bytes=size)
    log("postgres_version",version=run([bin/"postgres.exe","--version"]))
    admin_file=SECRETS/"admin-password.txt"
    if not admin_file.exists():
        admin_file.write_text(secrets.token_urlsafe(50),encoding="ascii")
    restrict(admin_file)
    if not (DATA/"PG_VERSION").is_file():
        if DATA.exists() and any(DATA.iterdir()):
            raise RuntimeError("REFUSE_TO_OVERWRITE_EXISTING_UNINITIALIZED_PGDATA")
        DATA.mkdir(parents=True,exist_ok=True)
        restrict(DATA,is_dir=True)
        run([bin/"initdb.exe","-D",DATA,"-U","factory_pg_admin","--encoding=UTF8","--auth-host=scram-sha-256","--auth-local=scram-sha-256","--pwfile="+str(admin_file),"--no-instructions"],timeout=180)
        log("cluster_initialized")
    config=DATA/"postgresql.conf"
    content=config.read_text(encoding="utf-8")
    # add reproducible and reload-safe settings once, never overwrite original config lines
    marker="\n# FACTORY_CORE_V06_LOOPBACK_SETTINGS\n"
    if marker not in content:
        with config.open("a",encoding="utf-8") as f:
            f.write(marker+"listen_addresses = '127.0.0.1'\nport = "+str(PORT)+"\n"+
                "max_connections = 40\nshared_buffers = '128MB'\n"+
                "log_connections = on\nlogging_collector = on\n"+
                "password_encryption = 'scram-sha-256'\n")
    service_name="FactoryCoreV06Pg"
    no_win=getattr(subprocess,"CREATE_NO_WINDOW",0)
    check=subprocess.run(["sc.exe","qc",service_name],capture_output=True,timeout=8,
        creationflags=no_win,startupinfo=_hidden_startupinfo())
    if check.returncode!=0:
        # Service account is an unprivileged well-known Windows identity.
        # pgdata is the only writable subtree.
        grant=subprocess.run(["icacls",str(DATA),"/grant",
                   "*S-1-5-19:(OI)(CI)F","/T","/C"],
                   capture_output=True,timeout=80,
                   creationflags=no_win,startupinfo=_hidden_startupinfo())
        if grant.returncode!=0:raise RuntimeError("CANNOT_GRANT_LOCAL_SERVICE_DATA")
        run([bin/"pg_ctl.exe","register","-N",service_name,"-D",DATA,
             "-U",r"NT AUTHORITY\LocalService","-S","demand",
             "-o",f"-h 127.0.0.1 -p {PORT}"],timeout=25)
        log("restricted_windows_service_registered",name=service_name,start="manual")
    if not port_open():
        process=subprocess.run(["sc.exe","start",service_name],capture_output=True,timeout=30,
            creationflags=no_win,startupinfo=_hidden_startupinfo())
        if process.returncode not in (0,1056):
            raise RuntimeError("POSTGRES_SERVICE_START_FAILED")
        import socket
        for i in range(45):
            if port_open():break
            time.sleep(.35)
        if not port_open():raise RuntimeError("POSTGRES_SERVICE_NOT_LISTENING")
        log("postgres_started",port=PORT,host="127.0.0.1",
            service=service_name,account="NT AUTHORITY\\LocalService")
    env=os.environ.copy()
    env["PGPASSWORD"]=admin_file.read_text(encoding="ascii")
    env["PGCONNECT_TIMEOUT"]="5"
    args=[bin/"psql.exe","-X","-A","-t","-h","127.0.0.1","-p",str(PORT),
          "-U","factory_pg_admin","-d","postgres","-c","SELECT current_setting('server_version'),inet_server_addr()::text,inet_server_port()"]
    r=subprocess.run([str(a) for a in args],capture_output=True,text=True,encoding="utf-8",errors="replace",
        env=env,timeout=12,creationflags=no_win,startupinfo=_hidden_startupinfo())
    if r.returncode!=0:raise RuntimeError("POSTGRES_FAILED_AUTH_OR_HEALTH: "+r.stderr[-200:])
    log("postgres_ready",check=r.stdout.strip())

if __name__=="__main__":
    try:main()
    except Exception as exc:
        log("failed",type=type(exc).__name__,reason=str(exc)[:300])
        sys.exit(1)
