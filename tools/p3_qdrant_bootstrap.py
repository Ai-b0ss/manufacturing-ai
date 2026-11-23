"""Start an isolated Qdrant 1.20.2 server via Docker Desktop Linux engine.

No Windows bind mounts (Qdrant warns about Windows/WSL data-loss risks).
One Docker named volume and one localhost-only port. API key is stored outside
Git in the protected Windows runtime. Fails closed on unexpected old container.
Never outputs credentials. This is only a demo/dev instance.
"""
from __future__ import annotations
import hashlib,json,os,re,secrets,socket,subprocess,sys,time,urllib.error,urllib.request
from pathlib import Path

ROOT=Path(r"K:\ManufacturingAI-CoreRuntime")
CREDS=ROOT/"credentials"
APIKEY=CREDS/"qdrant-api-key.txt"
ENV_FILE=CREDS/"qdrant-env.txt"
CONTAINER="factory-core-v06-qdrant-p3"
VOLUME="factory-core-v06-qdrant-data"
IMAGE="qdrant/qdrant:v1.20.2"
DIGEST="sha256:d277592721d2e3b73f9b797b53cf05e6a5bb2134f6d13fdbda4ca51782afbcac"
PORT=16333

def run(args,timeout=55):
    p=subprocess.run([str(x) for x in args],capture_output=True,text=True,encoding="utf-8",
        errors="replace",timeout=timeout,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if p.returncode:raise RuntimeError("COMMAND_FAILED:"+str(args[0])+":"+p.stderr[-260:])
    return p.stdout.strip()

def restrict(path):
    raw=run(["whoami","/user","/fo","csv","/nh"],9)
    sid=raw.replace('"',"").split(",")[-1]
    if not re.fullmatch(r"S-1-5-[0-9-]+",sid):raise RuntimeError("INVALID_CURRENT_SID")
    result=subprocess.run(["icacls",str(path),"/inheritance:r","/grant:r",
        f"*{sid}:F","*S-1-5-18:F","*S-1-5-32-544:F"],capture_output=True,timeout=10)
    if result.returncode:raise RuntimeError("CANNOT_RESTRICT_SECRET_FILE")

def port_open():
    with socket.socket() as s:
        s.settimeout(.5)
        return s.connect_ex(("127.0.0.1",PORT))==0

def request(path,authorized=False):
    headers={}
    if authorized:headers["api-key"]=APIKEY.read_text(encoding="ascii")
    r=urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",headers=headers)
    try:
        with urllib.request.urlopen(r,timeout=3) as resp:
            return resp.status,resp.read(10240).decode("utf-8","replace")
    except urllib.error.HTTPError as exc:
        return exc.code,exc.read(500).decode("utf-8","replace")

def main():
    if not CREDS.exists():raise RuntimeError("P1_CREDENTIALS_DIR_MISSING")
    if not APIKEY.exists():APIKEY.write_text(secrets.token_urlsafe(50),encoding="ascii")
    restrict(APIKEY)
    token=APIKEY.read_text(encoding="ascii")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{55,150}",token):raise RuntimeError("INVALID_API_KEY")
    if not ENV_FILE.exists():ENV_FILE.write_text("QDRANT__SERVICE__API_KEY="+token+"\n",encoding="ascii")
    elif ENV_FILE.read_text(encoding="ascii")!="QDRANT__SERVICE__API_KEY="+token+"\n":
        raise RuntimeError("QDRANT_ENV_FILE_DIVERGES_FROM_KEY")
    restrict(ENV_FILE)
    image=run(["docker","image","inspect",IMAGE,"--format","{{json .RepoDigests}}"],12)
    if "qdrant/qdrant@"+DIGEST not in image:
        raise RuntimeError("QDRANT_IMAGE_DIGEST_DOES_NOT_MATCH_PIN")
    existing=run(["docker","ps","-a","--filter","name=^/"+CONTAINER+"$",
                  "--format","{{.ID}}"],12).strip()
    if existing:
        labels=run(["docker","inspect",CONTAINER,"--format","{{index .Config.Labels \"factory.core.stage\"}}"],12)
        if labels!="v06-p3":raise RuntimeError("CONTAINER_OWNERSHIP_UNVERIFIED")
        state=run(["docker","inspect",CONTAINER,"--format","{{.State.Running}}"],12)
        if state=="false":run(["docker","start",CONTAINER],30)
        print(json.dumps({"status":"existing_started","container":CONTAINER,"port":PORT,
                          "image":IMAGE,"named_volume":VOLUME,"api_key_protected":True},ensure_ascii=True))
    else:
        if port_open():raise RuntimeError("PORT_16333_ALREADY_IN_USE")
        run(["docker","volume","create","--label","factory.core.stage=v06-p3",VOLUME],20)
        command=["docker","run","-d","--name",CONTAINER,
           "--label","factory.core.stage=v06-p3","--restart=no",
           "--pull=never","--cap-drop=ALL","--security-opt","no-new-privileges=true",
           "--pids-limit=128","--memory=1536m","--cpus=1.5",
           "-p",f"127.0.0.1:{PORT}:6333",
           "--mount",f"type=volume,source={VOLUME},target=/qdrant/storage",
           "--env-file",ENV_FILE,IMAGE]
        output=run(command,45)
        print(json.dumps({"status":"started","container":CONTAINER,
             "docker_id":output[:16],"image":IMAGE,"port":PORT,
             "named_volume":VOLUME,"api_key_protected":True},ensure_ascii=True))
    for i in range(35):
        try:
            unauthorized,_=request("/collections",False)
            authorized,content=request("/collections",True)
            if authorized==200 and unauthorized in (401,403):
                info=json.loads(content)
                print(json.dumps({"status":"ready","http_auth":authorized,
                    "http_no_auth":unauthorized,"collections":len(info.get("result",{}).get("collections",[])),
                    "host":"127.0.0.1","port":PORT,"image":IMAGE},ensure_ascii=True))
                return
        except Exception:pass
        time.sleep(.6)
    raise RuntimeError("QDRANT_NOT_READY_OR_API_KEY_NOT_ENFORCED")

if __name__=="__main__":
    try:main()
    except Exception as e:
        print(json.dumps({"status":"failed","type":type(e).__name__,
            "reason":str(e)[:280]},ensure_ascii=True),flush=True)
        sys.exit(1)
