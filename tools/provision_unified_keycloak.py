"""Ephemeral, isolated Keycloak test client for RP logout E2E acceptance.

Only creates/deletes client factory-web-unified, never touches factory-web,
factory-gateway, users or PostgreSQL. No credentials or tokens in output.
Use create -> run E2E -> delete. Delete refuses foreign configurations.
"""
from __future__ import annotations
from pathlib import Path
import json,sys

import httpx
SERVER="http://127.0.0.1:18080"
REALM="factory-core-v06"
CREDS=Path(r"K:\ManufacturingAI-CoreRuntime\credentials")
class Admin:
    def __init__(self):
        self.client=httpx.Client(timeout=10,trust_env=False,follow_redirects=False)
        password=(CREDS/"keycloak-bootstrap-admin.txt").read_text(encoding="ascii")
        reply=self.client.post(SERVER+"/realms/master/protocol/openid-connect/token",
            data={"grant_type":"password","client_id":"admin-cli",
                  "username":"factory_admin","password":password})
        if reply.status_code!=200:raise RuntimeError("KEYCLOAK_ADMIN_AUTH_FAILED")
        self.token=reply.json()["access_token"]
    def json(self,method,path,allowed=(200,201,204),**kwargs):
        if method not in ("GET","POST","DELETE"):raise RuntimeError("METHOD_NOT_ALLOWED")
        reply=self.client.request(method,SERVER+path,
             headers={"Authorization":"Bearer "+self.token},**kwargs)
        if reply.status_code not in allowed:
            raise RuntimeError("KEYCLOAK_HTTP_"+str(reply.status_code))
        return reply.json() if reply.content else None
    def audience(self,client_guid):
        path=f"/admin/realms/{REALM}/clients/{client_guid}/protocol-mappers/models"
        current=self.json("GET",path)
        if any(x.get("name")=="factory-gateway-audience" for x in current):return
        self.json("POST",path,allowed=(201,204),
            json={"name":"factory-gateway-audience","protocol":"openid-connect",
                  "protocolMapper":"oidc-audience-mapper","consentRequired":False,
                  "config":{"included.client.audience":"factory-gateway",
                            "id.token.claim":"false","access.token.claim":"true",
                            "introspection.token.claim":"true"}})
    def close(self):
        self.client.close()

CLIENT="factory-web-unified"
ORIGIN="http://localhost:19139"
REDIRECT=ORIGIN+"/auth/callback"
LOGOUT=ORIGIN+"/"

def lookup(admin):
    entries=admin.json("GET",f"/admin/realms/{REALM}/clients?clientId={CLIENT}")
    found=[x for x in entries if x.get("clientId")==CLIENT]
    if len(found)>1:raise RuntimeError("AUDIT_CLIENT_AMBIGUOUS")
    if not found:return None
    return admin.json("GET",f"/admin/realms/{REALM}/clients/{found[0]['id']}")

def match(doc):
    a=doc.get("attributes") or {}
    return doc.get("clientId")==CLIENT and doc.get("redirectUris")==[REDIRECT] and \
           doc.get("webOrigins")==[ORIGIN] and a.get("pkce.code.challenge.method")=="S256" and \
           a.get("post.logout.redirect.uris")==LOGOUT and doc.get("publicClient") is True

def main(action):
    adm=Admin()
    try:
        doc=lookup(adm)
        if action=="create":
            if doc:
                if match(doc):
                    return {"action":"exists","audit_client":CLIENT,"isolated":True,"logout_redirect_registered":True}
                raise RuntimeError("REFUSE_MUTATE_EXISTING_CLIENT_CONFIG")
            attrs={"pkce.code.challenge.method":"S256","post.logout.redirect.uris":LOGOUT}
            body={"clientId":CLIENT,"enabled":True,"protocol":"openid-connect",
                  "publicClient":True,"standardFlowEnabled":True,"implicitFlowEnabled":False,
                  "directAccessGrantsEnabled":False,"serviceAccountsEnabled":False,
                  "redirectUris":[REDIRECT],"webOrigins":[ORIGIN],"attributes":attrs}
            adm.json("POST",f"/admin/realms/{REALM}/clients",allowed=(201,204),json=body)
            doc=lookup(adm)
            if not doc or not match(doc):raise RuntimeError("CREATED_CLIENT_SETTINGS_INVALID")
            adm.audience(doc["id"])
            return {"action":"created","audit_client":CLIENT,"isolated":True,"logout_redirect_registered":True}
        if action=="verify":
            return {"action":"verified","exists":bool(doc),"valid_config":bool(doc and match(doc))}
        if action=="delete":
            if not doc:return {"action":"already_deleted","audit_client":CLIENT}
            if not match(doc):raise RuntimeError("REFUSE_DELETE_UNEXPECTED_CONFIG")
            adm.json("DELETE",f"/admin/realms/{REALM}/clients/{doc['id']}",allowed=(204,))
            return {"action":"deleted","audit_client":CLIENT,"other_clients_untouched":True}
        raise RuntimeError("UNKNOWN_ACTION")
    finally:adm.close()

if __name__=="__main__":
    try:print(json.dumps(main(sys.argv[1]),ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"error":type(e).__name__,"reason":str(e)[:280]},ensure_ascii=True))
        sys.exit(1)
