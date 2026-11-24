"""Service checks: http_targets parsing, one round of checks, the log file,
and the uptime and outage-grid arithmetic."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest


def test_read_http_targets() -> None:
    import server

    assert server._read_http_targets({}) == [], "absent: the feature is off"
    assert server._read_http_targets({"http_targets": None}) == []
    assert server._read_http_targets({"http_targets": []}) == []
    targets: list[Any] = server._read_http_targets(
        {
            "http_targets": [
                {"name": "pages", "url": "http://example-host:8000/"},
                {
                    "name": "cloud",
                    "url": "https://box:9200/",
                    "status": 302,
                    "insecure": True,
                },
            ]
        }
    )
    assert targets == [
        server.HttpTarget("pages", "http://example-host:8000/", None, False),
        server.HttpTarget("cloud", "https://box:9200/", 302, True),
    ]
    bad: object
    for bad in (
        "http://x/",
        ["http://x/"],
        [{"url": "http://x/"}],
        [{"name": "", "url": "http://x/"}],
        [{"name": "x"}],
        [{"name": "x", "url": "example-host:8000"}],
        [{"name": "x", "url": "ftp://x/"}],
        [{"name": "x", "url": "http://x/", "status": "200"}],
        [{"name": "x", "url": "http://x/", "status": True}],
        [{"name": "x", "url": "http://x/", "status": 99}],
        [{"name": "x", "url": "http://x/", "insecure": "yes"}],
        [{"name": "x", "url": "http://a/"}, {"name": "x", "url": "http://b/"}],
    ):
        with pytest.raises(SystemExit, match="http_targets must be a list"):
            server._read_http_targets({"http_targets": bad})


def run_round(
    targets: list[Any], handler: Any, timeout: float = 1.0
) -> dict[str, dict[str, Any]]:
    """One real check_http_targets round against a mock transport, no retry delay."""
    import server

    async def go() -> dict[str, dict[str, Any]]:
        client: httpx.AsyncClient
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            return await server.check_http_targets(
                targets, client, client, timeout, retry_delay=0
            )

    return asyncio.run(go())


def test_check_round_statuses() -> None:
    import server

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "refused":
            raise httpx.ConnectError("refused")
        if request.url.host == "bug":
            raise KeyError("bug")
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "http://s200/"})
        return httpx.Response(int(request.url.host.removeprefix("s")))

    targets: list[Any] = [
        server.HttpTarget("ok", "http://s200/", None, False),
        server.HttpTarget("redirect", "http://s200/moved", None, False),
        server.HttpTarget("teapot", "http://s418/", None, False),
        server.HttpTarget("error", "http://s500/", None, False),
        server.HttpTarget("wants-401", "http://s401/", 401, False),
        server.HttpTarget("wants-204", "http://s200/", 204, False),
        server.HttpTarget("refused", "http://refused/", None, False),
        server.HttpTarget("wants-302", "http://s200/moved", 302, False),
        server.HttpTarget("bug", "http://bug/", None, False),
    ]
    results: dict[str, dict[str, Any]] = run_round(targets, handler)
    assert results["wants-302"]["ok"], "a required 3xx is not followed away"
    assert results["bug"] == {"ok": False, "status": None, "error": "KeyError"}, (
        "an unexpected exception fails its own target, not the round"
    )
    assert results["ok"]["ok"] and results["ok"]["status"] == 200
    assert isinstance(results["ok"]["ms"], int)
    assert results["redirect"]["ok"], "a redirect is followed to its 200"
    assert results["teapot"] == {"ok": False, "status": 418, "error": "status 418"}
    assert results["error"] == {"ok": False, "status": 500, "error": "status 500"}
    assert results["wants-401"]["ok"], "a required status makes a 401 a pass"
    assert not results["wants-204"]["ok"], "and makes any other status a failure"
    assert results["refused"] == {"ok": False, "status": None, "error": "ConnectError"}
    assert list(results) == [t.name for t in targets], "config order is kept"


def test_check_retries_once_before_down() -> None:
    import server

    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host: str = request.url.host
        calls[host] = calls.get(host, 0) + 1
        if host == "restarting":
            return httpx.Response(502 if calls[host] == 1 else 200)
        return httpx.Response(502 if host == "dead" else 200)

    results: dict[str, dict[str, Any]] = run_round(
        [
            server.HttpTarget("restarting", "http://restarting/", None, False),
            server.HttpTarget("dead", "http://dead/", None, False),
            server.HttpTarget("fine", "http://fine/", None, False),
        ],
        handler,
    )
    assert results["restarting"]["ok"], "one failed request is not an outage"
    assert not results["dead"]["ok"]
    assert calls == {"restarting": 2, "dead": 2, "fine": 1}


def test_check_timeout_bounds_the_whole_request() -> None:
    import server

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    results: dict[str, dict[str, Any]] = run_round(
        [server.HttpTarget("slow", "http://slow/", None, False)], handler, timeout=0.05
    )
    assert results["slow"] == {"ok": False, "status": None, "error": "timeout"}


def test_uptime_windows() -> None:
    import server

    now: float = 1_000_000.0
    rounds: list[tuple[float, dict[str, bool]]] = [
        (now - 200 * 3600, {"a": False}),  # older than every window
        (now - 100 * 3600, {"a": False}),  # week only
        (now - 10 * 3600, {"a": True, "b": True}),  # day and week
        (now - 1800, {"a": True, "b": False}),  # all three
        (now - 60, {"a": True}),  # b was not configured in this round
    ]
    uptime: dict[str, dict[str, dict[str, int]]] = server.services_uptime(
        rounds, ["a", "b", "new"], now
    )
    assert uptime["a"] == {
        "1": {"up": 2, "checks": 2},
        "24": {"up": 3, "checks": 3},
        "168": {"up": 3, "checks": 4},
    }
    assert uptime["b"]["1"] == {"up": 0, "checks": 1}
    assert uptime["b"]["168"] == {"up": 1, "checks": 2}
    assert uptime["new"]["168"] == {"up": 0, "checks": 0}, "no data, not 0% or 100%"


def test_bucket_plan_and_buckets() -> None:
    import server

    now: float = 1_000_030.0
    start: float
    bucket: int
    count: int
    start, bucket, count = server.services_bucket_plan(1, now, 60)
    assert (bucket, count) == (60, 60), "an hour at 60 s is one check per bucket"
    assert start % 60 == 0 and start + count * bucket >= now > start + 59 * bucket
    assert server.services_bucket_plan(1, now + 20, 60)[0] == start, (
        "boundaries do not move between requests in the same minute"
    )
    start, bucket, count = server.services_bucket_plan(720, now, 60)
    assert count <= server.SERVICES_MAX_BUCKETS and bucket % 60 == 0
    assert bucket * count >= 720 * 3600
    start, bucket, count = server.services_bucket_plan(1, now, 600)
    assert (bucket, count) == (600, 6), "never finer than the check interval"

    grid: dict[str, list[list[int]]] = server.bucket_services(
        [
            (99.0, {"a": True}),  # before the grid
            (100.0, {"a": True, "gone": False}),
            (159.0, {"a": False}),
            (160.0, {"a": False}),
            (280.0, {"a": True}),  # at the end: outside
        ],
        ["a", "b"],
        100.0,
        60,
        3,
    )
    assert grid == {"a": [[1, 1], [0, 1], [0, 0]], "b": [[0, 0]] * 3}


def test_round_is_logged_and_reloaded(tmp_path: Path) -> None:
    """A round appends one line to services.jsonl, feeds the card's snapshot,
    and a restart reads the rounds back from the file."""
    import server

    def snapshot_at(now: float) -> dict[str, Any]:
        return asyncio.run(server.services_snapshot(now))

    log: Path = tmp_path / "services.jsonl"
    targets: list[Any] = [
        server.HttpTarget("up", "http://up/", None, False),
        server.HttpTarget("down", "http://down/", None, False),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.host == "up" else 503)

    async def two_rounds() -> None:
        client: httpx.AsyncClient
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await server.run_service_round(client, client)
            await server.run_service_round(client, client)

    with (
        patch.object(server, "SERVICES_FILE", log),
        patch.object(server, "LOGS_DIR", tmp_path),
        patch.object(server, "HTTP_TARGETS", targets),
        patch.object(server, "SERVICES_RETRY_DELAY", 0),
    ):
        asyncio.run(two_rounds())
        lines: list[dict[str, Any]] = [
            json.loads(line) for line in log.read_text().splitlines()
        ]
        assert len(lines) == 2
        assert lines[0]["interval"] == server.SERVICES_CHECK_INTERVAL
        assert lines[0]["targets"]["up"]["ok"] is True
        assert lines[0]["targets"]["down"] == {
            "ok": False,
            "status": 503,
            "error": "status 503",
        }
        datetime.fromisoformat(lines[0]["timestamp"])

        now: float = datetime.now(timezone.utc).timestamp()
        snapshot: dict[str, Any] = snapshot_at(now)
        assert [t["name"] for t in snapshot["targets"]] == ["up", "down"]
        assert snapshot["targets"][0]["ok"] is True
        assert snapshot["targets"][0]["uptime"]["1"] == {"up": 2, "checks": 2}
        assert snapshot["targets"][1]["error"] == "status 503"
        assert snapshot["targets"][1]["uptime"]["168"] == {"up": 0, "checks": 2}
        assert snapshot["targets"][1]["checked"] == lines[1]["timestamp"]
        assert snapshot["stale"] is False
        slow: float = 3 * server.SERVICES_CHECK_INTERVAL + 3 * server.SERVICES_TIMEOUT
        assert snapshot_at(now + slow)["stale"] is False, (
            "a round with a hanging target lasts two timeouts, and is stamped at its start"
        )
        later: float = now + 4 * server.SERVICES_CHECK_INTERVAL + 60
        assert snapshot_at(later)["stale"] is True, (
            "no round for several intervals: the checker has stopped"
        )
        # A restart after downtime: the rounds read back are old, the checker
        # is new. Not stale until the new checker itself falls behind.
        with patch.object(server, "_services_started", later - 5):
            assert snapshot_at(later)["stale"] is False
            assert (
                snapshot_at(later + 4 * server.SERVICES_CHECK_INTERVAL + 60)["stale"]
                is True
            )
        # No round at all: stale counts from the checker's start, so a first
        # round that never finishes does not read as pending forever.
        with (
            patch.object(server, "_services_rounds", deque()),
            patch.object(server, "_services_latest", {}),
        ):
            assert snapshot_at(later)["stale"] is False, "not running"
            with patch.object(server, "_services_started", now):
                assert snapshot_at(now + 5)["stale"] is False
                assert snapshot_at(later)["stale"] is True

        # A line that is not a round is skipped, not fatal.
        with open(log, "a") as f:
            f.write('{"timestamp": "2020-01-01T00:00:00+00:00"}\n')
        rounds: list[tuple[float, dict[str, bool]]]
        truncated: bool
        rounds, truncated = server._read_service_rounds(now - 3600)
        assert not truncated
        assert [oks for _, oks in rounds] == [{"up": True, "down": False}] * 2
        assert server._read_service_rounds(now + 10) == ([], False)

        history: dict[str, Any] = json.loads(
            bytes(asyncio.run(server.services_history(hours=1)).body)
        )
        assert history["count"] == len(history["targets"]["up"]) == 60
        assert sum(up for up, _ in history["targets"]["up"]) == 2
        assert sum(failed for _, failed in history["targets"]["down"]) == 2

    with patch.object(server, "SERVICES_FILE", tmp_path / "absent.jsonl"):
        assert server._read_service_rounds(0) == ([], False), "no file yet"


def test_endpoints_without_targets() -> None:
    import server

    assert server.HTTP_TARGETS == [], "the example config checks nothing"
    body: dict[str, Any] = json.loads(bytes(asyncio.run(server.services()).body))
    assert body["targets"] == []
    history: dict[str, Any] = json.loads(
        bytes(asyncio.run(server.services_history(hours=24)).body)
    )
    assert history["targets"] == {} and history["truncated"] is False


def test_text_report_services_line() -> None:
    import server

    record: dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
    services: list[dict[str, Any]] = [
        {"name": "pages", "ok": True, "uptime": {"24": {"up": 1439, "checks": 1440}}},
        {"name": "immich", "ok": False, "uptime": {"24": {"up": 0, "checks": 0}}},
        {"name": "new", "ok": None, "uptime": {}},
    ]
    now: datetime = datetime.now(timezone.utc)
    lines: list[str] = [
        line for _, line in server._render_latest(record, False, now, services=services)
    ]
    line: str = next(line for line in lines if line.startswith("Services"))
    assert "* pages UP 99.9%" in line
    assert "* immich DOWN" in line and "* new ?" in line
    # In colour, the dot of a target not checked yet is muted like its "?",
    # not the red of a failure.
    painted: str = next(
        line
        for _, line in server._render_latest(
            record, False, now, color=True, services=services
        )
        if "Services" in line
    )
    dot_new: str = painted[: painted.index("new")].rsplit("*", 1)[0]
    dot_down: str = painted[: painted.index("immich")].rsplit("*", 1)[0]
    assert dot_new.rsplit("\x1b[", 1)[1] != dot_down.rsplit("\x1b[", 1)[1]
    assert not any(
        line.startswith("Services")
        for _, line in server._render_latest(record, False, now)
    ), "no targets: no line"
    narrow: list[str] = [
        line
        for _, line in server._render_latest(
            record, False, now, width=30, services=services
        )
    ]
    assert (
        sum("UP" in line or "DOWN" in line or " ?" in line for line in narrow) == 3
    ), "the line wraps instead of overflowing"


def test_services_snapshot_does_not_block_or_mix_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker counts the captured checks while new checks arrive on the loop."""
    import server

    now = time.time()
    target = server.HttpTarget("site", "http://site/", None, False)
    monkeypatch.setattr(server, "HTTP_TARGETS", [target])
    server._note_service_round(
        {"timestamp": "first", "targets": {"site": {"ok": True}}}, now - 30
    )
    server._note_service_round(
        {"timestamp": "second", "targets": {"site": {"ok": False}}}, now - 10
    )
    original = server.services_uptime
    release = threading.Event()

    async def run() -> None:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocked_count(*args: Any) -> dict[str, dict[str, dict[str, int]]]:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "counting blocked the event loop"
            return original(*args)

        monkeypatch.setattr(server, "services_uptime", blocked_count)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.APP), base_url="http://dashboard"
        ) as client:
            pending = asyncio.create_task(client.get("/api/services"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                # This must finish before the counting worker is released.
                response = await asyncio.wait_for(client.get("/api/version"), 2)
                assert response.status_code == 200
                assert not pending.done()
                # Even an in-place edit of the latest result cannot change the
                # captured snapshot. Published round dictionaries stay immutable.
                server._services_latest["site"]["ok"] = True
                # A distant round prunes the earlier rounds from the live deque.
                server._note_service_round(
                    {"timestamp": "third", "targets": {"site": {"ok": True}}},
                    now + 200 * 3600,
                )
                assert len(server._services_rounds) == 1
            finally:
                release.set()
                response = await asyncio.wait_for(pending, 5)
        snapshot = response.json()
        row = snapshot["targets"][0]
        assert row["ok"] is False and row["checked"] == "second"
        assert row["down_since"] == now - 10
        assert row["uptime"]["1"] == {"up": 1, "checks": 2}
        assert snapshot["stale"] is False

    asyncio.run(run())


def test_uptime_is_fraction_of_checks_at_window_boundaries() -> None:
    import server

    now = 1_000_000.0
    rounds = [
        (now - 3600 - 0.01, {"site": False}),
        (now - 3600, {"site": True}),
        (now - 1, {"site": False}),
        (now, {"other": True}),
        (now + 0.01, {"site": True}),
    ]
    result = server.services_uptime(rounds, ["site", "never"], now, (1,))
    assert result["site"]["1"] == {"up": 1, "checks": 2}
    assert result["never"]["1"] == {"up": 0, "checks": 0}
