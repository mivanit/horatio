const API_BASE = '/api/4';
const DEFAULT_HISTORY_LENGTH = 60;
const MIN_HISTORY_LENGTH = 10;
let historyLength = Math.max(
  parseInt(localStorage.getItem('historyLength')) || DEFAULT_HISTORY_LENGTH,
  MIN_HISTORY_LENGTH
);
const DEFAULT_POLL_INTERVAL = 2000;

// Dashboard config (loaded from config.json)
let dashboardConfig = {};

// Warning thresholds (loaded from config.json)
let thresholds = {
  cpu_percent: 50,
  memory_percent: 75,
  gpu_util_percent: 80,
  gpu_memory_percent: 80,
  temp_celsius: 60,
  network_mbps: 3,
  disk_percent: 80,
  disk_io_mbps: 50,
  swap_percent: 10,
  ping_ms: 100
};

// Read a CSS custom property value
function getCssColor(varName) {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

// Status colors (read from CSS variables)
const COLORS = {
  success: getCssColor('--color-success'),
  error: getCssColor('--color-error'),
  errorLight: getCssColor('--color-error-light'),
  warning: getCssColor('--color-warning'),
  partial: getCssColor('--color-partial'),
  muted: getCssColor('--text-muted'),
};

const SLOW_POLL_INTERVAL = 30000; // 30 seconds for static/slow-changing data
const DEFAULT_PROCESS_COUNT = 8;
let topProcessesLimit = parseInt(localStorage.getItem('topProcessesLimit')) || DEFAULT_PROCESS_COUNT;
const MIN_POLL_INTERVAL = 1000;
let pollInterval = Math.max(
  parseInt(localStorage.getItem('pollInterval')) || DEFAULT_POLL_INTERVAL,
  MIN_POLL_INTERVAL
);

const DEFAULT_NETHOGS_DURATION = 3;
let nethogsDuration = parseInt(localStorage.getItem('nethogsDuration')) || DEFAULT_NETHOGS_DURATION;

const DEFAULT_DISK_DURATION = 1;
let diskDuration = parseInt(localStorage.getItem('diskDuration')) || DEFAULT_DISK_DURATION;

// Calculate nethogs fetch timeout (matches server: 1.5x duration + 5s buffer)
function getNethogsTimeout() {
  return (nethogsDuration * 1.5 + 5) * 1000;
}

// Calculate disk processes fetch timeout (similar to nethogs)
function getDiskTimeout() {
  return (diskDuration * 1.5 + 5) * 1000;
}

// Escape HTML to prevent XSS
function escapeHtml(str) {
  if (str == null) return '';  // only null/undefined -> '' (so 0 / false still render)
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Connection state
let pollingInterval = null;
let slowPollingInterval = null;
let isConnected = true;
let disconnectTime = null;

// Per-card connection tracking (keys match data-card-id attributes)
const cardStatus = {
  'cpu': { ok: true, lastSuccess: Date.now() },
  'cpu-cores': { ok: true, lastSuccess: Date.now() },
  'cpu-cores-spread': { ok: true, lastSuccess: Date.now() },
  'mem': { ok: true, lastSuccess: Date.now() },
  'swap': { ok: true, lastSuccess: Date.now() },
  'gpu': { ok: true, lastSuccess: Date.now() },
  'gpu-mem': { ok: true, lastSuccess: Date.now() },
  'network': { ok: true, lastSuccess: Date.now() },
  'disk': { ok: true, lastSuccess: Date.now() },
  'temps': { ok: true, lastSuccess: Date.now() },
  'fans': { ok: true, lastSuccess: Date.now() },
  'power': { ok: true, lastSuccess: Date.now() },
  'load': { ok: true, lastSuccess: Date.now() },
  'processes': { ok: true, lastSuccess: Date.now() },
  'gpu-info': { ok: true, lastSuccess: Date.now() },
  'cpu-info': { ok: true, lastSuccess: Date.now() },
  'drive-usage': { ok: true, lastSuccess: Date.now() },
  'system-info': { ok: true, lastSuccess: Date.now() },
  'disk-drives': { ok: true, lastSuccess: Date.now() },
  'ping': { ok: true, lastSuccess: Date.now() },
  'ip-info': { ok: true, lastSuccess: Date.now() },
  'mail': { ok: true, lastSuccess: Date.now() },
  'hardware': { ok: true, lastSuccess: Date.now() },
  'drive-health': { ok: true, lastSuccess: Date.now() },
};

// Set Chart.js defaults
Chart.defaults.font.family = "'JetBrains Mono', 'Fira Code', 'SF Mono', 'Consolas', monospace";
Chart.defaults.plugins.tooltip.animation = { duration: 100 };

// Error toast handling
let errorTimeout = null;

function showError(message) {
  const toast = document.getElementById('error-toast');
  const status = document.getElementById('status');
  const messageEl = document.getElementById('error-message');
  messageEl.textContent = message;
  toast.classList.add('visible');
  status.classList.add('hidden');

  if (errorTimeout) clearTimeout(errorTimeout);
  errorTimeout = setTimeout(() => {
    toast.classList.remove('visible');
  }, 5000);
}

function hideError() {
  const toast = document.getElementById('error-toast');
  const status = document.getElementById('status');
  toast.classList.remove('visible');
  status.classList.remove('hidden');
  if (errorTimeout) {
    clearTimeout(errorTimeout);
    errorTimeout = null;
  }
}

// Update connection status based on card states
function updateConnectionStatus() {
  const statusEl = document.getElementById('status');
  const statusText = statusEl.querySelector('.status-text');
  const spinner = statusEl.querySelector('.spinner');
  const tooltip = document.getElementById('status-tooltip');

  const cards = Object.entries(cardStatus);
  const working = cards.filter(([, s]) => s.ok).length;
  const total = cards.length;

  if (working === 0) {
    statusText.textContent = 'Disconnected';
    statusText.style.color = COLORS.errorLight;
    spinner.style.borderTopColor = COLORS.errorLight;
  } else if (working < total) {
    statusText.textContent = `Partial (${working}/${total})`;
    statusText.style.color = COLORS.partial;
    spinner.style.borderTopColor = COLORS.partial;
  } else {
    statusText.textContent = 'Connected';
    statusText.style.color = COLORS.success;
    spinner.style.borderTopColor = COLORS.success;
  }

  // Update tooltip content
  if (tooltip) {
    const failing = cards.filter(([, s]) => !s.ok).map(([name]) => name);
    if (failing.length > 0) {
      tooltip.textContent = 'Failing: ' + failing.join(', ');
    } else {
      tooltip.textContent = '';
    }
  }
}

// Something is actually bad (failed drive, degraded array, an alarm): red box.
// Independent of no-data (fetch failure) and warning (threshold); red wins on screen.
function updateCardCritical(cardId, bad) {
  const card = document.querySelector(`[data-card-id="${cardId}"]`);
  if (!card) return;
  card.classList.toggle('critical', !!bad);
}

// Update card no-data state
function updateCardNoData(cardId, ok) {
  const card = document.querySelector(`[data-card-id="${cardId}"]`);
  if (!card) return;

  const status = cardStatus[cardId];
  if (!status) return;

  if (ok) {
    status.ok = true;
    status.lastSuccess = Date.now();
    card.classList.remove('no-data');
    const noDataEl = card.querySelector('.no-data-text');
    if (noDataEl) noDataEl.remove();
  } else {
    status.ok = false;
    card.classList.add('no-data');
    // .card.warning is declared after .card.no-data with equal specificity, so
    // a leftover warning would paint orange over red. Red wins.
    card.classList.remove('warning');

    // Update or create no-data text
    const secsSinceSuccess = Math.floor((Date.now() - status.lastSuccess) / 1000);
    let noDataEl = card.querySelector('.no-data-text');
    if (!noDataEl) {
      noDataEl = document.createElement('div');
      noDataEl.className = 'no-data-text';
      card.appendChild(noDataEl);
    }
    noDataEl.textContent = `No data for ${secsSinceSuccess}s`;
  }
}

// Load dashboard config from JSON file
async function loadConfig() {
  try {
    const response = await fetch('/config.json');
    if (response.ok) {
      const data = await response.json();
      dashboardConfig = data;
      if (data.thresholds) {
        thresholds = { ...thresholds, ...data.thresholds };
      }
    }
  } catch (e) {
    console.warn('Failed to load config.json, using defaults');
  }
}

// Update card warning state (orange border when value exceeds threshold)
function updateCardWarning(cardId, value, threshold) {
  const card = document.querySelector(`[data-card-id="${cardId}"]`);
  if (!card) return;

  // Red (no-data) takes precedence over orange (warning)
  if (card.classList.contains('no-data')) {
    card.classList.remove('warning');
    return;
  }

  if (value > threshold) {
    card.classList.add('warning');
  } else {
    card.classList.remove('warning');
  }
}

// Animate numeric values smoothly
function animateValue(element, newValue, suffix = '', decimals = 1) {
  const current = parseFloat(element.dataset.value || 0);
  const duration = 400;
  const start = performance.now();

  function update(now) {
    const elapsed = now - start;
    const progress = Math.min(elapsed / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic
    const value = current + (newValue - current) * eased;
    element.textContent = value.toFixed(decimals) + suffix;
    if (progress < 1) requestAnimationFrame(update);
    else element.dataset.value = newValue;
  }
  requestAnimationFrame(update);
}

// Fade transition for text value updates
function fadeUpdateValue(element, newText) {
  if (element.textContent === newText) return;
  element.style.opacity = '0.6';
  element.textContent = newText;
  requestAnimationFrame(() => {
    element.style.opacity = '1';
  });
}

// GPU colors for multi-GPU support
const GPU_COLORS = [
  getCssColor('--color-gpu'), getCssColor('--color-gpu-mem'),
  getCssColor('--color-download'), getCssColor('--color-warning'),
  getCssColor('--color-success'), getCssColor('--color-error'),
  getCssColor('--color-accent'), getCssColor('--color-read'),
];

// ── Ping loss visualization (shared by the history and live ping charts) ────
// A target entry is {avg,min,max,loss} on success, {avg:null,...,loss:1.0} on a
// full outage, or absent (never measured). Drawn as a full-height band per slot:
// faint colored fill = partial loss, solid colored stripes = full outage, one
// colour per target (stripes phase-offset so overlapping outages interleave).
// Hover shows an aligned table via a custom HTML tooltip (Chart.js can't colour
// substrings of its built-in tooltip).
const _pingHue = p => Math.round(p * 1.2);   // success%: 0 red -> 60 yellow -> 120 green

// Loss fraction (0..1) for a target entry, or null when the target is absent.
function pingLoss(entry) {
  if (entry === undefined) return null;                 // never measured -> no data
  if (entry === null || entry.avg == null) return 1;    // full outage (legacy bare null too)
  return entry.loss ?? 0;                               // partial / ok (legacy record: no loss = 0)
}

// % of the window a target was up (non-null), for the live footer.
function pingWinUp(arr) {
  return arr.length ? Math.round(arr.filter(v => v != null).length / arr.length * 100) : 0;
}

// Canvas hatch for one slot: faint fill (partial) + solid stripes (full outage),
// per-target colour; stripes phase-offset by target index so overlaps interleave.
function pingBandPattern(entries) {   // entries: [{color, loss}]
  const s = 9, c = document.createElement('canvas'); c.width = c.height = s;
  const x = c.getContext('2d');
  entries.forEach(e => {
    if (e.loss > 0 && e.loss < 1) { x.globalAlpha = 0.16; x.fillStyle = e.color; x.fillRect(0, 0, s, s); }
  });
  x.globalAlpha = 0.9; x.lineWidth = 2;
  entries.forEach((e, i) => {
    if (e.loss >= 1) {
      x.strokeStyle = e.color;
      const ph = i * s / entries.length;
      x.beginPath();
      for (const o of [-2 * s, -s, 0, s]) { x.moveTo(o + ph, s); x.lineTo(o + ph + s, 0); }
      x.stroke();
    }
  });
  x.globalAlpha = 1;
  return x.createPattern(c, 'repeat');
}

// Rows for the shared tooltip. entry: {name,color,down,cur,min,max,pct}
function pingRowsHTML(entries) {
  return entries.map(e => {
    const cur = e.down ? 'down' : (e.cur == null ? '—' : `${e.cur}ms`);
    const curCol = e.down ? '#ef4444' : '#ccc';
    const rng = (e.down || e.min == null) ? '' : `[${e.min}ms, ${e.max}ms]`;
    const pct = e.pct == null ? '<span></span>'
      : `<span class="r" style="color:hsl(${_pingHue(e.pct)},75%,55%)">${e.pct}%</span>`;
    return `<span style="color:${e.color}">${e.name}</span>`
         + `<span style="color:${curCol}">${cur}</span>`
         + `<span style="color:#888">${rng}</span>${pct}`;
  }).join('');
}

function _ensurePingTooltip() {
  let el = document.getElementById('ping-tooltip');
  if (el) return el;
  const style = document.createElement('style');
  style.textContent =
    '#ping-tooltip{position:absolute;pointer-events:none;opacity:0;transition:opacity .08s;z-index:100;' +
    'background:#0c0c0c;border:1px solid #444;border-radius:6px;padding:7px 9px;box-shadow:0 4px 14px rgba(0,0,0,.5)}' +
    '#ping-tooltip .ttl{font:11px system-ui;color:#888;margin-bottom:4px}' +
    '#ping-tooltip .grid{display:grid;grid-template-columns:auto auto auto auto;gap:3px 12px;' +
    'font:12.5px ui-monospace,Menlo,monospace;white-space:nowrap}' +
    '#ping-tooltip .r{text-align:right}' +
    '#ping-tooltip .foot{font:11px system-ui;color:#777;margin-top:5px;border-top:1px solid #333;padding-top:4px}';
  document.head.appendChild(style);
  el = document.createElement('div');
  el.id = 'ping-tooltip';
  document.body.appendChild(el);
  return el;
}

// Build a Chart.js tooltip config using the shared external HTML tooltip.
// entriesFor(i) -> [rows]; footerFor() -> string (optional); titleFor(i) -> string (optional).
function pingTooltipCfg(entriesFor, footerFor, titleFor) {
  const el = _ensurePingTooltip();
  return {
    enabled: false,
    external: (ctx) => {
      const { chart, tooltip } = ctx;
      if (tooltip.opacity === 0) { el.style.opacity = 0; return; }
      const i = tooltip.dataPoints[0].dataIndex;
      const title = titleFor ? titleFor(i) : (chart.data.labels?.[i] ?? '');
      let html = `<div class="ttl">${title}</div><div class="grid">${pingRowsHTML(entriesFor(i))}</div>`;
      if (footerFor) html += `<div class="foot">${footerFor()}</div>`;
      el.innerHTML = html;
      const r = chart.canvas.getBoundingClientRect();
      el.style.left = window.scrollX + r.left + tooltip.caretX + 14 + 'px';
      el.style.top  = window.scrollY + r.top  + tooltip.caretY - 10 + 'px';
      el.style.opacity = 1;
    }
  };
}

// History feature constants
const HISTORY_TIME_RANGES = [
  { value: 1, label: '1 hour' },
  { value: 6, label: '6 hours' },
  { value: 24, label: '24 hours' },
  { value: 168, label: '7 days' },
  { value: 720, label: '30 days' },
];
const _DEFAULT_HISTORY_HOURS = 24;
let defaultHistoryHours = parseFloat(localStorage.getItem('defaultHistoryHours')) || _DEFAULT_HISTORY_HOURS;

// History metric display names and colors
const HISTORY_METRICS = {
  cpu: { name: 'CPU', color: getCssColor('--color-accent'), unit: '%', hasMinMax: true },
  cpu_cores: { name: 'CPU Cores', color: getCssColor('--color-cpu-cores'), unit: '%', hasMinMax: true, centerLabel: 'Median', showMinMaxByDefault: true },
  memory: { name: 'Memory', color: getCssColor('--color-success'), unit: '%', hasUnitToggle: true, altUnit: ' GB' },
  network: { name: 'Network', color: getCssColor('--color-download'), unit: 'MB/s', dual: true, colors: [getCssColor('--color-download'), getCssColor('--color-upload')], hasMinMax: true },
  disk: { name: 'Disk I/O', color: getCssColor('--color-read'), unit: 'MB/s', dual: true, colors: [getCssColor('--color-read'), getCssColor('--color-write')], hasMinMax: true },
  gpu: { name: 'GPU', color: getCssColor('--color-gpu'), unit: '%', perDevice: true },
  'gpu-mem': { name: 'GPU Memory', color: getCssColor('--color-gpu-mem'), unit: '%', hasUnitToggle: true, altUnit: ' GB', perDevice: true },
  temps: { name: 'Temperatures', color: getCssColor('--color-warning'), unit: '°C', perDevice: true },
  // modeToggle: one button that switches the card into an alternative drawing (app.js createHistoryChart).
  fans: { name: 'Fan Speeds', color: getCssColor('--color-download'), unit: ' RPM', perDevice: true,
          modeToggle: { mode: 'range', title: 'One band: min / median / mean / max of all fans' } },
  power: { name: 'Power', color: getCssColor('--color-upload'), unit: ' W', perDevice: true,
           modeToggle: { mode: 'stack', title: 'Stack the sources on top of each other' } },
  load: {
    name: 'System Load',
    color: getCssColor('--color-warning'),
    unit: '',
    triple: true,
    colors: [getCssColor('--color-warning'), getCssColor('--color-load-5m'), getCssColor('--color-load-15m')],
    labels: ['1m', '5m', '15m']
  },
  swap: { name: 'Swap', color: getCssColor('--color-swap'), unit: '%', hasUnitToggle: true, altUnit: ' GB' },
  'disk-drives': {
    name: 'Disk I/O (Per-Drive)',
    color: getCssColor('--color-read'),
    unit: 'MB/s',
    perDevice: true
  },
  'drive-usage': {
    name: 'Drive Usage',
    color: getCssColor('--color-download'),
    unit: '%',
    hasUnitToggle: true,
    altUnit: ' GB',
    perDevice: true
  },
  ping: { name: 'Ping', color: getCssColor('--color-ping-0'), unit: ' ms', multiLine: true, hasMinMax: true },
};

// Track active history cards
const historyCards = new Map(); // metric -> { card, chart }

// Endpoint discovery: which API endpoints does this backend support?
let availableEndpoints = null; // null = not yet loaded (assume all available), Set = loaded

// Map card IDs to the API endpoints they require (paths relative to /api)
const CARD_ENDPOINT_DEPS = {
  'gpu':          ['/4/gpu'],
  'gpu-mem':      ['/4/gpu'],
  'gpu-info':     ['/gpu-info'],
  'disk':         ['/4/diskio'],
  'disk-drives':  ['/4/diskio'],
  'processes':    ['/processlist'],
  'mail':         ['/mail'],
  'hardware':     ['/storage-health'],
  'drive-health': ['/storage-health'],
  'power':        ['/sensors'],
};

async function fetchEndpointList() {
  try {
    const r = await fetch('/api/list-endpoints');
    if (r.ok) {
      const data = await r.json();
      if (Array.isArray(data.endpoints)) {
        availableEndpoints = new Set(data.endpoints);
      }
    }
  } catch (e) {
    // Backend doesn't support list-endpoints — assume all available
  }
}

function isEndpointAvailable(endpointPath) {
  if (availableEndpoints === null) return true;
  return availableEndpoints.has(endpointPath);
}

function hideUnavailableCards() {
  if (availableEndpoints === null) return;

  for (const [cardId, deps] of Object.entries(CARD_ENDPOINT_DEPS)) {
    const allAvailable = deps.every(ep => availableEndpoints.has(ep));
    if (!allAvailable) {
      const card = document.querySelector(`[data-card-id="${cardId}"]`);
      if (card) card.style.display = 'none';
      delete cardStatus[cardId];
    }
  }

  updateConnectionStatus();
}

// Helper to fetch with error tracking and timeout: returns {data, ok, errorType?, status?}
async function safeFetch(url, fallback, timeout = 5000) {
  // Skip fetch if endpoint is not available on this backend
  const match = url.match(/^\/api(\/[^?]*)/);
  if (match && availableEndpoints !== null && !availableEndpoints.has(match[1])) {
    return { data: fallback, ok: false, errorType: 'unavailable' };
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  try {
    const r = await fetch(url, { signal: controller.signal });
    clearTimeout(timeoutId);
    if (!r.ok) {
      return { data: fallback, ok: false, errorType: 'http', status: r.status };
    }
    const data = typeof fallback === 'string' ? await r.text() : await r.json();
    return { data, ok: true };
  } catch (err) {
    clearTimeout(timeoutId);
    if (err.name === 'AbortError') {
      return { data: fallback, ok: false, errorType: 'timeout' };
    }
    return { data: fallback, ok: false, errorType: 'network', errorMessage: err.message };
  }
}

// Data cache for reducing API calls
const dataCache = {
  // 30 s, not static: power_draw feeds the Power card.
  gpuInfo: { ttl: 30000, data: null, lastUpdate: null },
  cpuInfo: { ttl: Infinity, data: null, lastUpdate: null },
  systemInfo: { ttl: Infinity, data: null, lastUpdate: null },
  drives: { ttl: 60000, data: null, lastUpdate: null },
  processes: { ttl: 10000, data: null, lastUpdate: null },
  networkProcesses: { ttl: 30000, data: null, lastUpdate: null },
  diskProcesses: { ttl: 30000, data: null, lastUpdate: null },
  ipInfo: { ttl: 60000, data: null, lastUpdate: null },
  // Well under the 30 s slow-poll interval: a ttl equal to it would still be
  // "valid" at the next tick and skip every other refresh.
  mail: { ttl: 10000, data: null, lastUpdate: null },
  storageHealth: { ttl: 10000, data: null, lastUpdate: null },
  sensors: { ttl: 10000, data: null, lastUpdate: null }
};

function isCacheValid(key) {
  const cache = dataCache[key];
  if (!cache || cache.data === null || cache.lastUpdate === null) return false;
  if (cache.ttl === Infinity) return true;
  return (Date.now() - cache.lastUpdate) < cache.ttl;
}

function updateCache(key, data) {
  if (dataCache[key]) {
    dataCache[key].data = data;
    dataCache[key].lastUpdate = Date.now();
  }
}

function getCache(key) {
  return dataCache[key]?.data;
}
// Same, but null once the last SUCCESSFUL fetch is older than maxAgeMs: a
// snapshot whose refetches keep failing must not be drawn as live forever.
function getFreshCache(key, maxAgeMs) {
  const c = dataCache[key];
  return c?.lastUpdate && Date.now() - c.lastUpdate <= maxAgeMs ? c.data : null;
}

// The CPU core temperatures (e.g. 36 on a two-socket box: Glances labels socket 0's
// "Core N" and socket 1's "Core N <index>") collapse to one min / median / mean /
// max, for the live bars and the history band alike. `entries` are {label, value}
// objects; returns the aggregate (null when there are no cores) and the rest.
const CORE_TEMP_LABEL = /^Core \d+( \d+)?$/;
// min/max/mean/median of a list of numbers (the "band" shape used for the CPU
// cores and, in the fans history's range mode, for the fans); null when empty.
function rangeStats(nums) {
  const xs = nums.filter(v => v != null).sort((a, b) => a - b);
  if (xs.length === 0) return null;
  const mid = xs.length >> 1;
  return {
    min: xs[0],
    max: xs[xs.length - 1],
    mean: Math.round(xs.reduce((s, v) => s + v, 0) / xs.length * 10) / 10,
    median: xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2,
    n: xs.length,
  };
}
function aggregateCoreTemps(entries) {
  return {
    cores: rangeStats(entries.filter(e => CORE_TEMP_LABEL.test(e.label)).map(e => e.value)),
    rest: entries.filter(e => !CORE_TEMP_LABEL.test(e.label)),
  };
}

function formatTimeAgo(timestamp) {
  if (!timestamp) return '';
  const secs = Math.floor((Date.now() - timestamp) / 1000);
  if (secs < 60) return secs + 's ago';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  return hours + 'h ago';
}

function updateCacheDisplay(cardId, cacheKey) {
  const card = document.querySelector(`[data-card-id="${cardId}"]`);
  if (!card) return;
  const timeEl = card.querySelector('.cache-time');
  if (timeEl && dataCache[cacheKey]) {
    timeEl.textContent = formatTimeAgo(dataCache[cacheKey].lastUpdate);
  }
}

// Formatting utilities
function formatBytes(bytes) {
  const kb = bytes / 1024;
  const mb = kb / 1024;
  const gb = mb / 1024;
  if (mb < 1) return kb.toFixed(1) + ' KB';
  if (gb < 1) return mb.toFixed(1) + ' MB';
  return gb.toFixed(1) + ' GB';
}

function formatNetworkRate(kbps) {
  if (kbps < 1) return kbps.toFixed(2) + ' KB/s';
  if (kbps < 1000) return kbps.toFixed(1) + ' KB/s';
  return (kbps / 1000).toFixed(1) + ' MB/s';
}

// ============================================================================
// Reusable Dropdown Picker
// ============================================================================

let _activeDropdown = null;

/**
 * Create a dropdown picker popup positioned below a trigger element.
 * Only one picker can be open at a time (singleton).
 *
 * @param {Element} triggerEl - Element to position the picker below
 * @param {Object} opts
 * @param {Array<{value:*, label:string}>} opts.options - Preset options
 * @param {*} opts.currentValue - Currently active value (highlighted)
 * @param {function(*):void} opts.onSelect - Called when a value is chosen
 * @param {Object|null} [opts.custom] - Custom input config, or null to omit
 * @param {string} [opts.custom.type] - 'number' or 'int'
 * @param {number} [opts.custom.min]
 * @param {number} [opts.custom.max]
 * @param {string} [opts.custom.step]
 * @param {string} [opts.custom.placeholder]
 * @returns {{close: function():void}}
 */
function createDropdownPicker(triggerEl, { options, currentValue, onSelect, custom }) {
  if (_activeDropdown) _activeDropdown.close();

  const picker = document.createElement('div');
  picker.className = 'dropdown-picker';

  const isPreset = options.some(o => o.value === currentValue);

  options.forEach(({ value, label }) => {
    const btn = document.createElement('button');
    btn.className = 'dropdown-picker-item';
    if (value === currentValue) btn.classList.add('active');
    btn.textContent = label;
    btn.addEventListener('click', () => { onSelect(value); close(); });
    picker.appendChild(btn);
  });

  if (custom) {
    const row = document.createElement('div');
    row.className = 'dropdown-picker-custom';
    const lbl = document.createElement('span');
    lbl.className = 'dropdown-picker-item' + (isPreset ? '' : ' active');
    lbl.textContent = 'Custom:';
    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'dropdown-picker-input';
    if (custom.min != null) input.min = custom.min;
    if (custom.max != null) input.max = custom.max;
    input.step = custom.step || 'any';
    input.placeholder = custom.placeholder || '';
    if (!isPreset) input.value = currentValue;
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const val = parseFloat(input.value);
        const lo = custom.min != null ? parseFloat(custom.min) : -Infinity;
        const hi = custom.max != null ? parseFloat(custom.max) : Infinity;
        if (!isNaN(val) && val >= lo && val <= hi) {
          onSelect(custom.type === 'int' ? Math.round(val) : val);
          close();
        }
      }
    });
    row.appendChild(lbl);
    row.appendChild(input);
    picker.appendChild(row);
  }

  document.body.appendChild(picker);

  const rect = triggerEl.getBoundingClientRect();
  picker.style.top = (rect.bottom + 4) + 'px';
  picker.style.left = rect.left + 'px';

  // Clamp to viewport right edge
  requestAnimationFrame(() => {
    const pr = picker.getBoundingClientRect();
    if (pr.right > window.innerWidth - 8) {
      picker.style.left = (window.innerWidth - pr.width - 8) + 'px';
    }
  });

  function onOutsideClick(e) {
    if (!picker.contains(e.target) && e.target !== triggerEl) close();
  }
  setTimeout(() => document.addEventListener('mousedown', onOutsideClick), 0);

  function close() {
    picker.remove();
    document.removeEventListener('mousedown', onOutsideClick);
    if (_activeDropdown?.picker === picker) _activeDropdown = null;
  }

  _activeDropdown = { picker, close };
  return { close };
}
