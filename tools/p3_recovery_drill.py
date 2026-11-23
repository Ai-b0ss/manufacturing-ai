"""Qdrant persistent-volume and fail-closed recovery drill for synthetic P3 only.

Restarts ONLY the labeled factory-core-v06-qdrant-p3 container, never P2,
PostgreSQL, Keycloak, Docker Desktop, or other containers. Uses finally to
restore the container even if tests fail. No secrets/tokens in report.
"""
from __future__ import annotations
import json,subprocess,sys,time
import httpx
from core06.hybrid_retrieval import COLLECTION,qdrant_client
from tools.p3_gateway_smoke import mint,API

NAME="factory-core-v06-qdrant-p3"

def docker(*args,timeout=25):
    p=subprocess.run(["docker",*args],capture_output=True,text=True,
        encoding="utf-8",errors="replace",timeout=timeout)
    if p.returncode:raise RuntimeError("DOCKER_COMMAND_FAILED:"+args[0])
    return p.stdout.strip()

def point_count():
    client=qdrant_client()
    try:
        return client.count(COLLECTION,exact=True).count
    finally:
        client.close()

def main():
    label=docker("inspect",NAME,"--format",'{{index .Config.Labels "factory.core.stage"}}')
    if label!="v06-p3":raise RuntimeError("UNKNOWN_CONTAINER_OWNER")
    image=docker("inspect",NAME,"--format","{{.Config.Image}}")
    if image!="qdrant/qdrant:v1.20.2":raise RuntimeError("UNKNOWN_CONTAINER_IMAGE")
    count_before=point_count()
    if count_before!=13:raise RuntimeError("UNEXPECTED_SOURCE_INDEX")
    token=mint("engineer")
    headers={"Authorization":"Bearer "+token}
    stopped=False
    fail_closed=None
    try:
        docker("stop","--time","10",NAME,timeout=25)
        stopped=True
        with httpx.Client(timeout=30,trust_env=False) as c:
            response=c.get(API+"/api/hybrid-search",
                params={"q":"Engineering document","limit":3},headers=headers)
            fail_closed=response.status_code
            if fail_closed!=503:raise RuntimeError("VECTOR_BACKEND_FAILURE_NOT_FAIL_CLOSED")
            if "documents" in response.text or "chunk" in response.text:
                raise RuntimeError("UNAUTHORIZED_TEXT_RETURNED_DURING_OUTAGE")
    finally:
        if stopped:docker("start",NAME,timeout=35)
    recovered=False
    for i in range(30):
        try:
            if point_count()==count_before:
                recovered=True
                break
        except Exception:pass
        time.sleep(.6)
    if not recovered:raise RuntimeError("QDRANT_STORAGE_NOT_RECOVERED")
    with httpx.Client(timeout=30,trust_env=False) as c:
        response=c.get(API+"/api/hybrid-search",
            params={"q":"Engineering document","limit":3},headers=headers)
        if response.status_code!=200:raise RuntimeError("GATEWAY_DID_NOT_RECONNECT_TO_QDRANT")
    print(json.dumps({"status":"PASS","container":NAME,
        "points_before":count_before,"points_after":point_count(),
        "during_qdrant_outage_http":fail_closed,
        "after_recovery_http":response.status_code,
        "docker_named_volume_persistent":True,"other_services_untouched":True},
        ensure_ascii=True),flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(json.dumps({"status":"FAILED","type":type(exc).__name__,
            "reason":str(exc)[:280]},ensure_ascii=True),flush=True)
        sys.exit(1)
