"""P7 browser acceptance on an isolated ephemeral loopback port."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
import app

HERE=Path(__file__).resolve().parent

def main():
    app.initialize()
    srv=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
    srv.database_path=app.DB
    thread=threading.Thread(target=srv.serve_forever,daemon=True)
    thread.start()
    env=os.environ.copy()
    env["P7_BROWSER_PORT"]=str(srv.server_port)
    t=time.monotonic()
    try:
        node=subprocess.run(["node",str(HERE/"p7_browser_smoke.cjs")],
            cwd=HERE,env=env,capture_output=True,text=True,
            encoding="utf-8",errors="replace",timeout=75)
        report={"exit_code":node.returncode,"elapsed_s":round(time.monotonic()-t,1),
                "stdout":node.stdout[-9000:],"stderr":node.stderr[-9000:]}
        (HERE/"data"/"p7_browser_report.json").write_text(
            json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=True))
        if node.returncode:
            raise SystemExit(node.returncode)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=3)

if __name__=="__main__":
    main()
