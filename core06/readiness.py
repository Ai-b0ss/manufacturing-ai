"""Read-only checks for the dependencies of protected Unified routes."""
import httpx

from core06.db_client import database_session
from core06.hybrid_retrieval import COLLECTION, VECTOR_SIZE, qdrant_client
from p2_gateway.security import ISSUER, JWKS_URL


def postgres_ready():
    with database_session() as db, db.transaction():
        db.execute("SET LOCAL statement_timeout = '2s'")
        active, _ = db.execute(
            "SELECT row_security_active('core.document'), "
            "(SELECT 1 FROM core.document LIMIT 1)"
        ).fetchone()
        return active is True


def vector_ready():
    with_vector = qdrant_client(timeout=3)
    try:
        info = with_vector.get_collection(COLLECTION)
        dense = info.config.params.vectors
        return (info.status.value in {"green", "yellow"}
                and isinstance(dense, dict) and "dense" in dense
                and dense["dense"].size == VECTOR_SIZE
                and "sparse" in (info.config.params.sparse_vectors or {}))
    finally:
        with_vector.close()


def oidc_ready():
    with httpx.Client(timeout=3, trust_env=False) as client:
        discovery = client.get(ISSUER + "/.well-known/openid-configuration")
        discovery.raise_for_status()
        metadata = discovery.json()
        if metadata.get("issuer") != ISSUER or metadata.get("jwks_uri") != JWKS_URL:
            return False
        keys = client.get(JWKS_URL)
        keys.raise_for_status()
        return bool(keys.json().get("keys"))


def check_dependencies():
    dependencies = {
        "postgres_policy": postgres_ready,
        "qdrant_collection": vector_ready,
        "keycloak_oidc": oidc_ready,
    }
    statuses = {}
    for name, probe in dependencies.items():
        try:
            statuses[name] = bool(probe())
        except Exception:
            statuses[name] = False  # Never return connection details or credentials.
    return statuses
