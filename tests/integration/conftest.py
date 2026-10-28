"""A real dashboard and Glances process, with files confined to a temporary tree."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

import httpx
import pytest


def free_port() -> int:
    sock: socket.socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(live_app_dir: Path) -> Iterator[str]:
    port: int = free_port()
    glances_port: int = free_port()
    while glances_port == port:
        glances_port = free_port()
    base: str = f"http://127.0.0.1:{port}"
    log_path: Path = live_app_dir / "test-server.log"
    log_file: TextIO
    with log_path.open("w") as log_file:
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [
                sys.executable,
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--glances-port",
                str(glances_port),
            ],
            cwd=live_app_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline: float = time.monotonic() + 30
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    if (
                        httpx.get(base, timeout=2).status_code == 200
                        and httpx.get(f"{base}/api/4/cpu", timeout=2).status_code == 200
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                pytest.fail(
                    f"Server failed to start; log: {log_path}\n{log_path.read_text()[-8000:]}"
                )
            yield base
        finally:
            # The child group includes Glances even if the dashboard has exited.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            except ProcessLookupError:
                proc.wait(timeout=5)
