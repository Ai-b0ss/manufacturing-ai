"""Transactional rollout of existing protected ingestion and versioned CAD kit.

Rehearse against a disposable pg_restore of a verified source backup.
Production apply is strictly opt-in; never migrates automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path

from psycopg import sql
from core06.db_client import database_session
from tools.provision_core import BIN,CREDS,DB,ROOT,HOST,PORT
from tools.check_backup_restore import cli

HERE=Path(__file__).resolve().parents[1]
FILE_NAMES=("ingestion_v1.sql","ingestion_v2.sql",
            "ingestion_v3_purge.sql","cad_kit_v1.sql")
EXPECTED_EXISTING=("document","document_chunk","engineering_source","engineering_bom_line",
                   "scope_grant","migration_manifest")
NEW_TABLES=("document_blob","ingest_outbox","scope_write_grant","cad_kit",
            "cad_kit_revision","cad_kit_request")
RECEIPT=HERE/"data"/"storage_rehearsal_receipt.json"

def scripts():
    results=[]
    for name in FILE_NAMES:
        p=HERE/"core06"/name
        content=p.read_text("utf-8-sig")
        lines=content.splitlines()
        assert sum(line.strip()=="BEGIN;" for line in lines)==1, name+"_BEGIN"
        assert sum(line.strip()=="COMMIT;" for line in lines)==1,name+"_COMMIT"
        filtered="\n".join(x for x in lines if x.strip() not in ("BEGIN;","COMMIT;"))
        results.append((name,filtered,hashlib.sha256(p.read_bytes()).hexdigest()))
    return results

def counts(db):
    return {name:db.execute(sql.SQL("SELECT count(*) FROM core.{}").format(sql.Identifier(name))).fetchone()[0]
            for name in EXPECTED_EXISTING}

def schema_state(db):
    output={}
    for name in NEW_TABLES:
        table=db.execute("SELECT to_regclass(%s)",("core."+name,)).fetchone()[0]
        if table is None:output[name]=None
        else:
            flags=db.execute("""SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_class c WHERE c.oid=%s::regclass""",(str(table),)).fetchone()
            output[name]=list(flags)
    return output

def check_new(db):
    state=schema_state(db)
    for name,flags in state.items():
        if flags!=[True,True]:raise RuntimeError("RLS_MISSING_"+name+":"+repr(flags))
    for table in ("document_blob","cad_kit","cad_kit_revision"):
        if db.execute(sql.SQL("SELECT count(*) FROM core.{}").format(sql.Identifier(table))).fetchone()[0]!=0:
            raise RuntimeError("MIGRATED_TABLE_HAS_UNEXPECTED_DATA_"+table)
    return state

def apply_scripts(db,steps):
    for name,command,_ in steps:
        db.execute(command)

def validate_backup(path,digest=None):
    p=Path(path)
    if not p.is_file() or p.stat().st_size<5000:
        raise RuntimeError("BACKUP_NOT_FOUND_OR_UNEXPECTED_SIZE")
    actual=hashlib.sha256(p.read_bytes()).hexdigest()
    if digest is not None and actual!=digest:
        raise RuntimeError("BACKUP_SHA256_MISMATCH")
    if not p.resolve().is_relative_to((ROOT/"backups").resolve()):
        raise RuntimeError("BACKUP_OUTSIDE_PROTECTED_DIRECTORY")
    return p,actual

class ForcedRollback(Exception):pass

def rehearse(backup,expected):
    archive,sha=validate_backup(backup,expected)
    steps=scripts()
    scratch="factory_storage_rehearse_"+secrets.token_hex(6)
    created=False
    manifest=None
    try:
        with database_session("postgres",admin=True) as sysdb:
            sysdb.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
            created=True
            sysdb.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO factory_owner").format(sql.Identifier(scratch)))
        cli("pg_restore.exe",scratch,
            ["--no-owner","--no-privileges","--role=factory_owner",
             "--exit-on-error",archive],CREDS/"admin-password.txt",timeout=100)
        with database_session(DB,admin=True) as source, database_session(scratch,admin=True) as target:
            live=counts(source)
            restored=counts(target)
            if live!=restored:raise RuntimeError("RESTORED_BASELINE_CHANGED")
            if any(schema_state(target).values()):
                raise RuntimeError("BACKUP_ALREADY_CONTAINS_MIGRATIONS")
            try:
                with target.transaction():
                    apply_scripts(target,steps)
                    check_new(target)
                    raise ForcedRollback()
            except ForcedRollback:pass
            if any(schema_state(target).values()):
                raise RuntimeError("TRANSACTION_ROLLBACK_LEFT_TABLES")
            if counts(target)!=live:
                raise RuntimeError("TRANSACTION_ROLLBACK_MODIFIED_ORIGINAL_ROWS")
            with target.transaction():
                apply_scripts(target,steps)
                state=check_new(target)
            check_new(target)
            if counts(target)!=live:
                raise RuntimeError("COMMITTED_MIGRATION_MODIFIED_ORIGINAL_ROWS")
        manifest={"status":"rehearsed","backup":str(archive),"backup_sha256":sha,
                  "schema_sha256":{name:digest for name,_,digest in steps},
                  "live_counts":live,"new_tables":state,
                  "atomic_rollback_verified":True,"live_database_unchanged":True,
                  "scratch_database_removed":True}
    finally:
        if created:
            with database_session("postgres",admin=True) as sysdb:
                sysdb.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch)))
                if sysdb.execute("SELECT 1 FROM pg_database WHERE datname=%s",(scratch,)).fetchone():
                    raise RuntimeError("SCRATCH_CLEANUP_FAILED")
    if manifest is not None:
        RECEIPT.write_text(json.dumps(manifest,indent=2,ensure_ascii=True),encoding="utf8")
        print(json.dumps(manifest,ensure_ascii=True))


def apply_live():
    if not RECEIPT.is_file():
        raise RuntimeError("MISSING_SUCCESSFUL_REHEARSAL")
    receipt=json.loads(RECEIPT.read_text("utf8"))
    if (receipt.get("status")!="rehearsed"
       or receipt.get("atomic_rollback_verified") is not True
       or receipt.get("scratch_database_removed") is not True):
        raise RuntimeError("REHEARSAL_NOT_VERIFIED")
    validate_backup(receipt["backup"],receipt["backup_sha256"])
    steps=scripts()
    if receipt["schema_sha256"]!={name:sha for name,_,sha in steps}:
        raise RuntimeError("SCHEMA_CHANGED_AFTER_REHEARSAL")
    before=receipt["live_counts"]
    with database_session(DB,admin=True) as db:
        if counts(db)!=before:
            raise RuntimeError("LIVE_SOURCE_CHANGED_SINCE_VERIFIED_BACKUP")
        if any(schema_state(db).values()):
            raise RuntimeError("LIVE_MIGRATION_ALREADY_PARTIAL_OR_APPLIED")
        grant=db.execute("""SELECT EXISTS (
             SELECT 1 FROM core.scope_grant
             WHERE subject_id='engineer' AND scope_id='engineering')""").fetchone()[0]
        if not grant:
            raise RuntimeError("ENGINEER_SCOPE_GRANT_NOT_PRESENT")
        try:
            with db.transaction():
                db.execute("SET LOCAL lock_timeout='7s'")
                db.execute("SET LOCAL statement_timeout='75s'")
                db.execute("SELECT pg_advisory_xact_lock(2026,11018)")
                if counts(db)!=before or any(schema_state(db).values()):
                    raise RuntimeError("LIVE_PRECONDITIONS_CHANGED_DURING_LOCK")
                apply_scripts(db,steps)
                check_new(db)
                db.execute("""INSERT INTO core.scope_write_grant(subject_id,scope_id)
                    VALUES('engineer','engineering')""")
                db.execute("SELECT set_config('factory.actor_id','engineer',true)")
                if not db.execute("SELECT core.can_write_scope('engineering')").fetchone()[0]:
                    raise RuntimeError("ENGINEER_WRITE_GRANT_NOT_EFFECTIVE")
                db.execute("SELECT set_config('factory.actor_id','finance',true)")
                if db.execute("SELECT core.can_write_scope('engineering')").fetchone()[0]:
                    raise RuntimeError("FINANCE_WRITE_LEAK")
                if counts(db)!=before:
                    raise RuntimeError("MIGRATION_MODIFIED_EXISTING_DOCUMENTS")
                # Gateway must write through SECURITY DEFINER only, never direct SQL.
                if db.execute("SELECT has_table_privilege('factory_gateway','core.cad_kit','INSERT')").fetchone()[0]:
                    raise RuntimeError("DIRECT_CAD_GATEWAY_WRITE_PERMISSION_LEAK")
                if db.execute("SELECT has_table_privilege('factory_gateway','core.document_blob','INSERT')").fetchone()[0]:
                    raise RuntimeError("DIRECT_DOCUMENT_GATEWAY_WRITE_PERMISSION_LEAK")
                # Transaction automatically commits after all assertions succeed.
        except Exception:
            # psycopg transaction context has already rolled back. Do not
            # attempt restoring over an active production database.
            raise
        final=check_new(db)
        if counts(db)!=before:
            raise RuntimeError("POSTCOMMIT_EXISTING_DOCUMENT_COUNT_CHANGED")
    outcome={"status":"applied","database":DB,
             "backup":receipt["backup"],"backup_sha256":receipt["backup_sha256"],
             "schema_sha256":receipt["schema_sha256"],"existing_rows":before,
             "new_tables":final,"engineer_write_scope":"engineering",
             "other_roles_not_granted":True,
             "transactional_migration":True,"backup_restore_rehearsed":True}
    outfile=HERE/"data"/"storage_live_migration_receipt.json"
    outfile.write_text(json.dumps(outcome,indent=2,ensure_ascii=True),encoding="utf8")
    print(json.dumps(outcome,ensure_ascii=True))


def status():
    with database_session(DB,admin=True) as db:
        print(json.dumps({"database":DB,"existing_rows":counts(db),
                "storage_tables":schema_state(db)},ensure_ascii=True))

def main():
    parser=argparse.ArgumentParser()
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rehearse",action="store_true")
    mode.add_argument("--status",action="store_true")
    mode.add_argument("--apply",action="store_true")
    parser.add_argument("--backup")
    parser.add_argument("--sha256")
    args=parser.parse_args()
    if args.status:status()
    elif args.apply:
        apply_live()
    else:
        if not args.backup or not args.sha256:parser.error("--backup and --sha256 required")
        rehearse(args.backup,args.sha256)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(json.dumps({"status":"failed","error":type(exc).__name__,
                          "message":str(exc)[:500]},ensure_ascii=True))
        sys.exit(1)
