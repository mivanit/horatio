"""The static demo (docs/): its build, and the fake backend covering every URL
the frontend requests, so a new endpoint cannot ship without a fake."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from tests.support import ROOT

FAKE_API: Path = ROOT / "docs" / "demo" / "fake-api.js"


def server_block(pattern: str) -> str:
    """Group 1 of `pattern` in server.py; a renamed definition fails by name."""
    match: re.Match[str] | None = re.search(
        pattern, (ROOT / "python" / "server.py").read_text(), re.S
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
            r"(API_BASE\s*\+\s*)?(['`])(/api/[^'`]*|/config\.json|/[\w-]+)\2(?=([\s\S]{0,80}))",
            src,
        ):
            url: str = match.group(3)
            if match.group(1):
                url = "/api/4" + url
            elif url == "/api/4" or not url.startswith(("/api/", "/config.json")):
                continue  # API_BASE itself, or a plain path that isn't a URL
            method: str = (
                "POST"
                if re.match(
                    r"(?:\s*:\s*['`\"][^'`\"]+['`\"])?\s*,\s*\{\s*method:\s*['\"]POST['\"]",
                    match.group(4),
                )
                else "GET"
            )
            if url.startswith("/api/history/field/"):
                urls.update(
                    (method, f"/api/history/field/{f}?hours=24") for f in side_fields
                )
                continue
            urls.add((method, re.sub(r"\$\{[^}]*\}", "3", url)))
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
    assert ("POST", "/api/alarms/retire") in urls
    assert ("GET", "/api/alarms/retire") not in urls
    assert ("POST", "/api/alarms/review") in urls
    assert ("GET", "/api/alarms/review") not in urls
    assert ("GET", "/api/history?hours=3") in urls
    assert len(urls) >= 25, sorted(urls)


def test_fake_backend(node: str) -> None:
    harness: str = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const [apiPath, urlsJson, endpointsJson] = process.argv.slice(1);
let clock = Date.now();
class DemoDate extends Date {
  constructor(...args) { super(...(args.length ? args : [clock])); }
  static now() { return clock; }
}
const context = vm.createContext({ Date: DemoDate, Response, URL, URLSearchParams, DOMException, setTimeout, clearTimeout, console });
vm.runInContext(fs.readFileSync(apiPath, 'utf8'), context);
(async () => {
  const { route } = context.HoratioDemo;
  for (const [method, href] of JSON.parse(urlsJson)) {
    const url = new URL(href, 'http://demo.invalid/');
    const answer = route(method, url.pathname, url.searchParams);
    assert(answer, `${method} ${href}: not routed`);
    const [status, body] = answer;
    if (method === 'POST' && ['/api/history/settings', '/api/alarms/retire', '/api/alarms/review'].includes(url.pathname)) {
      assert.equal(status, 403, 'the demo refuses settings and alarm changes');
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
  // The demo exercises both fan axes, in live readings and historical records.
  const info = route('GET', '/api/gpu-info', new URLSearchParams())[1];
  assert.equal(info.length, 2);
  assert(info.every(g => Number.isFinite(g.fan_percent) && g.fan_percent >= 0 && g.fan_percent <= 100));
  const sensors = route('GET', '/api/sensors', new URLSearchParams())[1];
  assert(sensors.fans.length && sensors.fans.every(f => f.value > 100));
  assert(hist.records.every(r => Object.keys(r.gpu_fans).length === 2 &&
    Object.values(r.gpu_fans).every(v => v >= 0 && v <= 100) &&
    Object.values(r.fans).every(v => v > 100)));
  assert(hist.records.some(r => r.fans.FAN1 === 250), 'the earlier fan incident is visible in sampled history');
  const since = new Date((hist.now - 1800) * 1000).toISOString();
  const delta = route('GET', '/api/history', new URLSearchParams({ hours: '24', since }))[1];
  assert(delta.count >= 2 && delta.count <= 3, `since= returns only newer records, got ${delta.count}`);
  const year = route('GET', '/api/history', new URLSearchParams('hours=8760'))[1];
  assert.equal(year.now - year.cutoff, 30 * 86400, 'a range past the 30 days held is clamped to them');
  const temps = route('GET', '/api/history/field/temps', new URLSearchParams('hours=1'))[1];
  assert(temps.records.every(r => r.temps && r.timestamp));
  // Alarm history is stable across refreshes, and each state has matching
  // evidence in current APIs rather than merely decorative timeline markers.
  const get = (path, query = '') => route('GET', path, new URLSearchParams(query))[1];
  const alarmHistory = get('/api/alarms/history', 'hours=24');
  const fullHistory = get('/api/alarms/history', 'hours=720');
  assert.equal(alarmHistory.now - alarmHistory.cutoff, 86400);
  assert.equal(alarmHistory.truncated, false);
  assert.equal(alarmHistory.skipped, 0);
  assert(alarmHistory.events.length >= 12);
  assert(fullHistory.events.length > alarmHistory.events.length);
  const at = e => Date.parse(e.timestamp) / 1000;
  assert(alarmHistory.events.every((e, i, es) => at(e) >= alarmHistory.cutoff && at(e) <= alarmHistory.now &&
    (!i || at(es[i - 1]) <= at(e))), 'events follow the requested range and observation order');
  const kinds = new Set(alarmHistory.events.map(e => e.event));
  for (const kind of ['raised', 'cleared', 'retired', 'reviewed', 'reopened', 'expired', 'gap']) assert(kinds.has(kind), kind);
  const fanRecoveries = alarmHistory.events.filter(e => e.event === 'cleared' && e.key.startsWith('fan/'));
  assert.equal(fanRecoveries.length, 2);
  assert.equal(fanRecoveries[0].key, fanRecoveries[1].key);
  assert.notEqual(fanRecoveries[0].since, fanRecoveries[1].since, 'a recurrence has a distinct occurrence');
  assert(fanRecoveries.every(e => at(e) - e.since === e.active_for_s));
  const retired = alarmHistory.events.find(e => e.event === 'retired');
  assert(retired.note.includes('Replaced') && retired.since < alarmHistory.cutoff,
    'a closed occurrence remains understandable without its out-of-range start');
  const current = get('/api/alarms');
  const health = get('/api/storage-health');
  assert.deepEqual(current.active.filter(a => a.source === 'state'), health.alarm_items);
  assert.deepEqual(health.alarms, health.alarm_items.map(a => a.text));
  assert.deepEqual(current.held, health.sensor_alarm_held);
  assert.deepEqual(current.reviewed, health.alarm_reviewed);
  assert(current.held.some(a => a.since < alarmHistory.cutoff && a.unchecked_since < alarmHistory.cutoff),
    'a current concern can have no event inside the selected range');
  const reviewed = current.reviewed[0];
  assert(reviewed.reviewed.note && reviewed.reviewed.timestamp < current.now);
  assert.deepEqual(reviewed.evidence, reviewed.reviewed.evidence);
  assert(!current.active.some(a => a.source === reviewed.source && a.key === reviewed.key));
  const reopened = alarmHistory.events.find(e => e.event === 'reopened');
  const oldReview = alarmHistory.events.find(e => e.event === 'reviewed' && e.key === reopened.key);
  assert.equal(reopened.since, oldReview.since);
  assert(reopened.evidence.some(token => !oldReview.evidence.includes(token)), 'new evidence reopens the same alarm');
  assert(current.active.some(a => a.key === reopened.key && a.source === reopened.source));
  assert(!current.active.some(a => a.key === retired.key));
  assert(!current.held.some(a => a.key === retired.key));
  for (const action of ['retire', 'review']) {
    const capability = action === 'retire' ? current.retirement : current.review;
    assert.equal(capability.supported, false);
    assert.equal(capability.password_configured, false);
    const [status, body] = route('POST', '/api/alarms/' + action, new URLSearchParams());
    assert.equal(status, 403);
    assert.match(body.error, /read-only demo/);
  }
  const identities = JSON.stringify(alarmHistory.events);
  clock += 120000;
  assert.equal(JSON.stringify(get('/api/alarms/history', 'hours=24').events), identities,
    'a refresh must not shift incident timestamps or occurrence identity');
  assert.deepEqual(get('/api/alarms').reviewed, current.reviewed, 'read-only actions cannot mutate reviewed evidence');
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


def repo_copy(tmp_path: Path) -> Path:
    """A copy of the checkout to run build-demo.sh in: a test that points it at
    its own checkout must never be able to touch the real one."""
    repo: Path = tmp_path / "repo"
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(".git", ".venv", "logs", "_site", "__pycache__"),
    )
    name: str
    for name in ("index.html", "static", "themes"):
        built: Path = repo / "docs" / name  # a local `make demo` build's copies
        if built.is_dir():
            shutil.rmtree(built)
        elif built.exists():
            built.unlink()
    return repo


def build(script: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), str(out)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "HORATIO_DEMO_SKIP_CHARTJS": "1"},
    )


def test_build_refuses_checkout(tmp_path: Path) -> None:
    """Building into the checkout would replace index.html and static/ themselves,
    also when the checkout is named through a symlink."""
    repo: Path = repo_copy(tmp_path)
    link: Path = tmp_path / "checkout"
    link.symlink_to(repo)
    target: Path
    for target in (repo, link):
        result: subprocess.CompletedProcess[str] = build(
            repo / "docs" / "build-demo.sh", target
        )
        assert result.returncode != 0, target
        assert "checkout itself" in result.stderr
        assert (repo / "static" / "app.js").is_file()
        assert (repo / "index.html").is_file()


def test_build_into_docs_via_symlink(tmp_path: Path) -> None:
    """docs/ named through a symlink is still docs/: its demo/ is the source, not
    a copy to replace."""
    repo: Path = repo_copy(tmp_path)
    link: Path = tmp_path / "docs-link"
    link.symlink_to(repo / "docs")
    result: subprocess.CompletedProcess[str] = build(
        repo / "docs" / "build-demo.sh", link
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "docs" / "demo" / "fake-api.js").is_file()
    assert (repo / "docs" / "index.html").is_file()
