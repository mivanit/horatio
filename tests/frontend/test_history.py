from __future__ import annotations

import os
import subprocess
import tempfile

from tests.support import ROOT


def test_history_fetch_js(node: str) -> None:
    """The browser's history cache (static/app.js fetchHistoryData and helpers),
    run in node against a scripted safeFetch."""
    src: str = (ROOT / "static" / "app.js").read_text()
    cfg_src: str = (ROOT / "static" / "config.js").read_text()

    def between(text: str, start: str, end: str) -> str:
        # The test slices app.js / config.js by these markers; a renamed one is
        # a test failure that names it, not a crash of the whole suite.
        a: int = text.find(start)
        b: int = text.find(end, a + 1)
        if a < 0 or b < 0:
            raise LookupError(f"marker not found: {start if a < 0 else end!r}")
        return text[a:b]

    harness: str = (
        r"""
const assert = require('assert');
let defaultHistoryHours = 24;
const toasts = [];
function showError(m) { toasts.push(m); }
console.warn = () => {};
const calls = [];
let responder = null;
async function safeFetch(url) { calls.push(url); return responder(url); }
const historyCards = new Map();
const document = { querySelector: () => null, getElementById: () => null, querySelectorAll: () => [] };
const HISTORY_METRICS = { cpu: {}, memory: {}, temps: { sideField: 'temps' } };
const draws = [];
function createHistoryChart(canvasId, metric, records, unitType, hours) {
  draws.push([metric, hours]);
  return { records, unitType, data: { datasets: [] }, destroy() {}, update() {} };
}
let thresholds = { network_mbps: 3 }, dashboardConfig = {};
let configBody = null;
"""
        + between(
            src,
            "const historyFetchInflight",
            "// Markup for a failed history fetch",
        )
        + between(
            src,
            "function historyErrorHTML",
            "// Extract metric value from a history record",
        )
        + between(
            src, "async function loadHistoryData", "// Show history for all metrics"
        )
        + between(
            src,
            "async function refreshAllHistory",
            "document.getElementById('refresh-history-btn')?.addEventListener",
        )
        + between(cfg_src, "// Ceilings (MB/s)", "// Read a CSS custom property")
        + between(cfg_src, "async function loadConfig", "// Update card warning state")
        + r"""
const MS = (min) => Date.UTC(2026, 9, 23, 12, min);
const T = (min) => new Date(MS(min)).toISOString().replace('Z', '+00:00');
const recs = (...mins) => mins.map(m => ({ timestamp: T(m) }));
// A server response for a window of `hours` whose clock reads T(nowMin).
const resp = (nowMin, records, extra = {}, hours = 24) => ({ ok: true, data: {
  records, count: records.length, skipped_lines: 0, truncated: false,
  cutoff: MS(nowMin) / 1000 - hours * 3600, now: MS(nowMin) / 1000, ...extra } });
const since = (url) => { const m = url.match(/since=([^&]+)/); return m ? decodeURIComponent(m[1]) : null; };
const ok = (r) => Promise.resolve(r);
const deferred = () => { let r; const p = new Promise(res => { r = res; }); return [p, r]; };
const card = (hours) => {
  const range = { value: String(hours) }, container = { innerHTML: '' };
  return { card: { dataset: { historyId: 'test' }, remove() {},
    querySelector: selector => selector === '.history-range-input' ? range : container },
    chart: null, loadSeq: 0 };
};
(async () => {
  // 1. First load: a full fetch.
  responder = () => ok(resp(25, recs(0, 20, 10)));
  let d = await fetchHistoryData(24);
  assert.strictEqual(since(calls.at(-1)), null, 'first fetch has no since');
  const firstObj = d;
  // 2. Any later fetch of a cached window (a reopened card, a range changed
  //    back, Refresh) tops it up: since = the server's last now - 5 min. The
  //    late record (15) merges in time order, held ones dedupe, records before
  //    the server's cutoff drop, and truncated keeps its window meaning.
  historyCache.get(24).truncated = true;
  responder = () => ok(resp(30, recs(10, 15, 20, 30), { cutoff: MS(5) / 1000 }));
  d = await fetchHistoryData(24);
  assert.strictEqual(Date.parse(since(calls.at(-1))), MS(25) - 300 * 1000, 'reopen tops up from last now - 5 min');
  assert.deepStrictEqual(d.records.map(r => r.timestamp), [T(10), T(15), T(20), T(30)], 'merged, deduped, sorted, trimmed');
  assert.strictEqual(d.count, 4);
  assert.strictEqual(d.truncated, false, 'truncation clears after its missing prefix ages out');
  assert.strictEqual(firstObj.records.length, 3, 'the cached object a chart holds is not mutated');
  const truncatedBase = { records: recs(0, 10), truncated: true };
  for (const [cutoffMin, expected] of [[-5, true], [0, false], [5, false]]) {
    const merged = mergeHistoryDelta(truncatedBase, resp(20, recs(20), { cutoff: MS(cutoffMin) / 1000 }).data);
    assert.strictEqual(merged.truncated, expected, `truncation boundary at ${cutoffMin}`);
  }
  assert.strictEqual(mergeHistoryDelta({ records: [], truncated: true }, resp(20, recs(20)).data).truncated,
                     true, 'an empty cache cannot establish that the missing prefix aged out');
  // 3. Record timestamps never set since: a future-stamped record changes nothing.
  responder = () => ok(resp(40, recs(35, 400)));
  await fetchHistoryData(24, { force: true });
  responder = () => ok(resp(45, recs(42)));
  await fetchHistoryData(24, { force: true });
  assert.strictEqual(Date.parse(since(calls.at(-1))), MS(40) - 300 * 1000, 'since from the server clock, not records');
  // 4. A window with corrupt lines refetches in full; a clean one tops up again.
  responder = () => ok(resp(5, recs(0), { skipped_lines: 3 }, 12));
  await fetchHistoryData(12);
  responder = () => ok(resp(6, recs(0, 5), {}, 12));
  d = await fetchHistoryData(12, { force: true });
  assert.strictEqual(since(calls.at(-1)), null, 'window with skipped lines -> full refetch');
  assert.strictEqual(d.skipped_lines, 0);
  responder = () => ok(resp(8, recs(7), {}, 12));
  await fetchHistoryData(12, { force: true });
  assert.notStrictEqual(since(calls.at(-1)), null, 'clean window -> top-up again');
  // 5. A backend that reports no clock (OpenWrt): always a full fetch, which replaces.
  responder = () => ok({ ok: true, data: { records: recs(1, 2), count: 2, skipped_lines: 0 } });
  await fetchHistoryData(6);
  responder = () => ok({ ok: true, data: { records: recs(2, 3), count: 2, skipped_lines: 0 } });
  d = await fetchHistoryData(6, { force: true });
  assert.strictEqual(since(calls.at(-1)), null, 'no now -> no since');
  assert.deepStrictEqual(d.records.map(r => r.timestamp), [T(2), T(3)], 'full window replaces');
  let before = calls.length;
  d = await fetchHistoryData(6);
  assert.strictEqual(calls.length, before, 'no clock: a toggle / reopen is served from the cache');
  assert.strictEqual(d.count, 2);
  // ...and a since= response without cutoff (an older server) is a full window too.
  assert.deepStrictEqual(mergeHistoryDelta({ records: recs(0) }, { records: recs(5), count: 1 }).records,
                         recs(5), 'no cutoff -> replace');
  // 6. A truncated top-up (gap) refetches in full, without since.
  let n = 0;
  responder = (url) => { n++; return since(url)
    ? ok(resp(50, recs(50), { truncated: true })) : ok(resp(50, recs(30, 40, 50))); };
  d = await fetchHistoryData(24, { force: true });
  assert.strictEqual(n, 2, 'truncated delta -> second, full request');
  assert.strictEqual(since(calls.at(-1)), null);
  assert.strictEqual(d.count, 3);
  // 7. A card opened during a Refresh joins it.
  const [p, resolve] = deferred();
  responder = () => p;
  before = calls.length;
  const refreshing = fetchHistoryData(24, { force: true });
  const opened = fetchHistoryData(24);
  assert.strictEqual(calls.length - before, 1, 'non-force caller shares the in-flight request');
  resolve(resp(60, recs(40, 50, 60)));
  assert.strictEqual((await opened).count, 4);
  assert.strictEqual((await refreshing).count, 4);
  // 8. A fetch overtaken by a Refresh does not overwrite the Refresh's result.
  const [p3, resolve3] = deferred();
  responder = () => p3;
  const slow = fetchHistoryData(24);
  responder = () => ok(resp(70, recs(70)));
  await fetchHistoryData(24, { force: true });
  resolve3(resp(65, recs(65)));
  await slow;
  assert.strictEqual(historyCache.get(24).now, MS(70) / 1000, 'superseded fetch is not cached');
  // 9. The last card closes during a Refresh (the real removeHistoryCard), then a
  //    card reopens: it starts its own fetch, which is cached; the old one isn't.
  historyCards.set('cpu', card(24));
  const [p2, resolve2] = deferred();
  responder = () => p2;
  const late = fetchHistoryData(24, { force: true });
  removeHistoryCard('cpu');
  assert.strictEqual(historyCache.size, 0, 'closing the last card clears the cache');
  responder = () => ok(resp(90, recs(80, 90)));
  const reopened = fetchHistoryData(24);
  assert.notStrictEqual(reopened, late, 'a reopened card does not join the discarded fetch');
  await reopened;
  resolve2(resp(75, recs(75)));
  await late;
  assert.strictEqual(historyCache.get(24)?.count, 2, 'the reopened fetch is cached, the late one is not');
  // 10. A failed fetch keeps the cached window.
  responder = () => ok({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  d = await fetchHistoryData(24, { force: true });
  assert.strictEqual(d.ok, false);
  assert.strictEqual(historyCache.get(24).count, 2, 'failure is not cached; window kept');
  // ...and a non-Refresh fetch that fails (a toggle or a card opened while the
  // server restarts) keeps drawing the cached window instead of an error box.
  d = await fetchHistoryData(24);
  assert.strictEqual(d.ok, true, 'failed top-up falls back to the cached window');
  assert.strictEqual(d.count, 2);
  // The same fallback applies when the delta succeeded but was truncated,
  // requiring a full read which then failed. Explicit Refresh still reports it.
  const cached = historyCache.get(24);
  responder = url => since(url) ? ok(resp(95, recs(95), { truncated: true }))
    : ok({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  before = calls.length;
  d = await fetchHistoryData(24);
  assert.strictEqual(calls.length - before, 2, 'truncated top-up attempted the full fallback');
  assert.strictEqual(d, cached, 'failed full fallback returns the cached window');
  d = await fetchHistoryData(24, { force: true });
  assert.strictEqual(d.ok, false, 'explicit Refresh reports failed full fallback');
  assert.strictEqual(d.errorType, 'timeout');
  assert.strictEqual(historyCache.get(24), cached, 'failed full fallback does not replace the cache');
  const fallbackCard = card(24);
  fallbackCard.unitType = 'gb';
  historyCards.set('memory', fallbackCard);
  await loadHistoryData('memory', 24);
  assert.strictEqual(fallbackCard.chart?.unitType, 'gb', 'unit toggle redraws cached data after full fallback failure');
  assert.strictEqual(fallbackCard.chart.records, cached.records);
  // A toggle and a newly opened card joining a forced fetch have their own
  // failure policy, including when the forced request retries a truncated delta.
  for (const fullRetry of [false, true]) {
    historyCards.clear(); historyCards.set('memory', fallbackCard);
    fallbackCard.unitType = 'percent';
    responder = () => ok(resp(90, recs(80, 90)));
    await loadHistoryData('memory', 24);
    const [failing, finishFailure] = deferred();
    responder = url => fullRetry && since(url) ? ok(resp(95, recs(95), { truncated: true })) : failing;
    before = calls.length;
    const forceCaller = fetchHistoryData(24, { force: true });
    fallbackCard.unitType = 'gb';
    const toggleCaller = loadHistoryData('memory', 24);
    const joiningCard = card(24);
    historyCards.set('cpu', joiningCard);
    const openCaller = loadHistoryData('cpu', 24);
    finishFailure({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
    assert.strictEqual((await forceCaller).ok, false, 'forced caller still receives the failure');
    await Promise.all([toggleCaller, openCaller]);
    assert.strictEqual(calls.length - before, fullRetry ? 2 : 1, 'joining cards share network work');
    assert.strictEqual(fallbackCard.chart.unitType, 'gb', 'joining toggle redraws cached data in the selected unit');
    assert.deepStrictEqual(joiningCard.chart?.records, recs(80, 90), 'joining card draws the cached window');
  }
  // Side fields have independent caches; a joined failure must use the same key.
  responder = () => ok(resp(90, recs(85), { field: 'temps' }));
  const cachedTemps = await fetchHistoryData(24, { field: 'temps' });
  const [tempsFailure, finishTemps] = deferred();
  responder = () => tempsFailure;
  const forceTemps = fetchHistoryData(24, { force: true, field: 'temps' });
  const joiningTemps = fetchHistoryData(24, { field: 'temps' });
  finishTemps({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  assert.strictEqual((await forceTemps).ok, false);
  assert.strictEqual(await joiningTemps, cachedTemps, 'joined side-field failure uses its own cached data');
  // With no cache, joining a failed forced fetch still reports an error.
  const [uncachedFailure, finishUncached] = deferred();
  responder = () => uncachedFailure;
  const forceUncached = fetchHistoryData(48, { force: true });
  const joiningUncached = fetchHistoryData(48);
  finishUncached({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  assert.strictEqual((await forceUncached).ok, false);
  assert.strictEqual((await joiningUncached).ok, false, 'no cached data means no fallback');
  // 11. Refresh skips a card whose range changed while its fetch was in flight
  //     (the real refreshAllHistory); the others redraw.
  const cpu = card(24), mem = card(24);
  historyCards.clear(); historyCards.set('cpu', cpu); historyCards.set('memory', mem);
  draws.length = 0;
  const [p4, resolve4] = deferred();
  responder = () => p4;
  const refresh = refreshAllHistory();
  cpu.card.querySelector('.history-range-input').value = '1';   // the user types 1 h meanwhile
  resolve4(resp(95, recs(95)));
  await refresh;
  assert.deepStrictEqual(draws, [['memory', 24]], 'only the unchanged card redraws at the old range');
  // 12. Exercise the complete Refresh -> close -> reopen -> late response path,
  //     including the actual loadHistoryData drawing gate, not just the cache.
  historyCards.clear(); historyCache.clear(); draws.length = 0;
  historyCards.set('cpu', card(24));
  responder = () => ok(resp(100, recs(90, 100)));
  await loadHistoryData('cpu', 24);
  const [p5, resolve5] = deferred();
  responder = () => p5;
  const oldRefresh = refreshAllHistory();
  removeHistoryCard('cpu');
  const newCard = card(24);
  historyCards.set('cpu', newCard);
  responder = () => ok(resp(110, recs(100, 110)));
  await loadHistoryData('cpu', 24);
  const newChart = newCard.chart;
  resolve5(resp(105, recs(100, 105)));
  await oldRefresh;
  assert.strictEqual(newCard.chart, newChart, 'old Refresh cannot redraw the reopened card');
  assert.deepStrictEqual(newCard.chart.records, recs(100, 110), 'reopened chart keeps the newer data');
  assert.deepStrictEqual(historyCache.get(24).records, recs(100, 110), 'reopened cache keeps the newer data');
  assert.strictEqual(draws.length, 2, 'only the two card opens draw');
  // 13. A newer Refresh supersedes the old one at start, regardless of which
  //     response finishes first. An old result must not suppress the new one.
  for (const oldFirst of [true, false]) {
    const [older, finishOlder] = deferred(), [newer, finishNewer] = deferred();
    responder = () => older;
    const olderRefresh = refreshAllHistory();
    responder = () => newer;
    const newerRefresh = refreshAllHistory();
    const previousChart = newCard.chart;
    const minute = oldFirst ? 120 : 130;
    if (oldFirst) {
      finishOlder(resp(minute - 5, recs(minute - 5)));
      await olderRefresh;
      assert.strictEqual(newCard.chart, previousChart, 'superseded Refresh does not draw even when it finishes first');
    }
    finishNewer(resp(minute, recs(minute)));
    await newerRefresh;
    const freshChart = newCard.chart;
    assert.strictEqual(freshChart.records.at(-1).timestamp, T(minute), 'newest Refresh draws its result');
    if (!oldFirst) {
      finishOlder(resp(minute - 5, recs(minute - 5)));
      await olderRefresh;
      assert.strictEqual(newCard.chart, freshChart, 'late superseded Refresh cannot overwrite the fresh chart');
    }
  }
  // 14. A clock rewind expands the window's older edge and can stamp new
  // records before the previous watermark. Refetch in full instead of losing
  // those records; either response clock field is enough to detect the rewind.
  for (const metadata of ['both', 'now', 'cutoff']) {
    historyCache.delete(1);
    let clockMin = 100, timeline = recs(20, 50, 100), rewound = false;
    responder = url => {
      const watermark = since(url);
      const records = timeline.filter(r => Date.parse(r.timestamp) >= MS(clockMin) - 3600000 &&
        (!watermark || Date.parse(r.timestamp) > Date.parse(watermark)));
      const response = resp(clockMin, records, {}, 1);
      if (rewound && metadata === 'now') delete response.data.cutoff;
      if (rewound && metadata === 'cutoff') delete response.data.now;
      return ok(response);
    };
    d = await fetchHistoryData(1);
    assert.deepStrictEqual(d.records, recs(50, 100));
    clockMin = 70; timeline = recs(20, 50, 60, 65, 100); rewound = true;
    before = calls.length;
    d = await fetchHistoryData(1, { force: true });
    assert.strictEqual(calls.length - before, 2, `${metadata} rewind triggers a full retry`);
    assert.strictEqual(since(calls.at(-1)), null, 'rewind retry has no watermark');
    assert.deepStrictEqual(d.records, timeline, 'full retry restores the older edge and newly backdated samples');
    if (metadata === 'both') {
      clockMin = 75; timeline = recs(20, 50, 60, 65, 73, 100);
      before = calls.length;
      d = await fetchHistoryData(1);
      assert.strictEqual(calls.length - before, 1, 'incremental reads resume after the rewind');
      assert.strictEqual(Date.parse(since(calls.at(-1))), MS(65), 'next watermark uses the corrected server clock');
      assert.deepStrictEqual(d.records, timeline);
    }
  }
  // 15. config.js: limits from /config.json, validated like the server's _cfg_num.
  responder = () => ({ ok: true, data: configBody });
  configBody = { thresholds: { network_mbps: 9 }, limits: { network_mbps: 5, disk_io_mbps: 'x', bogus: 7 } };
  await loadConfig();
  assert.deepStrictEqual(limits, { network_mbps: 5, disk_io_mbps: 15000 }, 'valid limit taken, bad one ignored');
  for (const bad of [0, -1, true, 1e10, null, Infinity]) {
    configBody = { limits: { network_mbps: bad } };
    await loadConfig();
    assert.strictEqual(limits.network_mbps, 5, `limit ${bad} rejected`);
  }
  assert.deepStrictEqual([clampRate(-3, 10), clampRate(50, 10), clampRate(4, 10), clampRate(NaN, 10), clampRate(undefined, 10)],
                         [0, 10, 4, 0, 0], 'clampRate');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    )
    f: tempfile._TemporaryFileWrapper[str]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        script: str = f.name
    try:
        r: subprocess.CompletedProcess[str] = subprocess.run(
            [node, script], capture_output=True, text=True, timeout=30
        )
        assert r.returncode == 0 and r.stdout.strip() == "OK", (
            f"history cache (top-up on reopen, order, server clock, skipped, no clock, gap, in-flight, superseded, close/reopen, per-caller failure + full-read cached fallback, clock rewind, aged-out truncation), refresh vs range change/reopen/newer refresh, config.js limits: {(r.stderr or r.stdout).strip()[-600:]}"
        )
    finally:
        os.unlink(script)
