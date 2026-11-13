"""The static demo (docs/): its build, and the fake backend covering every URL
the frontend requests, so a new endpoint cannot ship without a fake."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from tests.support import ROOT

FAKE_API: Path = ROOT / "docs" / "demo" / "fake-api.js"


def server_block(pattern: str) -> str:
    """Group 1 of `pattern` in server.py; a renamed definition fails by name."""
    match: re.Match[str] | None = re.search(
        pattern, (ROOT / "server.py").read_text(), re.S
    )
    assert match is not None, f"not found in server.py: {pattern}"
    return match.group(1)


def frontend_urls() -> set[tuple[str, str]]:
    """(method, url) for every URL static/*.js requests: each '/api/...' or
    '/config.json' string literal, and API_BASE + '...', with template parts
    filled in."""
    side_fields: list[str] = re.findall(
        r'"(\w+)"', server_block(r"HISTORY_SIDE_FIELDS.*?\{(.*?)\}")
    )
    urls: set[tuple[str, str]] = set()
    path: Path
    for path in sorted((ROOT / "static").glob("*.js")):
        if path.name == "chart.umd.min.js":
            continue
        src: str = path.read_text()
        match: re.Match[str]
        for match in re.finditer(
            r"(API_BASE\s*\+\s*)?(['`])(/api/[^'`]*|/config\.json|/[\w-]+)\2(?=(.{0,40}))",
            src,
        ):
            url: str = match.group(3)
            if match.group(1):
                url = "/api/4" + url
            elif url == "/api/4" or not url.startswith(("/api/", "/config.json")):
                continue  # API_BASE itself, or a plain path that isn't a URL
            method: str = "POST" if "method: 'POST'" in match.group(4) else "GET"
            if url.startswith("/api/history/field/"):
                urls.update(
                    (method, f"/api/history/field/{f}?hours=24") for f in side_fields
                )
                continue
            urls.add((method, re.sub(r"\$\{[^}]*\}", "3", url)))
    # The settings save is a multi-line fetch call the pattern above can't see.
    urls.add(("POST", "/api/history/settings"))
    return urls


def server_endpoints() -> list[str]:
    """The list /api/list-endpoints serves, read from server.py's source."""
    plugins: list[str] = re.findall(
        r'"(\w+)"', server_block(r"GLANCES_PROXY_PLUGINS.*?\((.*?)\)")
    )
    body: str = server_block(
        r"async def list_endpoints.*?endpoints: list\[str\] = \[(.*?)\n    \]"
    )
    return [f"/4/{p}" for p in plugins] + re.findall(r'^\s*"(/[^"]+)"', body, re.M)


def test_frontend_urls_found() -> None:
    urls: set[tuple[str, str]] = frontend_urls()
    # A broken extraction pattern must not pass vacuously.
    assert ("GET", "/api/4/cpu") in urls
    assert ("GET", "/config.json") in urls
    assert ("POST", "/api/history/record") in urls
    assert ("GET", "/api/history?hours=3") in urls
    assert len(urls) >= 25, sorted(urls)


def test_fake_backend(node: str) -> None:
    harness: str = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const [apiPath, urlsJson, endpointsJson] = process.argv.slice(1);
const context = vm.createContext({ Response, URL, URLSearchParams, DOMException, setTimeout, clearTimeout, console });
vm.runInContext(fs.readFileSync(apiPath, 'utf8'), context);
(async () => {
  const { route } = context.HoratioDemo;
  for (const [method, href] of JSON.parse(urlsJson)) {
    const url = new URL(href, 'http://demo.invalid/');
    const answer = route(method, url.pathname, url.searchParams);
    assert(answer, `${method} ${href}: not routed`);
    const [status, body] = answer;
    if (method === 'POST' && url.pathname === '/api/history/settings') {
      assert.equal(status, 403, 'the demo refuses to save settings');
      assert.match(body.error, /demo/);
      continue;
    }
    assert(status < 400, `${method} ${href}: status ${status}`);
    assert(body !== undefined && body !== null, `${method} ${href}: empty body`);
    JSON.stringify(body);
  }
  // The live view's history and the history endpoint agree on shape.
  const hist = route('GET', '/api/history', new URLSearchParams('hours=24'))[1];
  assert(hist.count >= 140 && hist.count <= 145, `24 h at 600 s is ~144 records, got ${hist.count}`);
  assert.equal(hist.records[0].temps, undefined, 'side fields stay out of /api/history');
  assert(hist.records.at(-1).gpus.length === 2 && hist.records.at(-1).ping['1.1.1.1']);
  const since = new Date((hist.now - 1800) * 1000).toISOString();
  const delta = route('GET', '/api/history', new URLSearchParams({ hours: '24', since }))[1];
  assert(delta.count >= 2 && delta.count <= 3, `since= returns only newer records, got ${delta.count}`);
  const year = route('GET', '/api/history', new URLSearchParams('hours=8760'))[1];
  assert.equal(year.now - year.cutoff, 30 * 86400, 'a range past the 30 days held is clamped to them');
  const temps = route('GET', '/api/history/field/temps', new URLSearchParams('hours=1'))[1];
  assert(temps.records.every(r => r.temps && r.timestamp));
  // Endpoint list as the server's.
  assert.deepEqual([...route('GET', '/api/list-endpoints', new URLSearchParams())[1].endpoints].sort(),
                   JSON.parse(endpointsJson).sort());
  // Through the fetch wrapper: an unknown endpoint is a 404, uptime a JSON string.
  const warnings = [];
  context.console = { warn: (...a) => warnings.push(a.join(' ')) };
  const missing = await context.fetch('/api/nope');
  assert.equal(missing.status, 404);
  const uptime = await (await context.fetch('/api/4/uptime')).text();
  assert.match(uptime, /^"\d+ days, \d+:\d\d:\d\d"$/);
})().catch(e => { console.error(e); process.exit(1); });
"""
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            node,
            "-e",
            harness,
            str(FAKE_API),
            json.dumps(sorted(frontend_urls())),
            json.dumps(server_endpoints()),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_build(tmp_path: Path) -> None:
    out: Path = tmp_path / "site"
    # `make demo` builds into docs/ itself: only the copies may be replaced.
    out.mkdir()
    (out / "README.md").write_text("keep me")
    (out / "static").mkdir()
    (out / "static" / "stale.js").write_text("")
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [str(ROOT / "docs" / "build-demo.sh"), str(out)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "HORATIO_DEMO_SKIP_CHARTJS": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    html: str = (out / "index.html").read_text()
    assert '"/static/' not in html
    # The fake backend loads before Chart.js and every frontend script.
    assert html.index('src="demo/fake-api.js"') < html.index(
        'src="static/chart.umd.min.js"'
    )
    assert (out / "demo" / "fake-api.js").is_file()
    assert (out / "static" / "app.js").is_file()
    # The picker's list, and the themes it names, ship with the demo.
    themes: list[str] = json.loads((out / "themes" / "index.json").read_text())
    assert "light" in themes and "classic" in themes
    assert all((out / "themes" / f"{name}.css").is_file() for name in themes)
    assert (out / "README.md").read_text() == "keep me"
    assert not (out / "static" / "stale.js").exists()


def test_build_refuses_checkout() -> None:
    """Building into the checkout would replace index.html and static/ themselves."""
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [str(ROOT / "docs" / "build-demo.sh"), str(ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "HORATIO_DEMO_SKIP_CHARTJS": "1"},
    )
    assert result.returncode != 0
    assert "checkout itself" in result.stderr
    assert (ROOT / "static" / "app.js").is_file()
