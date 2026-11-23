"""Plan/apply RP-Initiated Logout redirect allowlist for the shared P2 Keycloak web client.

Dry-run by default. Mutates only factory-web post.logout.redirect.uris on
explicit --apply, after validating the current client's existing settings.
DO NOT apply while the other P2 agent is editing/provisioning the realm.
Never print credentials, tokens, user data, or client secrets.
"""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
import httpx

CREDS=Path(r"K:\ManufacturingAI-CoreRuntime\credentials")
SERVER="http://127.0.0.1:18080"
REALM="factory-core-v06"
CLIENT="factory-web"
REDIRECT="http://localhost:19136/auth/callback"
ORIGIN="http://localhost:19136"
POST_LOGOUT=ORIGIN+"/"

class UnsafeConfiguration(Exception):pass

def review_or_apply(apply=False):
    credentials=(CREDS/"keycloak-bootstrap-admin.txt").read_text(encoding="ascii")
    with httpx.Client(timeout=8,trust_env=False,follow_redirects=False) as client:
        auth=client.post(SERVER+"/realms/master/protocol/openid-connect/token",
             data={"client_id":"admin-cli","grant_type":"password",
                   "username":"factory_admin","password":credentials})
        if auth.status_code!=200:raise UnsafeConfiguration("ADMIN_AUTH_UNAVAILABLE")
        headers={"Authorization":"Bearer "+auth.json()["access_token"]}
        path=f"/admin/realms/{REALM}/clients"
        r=client.get(SERVER+path,params={"clientId":CLIENT},headers=headers)
        r.raise_for_status()
        matches=[c for c in r.json() if c.get("clientId")==CLIENT]
        if len(matches)!=1:raise UnsafeConfiguration("TARGET_CLIENT_NOT_UNIQUE")
        ident=matches[0]["id"]
        url=SERVER+path+"/"+ident
        r=client.get(url,headers=headers);r.raise_for_status()
        doc=r.json()
        attrs=doc.get("attributes") or {}
        if doc.get("clientId")!=CLIENT or doc.get("redirectUris")!=[REDIRECT] or \
           doc.get("webOrigins")!=[ORIGIN] or \
           doc.get("publicClient") is not True or \
           doc.get("standardFlowEnabled") is not True or \
           doc.get("directAccessGrantsEnabled") is not False or \
           attrs.get("pkce.code.challenge.method")!="S256":
            raise UnsafeConfiguration("UNEXPECTED_WEB_CLIENT_POLICY")
        current=attrs.get("post.logout.redirect.uris")
        if current==POST_LOGOUT:
            return {"action":"already_configured","client":CLIENT,"post_logout":POST_LOGOUT}
        if current not in (None,""):
            raise UnsafeConfiguration("POST_LOGOUT_ALREADY_CUSTOMIZED_MANUAL_REVIEW")
        if not apply:
            return {"action":"dry_run","client":CLIENT,"post_logout":POST_LOGOUT,
                    "requires_apply":True}
        attrs=dict(attrs)
        attrs["post.logout.redirect.uris"]=POST_LOGOUT
        r=client.put(url,json={**doc,"attributes":attrs},headers=headers)
        if r.status_code!=204:raise UnsafeConfiguration("CLIENT_UPDATE_FAILED")
        v=client.get(url,headers=headers);v.raise_for_status()
        actual=(v.json().get("attributes") or {}).get("post.logout.redirect.uris")
        if actual!=POST_LOGOUT:raise UnsafeConfiguration("POST_LOGOUT_RECHECK_FAILED")
        return {"action":"configured","client":CLIENT,"post_logout":POST_LOGOUT,
                "isolated_change":"one client attribute"}

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--apply",action="store_true",
                   help="apply only in a controlled deployment window")
    args=p.parse_args()
    try:print(json.dumps(review_or_apply(apply=args.apply),ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"action":"blocked","type":type(e).__name__,
                          "reason":str(e)[:180]},ensure_ascii=True))
        sys.exit(1)
