# P3 hybrid search acceptance — 17 November 2026

Isolated branch: feat/core-v06-p3-hybrid-retrieval-20261117.
Main P2 Gateway remains on localhost:19136; P3 test-only Gateway on 127.0.0.1:19138.
Actual PostgreSQL 18.7, Keycloak and Qdrant 1.20.2; synthetic demo data only.

## Completed real tests
- Local Docker Qdrant: bound 127.0.0.1:16333, API key protected outside Git, unauthorized 401, authorized 200; CPU/memory bounded; 13-point collection persisted in Docker named volume.
- Local multilingual-e5-small model from existing HuggingFace cache; 384-dim dense vectors, sparse hashed lexical features. No external model APIs.
- 13 PostgreSQL document chunks indexed with exactly tenant_id, document_id, chunk_id, acl_scope_id in Qdrant payload: no plaintext, epochs or membership.
- Own retrieval adapter: dense + sparse Qdrant IDs and PostgreSQL lexical matching, weighted reciprocal-rank fusion, PostgreSQL RLS hydration.
- Director sees executive document; engineer/technologist/finance do not. Forged Qdrant payload claiming public scope for a secret chunk does NOT bypass PostgreSQL RLS; unknown actor gets zero results. Local source-index test PASS, exit 0, 30.2 seconds.
- Real Keycloak signed JWT introspection through /api/hybrid-search, 4/4 synthetic users: engineer 11 authorized hits, technologist 9, finance 10, director 13. Anonymous 401; actor_id and X-Actor-Id impersonation blocked. Real API acceptance PASS, exit 0.
- Real Qdrant outage/recovery: 13 points before, HTTP 503 while Qdrant stopped, restart ONLY this container, 13 points after, API HTTP 200. PASS.
- All prior P2 systems and P6–P13 work independently.

## Limitations
P3 remains a local pilot on synthetic data, not production. Existing P2 19136 was NOT upgraded to P3; pilot 19138 is separate.
Model quality on Russian manufacturing corpus is not benchmarked. Sparse hashing is a pilot, not BGE-M3 sparse embeddings. Stored vectors themselves can leak information if an attacker obtains raw Qdrant access; API key + loopback is demo-only, TLS and network segmentation are needed. Tenant fixed to synthetic-demo, not yet a complete multi-tenant design. Indexing reconciliation, source OCR ingest, provenance and document revision coverage are not complete. Service runs CPU-only for pilot and model selection is not frozen. Qdrant search first-stage filter is not an authorization boundary. RLS-backed hydration is critical; the PostgreSQL factory.actor_id GUC is trusted-gateway-only.

## Reproduction
- tools/p3_qdrant_bootstrap.py — authenticated Qdrant Docker service (official pinned image)
- tools/p3_live_acceptance.py — index demo and forged vector ACL proof
- tools/p3_gateway_smoke.py — four real Keycloak users through P3 HTTP gateway
- tools/p3_recovery_drill.py — controlled outage and restoration
- core06/hybrid_retrieval.py — local E5 encoder, dense/sparse vectors, RRF, PostgreSQL authorization
- p2_gateway/app.py — isolated P3 route /api/hybrid-search
- .p3venv — local-only environment, not in Git
