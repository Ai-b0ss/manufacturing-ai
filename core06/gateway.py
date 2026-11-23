"""Fail-closed FastAPI gateway: OIDC -> mapped internal subject -> PostgreSQL RLS.

No caller-controlled actor_id, SQL, scope, document ACL, or identity claims.
Designed to run behind TLS reverse proxy with Keycloak in a future deployment.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
import os
from fastapi import FastAPI,Depends,Header,HTTPException,Query,Request
from fastapi.responses import JSONResponse
from core06.oidc import OIDCConfig,OIDCVerifier,AuthenticationError,ConfigurationError
from core06.db_client import database_session,list_authorized_documents,read_authorized_document,search_authorized_chunks,authorized_finance

def build_verifier():
    config=OIDCConfig(
        issuer=os.environ.get("FACTORY_OIDC_ISSUER",""),
        audience=os.environ.get("FACTORY_OIDC_AUDIENCE",""),
        jwks_url=os.environ.get("FACTORY_OIDC_JWKS_URL",""),
        required_azp=os.environ.get("FACTORY_OIDC_AZP") or None,
        allow_insecure_loopback=os.environ.get("FACTORY_TEST_ALLOW_HTTP_LOOPBACK")=="1")
    return OIDCVerifier(config)

def create_app(verifier=None,db_factory=database_session):
    app=FastAPI(title="Factory Core v0.6 Policy Gateway",docs_url=None,redoc_url=None,
                openapi_url=None)
    app.state.verifier=verifier
    app.state.db_factory=db_factory

    @app.middleware("http")
    async def no_store(request:Request,call_next):
        response=await call_next(request)
        response.headers["Cache-Control"]="no-store, private"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Frame-Options"]="DENY"
        return response

    def authenticated(authorization:str|None=Header(default=None)):
        if not authorization or not authorization.startswith("Bearer ") or authorization.count(" ")!=1:
            raise HTTPException(401,"BEARER_TOKEN_REQUIRED",headers={"WWW-Authenticate":"Bearer"})
        try:
            verifier=app.state.verifier
            if verifier is None:raise ConfigurationError("OIDC_NOT_CONFIGURED")
            return verifier.verify(authorization[7:])
        except (AuthenticationError,ConfigurationError):
            raise HTTPException(401,"TOKEN_NOT_ACCEPTED",headers={"WWW-Authenticate":"Bearer"})

    def resolved_actor(identity=Depends(authenticated)):
        try:
            with app.state.db_factory() as db:
                row=db.execute("SELECT core.resolve_oidc_subject(%s,%s)",
                               (identity.issuer,identity.sub)).fetchone()
                actor=row[0] if row else None
                if not actor:raise HTTPException(403,"SUBJECT_NOT_PROVISIONED")
                return actor
        except HTTPException:raise
        except Exception:
            raise HTTPException(503,"POLICY_DATABASE_UNAVAILABLE")

    @app.get("/api/health")
    def health():
        return {"service":"factory-core-policy-gateway","status":"up",
                "oidc_configured":app.state.verifier is not None,
                "production_ready":False}

    @app.get("/api/v06/me")
    def me(actor:str=Depends(resolved_actor)):
        return {"subject":actor}

    @app.get("/api/v06/documents")
    def documents(limit:int=Query(default=30,ge=1,le=50),actor:str=Depends(resolved_actor)):
        try:
            with app.state.db_factory() as db:
                rows=list_authorized_documents(db,actor,limit)
                return {"documents":[dict(zip(("id","title","designation","type","revision"),r)) for r in rows]}
        except Exception:raise HTTPException(503,"POLICY_DATABASE_UNAVAILABLE")

    @app.get("/api/v06/documents/{doc_id}")
    def document(doc_id:str,actor:str=Depends(resolved_actor)):
        try:
            with app.state.db_factory() as db:
                record=read_authorized_document(db,actor,doc_id)
                if record is None:raise HTTPException(404,"DOCUMENT_NOT_FOUND")
                return {"document":dict(zip(("id","title","designation","type","revision"),record["document"])),
                        "chunks":[dict(zip(("id","text","page","method"),r)) for r in record["chunks"]]}
        except HTTPException:raise
        except Exception:raise HTTPException(503,"POLICY_DATABASE_UNAVAILABLE")

    @app.get("/api/v06/search")
    def search(q:str=Query(min_length=1,max_length=250),
               limit:int=Query(default=10,ge=1,le=50),actor:str=Depends(resolved_actor)):
        try:
            with app.state.db_factory() as db:
                rows=search_authorized_chunks(db,actor,q,limit)
                return {"hits":[{"chunk_id":r[0],"document_id":r[1],"rank":float(r[2])} for r in rows]}
        except Exception:raise HTTPException(503,"POLICY_DATABASE_UNAVAILABLE")

    @app.get("/api/v06/finance")
    def finance(limit:int=Query(default=30,ge=1,le=50),actor:str=Depends(resolved_actor)):
        try:
            with app.state.db_factory() as db:
                rows=authorized_finance(db,actor,limit)
                return {"metrics":[{"id":r[0],"title":r[1],"planned":str(r[2]),"actual":str(r[3])} for r in rows]}
        except Exception:raise HTTPException(503,"POLICY_DATABASE_UNAVAILABLE")

    return app

try:
    app=create_app(verifier=build_verifier())
except ConfigurationError:
    app=create_app()  # all protected endpoints fail closed until operator configures issuer
