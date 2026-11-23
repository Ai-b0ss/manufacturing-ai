"""Owned loopback Qdrant process for public/synthetic Windows test data."""
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time

import httpx

from tools.bringup_factory import HIDDEN_STARTUP, NO_WINDOW


@contextmanager
def native_vector(executable, scratch_root=None):
    executable = Path(executable).resolve(strict=True)
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        for _ in range(30):
            port = 19200 + secrets.randbelow(700)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            break
        else:
            raise RuntimeError("NO_FREE_VECTOR_TEST_PORT")
    with tempfile.TemporaryDirectory(prefix="qdr-", dir=scratch_root) as scratch:
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("QDRANT__")}
        env.update({
            "QDRANT__SERVICE__HOST": "127.0.0.1",
            "QDRANT__SERVICE__HTTP_PORT": str(port),
            "QDRANT__TELEMETRY_DISABLED": "true",
            "QDRANT__STORAGE__PERFORMANCE__MAX_SEARCH_THREADS": "2",
            "TEMP": scratch,
            "TMP": scratch,
        })
        config = Path(scratch) / "fixture.yaml"
        config.write_text(
            "service:\n  grpc_port: null\n"
            "storage:\n  optimizers:\n    default_segment_number: 1\n",
            encoding="ascii",
        )
        base = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryFile(dir=scratch) as log, httpx.Client(timeout=1, trust_env=False) as http:
            proc = subprocess.Popen(
                [str(executable), "--config-path", str(config)], cwd=scratch, env=env,
                stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, startupinfo=HIDDEN_STARTUP,
                creationflags=NO_WINDOW,
            )
            try:
                deadline = time.monotonic() + 30
                while proc.poll() is None and time.monotonic() < deadline:
                    if is_owned_listener(port, proc.pid):
                        try:
                            ready = http.get(base + "/readyz").status_code == 200
                        except httpx.RequestError:
                            ready = False
                        if ready:
                            yield port
                            return
                    time.sleep(.2)
                log.seek(0)
                raise RuntimeError("VECTOR_FIXTURE_START_FAILED " + log.read(3800).decode(errors="replace"))
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)


def is_owned_listener(port, pid):
    result = subprocess.run(
        ["netstat.exe", "-ano", "-p", "tcp"], capture_output=True, timeout=4,
        startupinfo=HIDDEN_STARTUP, creationflags=NO_WINDOW, check=True,
    )
    address = f"127.0.0.1:{port}".encode()
    return any(
        len(fields) == 5 and fields[:3] == [b"TCP", address, b"0.0.0.0:0"]
        and fields[-1] == str(pid).encode()
        for fields in map(bytes.split, result.stdout.splitlines())
    )
