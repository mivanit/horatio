// Data stores (pre-filled with nulls so data appears at right edge "now")
const history = {
  cpu: Array(historyLength).fill(null),
  mem: Array(historyLength).fill(null),
  swap: Array(historyLength).fill(null),
  gpus: [],      // Array of arrays, one per GPU (initialized when detected)
  gpuMems: [],   // Array of arrays, one per GPU (initialized when detected)
  netIn: Array(historyLength).fill(null),
  netOut: Array(historyLength).fill(null),
  diskRead: Array(historyLength).fill(null),
  diskWrite: Array(historyLength).fill(null),
  diskDriveReads: [],   // Array of arrays, one per drive (initialized when detected)
  diskDriveWrites: [],  // Array of arrays, one per drive (initialized when detected)
  pings: [],            // Array of arrays, one per ping target (initialized from config)
  // True for unmeasured slots: initial/resized padding and disconnect/pause
  // gaps. A measured failure can then stay null without losing its outage band.
  pingGap: Array(historyLength).fill(true),
  // Cross-core CPU spread: min/median/max across cores at each poll
  cpuCoresMin: Array(historyLength).fill(null),
  cpuCoresMedian: Array(historyLength).fill(null),
  cpuCoresMax: Array(historyLength).fill(null)
};

let gpuCount = 0;  // Track number of GPUs
let diskDriveCount = 0;  // Track number of disk drives
let diskDriveNames = [];  // Track drive names

// Endpoints can miss polls independently. Keep each series' sample times with
// its values, so changing the polling interval cannot redate retained samples.
const historyTimes = new WeakMap();

function pushHistory(arr, value, timestamp = Date.now()) {
  let times = historyTimes.get(arr);
  if (!times) {
    times = Array(arr.length).fill(null);
    historyTimes.set(arr, times);
  }
  arr.push(value);
  times.push(timestamp);
  if (arr.length > historyLength) {
    arr.shift();
    times.shift();
  }
}

function resizeHistory(newLength) {
  const oldLength = historyLength;
  historyLength = newLength;

  const resizeArray = (arr) => {
    if (newLength > oldLength) {
      const padding = Array(newLength - arr.length).fill(arr === history.pingGap ? true : null);
      arr.unshift(...padding);
    } else {
      while (arr.length > newLength) arr.shift();
    }
    const times = historyTimes.get(arr);
    if (times) resizeArray(times);
  };

  resizeArray(history.cpu);
  resizeArray(history.mem);
  resizeArray(history.swap);
  resizeArray(history.netIn);
  resizeArray(history.netOut);
  resizeArray(history.diskRead);
  resizeArray(history.diskWrite);
  resizeArray(history.cpuCoresMin);
  resizeArray(history.cpuCoresMedian);
  resizeArray(history.cpuCoresMax);
  resizeArray(history.pingGap);
  history.pings.forEach(resizeArray);
  history.gpus.forEach(resizeArray);
  history.gpuMems.forEach(resizeArray);
  history.diskDriveReads.forEach(resizeArray);
  history.diskDriveWrites.forEach(resizeArray);
  updatePingLossBand();
}

// Derived from the measured slots, so resizing and reconnect gaps must update
// the band too, even while polling is paused or the next ping request fails.
function updatePingLossBand() {
  const band = charts.ping.data.datasets[0];
  if (!band || band.label !== 'loss' || !history.pings.length) return;
  const targets = dashboardConfig.ping_targets || DEFAULT_PING_TARGETS;
  const colors = targets.map((_, i) => getCssColor('--color-ping-' + i) || GPU_COLORS[i % GPU_COLORS.length]);
  const slotLoss = i => targets.map((_, k) =>
    history.pings[k][i] == null && !history.pingGap[i] ? 1 : 0);
  band.data = history.pings[0].map((_, i) => slotLoss(i).some(loss => loss > 0) ? 1 : null);
  band.backgroundColor = history.pings[0].map((_, i) => {
    const losses = slotLoss(i);
    return losses.some(loss => loss > 0)
      ? pingBandPattern(targets.map((_, k) => ({ color: colors[k], loss: losses[k] })))
      : 'rgba(0,0,0,0)';
  });
}

// Generate time labels for x-axis (seconds ago). Padding has no sample time.
function generateTimeLabels(samples) {
  const now = Date.now();
  const times = historyTimes.get(samples) || Array(historyLength).fill(null);
  return times.map(time => time == null ? '' : Math.max(0, Math.round((now - time) / 1000)));
}

// Format time label (seconds ago -> human readable)
function formatTimeLabel(secsAgo) {
  if (secsAgo === '') return '';
  if (Number(secsAgo) === 0) return 'now';
  return secsAgo + 's';
}

// ============================================================================
// Shared chart styling (read from CSS variables, eliminates hex duplication)
// ============================================================================

const CHART_FONT_SIZE = getCssNumber('--chart-font-size', 16);
const CHART_RADIUS = getCssNumber('--chart-radius', 2);
const CHART_TOOLTIP = {
  enabled: true,
  backgroundColor: getCssColor('--bg-card'),
  borderColor: getCssColor('--border-color'),
  borderWidth: 1,
  titleColor: getCssColor('--text-secondary'),
  bodyColor: getCssColor('--text-primary'),
  bodyFont: { size: CHART_FONT_SIZE },
  cornerRadius: CHART_RADIUS,
  padding: 10,
};
const CHART_GRID = { color: getCssColor('--border-color') };
const CHART_TICK = { color: getCssColor('--text-muted'), font: { size: CHART_FONT_SIZE } };

// A y-axis number, short enough that axes of one width fit them all. A Chart.js
// tick callback: the decimals are those of the step between ticks, so ticks
// 0.002 apart (an idle box's MB/s) do not all read "0".
function axisNumber(v, i, ticks) {
  const size = Math.abs(v);
  if (size >= 1e6) return +(v / 1e6).toFixed(1) + 'M';
  if (size >= 1e4) return +(v / 1e3).toFixed(1) + 'k';
  const step = ticks && ticks.length > 1 ? Math.abs(ticks[1].value - ticks[0].value) : 1;
  const decimals = (String(+step.toPrecision(4)).split('.')[1] || '').length;
  return v.toLocaleString(undefined, { maximumFractionDigits: Math.min(decimals, 4) });
}

// Every history card keeps this much for its y axis (the unit and five-character
// numbers), so the plots, and the Services grid beside them, start at the same place.
const HISTORY_AXIS_WIDTH = Math.round(CHART_FONT_SIZE * 5.25);
// Room right of every history plot, so the newest point's marker is not cut off.
// Fixed, where Chart.js would size it by each chart's own markers.
const HISTORY_PLOT_INSET = 8;

// Ticks for a history time axis `width` px wide, from `min` to `max` (ms):
// [{ value, label }] at round local times, a whole number of steps from midnight.
// Midnight is labelled with its date. A tick whose label would reach past either
// end is left out, so the axis needs no room beyond the plot.
const HISTORY_TICK_MINUTES = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720];
const HISTORY_TICK_DAYS = [1, 2, 3, 7, 14, 30, 60, 90, 180, 365];
const HISTORY_TICK_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function historyTicks(min, max, width) {
  const span = max - min;
  if (!(span > 0) || !(width > 0)) return [];
  const labelPx = CHART_FONT_SIZE * 0.6 * 6;  // "HH:mm", "MMM dd" in the chart font
  const most = Math.max(1, Math.floor(width / (labelPx * 1.6)));
  const minutes = HISTORY_TICK_MINUTES.find(m => span / (m * 60000) <= most);
  const days = minutes ? 0 : HISTORY_TICK_DAYS.find(d => span / (d * 86400000) <= most) || 365;
  const midnight = new Date(min);
  midnight.setHours(0, 0, 0, 0);
  const two = n => String(n).padStart(2, '0');
  const ticks = [];
  for (let k = 0; ; k++) {
    const at = new Date(midnight);
    if (minutes) at.setTime(midnight.getTime() + k * minutes * 60000);
    else at.setDate(at.getDate() + k * days);  // by calendar day: a DST day is not 24 h
    const value = at.getTime(), px = (value - min) / span * width;
    if (value > max) break;
    if (px < labelPx / 2 || px > width - labelPx / 2) continue;
    const dated = !minutes || (!at.getHours() && !at.getMinutes());
    ticks.push({ value, label: dated ? `${HISTORY_TICK_MONTHS[at.getMonth()]} ${at.getDate()}` : `${two(at.getHours())}:${two(at.getMinutes())}` });
  }
  return ticks;
}
const CHART_X_AXIS = {
  display: true,
  ticks: {
    ...CHART_TICK,
    autoSkip: true,
    maxTicksLimit: 5,
    maxRotation: 0,
    minRotation: 0,
    callback: function(value, index) {
      const secs = this.chart.data.labels[index];
      return formatTimeLabel(secs);
    }
  },
  grid: { display: false },
  border: { display: false }
};
// Chart.js gives this axis at most half the canvas height and clips a rotated
// label that is longer from its start ("k10temp 0" drew as "0temp 0"), so a long
// name keeps its head and tail instead. The tooltip has the full name.
function barTickLabel(value) {
  const label = String(this.getLabelForValue(value));
  // 7 characters fit the shortest card; 8 still lost the first one on bodel.
  return label.length > 7 ? label.slice(0, 4) + '…' + label.slice(-2) : label;
}
const CHART_X_AXIS_BAR = {
  display: true,
  ticks: { ...CHART_TICK, autoSkip: false, maxRotation: 90, minRotation: 90, callback: barTickLabel },
  // drawTicks off: the 8 px Chart.js keeps for tick marks goes to the labels.
  grid: { display: false, drawTicks: false },
  border: { display: false }
};

function makeDataset(color, data, label) {
  const ds = {
    data: data,
    borderColor: color,
    backgroundColor: withAlpha(color, '20'),
    fill: true,
    tension: 0.4,
    pointRadius: 0,
    pointHoverRadius: 4,
    borderWidth: 2
  };
  if (label) ds.label = label;
  return ds;
}

function makeYAxis({ max, stepSize, tickFormat }) {
  return {
    display: true, min: 0,
    ...(max !== undefined && { max }),
    ticks: { ...CHART_TICK, ...(stepSize && { stepSize }), callback: tickFormat },
    grid: CHART_GRID, border: { display: false }
  };
}

function buildChart({ type = 'line', data, tooltip, xAxis, yAxis }) {
  return {
    type,
    data: data || { labels: type === 'bar' ? [] : generateTimeLabels(), datasets: [] },
    plugins: type === 'bar' ? [] : [{
      id: 'sampleTimes',
      beforeUpdate(chart) {
        // Ping's first dataset is the derived loss band, not a sample series.
        const series = chart.data.datasets.find(dataset => historyTimes.has(dataset.data));
        chart.data.labels = generateTimeLabels(series?.data);
      }
    }],
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: { legend: { display: false }, tooltip: { ...CHART_TOOLTIP, ...tooltip } },
      scales: { x: xAxis || CHART_X_AXIS, y: yAxis },
      animation: { duration: 0 }
    }
  };
}

// Tooltip title helper (most charts use time labels)
const timeTitle = (ctx) => formatTimeLabel(ctx[0].label);

// MB/s tick formatter (adaptive precision)
const mbpsTick = (v) => { if (v < 1) return v.toFixed(2) + ' MB/s'; if (v < 10) return v.toFixed(1) + ' MB/s'; return v.toFixed(0) + ' MB/s'; };

// ============================================================================
// Chart config factories
// ============================================================================

function chartConfig(color, data) {
  return buildChart({
    data: { labels: generateTimeLabels(), datasets: [makeDataset(color, data)] },
    tooltip: { displayColors: false, callbacks: { title: timeTitle, label: (ctx) => ctx.parsed.y.toFixed(1) + '%' } },
    yAxis: makeYAxis({ max: 100, stepSize: 25, tickFormat: (v) => v + '%' })
  });
}

function pingChartConfig() {
  return buildChart({
    tooltip: { callbacks: { title: timeTitle, label: (ctx) => ctx.parsed.y != null ? ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + ' ms' : '' } },
    yAxis: makeYAxis({ tickFormat: (v) => v + ' ms' })
  });
}

// Memory chart config (totalGiB stored in chart._totalGiB for tooltip)
function memChartConfig(color, data, totalGiB = 16) {
  return buildChart({
    data: { labels: generateTimeLabels(), datasets: [makeDataset(color, data)] },
    tooltip: { displayColors: false, callbacks: { title: timeTitle, label: function(ctx) {
      const gb = ctx.parsed.y;
      const totalGiB = ctx.chart._totalGiB || 16;
      const pct = totalGiB > 0 ? (gb / totalGiB * 100) : 0;
      return gb.toFixed(1) + ' GB (' + pct.toFixed(1) + '%)';
    }}},
    yAxis: makeYAxis({ max: totalGiB, tickFormat: (v) => v.toFixed(0) + ' GB' })
  });
}

function chartConfigDual(color1, color2, data1, data2, label1, label2) {
  return buildChart({
    data: { labels: generateTimeLabels(), datasets: [makeDataset(color1, data1, label1), makeDataset(color2, data2, label2)] },
    tooltip: { callbacks: { title: timeTitle, label: (ctx) => ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + ' MB/s' } },
    yAxis: makeYAxis({ tickFormat: mbpsTick })
  });
}

function cpuCoresConfig() {
  return buildChart({
    type: 'bar',
    data: { labels: [], datasets: [{ data: [], backgroundColor: [], borderWidth: 0, borderRadius: CHART_RADIUS }] },
    tooltip: { titleFont: { size: CHART_FONT_SIZE }, displayColors: false, callbacks: { title: (ctx) => 'Core ' + ctx[0].label, label: (ctx) => ctx.parsed.y.toFixed(1) + '%' } },
    xAxis: CHART_X_AXIS_BAR,
    yAxis: makeYAxis({ max: 100, stepSize: 25, tickFormat: (v) => v + '%' })
  });
}

// Cross-core spread: shaded min->max band (max fills down to min) + median line.
function cpuCoresSpreadConfig() {
  const color = getCssColor('--color-cpu-cores');
  return buildChart({
    data: { labels: generateTimeLabels(), datasets: [
      // Band floor (invisible line, just the fill target for max below)
      { label: 'min', data: history.cpuCoresMin, borderColor: 'transparent',
        backgroundColor: 'transparent', fill: false, tension: 0.4, pointRadius: 0, borderWidth: 0 },
      // Band ceiling, filled down to the min dataset (index -1) -> shaded region
      { label: 'max', data: history.cpuCoresMax, borderColor: withAlpha(color, '55'),
        backgroundColor: withAlpha(color, '22'), fill: '-1', tension: 0.4, pointRadius: 0, borderWidth: 1 },
      // Median line (solid)
      { label: 'median', data: history.cpuCoresMedian, borderColor: color,
        backgroundColor: 'transparent', fill: false, tension: 0.4, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2 }
    ]},
    tooltip: { callbacks: { title: timeTitle, label: (ctx) => ctx.parsed.y != null ? ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + '%' : '' } },
    yAxis: makeYAxis({ max: 100, stepSize: 25, tickFormat: (v) => v + '%' })
  });
}

// Value scales (CPU core bars, process table). Their colors are --scale-* in
// style.css, read on first use; the fallbacks are the default theme's.
const MEMORY_STEPS_MB = [100, 512, 1024, 2048, 4096, 8192, 16384];
let _scales;
function scaleColors() {
  if (_scales) return _scales;
  const rgb = (name, fallback) => cssRgb(getCssColor(name)) || fallback;
  const defaultMemory = ['#4a7a5a', '#5a9a5a', '#7ab856', '#eed535', '#ffb020', '#ff8830', '#ff5540', '#ff3030'];
  _scales = {
    low: rgb('--scale-low', [34, 197, 94]),
    mid: rgb('--scale-mid', [245, 158, 11]),
    high: rgb('--scale-high', [239, 68, 68]),
    idle: getCssColor('--scale-idle') || '#166534',
    infoLow: rgb('--scale-info-low', [100, 116, 139]),
    infoHigh: rgb('--scale-info-high', [59, 130, 246]),
    memory: defaultMemory.map((fallback, i) => getCssColor(`--scale-mem-${i}`) || fallback),
  };
  return _scales;
}

function mixRgb(from, to, ratio) {
  return `rgb(${[0, 1, 2].map(i => Math.round(from[i] + (to[i] - from[i]) * ratio)).join(', ')})`;
}

// Low to mid to high (green, yellow, red by default)
function getCoreColor(percent) {
  const s = scaleColors();
  return percent < 50 ? mixRgb(s.low, s.mid, percent / 50) : mixRgb(s.mid, s.high, (percent - 50) / 50);
}

function getNetworkRateColor(kbps) {
  // Idle color for very low rates (< 1 KB/s)
  if (kbps < 1) return scaleColors().idle;
  // Map network rate to 0-100 scale: 1 KB/s = 0%, 10 MB/s+ = 100%
  const percent = Math.min(100, (kbps / 10000) * 100);
  return getCoreColor(percent);
}

// Muted to vivid (gray-blue to blue by default) for informational metrics
// (threads, cpu time): not the low-to-high scale, since high isn't bad here.
function getInfoColor(value, maxValue) {
  if (!value) return null;
  const s = scaleColors();
  return mixRgb(s.infoLow, s.infoHigh, Math.min(100, (value / maxValue) * 100) / 100);
}

// Memory color in 8 steps of absolute size (same for RAM and GPU)
function getMemoryColor(bytes) {
  if (!bytes) return null;
  const mb = bytes / (1024 * 1024);
  const step = MEMORY_STEPS_MB.findIndex(limit => mb < limit);
  return scaleColors().memory[step === -1 ? MEMORY_STEPS_MB.length : step];
}

function gpuChartConfig() {
  return buildChart({
    tooltip: { callbacks: { title: timeTitle, label: (ctx) => 'GPU ' + ctx.datasetIndex + ': ' + ctx.parsed.y.toFixed(1) + '%' } },
    yAxis: makeYAxis({ max: 100, stepSize: 25, tickFormat: (v) => v + '%' })
  });
}

// GPU memory chart (totalGiB per GPU stored in chart._gpuMemTotals)
function gpuMemChartConfig() {
  return buildChart({
    tooltip: { callbacks: { title: timeTitle, label: function(ctx) {
      const gb = ctx.parsed.y;
      const totals = ctx.chart._gpuMemTotals || [];
      const totalGiB = totals[ctx.datasetIndex] || 0;
      const pct = totalGiB > 0 ? (gb / totalGiB * 100) : 0;
      return 'GPU ' + ctx.datasetIndex + ': ' + gb.toFixed(1) + ' GB (' + pct.toFixed(1) + '%)';
    }}},
    yAxis: makeYAxis({ max: 24, tickFormat: (v) => v.toFixed(0) + ' GB' })
  });
}

function diskDrivesChartConfig() {
  return buildChart({
    tooltip: { callbacks: { title: timeTitle, label: (ctx) => ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + ' MB/s' } },
    yAxis: makeYAxis({ tickFormat: mbpsTick })
  });
}

// The CPU cores are one floating bar [min, max] (dataset 0, a [lo, hi] datum) with a
// white median tick from dataset 1; the stats behind it sit in chart._cores for the tooltip.
function tempsChartConfig() {
  return buildChart({
    type: 'bar',
    data: { labels: [], datasets: [
      { data: [], backgroundColor: [], borderWidth: 0, borderRadius: CHART_RADIUS },
      { type: 'line', data: [], showLine: false, pointStyle: 'line', pointRadius: 10, pointHoverRadius: 10,
        pointBorderWidth: 2, pointBorderColor: getCssColor('--text-title'), pointHoverBorderColor: getCssColor('--text-title') },
    ] },
    tooltip: { displayColors: false, filter: (item) => item.datasetIndex === 0, callbacks: { label: (ctx) => {
      const c = ctx.chart._cores;
      if (c && ctx.label === 'Cores') return `min ${c.min} · median ${c.median} · mean ${c.mean} · max ${c.max} °C (${c.n} cores)`;
      return ctx.parsed.y.toFixed(0) + '°C';
    } } },
    xAxis: CHART_X_AXIS_BAR,
    yAxis: makeYAxis({ max: 100, stepSize: 25, tickFormat: (v) => v + '°C' })
  });
}

function fansChartConfig() {
  return buildChart({
    type: 'bar',
    data: { labels: [], datasets: [{ data: [], backgroundColor: getCssColor('--color-download'), borderWidth: 0, borderRadius: CHART_RADIUS }] },
    tooltip: { displayColors: false, callbacks: { label: (ctx) => ctx.parsed.y.toFixed(0) + ' RPM' } },
    xAxis: CHART_X_AXIS_BAR,
    // Not axisNumber: fans run in the low thousands, where it writes "1,500".
    yAxis: makeYAxis({ tickFormat: (v) => v >= 1000 ? (v/1000).toFixed(1) + 'k' : v })
  });
}

function getTempColor(temp, warning = 70, critical = 90) {
  if (temp >= critical) return COLORS.error;
  if (temp >= warning) return COLORS.warning;
  if (temp >= warning * 0.7) return getCssColor('--color-temp-mid');
  return COLORS.success;
}

function getDriveUsageColor(percent) {
  if (percent >= 90) return COLORS.error;
  if (percent >= 75) return COLORS.warning;
  return COLORS.success;
}

// config.json temp_labels: a display name for a temperature sensor, e.g.
// {"loc1": "I350 NIC"}. Display only: history records keep the sensor's own
// name, so a series does not split in two on the day it is renamed.
function tempDisplayLabel(label) {
  const alias = dashboardConfig.temp_labels?.[label];
  return typeof alias === 'string' && alias ? alias : label;
}

// config.json temp_thresholds: a sensor's own limit in °C, by its own name or
// its temp_labels name. null when it has none.
function tempLimitFor(label) {
  const limits = dashboardConfig.temp_thresholds;
  const valid = v => typeof v === 'number' && Number.isFinite(v);
  // A junk entry under one name must not hide a valid one under the other.
  const own = [limits?.[label], limits?.[tempDisplayLabel(label)]].find(valid);
  return own ?? null;
}

function abbreviateSensorLabel(label) {
  const alias = tempDisplayLabel(label);
  if (alias !== label) return alias;
  return label.replace(/^Package id \d+$/i, 'Pkg').replace(/^Core (\d+)$/i, 'C$1').replace(/^Composite$/i, 'NVMe');
}

function abbreviateGpuName(name) {
  if (!name) return '';
  return name.replace(/^NVIDIA\s+(GeForce\s+)?/i, '');
}

function updateGpuLegend(elementId, gpus) {
  const container = document.getElementById(elementId);
  if (gpus.length <= 1) {
    container.innerHTML = '';
    return;
  }
  let html = '';
  gpus.forEach((gpu, i) => {
    const color = GPU_COLORS[i % GPU_COLORS.length];
    const name = abbreviateGpuName(gpu.name);
    const label = name ? `GPU ${i}: ${name}` : `GPU ${i}`;
    html += `<span class="legend-item"><span class="legend-dot" style="background: ${color};"></span>${escapeHtml(label)}</span>`;
  });
  container.innerHTML = html;
}

function updateDiskDrivesLegend(elementId, driveNames) {
  const container = document.getElementById(elementId);
  if (!container) return;
  let html = '';
  driveNames.forEach((name, i) => {
    const escaped = escapeHtml(name);
    const color = GPU_COLORS[i % GPU_COLORS.length];
    html += `<span class="legend-item"><span class="legend-dot" style="background: ${color};"></span>${escaped} R</span>`;
    html += `<span class="legend-item"><span class="legend-dot" style="background: ${color}; opacity: 0.5;"></span>${escaped} W</span>`;
  });
  container.innerHTML = html;
}

// Initialize charts (colors read from CSS variables)
const charts = {
  cpu: new Chart(document.getElementById('cpu-chart'), chartConfig(getCssColor('--color-accent'), history.cpu)),
  cpuCores: new Chart(document.getElementById('cpu-cores-chart'), cpuCoresConfig()),
  cpuCoresSpread: new Chart(document.getElementById('cpu-cores-spread-chart'), cpuCoresSpreadConfig()),
  mem: new Chart(document.getElementById('mem-chart'), memChartConfig(getCssColor('--color-success'), history.mem, 16)),
  swap: new Chart(document.getElementById('swap-chart'), chartConfig(getCssColor('--color-warning'), history.swap)),
  gpu: new Chart(document.getElementById('gpu-chart'), gpuChartConfig()),
  gpuMem: new Chart(document.getElementById('gpu-mem-chart'), gpuMemChartConfig()),
  net: new Chart(document.getElementById('net-chart'), chartConfigDual(getCssColor('--color-download'), getCssColor('--color-upload'), history.netIn, history.netOut, '↓ In', '↑ Out')),
  disk: new Chart(document.getElementById('disk-chart'), chartConfigDual(getCssColor('--color-read'), getCssColor('--color-write'), history.diskRead, history.diskWrite, '↑ Read', '↓ Write')),
  diskDrives: new Chart(document.getElementById('disk-drives-chart'), diskDrivesChartConfig()),
  temps: new Chart(document.getElementById('temps-chart'), tempsChartConfig()),
  fans: new Chart(document.getElementById('fans-chart'), fansChartConfig()),
  ping: new Chart(document.getElementById('ping-chart'), pingChartConfig())
};
