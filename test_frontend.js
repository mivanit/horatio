// Focused browser behavior checks using the actual application functions.
// Run with: node test_frontend.js (Node with built-in fetch; no dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const vm = require('node:vm');

const read = name => fs.readFileSync(path.join(__dirname, 'static', name), 'utf8');
const app = read('app.js'), config = read('config.js'), charts = read('charts.js');
function between(source, start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, `source markers: ${start} -> ${end}`);
  return source.slice(a, b);
}
const plain = value => JSON.parse(JSON.stringify(value));
const evaluate = (context, source) => vm.runInContext(source, context);

// Preserve the DOM behavior relevant to GPU recovery: textContent destroys
// child elements, while the parent's dataset survives.
function makeDOM(ids) {
  const elements = new Map();
  class Element {
    constructor(id) {
      this.id = id;
      this.dataset = {};
      this.style = {};
      this.children = [];
      this.classList = { add() {}, remove() {}, toggle() {} };
    }
    clearChildren() {
      this.children.forEach(id => elements.delete(id));
      this.children = [];
    }
    set textContent(value) { this.clearChildren(); this.text = String(value); }
    get textContent() { return this.text || ''; }
    set innerHTML(value) {
      this.clearChildren();
      this.html = String(value);
      for (const match of value.matchAll(/id="([^"]+)"/g)) {
        this.children.push(match[1]);
        elements.set(match[1], new Element(match[1]));
      }
    }
    get innerHTML() { return this.html || ''; }
  }
  ids.forEach(id => elements.set(id, new Element(id)));
  return { elements, document: { getElementById: id => elements.get(id) || null, querySelector: () => null } };
}
const makeChart = () => ({ data: { datasets: [{ data: [] }, { data: [] }] }, options: { scales: { y: {} } }, update() {} });

function liveContext(ids) {
  const dom = makeDOM(ids), statuses = {}, errors = [];
  const context = vm.createContext({
    ...dom, statuses, errors, historyLength: 3, dataCache: {},
    thresholds: { gpu_util_percent: 80, gpu_memory_percent: 80, temp_celsius: 60, ping_ms: 100 },
    GPU_COLORS: ['#123456'], getCssColor: () => '#123456',
    updateCardNoData: (id, ok) => { statuses[id] = ok; }, updateCardWarning() {},
    console: { error: (...args) => errors.push(args.map(String).join(' ')) },
    performance: { now: () => 0 }, requestAnimationFrame: callback => callback(500),
  });
  evaluate(context,
    between(charts, '// Data stores', '// Generate time labels') +
    between(config, 'function animateValue', '// Fade transition') +
    between(config, 'function getCache', '// The CPU core temperatures') +
    between(app, 'function guard', 'async function fetchDataOnce'));
  return context;
}

async function testFetchBodyTimeout() {
  let stalledResponses = 0;
  const server = http.createServer((request, response) => {
    if (request.url === '/stall') {
      stalledResponses++;
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.write('{"value":');  // Headers arrive; the body never finishes.
    } else if (request.url === '/error') {
      response.writeHead(503);
      response.end('unavailable');
    } else {
      response.setHeader('Content-Type', 'application/json');
      response.end('{"value":42}');
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const timers = new Set();
  const context = vm.createContext({
    availableEndpoints: null, fetch, AbortController,
    setTimeout(callback, delay) {
      const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay);
      timers.add(timer);
      return timer;
    },
    clearTimeout(timer) { timers.delete(timer); clearTimeout(timer); },
  });
  evaluate(context, between(config, 'async function safeFetch', '// Data cache for reducing API calls'));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const success = await context.safeFetch(base + '/ok', {}, 1000);
    assert.equal(success.data.value, 42);
    assert.equal(timers.size, 0, 'successful reads clear the deadline');
    const failure = await context.safeFetch(base + '/error', {}, 1000);
    assert.equal(failure.status, 503);
    assert.equal(timers.size, 0, 'HTTP failures clear the deadline');
    for (const fallback of [{}, '']) {
      let watchdog;
      try {
        const result = await Promise.race([
          context.safeFetch(base + '/stall', fallback, 200),
          new Promise((resolve, reject) => {
            watchdog = setTimeout(() => reject(new Error('safeFetch hung after receiving headers')), 1500);
          }),
        ]);
        assert.equal(result.ok, false);
        assert.equal(result.errorType, 'timeout', 'the deadline covers JSON and text body reads');
        assert.equal(result.data, fallback);
        assert.equal(timers.size, 0, 'aborted reads clear the deadline');
      } finally { clearTimeout(watchdog); }
    }
    assert.equal(stalledResponses, 2, 'both stalled bodies reached the browser');
  } finally {
    timers.forEach(clearTimeout);
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}

function testGpuRecovery() {
  const context = liveContext(['gpu-value', 'gpu-mem-value']);
  context.charts = { gpu: makeChart(), gpuMem: makeChart() };
  context.updateGpuLegend = () => {};
  context.dataCache.gpuInfo = { data: [{ index: 0, memory_total: 24576 }] };
  evaluate(context, 'function tickGpu() {' + between(app, "  guard('gpu',", "  guard('network',") + '}');
  const sample = () => { context.gpu = { ok: true, data: [{ proc: 50, mem: 25 }] }; context.tickGpu(); };
  sample();
  assert.equal(context.elements.get('gpu-mem-used-0').textContent, '6.0');
  context.gpu = { ok: true, data: [] };
  context.tickGpu();
  assert.equal(context.elements.get('gpu-value').textContent, 'N/A');
  assert.equal(context.elements.has('gpu-usage-0'), false, 'empty response removed the spans');
  sample();
  sample();
  assert.deepEqual(context.errors, [], 'recovered GPU polls do not throw');
  assert.equal(context.elements.get('gpu-usage-0').textContent, '50');
  assert.equal(context.elements.get('gpu-mem-used-0').textContent, '6.0');
  assert.equal(context.statuses.gpu, true);
  assert.equal(context.statuses['gpu-mem'], true);
}

function testGpuUnavailableReadings() {
  const context = liveContext(['gpu-value', 'gpu-mem-value']);
  context.charts = { gpu: makeChart(), gpuMem: makeChart() };
  context.updateGpuLegend = () => {};
  let now = 1950, frames = [];
  context.performance.now = () => now;
  context.requestAnimationFrame = callback => frames.push(callback);
  const frame = at => {
    now = at;
    const pending = frames;
    frames = [];
    pending.forEach(callback => callback(now));
  };
  const warnings = {};
  context.updateCardWarning = (id, value, threshold) => { warnings[id] = context.statuses[id] && value > threshold; };
  context.dataCache.gpuInfo = { data: [{ index: 0, memory_total: 24576 }, { index: 1, memory_total: 81920 }] };
  evaluate(context, 'function tickGpu() {' + between(app, "  guard('gpu',", "  guard('network',") + '}');
  const tick = data => { context.gpu = { ok: true, data }; context.tickGpu(); };
  const label = id => context.elements.get(id).textContent;
  tick([{ proc: 95, mem: 90 }]);
  assert(warnings.gpu && warnings['gpu-mem']);
  frame(1966);
  now = 2020;  // A fast poll follows a slow one, before its old animation ends.
  // The installed Glances NVIDIA producer keeps the row, with null fields,
  // when the corresponding NVML calls raise NVMLError.
  tick([{ gpu_id: 'nvidia0', proc: null, mem: null, temperature: 55 }]);
  assert.equal(evaluate(context, 'history.gpus[0].at(-1)'), null);
  assert.equal(evaluate(context, 'history.gpuMems[0].at(-1)'), null);
  assert.equal(label('gpu-usage-0'), '--');
  assert.equal(label('gpu-mem-used-0'), '--');
  assert.equal(label('gpu-mem-total-0'), '24.0');
  assert.equal(context.statuses.gpu, false);
  assert.equal(context.statuses['gpu-mem'], false);
  assert(!warnings.gpu && !warnings['gpu-mem']);
  frame(2350);
  assert.equal(label('gpu-usage-0'), '--', 'old frames cannot restore unavailable utilization');
  assert.equal(label('gpu-mem-used-0'), '--', 'old frames cannot restore unavailable memory');
  tick([{ proc: 0, mem: 0 }]);
  assert.equal(label('gpu-usage-0'), '0', 'zero utilization is a valid reading');
  assert.equal(label('gpu-mem-used-0'), '0.0', 'zero used memory is a valid reading');
  assert(context.statuses.gpu && context.statuses['gpu-mem']);
  tick([{ proc: null, mem: 25 }]);
  assert.equal(context.statuses.gpu, false);
  assert.equal(context.statuses['gpu-mem'], true);
  assert.equal(label('gpu-mem-used-0'), '6.0');
  tick([{ proc: 50, mem: null }]);
  assert.equal(context.statuses.gpu, true);
  assert.equal(context.statuses['gpu-mem'], false);
  tick([{ proc: null, mem: 50 }, { proc: 90, mem: null }]);
  assert.deepEqual(plain(evaluate(context, 'history.gpus.map(a => a.at(-1))')), [null, 90]);
  assert.deepEqual(plain(evaluate(context, 'history.gpuMems.map(a => a.at(-1))')), [12, null]);
  assert(context.statuses.gpu && context.statuses['gpu-mem'], 'partial multi-GPU readings remain visible');
  assert(warnings.gpu && !warnings['gpu-mem']);
  for (const memory_total of [null, 0, NaN, Infinity]) {
    context.dataCache.gpuInfo.data = [{ index: 0, memory_total }];
    tick([{ proc: 50, mem: 90 }]);
    assert.equal(context.statuses['gpu-mem'], false, 'memory needs a readable total');
    assert.equal(label('gpu-mem-used-0'), '--');
    assert.equal(label('gpu-mem-total-0'), '--');
    assert.equal(evaluate(context, 'history.gpuMems[0].at(-1)'), null);
  }
  assert.deepEqual(context.errors, []);
}

function testDriveTemperatureFreshness() {
  const context = liveContext(['temps-value', 'fans-card', 'power-card']);
  let now = 100000000;
  context.Date = { now: () => now };
  context.sensors = { ok: false };
  context.gpu = { ok: false };
  context.charts = { temps: makeChart(), fans: makeChart() };
  context.sensorsAgeText = () => '';
  context.abbreviateSensorLabel = label => label;
  context.getTempColor = () => '#123456';
  evaluate(context,
    between(config, 'const CORE_TEMP_LABEL', 'function formatTimeAgo') +
    'function tickTemps() {' + between(app, "  guard('temps',", "  guard('load',") + '}');
  context.dataCache.storageHealth = {
    lastUpdate: now,
    data: { status: 'ok', age_sec: 59 * 60, drives: [{ device: 'sda', temperature_c: 35 }] },
  };
  context.tickTemps();
  assert.equal(context.statuses.temps, true);
  assert.deepEqual(plain(context.charts.temps.data.datasets[0].data), [35]);
  // Failed HTTP refreshes leave the last successful cache untouched.
  now += 2 * 60000;
  context.tickTemps();
  assert.equal(context.statuses.temps, false, 'the snapshot expires based on its age before and after retrieval');
  // Expired drive data must not hide fresh readings from another source.
  context.sensors = { ok: true, data: [{ type: 'temperature_core', label: 'Board', value: 42 }] };
  context.tickTemps();
  assert.equal(context.statuses.temps, true);
  assert.deepEqual(plain(context.charts.temps.data.labels), ['Board']);
  // A recovered HTTP fetch restores the drive reading.
  context.dataCache.storageHealth = {
    lastUpdate: now, data: { status: 'ok', age_sec: 10, drives: [{ device: 'sda', temperature_c: 36 }] },
  };
  context.tickTemps();
  assert.deepEqual(plain(context.charts.temps.data.labels), ['Board', 'sda']);
  for (const age_sec of [undefined, null, '10', Infinity, NaN]) {
    context.dataCache.storageHealth.data.age_sec = age_sec;
    assert.equal(context.getFreshSnapshot('storageHealth', 3600000), null, 'invalid snapshot ages are not fresh');
  }
  context.dataCache.storageHealth.data.age_sec = 0;
  context.dataCache.storageHealth.data.status = 'stale';
  assert.equal(context.getFreshSnapshot('storageHealth', 3600000), null, 'server stale status remains authoritative');
  context.dataCache.storageHealth.data.status = 'ok';
  now += 24 * 3600000;
  assert.equal(context.getFreshSnapshot('storageHealth', 3600000), null, 'long-failed refetches cannot retain fresh drive data');
  assert.deepEqual(context.errors, []);
}

function testSensorFreshness() {
  const context = liveContext(['temps-value', 'fans-card', 'fans-value', 'power-card', 'power-value',
    'power-bar', 'hardware-content', 'hardware-value', 'snapshot-age']);
  let now = 100000000;
  context.Date = { now: () => now };
  context.sensors = { ok: false };
  context.gpu = { ok: false };
  context.charts = { temps: makeChart(), fans: makeChart() };
  context.sensorsAgeText = () => '';
  context.abbreviateSensorLabel = value => value;
  context.getTempColor = () => '#123456';
  context.escapeHtml = String;
  context.hwHint = () => '';
  context.segColorVar = () => '--color-accent';
  context.updateCardCritical = () => {};
  context.document.querySelector = () => context.elements.get('snapshot-age');
  evaluate(context,
    between(config, 'const CORE_TEMP_LABEL', 'function formatTimeAgo') +
    between(app, 'function renderHardware', '// Power bar segment colours') +
    'function tickTemps() {' + between(app, "  guard('temps',", "  guard('load',") + '}');
  const snapshot = { status: 'ok', age_sec: 299, alarms: [],
    temps: [{ label: 'BMC Board', value: 42 }], fans: [{ label: 'Fan1', value: 1200 }],
    power: [{ label: 'Pwr Consumption', value: 100, reading: '100 Watts' }] };
  context.dataCache.sensors = { lastUpdate: now, data: snapshot };
  const render = () => {
    context.tickTemps();
    // Storage health still succeeds while sensor refetches fail and leave the
    // previous sensor cache untouched. A total backend outage shows an error.
    context.renderHardware({ status: 'ok', age_sec: 30, alarms: [] });
  };
  render();
  assert.equal(context.elements.get('hardware-value').textContent, 'all clear');
  assert.equal(context.statuses.temps, true);
  assert.equal(context.elements.get('fans-card').style.display, '');
  assert.equal(context.elements.get('power-card').style.display, '');
  now += 2000;
  render();
  assert.equal(context.elements.get('hardware-value').textContent, 'sensors stale');
  assert(!context.elements.get('hardware-content').innerHTML.includes('no alarms'));
  assert.equal(context.statuses.temps, false, 'a 301-second-old snapshot is unavailable');
  assert.equal(context.elements.get('fans-card').style.display, 'none');
  assert.equal(context.elements.get('power-card').style.display, 'none');
  context.sensors = { ok: true, data: [{ type: 'temperature_core', label: 'CPU', value: 39 }] };
  render();
  assert.deepEqual(plain(context.charts.temps.data.labels), ['CPU'], 'live Glances temperatures stay visible');
  context.dataCache.sensors = { lastUpdate: now, data: { ...snapshot, age_sec: 0, alarms: ['Fan fault'] } };
  render();
  assert.equal(context.elements.get('hardware-value').textContent, '1 issue');
  assert(context.elements.get('hardware-content').innerHTML.includes('Fan fault'));
  assert.deepEqual(plain(context.charts.temps.data.labels), ['CPU', 'BMC Board']);
  assert.equal(context.elements.get('fans-card').style.display, '');
  assert.equal(context.elements.get('power-card').style.display, '');
  context.dataCache.sensors.data.status = 'stale';
  render();
  assert.equal(context.elements.get('hardware-value').textContent, 'sensors stale', 'server stale status remains authoritative');
  assert.deepEqual(context.errors, []);
}

async function testPausedStatus() {
  for (const ok of [true, false]) {
    const dom = makeDOM(['status', 'status-text', 'spinner', 'status-tooltip', 'toggle-btn']);
    dom.elements.get('status').querySelector = selector => dom.elements.get(selector.slice(1));
    let finish;
    const completed = new Promise(resolve => { finish = resolve; });
    const context = vm.createContext({ ...dom, isConnected: true, pollingInterval: 1, slowPollingInterval: 2,
      disconnectTime: null, clearInterval() {}, cardStatus: { cpu: { ok } },
      COLORS: { success: 'green', errorLight: 'red', partial: 'orange' },
      async fetchDataOnce() { await completed; context.updateConnectionStatus(); },
    });
    evaluate(context,
      between(config, 'function updateConnectionStatus', '// Something is actually bad') +
      between(app, 'let fetchInFlight', '// One card') +
      between(app, 'function toggleConnection', '// Apply config to DOM'));
    const pending = context.fetchData();
    context.toggleConnection();
    assert.equal(dom.elements.get('status-text').textContent, 'Paused');
    finish();
    await pending;
    assert.equal(dom.elements.get('status-text').textContent, 'Paused', 'completed polls cannot overwrite the paused state');
    assert.equal(dom.elements.get('spinner').style.animationPlayState, 'paused');
    context.isConnected = true;
    context.updateConnectionStatus();
    assert.equal(dom.elements.get('status-text').textContent, ok ? 'Connected' : 'Disconnected');
  }
}

async function testStartupPolling() {
  for (const action of ['normal', 'pause', 'pause-resume']) {
    const dom = makeDOM(['status', 'status-text', 'spinner', 'status-tooltip', 'toggle-btn']);
    dom.elements.get('status').querySelector = selector => dom.elements.get(selector.slice(1));
    let finishDiscovery;
    const discovery = new Promise(resolve => { finishDiscovery = resolve; });
    const timers = new Map(), calls = { fast: 0, slow: 0, static: 0, setup: 0 };
    let nextTimer = 1;
    const context = vm.createContext({
      ...dom, isConnected: true, pollingInterval: null, slowPollingInterval: null, disconnectTime: null,
      pollInterval: 2000, SLOW_POLL_INTERVAL: 30000, historyLength: 3,
      COLORS: { success: 'green', errorLight: 'red', partial: 'orange' }, cardStatus: { cpu: { ok: true } },
      dataCache: Object.fromEntries(['drives', 'processes', 'networkProcesses', 'diskProcesses']
        .map(key => [key, { data: [], lastUpdate: 1 }])),
      charts: Object.fromEntries(['cpu', 'cpuCoresSpread', 'mem', 'swap', 'net', 'disk', 'gpu', 'gpuMem', 'ping', 'diskDrives']
        .map(key => [key, makeChart()])),
      loadConfig: async () => {}, applyConfigToDom() {}, fetchEndpointList: () => discovery,
      hideUnavailableCards() { context.updateConnectionStatus(); },
      initProcessTableListeners() { calls.setup++; }, fillHistoryGaps() {},
      fetchStaticData() { calls.static++; }, fetchSlowData() { calls.slow++; },
      async fetchDataOnce() { calls.fast++; context.updateConnectionStatus(); },
      setInterval(callback, delay) { const id = nextTimer++; timers.set(id, { callback, delay }); return id; },
      clearInterval(id) { timers.delete(id); },
    });
    evaluate(context,
      between(config, 'function updateConnectionStatus', '// Something is actually bad') +
      between(app, 'let fetchInFlight', '// One card') +
      between(app, 'function startPolling', '// Apply config to DOM'));
    evaluate(context, 'globalThis.startup = ' + between(app, 'loadConfig().then', '// Set up toggle button'));
    await Promise.resolve();  // Config loaded; endpoint discovery is still pending.
    if (action !== 'normal') context.toggleConnection();
    if (action === 'pause-resume') {
      context.toggleConnection();
      assert.equal(timers.size, 2);
    }
    finishDiscovery();
    await context.startup;
    assert.equal(calls.setup, 1, 'discovery/listener setup completes even while paused');
    if (action === 'pause') {
      assert.equal(timers.size, 0, 'startup cannot restart paused timers');
      assert.deepEqual([calls.fast, calls.slow, calls.static], [0, 0, 0], 'startup does not fetch while paused');
      assert.equal(dom.elements.get('status-text').textContent, 'Paused');
      context.toggleConnection();
    }
    assert.equal(timers.size, 2, 'startup and reconnect share one pair of timers');
    assert.deepEqual([...timers.values()].map(timer => timer.delay).sort((a, b) => a - b), [2000, 30000]);
    await new Promise(setImmediate);  // Finish the immediate reconnect poll before a timer tick.
    const before = { ...calls };
    await Promise.all([...timers.values()].map(timer => timer.callback()));
    assert.equal(calls.fast, before.fast + 1);
    assert.equal(calls.slow, before.slow + 1);
    context.toggleConnection();
    assert.equal(timers.size, 0, 'Pause clears every timer, including after an early reconnect');
    assert.equal(dom.elements.get('status-text').textContent, 'Paused');
    context.toggleConnection();
    assert.equal(timers.size, 2, 'normal reconnect restores one pair');
    context.toggleConnection();
    assert.equal(timers.size, 0);
  }
}

function testPingLoss() {
  const context = liveContext(['ping-value-0']);
  context.dashboardConfig = { ping_targets: [{ address: 'example.test' }] };
  context.DEFAULT_PING_TARGETS = [];
  context.charts = { ping: makeChart() };
  context.charts.ping.data.datasets[0].label = 'loss';
  context.pingBandPattern = () => 'hatch';
  evaluate(context,
    'history.pings = [Array(historyLength).fill(null)];\n' +
    between(app, 'function fillHistoryGaps', '// Render functions for cached cards') +
    'function tickPing() {' + between(app, "  guard('ping',", "  guard('processes',") + '}');
  const tick = up => {
    context.ping = { ok: true, data: { targets: { 'example.test': up ? { latency_ms: 20 } : { error: 'timeout' } } } };
    context.tickPing();
    return plain(context.charts.ping.data.datasets[0].data);
  };
  assert.deepEqual(tick(false), [null, null, 1], 'a target down from startup is an outage; initial padding is not');
  tick(true);
  tick(false);
  tick(false);
  assert.deepEqual(tick(false), [1, 1, 1], 'the outage remains after the last success ages out');
  const band = () => plain(context.charts.ping.data.datasets[0].data);
  const colors = () => plain(context.charts.ping.data.datasets[0].backgroundColor);
  const clear = 'rgba(0,0,0,0)';
  evaluate(context, 'resizeHistory(5)');
  assert.deepEqual(band(), [null, null, 1, 1, 1], 'resizing updates the band before another poll');
  assert.deepEqual(colors(), [clear, clear, 'hatch', 'hatch', 'hatch']);
  assert.deepEqual(tick(false), [null, 1, 1, 1, 1], 'new padding is unmeasured, not an outage');
  evaluate(context, 'fillHistoryGaps(2)');
  assert.deepEqual(band(), [1, 1, 1, null, null], 'reconnect gaps update the band before a successful ping');
  assert.deepEqual(colors(), ['hatch', 'hatch', 'hatch', clear, clear]);
  context.ping = { ok: false };
  context.tickPing();
  assert.deepEqual(band(), [1, 1, 1, null, null], 'failed HTTP refreshes cannot restore the old band positions');
  assert.deepEqual(tick(false), [1, 1, null, null, 1], 'pause slots remain gaps while measured failures stay outages');
  evaluate(context, 'resizeHistory(3)');
  assert.deepEqual(band(), [null, null, 1], 'shrinking aligns shading immediately while paused');
  assert.deepEqual(colors(), [clear, clear, 'hatch']);
  assert.deepEqual(tick(true), [null, 1, null], 'shrinking retains alignment and recovery clears the newest outage');
  assert.deepEqual(context.errors, []);
}

(async () => {
  for (const test of [testFetchBodyTimeout, testGpuRecovery, testGpuUnavailableReadings,
    testDriveTemperatureFreshness, testSensorFreshness, testPausedStatus, testStartupPolling, testPingLoss]) {
    await test();
    console.log(`PASS ${test.name}`);
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
