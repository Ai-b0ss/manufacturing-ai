"""OIDC verification boundary. No identity is trusted without a verified signature.

Fixed, operator-controlled issuer/JWKS/audience. Requests never supply a JWKS
URL. No algorithm confusion, unsigned tokens, or username-derived identity.
"""
from __future__ import annotations
from dataclasses import dataclass
import json,re,time
from typing import Callable
import httpx
import jwt
from jwt import PyJWK

class AuthenticationError(Exception):pass
class ConfigurationError(Exception):pass

@dataclass(frozen=True)
class OIDCConfig:
    issuer:str
    audience:str
    jwks_url:str
    required_azp:str|None=None
    allow_insecure_loopback:bool=False

    def validate(self):
        if not self.issuer or not self.audience or not self.jwks_url:
            raise ConfigurationError("OIDC_NOT_CONFIGURED")
        if not self.issuer.startswith("https://"):
            if not(self.allow_insecure_loopback and self.issuer.startswith("http://127.0.0.1:")):
                raise ConfigurationError("OIDC_ISSUER_MUST_BE_HTTPS")
        if not self.jwks_url.startswith("https://"):
            if not(self.allow_insecure_loopback and self.jwks_url.startswith("http://127.0.0.1:")):
                raise ConfigurationError("OIDC_JWKS_MUST_BE_HTTPS")
        if "#" in self.issuer or "?" in self.issuer:
            raise ConfigurationError("INVALID_ISSUER")
        return self

@dataclass(frozen=True)
class VerifiedIdentity:
    issuer:str
    sub:str
    audience:str
    expires_at:int

class OIDCVerifier:
    def __init__(self,config:OIDCConfig,fetch_jwks:Callable[[],dict]|None=None):
        self.config=config.validate()
        self._fetch=fetch_jwks or self._fetch_live
        self._cached={}
        self._expires=0.0

    def _fetch_live(self):
        with httpx.Client(timeout=4,follow_redirects=False,trust_env=False) as client:
            response=client.get(self.config.jwks_url,headers={"Accept":"application/json"})
            response.raise_for_status()
            if len(response.content)>131072:raise AuthenticationError("JWKS_TOO_LARGE")
            return response.json()

    def _key(self,kid):
        now=time.monotonic()
        if now>=self._expires or kid not in self._cached:
            try:
                document=self._fetch()
                keys=document["keys"]
                if not isinstance(keys,list) or len(keys)>40:
                    raise AuthenticationError("JWKS_INVALID")
                valid={}
                for k in keys:
                    if not isinstance(k,dict) or k.get("kty")!="RSA" or k.get("alg","RS256")!="RS256":
                        continue
                    if k.get("use","sig")!="sig" or not isinstance(k.get("kid"),str):
                        continue
                    if len(k["kid"])>150:continue
                    valid[k["kid"]]=PyJWK.from_dict(k,algorithm="RS256").key
                if not valid:raise AuthenticationError("JWKS_EMPTY")
                self._cached=valid
                self._expires=now+60
            except Exception as e:
                self._cached={}
                self._expires=0
                raise AuthenticationError("JWKS_UNAVAILABLE") from e
        if kid not in self._cached:raise AuthenticationError("UNKNOWN_KID")
        return self._cached[kid]

    def verify(self,token:str)->VerifiedIdentity:
        if not isinstance(token,str) or not 20<len(token)<=12000:
            raise AuthenticationError("TOKEN_INVALID")
        try:
            header=jwt.get_unverified_header(token)
            if header.get("alg")!="RS256" or not isinstance(header.get("kid"),str):
                raise AuthenticationError("TOKEN_ALGORITHM_REJECTED")
            if header.get("crit") or header.get("jku") or header.get("x5u"):
                raise AuthenticationError("UNTRUSTED_JWT_HEADER")
            key=self._key(header["kid"])
            payload=jwt.decode(token,key,algorithms=["RS256"],
                issuer=self.config.issuer,audience=self.config.audience,
                options={"require":["exp","iat","nbf","iss","aud","sub"],"verify_signature":True},
                leeway=10)
            sub=payload.get("sub")
            if not isinstance(sub,str) or not 1<=len(sub)<=255:
                raise AuthenticationError("SUBJECT_INVALID")
            if self.config.required_azp and payload.get("azp")!=self.config.required_azp:
                raise AuthenticationError("CLIENT_NOT_ALLOWED")
            if int(payload["iat"])>int(time.time())+10:
                raise AuthenticationError("TOKEN_FROM_FUTURE")
            return VerifiedIdentity(payload["iss"],sub,self.config.audience,int(payload["exp"]))
        except AuthenticationError:raise
        except Exception as e:raise AuthenticationError("TOKEN_VERIFICATION_FAILED") from e
