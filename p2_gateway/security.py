"""Strict authentication boundary for the local Keycloak-backed P2 gateway.

All access decisions use signed OIDC access tokens, Keycloak introspection,
and a PostgreSQL binding from issuer+sub to a stable internal subject.
No username, role, scope, request header or JSON actor_id can impersonate.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os,re
import httpx
import jwt
from fastapi import HTTPException

ISSUER="http://127.0.0.1:18080/realms/factory-core-v06"
TOKEN_ENDPOINT=ISSUER+"/protocol/openid-connect/token"
INTROSPECT_ENDPOINT=ISSUER+"/protocol/openid-connect/token/introspect"
END_SESSION_ENDPOINT=ISSUER+"/protocol/openid-connect/logout"
JWKS_URL=ISSUER+"/protocol/openid-connect/certs"
AUDIENCE="factory-gateway"
WEB_CLIENT=os.environ.get("FACTORY_P2_WEB_CLIENT","factory-web")
E2E_CLIENT="factory-e2e"
if not re.fullmatch(r"factory-web(?:-audit|-unified)?",WEB_CLIENT):
    raise RuntimeError("INVALID_TEST_ONLY_OIDC_CLIENT")
ALLOWED_CALLERS=frozenset((WEB_CLIENT,E2E_CLIENT))
CREDS=Path(r"K:\ManufacturingAI-CoreRuntime\credentials")

@dataclass(frozen=True)
class Principal:
    issuer:str
    external_sub:str
    client_id:str
    exp:int

class _LocalJWKClient(jwt.PyJWKClient):
    def fetch_data(self):
        last_exc=None
        for attempt in range(2):
            try:
                with httpx.Client(timeout=max(10,self.timeout),trust_env=False) as client:
                    resp=client.get(self.uri)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.TimeoutException as exc:
                last_exc=exc
                if attempt==0:
                    continue
            except Exception as exc:
                last_exc=exc
                break
        raise jwt.PyJWKClientConnectionError(
            f'Fail to fetch data from the url, err: "{type(last_exc).__name__}"'
        ) from last_exc

_JWKS=_LocalJWKClient(JWKS_URL,cache_keys=True,cache_jwk_set=True,lifespan=60,timeout=6)

def _err(code:int,reason:str):
    raise HTTPException(status_code=code,detail=reason)

def verify_access_token(value:str)->Principal:
    if not isinstance(value,str) or not 100<=len(value)<=8192:
        _err(401,"INVALID_BEARER_TOKEN")
    # Bearer access token is a compact JWT, not an opaque arbitrary string.
    if not re.fullmatch(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",value):
        _err(401,"INVALID_BEARER_TOKEN")
    try:
        header=jwt.get_unverified_header(value)
        if header.get("alg")!="RS256" or not isinstance(header.get("kid"),str):
            _err(401,"UNSUPPORTED_SIGNATURE_ALGORITHM")
        key=_JWKS.get_signing_key_from_jwt(value)
        claims=jwt.decode(value,key.key,algorithms=["RS256"],
                          issuer=ISSUER,audience=AUDIENCE,
                          options={"require":["exp","iat","iss","aud","sub"]},
                          leeway=3)
    except HTTPException:
        raise
    except jwt.PyJWKClientConnectionError:
        _err(503,"IDENTITY_JWKS_UNAVAILABLE")
    except jwt.PyJWTError:
        _err(401,"INVALID_BEARER_TOKEN")
    except Exception:
        _err(503,"IDENTITY_JWKS_UNAVAILABLE")
    if claims.get("typ")!="Bearer":
        _err(401,"NOT_AN_ACCESS_TOKEN")
    client_id=claims.get("azp")
    if client_id not in ALLOWED_CALLERS:
        _err(401,"UNTRUSTED_OIDC_CLIENT")
    sub=claims.get("sub")
    if not isinstance(sub,str) or not 8<=len(sub)<=180:
        _err(401,"INVALID_SUBJECT_CLAIM")
    # Mandatory introspection avoids accepting a token whose user/session was
    # disabled or logged out after JWT issuance. No stale approval cache.
    try:
        password=(CREDS/"keycloak-gateway-client-secret.txt").read_text(encoding="ascii")
        r=None
        for attempt in range(2):
            try:
                with httpx.Client(timeout=12,trust_env=False) as client:
                    r=client.post(INTROSPECT_ENDPOINT,
                         data={"token":value,"token_type_hint":"access_token"},
                         auth=(AUDIENCE,password))
                break
            except httpx.TimeoutException:
                if attempt==1:
                    raise
        if r is None or r.status_code!=200:
            _err(503,"KEYCLOAK_INTROSPECTION_FAILED")
        result=r.json()
        if result.get("active") is not True:
            _err(401,"TOKEN_REVOKED_OR_EXPIRED")
        if result.get("sub")!=sub:
            _err(401,"TOKEN_SUBJECT_MISMATCH")
        if result.get("client_id")!=client_id:
            _err(401,"TOKEN_CLIENT_MISMATCH")
    except HTTPException:
        raise
    except Exception:
        _err(503,"KEYCLOAK_INTROSPECTION_UNAVAILABLE")
    return Principal(issuer=ISSUER,external_sub=sub,client_id=client_id,exp=int(claims["exp"]))

def verify_id_token(value:str,nonce:str):
    if not value or len(value)>8192:
        _err(401,"ID_TOKEN_INVALID")
    try:
        header=jwt.get_unverified_header(value)
        if header.get("alg")!="RS256":
            _err(401,"ID_TOKEN_ALGORITHM_REJECTED")
        key=_JWKS.get_signing_key_from_jwt(value)
        claims=jwt.decode(value,key.key,algorithms=["RS256"],issuer=ISSUER,
                          audience=WEB_CLIENT,
                          options={"require":["exp","iat","iss","aud","sub","nonce"]},
                          leeway=3)
    except HTTPException:
        raise
    except jwt.PyJWKClientConnectionError:
        _err(503,"IDENTITY_JWKS_UNAVAILABLE")
    except jwt.PyJWTError:
        _err(401,"ID_TOKEN_INVALID")
    except Exception:
        _err(503,"IDENTITY_JWKS_UNAVAILABLE")
    import hmac
    if not hmac.compare_digest(str(claims["nonce"]),str(nonce)):
        _err(401,"ID_TOKEN_NONCE_MISMATCH")
    return claims
