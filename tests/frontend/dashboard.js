// Focused browser behavior checks using the actual application functions.
// Run with: node tests/frontend/dashboard.js (Node with built-in fetch; no dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const vm = require('node:vm');

const read = name => fs.readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8');
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
    ...dom, statuses, errors, historyLength: 3, pollInterval: 2000, dataCache: {},
    thresholds: { gpu_util_percent: 80, gpu_memory_percent: 80, temp_celsius: 60, ping_ms: 100 },
    GPU_COLORS: ['#123456'], getCssColor: () => '#123456',
    updateCardNoData: (id, ok) => { statuses[id] = ok; }, updateCardWarning() {},
    console: { error: (...args) => errors.push(args.map(String).join(' ')) },
    performance: { now: () => 0 }, requestAnimationFrame: callback => callback(500),
  });
  evaluate(context,
    between(charts, '// Data stores', '// Generate time labels') +
    between(config, 'function animateValue', '// GPU colors') +
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

async function testStartupReadTimeouts() {
  const server = http.createServer((request, response) => {
    const phase = request.url.split('/')[1];
    const url = request.url.slice(phase.length + 1);
    response.writeHead(200, { 'Content-Type': 'application/json' });
    if ((phase === 'config' && url === '/config.json') ||
        (phase === 'discovery' && url === '/api/list-endpoints')) {
      response.write('{"unfinished":');
    } else response.end(JSON.stringify(url === '/config.json'
      ? { title: 'Configured', limits: { network_mbps: 100 } } : { endpoints: ['/4/cpu'] }));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const timers = new Set();
  try {
    for (const phase of ['config', 'discovery', 'healthy']) {
      const calls = { fast: 0, slow: 0, intervals: 0 };
      const context = vm.createContext({
        availableEndpoints: null, dashboardConfig: {}, thresholds: {}, limits: { network_mbps: 1250 },
        isConnected: true, pollingInterval: null, slowPollingInterval: null,
        pollInterval: 2000, SLOW_POLL_INTERVAL: 30000, AbortController, console: { warn() {} },
        fetch: (url, options) => fetch(`http://127.0.0.1:${server.address().port}/${phase}${url}`, options),
        setTimeout(callback, delay) {
          assert.equal(delay, 5000, 'startup reads use the existing safeFetch deadline');
          const timer = setTimeout(() => { timers.delete(timer); callback(); }, 150);
          timers.add(timer);
          return timer;
        },
        clearTimeout(timer) { timers.delete(timer); clearTimeout(timer); },
        setInterval() { return ++calls.intervals; },
        applyConfigToDom() {}, hideUnavailableCards() {}, initProcessTableListeners() {},
        fetchData() { calls.fast++; }, fetchSlowData() { calls.slow++; },
      });
      evaluate(context,
        between(config, 'async function loadConfig', '// Update card warning') +
        between(config, 'async function fetchEndpointList', 'function hideUnavailableCards') +
        between(config, 'async function safeFetch', '// Data cache for reducing API calls') +
        between(app, 'function startPolling', '// Toggle connection'));
      let watchdog;
      try {
        await Promise.race([
          evaluate(context, between(app, 'loadConfig().then', '// Set up toggle button')),
          new Promise((resolve, reject) => {
            watchdog = setTimeout(() => reject(new Error(`${phase} read blocked dashboard startup`)), 1500);
          }),
        ]);
      } finally { clearTimeout(watchdog); }
      assert.deepEqual(calls, { fast: 1, slow: 1, intervals: 2 }, 'startup reaches both polling loops');
      assert.equal(context.dashboardConfig.title, phase === 'config' ? undefined : 'Configured');
      assert.equal(context.limits.network_mbps, phase === 'config' ? 1250 : 100);
      assert.deepEqual(context.availableEndpoints === null ? null : [...context.availableEndpoints],
        phase === 'discovery' ? null : ['/4/cpu']);
      assert.equal(timers.size, 0, 'startup reads clear every deadline');
    }
  } finally {
    timers.forEach(clearTimeout);
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}

async function testIpInfoTimeout() {
  const info = { lan: [{ interface: 'eth0', ipv4: '192.168.1.20' }],
    public: { error: 'lookup failed' }, tailscale: {} };
  const responses = new Set(), timers = new Set();
  let ipRequests = 0, completedIPs = 0;
  const server = http.createServer((request, response) => {
    if (request.url === '/api/ip-info') {
      ipRequests++;
      // Two public-IP lookups can each spend 3 s timing out before LAN data returns.
      const timer = setTimeout(() => {
        responses.delete(timer);
        completedIPs++;
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify(info));
      }, 6000);
      responses.add(timer);
    } else if (request.url !== '/stall') response.end('');
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const makeContext = () => {
    const dom = makeDOM(['ip-info-content']), statuses = {};
    const context = vm.createContext({
      ...dom, statuses, availableEndpoints: null, API_BASE: '/api/4', AbortController,
      fetch: (url, options) => fetch(base + url, options),
      setTimeout(callback, delay) {
        const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay);
        timers.add(timer);
        return timer;
      },
      clearTimeout(timer) { timers.delete(timer); clearTimeout(timer); },
      updateCardNoData: (id, ok) => { statuses[id] = ok; }, updateCacheDisplay() {}, escapeHtml: String,
      renderGpuInfo() {}, renderDrives() {}, renderMail() {}, renderHardware() {},
      renderDriveHealth() {}, renderSystemInfo() {},
      diskCapture: { handleResponse() {} }, networkCapture: { handleResponse() {} },
    });
    evaluate(context,
      between(config, 'const IP_INFO_TIMEOUT_MS', 'const SLOW_POLL_INTERVAL') +
      between(config, 'async function safeFetch', '// The CPU core temperatures') +
      between(app, 'function renderIpInfo', '// System Mail card') +
      between(app, 'async function fetchSlowData', '// One poll at a time') +
      between(app, 'async function refreshData', '// Set up refresh button handlers') +
      "Object.keys(dataCache).filter(key => key !== 'ipInfo').forEach(key => updateCache(key, []));");
    return context;
  };
  const scheduled = makeContext(), manual = makeContext();
  try {
    const [, , ordinary] = await Promise.all([
      scheduled.fetchSlowData(),
      manual.refreshData('ipInfo', { classList: { add() {}, remove() {} } }),
      scheduled.safeFetch('/stall', {}).then(result => {
        assert.equal(completedIPs, 0, 'ordinary requests time out before the slower IP response');
        return result;
      }),
    ]);
    assert.equal(ordinary.errorType, 'timeout', 'ordinary requests still use the shorter deadline');
    assert.equal(ipRequests, 2, 'both scheduled and manual refresh requested IP information');
    for (const context of [scheduled, manual]) {
      assert.deepEqual(plain(context.getCache('ipInfo')), info);
      const html = context.elements.get('ip-info-content').innerHTML;
      assert(html.includes('192.168.1.20') && html.includes('lookup failed'), 'LAN data survives public lookup timeouts');
      assert.equal(context.statuses['ip-info'], true);
    }
    assert.equal(timers.size, 0, 'completed and aborted requests clear their deadlines');
  } finally {
    timers.forEach(clearTimeout);
    responses.forEach(clearTimeout);
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

function testGpuInfoUnavailableReadings() {
  const context = liveContext(['gpu-info-card', 'gpu-info-content', 'gpu-value', 'gpu-mem-value']);
  context.charts = { gpu: makeChart(), gpuMem: makeChart() };
  context.updateGpuLegend = () => {};
  context.updateCacheDisplay = () => {};
  context.escapeHtml = String;
  context.abbreviateGpuName = String;
  evaluate(context,
    between(app, 'function renderGpuInfo', 'function renderCpuInfo') +
    'function tickGpu() {' + between(app, "  guard('gpu',", "  guard('network',") + '}');
  const info = { index: 0, name: 'NVIDIA A100', driver_version: 'test',
    utilization: null, temperature: 55, memory_used: 12288, memory_total: 24576 };
  context.dataCache.gpuInfo = { data: [info] };
  context.renderGpuInfo([info]);
  const html = () => context.elements.get('gpu-info-content').innerHTML;
  assert(html().includes('GPU 0: NVIDIA A100'));
  assert(html().includes('Util: --%'));
  assert(html().includes('Temp: 55°C'));
  assert(html().includes('Mem: 12.0 / 24.0 GB'));
  context.gpu = { ok: true, data: [{ proc: null, mem: 50 }] };
  context.tickGpu();
  assert.equal(context.elements.get('gpu-mem-used-0').textContent, '12.0');
  assert.equal(evaluate(context, 'history.gpuMems[0].at(-1)'), 12);
  assert.equal(context.statuses['gpu-mem'], true, 'unavailable utilization does not hide valid memory');
  for (const unavailable of [null, undefined, NaN, Infinity]) {
    context.renderGpuInfo([{ ...info, utilization: unavailable, temperature: unavailable,
      memory_used: unavailable, memory_total: unavailable }]);
    assert(html().includes('Util: --%'));
    assert(html().includes('Temp: --°C'));
    assert(html().includes('Mem: -- / -- GB'));
  }
  context.renderGpuInfo([{ ...info, utilization: 0, temperature: 0, memory_used: 0 }]);
  assert(html().includes('Util: 0%'));
  assert(html().includes('Temp: 0°C'));
  assert(html().includes('Mem: 0.0 / 24.0 GB'), 'genuine zeros remain visible');
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
    const timers = new Map(), calls = { fast: 0, slow: 0, setup: 0 };
    let nextTimer = 1;
    const context = vm.createContext({
      ...dom, isConnected: true, pollingInterval: null, slowPollingInterval: null, disconnectTime: null,
      pollInterval: 2000, SLOW_POLL_INTERVAL: 30000, historyLength: 3,
      COLORS: { success: 'green', errorLight: 'red', partial: 'orange' }, cardStatus: { cpu: { ok: true } },
      dataCache: Object.fromEntries(['drives', 'processes', 'networkProcesses', 'diskProcesses', 'cpuInfo', 'systemInfo', 'gpuInfo']
        .map(key => [key, { data: [], lastUpdate: 1 }])),
      charts: Object.fromEntries(['cpu', 'cpuCoresSpread', 'mem', 'swap', 'net', 'disk', 'gpu', 'gpuMem', 'ping', 'diskDrives']
        .map(key => [key, makeChart()])),
      loadConfig: async () => {}, applyConfigToDom() {}, fetchEndpointList: () => discovery,
      hideUnavailableCards() { context.updateConnectionStatus(); },
      initProcessTableListeners() { calls.setup++; }, fillHistoryGaps() {},
      fetchSlowData() { calls.slow++; },
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
      assert.deepEqual([calls.fast, calls.slow], [0, 0], 'startup does not fetch while paused');
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
    for (const key of ['cpuInfo', 'systemInfo', 'gpuInfo']) {
      assert.equal(context.dataCache[key].lastUpdate, null, 'reconnect refreshes metadata');
      assert.deepEqual(context.dataCache[key].data, [], 'reconnect retains the last metadata until replacement');
    }
    context.toggleConnection();
    assert.equal(timers.size, 0);
  }
}

function testPingDisplay() {
  const context = liveContext(['ping-value', 'ping-legend']);
  const tooltip = { style: {} };
  context.dashboardConfig = { ping_targets: [{ address: 'example.test' }] };
  context.DEFAULT_PING_TARGETS = [];
  context.charts = { ping: makeChart() };
  context.charts.ping.options.plugins = {};
  context.charts.ping.canvas = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
  context.pingBandPattern = () => 'hatch';
  context.escapeHtml = String;
  context.window = { scrollX: 0, scrollY: 0 };
  context._ensurePingTooltip = () => tooltip;
  evaluate(context,
    between(config, 'function pingWinUp', '// Canvas hatch') +
    between(config, 'function pingRowsHTML', 'function _ensurePingTooltip') +
    between(config, 'function pingTooltipCfg', '// History feature constants') +
    between(app, 'function fillHistoryGaps', '// Render functions for cached cards') +
    between(app, 'function applyConfigToDom', '// Load config, discover endpoints') +
    'function tickPing() {' + between(app, "  guard('ping',", "  guard('processes',") + '}');
  context.applyConfigToDom();
  const hover = i => {
    context.charts.ping.options.plugins.tooltip.external({
      chart: context.charts.ping,
      tooltip: { opacity: 1, dataPoints: [{ dataIndex: i }], caretX: 0, caretY: 0 },
    });
    return tooltip.innerHTML;
  };
  const expectTooltip = (i, value, uptime) => {
    const html = hover(i);
    assert(html.includes(`>${value}</span>`), `slot ${i} should show ${value}: ${html}`);
    assert(html.includes(`example.test ${uptime} up</div>`), `uptime should be ${uptime}: ${html}`);
  };
  const tick = ms => {
    context.ping = { ok: true, data: { targets: {
      'example.test': ms == null ? { error: 'timeout' } : { latency_ms: ms },
    } } };
    context.tickPing();
  };
  let now = 1950, frames = [];
  context.performance.now = () => now;
  context.requestAnimationFrame = callback => frames.push(callback);
  const frame = at => {
    now = at;
    const pending = frames;
    frames = [];
    pending.forEach(callback => callback(now));
  };
  const label = () => context.elements.get('ping-value-0').textContent;

  expectTooltip(0, '—', '--');  // No measurement yet, so uptime is unknown.
  tick(20);
  expectTooltip(1, '—', '100%');  // Initial padding is not a failed ping.
  expectTooltip(2, '20ms', '100%');
  frame(1966);
  now = 2020;  // A quick failure follows a slow poll before its animation ends.
  tick(null);
  assert.equal(label(), '--');
  expectTooltip(2, 'down', '50%');
  frame(2350);
  assert.equal(label(), '--', 'old frames cannot restore latency after a measured failure');
  assert.equal(evaluate(context, 'history.pings[0].at(-1)'), null);
  tick(0);
  assert.equal(label(), '0.0', 'zero latency is a successful reading');
  expectTooltip(2, '0ms', '67%');

  evaluate(context, 'resizeHistory(5)');
  expectTooltip(0, '—', '67%');  // New padding does not change the denominator.
  expectTooltip(3, 'down', '67%');
  evaluate(context, 'fillHistoryGaps(2)');
  expectTooltip(3, '—', '67%');  // Pause/reconnect gaps are also unmeasured.
  expectTooltip(1, 'down', '67%');
  evaluate(context, 'resizeHistory(3)');
  expectTooltip(0, '0ms', '100%');
  expectTooltip(1, '—', '100%');
  evaluate(context, 'fillHistoryGaps(3)');
  expectTooltip(2, '—', '--');  // Every measured sample has now aged out.
  tick(null);
  expectTooltip(2, 'down', '0%');
  tick(0);
  expectTooltip(2, '0ms', '50%');
  assert.deepEqual(context.errors, []);
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

async function testStaticInfoRecovery() {
  const dom = makeDOM(['cpu-info-content', 'system-info-content']);
  const calls = {}, statuses = {}, rendered = { cpu: [], system: [] };
  let now = 1000;
  let pendingIp = null;
  const context = vm.createContext({
    ...dom, Date: { now: () => now }, API_BASE: '/api/4', IP_INFO_TIMEOUT_MS: 20000,
    async safeFetch(url) {
      calls[url] = (calls[url] || 0) + 1;
      if (url === '/api/ip-info' && pendingIp) return pendingIp;
      if (url === '/api/cpu-info') return { ok: calls[url] > 1, data: { model: 'CPU' } };
      if (url === '/api/4/system') return { ok: calls[url] > 2, data: { hostname: 'server' } };
      return { ok: true, data: `${now}s` };
    },
    renderCpuInfo(data) { rendered.cpu.push(data); statuses['cpu-info'] = true; },
    renderSystemInfo(data, uptime) { rendered.system.push({ data, uptime }); statuses['system-info'] = true; },
    updateCardNoData(id, ok) { statuses[id] = ok; },
    renderGpuInfo() {}, renderDrives() {}, renderIpInfo() {}, renderMail() {}, renderHardware() {}, renderDriveHealth() {},
    diskCapture: { handleResponse() {} }, networkCapture: { handleResponse() {} },
  });
  evaluate(context,
    between(config, 'const dataCache =', '// The CPU core temperatures') +
    between(app, 'async function fetchSlowData', '// One poll at a time') +
    between(app, 'async function refreshData', '// Set up refresh button handlers') +
    "Object.keys(dataCache).filter(key => !['cpuInfo', 'systemInfo'].includes(key)).forEach(key => { dataCache[key].ttl = Infinity; updateCache(key, []); });");
  await context.fetchSlowData();
  assert.deepEqual(statuses, { 'cpu-info': false, 'system-info': false });
  assert.equal(context.getCache('cpuInfo'), null);
  assert.equal(context.getCache('systemInfo'), null);

  now += 30000;
  let finishIp, finished = false;
  pendingIp = new Promise(resolve => { finishIp = resolve; });
  evaluate(context, 'dataCache.ipInfo.lastUpdate = null;');
  const pendingPoll = context.fetchSlowData().then(() => { finished = true; });
  await new Promise(setImmediate);
  assert.equal(finished, false, 'IP lookup remains pending');
  assert.equal(rendered.cpu.length, 1, 'metadata renders before the pending IP lookup finishes');
  assert.equal(calls['/api/4/uptime'], 2, 'one uptime request per slow poll');
  finishIp({ ok: true, data: [] });
  await pendingPoll;
  pendingIp = null;
  assert.deepEqual(statuses, { 'cpu-info': true, 'system-info': false }, 'each static endpoint recovers independently');
  now += 30000;
  await context.fetchSlowData();
  assert.deepEqual(statuses, { 'cpu-info': true, 'system-info': true });
  assert.equal(calls['/api/cpu-info'], 2);
  assert.equal(calls['/api/4/system'], 3);
  assert.equal(rendered.cpu.length, 1);
  assert.equal(rendered.system.length, 1);

  now += 86400000;
  await context.fetchSlowData();
  assert.equal(calls['/api/cpu-info'], 2, 'successful CPU metadata stays cached');
  assert.equal(calls['/api/4/system'], 3, 'successful system metadata stays cached');
  assert.equal(rendered.system.at(-1).uptime, `${now}s`, 'uptime still refreshes beside cached metadata');

  const button = { classList: { add() {}, remove() {} } };
  await context.refreshData('cpuInfo', button);
  await context.refreshData('systemInfo', button);
  assert.equal(calls['/api/cpu-info'], 3, 'manual CPU refresh bypasses the permanent cache');
  assert.equal(calls['/api/4/system'], 4, 'manual system refresh bypasses the permanent cache');
}

async function testCaptureCacheAge() {
  let now = 103000, captureError = false;
  const calls = {};
  const context = vm.createContext({
    Date: { now: () => now }, console: { error() {}, warn() {} },
    document: { getElementById: () => null, querySelector: () => null },
    setInterval: () => 1, clearInterval() {},
    nethogsDuration: 3, diskDuration: 1, API_BASE: '/api/4',
    getDiskTimeout: () => 6500, getNethogsTimeout: () => 9500,
    renderUnifiedProcesses() {}, renderGpuInfo() {}, renderCpuInfo() {}, renderDrives() {}, renderIpInfo() {},
    renderMail() {}, renderHardware() {}, renderDriveHealth() {}, renderSystemInfo() {},
    async safeFetch(url) {
      calls[url] = (calls[url] || 0) + 1;
      const data = url.includes('processes')
        ? captureError ? { error: 'capture failed' } : [{ pid: 100, name: 'worker' }]
        : url.endsWith('/fs') ? [] : url.endsWith('/uptime') ? '1h' : {};
      return { ok: true, data };
    },
  });
  evaluate(context,
    between(config, 'const IP_INFO_TIMEOUT_MS', 'const SLOW_POLL_INTERVAL') +
    between(config, 'const dataCache =', '// The CPU core temperatures') +
    between(config, 'function formatTimeAgo', 'function updateCacheDisplay') +
    between(app, 'function createCaptureState', '// Unified processes table state') +
    between(app, 'async function fetchSlowData', '// One poll at a time'));
  await context.fetchSlowData();  // Initial captures complete three seconds after startup.
  const timestamp = name => evaluate(context, `${name}Capture.getTimestamp()`);
  const capturedAt = name => evaluate(context, `${name}Capture.lastCaptureTime`);
  now = 130000;  // The regular slow poll reuses a still-valid 27-second-old result.
  assert.equal(timestamp('network'), '27s ago');
  await context.fetchSlowData();
  for (const name of ['network', 'disk']) {
    assert.equal(timestamp(name), '27s ago', 'cache reuse preserves the capture age');
    assert.equal(capturedAt(name), 103000);
    const url = `/api/${name}-processes?duration=${name === 'network' ? 3 : 1}`;
    assert.equal(calls[url], 1, 'the cached poll did not perform another capture');
  }
  now = 163000;
  await context.fetchSlowData();
  assert.equal(timestamp('network'), '0s ago', 'a fresh capture advances the timestamp');
  assert.equal(capturedAt('disk'), now);
  captureError = true;
  now = 200000;
  await context.fetchSlowData();
  for (const name of ['network', 'disk']) {
    assert.equal(capturedAt(name), 163000, 'errors do not replace the last successful capture');
    assert.equal(evaluate(context, `${name}Capture.error`), true);
    assert.equal(evaluate(context, `${name}Capture.capturing`), false);
  }
}

// Popup-local elements matter here: a closed popup's pending request must not
// resolve global IDs against a newly opened form. Inputs derive from the real HTML.
function historySettingsContext() {
  const popups = [], reads = [], writes = [], timers = [];
  class Element {
    constructor(tag) { this.tag = tag; this.style = {}; this.events = {}; this.value = ''; this.checked = false; this.disabled = false; }
    set value(value) { this._value = String(value); }
    get value() { return this._value; }
    set innerHTML(html) {
      this.children = new Map();
      for (const match of html.matchAll(/<(input|button|div)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
        const el = new Element(match[1]);
        el.checked = /\bchecked\b/.test(match[2]);
        el.disabled = /\bdisabled\b/.test(match[2]);
        this.children.set(match[3], el);
      }
    }
    querySelector(selector) { return this.children.get(selector.slice(1)); }
    querySelectorAll() { return [...this.children.values()].filter(el => ['input', 'button'].includes(el.tag)); }
    addEventListener(type, callback) { this.events[type] = callback; }
    click() { if (!this.disabled) return this.events.click?.(); }
    remove() { this.removed = true; }
    getBoundingClientRect() { return { left: 0, bottom: 10 }; }
  }
  const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
  const context = vm.createContext({
    document: {
      createElement: tag => new Element(tag), body: { appendChild: popup => popups.push(popup) },
      querySelector: () => new Element('label'),
      getElementById: id => popups.findLast(popup => !popup.removed)?.children.get(id),
      addEventListener() {}, removeEventListener() {},
    },
    safeFetch() { const pending = defer(); reads.push(pending); return pending.promise; },
    fetch(url, options) { const pending = defer(); writes.push({ ...pending, body: JSON.parse(options.body) }); return pending.promise; },
    setTimeout(callback, delay) { timers.push({ callback, delay }); return timers.length; },
  });
  evaluate(context, between(app, 'let historySettingsPopup =', '// Middle-click on History label'));
  const current = () => evaluate(context, 'historySettingsPopup');
  const open = () => { context.createHistorySettingsPopup(); return current(); };
  const finishRead = async (index, data, ok = true) => {
    reads[index].resolve({ ok, data });
    await new Promise(setImmediate);
  };
  const finishWrite = (index, data, ok = true) => writes[index].resolve({ ok, json: async () => data });
  return { context, reads, writes, timers, current, open, finishRead, finishWrite };
}

const historySettings = {
  record_interval: 600, sample_duration: 10, min_record_interval: 60,
  ping_enabled: false, processes_enabled: true, process_count: 10, password_configured: true,
  defaults: { record_interval: 300, sample_duration: 5, min_record_interval: 30,
    ping_enabled: true, processes_enabled: false, process_count: 8 },
};

async function testHistorySettingsLoading() {
  const h = historySettingsContext();
  let popup = h.open();
  const field = id => popup.querySelector('#hs-' + id);
  assert(field('confirm').disabled && field('interval').disabled);
  field('password').value = 'test';
  await h.context.submitHistorySettings();  // Also guard direct invocation before GET completes.
  assert.equal(h.writes.length, 0, 'unloaded checkbox defaults cannot be submitted');
  await h.finishRead(0, {}, false);
  assert(field('status').textContent.includes('Could not load settings'));
  assert(field('confirm').disabled);
  await h.context.submitHistorySettings();
  assert.equal(h.writes.length, 0, 'a failed read cannot silently enable Confirm');
  h.context.closeHistorySettingsPopup();
  popup = h.open();
  await h.finishRead(1, historySettings);
  assert(!field('confirm').disabled && !field('interval').disabled);
  field('interval').value = '60';
  field('password').value = 'test';
  const pending = field('confirm').click();
  assert.deepEqual(h.writes[0].body, { password: 'test', record_interval: 60, sample_duration: 10,
    min_record_interval: 60, ping_enabled: false, processes_enabled: true, process_count: 10 });
  assert(field('confirm').disabled, 'a pending save cannot be submitted twice');
  await h.context.submitHistorySettings();
  assert.equal(h.writes.length, 1);
  h.finishWrite(0, { error: 'Incorrect password' }, false);
  await pending;
  assert(!field('confirm').disabled, 'a failed save permits correction and retry');
  assert.equal(field('interval').value, '60');
  assert.equal(field('status').textContent, 'Incorrect password');
  field('reset').click();
  assert.equal(h.reads.length, 2, 'Reset uses the defaults included in the initial read');
  assert.equal(field('interval').value, '300');
  assert.equal(field('processes-enabled').checked, false);
}

async function testHistorySettingsPopupOwnership() {
  const h = historySettingsContext();
  const first = h.open();
  h.context.closeHistorySettingsPopup();
  const second = h.open();
  await h.finishRead(1, historySettings);
  second.querySelector('#hs-interval').value = '120';
  await h.finishRead(0, { ...historySettings, record_interval: 86400 });
  assert.equal(second.querySelector('#hs-interval').value, '120', 'old GET cannot overwrite a reopened form');
  second.querySelector('#hs-password').value = 'test';
  const oldSave = second.querySelector('#hs-confirm').click();
  h.context.closeHistorySettingsPopup();
  const third = h.open();
  await h.finishRead(2, historySettings);
  third.querySelector('#hs-password').value = 'keep this';
  h.finishWrite(0, { ...historySettings, record_interval: 120 });
  await oldSave;
  assert.equal(h.current(), third);
  assert.equal(third.querySelector('#hs-password').value, 'keep this', 'old POST cannot clear a new password');
  assert.equal(third.querySelector('#hs-interval').value, '600');
  assert.equal(h.timers.filter(t => t.delay === 1200).length, 0, 'old POST does not schedule a close');
  const saved = third.querySelector('#hs-confirm').click();
  h.finishWrite(1, historySettings);
  await saved;
  assert.equal(third.querySelector('#hs-password').value, '');
  assert(third.querySelector('#hs-confirm').disabled, 'successful form stays disabled until it closes');
  h.context.closeHistorySettingsPopup();
  const fourth = h.open();
  h.timers.filter(t => t.delay === 1200).forEach(t => t.callback());
  assert.equal(h.current(), fourth, 'an old success timer cannot close the new popup');
  await h.finishRead(3, historySettings);
  assert(!fourth.querySelector('#hs-confirm').disabled);
  assert(first.removed && second.removed && third.removed);
}

function testLiveSampleTimes() {
  const dom = makeDOM(['poll-select']);
  let click, picker, now = 100000;
  dom.elements.get('poll-select').addEventListener = (_, callback) => { click = callback; };
  const context = vm.createContext({
    ...dom, historyLength: 3, pollInterval: 2000, MIN_POLL_INTERVAL: 1000,
    isConnected: false, pollingInterval: null, Date: { now: () => now },
    getCssColor: () => '#123456', localStorage: { setItem() {} },
    createDropdownPicker: (_, options) => { picker = options; },
  });
  evaluate(context, between(charts, '// Data stores', '// Initialize charts'));
  evaluate(context, `
    function makeLiveChart(data) {
      const config = buildChart({ data: { labels: [], datasets: [{ data }] } });
      return { ...config, update() { config.plugins.forEach(plugin => plugin.beforeUpdate(this)); } };
    }
    const charts = {
      cpu: makeLiveChart(history.cpu), mem: makeLiveChart(history.mem),
      cpuCoresSpread: makeLiveChart(history.cpuCoresMin), swap: makeLiveChart(history.swap),
      net: makeLiveChart(history.netIn), disk: makeLiveChart(history.diskRead),
      gpu: makeLiveChart([]), gpuMem: makeLiveChart([]), diskDrives: makeLiveChart([]),
      ping: makeLiveChart([]),
    };
  `);
  evaluate(context,
    between(app, 'function fillHistoryGaps', '// Render functions for cached cards') +
    between(app, 'const pollSelectBtn =', '// Set up process count selector'));
  const labels = name => plain(evaluate(context, `charts.${name}.data.labels`));
  const values = name => plain(evaluate(context, `history.${name}`));
  for (let i = 0; i < 3; i++) {
    now = 100000 + i * 2000;
    evaluate(context, `pushHistory(history.cpu, ${10 + i}); pushHistory(history.mem, ${20 + i});`);
  }
  click();
  picker.onSelect(30);
  assert.deepEqual(labels('cpu'), [4, 2, 0], 'changing cadence preserves old sample ages');
  assert.deepEqual(values('cpu'), [10, 11, 12], 'changing cadence preserves every value');
  now = 134000;
  evaluate(context, 'pushHistory(history.cpu, 40); charts.cpu.update(); charts.mem.update();');
  assert.deepEqual(labels('cpu'), [32, 30, 0], 'new and old cadences coexist');
  assert.deepEqual(labels('mem'), [34, 32, 30], 'an endpoint that missed a poll keeps its own sample times');
  assert.deepEqual(values('cpu'), [11, 12, 40]);

  evaluate(context, 'resizeHistory(5); charts.cpu.update();');
  assert.deepEqual(labels('cpu'), ['', '', 32, 30, 0], 'resizing pads times without redating samples');
  picker.onSelect(1);
  now = 135000;
  evaluate(context, 'pushHistory(history.cpu, 50); charts.cpu.update();');
  assert.deepEqual(labels('cpu'), ['', 33, 31, 1, 0]);
  evaluate(context, 'resizeHistory(3); charts.cpu.update();');
  assert.deepEqual(labels('cpu'), [31, 1, 0]);
  assert.deepEqual(values('cpu'), [12, 40, 50]);

  // A request started before Pause can still complete during it. Reconnect
  // gaps are unmeasured and must not place synthetic dates before that result.
  now = 138000;
  evaluate(context, 'pushHistory(history.cpu, 60);');
  now = 140000;
  evaluate(context, 'fillHistoryGaps(2); charts.cpu.update();');
  assert.deepEqual(labels('cpu'), [2, '', '']);
  assert.deepEqual(values('cpu'), [60, null, null]);
  evaluate(context, 'pushHistory(history.cpu, 70); charts.cpu.update();');
  assert.deepEqual(labels('cpu'), ['', '', 0]);

  // Dynamically created series use the same clock; the ping shading is a
  // derived dataset and must not hide the actual measured (even failed) ping.
  evaluate(context, `
    history.pings = [Array(historyLength).fill(null)];
    charts.ping.data.datasets = [{ label: 'loss', data: [1, 1, 1] }, { data: history.pings[0] }];
    pushHistory(history.pings[0], null); charts.ping.update();
  `);
  assert.deepEqual(labels('ping'), ['', '', 0], 'a measured ping failure still has a sample time');
  evaluate(context, `
    const bars = buildChart({ type: 'bar', data: { labels: ['Core 0'], datasets: [] } });
    bars.plugins.forEach(plugin => plugin.beforeUpdate(bars));
  `);
  assert.deepEqual(plain(evaluate(context, 'bars.data.labels')), ['Core 0'], 'categorical charts retain their labels');
}

(async () => {
  for (const test of [testFetchBodyTimeout, testStartupReadTimeouts, testIpInfoTimeout, testGpuRecovery, testGpuUnavailableReadings, testGpuInfoUnavailableReadings,
    testDriveTemperatureFreshness, testSensorFreshness, testPausedStatus, testStartupPolling, testPingDisplay, testPingLoss,
    testStaticInfoRecovery, testCaptureCacheAge, testHistorySettingsLoading, testHistorySettingsPopupOwnership, testLiveSampleTimes]) {
    await test();
    console.log(`PASS ${test.name}`);
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
