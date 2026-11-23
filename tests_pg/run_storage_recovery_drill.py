"""Restore public fixture data into fresh PostgreSQL and Qdrant targets."""
import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import time
from unittest.mock import patch

import httpx
from psycopg import sql
from qdrant_client import QdrantClient, models

from core06 import hybrid_retrieval as hybrid, ingest_index_worker
from core06.db_client import _creds_dir, database_session
from core06.ingest_service import prepare_upload, persist_upload
from tests_pg.native_vector_fixture import native_vector
from tools.check_backup_restore import backup_digest, cli, verify_restored_gateway

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ("schema.sql", "p2_oidc.sql", "ingestion_v1.sql", "ingestion_v2.sql",
              "ingestion_v3_purge.sql", "cad_kit_v1.sql")


@contextmanager
def scratch_database(name):
    with database_session("postgres", admin=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with database_session("postgres", admin=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class FixtureEncoder:
    def embed(self, texts, query=False):
        return [[1.0] + [0.0] * (hybrid.VECTOR_SIZE - 1) for _ in texts]


def check(condition, code):
    if not condition:
        raise RuntimeError(code)


def verify_vector_snapshot(executable, source, collection, original_points, folder, verify_search):
    with httpx.Client(timeout=60, trust_env=False) as http:
        started = time.perf_counter()
        created = http.post(f"{source}/collections/{collection}/snapshots", params={"wait": "true"})
        check(created.is_success, "FIXTURE_SNAPSHOT_CREATE_FAILED " + created.text[:1200])
        created.raise_for_status()
        snapshot = created.json()["result"]
        uri = f"{source}/collections/{collection}/snapshots/{snapshot['name']}"
        archive = folder / "fixture.snapshot"
        with http.stream("GET", uri) as response, archive.open("wb") as output:
            response.raise_for_status()
            digest = hashlib.sha256()
            for chunk in response.iter_bytes():
                digest.update(chunk)
                output.write(chunk)
        sha256 = digest.hexdigest()
        check(archive.stat().st_size > 0 and sha256 == snapshot["checksum"], "SNAPSHOT_DIGEST_MISMATCH")
        capture_ms = round((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        with native_vector(executable, scratch_root=folder.parent) as restored_port:
            restored = f"http://127.0.0.1:{restored_port}"
            restore_path = f"{restored}/collections/{collection}/snapshots/upload"
            with archive.open("rb") as content:
                bad = http.post(restore_path, files={"snapshot": content},
                                params={"checksum": "0" * 64, "priority": "snapshot"})
            check(bad.status_code == 400, "SNAPSHOT_BAD_CHECKSUM_NOT_REJECTED")
            restore_started = time.perf_counter()
            with archive.open("rb") as content:
                good = http.post(restore_path, files={"snapshot": content},
                                 params={"checksum": sha256, "priority": "snapshot", "wait": "true"})
            good.raise_for_status()
            check(good.json()["result"] is True, "SNAPSHOT_RESTORE_NOT_CONFIRMED")
            with closing(QdrantClient(url=restored, trust_env=False, check_compatibility=False)) as client:
                count = client.count(collection, exact=True).count
                recovered, next_page = client.scroll(collection, with_vectors=True)
                check(not next_page and count == len(original_points) and recovered == original_points,
                      "RESTORED_VECTOR_CONTENT_MISMATCH")
                verify_search(client)
            restore_ms = round((time.perf_counter() - restore_started) * 1000)
        isolated_ms = round((time.perf_counter() - started) * 1000)
    return {"snapshot_bytes": archive.stat().st_size, "sha256": sha256, "points": count,
            "bad_checksum_http": bad.status_code, "capture_ms": capture_ms,
            "restore_and_compare_ms": restore_ms, "target_lifecycle_ms": isolated_ms,
            "restored_search_rls": True}


def drill(executable, folder):
    stamp = secrets.token_hex(6)
    name = "s62_" + stamp
    # This CLI starts before substituting a collection and never uses the primary Qdrant client.
    hybrid.COLLECTION = name
    upload = prepare_upload("fixture.txt", "Recovery drill", b"Public fixture recovery reference SOL62-DR")
    password = _creds_dir() / "admin-password.txt"
    with ExitStack() as resources:
        source_db = resources.enter_context(scratch_database("factory_ingest_stage_" + stamp))
        target_db = resources.enter_context(scratch_database("factory_recovery_" + stamp))
        port = resources.enter_context(native_vector(executable, scratch_root=folder.parent))
        url = f"http://127.0.0.1:{port}"
        client = resources.enter_context(closing(QdrantClient(
            url=url, trust_env=False, check_compatibility=False, timeout=30,
        )))
        with database_session(source_db, admin=True) as source:
            for file in MIGRATIONS:
                source.execute((ROOT / "core06" / file).read_text(encoding="utf-8-sig"))
            source.execute("INSERT INTO core.subject(id) VALUES ('engineer'), ('finance')")
            source.execute("INSERT INTO core.scope(id) VALUES ('engineering')")
            source.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES ('engineer','engineering')")
            source.execute("INSERT INTO core.scope_write_grant(subject_id,scope_id) VALUES ('engineer','engineering')")
        with database_session(source_db) as gateway:
            doc_id = persist_upload(gateway, "engineer", "engineering", upload, "recovery-fixture-upload-key")

        def source_session(admin=False):
            return database_session(source_db, admin=admin)

        class UnavailableVector:
            def get_collections(self):
                raise ConnectionError("fixture outage")

        try:
            ingest_index_worker.process_one(source_session, FixtureEncoder(), UnavailableVector())
        except ConnectionError:
            pass
        else:
            raise RuntimeError("OUTAGE_DID_NOT_PREVENT_CLAIM")
        with source_session(admin=True) as source:
            check(source.execute("SELECT status, attempts FROM core.ingest_outbox").fetchone() == ("pending", 0),
                  "OUTAGE_SPENT_RETRY")
            source.execute("""
                UPDATE core.ingest_outbox SET status='processing', attempts=1,
                    lease_token='dead-worker', updated_at=now()-interval '11 minutes'
            """)
        hybrid.ensure_collection(client)
        check(ingest_index_worker.process_one(source_session, FixtureEncoder(), client).result == "indexed",
              "STALE_LEASE_NOT_RECOVERED")
        with source_session(admin=True) as source:
            check(source.execute("SELECT status, attempts, lease_token FROM core.ingest_outbox").fetchone()
                  == ("indexed", 2, None), "RECOVERED_QUEUE_STATE_INVALID")
        points, next_page = client.scroll(name, with_vectors=True)
        check(not next_page and len(points) == 1 and set(points[0].payload) == hybrid.PAYLOAD_KEYS,
              "VECTOR_PAYLOAD_UNEXPECTED")
        check(isinstance(points[0].vector["sparse"], models.SparseVector), "SPARSE_VECTOR_MISSING")

        dump = folder / "fixture.dump"
        started = time.perf_counter()
        cli("pg_dump.exe", source_db, ["--no-password", "--format=custom", "--file", dump], password)
        dump_ms = round((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        cli("pg_restore.exe", target_db, ["--no-password", "--exit-on-error", dump], password)
        verify_restored_gateway(target_db)
        with database_session(target_db) as gateway, gateway.transaction():
            gateway.execute("SELECT set_config('factory.actor_id','engineer',true)")
            record = gateway.execute(
                "SELECT original_bytes, content_sha256 FROM core.document_blob WHERE document_id=%s",
                (doc_id,),
            ).fetchone()
            check(record == (upload.original_bytes, hashlib.sha256(upload.original_bytes).hexdigest()),
                  "RESTORED_ORIGINAL_MISMATCH")
            gateway.execute("SELECT set_config('factory.actor_id','finance',true)")
            check(gateway.execute("SELECT count(*) FROM core.document_blob").fetchone() == (0,),
                  "RESTORED_FINANCE_SAW_ORIGINAL")
        postgres = {"dump_ms": dump_ms, "restore_and_verify_ms": round((time.perf_counter()-started)*1000),
                    "dump_bytes": dump.stat().st_size, "sha256": backup_digest(dump),
                    "gateway_rls": True, "authorized_original_sha256_matches": True}
        print(json.dumps({"postgres_checkpoint": postgres, "outage_attempts": 0,
                          "reclaimed_lease_attempts": 2}), flush=True)

        def verify_search(restored_client):
            retriever = hybrid.PolicyHybridRetriever(FixtureEncoder(), restored_client)
            with patch.object(hybrid, "database_session", side_effect=lambda: database_session(target_db)):
                query = "SOL62-DR"
                hits = retriever.search("engineer", query)
                check([hit.document_id for hit in hits] == [doc_id], "RESTORED_SEARCH_MISSING_SOURCE")
                check(not retriever.search("finance", query), "RESTORED_SEARCH_FINANCE_SAW_SOURCE")
                with database_session(target_db, admin=True) as restored_db:
                    restored_db.execute("SELECT core.revoke_scope_access('engineer','engineering','drill')")
                check(not retriever.search("engineer", query), "RESTORED_SEARCH_EXPOSED_REVOKED_SOURCE")
                check(restored_client.count(name, exact=True).count == 1, "REVOKE_CHANGED_RESTORED_VECTOR")

        qdrant = verify_vector_snapshot(executable, url, name, points, folder, verify_search)
    return {"status": "passed", "postgres": postgres, "qdrant": qdrant,
            "outage_attempts": 0, "reclaimed_lease_attempts": 2, "scratch_cleanup": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-exe", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, help="Existing directory with space for two Qdrant stores")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="factory-recovery-fixture-", dir=args.scratch_root) as folder:
        print(json.dumps(drill(args.qdrant_exe, Path(folder)), indent=2))
