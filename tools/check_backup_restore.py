"""Run a real pg_dump + pg_restore recovery drill on disposable PostgreSQL DB.

Uses only the isolated v0.6 demo cluster; never mutates its source database.
"""
from __future__ import annotations
import hashlib,json,os,re,secrets,subprocess,sys,datetime
HIDDEN_STARTUP=subprocess.STARTUPINFO()
HIDDEN_STARTUP.dwFlags |= subprocess.STARTF_USESHOWWINDOW
HIDDEN_STARTUP.wShowWindow=subprocess.SW_HIDE
from pathlib import Path
from psycopg import sql
from core06.db_client import _creds_dir,_pg_port,database_session
from tools.provision_core import ROOT,BIN,CREDS,DB,HOST,PORT

def restrict_folder(path):
    raw=subprocess.run(["whoami","/user","/fo","csv","/nh"],capture_output=True,text=True,
                       encoding="utf-8",errors="replace",timeout=8,check=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=HIDDEN_STARTUP).stdout.strip()
    sid=raw.replace('"',"").split(",")[-1]
    if not re.fullmatch(r"S-1-5-[0-9-]+",sid):raise RuntimeError("SID_INVALID")
    r=subprocess.run(["icacls",str(path),"/inheritance:r","/grant:r",
                      f"*{sid}:(OI)(CI)F","*S-1-5-18:(OI)(CI)F","*S-1-5-32-544:(OI)(CI)F"],
                     capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW,
                       startupinfo=HIDDEN_STARTUP)
    if r.returncode:raise RuntimeError("BACKUP_ACL_FAILED")

def cli(tool,dbname,extra,password_file,timeout=75):
    env=os.environ.copy()
    env["PGPASSWORD"]=password_file.read_text(encoding="ascii")
    env["PGCONNECT_TIMEOUT"]="8"
    args=[str(BIN/tool),"-h",HOST,"-p",str(_pg_port()),"-U","factory_pg_admin",
          "-d",dbname,*map(str,extra)]
    r=subprocess.run(args,capture_output=True,text=True,encoding="utf-8",errors="replace",
                     timeout=timeout,env=env,creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=HIDDEN_STARTUP)
    if r.returncode:raise RuntimeError(tool+"_FAILED: "+r.stderr[-500:])
    return r.stdout

def count(db,table):
    return db.execute(sql.SQL("SELECT count(*) FROM core.{}").format(sql.Identifier(table))).fetchone()[0]


def backup_digest(path):
    with path.open("rb") as archive:
        return hashlib.file_digest(archive, "sha256").hexdigest()


def main():
    outdir=ROOT/"backups"
    outdir.mkdir(exist_ok=True)
    restrict_folder(outdir)
    temp=outdir/("tmp-"+secrets.token_hex(5)+".dump")
    name="factory_core_v06"
    scratch="factory_core_v06_restore_"+secrets.token_hex(4)
    admin_file=_creds_dir()/"admin-password.txt"
    created=False
    try:
        cli("pg_dump.exe",name,["--format=custom","--no-password",
                            "--file",temp],admin_file,timeout=75)
        if temp.stat().st_size<5000:raise RuntimeError("UNEXPECTED_DUMP_SIZE")
        digest=backup_digest(temp)
        with database_session("postgres",admin=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
            created=True
        cli("pg_restore.exe",scratch,["--no-password","--exit-on-error",temp],
            admin_file,timeout=90)
        tables=("subject","scope","scope_grant","document","document_chunk",
                "engineering_source","engineering_bom_line","migration_manifest",
                "scope_write_grant","document_blob","ingest_outbox",
                "cad_kit","cad_kit_revision","cad_kit_request")
        with database_session(name,admin=True) as source,database_session(scratch,admin=True) as target:
            left={key:count(source,key) for key in tables}
            right={key:count(target,key) for key in tables}
            if left!=right:raise RuntimeError("RESTORE_ROW_COUNT_MISMATCH")
            checksums_source=source.execute(
                "SELECT source_name,source_sha256 FROM core.migration_manifest ORDER BY source_name").fetchall()
            checksums_restored=target.execute(
                "SELECT source_name,source_sha256 FROM core.migration_manifest ORDER BY source_name").fetchall()
            if checksums_source!=checksums_restored:raise RuntimeError("RESTORE_MANIFEST_MISMATCH")
            original_queries=(
                ("upload_originals", """
                  SELECT document_id,content_sha256,
                         encode(sha256(original_bytes),'hex') AS calculated
                  FROM core.document_blob ORDER BY document_id"""),
                ("cad_packages", """
                  SELECT id,package_sha256,
                         encode(sha256(package),'hex') AS calculated
                  FROM core.cad_kit_revision ORDER BY id"""),
            )
            verified_originals={}
            for label,query in original_queries:
                source_rows=source.execute(query).fetchall()
                restored_rows=target.execute(query).fetchall()
                if source_rows!=restored_rows:
                    raise RuntimeError("RESTORED_ORIGINAL_HASH_MISMATCH_"+label)
                if any(expected!=calculated for _,expected,calculated in source_rows):
                    raise RuntimeError("CORRUPT_SOURCE_ORIGINAL_"+label)
                verified_originals[label]=len(source_rows)

        verify_restored_gateway(scratch)
        final=outdir/("factory_core_v06_"+digest[:12]+".dump")
        if not final.exists():temp.replace(final)
        elif backup_digest(final)!=digest:
            raise RuntimeError("EXISTING_BACKUP_HASH_MISMATCH")
        with database_session("postgres",admin=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch)))
            created=False
        print(json.dumps({"result":"restored_and_verified","backup":str(final),"sha256":digest,
                          "dump_bytes":final.stat().st_size,"counts":right,
                          "verified_originals":verified_originals,
                          "gateway_rls_active":True,
                          "temporary_database_removed":True},ensure_ascii=True))
    finally:
        if created:
            with database_session("postgres",admin=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch)))
        if temp.exists():temp.unlink()


def verify_restored_gateway(database):
    with database_session(database) as gateway, gateway.transaction():
        gateway.execute("SELECT set_config('factory.actor_id','',true)")
        rls, visible = gateway.execute("""
            SELECT row_security_active('core.document'),
                   (SELECT count(*) FROM core.document)
        """).fetchone()
        if (rls, visible) != (True, 0):
            raise RuntimeError("RESTORED_GATEWAY_POLICY_MISMATCH")

if __name__=="__main__":
    try:main()
    except Exception as e:
        print(json.dumps({"result":"failed","detail":str(e)[:900]},ensure_ascii=True))
        sys.exit(1)
