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
const makeChart = () => ({ data: { datasets: [{ data: [] }, { data: [] }] }, options: { scales: { y: {}, y1: {} } }, update() {} });

function liveContext(ids) {
  const dom = makeDOM(ids), statuses = {}, errors = [];
  const context = vm.createContext({
    ...dom, statuses, errors, historyLength: 3, pollInterval: 2000, dataCache: {},
    thresholds: { gpu_util_percent: 80, gpu_memory_percent: 80, temp_celsius: 60, ping_ms: 100 },
    GPU_COLORS: ['#123456'], getCssColor: () => '#123456', withAlpha: (c, a) => c + a,
    updateCardNoData: (id, ok) => { statuses[id] = ok; }, updateCardWarning() {},
    console: { error: (...args) => errors.push(args.map(String).join(' ')) },
    performance: { now: () => 0 }, requestAnimationFrame: callback => callback(500),
    dashboardConfig: {},
  });
  evaluate(context,
    between(charts, '// Data stores', '// Generate time labels') +
    between(charts, '// config.json temp_labels', 'function abbreviateGpuName') +
    between(config, 'function animateValue', '// GPU colors') +
    between(config, 'function getCache', '// The CPU core temperatures') +
    between(app, 'function fansHeader', '// Power bar segment colours') +
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
      renderGpuInfo() {}, renderDrives() {}, renderMail() {}, renderHardware() {}, servicesConfigured() { return false; },
      renderDriveHealth() {}, renderSystemInfo() {},
      diskCapture: { handleResponse() {} }, networkCapture: { handleResponse() {} },
    });
    evaluate(context,
      between(config, 'const IP_INFO_TIMEOUT_MS', 'const SLOW_POLL_INTERVAL') +
      between(config, 'async function safeFetch', '// The CPU core temperatures') +
      between(app, 'function renderIpInfo', '// System Mail card') +
      between(app, 'async function fetchSlowData', '// One poll at a time') +
      between(app, 'function guard', 'async function fetchDataOnce') +
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
  const context = liveContext(['temps-value', 'temps-card', 'fans-card', 'power-card']);
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

// A box with no temperature source (an OpenWrt router without thermal zones)
// hides the card like fans/power; failed or stale sources keep it red.
// config.json temp_labels renames a sensor on the card; temp_thresholds gives
// it its own orange line, under either name.
function testTempLabelsAndLimits() {
  const context = liveContext(['temps-value', 'temps-card', 'fans-card', 'power-card']);
  context.charts = { temps: makeChart(), fans: makeChart() };
  context.sensorsAgeText = () => '';
  context.getTempColor = (value, warning, critical) => `${value}/${warning}/${critical}`;
  evaluate(context,
    between(config, 'const CORE_TEMP_LABEL', 'function formatTimeAgo') +
    'function tickTemps() {' + between(app, "  guard('temps',", "  guard('load',") + '}');
  context.gpu = { ok: false };
  context.sensors = { ok: true, data: [
    { type: 'temperature_core', label: 'loc1', value: 52, warning: 120, critical: 110 },
    { type: 'temperature_core', label: 'board', value: 70, warning: 65, critical: 80 },
    { type: 'temperature_core', label: 'hot', value: 99, warning: 65, critical: 80 },
  ] };
  evaluate(context, `dashboardConfig = { temp_labels: { loc1: 'I350 NIC' },
    temp_thresholds: { 'I350 NIC': 95, loc1: 'junk', hot: 100, board: 'junk' } };`);
  context.tickTemps();
  assert.deepEqual(context.errors, []);
  assert.deepEqual(plain(context.charts.temps.data.labels), ['I350 NIC', 'board', 'hot']);
  assert.deepEqual(plain(context.charts.temps.data.datasets[0].backgroundColor),
    ['52/95/110', '70/65/80', '99/100/110'],
    'own limit by display name or sensor name; critical stays above it; junk is ignored');
  const fan = '@pci0000:00/0000:01:00.0/amdgpu/fan1';
  assert.equal(context.tempDisplayLabel(fan), 'amdgpu fan1 (0000:01:00.0)');
  assert.equal(context.tempDisplayLabel(fan.replace('01:00.0', '02:00.0')), 'amdgpu fan1 (0000:02:00.0)');
  context.dashboardConfig.sensor_labels = { [fan]: 'Front GPU fan' };
  assert.equal(context.tempDisplayLabel(fan), 'Front GPU fan');
  context.dashboardConfig.sensor_labels['amdgpu fan1'] = 'Original fan alias';
  assert.equal(context.tempDisplayLabel(fan, 'amdgpu fan1'), 'Front GPU fan', 'an id alias takes precedence');
  delete context.dashboardConfig.sensor_labels[fan];
  assert.equal(context.tempDisplayLabel(fan, 'amdgpu fan1'), 'Original fan alias', 'display aliases do not require migration to ids');
  assert.equal(context.tempDisplayLabel(fan, 'Processor Fan'), 'Processor Fan', 'an available kernel label wins over an id-built name');
  const otherFan = fan.replace('01:00.0', '02:00.0');
  const matchingFans = plain(context.sensorDisplayLabels([fan, otherFan], ['Processor Fan', 'Processor Fan']));
  assert.notEqual(matchingFans[0], matchingFans[1], 'same-label fans remain distinguishable');
  assert.deepEqual(matchingFans, ['Processor Fan [0000:01:00.0/amdgpu/fan1]', 'Processor Fan [0000:02:00.0/amdgpu/fan1]']);
  assert.equal(context.tempDisplayLabel('legacy fan'), 'legacy fan');
  const composite = 'Composite [pci0000:00/0000:01:00.0/temp1]';
  assert.equal(context.tempDisplayLabel(composite), 'Composite', 'an unambiguous history label hides its physical path');
  assert.equal(context.abbreviateSensorLabel(composite), 'NVMe');
  assert.equal(context.abbreviateSensorLabel('Package id 0 [platform/coretemp.0/temp1]'), 'Pkg');
  context.dashboardConfig.temp_thresholds.Composite = 65;
  assert.equal(context.tempLimitFor(composite), 65, 'qualified history keys retain plain-name thresholds');
  context.dashboardConfig.temp_thresholds[composite] = 75;
  assert.equal(context.tempLimitFor(composite), 75, 'an identity-specific threshold takes precedence');
  const otherComposite = composite.replace('01:00.0', '02:00.0');
  assert.deepEqual(plain(context.sensorDisplayLabels([composite, otherComposite])),
    ['Composite [0000:01:00.0/temp1]', 'Composite [0000:02:00.0/temp1]'],
    'duplicate display names retain only the distinguishing path suffix');
  assert.deepEqual(plain(context.sensorDisplayLabels(['Composite', composite])), ['Composite', 'Composite [temp1]'],
    'legacy and new series remain separate');
  context.dashboardConfig.sensor_labels.Composite = 'Drive temperature';
  assert.equal(context.tempDisplayLabel(composite), 'Drive temperature', 'Lua physical keys preserve old display aliases');
  context.dashboardConfig.sensor_labels[composite] = 'Primary drive';
  assert.equal(context.tempDisplayLabel(composite), 'Primary drive');
  const aggregated = context.aggregateCoreTemps([
    { label: 'Core 0', value: 40 }, { label: 'Core 1 [platform/coretemp.0/temp3]', value: 60 },
    { label: composite, value: 30 },
  ]);
  assert.equal(aggregated.cores.median, 50, 'old and physical-key CPU samples retain their core band');
  assert.deepEqual(plain(aggregated.rest), [{ label: composite, value: 30 }]);
}

function testTempsWithoutSensors() {
  const context = liveContext(['temps-value', 'temps-card', 'fans-card', 'power-card']);
  context.charts = { temps: makeChart(), fans: makeChart() };
  context.sensorsAgeText = () => '';
  context.abbreviateSensorLabel = label => label;
  context.getTempColor = () => '#123456';
  evaluate(context,
    between(config, 'const CORE_TEMP_LABEL', 'function formatTimeAgo') +
    'function tickTemps() {' + between(app, "  guard('temps',", "  guard('load',") + '}');
  const card = context.elements.get('temps-card');
  const tick = (sensors, gpu = { ok: false, errorType: 'unavailable' }) => {
    Object.assign(context, { sensors, gpu });
    context.tickTemps();
    return [context.statuses.temps, card.style.display];
  };
  assert.deepEqual(tick({ ok: false }), [false, ''], 'a failed sensors fetch is shown red');
  assert.deepEqual(tick({ ok: true, data: [] }), [true, 'none'], 'no temperature source is hidden, not failed');
  assert.deepEqual(tick({ ok: true, data: [{ type: 'temperature_core', label: 'cpu', value: 50 }] }), [true, ''], 'readings bring it back');
  assert.deepEqual(tick({ ok: true, data: [] }, { ok: false, errorType: 'http' }), [false, ''], 'a failed GPU source stays red');
  context.dataCache.storageHealth = { lastUpdate: 1, data: { status: 'ok', age_sec: 0, drives: [{ device: 'sda', temperature_c: 30 }] } };
  assert.deepEqual(tick({ ok: true, data: [] }), [false, ''], 'stale drive temperatures stay red');
  assert.deepEqual(context.errors, []);
}

// Glances returning no mounts hides Drive Usage; a hidden card is not a failed one.
function testDriveUsageHidden() {
  const context = liveContext(['drive-usage-card', 'drive-usage-content']);
  context.updateCacheDisplay = () => {};
  evaluate(context, between(app, 'function renderDrives', '// Helper to enrich'));
  context.statuses['drive-usage'] = false;  // e.g. an earlier failed fetch
  context.renderDrives([]);
  assert.equal(context.elements.get('drive-usage-card').style.display, 'none');
  assert.equal(context.statuses['drive-usage'], true, 'the header must not stay Partial for a hidden card');
}

function testSensorFreshness() {
  const context = liveContext(['temps-value', 'temps-card', 'fans-card', 'fans-value', 'power-card', 'power-value',
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
  evaluate(context, between(app, 'function powerSegKey', 'function segColorVar'));
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

// The Hardware card says since when each alarm has been there (the collector's
// alarm_items), keeps showing one that could not be re-checked, and lists the
// newest lines of the alarm log.
function testHardwareSince() {
  const context = liveContext(['hardware-content', 'hardware-value', 'snapshot-age']);
  const now = Date.UTC(2026, 10, 24, 8, 0, 0);
  context.Date = class extends Date { static now() { return now; } };
  context.escapeHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  context.hwHint = text => text.startsWith('journal:') ? 'journalctl' : text.startsWith('sd') ? 'smartctl' : '';
  context.updateCardCritical = (id, bad) => { context.statuses.critical = bad; };
  context.updateCardWarning = (id, value) => { context.statuses.warning = value > 0; };
  context.document.querySelector = () => context.elements.get('snapshot-age');
  evaluate(context, between(app, 'function renderHardware', '// Power bar segment colours'));
  const html = () => context.elements.get('hardware-content').innerHTML;
  const header = () => context.elements.get('hardware-value').textContent;
  const t = Math.floor(now / 1000);
  const item = (key, text, since, extra = {}) => ({ key, text, since, ...extra });
  const journal = ['journal: chassis-fans: failsafe (1 in 24h, at 2026-11-23 15:28)', 'journal: kernel: NVRM (1 in 24h, at 2026-11-24 01:24)'];
  const state = { status: 'ok', age_sec: 30,
    alarms: ['sde: reallocated=8', ...journal],
    alarm_items: [item('smart/S/reallocated', 'sde: reallocated=8', t - 2 * 86400 - 3600, { value: 8, initial: true }),
      item('journal/aaaa', journal[0], t - 5 * 3600), item('journal/bbbb', journal[1], t - 120)],
    alarm_held: [item('smart/S2/health', 'sdb: SMART health FAILED', t - 86400 * 9, { unchecked_since: t - 1800 }),
      item('x', 'absent for one run: flap damping, not shown', t, { absent: 1 })] };
  context.dataCache.sensors = { lastUpdate: now, data: { status: 'ok', age_sec: 5,
    alarms: ['GPU 1: thermal slowdown active (sw)'], alarm_items: [item('gpu/1/slowdown', 'GPU 1: thermal slowdown active (sw)', t - 59)] } };

  // The server's own fan alarm arrives the same way, with since when.
  const fan = 'fan cpu: 0 RPM, below its 300 RPM minimum';
  context.renderHardware({ ...state, sensor_alarms: [fan], sensor_alarm_items: [item('fan/cpu', fan, t - 7 * 60)] });
  assert.equal(header(), '5 issues');
  assert(html().includes(fan) && html().includes('(7 min)'));

  context.renderHardware(state);
  assert.equal(header(), '4 issues');
  // The date as the card must print it, worked out here and not by the code under test.
  const stamp = seconds => { const d = new Date(seconds * 1000), p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; };
  assert.equal(context.sinceText(t - 2 * 86400 - 3600), `${stamp(t - 2 * 86400 - 3600)} (2 d)`);
  assert.match(context.sinceText(t), /^2026-11-2[345] \d\d:\d\d \(0 min\)$/);
  assert.equal(context.sinceText(t + 3600), `${stamp(t + 3600)} (0 min)`, 'a time in the future is not a negative age');
  for (const bad of [null, undefined, NaN, Infinity, 1e18, '5']) assert.equal(context.sinceText(bad), '', `no date from ${bad}`);
  assert(html().includes(`seen since ${context.sinceText(t - 2 * 86400 - 3600)}`) && html().includes('(2 d)'), 'an alarm older than the history says so');
  assert(html().includes(`>since ${context.sinceText(t - 5 * 3600)}<`) && html().includes('(5 h)') && html().includes('(2 min)') && html().includes('(0 min)'));
  assert.equal(html().split('check: journalctl').length - 1, 1, 'two journal lines in a row share one hint');
  assert(html().indexOf('check: journalctl') > html().indexOf('NVRM'), 'after the last of them');
  assert(html().includes('check: smartctl'));
  assert(html().includes('sdb: SMART health FAILED<br>Not re-checked for 30 min'));
  assert(html().includes(`title="Alarm since ${context.sinceText(t - 86400 * 9)}; Not re-checked since ${context.sinceText(t - 1800)}"`),
    'held timestamps remain available without repeating them in the compact row');
  assert(!html().includes('flap damping') && !html().includes('Alarm history'));

  // Nothing wrong that could be checked, but one alarm that could not: not "all clear".
  context.dataCache.sensors.data = { status: 'ok', age_sec: 5, alarms: [] };
  context.renderHardware({ ...state, alarms: [], alarm_items: [] });
  assert.deepEqual([header(), context.elements.get('hardware-value').className, context.statuses.warning, context.statuses.critical],
    ['1 unchecked', 'value text-warning', true, false]);
  assert(!html().includes('no alarms') && html().includes('Not re-checked'));

  // A collector from before alarm_items, or an entry that is not for this line: the text alone.
  context.renderHardware({ status: 'ok', age_sec: 30, alarms: ['md0: DEGRADED (1 missing)'] });
  assert.equal(header(), '1 issue');
  assert(!html().includes('since '));
  context.renderHardware({ status: 'ok', age_sec: 30, alarms: ['a'], alarm_items: [item('k', 'another text', t)] });
  assert(!html().includes('since '));

  // The latest three alarm transitions are visible without opening the full history card.
  const at = seconds => new Date((t - seconds) * 1000).toISOString();
  // Text from the box is escaped wherever it is shown.
  const markup = 'journal: app: <b>&"x"';
  context.renderHardware({ status: 'ok', age_sec: 30, alarms: [markup], alarm_items: [item('journal/m', markup, t - 60)],
    alarm_held: [item('h', '<i>held', t - 60, { unchecked_since: t })] });
  assert(html().includes('journal: app: &lt;b>&amp;&quot;x&quot;') && html().includes('&lt;i>held') && !html().includes('<b>') && !html().includes('<i>'));

  context.dataCache.alarms = { lastUpdate: now, data: { recent: [
    { timestamp: at(9000), event: 'raised', text: 'too old to show' },
    { timestamp: at(8000), event: 'raised', text: 'sde: reallocated=8', initial: true },
    { timestamp: at(7000), event: 'gap', from: 'x', to: 'y' },
    { timestamp: at(6000), event: 'changed', text: 'sde: reallocated=9', was: 8, value: 9 },
    { timestamp: at(5000), event: 'boot', kernel: '6.8.0-149-generic', previous: { clean: false, ended: '2026-11-24T01:21:26+00:00' } },
    { timestamp: 'not a time', event: 'raised', text: 'unreadable line' }, null,
    { timestamp: at(4000), event: 'raised', text: 'GPU 1: thermal slowdown active (sw)' },
    { timestamp: at(2800), event: 'cleared', text: 'GPU 1: thermal slowdown active (sw)', active_for_s: 1200 },
  ] } };
  context.renderHardware({ status: 'ok', age_sec: 30, alarms: [] });
  const recent = context.hardwareAlarmHistoryHTML();
  assert(recent.includes('46 min ago cleared') && recent.includes(`title="${context.alarmTime(t - 2800)}"`),
    'the preview uses relative ages while keeping the exact event time available');
  assert(recent.includes('cleared after 20 min: GPU 1'));
  assert(recent.includes('raised: GPU 1') && recent.includes('sde: reallocated=8'));
  assert(recent.indexOf('cleared after') < recent.indexOf('raised: GPU 1'));
  assert(!recent.includes('too old') && !recent.includes('unreadable') && !recent.includes('gap') && !recent.includes('reallocated=9'));
  // Lines that lack what they should have are shown without the missing part, or not at all.
  context.dataCache.alarms.data.recent = [
    { timestamp: at(500), event: 'changed', text: '<s>sde', value: 9 },
    { timestamp: at(400), event: 'raised' },
    { timestamp: at(300), event: 'boot', previous: { clean: false } },
    { timestamp: at(200), event: 'cleared', text: 'x', active_for_s: -6000 },
    { timestamp: 4e22, event: 'raised', text: 'no such date' },
  ];
  context.renderHardware({ status: 'ok', age_sec: 30, alarms: [] });
  const odd = context.hardwareAlarmHistoryHTML();
  assert(odd.includes(' cleared: x') && !odd.includes('booted') && !odd.includes('sde'));
  assert(!odd.includes('undefined') && !odd.includes('NaN') && !odd.includes('after -') && !odd.includes('no such date'), odd);
  assert.equal((odd.match(/class="hw-row"/g) || []).length, 1, 'only readable lifecycle events are previewed');
  // Historical facts remain visible when a refresh has not succeeded recently.
  context.dataCache.alarms.lastUpdate = now - 6 * 60 * 1000;
  context.renderHardware({ status: 'ok', age_sec: 30, alarms: [] });
  assert(html().includes('Alarm history') && html().includes('previously fetched'));
  context.historyMetricAvailable = metric => metric === 'alarms';
  context.renderHardwareError();
  assert(html().includes('Error loading hardware health') && html().includes(' cleared: x'));
  assert(html().includes('class="history-settings-btn" data-history-metric="alarms"'));
  assert.equal(header(), '--', 'cached history does not imply current health');
  assert.equal(context.statuses.hardware, false, 'failed current health retains the error state');
  assert.equal(context.elements.get('hardware-content').onclick, null);
  assert.deepEqual(context.errors, []);
}

// Absent hardware is absent: a box with no BMC and no EDAC gets no Memory rows
// (the collector writes null for both), and the check hint names only the tools
// for what is present.
function testHardwareMemoryRows() {
  const context = liveContext(['hardware-content', 'hardware-value', 'snapshot-age']);
  context.Date = { now: () => 100000000 };
  context.escapeHtml = String;
  context.hwHint = () => '';
  context.updateCardCritical = () => {};
  context.document.querySelector = () => context.elements.get('snapshot-age');
  evaluate(context, between(app, 'function renderHardware', '// Power bar segment colours'));
  const html = data => {
    context.renderHardware({ status: 'ok', age_sec: 30, alarms: [], info: [], ...data });
    return context.elements.get('hardware-content').innerHTML;
  };
  const none = html({ edac: null, sel_memory: null, sel_hardware: null });
  assert(!none.includes('Memory'), 'no Memory section without a BMC or EDAC');
  assert(!none.includes('ECC since boot') && !none.includes('BMC log'));
  const ecc = html({ edac: { uptime_seconds: 7200, ce_total: 0, ue_total: 0 }, sel_memory: null, sel_hardware: null });
  assert(ecc.includes('ECC since boot (2 h): 0 corrected, 0 uncorrectable'));
  assert(ecc.includes('check: sudo ras-mc-ctl --errors') && !ecc.includes('ipmitool'));
  assert(ecc.includes('<details data-hardware-details >'), 'secondary diagnostics start collapsed');
  context.elements.get('hardware-content').querySelector = selector => selector === '[data-hardware-details]' ? { open: true } : null;
  const server = html({ edac: { uptime_seconds: 7200, ce_total: 0, ue_total: 0 },
    sel_memory: { count: 0, recent: [] }, sel_hardware: { count: 0, recent: [] } });
  assert(server.includes('BMC log, memory events: 0 lifetime'));
  assert(server.includes('check: sudo ipmitool sel elist; sudo ras-mc-ctl --errors'));
  assert(server.includes('<details data-hardware-details open>'), 'refresh keeps diagnostics expanded');
}

// hwmon power (/api/4/sensors rows of type 'power') joins the bar as a part; a
// whole-machine reading is the total, and the BMC's wins over an hwmon meter. No
// PSU in config means no dashed line and no PSU warning.
function testPowerFromHwmonAndTotals() {
  const context = liveContext(['temps-value', 'temps-card', 'fans-card', 'fans-value', 'power-card', 'power-value',
    'power-bar', 'snapshot-age']);
  const now = 100000000;
  context.Date = { now: () => now };
  context.gpu = { ok: false };
  context.charts = { temps: makeChart(), fans: makeChart() };
  context.sensorsAgeText = () => '';
  context.abbreviateSensorLabel = value => value;
  context.getTempColor = () => '#123456';
  context.escapeHtml = String;
  context.segColorVar = () => '--color-accent';
  context.setHtml = (el, html) => { el.innerHTML = html; };
  context.document.querySelector = () => context.elements.get('snapshot-age');
  evaluate(context,
    between(config, 'const CORE_TEMP_LABEL', 'function formatTimeAgo') +
    between(app, 'function powerSegKey', 'function segColorVar') +
    'function tickTemps() {' + between(app, "  guard('temps',", "  guard('load',") + '}');
  assert.deepEqual(['GPU 0', 'CPU 1', 'CPU (est.)', 'DRAM 0', 'GPU (amdgpu)', 'acme power3'].map(context.powerSegKey),
    ['gpu0', 'cpu1', 'cpu0', 'dram0', 'gpu0', 'other']);
  context.sensors = { ok: true, data: [
    { type: 'power', label: 'CPU (est.)', value: 64, total: false },
    { type: 'fan_speed', label: 'f71889ed fan1', value: 1600 },
  ] };
  context.tickTemps();
  const bar = () => context.elements.get('power-bar').innerHTML;
  assert(bar().includes('power-seg-cpu0') && !bar().includes('power-psu'), 'a part, coloured as a CPU, no PSU line');
  assert.equal(context.elements.get('power-value').textContent, '≥ 64 W');
  assert(bar().includes('scale 100 W'), '1.5 x 64 W rounded up to 50 W');
  // Parts only, with a PSU: the lower bound warns once it alone passes the line.
  context.thresholds.power_psu_watts = 430;
  context.thresholds.power_warn_fraction = 300 / 430;
  context.tickTemps();
  assert(bar().includes('warns at 300 W (70% of PSU)'));
  assert.equal(context.elements.get('power-value').className, 'value');
  context.sensors.data[0].value = 310;
  context.tickTemps();
  assert.equal(context.elements.get('power-value').className, 'value text-warning');
  context.sensors.data[0].value = 64;
  delete context.thresholds.power_psu_watts;
  delete context.thresholds.power_warn_fraction;
  // An hwmon whole-machine meter, then the BMC's reading as well: the BMC wins.
  context.sensors.data.push({ type: 'power', label: 'System (ACPI)', value: 150, total: true });
  context.tickTemps();
  assert.equal(context.elements.get('power-value').textContent, '150 W');
  context.dataCache.sensors = { lastUpdate: now, data: { status: 'ok', age_sec: 0, alarms: [], power: [
    { label: 'Pwr Consumption', value: 200, unit: 'W', total: true },
    { label: 'PSU1 Current', value: 0.9, unit: 'A', total: false },
  ] } };
  context.thresholds.power_psu_watts = 500;
  context.tickTemps();
  assert.equal(context.elements.get('power-value').textContent, '200 W (40%)');
  assert(bar().includes('power-psu') && !bar().includes('System (ACPI)') && !bar().includes('PSU1 Current'));
  assert.deepEqual(context.errors, []);
  // History: each record names its whole-machine reading; old records had only the BMC's.
  evaluate(context, between(app, 'const LEGACY_POWER_TOTAL_KEY', '// History charts with more records'));
  assert.equal(context.powerTotalKey({ power: { 'System (ACPI)': 150 }, power_total: 'System (ACPI)' }), 'System (ACPI)');
  assert.equal(context.powerTotalKey({ power: { 'Pwr Consumption': 238, 'CPU 0': 80 } }), 'Pwr Consumption');
  assert.equal(context.powerTotalKey({ power: { 'CPU (est.)': 64 } }), null);

  // The normalized API is authoritative, including an empty list. Legacy PSU
  // readings cannot become a partial total or a component beside CPU power.
  const snapshot = context.dataCache.sensors.data;
  snapshot.power = [
    { label: 'PS1 Input Power', unit: 'W', value: 100, psu_input: true },
    { label: 'PS2 Input Power', unit: null, value: null, psu_input: true },
  ];
  context.tickTemps();
  assert.equal(context.elements.get('power-value').textContent, '150 W (30%)', 'the ACPI total wins over incomplete PSU inputs');
  assert(!bar().includes('PS1 Input Power'));
  snapshot.power_rows = [{ label: 'PSU input', value: 230, total: true }];
  context.tickTemps();
  assert.equal(context.elements.get('power-value').textContent, '230 W (46%)', 'the server can supply a verified complete PSU sum');
  snapshot.power_rows = [];
  context.sensors.data = context.sensors.data.filter(s => s.type !== 'power' || !s.total);
  context.tickTemps();
  assert.equal(context.elements.get('power-value').textContent, '≥ 64 W', 'no total means a lower bound');
  delete snapshot.power_rows;
  snapshot.power = [{ label: 'PS1 Input Power', value: 100, reading: '100 Watts' }];
  context.tickTemps();
  assert.equal(context.elements.get('power-value').textContent, '≥ 64 W', 'older collectors without role flags are conservative too');
  context.sensors.data.find(s => s.type === 'fan_speed').id = '@platform/controller0/acme/fan1';
  context.tickTemps();
  assert.deepEqual(plain(context.charts.fans.data.labels), ['f71889ed fan1']);
  context.dashboardConfig.sensor_labels = { 'f71889ed fan1': 'CPU fan' };
  context.tickTemps();
  assert.deepEqual(plain(context.charts.fans.data.labels), ['CPU fan']);
  assert.equal(context.tempDisplayLabel('@platform/controller0/acme/fan1'), 'CPU fan', 'history can use the current inventory label without changing its key');
  assert.deepEqual(context.errors, []);
}

// The server's own findings (hwmon fan rules, hints) reach the Hardware card through
// /api/storage-health, also on a box with no collector snapshot at all.
function testHardwareShowsServerSensorAlarms() {
  const context = liveContext(['hardware-content', 'hardware-value', 'snapshot-age']);
  context.Date = { now: () => 100000000 };
  context.escapeHtml = String;
  context.hwHint = () => '';
  const critical = [];
  context.updateCardCritical = (id, on) => critical.push(on);
  context.document.querySelector = () => context.elements.get('snapshot-age');
  context.dataCache.sensors = { lastUpdate: 100000000, data: { status: 'ok', age_sec: 0, alarms: [] } };
  evaluate(context, between(app, 'function renderHardware', '// Power bar segment colours'));
  const fanAlarm = 'fan f71889ed fan1: 0 RPM, below its 300 RPM minimum';
  context.renderHardware({ status: 'missing', path: '/var/lib/horatio/state.json',
    sensor_alarms: [fanAlarm], sensor_notes: ['a hint'] });
  let html = context.elements.get('hardware-content').innerHTML;
  assert(html.includes(fanAlarm) && html.includes('a hint') && html.includes('No health snapshot yet'));
  assert.equal(context.elements.get('hardware-value').textContent, '1 issue');
  assert.equal(critical.at(-1), true);
  context.renderHardware({ status: 'ok', age_sec: 30, alarms: [], info: [],
    sensor_alarms: [fanAlarm], sensor_notes: ['a hint'] });
  html = context.elements.get('hardware-content').innerHTML;
  assert.equal(context.elements.get('hardware-value').textContent, '1 issue');
  assert(html.includes(fanAlarm) && html.includes('a hint'));
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
    renderGpuInfo() {}, renderDrives() {}, renderIpInfo() {}, renderMail() {}, renderHardware() {}, servicesConfigured() { return false; }, renderDriveHealth() {},
    diskCapture: { handleResponse() {} }, networkCapture: { handleResponse() {} },
  });
  evaluate(context,
    between(config, 'const dataCache =', '// The CPU core temperatures') +
    between(app, 'async function fetchSlowData', '// One poll at a time') +
    between(app, 'function guard', 'async function fetchDataOnce') +
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
  assert.equal(finished, true, 'the slow poll does not wait for the IP lookup');
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

async function testSlowPollRenderIsolation() {
  const statuses = {}, rendered = [], errors = [];
  const record = name => () => { rendered.push(name); };
  const context = vm.createContext({
    Date, API_BASE: '/api/4', IP_INFO_TIMEOUT_MS: 20000,
    console: { error: (...args) => errors.push(args.map(String).join(' ')) },
    // storage-health answers 200 with a null body.
    async safeFetch(url) {
      return { ok: true, data: url.endsWith('/fs') ? [] : url.endsWith('/uptime') ? '1h' : url.endsWith('health') ? null : {} };
    },
    updateCardNoData(id, ok) { statuses[id] = ok; },
    renderGpuInfo() { throw new TypeError('bad GPU info'); },
    renderHardware(data) { return data.status; }, servicesConfigured() { return false; },
    renderCpuInfo: record('cpu'), renderSystemInfo: record('system'), renderDrives: record('drives'),
    renderIpInfo: record('ip'), renderMail: record('mail'), renderDriveHealth: record('drive-health'),
    diskCapture: { handleResponse: record('disk') }, networkCapture: { handleResponse: record('network') },
  });
  evaluate(context,
    between(config, 'const dataCache =', '// The CPU core temperatures') +
    between(app, 'async function fetchSlowData', '// One poll at a time') +
    between(app, 'function guard', 'async function fetchDataOnce') +
    "['diskProcesses', 'networkProcesses'].forEach(key => { dataCache[key].ttl = Infinity; updateCache(key, []); });");
  await context.fetchSlowData();
  assert.deepEqual(statuses, { 'gpu-info': false, hardware: false }, 'only the throwing cards are marked failed');
  assert.deepEqual(rendered.sort(),
    ['cpu', 'disk', 'drive-health', 'drives', 'ip', 'mail', 'network', 'system'], 'every other card still renders');
  assert.equal(errors.length, 2);
}

// A capture still running when the next poll (or a refresh click) comes is
// waited for, not requested again; a hidden tab starts no timers.
async function testCapturePendingAndHiddenTab() {
  const calls = {}, release = {};
  const context = vm.createContext({
    Date, console: { error() {}, warn() {} },
    document: { getElementById: () => null, querySelector: () => null },
    setInterval: () => 1, clearInterval() {},
    nethogsDuration: 3, diskDuration: 1, API_BASE: '/api/4', cardStatus: { processes: { ok: true } },
    getDiskTimeout: () => 6500, getNethogsTimeout: () => 9500,
    renderUnifiedProcesses() {}, renderGpuInfo() {}, renderCpuInfo() {}, renderDrives() {}, renderIpInfo() {},
    renderMail() {}, renderHardware() {}, servicesConfigured() { return false; }, renderDriveHealth() {}, renderSystemInfo() {},
    safeFetch(url) {
      calls[url] = (calls[url] || 0) + 1;
      if (url.includes('-processes')) return new Promise(resolve => { release[url] = resolve; });
      return Promise.resolve({ ok: true, data: url.endsWith('/fs') ? [] : url.endsWith('/uptime') ? '1h' : {} });
    },
  });
  evaluate(context,
    between(config, 'const IP_INFO_TIMEOUT_MS', 'const SLOW_POLL_INTERVAL') +
    between(config, 'const dataCache =', '// The CPU core temperatures') +
    between(config, 'function formatTimeAgo', 'function updateCacheDisplay') +
    between(app, 'function createCaptureState', '// Unified processes table state') +
    between(app, 'async function fetchSlowData', '// One poll at a time') +
    between(app, 'function guard', 'async function fetchDataOnce'));
  const net = '/api/network-processes?duration=3', disk = '/api/disk-processes?duration=1';
  await context.fetchSlowData();
  await context.fetchSlowData();
  assert.deepEqual([calls[net], calls[disk]], [1, 1], 'a capture still out is not requested again');
  assert.equal(evaluate(context, 'networkCapture.capturing'), true);
  release[net]({ ok: true, data: [{ pid: 1, name: 'a' }] });
  release[disk]({ ok: false, data: [], errorType: 'timeout' });
  await new Promise(setImmediate);
  assert.equal(evaluate(context, 'networkCapture.requestPending || diskCapture.requestPending'), false);
  assert.equal(evaluate(context, 'diskCapture.error'), true);
  await context.fetchSlowData();
  assert.deepEqual([calls[net], calls[disk]], [1, 2], 'a failed capture is retried; a fresh result is reused');

  const timers = [];
  const polling = vm.createContext({
    isConnected: true, pollingInterval: null, slowPollingInterval: null, pollInterval: 2000,
    SLOW_POLL_INTERVAL: 30000, document: { hidden: true }, fetchData() {}, fetchSlowData() {},
    setInterval: (callback, delay) => timers.push(delay),
  });
  evaluate(polling, between(app, 'function startPolling', '// Toggle connection'));
  polling.startPolling();
  assert.deepEqual(timers, [], 'a hidden tab starts no polling');
  polling.document.hidden = false;
  polling.startPolling();
  assert.deepEqual(timers, [2000, 30000], 'a visible one does');
}

async function testCaptureCacheAge() {
  let now = 103000, captureError = false, captureDelay = 0;
  const calls = {};
  const context = vm.createContext({
    Date: { now: () => now }, console: { error() {}, warn() {} },
    document: { getElementById: () => null, querySelector: () => null },
    setInterval: () => 1, clearInterval() {},
    nethogsDuration: 3, diskDuration: 1, API_BASE: '/api/4', cardStatus: { processes: { ok: true } },
    getDiskTimeout: () => 6500, getNethogsTimeout: () => 9500,
    renderUnifiedProcesses() {}, renderGpuInfo() {}, renderCpuInfo() {}, renderDrives() {}, renderIpInfo() {},
    renderMail() {}, renderHardware() {}, servicesConfigured() { return false; }, renderDriveHealth() {}, renderSystemInfo() {},
    async safeFetch(url) {
      calls[url] = (calls[url] || 0) + 1;
      if (url.includes('network-processes')) now += captureDelay;  // the tick's captures take time
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
    between(app, 'async function fetchSlowData', '// One poll at a time') +
    between(app, 'function guard', 'async function fetchDataOnce'));
  await context.fetchSlowData();  // Initial captures complete three seconds after startup.
  const timestamp = name => evaluate(context, `${name}Capture.getTimestamp()`);
  const capturedAt = name => evaluate(context, `${name}Capture.lastCaptureTime`);
  const url = name => `/api/${name}-processes?duration=${name === 'network' ? 3 : 1}`;
  now = 120000;  // A manual refresh of another card reuses a still-valid 17-second-old result.
  assert.equal(timestamp('network'), '17s ago');
  await context.fetchSlowData();
  for (const name of ['network', 'disk']) {
    assert.equal(timestamp(name), '17s ago', 'cache reuse preserves the capture age');
    assert.equal(capturedAt(name), 103000);
    assert.equal(calls[url(name)], 1, 'the cached poll did not perform another capture');
  }
  now = 133000;  // The next regular slow poll (30 s after the capture started) captures again.
  await context.fetchSlowData();
  for (const name of ['network', 'disk']) {
    assert.equal(calls[url(name)], 2, 'every slow poll gets a fresh capture');
  }
  // A capture's age counts from its start: a long capture must not leave a
  // result the following poll still treats as fresh (that would halve the cadence).
  captureDelay = 10000;
  now = 163000;
  await context.fetchSlowData();
  captureDelay = 0;
  assert.equal(now, 173000, 'the capture advanced the clock');
  assert.equal(timestamp('network'), '10s ago', 'the timestamp counts from the capture start');
  assert.equal(capturedAt('disk'), 163000);
  now = 193000;  // 30 s after that capture started, 20 s after it ended.
  await context.fetchSlowData();
  for (const name of ['network', 'disk']) {
    assert.equal(calls[url(name)], 4, 'the poll after a long capture still re-captures');
  }
  assert.equal(timestamp('network'), '0s ago', 'a fresh capture advances the timestamp');
  assert.equal(capturedAt('disk'), now);
  captureError = true;
  now = 240000;
  await context.fetchSlowData();
  for (const name of ['network', 'disk']) {
    assert.equal(capturedAt(name), 193000, 'errors do not replace the last successful capture');
    assert.equal(evaluate(context, `${name}Capture.error`), true);
    assert.equal(evaluate(context, `${name}Capture.capturing`), false);
  }
  // A backend with no process card (Lua) starts no capture and logs no error.
  evaluate(context, 'delete cardStatus.processes; networkCapture.clearError(); diskCapture.clearError();');
  now = 300000;
  await context.fetchSlowData();
  for (const name of ['network', 'disk']) {
    assert.equal(calls[url(name)], 5, 'no capture request without a process card');
    assert.equal(evaluate(context, `${name}Capture.capturing`), false);
    assert.equal(evaluate(context, `${name}Capture.error`), false);
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
      for (const match of html.matchAll(/<(input|button|div|textarea)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
        const el = new Element(match[1]);
        el.checked = /\bchecked\b/.test(match[2]);
        el.disabled = /\bdisabled\b/.test(match[2]);
        this.children.set(match[3], el);
      }
    }
    querySelector(selector) { return this.children.get(selector.slice(1)); }
    querySelectorAll() { return [...this.children.values()].filter(el => ['input', 'button'].includes(el.tag)); }
    addEventListener(type, callback) { this.events[type] = callback; }
    setAttribute(name, value) { this[name] = value; }
    focus() {}
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
    safeFetch(url) { const pending = defer(); reads.push({ ...pending, url }); return pending.promise; },
    fetch(url, options) { const pending = defer(); writes.push({ ...pending, url, body: JSON.parse(options.body) }); return pending.promise; },
    setTimeout(callback, delay) { timers.push({ callback, delay }); return timers.length; },
  });
  evaluate(context, between(app, 'let historySettingsPopup =', '// Middle-click on History label'));
  const current = () => evaluate(context, 'historySettingsPopup');
  const open = () => { context.createHistorySettingsPopup(); return current(); };
  const finishRead = async (index, data, ok = true) => {
    reads[index].resolve({ ok, data });
    await new Promise(setImmediate);
  };
  const finishWrite = (index, data, ok = true, status = ok ? 200 : 400) => writes[index].resolve({ ok, status, json: async () => data });
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
  h.context.closeHistorySettingsPopup();
  popup = h.open();
  await h.finishRead(2, { ...historySettings, editable: false, password_configured: false,
    detail: 'Edit www/config.json and restart the service.' });
  assert(field('confirm').disabled && field('interval').disabled && field('password').disabled);
  assert.equal(field('status').textContent, 'Edit www/config.json and restart the service.');
  const writes = h.writes.length;
  await h.context.submitHistorySettings();
  assert.equal(h.writes.length, writes, 'a read-only backend never gets a settings POST');
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
    getCssColor: () => '#123456', getCssNumber: (_, fallback) => fallback, lsSet() {},
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

async function testVersionFooter() {
  const footer = async answer => {
    const dom = makeDOM(['version-label']);
    await evaluate(vm.createContext({ ...dom, safeFetch: async () => answer }),
      app.slice(app.indexOf('// The running version (/api/version)')));
    return dom.elements.get('version-label');
  };
  let label = await footer({ ok: true, data: { version: '1.2.0', build: 'v1.2.0-3-gaaaaaaa' } });
  assert.equal(label.textContent, 'horatio v1.2.0');
  assert.equal(label.title, 'build v1.2.0-3-gaaaaaaa');
  label = await footer({ ok: true, data: { version: 'demo', build: null } });
  assert.equal(label.textContent, 'horatio demo');
  label = await footer({ ok: false, data: null, errorType: 'network' });
  assert.equal(label.textContent, '', 'no version, no footer text');
}

async function testThemes() {
  // The <head> loader: a stored valid name becomes a stylesheet link ahead of
  // the scripts; traversal, odd names and storage that throws write nothing.
  const html = fs.readFileSync(path.join(__dirname, '..', '..', 'index.html'), 'utf8');
  const loader = /<script>(\(function \(\) \{[\s\S]*?horatioTheme[\s\S]*?\}\)\(\);)<\/script>/.exec(html);
  assert(loader, 'theme loader in index.html');
  assert(html.indexOf(loader[0]) < html.indexOf('<script src='), 'the theme loads before the scripts');
  const written = stored => {
    const out = [];
    vm.runInNewContext(loader[1], {
      localStorage: { getItem() { if (stored instanceof Error) throw stored; return stored; } },
      document: { write: s => out.push(s) },
    });
    return out;
  };
  assert.deepEqual(written('classic'), ['<link rel="stylesheet" href="themes/classic.css">']);
  for (const bad of ['../x', 'a.b', 'x y', '', null, new Error('blocked')]) {
    assert.deepEqual(written(bad), [], String(bad));
  }

  // withAlpha takes any color the canvas understands, not just #rrggbb.
  const canvasForm = { '#000': '#000000', '#fff': '#ffffff', '#abc': '#aabbcc', red: '#ff0000', '#123456': '#123456',
    'rgb(1 2 3 / 50%)': 'rgba(1, 2, 3, 0.5)' };
  const canvas = { value: '#000000', set fillStyle(c) { if (c in canvasForm) this.value = canvasForm[c]; },
    get fillStyle() { return this.value; } };
  const colors = vm.createContext({ document: { createElement: () => ({ getContext: () => canvas }) } });
  evaluate(colors, between(config, '// `color` at the opacity', '// A unitless number'));
  assert.equal(colors.withAlpha('#123456', '20'), 'rgba(18, 52, 86, 0.125)');
  assert.equal(colors.withAlpha('#abc', 'ff'), 'rgba(170, 187, 204, 1)');
  assert.equal(colors.withAlpha('red', '80'), 'rgba(255, 0, 0, 0.502)');
  assert.equal(colors.withAlpha('rgb(1 2 3 / 50%)', '80'), 'rgba(1, 2, 3, 0.251)', 'alpha multiplies');
  assert.equal(colors.cssRgb('not-a-color'), null, 'the canvas ignored it');
  assert.equal(colors.cssRgb(''), null);
  assert.equal(colors.withAlpha('not-a-color', '20'), 'not-a-color');

  // Value scales: the default colors without a theme, a theme's when it sets them.
  const scaleSource = between(charts, '// Value scales', 'function gpuChartConfig');
  const scales = vars => {
    const context = vm.createContext({ getCssColor: name => vars[name] || '', cssRgb: colors.cssRgb });
    evaluate(context, scaleSource);
    return context;
  };
  let s = scales({});
  assert.equal(s.getCoreColor(0), 'rgb(34, 197, 94)');
  assert.equal(s.getCoreColor(50), 'rgb(245, 158, 11)');
  assert.equal(s.getCoreColor(100), 'rgb(239, 68, 68)');
  assert.equal(s.getNetworkRateColor(0.5), '#166534');
  assert.equal(s.getInfoColor(5, 10), 'rgb(80, 123, 193)');
  assert.equal(s.getMemoryColor(50 * 2 ** 20), '#4a7a5a');
  assert.equal(s.getMemoryColor(3000 * 2 ** 20), '#ffb020');
  assert.equal(s.getMemoryColor(20000 * 2 ** 20), '#ff3030');
  s = scales({ '--scale-low': '#123456', '--scale-high': 'red', '--scale-mem-7': '#000000' });
  assert.equal(s.getCoreColor(0), 'rgb(18, 52, 86)');
  assert.equal(s.getCoreColor(100), 'rgb(255, 0, 0)');
  assert.equal(s.getMemoryColor(20000 * 2 ** 20), '#000000');

  // A frame stamped before the animation started must not swing the number backwards.
  const shown = makeDOM(['n']).elements.get('n');
  shown.dataset.value = '10';
  let nextFrame = null;
  const animation = vm.createContext({
    performance: { now: () => 0 }, requestAnimationFrame: callback => { nextFrame = callback; },
  });
  evaluate(animation, between(config, 'function animateValue', '// GPU colors'));
  animation.animateValue(shown, 20, '%', 1);
  const shownAt = t => { const frame = nextFrame; nextFrame = null; frame(t); return shown.textContent; };
  assert.equal(shownAt(-500), '10.0%', 'a frame stamped before the start shows the old value');
  assert.equal(shownAt(400), '20.0%');
  assert.equal(nextFrame, null, 'and the animation ends');

  // A new value takes over an animation still running, from where it had got to.
  const cancelled = [];
  animation.requestAnimationFrame = callback => { nextFrame = callback; return 7; };
  animation.cancelAnimationFrame = id => { cancelled.push(id); nextFrame = null; };
  animation.animateValue(shown, 40, '%', 1);
  const halfway = parseFloat(shownAt(100));
  assert(halfway > 20 && halfway < 40, 'part of the way: ' + halfway);
  animation.animateValue(shown, 0, '%', 1);
  assert.deepEqual(cancelled, [7], 'the running animation is cancelled');
  assert.equal(parseFloat(shownAt(0)), halfway, 'the new one starts from the value on screen');
  assert.equal(shownAt(400), '0.0%');
  assert.equal(nextFrame, null);

  // The picker: options from themes/index.json; a change stores it and reloads.
  const pickerContext = (stored, fetchImpl) => {
    const dom = makeDOM(['theme-select']), store = new Map(stored ? [['horatioTheme', stored]] : []);
    const state = { store, reloads: 0, picked: null, click: null, button: dom.elements.get('theme-select') };
    state.button.addEventListener = (type, handler) => { state.click = handler; };
    const context = vm.createContext({
      ...dom, fetch: fetchImpl,
      lsGet: key => store.get(key) ?? null, lsSet: (key, value) => store.set(key, String(value)),
      lsRemove: key => store.delete(key), location: { reload: () => state.reloads++ },
      createDropdownPicker: (el, options) => { state.picked = options; },
    });
    evaluate(context, between(app, '// Set up theme selector', '// Set up poll interval selector'));
    return state;
  };
  const tick = () => new Promise(resolve => setImmediate(resolve));
  const listing = async () => ({ ok: true, json: async () => ['classic', 'light', 'default', '../x', 3] });
  s = pickerContext('gone', listing);
  await tick();
  assert.equal(s.store.has('horatioTheme'), false, 'a deleted theme is forgotten');
  assert.equal(s.button.textContent, 'default');
  await s.click();
  assert.deepEqual(plain(s.picked.options.map(o => o.value)), ['default', 'classic', 'light']);
  assert.equal(s.picked.currentValue, 'default');
  s.picked.onSelect('default');
  assert.equal(s.reloads, 0, 'choosing the current theme does nothing');
  s.picked.onSelect('classic');
  assert.equal(s.store.get('horatioTheme'), 'classic');
  assert.equal(s.reloads, 1);
  s.picked.onSelect('default');
  assert.equal(s.store.has('horatioTheme'), false);
  assert.equal(s.reloads, 2);
  // An unreadable list (offline, demo without it) keeps the choice and offers default.
  s = pickerContext('classic', async () => { throw new Error('offline'); });
  await tick();
  assert.equal(s.store.get('horatioTheme'), 'classic');
  assert.equal(s.button.textContent, 'classic');
  await s.click();
  assert.deepEqual(plain(s.picked.options.map(o => o.value)), ['default']);
}

function testHistoryGapSkip() {
  const context = vm.createContext({});
  evaluate(context, between(app, 'function historyGapMs', 'async function createHistoryCard'));
  assert.equal(context.historyGapMs(undefined), 3600e3, 'the default is an hour');
  assert.equal(context.historyGapMs(120), 120e3, 'the limit comes from config');
  assert.equal(context.historyGapMs('7200'), 7200e3);
  assert.equal(context.historyGapMs(0), Infinity, '0 turns it off');
  assert.equal(context.historyGapMs(-5), Infinity);
  assert.equal(context.historyGapMs('junk'), Infinity);
  // Markers past HISTORY_MARKER_MAX: only a point with no line to either side.
  const at = min => new Date(min * 60e3);
  const times = [0, 10, 20, 200, 400, 410, 420].map(at), values = [1, 1, 1, 1, 1, null, 1];
  const alone = gap => plain(times.map((_, i) => context.historyPointAlone(values, times, i, gap)));
  assert.deepEqual(alone(3600e3), [false, false, false, true, true, false, true],
    'a record between two outages, one between an outage and a null, and the last after a null');
  assert.deepEqual(alone(Infinity), [false, false, false, false, false, false, true], 'gap rule off: only nulls count');
}

// What every history card's axes are built from (charts.js).
const HISTORY_AXES = between(charts, '// A y-axis number', 'const CHART_X_AXIS');

function testHistoryAxes() {
  const context = vm.createContext({ CHART_FONT_SIZE: 16 });
  evaluate(context, HISTORY_AXES + '; globalThis.HISTORY_AXIS_WIDTH = HISTORY_AXIS_WIDTH;');
  const labels = (from, to, width) => plain(context.historyTicks(from.getTime(), to.getTime(), width)).map(t => t.label);
  // Local times throughout, as the axis shows them.
  assert.deepEqual(labels(new Date(2026, 10, 18, 19, 55), new Date(2026, 10, 18, 20, 55), 1180),
    ['20:00', '20:05', '20:10', '20:15', '20:20', '20:25', '20:30', '20:35', '20:40', '20:45', '20:50'],
    'round five-minute steps; a tick at either end of the axis is left out');
  assert.deepEqual(labels(new Date(2026, 10, 17, 20, 55), new Date(2026, 10, 18, 20, 55), 1000),
    ['Nov 18', '03:00', '06:00', '09:00', '12:00', '15:00', '18:00'], 'midnight carries the date; 21:00 is too near the end');
  assert.deepEqual(labels(new Date(2026, 10, 17, 20, 55), new Date(2026, 10, 18, 20, 55), 400),
    ['Nov 18', '06:00', '12:00', '18:00'], 'a narrow axis takes longer steps');
  const month = labels(new Date(2026, 9, 19, 20, 55), new Date(2026, 10, 18, 20, 55), 1180);
  assert.deepEqual(month, ['Oct 22', 'Oct 25', 'Oct 28', 'Oct 31', 'Nov 3', 'Nov 6', 'Nov 9', 'Nov 12', 'Nov 15', 'Nov 18']);
  assert.deepEqual(labels(new Date(0), new Date(0), 1000), []);
  assert.deepEqual(labels(new Date(0), new Date(3600e3), 0), []);

  // As Chart.js calls it: the value, its index, and all the ticks.
  const axis = values => values.map((v, i) =>
    context.axisNumber(v, i, values.map(value => ({ value }))).replace(/[^0-9kM]/g, '.'));
  assert.deepEqual(axis([0, 0.002, 0.004, 0.006]), ['0', '0.002', '0.004', '0.006'], 'decimals follow the step');
  assert.deepEqual(axis([0, 2.5, 5, 7.5]), ['0', '2.5', '5', '7.5']);
  assert.deepEqual(axis([0, 20, 40]), ['0', '20', '40']);
  assert.deepEqual(axis([0, 500, 1000, 1500]), ['0', '500', '1.000', '1.500']);
  assert.deepEqual(axis([0, 12500, 2000000]), ['0', '12.5k', '2M']);
  assert.equal(context.HISTORY_AXIS_WIDTH, 84);
}

// The Services card and the outage grid, from the real render functions.
function testServicesCard() {
  const elements = new Map(['services-value', 'services-content'].map(id =>
    [id, { id, textContent: '', className: '', innerHTML: '' }]));
  const flags = {};
  const context = vm.createContext({
    document: { getElementById: id => elements.get(id) },
    setHtml(el, html) { el.innerHTML = html; },
    escapeHtml: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'),
    updateCardNoData(id, ok) { flags.data = ok; },
    updateCardCritical(id, bad) { flags.critical = bad; },
    updateCardWarning(id, value, threshold) { flags.warning = value > threshold; },
    dashboardConfig: {},
  });
  evaluate(context, 'const CHART_FONT_SIZE = 16;' + HISTORY_AXES);
  evaluate(context, between(app, 'function renderServices', '// Shared by the Hardware health'));
  const target = (name, ok, extra = {}) => ({ name, url: `http://${name}/`, ok, status: ok ? 200 : null,
    ms: ok ? 12 : null, error: ok === false ? 'timeout' : null,
    uptime: { 1: { up: 60, checks: 60 }, 24: { up: 1439, checks: 1440 }, 168: { up: 0, checks: 0 } }, ...extra });

  context.renderServices({ targets: [target('pages', true), target('<immich>', true)] });
  assert.equal(elements.get('services-value').textContent, 'all up');
  assert.deepEqual(flags, { data: true, critical: false, warning: false });
  const html = elements.get('services-content').innerHTML;
  assert(html.includes('12 ms') && html.includes('>100%<') && html.includes('>99.93%<') && html.includes('>--<'));
  assert(html.includes('&lt;immich>') && !html.includes('<immich>'), 'names are escaped');

  context.renderServices({ targets: [target('pages', true), target('immich', false)] });
  assert.equal(elements.get('services-value').textContent, '1 down');
  assert.equal(flags.critical, true);
  assert(elements.get('services-content').innerHTML.includes('down (timeout)'));
  assert(!elements.get('services-content').innerHTML.includes(' since '), 'a backend that does not say since when');
  // When the outage began, from the server; never on a target that is up.
  const began = Date.now() / 1000 - 3 * 3600 - 60;
  context.renderServices({ targets: [target('pages', true, { down_since: began }), target('immich', false, { down_since: began })] });
  assert.equal(elements.get('services-content').innerHTML.split(`down (timeout) since ${context.sinceText(began)}`).length - 1, 2, 'in the cell and its tooltip, once each');
  assert(elements.get('services-content').innerHTML.includes('(3 h)'));

  // The checker stopped: the last results are not shown as the present.
  context.renderServices({ stale: true, targets: [target('pages', true), target('immich', false)] });
  assert.equal(elements.get('services-value').textContent, 'stale');
  assert.deepEqual(flags, { data: true, critical: false, warning: true });
  assert(elements.get('services-content').innerHTML.includes('stale, was up'));
  assert(!elements.get('services-content').innerHTML.includes('svc-dot svc-up'));

  context.renderServices({ targets: [target('pages', null)] });
  assert.equal(elements.get('services-value').textContent, '--', 'nothing checked yet is not "all up"');

  // A failed fetch after a round with a target down: nothing of that round stays.
  context.renderServices({ targets: [target('pages', true), target('immich', false)] });
  assert.deepEqual([elements.get('services-value').textContent, flags.critical], ['1 down', true]);
  context.resetServicesHeader();
  assert.equal(elements.get('services-value').textContent, '--');
  assert.equal(elements.get('services-value').className, 'value');
  assert.equal(flags.critical, false);

  // 1439 of 1440 must never round up to 100%.
  assert.equal(context.uptimeText({ up: 99999, checks: 100000 }).text, '99.99%');
  assert.equal(context.uptimeText({ up: 9, checks: 10 }).text, '90.0%');

  const grid = context.servicesGridHTML({ start: 0, bucket_sec: 300, count: 3, truncated: false,
    targets: { pages: [[5, 0], [1, 4], [0, 0]] } });
  assert.equal((grid.match(/svc-cell-up/g) || []).length, 1);
  assert.equal((grid.match(/svc-cell-none/g) || []).length, 1);
  assert(grid.includes('opacity:0.89') && grid.includes('pages, ') && grid.includes('4 of its 5 checks failed') && grid.includes('no checks'));
  assert(grid.includes('>60.0%<'), 'the row total counts only buckets with checks');
  assert(grid.includes('margin-left:0.000%;width:100.000%'), 'no window given: the buckets fill the row');
  // The last hour up to the server's clock, as the history charts draw it; the
  // buckets end on the next round minute, so they begin a minute into it.
  context.dashboardConfig.http_targets = [{ name: '<pages>', url: 'http://pages.test/<x>' }];
  const hour = context.servicesGridHTML({ start: 660, bucket_sec: 60, count: 60, now: 4200, truncated: false,
    targets: { '<pages>': Array(60).fill([1, 0]) } }, 1, 1000);
  assert(hour.includes('margin-left:1.667%;width:100.000%'));
  assert(hour.includes('its one check passed') && !hour.includes('1 checks'));
  assert(hour.includes('<a class="svc-label" href="http://pages.test/&lt;x>"') &&
    hour.includes('title="http://pages.test/&lt;x>">&lt;pages></a>'), 'the name links to its address');
  assert(grid.includes('<span class="svc-label">pages</span>'), 'no address known: plain text');
  assert(hour.includes('grid-template-columns:84px '));
  const ticks = hour.match(/<span style="left:[0-9.]+%">\d\d:\d\d<\/span>/g) || [];
  assert(ticks.length >= 5, 'the charts\' time ticks under the cells');
  assert(context.servicesGridHTML({ targets: {} }).includes('No services configured'));
}

// The error branch (state.json unreadable) still shows the server's own findings.
function testHardwareErrorKeepsServerFindings() {
  const context = liveContext(['hardware-content', 'hardware-value', 'snapshot-age']);
  context.Date = { now: () => 100000000 };
  context.escapeHtml = String;
  context.hwHint = () => '';
  const critical = [];
  context.updateCardCritical = (id, on) => critical.push(on);
  context.document.querySelector = () => context.elements.get('snapshot-age');
  evaluate(context, between(app, 'function renderHardware', '// Power bar segment colours'));
  const fanAlarm = 'fan f71889ed fan1: 0 RPM, below its 300 RPM minimum';
  context.renderHardware({ status: 'error', detail: 'ts missing', sensor_alarms: [fanAlarm], sensor_notes: ['a hint'] });
  const html = context.elements.get('hardware-content').innerHTML;
  assert(html.includes(fanAlarm), 'a stopped fan disappears when state.json is unreadable');
  assert(html.includes('a hint'));
  assert.equal(context.elements.get('hardware-value').textContent, '1 issue');
  assert.equal(critical.at(-1), true);
}

function testHardwareIndependentSources() {
  const context = liveContext(['hardware-content', 'hardware-value', 'snapshot-age']);
  const now = Date.UTC(2026, 10, 24, 8), ts = now / 1000;
  context.Date = class extends Date { static now() { return now; } };
  context.escapeHtml = String;
  context.hwHint = () => '';
  context.updateCardCritical = (id, on) => { context.statuses.critical = on; };
  context.updateCardWarning = (id, count) => { context.statuses.warning = count > 0; };
  context.document.querySelector = () => context.elements.get('snapshot-age');
  evaluate(context, between(app, 'function renderHardware', '// Power bar segment colours') +
    between(app, 'function backupService', '// The sensors snapshot'));
  const item = (key, text) => ({ key, text, since: ts - 3600 });
  const held = (key, text) => ({ ...item(key, text), unchecked_since: ts - 120 });
  const snapshot = {
    status: 'ok', age_sec: 5,
    alarms: ['GPU ECC errors', 'collector driver mismatch'],
    alarm_items: [item('gpu/0/ecc', 'GPU ECC errors'), item('gpu/driver-mismatch', 'collector driver mismatch')],
    alarm_held: [held('bmc/fan/chassis', 'Chassis fan fault')], info: ['Collector diagnostic note'],
  };
  context.dataCache.sensors = { lastUpdate: now, data: snapshot };
  context.dataCache.alarms = { lastUpdate: now, data: { recent: [
    { event: 'cleared', timestamp: new Date(now - 60000).toISOString(), text: 'Previous fault', active_for_s: 60 },
  ] } };
  const server = {
    sensor_alarms: ['Host driver mismatch: reboot fixes it', 'CPU fan stopped'],
    sensor_alarm_items: [item('gpu/driver-mismatch', 'Host driver mismatch: reboot fixes it'), item('fan/cpu', 'CPU fan stopped')],
    sensor_alarm_held: [held('fan/case', 'Case fan fault')], sensor_notes: ['Host diagnostic note'],
  };
  const html = () => context.elements.get('hardware-content').innerHTML;
  const header = () => context.elements.get('hardware-value').textContent;
  for (const status of ['ok', 'missing', 'error', 'stale']) {
    context.renderHardware({ ...server, status, age_sec: 30, detail: 'invalid timestamp',
      edac: { uptime_seconds: 3600, ce_total: 0, ue_total: 0 },
      sel_memory: { count: 0, recent: [] }, sel_hardware: { count: 0, recent: [] },
      backup: { age_hours: 2, timer: 'backup.timer' } });
    assert.equal(header(), '3 issues', `${status}: independent alarms stay counted`);
    for (const text of ['GPU ECC errors', 'Host driver mismatch', 'CPU fan stopped', 'Case fan fault',
      'Chassis fan fault', 'Collector diagnostic note', 'Host diagnostic note', 'Alarm history', 'Previous fault', '(1 h)']) {
      assert(html().includes(text), `${status}: ${text}`);
    }
    assert(!html().includes('collector driver mismatch'), 'only the duplicate mismatch diagnosis is suppressed');
    assert(!html().includes('no alarms'));
    assert(context.statuses.critical);
    if (status === 'missing') assert(html().includes('No health snapshot yet'));
    if (status === 'error') assert(html().includes('Cannot read') && html().includes('invalid timestamp'));
    const stateKnown = status === 'ok' || status === 'stale';
    for (const text of ['ECC since boot', 'BMC log', 'last run 2 h ago', 'ras-mc-ctl', 'ipmitool', 'backup.service']) {
      assert.equal(html().includes(text), stateKnown, `${status}: collector detail ${text}`);
    }
  }
  context.renderHardware({ status: 'missing' });
  assert.equal(header(), '2 issues');
  assert(html().includes('collector driver mismatch'), 'the collector diagnosis survives when the host has none');
  context.renderHardware({ status: 'ok', age_sec: 0, sensor_alarms: ['GPU ECC errors'],
    sensor_alarm_items: [item('unrelated/key', 'GPU ECC errors')] });
  assert.equal(header(), '3 issues', 'matching text is not a reason to merge distinct diagnoses');

  snapshot.alarms = []; snapshot.alarm_items = []; snapshot.alarm_held = [];
  context.renderHardware({ status: 'error', sensor_alarm_held: server.sensor_alarm_held });
  assert.equal(header(), '1 unchecked');
  assert(html().includes('Case fan fault') && html().includes('Not re-checked'));
  for (const sensor_status of ['pending', 'stale']) {
    context.renderHardware({ status: 'ok', age_sec: 0, sensor_status });
    assert.equal(header(), `checks ${sensor_status}`);
    assert(context.statuses.warning && !html().includes('no alarms'));
  }
  context.renderHardware({ status: 'error' });
  assert.equal(header(), 'snapshot unavailable');
  assert(context.statuses.warning && !context.statuses.critical);
  context.renderHardware({ status: 'ok', age_sec: 0, sensor_status: 'ok' });
  assert.equal(header(), 'all clear');
  assert(!context.statuses.warning && !context.statuses.critical);
  snapshot.status = 'stale';
  context.renderHardware({ status: 'ok', age_sec: 0 });
  assert(!html().includes('Collector diagnostic note'), 'old sensor notes are not presented as current');
  snapshot.status = 'missing'; snapshot.expected = false;
  context.renderHardware({ status: 'missing', expected: false, sensor_status: 'ok' });
  assert.equal(header(), 'no detected issues');
  assert(!context.statuses.warning && !context.statuses.critical);
  assert(html().includes('Hardware collector not configured') && html().includes('Sensors collector not configured'));
  assert(!html().includes('no alarms') && !html().includes('text-warning'));
  context.renderHardware({ status: 'missing', expected: true, sensor_status: 'ok' });
  assert.equal(header(), 'snapshot unavailable');
  assert(context.statuses.warning, 'expected-but-missing remains a warning');
  context.renderHardware({ status: 'error', expected: false });
  assert(context.statuses.warning, 'configuration cannot make corrupt data healthy');
  snapshot.status = 'stale';
  context.renderHardware({ status: 'missing', expected: false });
  assert(context.statuses.warning, 'stale sensors still warn even with expected:false');
  snapshot.status = 'missing';
  context.renderHardware({ status: 'missing', expected: false, sensor_alarm_held: server.sensor_alarm_held });
  assert.equal(header(), '1 unchecked');
  assert(context.statuses.warning, 'optional coverage cannot hide a retained alarm');

  snapshot.status = 'ok';
  snapshot.alarm_held = [held('bmc/fan/removed', 'Removed chassis fan')];
  context.dataCache.alarms.data.retirement = { supported: true, password_configured: true };
  const selected = [];
  context.openAlarmRetirement = alarm => selected.push(alarm);
  const retiredSources = { status: 'ok', age_sec: 0,
    alarm_held: [held('smart/removed/health', 'Removed drive')], sensor_alarm_held: server.sensor_alarm_held };
  context.renderHardware(retiredSources);
  assert.equal((html().match(/data-retire-alarm=/g) || []).length, 3, 'only retained faults get retirement controls');
  for (let i = 0; i < 3; i++) context.elements.get('hardware-content').onclick({
    target: { closest: selector => selector === '[data-retire-alarm]' ? ({ dataset: { retireAlarm: String(i) }, disabled: false }) : null },
  });
  assert.deepEqual(selected.map(h => h.source), ['state', 'sensors', 'dashboard']);
  assert(selected.every(h => h.since === ts - 3600 && h.unchecked_since === ts - 120));
  context.dataCache.alarms.data.retirement.password_configured = false;
  context.renderHardware(retiredSources);
  assert(html().includes('disabled') && html().includes('Configure the settings password'));
  context.dataCache.alarms.data.retirement.supported = false;
  context.renderHardware(retiredSources);
  assert(!html().includes('data-retire-alarm='), 'older backends do not offer an unsupported operation');
  assert(context.hwEventText({ event: 'retired', text: 'Removed drive', timestamp: new Date(now).toISOString() })
    .includes('retired by operator: Removed drive'));
  snapshot.alarms = []; snapshot.alarm_items = []; snapshot.alarm_held = [];
  const reviewed = { source: 'state', key: 'sel/memory', text: 'Investigated memory event', since: ts,
    reviewed: { timestamp: ts, note: 'Replaced DIMM' } };
  context.renderHardware({ status: 'ok', age_sec: 0, alarms: [], alarm_reviewed: [reviewed] });
  assert.equal(header(), '1 reviewed');
  assert(!context.elements.get('hardware-value').className.includes('text-success'));
  assert(!html().includes('no alarms') && html().includes('No unreviewed issues'));
  assert(!context.statuses.critical && !context.statuses.warning, 'reviewed evidence is neutral, not recovered or critical');
}

async function testPowerHistoryPerRecord() {
  const context = liveContext([]);
  Object.assign(context, {
    HISTORY_METRICS: { power: { perDevice: true, unit: ' W' }, fans: { perDevice: true, unit: ' RPM' }, temps: { perDevice: true, unit: '°C' } },
    CHART_TICK: {}, CHART_GRID: {}, CHART_FONT_SIZE: 16, HISTORY_AXIS_WIDTH: 84, HISTORY_PLOT_INSET: 8,
    axisNumber: v => v,
    Chart: class {
      constructor(canvas, chart) { Object.assign(this, chart); }
      isDatasetVisible(i) { return !this.data.datasets[i].hidden; }
      setDatasetVisibility(i, visible) { this.data.datasets[i].hidden = !visible; }
      destroy() {}
      update() {}
    },
  });
  evaluate(context, between(config, 'const CORE_TEMP_LABEL', 'function formatTimeAgo') +
    between(app, 'function extractHistoryValue', '// Create a history card for a metric'));
  const record = power => ({ timestamp: '2026-11-24T12:00:00Z', power });
  const plot = records => context.createHistoryChart('canvas', 'power', records, 'percent', 24, 'stack');
  const column = (chart, index) => plain(Object.fromEntries(chart.data.datasets
    .filter(d => d.data[index] != null).map(d => [d.label, d.data[index]])));
  const old = { ...record({ 'Pwr Consumption': 200, 'System (ACPI)': 200, 'CPU 0': 50 }), power_total: 'Pwr Consumption' };
  assert.deepEqual(column(plot([old]), 0), { 'CPU 0': 50, other: 150 }, 'old records do not stack two totals');
  const legacyPsu = { ...old, power: { ...old.power, 'PS1 Input Power': 100, 'PS2 Input Power': 100 } };
  assert.deepEqual(column(plot([legacyPsu]), 0), { 'CPU 0': 50, other: 150 }, 'legacy PSU inputs are not additional components');
  const partialPsu = record({ 'PS1 Input Power': 100, 'PS2 Input Power': null, 'CPU 0': 50 });
  assert.deepEqual(column(plot([partialPsu]), 0), { 'CPU 0': 50 }, 'an unknown PSU never becomes zero or a guessed whole-machine total');
  const acpi = record({ 'System (ACPI)': 200, 'CPU 0': 50 });
  assert.deepEqual(column(plot([acpi, old]), 1), column(plot([old]), 0), 'a wider range cannot change a record');
  assert.deepEqual(column(plot([acpi]), 0), { 'CPU 0': 50, other: 150 });
  const custom = { ...record({ meter: 200, 'CPU 0': 50, auxiliary: 5 }), power_total: 'meter', power_totals: ['meter'] };
  const parts = record({ meter: 20, 'CPU 0': 50, auxiliary: 5 });
  const mixed = plot([custom, parts]);
  assert.deepEqual(column(mixed, 0), { 'CPU 0': 50, auxiliary: 5, other: 145 });
  assert.deepEqual(column(mixed, 1), { 'CPU 0': 50, meter: 20, auxiliary: 5 }, 'a total in another record cannot erase a component');
  assert.deepEqual(column(mixed, 1), column(plot([parts]), 0));
  const items = i => mixed.data.datasets.filter(d => d.data[i] != null)
    .map(d => ({ dataIndex: i, parsed: { y: d.data[i] } }));
  assert.equal(mixed.options.plugins.tooltip.callbacks.footer(items(0)), 'total 200 W');
  assert.equal(mixed.options.plugins.tooltip.callbacks.footer(items(1)), '≥ 75 W');
  const named = record({ 'System Power': 200, 'Power Meter': 200, 'CPU 0': 50 });
  assert.deepEqual(column(plot([named]), 0), { 'CPU 0': 50, other: 150 });
  const noTotal = { ...named, power_totals: [] };
  assert.deepEqual(column(plot([noTotal]), 0), { 'CPU 0': 50, 'System Power': 200, 'Power Meter': 200 }, 'explicit metadata takes precedence over legacy guesses');
  assert.deepEqual(column(plot([{ timestamp: old.timestamp }, old]), 0), {}, 'missing records remain gaps');

  const fanId = '@pci0000:00/0000:01:00.0/amdgpu/fan1';
  const fans = context.createHistoryChart('canvas', 'fans', [
    { timestamp: old.timestamp, fans: { [fanId]: 1200 }, gpu_fans: { 'GPU 1 fan': 25 } },
  ], 'percent', 24, 'range');
  assert.equal(fans.data.datasets.find(d => d.label === 'Fans median').data[0], 1200, 'GPU percent never enters the RPM range');
  assert.equal(fans.data.datasets.find(d => d.label === 'GPU 1 fan').yAxisID, 'y1');
  const individual = context.createHistoryChart('canvas', 'fans', [
    { timestamp: old.timestamp, fans: { [fanId]: 1200, 'legacy fan': 800 } },
  ]);
  assert.deepEqual(plain(individual.data.datasets.map(d => d.label)), ['amdgpu fan1 (0000:01:00.0)', 'legacy fan']);
  const physical = 'Composite [pci0000:00/0000:01:00.0/temp1]';
  const temperatures = records => context.createHistoryChart('canvas', 'temps', records);
  const current = { timestamp: old.timestamp, temps: { [physical]: 43 } };
  assert.equal(temperatures([current]).data.datasets[0].label, 'Composite', 'a lone physical series has a short legend');
  const mixedTemps = temperatures([{ timestamp: old.timestamp, temps: { Composite: 40 } }, current]);
  assert.deepEqual(plain(mixedTemps.data.datasets.map(d => d.label)), ['Composite', 'Max', 'Composite [temp1]']);
  assert.deepEqual(plain(mixedTemps.data.datasets.map(d => d.data)), [[40, null], [40, 43], [null, 43]], 'display polish never joins old and new identities');

  // A range change removes the naming collision and an alias changes the name
  // again. Neither may transfer the user's hidden selection to the other key.
  mixedTemps.setDatasetVisibility(2, false);
  const box = { innerHTML: '' };
  const entry = { chart: mixedTemps, loadSeq: 0,
    card: { dataset: { historyId: 'temps' }, querySelector: () => box } };
  context.historyCards = new Map([['temps', entry]]);
  evaluate(context, between(app, 'async function loadHistoryData', '// Remove a history card'));
  await context.loadHistoryData('temps', 1, { ok: true, records: [current] });
  let index = entry.chart.data.datasets.findIndex(d => d.sensorKey === physical);
  assert.equal(entry.chart.data.datasets[index].label, 'Composite');
  assert(!entry.chart.isDatasetVisible(index), 'hidden state survives a changed display name');
  context.dashboardConfig.sensor_labels = { [physical]: 'Drive temperature' };
  await context.loadHistoryData('temps', 24, { ok: true, records: [current,
    { timestamp: old.timestamp, temps: { Composite: 40 } }] });
  index = entry.chart.data.datasets.findIndex(d => d.sensorKey === physical);
  assert.equal(entry.chart.data.datasets[index].label, 'Drive temperature');
  assert(!entry.chart.isDatasetVisible(index), 'an alias does not reset hidden state');
  assert(entry.chart.isDatasetVisible(entry.chart.data.datasets.findIndex(d => d.sensorKey === 'Composite')),
    'the separate legacy series stays visible');
}

async function testHistoryCapabilities() {
  const calls = [];
  const context = vm.createContext({ getCssColor: () => '#123456', dashboardConfig: {},
    historyCards: new Map(), createHistoryCard: metric => calls.push(metric) });
  evaluate(context, between(config, 'const HISTORY_METRICS =', '// Track active history cards') +
    between(config, '// Endpoint discovery:', '// Helper to fetch with error tracking') +
    between(app, 'function showAllHistory', '// Hide all history cards') +
    between(app, 'function historyErrorHTML', '// Extract metric value'));
  const metrics = ['cpu', 'cpu_cores', 'memory', 'network', 'temps', 'load', 'swap', 'drive-usage', 'ping'];
  const endpoints = ['/history', '/history/field/temps', '/4/cpu', '/4/percpu', '/4/mem', '/4/network',
    '/4/sensors', '/4/load', '/4/memswap', '/4/fs', '/ping'];
  context.safeFetch = async () => ({ ok: true, data: { endpoints, history_metrics: metrics } });
  await context.fetchEndpointList();
  context.showAllHistory();
  assert.deepEqual(calls, metrics, 'Show All follows the backend inventory, including derived metrics');
  assert(!context.historyMetricAvailable('cpu_freq') && !context.historyMetricAvailable('processes'));
  const unavailable = context.historyErrorHTML('unavailable');
  assert(unavailable.includes('does not provide') && !unavailable.includes('Could not reach') && !unavailable.includes('retry'));
  // Older endpoint discovery responses have no history inventory. Supported
  // live and side-field endpoints still prevent unsupported requests.
  context.safeFetch = async () => ({ ok: true, data: { endpoints } });
  await context.fetchEndpointList();
  assert(context.historyMetricAvailable('temps'));
  assert(!context.historyMetricAvailable('gpu') && !context.historyMetricAvailable('processes'));
  context.safeFetch = async () => ({ ok: true, data: { endpoints: [...endpoints, '/alarms/history', '/alarms'] } });
  await context.fetchEndpointList();
  assert(context.historyMetricAvailable('alarms'), 'alarm history does not require configured services');
  assert(!context.historyMetricAvailable('services'));
}

async function testAlarmHistory() {
  const context = liveContext([]);
  context.escapeHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  evaluate(context, between(app, 'function renderHardware', '// Power bar segment colours'));
  Object.assign(context, { HISTORY_AXIS_WIDTH: 84, HISTORY_PLOT_INSET: 8,
    historyTicks: () => [{ value: 1000000, label: 'start' }, { value: 2000000, label: 'end' }] });
  const stamp = epoch => new Date(epoch * 1000).toISOString();
  const event = (type, at, extra = {}) => ({ source: 'dashboard', key: 'fan/cpu', text: 'CPU fan <stopped>', since: 900,
    event: type, timestamp: stamp(at), ...extra });
  const entry = { alarmHistory: { cutoff: 1000, now: 2000, truncated: true, skipped: 2, events: [
    event('cleared', 1300, { active_for_s: 400 }),
    event('raised', 1400, { since: 1400 }),
    event('cleared', 1600, { since: 1400, active_for_s: 200 }),
    event('retired', 1700, { key: 'fan/old', note: '<script>replaced fan</script>' }),
    event('expired', 1800, { key: 'fan/expired' }),
    event('cleared', 1900, { source: 'sensors', after_boot: true }),
    event('raised', 1950, { key: 'fan/initial', initial: true }),
    event('reviewed', 1550, { source: 'state', key: 'journal/error', evidence: ['old'], note: 'Investigated' }),
    event('reopened', 1650, { source: 'state', key: 'journal/error', evidence: ['old', 'new'] }),
    { source: 'dashboard', event: 'gap', timestamp: stamp(1200), from: stamp(1000), to: stamp(1200) },
  ] }, alarmCurrent: {
    now: 1990,
    sources: { state: { status: 'ok' }, sensors: { status: 'error' } },
    active: [{ source: 'state', key: 'journal/error', text: 'Journal issue', since: 900, evidence: ['old', 'new'] },
      { source: 'dashboard', key: 'fan/longstanding', text: 'Longstanding fan fault', since: 500 }],
    held: [{ source: 'dashboard', key: 'fan/removed', text: 'Removed fan', since: 600, unchecked_since: 800 }],
    reviewed: [{ source: 'state', key: 'sel/memory', text: 'Reviewed SEL', since: 700, reviewed: { note: 'Replaced DIMM' } }],
    review: { supported: true, password_configured: true }, retirement: { supported: true, password_configured: true },
  } };
  let html = context.alarmHistoryHTML(entry, 800);
  const snapshotNote = `As of ${context.alarmTime(1990)}. Refresh to update.`;
  assert(html.includes(snapshotNote), 'concerns show their own fetch time, not the history range end');
  assert(html.includes('First observed') && html.includes('before this range'));
  assert(html.includes('Longstanding fan fault') && html.includes('Removed fan'));
  assert(html.includes('History is incomplete') && html.includes('2 unreadable'));
  assert(html.includes('Checks unavailable or stale: sensors'));
  assert(html.includes('Monitoring events (1)'));
  assert.equal(context.alarmEventLabel({ event: 'changed', reason: 'new_evidence' }), 'New evidence');
  assert.equal(context.alarmEventLabel({ event: 'reopened', reason: 'evidence_unavailable' }), 'Evidence unavailable; review no longer applies');
  assert(context.hwEventText(event('reopened', 1600, { reason: 'evidence_unavailable' })).includes('evidence unavailable; review no longer applies'));
  assert(!context.hwEventText(event('changed', 1600, { reason: 'new_evidence' })).includes('? -> ?'));
  assert(!html.includes('<stopped>') && !html.includes('<script>'));
  const group = (source, key) => entry.alarmGroups.find(g => g.source === source && g.key === key);
  assert.equal(group('dashboard', 'fan/cpu').events.length, 3, 'repeated occurrences stay accessible in one lane');
  assert.equal(group('sensors', 'fan/cpu').events.length, 1, 'identical keys from separate sources do not merge');
  entry.alarmSelection = group('dashboard', 'fan/cpu').id;
  html = context.alarmHistoryHTML(entry, 800);
  assert.equal((html.match(/<strong>Cleared<\/strong>/g) || []).length, 2);
  assert(html.includes('Observed interval: 6 min 40 s'));
  assert(!html.includes('History only; current state unconfirmed.'));
  assert(!html.includes('Markers are recorded observations') && !html.includes('<small>dashboard</small>'));
  assert(html.includes('<details class="alarm-event"') && html.includes('<div class="alarm-event-meta">'),
    'event readings and occurrence metadata remain available behind each event summary');
  assert.equal(context.alarmInterval(0), '0 s');
  assert.equal(context.alarmInterval(3600), '1 h');
  assert.equal(context.alarmInterval(90061), '1 d 1 h 1 min 1 s');
  const selected = entry.alarmSelection;
  context.alarmHistoryHTML(entry, 800);
  assert.equal(entry.alarmSelection, selected, 'refresh preserves selected identity');
  entry.alarmSelection = group('dashboard', 'fan/old').id;
  html = context.alarmHistoryHTML(entry, 800);
  assert(html.includes('<strong>Closed by operator</strong>') && html.includes('&lt;script>replaced fan&lt;/script>'));
  assert(!html.includes('data-alarm-action='), 'historical closure has no active action');
  entry.alarmSelection = group('dashboard', 'fan/expired').id;
  assert(context.alarmHistoryHTML(entry, 800).includes('<strong>Expired without re-check</strong>'));
  entry.alarmSelection = group('sensors', 'fan/cpu').id;
  assert(context.alarmHistoryHTML(entry, 800).includes('<strong>Cleared after reboot</strong>'));
  entry.alarmSelection = group('state', 'journal/error').id;
  html = context.alarmHistoryHTML(entry, 800);
  assert(html.includes('data-alarm-action="review"') && html.includes('New evidence'));
  entry.alarmCurrentStale = true;
  html = context.alarmHistoryHTML(entry, 800);
  assert(html.includes('data-alarm-action="review" disabled'));
  assert(html.includes(snapshotNote), 'a failed refresh keeps the original snapshot time');
  entry.alarmCurrentStale = false;
  entry.alarmSelection = group('dashboard', 'fan/removed').id;
  assert(context.alarmHistoryHTML(entry, 800).includes('data-alarm-action="retire"'));
  entry.alarmHistory.events = [];
  html = context.alarmHistoryHTML(entry, 800);
  assert(html.includes('No alarm events recorded in this range') && html.includes('Longstanding fan fault'));
  entry.alarmCurrent = {};
  html = context.alarmHistoryHTML(entry, 800);
  assert(html.includes('Snapshot time unavailable. Refresh to update.'), 'missing timestamps are not replaced by the current clock');
  assert(html.includes('No concerns reported in this snapshot.'));
  assert.equal(entry.alarmSelection, null, 'selection clears when no longer present');

  let handler, recordings = 0;
  const opened = [], closed = [], cards = new Map();
  const clickContext = vm.createContext({ document: { querySelector: () => ({ addEventListener: (_, cb) => { handler = cb; } }) },
    historyCards: cards, fetch: () => { recordings++; return Promise.resolve(); },
    createHistoryCard: metric => { opened.push(metric); cards.set(metric, {}); },
    removeHistoryCard: metric => { closed.push(metric); cards.delete(metric); } });
  evaluate(clickContext, between(app, '// Set up history button handlers', '// Set up global history controls'));
  // Match the actual rendered attributes: an unconditional closest() stub hid
  // the text button's mismatch with the old class-based delegation selector.
  const clickButton = markup => {
    const attrs = Object.fromEntries([...markup.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
    const button = { dataset: { historyMetric: attrs['data-history-metric'] } };
    handler({ target: { closest: selector => {
      const match = selector.match(/^(\w+)?(?:\.([\w-]+))?(?:\[([\w-]+)\])?$/);
      assert(match, `unsupported button selector: ${selector}`);
      const [, tag, cls, attr] = match;
      return (!tag || tag === 'button') && (!cls || (attrs.class || '').split(/\s+/).includes(cls)) &&
        (!attr || Object.hasOwn(attrs, attr)) ? button : null;
    } } });
  };
  context.historyMetricAvailable = () => true;
  const textButton = context.hardwareAlarmHistoryHTML().match(/<button[^>]*>Alarm history<\/button>/)[0];
  clickButton(textButton);
  assert.deepEqual(opened, ['alarms'], 'the rendered Hardware history button opens its card');
  clickButton(textButton);
  assert.deepEqual(closed, ['alarms'], 'clicking again closes its card');
  const index = fs.readFileSync(path.join(__dirname, '..', '..', 'index.html'), 'utf8');
  clickButton(index.match(/<button[^>]*data-history-metric="alarms"[^>]*>/)[0]);
  assert.deepEqual(opened, ['alarms', 'alarms'], 'the header H button still opens alarm history');
  assert.equal(recordings, 0, 'neither alarm history button records a metric sample');
  clickButton(index.match(/<button[^>]*data-history-metric="cpu"[^>]*>/)[0]);
  assert.equal(opened.at(-1), 'cpu');
  assert.equal(recordings, 1, 'numeric history keeps its record-on-open behavior');
}

async function testAlarmHistoryRefresh() {
  const loads = [], rendered = [];
  const box = { innerHTML: '', clientWidth: 800 };
  const entry = { loadSeq: 0, card: { querySelector: () => box } };
  const context = vm.createContext({ historyCards: new Map([['alarms', entry]]),
    HISTORY_METRICS: { alarms: { grid: '/api/alarms/history' } },
    safeFetch: url => new Promise(resolve => loads.push({ url, resolve })),
    renderAlarmHistory: entry => rendered.push(entry.alarmHistory.now), historyErrorHTML: error => error });
  evaluate(context, between(app, 'async function loadHistoryData', '// Remove a history card'));
  const old = context.loadHistoryData('alarms', 24), newer = context.loadHistoryData('alarms', 1);
  loads[2].resolve({ ok: true, data: { cutoff: 100, now: 200, events: [] } });
  loads[3].resolve({ ok: true, data: { now: 190, active: [] } });
  await newer;
  loads[0].resolve({ ok: true, data: { cutoff: 0, now: 100, events: [] } });
  loads[1].resolve({ ok: true, data: { now: 90, active: [{ text: 'old' }] } });
  await old;
  assert.deepEqual(rendered, [200], 'older responses never replace newer history');
  assert.equal(entry.alarmCurrent.now, 190, 'older responses never replace the concern snapshot time');
  const failure = context.loadHistoryData('alarms', 1);
  loads[4].resolve({ ok: false, errorType: 'network' }); loads[5].resolve({ ok: false });
  await failure;
  assert.equal(entry.alarmHistory.now, 200);
  assert.equal(entry.alarmCurrent.now, 190, 'failed fetches do not advance the concern snapshot time');
  assert(entry.alarmHistoryError && entry.alarmCurrentStale, 'last good history remains with stale current actions disabled');
  const closed = context.loadHistoryData('alarms', 1);
  context.historyCards.delete('alarms');
  loads[6].resolve({ ok: true, data: { now: 300 } }); loads[7].resolve({ ok: true, data: {} });
  await closed;
  assert.deepEqual(rendered, [200, 200], 'closed card is not resurrected by its response');
}

function testAlarmHistoryFocus() {
  const context = liveContext([]);
  evaluate(context, between(app, 'function renderAlarmHistory', '// Shared by the Hardware health'));
  let focusedIndex, currentRow = false;
  let identity = { open: true }, legend = { open: true };
  let event = { open: true, dataset: { alarmEvent: 'same-occurrence' } };
  const groups = [{ id: 'first' }, { id: 'second' }];
  const box = {
    clientWidth: 600,
    set innerHTML(value) { identity = { open: false }; legend = { open: false }; event = { open: false, dataset: { alarmEvent: 'same-occurrence' } }; },
    querySelectorAll(selector) { return selector === '[data-alarm-event]' || selector === '[data-alarm-event][open]' && event.open ? [event] : []; },
    querySelector(selector) {
      if (selector === '[data-alarm-identity]') return identity;
      if (selector === '[data-alarm-legend]') return legend;
      if (selector === '[data-alarm-lane]:focus') return {
        dataset: { alarmLane: '0' }, classList: { contains: () => !currentRow },
      };
      const match = selector.match(/^\.(alarm-lane|alarm-current-row)\[data-alarm-lane="(\d+)"\]$/);
      return match ? { focus: () => { focusedIndex = [match[1], Number(match[2])]; } } : null;
    },
  };
  Object.assign(context, { HISTORY_AXIS_WIDTH: 84, HISTORY_PLOT_INSET: 8,
    alarmHistoryHTML: entry => { entry.alarmGroups = [...groups].reverse(); return ''; } });
  const entry = { card: { querySelector: () => box }, alarmGroups: groups };
  context.renderAlarmHistory(entry);
  assert.deepEqual(focusedIndex, ['alarm-lane', 1], 'refresh follows focused identity when groups reorder');
  assert.equal(identity.open, true, 'refresh keeps source and identity expanded');
  assert.equal(legend.open, true, 'refresh keeps the symbol key expanded');
  assert.equal(event.open, true, 'refresh keeps the selected event details expanded');
  identity.open = false;
  legend.open = false;
  event.open = false;
  currentRow = true;
  entry.alarmGroups = groups;
  context.renderAlarmHistory(entry);
  assert.deepEqual(focusedIndex, ['alarm-current-row', 1], 'current concern buttons retain keyboard focus too');
  assert.equal(identity.open, false, 'refresh also preserves a collapsed disclosure');
  assert.equal(legend.open, false);
  assert.equal(event.open, false);
}

async function testAlarmRetirement() {
  const h = historySettingsContext();
  const refreshed = [], rendered = [], cancelledTimers = [];
  Object.assign(h.context, { AbortController,
    clearTimeout: timer => cancelledTimers.push(timer),
    updateCache: (key, data) => refreshed.push([key, data]),
    renderHardware: data => rendered.push(data),
  });
  evaluate(h.context, between(app, 'let alarmRetirementPopup =', '// One line of the event log'));
  const current = () => evaluate(h.context, 'alarmRetirementPopup');
  const alarm = { source: 'dashboard', key: 'fan/@platform/removed/chip/fan1', since: 1000,
    unchecked_since: 2000, text: 'Removed fan <name>' };
  let restoredFocus = 0;
  h.context.document.activeElement = { isConnected: true, focus: () => { restoredFocus++; } };
  h.context.openAlarmRetirement(alarm);
  let popup = current();
  const field = id => popup.querySelector('#ar-' + id);
  assert.equal(field('alarm').textContent, alarm.text, 'alarm text is inserted as text, not markup');
  await h.context.submitAlarmRetirement(popup);
  assert.equal(h.writes.length, 0, 'a password is required before any request');
  field('password').value = 'wrong';
  let pending = h.context.submitAlarmRetirement(popup);
  await h.context.submitAlarmRetirement(popup);
  assert.equal(h.writes.length, 1, 'an in-flight submission cannot be duplicated');
  assert.equal(h.writes[0].url, '/api/alarms/retire');
  assert.equal(h.timers.at(-1).delay, 30000, 'retirement allows an in-flight server health check to finish');
  assert.deepEqual(h.writes[0].body, { source: alarm.source, key: alarm.key, since: 1000, unchecked_since: 2000, password: 'wrong' });
  h.finishWrite(0, { error: 'Incorrect password' }, false, 401);
  await pending;
  assert.equal(field('status').textContent, 'Incorrect password');
  assert(!field('confirm').disabled);
  assert.equal(h.reads.length, 0, 'failed authentication does not claim a mutation or erase the row');

  // A stale dialog cannot retire a different occurrence of the same key.
  field('password').value = 'test';
  pending = h.context.submitAlarmRetirement(popup);
  h.finishWrite(1, { error: 'Alarm changed' }, false, 409);
  await new Promise(setImmediate);
  assert.deepEqual(h.reads.map(r => r.url), ['/api/alarms', '/api/sensors', '/api/storage-health']);
  await h.finishRead(0, { held: [] });
  await h.finishRead(1, { status: 'ok', alarm_held: [] });
  await h.finishRead(2, { status: 'ok', sensor_alarm_held: [], sensor_alarms: ['Fan failed again'] });
  await pending;
  assert(field('status').textContent.includes('alarm changed'));
  assert(field('confirm').disabled && field('password').value === '', 'a conflict needs a fresh selection');
  assert.equal(rendered.at(-1).sensor_alarms[0], 'Fan failed again');
  field('cancel').click();
  assert.equal(current(), null);
  assert.equal(restoredFocus, 1, 'cancel restores focus to the action that opened the dialog');

  h.context.openAlarmRetirement(alarm);
  popup = current(); field('password').value = 'test'; field('note').value = 'Replaced the fan';
  pending = h.context.submitAlarmRetirement(popup);
  assert.equal(h.writes[2].body.note, 'Replaced the fan');
  h.finishWrite(2, { status: 'retired', source: alarm.source, key: alarm.key });
  await new Promise(setImmediate);
  await h.finishRead(3, { held: [], recent: [{ event: 'retired', text: alarm.text }] });
  await h.finishRead(4, { status: 'ok', alarm_held: [] });
  await h.finishRead(5, { status: 'ok', sensor_alarm_held: [] });
  await pending;
  assert.equal(current(), null);
  assert.equal(field('password').value, '', 'the password is cleared on success');
  assert.deepEqual(refreshed.slice(-3).map(([key]) => key), ['alarms', 'sensors', 'storageHealth']);
  assert.deepEqual(rendered.at(-1).sensor_alarm_held, []);
  assert.equal(cancelledTimers.length, 3, 'every completed attempt clears its deadline');

  const evidence = { source: 'state', key: 'journal/error', since: 100, text: 'Journal error', evidence: ['one', 'two'] };
  h.context.openAlarmReview(evidence);
  popup = current();
  await h.context.submitAlarmRetirement(popup);
  assert.equal(h.writes.length, 3, 'review also requires a password');
  field('password').value = 'test'; field('note').value = 'Investigated <error>';
  pending = h.context.submitAlarmRetirement(popup);
  assert.equal(h.writes[3].url, '/api/alarms/review');
  assert.deepEqual(h.writes[3].body, { source: 'state', key: 'journal/error', since: 100, evidence: ['one', 'two'], password: 'test', note: 'Investigated <error>' });
  h.finishWrite(3, { status: 'reviewed' });
  await new Promise(setImmediate);
  await h.finishRead(6, { active: [], reviewed: [evidence] });
  await h.finishRead(7, { status: 'ok' }); await h.finishRead(8, { status: 'ok' });
  await pending;
  assert.equal(current(), null);
}

// The Fans header never compares RPM with a GPU fan's percent.
function testFansHeader() {
  const context = liveContext([]);
  evaluate(context, between(app, '// The Fans card\'s header', '// Power bar segment colours'));
  assert.equal(context.fansHeader([{ value: 1657 }, { value: 883 }]), '1657 RPM max');
  assert.equal(context.fansHeader([{ value: 1657 }, { value: 28, unit: '%' }]), '1657 RPM max · GPU 28% max');
  assert.equal(context.fansHeader([{ value: 40, unit: '%' }]), 'GPU 40% max');
}

(async () => {
  for (const test of [testHardwareMemoryRows, testHardwareErrorKeepsServerFindings, testHardwareIndependentSources, testPowerHistoryPerRecord, testHistoryCapabilities, testAlarmHistory, testAlarmHistoryRefresh, testAlarmHistoryFocus, testAlarmRetirement, testFansHeader, testPowerFromHwmonAndTotals, testHardwareShowsServerSensorAlarms, testHistoryGapSkip, testHistoryAxes, testTempLabelsAndLimits, testServicesCard, testThemes,testFetchBodyTimeout, testStartupReadTimeouts, testIpInfoTimeout, testGpuRecovery, testGpuUnavailableReadings, testGpuInfoUnavailableReadings,
    testDriveTemperatureFreshness, testTempsWithoutSensors, testDriveUsageHidden, testSensorFreshness, testHardwareSince, testPausedStatus, testStartupPolling, testPingDisplay, testPingLoss,
    testStaticInfoRecovery, testSlowPollRenderIsolation, testCaptureCacheAge, testCapturePendingAndHiddenTab, testHistorySettingsLoading, testHistorySettingsPopupOwnership, testLiveSampleTimes,
    testVersionFooter]) {
    await test();
    console.log(`PASS ${test.name}`);
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
