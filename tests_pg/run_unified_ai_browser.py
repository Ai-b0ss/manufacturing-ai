"""Run real Chrome CAD upload acceptance on isolated port 19137, not live 19139."""
from __future__ import annotations
import json,os,socket,subprocess,sys,time,urllib.request
HIDDEN_STARTUP=subprocess.STARTUPINFO()
HIDDEN_STARTUP.dwFlags |= subprocess.STARTF_USESHOWWINDOW
HIDDEN_STARTUP.wShowWindow=subprocess.SW_HIDE
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PY=Path(r"C:\Users\user\Documents\ManufacturingAI-Core-V06-P3-Hybrid-Retrieval\.p3venv\Scripts\python.exe")
NODE=Path(r"K:\FF-Windows-Bridge\state\universal-agent-canary-688ef3e\tools\node\node-v22.14.0-win-x64\node.exe")
REPORT=ROOT/"data"/"unified_ai_browser_runner.json"


def command(args,timeout=24):
    p=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,stdin=subprocess.DEVNULL,
                      timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=HIDDEN_STARTUP)
    if p.returncode:
        raise RuntimeError("COMMAND_FAILED "+p.stdout[-300:]+" "+p.stderr[-250:])
    return p.stdout.strip()


def available():
    with socket.socket() as sock:
        sock.settimeout(.25)
        return sock.connect_ex(("127.0.0.1",19137))!=0


def healthy():
    try:
        with urllib.request.urlopen("http://localhost:19137/unified/health",timeout=2) as r:
            return r.status==200
    except Exception:return False


def main():
    assert available(),"REFUSE_PORT_19137_ALREADY_OWNED"
    REPORT.parent.mkdir(exist_ok=True)
    response_file=ROOT/"data"/"unified_ai_browser_acceptance.json"
    response_file.unlink(missing_ok=True)
    state=json.loads(command([str(PY),"-m","tests_pg.ephemeral_keycloak_client","verify"]))
    if state["exists"]:
        raise RuntimeError("REFUSE_REUSE_OTHER_AGENT_KEYCLOAK_CLIENT")
    created=False
    service=None
    results={"purpose":"existing_local_llm_with_protected_sources","live_19139_untouched":True}
    try:
        info=json.loads(command([str(PY),"-m","tests_pg.ephemeral_keycloak_client","create"]))
        if info.get("action")!="created":
            raise RuntimeError("KEYCLOAK_CLIENT_CREATION_UNCONFIRMED")
        created=True
        env=os.environ.copy()
        env.update({
            "FACTORY_P2_APP_ORIGIN":"http://localhost:19137",
            "FACTORY_P2_WEB_CLIENT":"factory-web-audit",
            "FACTORY_V06_UNIFIED_STAGING":"1",
            "HF_HUB_OFFLINE":"1","TRANSFORMERS_OFFLINE":"1",
            "NO_PROXY":"127.0.0.1,localhost","no_proxy":"127.0.0.1,localhost",
            "OMP_NUM_THREADS":"2","MKL_NUM_THREADS":"2",
        })
        with (ROOT/"data"/"unified_ai_stage.log").open("wb") as logfile:
            service=subprocess.Popen(
                [str(PY),"-m","uvicorn","unified_app:app",
                 "--host","127.0.0.1","--port","19137","--no-access-log"],
                cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                stdout=logfile,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=HIDDEN_STARTUP,
            )
            for _ in range(90):
                if service.poll() is not None:
                    raise RuntimeError("STAGING_SERVER_EXITED "+str(service.returncode))
                if healthy():break
                time.sleep(.25)
            else:raise RuntimeError("STAGING_NOT_HEALTHY")
            print("STAGING_READY",flush=True)
            p=subprocess.run(
                [str(NODE),str(ROOT/"tests_pg"/"browser_unified_ai.cjs")],
                cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=240,
                creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=HIDDEN_STARTUP,
            )
            results["browser_exit"]=p.returncode
            results["browser_stdout_tail"]=p.stdout[-1700:]
            results["browser"]=json.loads(response_file.read_text("utf-8")) if response_file.exists() else {}
            if p.returncode!=0:
                raise RuntimeError("UNIFIED_AI_BROWSER_FAILED "+p.stdout[-550:]+p.stderr[-300:])
            results["success"]=True
    finally:
        if service is not None and service.poll() is None:
            service.terminate()
            try:service.wait(timeout=9)
            except subprocess.TimeoutExpired:service.kill();service.wait(timeout=9)
        if created:
            deleted=json.loads(command([str(PY),"-m","tests_pg.ephemeral_keycloak_client","delete"]))
            results["client_deleted"]=deleted.get("action")=="deleted"
        results["stage_closed"]=available()
        REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
        print("STAGING_CLEAN",results["stage_closed"],results.get("client_deleted"),flush=True)
    return results


if __name__=="__main__":
    try:
        result=main()
        print("PRODUCT_BROWSER_RESULT "+json.dumps(result,ensure_ascii=True),flush=True)
    except Exception as exc:
        print("PRODUCT_BROWSER_FAILURE "+type(exc).__name__+": "+str(exc)[:800],flush=True)
        sys.exit(1)
