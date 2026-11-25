"""Live signed Keycloak/OIDC -> P3 FastAPI -> Qdrant -> PostgreSQL RLS.

Credentials remain on host outside Git; JWTs and document bodies are never logged.
Tests all four synthetic users; no modification of source DB or Keycloak.
"""
from __future__ import annotations
import json,sys,time
from pathlib import Path
import httpx
from core06.db_client import database_session

CREDS=Path(r"K:\ManufacturingAI-CoreRuntime\credentials")
ISSUER="http://127.0.0.1:18080/realms/factory-core-v06"
API="http://127.0.0.1:19138"

def mint(name):
    from p2_gateway.security import ISSUER as p2_issuer, _creds_dir
    creds=_creds_dir()
    pwd=json.loads((creds/"keycloak-demo-users.json").read_text(encoding="utf-8"))[name]
    secret=(creds/"keycloak-e2e-client-secret.txt").read_text(encoding="ascii").strip()
    with httpx.Client(timeout=15,trust_env=False) as c:
        r=c.post(p2_issuer+"/protocol/openid-connect/token",
          data={"grant_type":"password","client_id":"factory-e2e",
                "client_secret":secret,"username":name,"password":pwd})
        if r.status_code!=200:raise RuntimeError("KEYCLOAK_TEST_USER_AUTH_FAILED:"+name)
        return r.json()["access_token"]

def main():
    with database_session(admin=True) as db:
        row=db.execute("""SELECT c.body,c.document_id
                  FROM core.document_chunk c JOIN core.document d ON d.id=c.document_id
                  WHERE d.scope_id='executive' ORDER BY c.id LIMIT 1""").fetchone()
    if not row:raise RuntimeError("NO_EXECUTIVE_DEMO_FIXTURE")
    q,secret_doc=row[0][:140],row[1]
    results={}
    with httpx.Client(timeout=95,trust_env=False) as client:
        anon=client.get(API+"/api/hybrid-search",params={"q":q})
        if anon.status_code!=401:raise RuntimeError("ANONYMOUS_SEARCH_ACCEPTED")
        for name in ("engineer","technologist","finance","director"):
            token=mint(name)
            response=client.get(API+"/api/hybrid-search",
                                params={"q":q,"limit":13},
                                headers={"Authorization":"Bearer "+token})
            if response.status_code!=200:
                raise RuntimeError("AUTHORIZED_SEARCH_FAILED:"+name+":"+str(response.status_code))
            j=response.json()
            docs={hit["document_id"] for hit in j["results"]}
            secret_visible=secret_doc in docs
            if (name=="director")!=secret_visible:
                raise RuntimeError("RLS_INCORRECT:"+name)
            if response.headers.get("cache-control")!="no-store, max-age=0":
                raise RuntimeError("API_CACHE_POLICY_MISSING")
            if any(not isinstance(hit.get("text"),str) for hit in j["results"]):
                raise RuntimeError("TEXT_HYDRATION_INCOMPLETE")
            if name=="engineer":
                spoof=client.get(API+"/api/hybrid-search",
                    params={"q":q,"limit":13,"actor_id":"director"},
                    headers={"Authorization":"Bearer "+token,
                             "X-Actor-Id":"director"})
                if spoof.status_code!=200 or any(h["document_id"]==secret_doc
                                   for h in spoof.json()["results"]):
                    raise RuntimeError("ACTOR_IMPERSONATION_IN_P3")
            results[name]={"status":response.status_code,
                  "hits":j["count"],"executive_visible":secret_visible}
    print(json.dumps({"status":"PASS","auth":"real Keycloak RS256+introspection",
         "route":"/api/hybrid-search","users":results,"anonymous_denied":True,
         "actor_spoof_denied":True,"qdrant":"real local Docker",
         "hydration":"PostgreSQL RLS"},ensure_ascii=True))

if __name__=="__main__":
    try:main()
    except Exception as e:
        print(json.dumps({"status":"FAILED","type":type(e).__name__,
            "detail":str(e)[:280]},ensure_ascii=True))
        sys.exit(1)
