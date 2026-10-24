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

function pushHistory(arr, value) {
  arr.push(value);
  if (arr.length > historyLength) arr.shift();
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
}

// Generate time labels for x-axis (seconds ago)
function generateTimeLabels() {
  const labels = [];
  for (let i = 0; i < historyLength; i++) {
    const secsAgo = (historyLength - 1 - i) * (pollInterval / 1000);
    labels.push(secsAgo);
  }
  return labels;
}

// Format time label (seconds ago -> human readable)
function formatTimeLabel(secsAgo) {
  if (secsAgo === 0) return 'now';
  return secsAgo + 's';
}

// ============================================================================
// Shared chart styling (read from CSS variables, eliminates hex duplication)
// ============================================================================

const CHART_TOOLTIP = {
  enabled: true,
  backgroundColor: getCssColor('--bg-card'),
  borderColor: getCssColor('--border-color'),
  borderWidth: 1,
  titleColor: getCssColor('--text-secondary'),
  bodyColor: getCssColor('--text-primary'),
  bodyFont: { size: 16 },
  padding: 10,
};
const CHART_GRID = { color: getCssColor('--border-color') };
const CHART_TICK = { color: getCssColor('--text-muted'), font: { size: 16 } };
const CHART_X_AXIS = {
  display: true,
  ticks: {
    ...CHART_TICK,
    autoSkip: true,
    maxRotation: 0,
    minRotation: 0,
    callback: function(value, index) {
      const secs = this.chart.data.labels[index];
      if (secs % 30 !== 0) return null;
      return formatTimeLabel(secs);
    }
  },
  grid: { display: false },
  border: { display: false }
};
const CHART_X_AXIS_BAR = {
  display: true,
  ticks: { ...CHART_TICK, autoSkip: false, maxRotation: 90, minRotation: 90 },
  grid: { display: false },
  border: { display: false }
};

function makeDataset(color, data, label) {
  const ds = {
    data: data,
    borderColor: color,
    backgroundColor: color + '20',
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
    data: { labels: [], datasets: [{ data: [], backgroundColor: [], borderWidth: 0, borderRadius: 2 }] },
    tooltip: { titleFont: { size: 16 }, displayColors: false, callbacks: { title: (ctx) => 'Core ' + ctx[0].label, label: (ctx) => ctx.parsed.y.toFixed(1) + '%' } },
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
      { label: 'max', data: history.cpuCoresMax, borderColor: color + '55',
        backgroundColor: color + '22', fill: '-1', tension: 0.4, pointRadius: 0, borderWidth: 1 },
      // Median line (solid)
      { label: 'median', data: history.cpuCoresMedian, borderColor: color,
        backgroundColor: 'transparent', fill: false, tension: 0.4, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2 }
    ]},
    tooltip: { callbacks: { title: timeTitle, label: (ctx) => ctx.parsed.y != null ? ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + '%' : '' } },
    yAxis: makeYAxis({ max: 100, stepSize: 25, tickFormat: (v) => v + '%' })
  });
}

function getCoreColor(percent) {
  // Green to yellow to red gradient
  if (percent < 50) {
    const ratio = percent / 50;
    const r = Math.round(34 + (245 - 34) * ratio);
    const g = Math.round(197 + (158 - 197) * ratio);
    const b = Math.round(94 + (11 - 94) * ratio);
    return `rgb(${r}, ${g}, ${b})`;
  } else {
    const ratio = (percent - 50) / 50;
    const r = Math.round(245 + (239 - 245) * ratio);
    const g = Math.round(158 + (68 - 158) * ratio);
    const b = Math.round(11 + (68 - 11) * ratio);
    return `rgb(${r}, ${g}, ${b})`;
  }
}

function getNetworkRateColor(kbps) {
  // Dark green for very low rates (< 1 KB/s)
  if (kbps < 1) return '#166534';
  // Map network rate to 0-100 scale: 1 KB/s = 0%, 10 MB/s+ = 100%
  const percent = Math.min(100, (kbps / 10000) * 100);
  return getCoreColor(percent);
}

// Blue gradient for informational metrics (threads, cpu time)
// Not red/green since high values aren't necessarily bad
function getInfoColor(value, maxValue) {
  if (!value) return null;
  const percent = Math.min(100, (value / maxValue) * 100);
  // Muted gray-blue to vibrant blue
  // 0%: #64748b (slate-500), 100%: #3b82f6 (blue-500)
  const r = Math.round(100 - (100 - 59) * (percent / 100));
  const g = Math.round(116 - (116 - 130) * (percent / 100));
  const b = Math.round(139 + (246 - 139) * (percent / 100));
  return `rgb(${r}, ${g}, ${b})`;
}

// Memory color based on absolute bytes (same for RAM and GPU)
function getMemoryColor(bytes) {
  if (!bytes) return null;
  const mb = bytes / (1024 * 1024);
  if (mb < 100) return '#4a7a5a';      // dim sage
  if (mb < 512) return '#5a9a5a';      // muted green
  if (mb < 1024) return '#7ab856';     // soft lime
  if (mb < 2048) return '#eed535';     // yellow
  if (mb < 4096) return '#ffb020';     // amber
  if (mb < 8192) return '#ff8830';     // orange
  if (mb < 16384) return '#ff5540';    // coral
  return '#ff3030';                     // red
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
      { data: [], backgroundColor: [], borderWidth: 0, borderRadius: 2 },
      { type: 'line', data: [], showLine: false, pointStyle: 'line', pointRadius: 10, pointHoverRadius: 10,
        pointBorderWidth: 2, pointBorderColor: '#fff', pointHoverBorderColor: '#fff' },
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
    data: { labels: [], datasets: [{ data: [], backgroundColor: getCssColor('--color-download'), borderWidth: 0, borderRadius: 2 }] },
    tooltip: { displayColors: false, callbacks: { label: (ctx) => ctx.parsed.y.toFixed(0) + ' RPM' } },
    xAxis: CHART_X_AXIS_BAR,
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

function abbreviateSensorLabel(label) {
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
