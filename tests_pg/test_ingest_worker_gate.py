"""Worker must refuse to start without explicit operator flags."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def _hidden_startupinfo():
    if not hasattr(subprocess,"STARTUPINFO"):
        return None
    info=subprocess.STARTUPINFO()
    info.dwFlags|=getattr(subprocess,"STARTF_USESHOWWINDOW",0)
    info.wShowWindow=getattr(subprocess,"SW_HIDE",0)
    return info


def main():
    env=os.environ.copy()
    env.pop("FACTORY_V06_INGEST_ENABLED",None)
    env.pop("FACTORY_V06_INDEX_WORKER_ENABLED",None)
    result=subprocess.run(
        [sys.executable,"-m","tools.run_ingest_indexer","--once"],
        cwd=ROOT,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=10,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
        startupinfo=_hidden_startupinfo(),
    )
    assert result.returncode==2,(result.returncode,result.stderr[-450:])
    assert "both feature flags" in result.stderr
    print("PASS worker fails closed without explicit dual operator enable flags")


if __name__=="__main__":
    main()
