from __future__ import annotations

import asyncio
from pathlib import Path

import httpx


def get(path: str) -> httpx.Response:
    import server

    async def request() -> httpx.Response:
        transport: httpx.ASGITransport = httpx.ASGITransport(app=server.APP)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get(path)

    return asyncio.run(request())


def test_theme_routes(app_dir: Path) -> None:
    """The picker's list and the theme files: valid names only, never cached."""
    themes: Path = app_dir / "themes"
    (themes / "my-theme_2.css").write_text(":root { --bg-body: red; }")
    (themes / "default.css").write_text("")
    (themes / "a.b.css").write_text("")
    (themes / "notes.txt").write_text("")
    listing: httpx.Response = get("/themes/index.json")
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-cache"
    names: list[str] = listing.json()
    assert names == sorted(names)
    assert {"light", "classic", "my-theme_2"} <= set(names)
    assert not {"default", "a.b", "notes"} & set(names)

    css: httpx.Response = get("/themes/my-theme_2.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert css.headers["cache-control"] == "no-cache"
    assert "--bg-body: red" in css.text
    path: str
    for path in (
        "/themes/nope.css",
        "/themes/default.css",
        "/themes/a.b.css",
        "/themes/..%2fserver.py.css",
        "/themes/README.md",
    ):
        assert get(path).status_code == 404, path


def test_theme_routes_without_dir(app_dir: Path) -> None:
    import shutil

    shutil.rmtree(app_dir / "themes")
    assert get("/themes/index.json").json() == []
    assert get("/themes/light.css").status_code == 404
