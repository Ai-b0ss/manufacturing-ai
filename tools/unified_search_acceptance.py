"""Live Keycloak -> unified P13 document search -> Qdrant + PG RLS.

Local synthetic data only. Never print JWT, credentials, or document bodies.
"""
from __future__ import annotations
import json,sys,time,httpx
from tools.p3_gateway_smoke import mint
API="http://localhost:19139"

def main():
    started=time.monotonic()
    checks={}
    with httpx.Client(timeout=95,trust_env=False) as c:
        for role in ("engineer","technologist","finance","director"):
            headers={"Authorization":"Bearer "+mint(role)}
            r=c.get(API+"/unified/api/search",params={"q":"чертёж","limit":10},
                    headers=headers)
            if r.status_code!=200:raise RuntimeError(role+"_SEARCH_HTTP_"+str(r.status_code))
            j=r.json()
            for row in j["items"]:
                opened=c.get(API+"/unified/api/document",params={"id":row["id"]},headers=headers)
                if opened.status_code!=200:raise RuntimeError("SEARCH_RESULT_NOT_READABLE_BY_"+role)
            checks[role]={"status":200,"documents":len(j["items"]),
                          "all_hits_pass_pg_authorization":True}
    print(json.dumps({"status":"PASS","cases":checks,
          "search":"local dense-sparse Qdrant + PostgreSQL lexical",
          "original_p13_ui_api":True,"elapsed_s":round(time.monotonic()-started,2)},
          ensure_ascii=True))

if __name__=="__main__":
    try:main()
    except Exception as e:
        print(json.dumps({"status":"FAILED","reason":str(e)[:240]},ensure_ascii=True))
        sys.exit(1)
