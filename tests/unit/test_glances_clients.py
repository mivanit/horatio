"""Recording connections stay independent of interactive Glances traffic."""

from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any

import httpx
import pytest


@pytest.fixture
def sampler(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    import server

    for name, value in {
        "history_sample_duration": 1,
        "history_sample_count": 1,
        "history_processes_enabled": False,
        "history_process_count": 10,
        "ping_enabled": False,
        "manual_record_min_interval": 0,
        "glances_url": "http://glances.invalid",
    }.items():
        monkeypatch.setattr(server.APP.state, name, value, raising=False)
    monkeypatch.setattr(server, "_read_cpu_freqs", lambda: [])
    monkeypatch.setattr(server, "get_gpu_stats", lambda: [])
    monkeypatch.setattr(server, "get_hw_sensors", lambda: {"rows": []})
    monkeypatch.setattr(
        server, "_get_storage_health", lambda *args: {"status": "missing"}
    )

    async def no_ip() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(server, "_ip_info_cached", no_ip)
    return server


@pytest.mark.parametrize("recording", [False, True])
def test_temporary_glances_client_closes(recording: bool) -> None:
    import server

    async def run() -> None:
        with pytest.raises(RuntimeError, match="sample failed"):
            async with server._glances_client(recording=recording) as client:
                assert not client.is_closed
                raise RuntimeError("sample failed")
        assert client.is_closed

    asyncio.run(run())


def test_recording_and_live_samples_use_separate_persistent_clients(
    sampler: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = sampler
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "append_history_record", captured.append)
    real_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []
    requests: list[list[str]] = [[], []]

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        index = len(clients)

        def handle(request: httpx.Request) -> httpx.Response:
            requests[index].append(request.url.path)
            if request.url.path == "/api/4/cpu":
                return httpx.Response(200, json={"total": 10 + index})
            return httpx.Response(404)

        client = real_client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(server.httpx, "AsyncClient", make_client)

    async def run() -> None:
        async with server._glances_clients():
            async with server._glances_client() as interactive:
                async with server._glances_client(recording=True) as recording:
                    assert interactive is not recording
            assert not interactive.is_closed and not recording.is_closed
            live = await server.collect_history_sample(
                server.APP.state.glances_url, live=True
            )
            assert live["cpu"]["avg"] == 10
            interactive_count = len(requests[0])
            recorded = await server.collect_history_sample(server.APP.state.glances_url)
            assert recorded["cpu"]["avg"] == 11
            response = await server.trigger_history_record()
            assert response.status_code == 200 and captured[-1]["cpu"]["avg"] == 11
            # The warm-up's first CPU/percpu pair must also bypass the browser pool.
            warming = asyncio.create_task(
                server.wait_for_glances_warm(server.APP.state.glances_url)
            )
            await asyncio.sleep(0)
            assert requests[1][-2:] == ["/api/4/cpu", "/api/4/percpu"]
            warming.cancel()
            await asyncio.gather(warming, return_exceptions=True)
            assert len(requests[0]) == interactive_count
            assert len(clients) == 2, "sampling must reuse clients"
        assert all(client.is_closed for client in clients)
        assert server._glances_client_shared is None
        assert server._glances_client_recording is None

    asyncio.run(run())


def test_recording_works_with_interactive_pool_exhausted(sampler: ModuleType) -> None:
    """Real sockets exercise HTTPX pooling, which MockTransport bypasses."""
    server = sampler

    async def run() -> None:
        all_held = asyncio.Event()
        release = asyncio.Event()
        held = 0
        handlers: set[asyncio.Task[None]] = set()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal held
            task = asyncio.current_task()
            assert task is not None
            handlers.add(task)
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                path = request.split(b" ")[1]
                if path == b"/hold":
                    held += 1
                    if held == 20:
                        all_held.set()
                    await release.wait()
                body = b'{"total":23}'
                status = (
                    b"200 OK" if path in (b"/hold", b"/api/4/cpu") else b"404 Not Found"
                )
                writer.write(
                    b"HTTP/1.1 "
                    + status
                    + b"\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        listener = await asyncio.start_server(handle, "127.0.0.1", 0)
        address = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        async with listener, server._glances_clients():
            async with server._glances_client() as interactive:
                blocked = [
                    asyncio.create_task(interactive.get(address + "/hold", timeout=10))
                    for _ in range(20)
                ]
                try:
                    await asyncio.wait_for(all_held.wait(), 5)
                    with pytest.raises(httpx.PoolTimeout):
                        await interactive.get(
                            address + "/api/4/cpu", timeout=httpx.Timeout(2, pool=0.05)
                        )
                    record = await asyncio.wait_for(
                        server.collect_history_sample(address), 3
                    )
                    assert record["cpu"]["avg"] == 23
                    assert all(not task.done() for task in blocked)
                finally:
                    release.set()
                    await asyncio.gather(*blocked, return_exceptions=True)
                    await asyncio.gather(*handlers, return_exceptions=True)

    asyncio.run(run())


def test_lifespan_cleans_up_clients_when_glances_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    clients: list[httpx.AsyncClient] = []
    monkeypatch.setattr(server.APP.state, "glances_port", 12345, raising=False)

    def fail_start(port: int) -> None:
        clients.extend(
            [server._glances_client_shared, server._glances_client_recording]
        )
        raise RuntimeError("cannot start Glances")

    monkeypatch.setattr(server, "start_glances", fail_start)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="cannot start Glances"):
            async with server.lifespan(server.APP):
                pytest.fail("startup should have failed")
        assert len(clients) == 2 and all(client.is_closed for client in clients)
        assert server._glances_client_shared is None
        assert server._glances_client_recording is None

    asyncio.run(run())


def test_lifespan_drains_replaced_recorder_before_closing_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    clients: list[httpx.AsyncClient] = []
    drained: list[bool] = []
    stopped: list[object] = []
    process = object()
    monkeypatch.setattr(server.APP.state, "glances_port", 12345, raising=False)
    monkeypatch.setattr(
        server.APP.state, "glances_url", "http://glances.invalid", raising=False
    )
    monkeypatch.setattr(server, "start_glances", lambda port: process)
    monkeypatch.setattr(server, "stop_glances", stopped.append)

    async def background(*args: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(all(not client.is_closed for client in clients))

    for name in (
        "check_disconnected_clients",
        "history_recorder",
        "alarm_tracker",
        "record_initial_sample",
        "services_checker",
    ):
        monkeypatch.setattr(server, name, background)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="application failed"):
            async with server.lifespan(server.APP):
                clients.extend(
                    [server._glances_client_shared, server._glances_client_recording]
                )
                original = server.APP.state.history_task
                original.cancel()
                await asyncio.gather(original, return_exceptions=True)
                # Same replacement performed when the recording interval changes.
                replacement = asyncio.create_task(background())
                server.APP.state.history_task = replacement
                await asyncio.sleep(0)
                raise RuntimeError("application failed")
        assert replacement.cancelled()
        assert len(drained) == 5 and all(drained)
        assert all(client.is_closed for client in clients)
        assert server._glances_client_shared is None
        assert server._glances_client_recording is None
        assert stopped == [process]

    asyncio.run(run())
