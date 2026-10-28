from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from starlette.requests import Request


def test_auth_throttle_unit() -> None:
    """_verify_password_throttled under a concurrent burst: the lockout counts
    attempts before the KDF, so only AUTH_MAX_FAILS guesses reach it, one at a
    time, and the rest get 429 at once."""
    import server

    running: dict[str, int] = {"now": 0, "max": 0, "calls": 0}

    def slow_wrong(password: str) -> bool:
        running["now"] += 1
        running["calls"] += 1
        running["max"] = max(running["max"], running["now"])
        time.sleep(0.05)
        running["now"] -= 1
        return password == "right"

    server._auth_failures.clear()
    req: Request = Request({"type": "http", "client": ("198.51.100.7", 12345)})

    async def burst() -> list[server.Response | None]:
        return await asyncio.gather(
            *(server._verify_password_throttled(req, "wrong") for _ in range(20))
        )

    try:
        with patch.object(server, "_verify_password", slow_wrong):
            replies: list[server.Response | None] = asyncio.run(burst())
            assert all(reply is not None for reply in replies)
            codes: list[int] = sorted(r.status_code for r in replies if r is not None)
            n: int = server.AUTH_MAX_FAILS
            assert (
                codes == [403] * n + [429] * (20 - n)
                and running["calls"] == n
                and (running["max"] == 1)
            ), (
                f"20 concurrent wrong guesses: {n} checked (one at a time), the rest locked out: codes={codes} kdf_calls={running['calls']} max_concurrent={running['max']}"
            )
            server._auth_failures.clear()
            ok_resp: server.Response | None = asyncio.run(
                server._verify_password_throttled(req, "right")
            )
            assert ok_resp is None and "198.51.100.7" not in server._auth_failures, (
                "a correct password passes and clears the streak"
            )
    finally:
        server._auth_failures.clear()
