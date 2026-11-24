"""Real HTTP smoke checks: LUA_RUNTIME=/path/to/prefix pytest tests/lua/test_runtime.py."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from tests.support import ROOT


def request(base: str, path: str, method: str = "GET") -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(base + path, method=method), timeout=15) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    prefix = os.environ.get("LUA_RUNTIME")
    if not prefix:
        pytest.skip("set LUA_RUNTIME to a built private runtime prefix")
    runtime = Path(prefix).resolve()
    assert (runtime / "bin/uhttpd").is_file(), "LUA_RUNTIME needs bin/uhttpd"
    app = tmp_path_factory.mktemp("lua-http")
    shutil.copytree(ROOT / "lua", app / "lua")
    web = app / "www"
    web.mkdir()
    (web / "index.html").write_text("runtime smoke page", encoding="utf-8")
    (web / "empty-directory").mkdir()
    (app / "VERSION").write_text("runtime-smoke\n", encoding="utf-8")
    (web / "config.json").write_text(
        json.dumps({"history": {"record_interval": 987}, "ping_targets": []}),
        encoding="utf-8",
    )
    (app / "private.txt").write_text("private runtime fixture", encoding="utf-8")
    (web / "outside.txt").symlink_to(app / "private.txt")
    state = app / "state"
    state.mkdir()
    (state / "dashboard-last-record").write_text(
        str(int(time.time())), encoding="ascii"
    )
    # Inject only the existing state-directory test seam. All handler, metrics,
    # history, Nixio and JSON code runs unchanged, with no shared /tmp state.
    bootstrap = app / "bootstrap.lua"
    bootstrap.write_text(
        f"package.path = {json.dumps(str(app / 'lua' / '?.lua'))} .. ';' .. package.path\n"
        "local history = require('history')\n"
        "local init = history.init\n"
        "history.init = function(cfg, metrics, root)\n"
        f"  return init(cfg, metrics, root, {json.dumps(str(state))})\n"
        "end\n"
        "local metrics = require('metrics')\n"
        "local metrics_init = metrics.init\n"
        "metrics.init = function(cfg)\n"
        f"  return metrics_init(cfg, {json.dumps(str(state))})\n"
        "end\n"
        f"dofile({json.dumps(str(app / 'lua' / 'handler.lua'))})\n",
        encoding="utf-8",
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (app / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                str(runtime / "bin/uhttpd"),
                "-f",
                "-p",
                f"127.0.0.1:{port}",
                "-h",
                str(web),
                "-D",
                "-S",
                "-l",
                "/api",
                "-L",
                str(bootstrap),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(f"uhttpd exited during startup:\n{log.read()}")
                try:
                    if request(base, "/")[0] == 200:
                        break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("uhttpd did not start within 10 seconds")
            yield base, app
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_api_and_json_config(server: tuple[str, Path]) -> None:
    base, _ = server
    status, body = request(base, "/api/version")
    assert status == 200
    assert json.loads(body) == {"version": "runtime-smoke", "build": None}
    status, body = request(base, "/api/history/settings")
    assert status == 200
    assert json.loads(body)["record_interval"] == 987
    status, body = request(base, "/api/4/mem")
    assert status == 200
    assert json.loads(body)["total"] > 0
    assert request(base, "/api/unknown")[0] == 404
    assert request(base, "/api/history/record")[0] == 405
    assert request(base, "/api/history/settings", "POST")[0] == 501


def test_history_survives_request_forks(server: tuple[str, Path]) -> None:
    base, app = server
    (app / "state/dashboard-last-record").unlink()
    status, body = request(base, "/api/history/tick")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    status, body = request(base, "/api/history?hours=1")
    assert status == 200
    history = json.loads(body)
    assert history["count"] == 1
    assert history["records"][0]["memory"]["total_gb"] > 0
    assert (app / "logs/history.jsonl").is_file()
    status, body = request(base, "/api/history/field/temps?hours=1")
    assert status == 200
    assert json.loads(body)["count"] == 1
    status, body = request(base, "/api/history/record", "POST")
    assert status == 200
    assert json.loads(body)["status"] == "skipped"


def test_static_files_stay_inside_public_root(server: tuple[str, Path]) -> None:
    base, _ = server
    assert request(base, "/") == (200, b"runtime smoke page")
    for path in (
        "/private.txt",
        "/lua/handler.lua",
        "/logs/history.jsonl",
        "/outside.txt",
        "/empty-directory/",
        "/../private.txt",
    ):
        status, body = request(base, path)
        assert status in (400, 403, 404), path
        assert b"private runtime fixture" not in body
