// Load threshold constant
const LOAD_WARN_THRESHOLD = 0.7;

// Grid layout
const DEFAULT_MAX_COLUMNS = 3;

// Default ping targets (fallback when config.json doesn't define ping_targets)
const DEFAULT_PING_TARGETS = [
  { address: '8.8.8.8', label: 'WAN' },
  { address: '192.168.1.1', label: 'LAN' }
];

// Capture state factory (shared by network and disk capture)
function createCaptureState({ name, timestampId, headerSelector, durationFn }) {
  const state = {
    capturing: false, error: false, captureStart: null, captureInterval: null, lastCaptureTime: null,
    start() {
      state.clearError();
      state.capturing = true;
      state.captureStart = Date.now();
      function tick() {
        const el = document.getElementById(timestampId);
        const hdr = document.querySelector(headerSelector);
        if (el) {
          const elapsed = Math.floor((Date.now() - state.captureStart) / 1000);
          el.textContent = `${elapsed}s/${durationFn()}s`;
        }
        if (hdr) hdr.classList.add('capturing');
      }
      tick();
      state.captureInterval = setInterval(tick, 200);
    },
    stop() {
      state.capturing = false;
      state.captureStart = null;
      if (state.captureInterval) { clearInterval(state.captureInterval); state.captureInterval = null; }
      const hdr = document.querySelector(headerSelector);
      if (hdr) hdr.classList.remove('capturing');
    },
    setError() {
      state.error = true;
      const hdr = document.querySelector(headerSelector);
      if (hdr) hdr.classList.add('error');
    },
    clearError() {
      state.error = false;
      const hdr = document.querySelector(headerSelector);
      if (hdr) hdr.classList.remove('error');
    },
    handleResponse(data, capturedAt = Date.now()) {
      if (data && data.status === 'in_progress') {
        if (!state.capturing) state.start();
        return;
      }
      if (state.capturing) state.stop();
      if (data && data.error) {
        console.warn(name + ' processes error:', data.error, data.detail || '');
        state.setError();
        return;
      }
      state.clearError();
      if (data && Array.isArray(data)) {
        state.lastCaptureTime = capturedAt;
        renderUnifiedProcesses();
      }
    },
    getTimestamp() {
      if (state.capturing && state.captureStart) {
        const elapsed = Math.floor((Date.now() - state.captureStart) / 1000);
        return `${elapsed}s/${durationFn()}s`;
      }
      if (state.lastCaptureTime) return formatTimeAgo(state.lastCaptureTime);
      return '--';
    }
  };
  return state;
}

const networkCapture = createCaptureState({
  name: 'Network', timestampId: 'network-timestamp',
  headerSelector: '.network-header-group', durationFn: () => nethogsDuration
});
const diskCapture = createCaptureState({
  name: 'Disk', timestampId: 'disk-timestamp',
  headerSelector: '.disk-header-group', durationFn: () => diskDuration
});

// Unified processes table state
let processSort = { key: 'cpu_percent', dir: 'desc' };
let lastGpuProcesses = []; // Cache GPU process data for joining
let lastGpuProcessesTime = null; // Track when GPU data was last fetched

// Update process table max-height based on process count
function updateProcessTableHeight() {
  const rowHeight = 29;
  const headerHeight = 60;
  const calculatedHeight = headerHeight + (topProcessesLimit * rowHeight) + 20;
  const finalHeight = Math.max(200, Math.min(800, calculatedHeight));
  document.documentElement.style.setProperty('--table-max-height-large', finalHeight + 'px');
}

// GPU data TTL in milliseconds (30 seconds)
const GPU_PROCESS_TTL = 30000;

// Factory for rate columns (network/disk I/O)
// Note on fallback patterns:
// - getValue uses ?? 0 (nullish coalescing) for sorting, since 0 is a valid rate
// - render uses !== undefined check to distinguish "no data" from "0 rate"
// - color follows same pattern as render
function createRateColumn(key, label, field, width, group) {
  return {
    key,
    label,
    group,
    sortable: true,
    width,
    getValue: (p) => p[field] ?? 0,
    render: (p) => p[field] !== undefined ? formatNetworkRate(p[field]) : '',
    color: (p) => p[field] !== undefined ? getNetworkRateColor(p[field]) : null,
    cssClass: 'num-cell'
  };
}

// Get column indices for a given group (e.g., 'network', 'disk', 'gpu')
function getColumnIndicesByGroup(group) {
  return PROCESS_COLUMNS
    .map((col, i) => col.group === group ? i : -1)
    .filter(i => i >= 0);
}

// Status code descriptions for tooltips
const STATUS_LABELS = {
  'R': 'Running - actively executing',
  'S': 'Sleeping - waiting for event',
  'I': 'Idle - kernel thread',
  'D': 'Disk sleep - uninterruptible I/O (may be stuck)',
  'T': 'Stopped - by signal',
  't': 'Tracing - being debugged',
  'Z': 'Zombie - dead but not reaped',
  'X': 'Dead'
};

// Process table column definitions (extensible - add new columns here)
// Each column can have a 'group' property for grouped header controls
// Order: Process, Path, CPU, Mem, GPU Mem, Network, Disk, CPU Time, Process Info
const PROCESS_COLUMNS = [
  {
    key: 'name',
    label: 'Process',
    group: null,
    sortable: true,
    width: '150px',
    getValue: (p) => p.display_name || p.name || '',
    render: (p) => {
      const name = p.display_name || p.name || '';
      return `<span data-tip="${escapeHtml(name)}">${escapeHtml(name)}</span>`;
    },
    cssClass: 'name-cell'
  },
  {
    key: 'path',
    label: 'Path',
    group: null,
    sortable: false,
    width: 'auto',
    getValue: (p) => p.display_path || p.path || '',
    render: (p) => {
      const path = p.display_path || p.path || '';
      return `<span data-tip="${escapeHtml(path)}">${escapeHtml(path)}</span>`;
    },
    cssClass: 'path-cell'
  },
  {
    key: 'cpu_percent',
    label: 'CPU',
    group: null,
    sortable: true,
    width: '70px',
    getValue: (p) => p.cpu_percent || 0,
    render: (p) => (p.cpu_percent || 0).toFixed(1) + '%',
    color: (p) => getCoreColor(Math.min(p.cpu_percent || 0, 100)),
    cssClass: 'num-cell'
  },
  {
    key: 'memory_rss',
    label: 'Mem',
    group: null,
    sortable: true,
    width: '80px',
    getValue: (p) => p.memory_info?.rss || 0,
    render: (p) => {
      const rss = p.memory_info?.rss;
      if (!rss) return '';
      return formatBytes(rss);
    },
    color: (p) => getMemoryColor(p.memory_info?.rss),
    cssClass: 'num-cell'
  },
  {
    key: 'gpu_memory_mib',
    label: 'GPU',
    group: null,
    sortable: true,
    width: '90px',
    getValue: (p) => p.gpu_memory_mib || 0,
    render: (p) => {
      if (!p.gpu_memory_mib) return '';
      // Hover names the GPU(s) this process holds memory on; the cell is the
      // sum over them (gpu_indices is aggregated per pid in fetchDataOnce).
      const gpus = [...new Set(p.gpu_indices ?? [p.gpu_index])].filter(i => i != null).sort((a, b) => a - b);
      const tip = gpus.length ? `GPU ${gpus.join(', ')}` : 'GPU';
      return `<span data-tip="${escapeHtml(tip)}">${formatBytes(p.gpu_memory_mib * 1024 * 1024)}</span>`;
    },
    color: (p) => getMemoryColor(p.gpu_memory_mib * 1024 * 1024),  // MiB to bytes
    cssClass: 'num-cell'
  },
  {
    key: 'gpu_util_percent',
    label: 'GPU %',
    group: null,
    sortable: true,
    width: '60px',
    getValue: (p) => p.gpu_util_percent ?? -1,
    // SM utilization from nvidia-smi pmon, summed over the process's GPUs; the
    // hover lists each GPU's own figure. Empty when the driver had no sample for
    // this pid in the window (an idle process): unknown, not 0.
    render: (p) => {
      if (p.gpu_util_percent == null) return '';
      const per = (p.gpu_utils ?? []).filter(u => u.sm != null).map(u => `GPU ${u.gpu}: ${u.sm}% SM, ${u.mem ?? '-'}% mem bandwidth`);
      return `<span data-tip="${escapeHtml(per.join('; ') || 'SM utilization')}">${p.gpu_util_percent.toFixed(0)}%</span>`;
    },
    color: (p) => p.gpu_util_percent == null ? null : getCoreColor(Math.min(p.gpu_util_percent, 100)),
    cssClass: 'num-cell'
  },
  createRateColumn('net_up', 'Up', 'net_up', '98px', 'network'),
  createRateColumn('net_down', 'Down', 'net_down', '98px', 'network'),
  createRateColumn('disk_read', 'Read', 'disk_read', '93px', 'disk'),
  createRateColumn('disk_write', 'Write', 'disk_write', '93px', 'disk'),
  {
    key: 'cpu_time_user',
    label: 'User',
    group: 'cpu_time',
    sortable: true,
    width: '70px',
    getValue: (p) => p.cpu_times?.user || 0,
    render: (p) => {
      const val = p.cpu_times?.user;
      if (val === undefined) return '';
      if (val < 1000) return val.toFixed(0) + 's';
      return (val / 1000).toFixed(1) + ' Ks';
    },
    color: (p) => getInfoColor(p.cpu_times?.user, 10000),
    cssClass: 'num-cell'
  },
  {
    key: 'cpu_time_system',
    label: 'Sys',
    group: 'cpu_time',
    sortable: true,
    width: '70px',
    getValue: (p) => p.cpu_times?.system || 0,
    render: (p) => {
      const val = p.cpu_times?.system;
      if (val === undefined) return '';
      if (val < 1000) return val.toFixed(0) + 's';
      return (val / 1000).toFixed(1) + ' Ks';
    },
    color: (p) => getInfoColor(p.cpu_times?.system, 10000),
    cssClass: 'num-cell'
  },
  {
    key: 'num_threads',
    label: 'Thr',
    group: 'process_info',
    sortable: true,
    width: '40px',
    getValue: (p) => p.num_threads || 0,
    render: (p) => p.num_threads || '',
    color: (p) => getInfoColor(p.num_threads, 100),
    cssClass: 'num-cell process-info-cell'
  },
  {
    key: 'status',
    label: 'State',
    group: 'process_info',
    sortable: true,
    width: '45px',
    getValue: (p) => p.status || '',
    render: (p) => {
      const s = p.status || '';
      return `<span data-tip="${escapeHtml(STATUS_LABELS[s] || s)}">${escapeHtml(s)}</span>`;
    },
    color: (p) => {
      const s = p.status;
      if (s === 'R') return '#22c55e';  // green - running
      if (s === 'Z' || s === 'X') return '#ef4444';  // red - problem
      if (s === 'D' || s === 'T' || s === 't') return '#f59e0b';  // orange - attention
      return null;  // S, I - no color (gray text)
    },
    cssClass: 'num-cell process-info-cell'
  },
  {
    key: 'username',
    label: 'Owner',
    group: 'process_info',
    sortable: true,
    width: '60px',
    getValue: (p) => p.username || '',
    render: (p) => {
      const user = p.username || '';
      return `<span data-tip="${escapeHtml(user)}">${escapeHtml(user)}</span>`;
    },
    cssClass: 'name-cell process-info-cell'
  }
];

// Get CSS class for load value based on core count
function getLoadClass(val, cores) {
  if (val >= cores) return 'load-crit';
  if (val >= cores * LOAD_WARN_THRESHOLD) return 'load-warn';
  return 'load-ok';
}

// Set the grid column count. The `.grid` spans ~90% of the available width in
// CSS; this just sets how many equal columns fill it (via the --grid-columns
// custom property that grid-template-columns reads).
function updateGridColumns(grid, maxCols) {
  grid.style.setProperty('--grid-columns', maxCols);
}

// Initialize max columns control
function initMaxColumns() {
  const btn = document.getElementById('max-columns');
  const grid = document.querySelector('.grid');
  const saved = localStorage.getItem('maxColumns');
  let maxCols = saved ? parseInt(saved) : DEFAULT_MAX_COLUMNS;

  btn.textContent = maxCols;
  updateGridColumns(grid, maxCols);

  btn.addEventListener('click', () => {
    createDropdownPicker(btn, {
      options: [1, 2, 3, 4, 5, 6].map(n => ({ value: n, label: String(n) })),
      currentValue: maxCols,
      onSelect: (val) => {
        maxCols = val;
        btn.textContent = val;
        localStorage.setItem('maxColumns', val);
        updateGridColumns(grid, val);
      },
      custom: { type: 'int', min: 1, max: 32, placeholder: 'cols' },
    });
  });
}

// Fill history arrays with nulls for gap periods
function fillHistoryGaps(count) {
  const arrays = [
    history.cpu, history.mem, history.swap,
    history.netIn, history.netOut,
    history.diskRead, history.diskWrite,
    history.cpuCoresMin, history.cpuCoresMedian, history.cpuCoresMax,
    ...(history.pings || [])
  ];

  for (let i = 0; i < count; i++) {
    // Gaps are unmeasured, so don't invent sample times for them. An in-flight
    // poll may have finished during the pause and already added a real sample.
    arrays.forEach(arr => pushHistory(arr, null, null));

    // Mark the slot as "not measured" so the ping loss band doesn't render the
    // gap as a 100%-loss outage.
    pushHistory(history.pingGap, true, null);

    // GPU and per-drive disk arrays (dynamic)
    [...history.gpus, ...history.gpuMems, ...history.diskDriveReads, ...history.diskDriveWrites].forEach(arr => {
      pushHistory(arr, null, null);
    });
  }
  updatePingLossBand();
}

// Render functions for cached cards
function renderGpuInfo(data) {
  const gpuInfoCard = document.getElementById('gpu-info-card');
  const gpuInfoContent = document.getElementById('gpu-info-content');
  gpuInfoCard.style.display = '';

  // /api/gpu-info is always a list: empty when nvidia-smi is missing or failed.
  if (data && Array.isArray(data) && data.length > 0) {
    gpuInfoContent.innerHTML = data.map(g => {
      const shortName = escapeHtml(abbreviateGpuName(g.name));
      const utilization = Number.isFinite(g.utilization) ? Math.round(g.utilization) : '--';
      const temperature = Number.isFinite(g.temperature) ? Math.round(g.temperature) : '--';
      const memUsedGiB = Number.isFinite(g.memory_used) ? (g.memory_used / 1024).toFixed(1) : '--';
      const memTotalGiB = Number.isFinite(g.memory_total) ? (g.memory_total / 1024).toFixed(1) : '--';
      const powerStr = g.power_draw != null && g.power_limit != null
        ? `<span>${Math.round(g.power_draw)} / ${Math.round(g.power_limit)} W</span>` : '';
      return `<div class="info-item">
        <div class="info-name">GPU ${g.index}: ${shortName}</div>
        <div class="info-details">
          <span>Util: ${utilization}%</span>
          <span>Temp: ${temperature}°C</span>
          ${powerStr}
        </div>
        <div class="info-details">
          <span>Mem: ${memUsedGiB} / ${memTotalGiB} GB</span>
          <span>Driver: ${escapeHtml(g.driver_version)}</span>
          ${g.cuda_version ? `<span>CUDA: ${escapeHtml(g.cuda_version)}</span>` : ''}
        </div>
      </div>`;
    }).join('');
    updateCardNoData('gpu-info', true);
  } else {
    // No GPUs detected (empty array)
    gpuInfoContent.innerHTML = '<div class="info-item">No GPUs detected</div>';
    updateCardNoData('gpu-info', true);
  }
  updateCacheDisplay('gpu-info', 'gpuInfo');
}

function renderCpuInfo(data) {
  const cpuInfoCard = document.getElementById('cpu-info-card');
  const cpuInfoContent = document.getElementById('cpu-info-content');
  if (data && data.model) {
    cpuInfoCard.style.display = '';
    const cache = data.l3_cache ? `L3: ${escapeHtml(data.l3_cache)}` :
                  data.cache_size ? `Cache: ${escapeHtml(data.cache_size)}` : '';
    cpuInfoContent.innerHTML = `<div class="info-item">
      <div class="info-name">${escapeHtml(data.model)}</div>
      <div class="info-details">
        <span>${data.physical_cores} cores / ${data.logical_cores} threads</span>
        ${cache ? `<span>${cache}</span>` : ''}
      </div>
    </div>`;
    updateCardNoData('cpu-info', true);
  } else {
    updateCardNoData('cpu-info', true);   // hidden, not failed: must not count as "Partial"
    cpuInfoCard.style.display = 'none';
  }
  updateCacheDisplay('cpu-info', 'cpuInfo');
}

function renderSystemInfo(data, uptimeStr) {
  const systemInfoContent = document.getElementById('system-info-content');
  if (data) {
    const hostname = escapeHtml(data.hostname || 'Unknown');
    const osName = escapeHtml(data.linux_distro || data.os_name || 'Unknown OS');
    const kernel = escapeHtml(data.os_version || '');
    systemInfoContent.innerHTML = `<div class="info-item">
      <div class="system-info-hostname">${hostname}</div>
      <div class="info-details system-info-details">
        <span>${osName}</span>
        ${kernel ? `<span>Kernel: ${kernel}</span>` : ''}
      </div>
      <div class="system-info-uptime">Uptime: ${escapeHtml(uptimeStr || '--')}</div>
    </div>`;
    updateCardNoData('system-info', true);
  }
  updateCacheDisplay('system-info', 'systemInfo');
}

function renderIpInfo(data) {
  const content = document.getElementById('ip-info-content');
  if (!data || (!data.lan?.length && !data.public?.ipv4 && !data.tailscale?.ipv4)) {
    content.innerHTML = '<div class="info-item">No IP data available</div>';
    updateCardNoData('ip-info', false);
    updateCacheDisplay('ip-info', 'ipInfo');
    return;
  }

  function ipSpan(addr) {
    return `<span class="ip-copyable" data-ip="${escapeHtml(addr)}" title="Click to copy">${escapeHtml(addr)}</span>`;
  }

  function ipLines(label, addr) {
    if (!addr) return '';
    if (Array.isArray(addr)) return addr.map(a => ipLines(label, a)).join('');
    return `<div class="ip-line"><span class="ip-label">${escapeHtml(label)}</span>${ipSpan(addr)}</div>`;
  }

  function ipSection(name, v4, v6) {
    const lines = ipLines('IPv4', v4) + ipLines('IPv6', v6);
    if (!lines) return '';
    return `<div class="info-item"><div class="info-name">${escapeHtml(name)}</div>${lines}</div>`;
  }

  let html = '';

  // LAN interfaces
  if (data.lan && data.lan.length > 0) {
    html += data.lan.map(iface =>
      ipSection(`LAN (${iface.interface})`, iface.ipv4, iface.ipv6)
    ).join('');
  }

  // Public
  if (data.public) {
    if (data.public.error) {
      html += `<div class="info-item"><div class="info-name">Public</div>` +
        `<div class="ip-line"><span class="text-warning">${escapeHtml(data.public.error)}</span></div></div>`;
    } else {
      html += ipSection('Public', data.public.ipv4, data.public.ipv6);
    }
  }

  // Tailscale
  if (data.tailscale) {
    html += ipSection('Tailscale', data.tailscale.ipv4, data.tailscale.ipv6);
  }

  content.innerHTML = html;
  updateCardNoData('ip-info', true);
  updateCacheDisplay('ip-info', 'ipInfo');
}

// System Mail card. `data` is the HTTP 200 body of /api/mail: `status` is
// ok / missing / error, `messages` the newest 10 (subject + date only; the full
// text is read with mail(1) on the host). Alarms live in the Hardware card, so
// this card never turns orange.
function renderMail(data) {
  const content = document.getElementById('mail-content');
  const messages = Array.isArray(data?.messages) ? data.messages : [];
  const count = Number.isFinite(data?.count) ? data.count : messages.length;

  let html = '';
  let errored = false;

  if (data?.status === 'missing') {
    // Expected before the MTA is set up: not an error, nothing to alert on.
    html = `<div class="info-item mail-muted">No mail spool yet (${escapeHtml(data.path || '')})</div>`;
  } else if (data?.status === 'error') {
    // A malformed message is reported, never skipped: the spool itself is still
    // readable with mail(1), so say how.
    const detail = data.detail ? `: ${escapeHtml(data.detail)}` : '';
    html = `<div class="info-item text-error">Cannot read ${escapeHtml(data.path || 'the mail spool')}${detail}</div>` +
      '<div class="info-item mail-muted">read it directly: mail(1) on the server</div>';
    errored = true;
  } else if (count === 0) {
    html = '<div class="info-item text-success">No mail</div>';
  } else {
    // Readable spool. Any message the server could not parse is kept as a red row
    // (never dropped); if there are any, the whole card also goes red and the count
    // line says how many — but every readable message and the full count still show.
    // Goal: obvious, nothing hidden.
    const unreadable = Number.isFinite(data?.unreadable)
      ? data.unreadable
      : messages.filter(m => m.error).length;
    const lines = messages.map(m => {
      const when = m.date_epoch != null ? new Date(m.date_epoch * 1000).toLocaleString() : 'unknown date';
      return `<div class="mail-line${m.error ? ' text-error' : ''}"><span class="mail-date">${escapeHtml(when)}</span>` +
        `<span class="mail-subject">${escapeHtml(m.subject)}</span></div>`;
    }).join('');
    const countLine = `${count} message${count === 1 ? '' : 's'}` +
      (unreadable ? ` — ${unreadable} unreadable` : '');
    html = `<div class="info-item">
      <div class="${unreadable ? 'text-error' : ''}">${countLine}</div>
      ${lines}
      <div class="mail-line mail-muted">read the full text: mail(1) on the server</div>
    </div>`;
    if (unreadable > 0) errored = true;
  }

  content.innerHTML = html;
  // Flag the card red (and Partial in the header) on a real unreadable spool OR any
  // message the server could not parse — surfacing failures is the whole point.
  updateCardNoData('mail', !errored);
  // On a readable spool the auto "No data for Ns" corner label is false (the card
  // has data); replace it with the unreadable count.
  if (errored && data?.status !== 'error') {
    const nd = document.querySelector('[data-card-id="mail"] .no-data-text');
    if (nd) nd.textContent = `${messages.filter(m => m.error).length} unreadable`;
  }
  updateCacheDisplay('mail', 'mail');
}

// Hardware card. `data` is the HTTP 200 body of /api/storage-health: the
// collector's state.json (collector/health-collect.sh)
// merged with `status` (ok / stale / missing / error), `detail` and `age_sec`.
// The alarm rules live in the collector only; this prints its `alarms` /
// `info` lists and one compact line per raw section. Any section may be absent.
// The 60 s sensors snapshot's `alarms` (fans, BMC thresholds, GPU thermals;
// collector/health-sensors.sh) are printed and counted with them, read from the
// sensors cache that fetchSlowData fills just before this runs.
function renderHardware(data) {
  const content = document.getElementById('hardware-content');
  const collector = 'horatio-collect.timer';

  // Both non-snapshot branches reset what the last good snapshot may have set:
  // the red border, the "N issues" header and the snapshot age.
  const reset = () => {
    updateCardCritical('hardware', false);
    const valueEl = document.getElementById('hardware-value');
    valueEl.textContent = '--';
    valueEl.className = 'value';
    document.querySelector('#hardware-card .cache-time').textContent = '';
  };
  if (data?.status === 'missing') {
    // Expected before the housekeeping installer has run: not a failure.
    content.innerHTML = `<div class="info-item mail-muted">No health snapshot yet (${escapeHtml(data.path || '')}); ` +
      `is ${collector} installed?</div>`;
    updateCardNoData('hardware', true);
    updateCardWarning('hardware', 0, 0);
    reset();
    return;
  }
  if (data?.status === 'error') {
    const detail = data.detail ? `: ${escapeHtml(data.detail)}` : '';
    content.innerHTML = `<div class="info-item text-error">Cannot read the ${collector} snapshot${detail}</div>`;
    updateCardNoData('hardware', false);
    reset();
    return;
  }

  const ageText = snapshotAgeText(data);
  const stale = data.status === 'stale';
  // The sensors alarms count only from a fresh snapshot (same 5 min rule as the
  // fans / temps / power cards). Stale, missing, errored or never fetched means
  // its alarm list is unknown, not clear: flagged like a stale state.json.
  const sen = getFreshSnapshot('sensors', 300000);
  const sensorsUnknown = sen?.status !== 'ok';
  const alarms = [...(Array.isArray(data?.alarms) ? data.alarms : []),
                  ...(!sensorsUnknown && Array.isArray(sen.alarms) ? sen.alarms : [])];
  const info = Array.isArray(data?.info) ? data.info : [];

  // Header: the verdict, and the snapshot's OWN age (not when it was fetched).
  const valueEl = document.getElementById('hardware-value');
  valueEl.textContent = stale ? 'stale' : alarms.length ? `${alarms.length} issue${alarms.length === 1 ? '' : 's'}` :
    sensorsUnknown ? 'sensors stale' : 'all clear';
  valueEl.className = 'value ' + (stale ? 'text-warning' : alarms.length ? 'text-error' : sensorsUnknown ? 'text-warning' : 'text-success');
  document.querySelector('#hardware-card .cache-time').textContent = ageText;

  // Issues: alarms in red, then the collector's info notes in grey. A stale
  // snapshot means the alarm list is unknown, not clear: never say "no alarms".
  // Drive-specific notes ("sdb: no self-test on record") are on Drive Health.
  const issues = [];
  if (stale) issues.push(hwRow('text-warning', `${ageText} (stale); is ${collector} running?`));
  if (sensorsUnknown) {
    const why = sen ? `sensors snapshot ${sen.status}${sen.age_sec != null ? ` (${Math.round(sen.age_sec / 60)} min old)` : ''}` : 'no fresh sensors snapshot';
    issues.push(hwRow('text-warning', `${why}: fan / GPU alarms unknown; is horatio-sensors.timer running?`));
  }
  alarms.forEach(a => {
    issues.push(hwRow('text-error', a));
    const hint = hwHint(a, data);
    if (hint) issues.push(hwRow('hw-note', `check: ${hint}`));
  });
  if (!stale && !sensorsUnknown && alarms.length === 0) issues.push(hwRow('text-success', 'no alarms'));
  info.filter(i => !/^(sd[a-z]+|nvme\d+n\d+):/.test(i)).forEach(i => issues.push(hwRow('hw-note', i)));

  const memory = [];
  if (data.edac) {
    const hours = data.edac.uptime_seconds != null ? Math.round(data.edac.uptime_seconds / 3600) : null;
    memory.push(hwRow('', `ECC since boot${hours != null ? ` (${hours} h)` : ''}: ${data.edac.ce_total ?? '?'} corrected, ${data.edac.ue_total ?? '?'} uncorrectable`));
  }
  if (data.sel_memory) memory.push(hwRow('', `BMC log, memory events: ${data.sel_memory.count} lifetime, ${data.sel_memory.recent?.length ?? '?'} in the last 7 days`));
  if (data.sel_hardware) memory.push(hwRow('', `BMC log, hardware faults: ${data.sel_hardware.count} lifetime, ${data.sel_hardware.recent?.length ?? '?'} in the last 7 days`));
  if (memory.length) memory.push(hwRow('hw-note', 'check: sudo ipmitool sel elist; sudo ras-mc-ctl --errors'));

  const backup = [];
  if (data.backup) {
    const b = data.backup;
    backup.push(hwRow('', b.age_hours != null ? `last run ${Math.round(b.age_hours)} h ago` : 'never run'));
    backup.push(hwRow('hw-note', `check: systemctl list-timers ${b.timer}; journalctl --unit ${backupService(b.timer)} --since -2days`));
  }

  content.innerHTML = hwSection('Issues', issues) + hwSection('Memory', memory) + hwSection('Backup', backup) +
    '<div class="hw-note">alarm rules and the full table: collector/README.md</div>';
  // no-data must be cleared first: updateCardWarning bails out (stripping
  // .warning) while .no-data is set, and nothing else ever clears .no-data.
  updateCardNoData('hardware', true);
  // Any alarm is red; a stale snapshot (alarms unknown, not clear) is orange.
  updateCardCritical('hardware', alarms.length > 0);
  updateCardWarning('hardware', stale || sensorsUnknown ? 1 : 0, 0);
}

// Shared by the Hardware health and Drive Health cards.
const hwRow = (cls, text) => `<div class="hw-row${cls ? ' ' + cls : ''}">${escapeHtml(text)}</div>`;
const hwSection = (title, rows) => rows.length ? `<div class="info-item"><div class="hw-heading">${title}</div>${rows.join('')}</div>` : '';
function snapshotAgeText(data) {
  if (data?.age_sec == null) return 'snapshot age unknown';
  const m = Math.round(data.age_sec / 60);
  return m < 1 ? 'snapshot <1 min ago' : `snapshot ${m} min ago`;
}
// Rewrite an element's HTML only when it changed. The poll loop re-renders every
// card every 2 s; replacing the DOM under the cursor kills the browser's native
// `title` tooltips before their hover delay elapses. Compare the source string,
// not el.innerHTML (its serialisation differs), and make this the ONLY writer of
// the element, or a direct write would be missed and the next equal string skipped.
const lastHtml = new Map();
function setHtml(el, html) {
  if (lastHtml.get(el.id) === html) return;
  lastHtml.set(el.id, html);
  el.innerHTML = html;
}

// Power bar segment colours, shared by the CSS classes (style.css .power-seg-*) and the legend text.
function segColorVar(label) {
  const key = label.toLowerCase().replaceAll(' ', '');
  return { gpu0: '--color-gpu', gpu1: '--color-gpu-mem', cpu0: '--color-accent', cpu1: '--color-download',
           dram0: '--color-success', dram1: '--color-warning' }[key] || '--text-muted';
}

// What to run for an alarm line, keyed by how the collector's alarm strings start.
// Mirrors the alarm table in collector/README.md; first match wins.
const HW_HINTS = [
  [/^SEL: .*memory/, 'sudo ipmitool sel elist | grep -i ecc'],
  [/^SEL: /, 'sudo ipmitool sel elist'],
  [/^ECC: /, 'sudo ras-mc-ctl --errors'],
  [/^(sd[a-z]+|nvme\d+n\d+)[ :(]/, m => `sudo smartctl --all /dev/${m[1]}`],
  [/^(md\d+): /, m => `cat /proc/mdstat; sudo mdadm --detail /dev/${m[1]}`],
  // The backup unit is whatever timer the collector's config names (state.json backup.timer).
  [/^backup: /, (m, data) => `journalctl --unit ${backupService(data?.backup?.timer)} --since -2days`],
  [/^journal: /, 'journalctl --priority err --since -24hours'],
  [/^unit failed: (\S+)/, m => `systemctl status ${m[1]}`],
  [/^collector: /, 'systemctl status horatio-collect.service horatio-sensors.service'],
  [/^fans: |: status /, 'sudo ipmitool sdr type fan; sudo ipmitool sdr type temperature'],
  [/^GPU \d+: /, 'nvidia-smi -q -d TEMPERATURE,PERFORMANCE'],
];
function hwHint(alarm, data) {
  for (const [re, hint] of HW_HINTS) {
    const m = alarm.match(re);
    if (m) return typeof hint === 'function' ? hint(m, data) : hint;
  }
  return '';
}
// "system-backup.timer" -> "system-backup.service" (the oneshot the timer fires).
function backupService(timer) {
  return (timer || 'backup.timer').replace(/\.timer$/, '.service');
}

// The sensors snapshot's own age: the collector's age_sec at fetch time plus the time since.
function sensorsAgeText() {
  const c = dataCache.sensors;
  if (!c?.data || c.data.age_sec == null || !c.lastUpdate) return '';
  return `sensors ${formatTimeAgo(c.lastUpdate - c.data.age_sec * 1000)}`;
}

// Drive Health card: one row per PHYSICAL drive from the same snapshot, with what
// it holds (a partition's mount, or "md0 -> /mnt/storage" for an array member),
// then the arrays. Mounts are on Drive Usage; this is the SMART side.
function renderDriveHealth(data) {
  const content = document.getElementById('drive-health-content');
  const collector = 'horatio-collect.timer';
  const ageEl = document.querySelector('#drive-health-card .cache-time');

  if (data?.status === 'missing') {
    setHtml(content, `<div class="info-item mail-muted">No health snapshot yet; is ${collector} installed?</div>`);
    ageEl.textContent = '';
    updateCardNoData('drive-health', true);
    updateCardWarning('drive-health', 0, 0);
    updateCardCritical('drive-health', false);
    return;
  }
  if (data?.status === 'error') {
    setHtml(content, `<div class="info-item text-error">Cannot read the ${collector} snapshot${data.detail ? `: ${escapeHtml(data.detail)}` : ''}</div>`);
    ageEl.textContent = '';
    updateCardNoData('drive-health', false);
    updateCardCritical('drive-health', false);
    return;
  }

  const info = Array.isArray(data?.info) ? data.info : [];
  const stale = data.status === 'stale';
  ageEl.textContent = snapshotAgeText(data) + (stale ? ' (stale)' : '');
  let bad = 0;

  // Usage of what a drive holds comes from the Glances filesystem rows (Drive
  // Usage's data), matched by mount point; an md member shows its array's usage.
  const fsRows = Array.isArray(getCache('drives')) ? getCache('drives') : [];
  const usageOf = holds => {
    const mounts = new Set(holds.map(h => h.replace(/^.* -> /, '')));
    const rows = fsRows.filter(f => mounts.has(f.mnt_point));
    const size = rows.reduce((s, f) => s + (f.size || 0), 0);
    const used = rows.reduce((s, f) => s + (f.used || 0), 0);
    return size > 0 ? Math.round(used / size * 100) : null;
  };
  const gb = bytes => bytes >= 1e12 ? `${(bytes / 1e12).toFixed(1)} TB` : `${Math.round(bytes / 1e9)} GB`;
  const pill = (cls, text) => `<span class="hw-pill ${cls}">${escapeHtml(text)}</span>`;

  const drives = (Array.isArray(data.drives) ? data.drives : []).map(d => {
    let verdict;
    if (d.standby) verdict = pill('hw-pill-muted', 'asleep');
    else if (d.error) { verdict = pill('hw-pill-bad', 'error'); bad++; }
    else if (d.passed === true) verdict = pill('hw-pill-ok', 'passed');
    else if (d.passed === false) { verdict = pill('hw-pill-bad', 'FAILED'); bad++; }
    else verdict = pill('hw-pill-muted', 'unknown');
    const holds = Array.isArray(d.holds) ? d.holds : [];
    const tripWarn = d.drive_trip_c != null ? d.drive_trip_c - 5 : 55;
    const temp = d.temperature_c != null
      ? `<span class="${d.temperature_c >= tripWarn ? 'text-bad' : ''}">${d.temperature_c} °C</span>` : '';
    const pct = usageOf(holds);
    const usage = pct != null
      ? `<div class="drive-usage-mini"><div class="drive-usage-bar-container"><div class="drive-usage-bar" style="width:${pct}%;background:${getDriveUsageColor(pct)}"></div></div><span>${pct}%</span></div>`
      : `<span class="mail-muted">${holds.length ? '' : 'no mounts'}</span>`;
    const model = [d.model, d.size_bytes ? gb(d.size_bytes) : ''].filter(Boolean).join(' · ');
    // Second line: what it holds, grown defects (SAS only, red past the config baseline),
    // and the collector's notes about this drive ("no self-test on record").
    const sub = [];
    if (holds.length) sub.push(escapeHtml(holds.join(', ')));
    if (d.grown_defect_list != null) {
      // SAS: sectors remapped since the factory, against the baseline recorded in the
      // collector's config; the alarm (and the red) is growth past that baseline.
      const base = d.grown_defect_baseline;
      const over = base != null && d.grown_defect_list > base;
      sub.push(`<span class="${over ? 'text-bad' : ''}">${d.grown_defect_list} grown defects (${
        over ? `+${d.grown_defect_list - base} over baseline ${base}` : `baseline ${base ?? '?'}`})</span>`);
    }
    info.filter(i => i.startsWith(`${d.device}: `) && !i.endsWith(': standby'))
      .forEach(i => sub.push(escapeHtml(i.slice(d.device.length + 2))));
    if (d.error) sub.push(escapeHtml(d.error));
    return `<div class="drive-tile" title="${escapeHtml(d.serial ? `serial ${d.serial}` : '')}">` +
      `<div class="drive-tile-main">` +
        `<span class="drive-tile-dev">${escapeHtml(d.device)}${d.kind ? `<span class="drive-kind">${escapeHtml(d.kind)}</span>` : ''}</span>` +
        `<span class="drive-tile-model" title="${escapeHtml(model)}">${escapeHtml(model)}</span>` +
        `<span>${temp}</span><span>${usage}</span><span>${verdict}</span>` +
      `</div>` +
      (sub.length ? `<div class="drive-tile-sub">${sub.join(' · ')}</div>` : '') +
      `</div>`;
  });

  const arrays = (Array.isArray(data.md) ? data.md : []).map(a => {
    const degraded = a.degraded != null && String(a.degraded) !== '0';
    if (degraded) bad++;
    let state = a.array_state ?? 'unknown';
    if (degraded) state += `, ${a.degraded} missing`;
    if (a.sync_action && a.sync_action !== 'idle') state += `, ${a.sync_action}`;
    const members = (Array.isArray(a.members) ? a.members : []).map(m => {
      const mstate = Array.isArray(m.state) ? m.state.join('+') : (m.state ?? '?');
      const errs = m.errors != null && String(m.errors) !== '0' ? ` errors ${m.errors}` : '';
      return `${m.name} ${mstate}${errs}`;
    }).join(', ');
    const ok = !degraded && (a.array_state === 'clean' || a.array_state === 'active');
    return `<div class="hw-row"><span class="drive-tile-dev">${escapeHtml(a.name)}</span> ${escapeHtml(a.level ?? '')} ` +
      pill(ok ? 'hw-pill-ok' : 'hw-pill-bad', state) + (members ? ` <span class="hw-holds">${escapeHtml(members)}</span>` : '') + '</div>';
  });

  setHtml(content, (drives.length ? `<div class="hw-heading">Drives</div>${drives.join('')}` : '') +
    hwSection('Arrays', arrays) ||
    '<div class="info-item mail-muted">no drives in the snapshot</div>');
  updateCardNoData('drive-health', true);
  // Bad drive or degraded array: red. Stale snapshot: orange.
  updateCardCritical('drive-health', bad > 0);
  updateCardWarning('drive-health', stale ? 1 : 0, 0);
}

// Click-to-copy handler for IP addresses
function copyToClipboard(text) {
  if (navigator.clipboard) {
    return navigator.clipboard.writeText(text);
  }
  // Fallback for non-secure contexts (HTTP)
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  return Promise.resolve();
}

document.addEventListener('click', (e) => {
  const el = e.target.closest('.ip-copyable');
  if (!el) return;
  const ip = el.dataset.ip;
  if (!ip) return;
  copyToClipboard(ip).then(() => {
    el.classList.add('copied');
    setTimeout(() => el.classList.remove('copied'), 600);
  });
});

function renderDrives(data) {
  const driveUsageCard = document.getElementById('drive-usage-card');
  const driveUsageContent = document.getElementById('drive-usage-content');
  if (data && Array.isArray(data) && data.length > 0) {
    driveUsageCard.style.display = '';
    driveUsageContent.innerHTML = data.map(drive => {
      const usedBytes = drive.used || 0;
      const totalBytes = drive.size || 1;
      const percent = drive.percent || 0;
      const mountPoint = escapeHtml(drive.mnt_point || 'Unknown');
      const color = getDriveUsageColor(percent);
      return `<div class="drive-usage-item">
        <div class="drive-usage-header">
          <span class="drive-usage-mount">${mountPoint}</span>
          <span class="drive-usage-stats">${formatBytes(usedBytes)} / ${formatBytes(totalBytes)}</span>
        </div>
        <div class="drive-usage-bar-container">
          <div class="drive-usage-bar" style="width: ${percent}%; background: ${color};"></div>
        </div>
        <div class="drive-usage-percent">${percent.toFixed(1)}%</div>
      </div>`;
    }).join('');
    updateCardNoData('drive-usage', true);
    const maxDrivePercent = Math.max(...data.map(d => d.percent || 0));
    updateCardWarning('drive-usage', maxDrivePercent, thresholds.disk_percent);
  } else {
    driveUsageCard.style.display = 'none';
  }
  updateCacheDisplay('drive-usage', 'drives');
}

// Helper to enrich a process with data from network, disk, and GPU sources
function enrichProcessData(proc, networkByPid, diskByPid, gpuByPid) {
  const net = networkByPid[proc.pid];
  const disk = diskByPid[proc.pid];
  const gpu = gpuByPid[proc.pid];
  return {
    ...proc,
    net_up: net?.sent_kbps,
    net_down: net?.recv_kbps,
    disk_read: disk?.read_kbps,
    disk_write: disk?.write_kbps,
    gpu_index: gpu?.gpu_index,
    gpu_memory_mib: gpu?.gpu_memory_mib,
    gpu_indices: gpu?.gpu_indices,
    gpu_util_percent: gpu?.gpu_util_percent,
    gpu_utils: gpu?.gpu_utils
  };
}

// Helper to create a base process object from CPU data or fallback values
function createBaseProcess(pid, cpuProc, fallbackName, fallbackPath) {
  return {
    pid,
    name: cpuProc?.name || fallbackName || 'unknown',
    display_name: cpuProc?.display_name || cpuProc?.name || fallbackName || 'unknown',
    path: cpuProc?.path || fallbackPath || '',
    display_path: cpuProc?.display_path || cpuProc?.path || fallbackPath || '',
    cpu_percent: cpuProc?.cpu_percent || 0,
    memory_percent: cpuProc?.memory_percent || 0,
    memory_info: cpuProc?.memory_info || null,
    num_threads: cpuProc?.num_threads || 0,
    status: cpuProc?.status || '',
    username: cpuProc?.username || '',
    cpu_times: cpuProc?.cpu_times || null
  };
}

// Helper to build a PID lookup map from an array of processes
function buildPidMap(arr) {
  const map = {};
  if (Array.isArray(arr)) {
    arr.forEach(p => { if (p.pid) map[p.pid] = p; });
  }
  return map;
}

// Set up event delegation for process table (called once at startup)
// This avoids adding new listeners on every render, preventing memory leaks
function initProcessTableListeners() {
  const processesTable = document.getElementById('processes-table');
  if (!processesTable) return;

  // Event delegation for sortable headers
  processesTable.addEventListener('click', (e) => {
    // Handle sort clicks
    const th = e.target.closest('th.sortable');
    if (th) {
      const key = th.dataset.sortKey;
      if (processSort.key === key) {
        processSort.dir = processSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        processSort.key = key;
        processSort.dir = 'desc';
      }
      renderUnifiedProcesses();
      return;
    }

    // Handle network refresh button
    if (e.target.id === 'network-refresh-btn' || e.target.closest('#network-refresh-btn')) {
      dataCache.networkProcesses.data = null;
      dataCache.networkProcesses.lastUpdate = null;
      fetchSlowData();
      return;
    }

    // Handle disk refresh button
    if (e.target.id === 'disk-refresh-btn' || e.target.closest('#disk-refresh-btn')) {
      dataCache.diskProcesses.data = null;
      dataCache.diskProcesses.lastUpdate = null;
      fetchSlowData();
      return;
    }
  });

  // Event delegation for duration picker triggers
  processesTable.addEventListener('click', (e) => {
    const target = e.target.closest('#nethogs-duration-table');
    if (target) {
      createDropdownPicker(target, {
        options: [1, 3, 5, 10, 30, 60].map(n => ({ value: n, label: n + 's' })),
        currentValue: nethogsDuration,
        onSelect: (val) => {
          nethogsDuration = val;
          localStorage.setItem('nethogsDuration', nethogsDuration);
          target.textContent = val + 's';
        },
        custom: { type: 'int', min: 1, max: 60, placeholder: 'secs' },
      });
      return;
    }

    const diskTarget = e.target.closest('#disk-duration-table');
    if (diskTarget) {
      createDropdownPicker(diskTarget, {
        options: [1, 3, 5].map(n => ({ value: n, label: n + 's' })),
        currentValue: diskDuration,
        onSelect: (val) => {
          diskDuration = val;
          localStorage.setItem('diskDuration', diskDuration);
          diskTarget.textContent = val + 's';
        },
        custom: { type: 'int', min: 1, max: 10, placeholder: 'secs' },
      });
    }
  });
}

function renderUnifiedProcesses() {
  const processesTable = document.getElementById('processes-table');
  const processesValueEl = document.getElementById('processes-value');

  // Get data from all sources
  const processList = getCache('processes') || [];
  const networkProcesses = getCache('networkProcesses') || [];
  const diskProcesses = getCache('diskProcesses') || [];
  // Use GPU data only if it's not stale (within TTL)
  const gpuDataFresh = lastGpuProcessesTime && (Date.now() - lastGpuProcessesTime < GPU_PROCESS_TTL);
  const gpuProcesses = gpuDataFresh ? (lastGpuProcesses || []) : [];

  // Only show "no data" if ALL sources are empty
  const hasAnyData =
    (Array.isArray(processList) && processList.length > 0) ||
    gpuProcesses.length > 0 ||
    (Array.isArray(networkProcesses) && networkProcesses.length > 0) ||
    (Array.isArray(diskProcesses) && diskProcesses.length > 0);

  if (!hasAnyData) {
    processesTable.innerHTML = '<div class="empty-state">No process data</div>';
    updateCardNoData('processes', false);
    return;
  }

  // Build lookup maps by PID
  const processListByPid = buildPidMap(processList);
  const networkByPid = buildPidMap(networkProcesses);
  const diskByPid = buildPidMap(diskProcesses);

  // Aggregate GPU data by PID (handles multi-GPU processes)
  const gpuByPid = {};
  gpuProcesses.forEach(p => {
    if (!p.pid) return;

    if (!gpuByPid[p.pid]) {
      // Create a clean aggregation object with explicit properties
      gpuByPid[p.pid] = {
        pid: p.pid,
        name: p.name,
        process_name: p.process_name,
        path: p.path,
        gpu_indices: [p.gpu_index],
        gpu_memory_mib: p.gpu_memory_mib || 0,
        // SM % summed over GPUs (null until one GPU reports a sample); per-GPU detail for the hover
        gpu_util_percent: p.gpu_util_percent ?? null,
        gpu_utils: [{ gpu: p.gpu_index, sm: p.gpu_util_percent ?? null, mem: p.gpu_mem_util_percent ?? null }]
      };
    } else {
      // Aggregate into existing object
      const agg = gpuByPid[p.pid];
      agg.gpu_memory_mib += p.gpu_memory_mib || 0;
      agg.gpu_indices.push(p.gpu_index);
      if (p.gpu_util_percent != null) agg.gpu_util_percent = (agg.gpu_util_percent ?? 0) + p.gpu_util_percent;
      agg.gpu_utils.push({ gpu: p.gpu_index, sm: p.gpu_util_percent ?? null, mem: p.gpu_mem_util_percent ?? null });
    }
  });

  // True union of all process sources
  // 1. Start with CPU/memory processes (enriched with other data)
  let procs = processList
    .filter(p => p.cpu_percent > 0 || p.memory_percent > 0)
    .map(p => enrichProcessData(p, networkByPid, diskByPid, gpuByPid));

  // Track which PIDs we've already included
  const includedPids = new Set(procs.map(p => p.pid));

  // 2. Add GPU-only processes (not in CPU list)
  gpuProcesses.forEach(gpu => {
    if (gpu.pid && !includedPids.has(gpu.pid)) {
      includedPids.add(gpu.pid);
      const cpuProc = processListByPid[gpu.pid];
      const base = createBaseProcess(gpu.pid, cpuProc, gpu.name || gpu.process_name, gpu.path);
      const enriched = enrichProcessData(base, networkByPid, diskByPid, gpuByPid);
      procs.push(enriched);
    }
  });

  // 3. Add Network-only processes (not in CPU or GPU list)
  networkProcesses.forEach(net => {
    if (net.pid && !includedPids.has(net.pid)) {
      includedPids.add(net.pid);
      const cpuProc = processListByPid[net.pid];
      const base = createBaseProcess(net.pid, cpuProc, net.name, net.path);
      const enriched = enrichProcessData(base, networkByPid, diskByPid, gpuByPid);
      // Override with actual network data from this source
      enriched.net_up = net.sent_kbps;
      enriched.net_down = net.recv_kbps;
      procs.push(enriched);
    }
  });

  // 4. Add Disk-only processes (not in any other list)
  diskProcesses.forEach(disk => {
    if (disk.pid && !includedPids.has(disk.pid)) {
      includedPids.add(disk.pid);
      const cpuProc = processListByPid[disk.pid];
      const base = createBaseProcess(disk.pid, cpuProc, disk.name, disk.path);
      const enriched = enrichProcessData(base, networkByPid, diskByPid, gpuByPid);
      // Override with actual disk data from this source
      enriched.disk_read = disk.read_kbps;
      enriched.disk_write = disk.write_kbps;
      procs.push(enriched);
    }
  });

  // Sort by current sort column (with null/undefined guards)
  const sortCol = PROCESS_COLUMNS.find(c => c.key === processSort.key);
  if (sortCol) {
    procs.sort((a, b) => {
      const aVal = sortCol.getValue(a) ?? '';
      const bVal = sortCol.getValue(b) ?? '';
      // String comparison (coerce both to string if either is string)
      if (typeof aVal === 'string' || typeof bVal === 'string') {
        const aStr = String(aVal);
        const bStr = String(bVal);
        return processSort.dir === 'asc'
          ? aStr.localeCompare(bStr)
          : bStr.localeCompare(aStr);
      }
      // Numeric comparison (treat non-finite values as 0)
      const aNum = Number.isFinite(aVal) ? aVal : 0;
      const bNum = Number.isFinite(bVal) ? bVal : 0;
      return processSort.dir === 'asc' ? aNum - bNum : bNum - aNum;
    });
  }

  // Limit display
  procs = procs.slice(0, topProcessesLimit);

  if (procs.length === 0) {
    processesTable.innerHTML = '<div class="empty-state">No active processes</div>';
    processesValueEl.textContent = '0';
  } else {
    processesValueEl.textContent = 'shown';

    // Build two-row header: row 1 has group controls (network, disk, cpu_time, process_info), row 2 has individual column headers
    const networkColIndices = getColumnIndicesByGroup('network');
    const diskColIndices = getColumnIndicesByGroup('disk');
    const cpuTimeColIndices = getColumnIndicesByGroup('cpu_time');
    const processInfoColIndices = getColumnIndicesByGroup('process_info');
    // Track which columns are part of a group (for skipping in row 1)
    const groupedColIndices = new Set([...networkColIndices, ...diskColIndices, ...cpuTimeColIndices, ...processInfoColIndices]);

    // Helper to build a standard header cell
    const buildHeaderCell = (col, rowspan = 1) => {
      const sortClass = col.sortable ? 'sortable' : '';
      const sortDir = col.key === processSort.key ? (processSort.dir === 'asc' ? 'sort-asc' : 'sort-desc') : '';
      const indicator = col.sortable ? `<span class="sort-indicator">${col.key === processSort.key ? (processSort.dir === 'asc' ? '▲' : '▼') : '⇅'}</span>` : '';
      const numHeader = col.cssClass === 'num-cell' ? 'num-header' : '';
      const rowspanAttr = rowspan > 1 ? `rowspan="${rowspan}"` : '';
      // Smaller text for process_info subheaders (row 2 only, not group header)
      const processInfoSubheader = col.group === 'process_info' && rowspan === 1 ? 'process-info-subheader' : '';
      const titleAttr = col.sortable ? ' title="Click to sort"' : '';
      return `<th class="${sortClass} ${sortDir} ${numHeader} ${processInfoSubheader}" data-sort-key="${col.key}" ${rowspanAttr}${titleAttr}>${col.label}${indicator}</th>`;
    };

    // Build network controls cell content
    const netCapturingClass = networkCapture.capturing ? 'capturing' : '';
    const netButtonClass = networkCapture.capturing ? 'refresh-btn refresh-btn-small capturing' : 'refresh-btn refresh-btn-small';
    const networkControlsCell = `
      <th colspan="${networkColIndices.length}" class="network-header-group ${netCapturingClass} ${networkCapture.error ? 'error' : ''}">
        <div class="network-header-controls">
          <button class="${netButtonClass}" id="network-refresh-btn" title="Capture network data">${networkCapture.capturing ? '' : '▷'}</button>
          <button class="dropdown-trigger dropdown-trigger-small" id="nethogs-duration-table" title="Network capture duration">${nethogsDuration}s</button>
          <span class="network-timestamp" id="network-timestamp">${networkCapture.getTimestamp()}</span>
        </div>
      </th>`;

    // Build disk controls cell content
    const diskCapturingClass = diskCapture.capturing ? 'capturing' : '';
    const diskButtonClass = diskCapture.capturing ? 'refresh-btn refresh-btn-small capturing' : 'refresh-btn refresh-btn-small';
    const diskControlsCell = `
      <th colspan="${diskColIndices.length}" class="disk-header-group ${diskCapturingClass} ${diskCapture.error ? 'error' : ''}">
        <div class="disk-header-controls">
          <button class="${diskButtonClass}" id="disk-refresh-btn" title="Sample disk I/O">${diskCapture.capturing ? '' : '▷'}</button>
          <button class="dropdown-trigger dropdown-trigger-small" id="disk-duration-table" title="Disk sample duration">${diskDuration}s</button>
          <span class="disk-timestamp" id="disk-timestamp">${diskCapture.getTimestamp()}</span>
        </div>
      </th>`;

    // Build CPU time header cell (simple label, no controls)
    const cpuTimeCell = `<th colspan="${cpuTimeColIndices.length}" class="cpu-time-header-group">CPU Time</th>`;

    // Build process info header cell (simple label, smaller text)
    const processInfoCell = `<th colspan="${processInfoColIndices.length}" class="process-info-header-group">Info</th>`;

    // Row 1: non-grouped columns with rowspan=2, group controls with colspan
    let headerRow1 = '';
    for (let i = 0; i < PROCESS_COLUMNS.length; i++) {
      if (i === networkColIndices[0]) {
        // Insert network controls cell (spans network columns)
        headerRow1 += networkControlsCell;
      } else if (i === diskColIndices[0]) {
        // Insert disk controls cell (spans disk columns)
        headerRow1 += diskControlsCell;
      } else if (i === cpuTimeColIndices[0]) {
        // Insert CPU time header cell (spans cpu_time columns)
        headerRow1 += cpuTimeCell;
      } else if (i === processInfoColIndices[0]) {
        // Insert process info header cell (spans process_info columns)
        headerRow1 += processInfoCell;
      } else if (groupedColIndices.has(i)) {
        // Skip - covered by colspan
        continue;
      } else {
        headerRow1 += buildHeaderCell(PROCESS_COLUMNS[i], 2);
      }
    }

    // Row 2: just the grouped column headers (sortable)
    const headerRow2 = [...cpuTimeColIndices, ...networkColIndices, ...diskColIndices, ...processInfoColIndices]
      .sort((a, b) => a - b)
      .map(i => buildHeaderCell(PROCESS_COLUMNS[i]))
      .join('');

    // Build colgroup for fixed column widths
    const colgroup = PROCESS_COLUMNS.map(col => {
      return `<col style="width: ${col.width || 'auto'}">`;
    }).join('');

    // Build rows
    const rows = procs.map(p => {
      const cells = PROCESS_COLUMNS.map(col => {
        const value = col.render(p);
        const color = col.color ? col.color(p) : null;
        const style = color ? `style="color: ${color}"` : '';
        return `<td class="${col.cssClass}" ${style}>${value}</td>`;
      }).join('');
      return `<tr>${cells}</tr>`;
    }).join('');

    processesTable.innerHTML = `<table class="data-table"><colgroup>${colgroup}</colgroup><thead><tr>${headerRow1}</tr><tr>${headerRow2}</tr></thead><tbody>${rows}</tbody></table>`;
    // Event handlers are set up via delegation in initProcessTableListeners()
  }

  updateCardNoData('processes', true);
  updateCacheDisplay('processes', 'processes');
}

// Fetch slow-changing data; CPU/System Info retry until their permanent caches fill.
async function fetchSlowData() {
  const cpuInfoPromise = isCacheValid('cpuInfo')
    ? Promise.resolve({ data: getCache('cpuInfo'), ok: true, cached: true })
    : safeFetch('/api/cpu-info', {});
  const systemInfoPromise = isCacheValid('systemInfo')
    ? Promise.resolve({ data: getCache('systemInfo'), ok: true, cached: true })
    : safeFetch(API_BASE + '/system', {});

  const drivesPromise = isCacheValid('drives')
    ? Promise.resolve({ data: getCache('drives'), ok: true, cached: true })
    : safeFetch(API_BASE + '/fs', []);

  const ipPromise = isCacheValid('ipInfo')
    ? Promise.resolve({ data: getCache('ipInfo'), ok: true, cached: true })
    : safeFetch('/api/ip-info', {}, IP_INFO_TIMEOUT_MS);

  // /api/mail is a dashboard endpoint (not under the API_BASE Glances proxy);
  // CARD_ENDPOINT_DEPS lists it as '/mail', which is what safeFetch checks.
  const mailPromise = isCacheValid('mail')
    ? Promise.resolve({ data: getCache('mail'), ok: true, cached: true })
    : safeFetch('/api/mail', {});

  const storageHealthPromise = isCacheValid('storageHealth')
    ? Promise.resolve({ data: getCache('storageHealth'), ok: true, cached: true })
    : safeFetch('/api/storage-health', {});

  // The 60 s sensors snapshot (fans, temps, power) and nvidia-smi's GPU info
  // (power draw): both feed the fast-poll sensors block through the cache.
  const sensorsPromise = isCacheValid('sensors')
    ? Promise.resolve({ data: getCache('sensors'), ok: true, cached: true })
    : safeFetch('/api/sensors', {});
  const gpuInfoPromise = isCacheValid('gpuInfo')
    ? Promise.resolve({ data: getCache('gpuInfo'), ok: true, cached: true })
    : safeFetch('/api/gpu-info', []);

  // Metadata must not wait for the longer public-IP lookup. Uptime is read
  // once per slow poll and shares the metadata update.
  const infoPromise = Promise.all([
    gpuInfoPromise, cpuInfoPromise, systemInfoPromise, safeFetch(API_BASE + '/uptime', '')
  ]).then(([gpuInfo, cpuInfo, systemInfo, uptime]) => {
    if (gpuInfo.ok && !gpuInfo.cached) {
      updateCache('gpuInfo', gpuInfo.data);
      renderGpuInfo(gpuInfo.data);
    } else if (!gpuInfo.ok && !getCache('gpuInfo')) {
      document.getElementById('gpu-info-content').innerHTML = '<div class="info-item text-error">Error loading GPU info</div>';
      updateCardNoData('gpu-info', false);
    }

    if (cpuInfo.ok) {
      if (!cpuInfo.cached) {
        updateCache('cpuInfo', cpuInfo.data);
        renderCpuInfo(cpuInfo.data);
      }
    } else {
      document.getElementById('cpu-info-content').innerHTML = '<div class="info-item text-error">Error loading CPU info</div>';
      updateCardNoData('cpu-info', false);
    }

    if (systemInfo.ok) {
      if (!systemInfo.cached) updateCache('systemInfo', systemInfo.data);
      if (!systemInfo.cached || (uptime.ok && uptime.data)) {
        renderSystemInfo(systemInfo.data, uptime.ok && uptime.data ? uptime.data.replace(/"/g, '') : '--');
      }
    } else {
      document.getElementById('system-info-content').innerHTML = '<div class="info-item text-error">Error loading system info</div>';
      updateCardNoData('system-info', false);
    }
  });

  const [fs, ipInfo, mail, storageHealth, sensorsSnap] = await Promise.all([
    drivesPromise, ipPromise, mailPromise, storageHealthPromise, sensorsPromise, infoPromise
  ]);
  if (sensorsSnap.ok && !sensorsSnap.cached) updateCache('sensors', sensorsSnap.data);

  // === PROCESS FAST RESULTS IMMEDIATELY ===

  // Handle drives
  if (fs.ok && Array.isArray(fs.data)) {
    if (!fs.cached) {
      updateCache('drives', fs.data);
    }
    renderDrives(fs.data);   // an empty list hides the card (Glances could not read the mounts)
  } else if (!fs.ok) {
    const driveUsageContent = document.getElementById('drive-usage-content');
    driveUsageContent.innerHTML = '<div class="drive-usage-item text-error">Error loading drive info</div>';
    updateCardNoData('drive-usage', false);
  }

  // Handle IP info
  if (ipInfo.ok) {
    if (!ipInfo.cached) {
      updateCache('ipInfo', ipInfo.data);
    }
    renderIpInfo(ipInfo.data);
  } else if (!ipInfo.ok) {
    const ipContent = document.getElementById('ip-info-content');
    ipContent.innerHTML = '<div class="info-item text-error">Error loading IP info</div>';
    updateCardNoData('ip-info', false);
  }

  // Handle mail ('unavailable' means the backend lacks the endpoint and the
  // card is hidden, so there is nothing to report)
  if (mail.ok) {
    if (!mail.cached) {
      updateCache('mail', mail.data);
    }
    renderMail(mail.data);
  } else if (mail.errorType !== 'unavailable') {
    const mailContent = document.getElementById('mail-content');
    mailContent.innerHTML = '<div class="info-item text-error">Error loading mail</div>';
    updateCardNoData('mail', false);
  }

  // Handle the hardware snapshot (same 'unavailable' rule as mail)
  if (storageHealth.ok) {
    if (!storageHealth.cached) {
      updateCache('storageHealth', storageHealth.data);
    }
    renderHardware(storageHealth.data);
    renderDriveHealth(storageHealth.data);
  } else if (storageHealth.errorType !== 'unavailable') {
    document.getElementById('hardware-content').innerHTML = '<div class="info-item text-error">Error loading hardware health</div>';
    updateCardNoData('hardware', false);
    setHtml(document.getElementById('drive-health-content'), '<div class="info-item text-error">Error loading drive health</div>');
    updateCardNoData('drive-health', false);
  }

  // === DISK CAPTURE - runs independently, doesn't block above ===
  const useDiskCache = isCacheValid('diskProcesses');
  if (useDiskCache) {
    diskCapture.handleResponse(getCache('diskProcesses'), dataCache.diskProcesses.lastUpdate);
  } else {
    if (!diskCapture.capturing) diskCapture.start();
    safeFetch(`/api/disk-processes?duration=${diskDuration}`, [], getDiskTimeout())
      .then(diskProcesses => {
        if (diskProcesses.ok) {
          // Cache only a real result array — never an error object, which would
          // otherwise pin the card in its error state for the whole TTL.
          if (Array.isArray(diskProcesses.data)) updateCache('diskProcesses', diskProcesses.data);
          diskCapture.handleResponse(diskProcesses.data);
        } else {
          console.error('Disk processes fetch failed:', diskProcesses);
          diskCapture.stop();
          diskCapture.setError();
        }
      })
      .catch(err => {
        console.error('Disk processes fetch error:', err);
        diskCapture.stop();
        diskCapture.setError();
      });
  }

  // === NETWORK CAPTURE - runs independently, doesn't block above ===
  const useNetworkCache = isCacheValid('networkProcesses');
  if (useNetworkCache) {
    networkCapture.handleResponse(getCache('networkProcesses'), dataCache.networkProcesses.lastUpdate);
  } else {
    if (!networkCapture.capturing) networkCapture.start();
    safeFetch(`/api/network-processes?duration=${nethogsDuration}`, [], getNethogsTimeout())
      .then(networkProcesses => {
        if (networkProcesses.ok) {
          // Cache only a real result array (see diskProcesses above).
          if (Array.isArray(networkProcesses.data)) updateCache('networkProcesses', networkProcesses.data);
          networkCapture.handleResponse(networkProcesses.data);
        } else {
          console.error('Network processes fetch failed:', networkProcesses);
          networkCapture.stop();
          networkCapture.setError();
        }
      })
      .catch(err => {
        console.error('Network processes fetch error:', err);
        networkCapture.stop();
        networkCapture.setError();
      });
  }
}

// One poll at a time: safeFetch's 5 s timeout is longer than the 2 s poll, so a
// slow backend would otherwise stack overlapping polls and push the live-chart
// samples out of order.
let fetchInFlight = false;
async function fetchData() {
  if (fetchInFlight) return;
  fetchInFlight = true;
  try { await fetchDataOnce(); } finally { fetchInFlight = false; }
}
// One card's processing bug must not skip the cards after it. The card goes
// red like a failed fetch (header shows Partial); the error still hits the console.
function guard(cardId, fn) {
  try { fn(); } catch (err) {
    console.error(`${cardId}: processing error`, err);
    updateCardNoData(cardId, false);
  }
}
async function fetchDataOnce() {
  // Build fetch promises - skip cached endpoints
  const fetchPromises = [
    safeFetch(API_BASE + '/cpu', {}),
    safeFetch(API_BASE + '/percpu', []),
    safeFetch(API_BASE + '/mem', {}),
    safeFetch(API_BASE + '/memswap', {}),
    safeFetch(API_BASE + '/gpu', []),
    safeFetch(API_BASE + '/network', []),
    safeFetch(API_BASE + '/diskio', []),
    safeFetch(API_BASE + '/sensors', []),
    safeFetch(API_BASE + '/load', {}),
    isCacheValid('processes') ? Promise.resolve({ data: getCache('processes'), ok: true, cached: true }) : safeFetch('/api/processlist', []),
    safeFetch('/api/gpu-processes', []),
    safeFetch('/api/ping', {})
  ];

  const [cpu, percpu, mem, swap, gpu, network, diskio, sensors, load, processlist, gpuProcesses, ping] = await Promise.all(fetchPromises);

  guard('cpu', () => {
    // CPU
    const cpuValueEl = document.getElementById('cpu-value');
    if (cpu.ok) {
      const cpuTotal = cpu.data.total || 0;
      pushHistory(history.cpu, cpuTotal);
      cpuValueEl.style.color = '';
      animateValue(cpuValueEl, cpuTotal, '%');
      charts.cpu.update();
      updateCardNoData('cpu', true);
      updateCardWarning('cpu', cpuTotal, thresholds.cpu_percent);
    } else {
      // Don't plot anything - skip history update
      updateCardNoData('cpu', false);
    }
  });

  guard('cpu-cores', () => {
    // Per-CPU cores
    const cpuCoresValueEl = document.getElementById('cpu-cores-value');
    if (percpu.ok && Array.isArray(percpu.data) && percpu.data.length > 0) {
      const coreCount = percpu.data.length;
      const hideLabels = coreCount > 32;

      // Create array of {index, value} pairs
      let coreData = percpu.data.map((core, i) => ({
        index: i,
        value: core.total || 0
      }));

      // Sort by usage descending if >32 cores
      if (hideLabels) {
        coreData.sort((a, b) => b.value - a.value);
      }

      const labels = coreData.map(c => c.index.toString());
      const values = coreData.map(c => c.value);
      const colors = values.map(v => getCoreColor(v));

      charts.cpuCores.data.labels = labels;
      charts.cpuCores.data.datasets[0].data = values;
      charts.cpuCores.data.datasets[0].backgroundColor = colors;
      charts.cpuCores.options.scales.x.ticks.display = !hideLabels;
      charts.cpuCores.update();

      cpuCoresValueEl.style.color = '';
      cpuCoresValueEl.textContent = percpu.data.length + ' cores';
      updateCardNoData('cpu-cores', true);

      // Cross-core spread (live): min/median/max across cores this poll
      const { min: coreMin, median: coreMedian, max: coreMax } = rangeStats(values);
      pushHistory(history.cpuCoresMin, coreMin);
      pushHistory(history.cpuCoresMedian, coreMedian);
      pushHistory(history.cpuCoresMax, coreMax);
      charts.cpuCoresSpread.update();
      const spreadValueEl = document.getElementById('cpu-cores-spread-value');
      spreadValueEl.style.color = '';
      spreadValueEl.textContent = coreMin.toFixed(0) + '–' + coreMax.toFixed(0) + '%';
      updateCardNoData('cpu-cores-spread', true);
    } else {
      // Covers both a failed fetch and ok-but-empty (OpenWrt returns "[]" with
      // HTTP 200 when /proc/stat is unreadable) — otherwise the cards keep
      // showing stale cores with no no-data marker.
      updateCardNoData('cpu-cores', false);
      updateCardNoData('cpu-cores-spread', false);
    }
  });

  guard('mem', () => {
    // Memory
    const memUsedEl = document.getElementById('mem-used-value');
    const memTotalEl = document.getElementById('mem-total-value');
    if (mem.ok) {
      const memUsed = mem.data.used || 0;
      const memTotal = mem.data.total || 1;
      const memUsedGiB = memUsed / 1024 / 1024 / 1024;
      const memTotalGiB = memTotal / 1024 / 1024 / 1024;
      pushHistory(history.mem, memUsedGiB);
      // Update chart Y-axis max and store total for tooltip
      charts.mem.options.scales.y.max = Math.ceil(memTotalGiB);
      charts.mem._totalGiB = memTotalGiB;
      animateValue(memUsedEl, memUsedGiB, '', 1);
      memTotalEl.textContent = memTotalGiB.toFixed(1);
      charts.mem.update();
      updateCardNoData('mem', true);
      const memPercent = (memUsed / memTotal) * 100;
      updateCardWarning('mem', memPercent, thresholds.memory_percent);
    } else {
      updateCardNoData('mem', false);
    }
  });

  guard('swap', () => {
    // Swap
    const swapUsedEl = document.getElementById('swap-used-value');
    const swapTotalEl = document.getElementById('swap-total-value');
    if (swap.ok) {
      const swapUsed = swap.data.used || 0;
      const swapTotal = swap.data.total || 1;
      const swapUsedGiB = swapUsed / 1024 / 1024 / 1024;
      const swapTotalGiB = swapTotal / 1024 / 1024 / 1024;
      const swapPercent = (swapUsed / swapTotal) * 100;
      pushHistory(history.swap, swapPercent);
      animateValue(swapUsedEl, swapUsedGiB, '', 1);
      swapTotalEl.textContent = swapTotalGiB.toFixed(1);
      charts.swap.update();
      updateCardNoData('swap', true);
      updateCardWarning('swap', swapPercent, thresholds.swap_percent);
    } else {
      updateCardNoData('swap', false);
    }
  });

  guard('gpu', () => {
    // GPU (handle multiple GPUs)
    if (gpu.ok) {
      const gpuArray = Array.isArray(gpu.data) ? gpu.data : (gpu.data ? [gpu.data] : []);
      const validGpus = gpuArray.filter(g => g && (g.proc !== undefined || g.mem !== undefined));

      if (validGpus.length > 0) {
        // Initialize history arrays if GPU count changed
        if (validGpus.length !== gpuCount) {
          gpuCount = validGpus.length;
          history.gpus = validGpus.map(() => Array(historyLength).fill(null));
          history.gpuMems = validGpus.map(() => Array(historyLength).fill(null));

          // Recreate chart datasets
          charts.gpu.data.datasets = validGpus.map((_, i) => ({
            label: 'GPU ' + i,
            data: history.gpus[i],
            borderColor: GPU_COLORS[i % GPU_COLORS.length],
            backgroundColor: GPU_COLORS[i % GPU_COLORS.length] + '20',
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 4,
            borderWidth: 2
          }));
          charts.gpuMem.data.datasets = validGpus.map((_, i) => ({
            label: 'GPU ' + i,
            data: history.gpuMems[i],
            borderColor: GPU_COLORS[i % GPU_COLORS.length],
            backgroundColor: GPU_COLORS[i % GPU_COLORS.length] + '20',
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 4,
            borderWidth: 2
          }));

          // Update legends with GPU names
          updateGpuLegend('gpu-legend', validGpus);
          updateGpuLegend('gpu-mem-legend', validGpus);
        }

        // Rebuild GPU value spans if GPU count changed
        const gpuValueEl = document.getElementById('gpu-value');
        const gpuMemValueEl = document.getElementById('gpu-mem-value');
        if (validGpus.length !== Number(gpuValueEl.dataset.gpuCount)) {
          gpuValueEl.dataset.gpuCount = validGpus.length;
          gpuMemValueEl.dataset.gpuCount = validGpus.length;
          // Build usage spans: <span id="gpu-usage-0">--</span> / <span id="gpu-usage-1">--</span>%
          gpuValueEl.innerHTML = validGpus.map((_, i) =>
            `<span id="gpu-usage-${i}">--</span>`
          ).join(' / ') + '%';
          // Build memory spans: <span id="gpu-mem-used-0">--</span>/<span id="gpu-mem-total-0">--</span> GB
          gpuMemValueEl.innerHTML = validGpus.map((_, i) =>
            `<span id="gpu-mem-used-${i}">--</span>/<span id="gpu-mem-total-${i}">--</span>`
          ).join(' / ') + ' GB';
        }

        // Update GPU labels immediately: an older animation must not restore a
        // numeric label after a newer poll marks that reading unavailable.
        const gpuInfoData = getCache('gpuInfo') || [];
        const gpuMemTotals = [];
        let maxGpuMemGiB = 0;
        let maxGpuUtil = null, maxGpuMem = null;
        validGpus.forEach((g, i) => {
          // Glances uses null when an NVML field is unavailable. Keep each
          // metric independent: a missing reading is a gap, never idle/empty.
          const proc = Number.isFinite(g.proc) ? g.proc : null;
          const memPercent = Number.isFinite(g.mem) ? g.mem : null;
          pushHistory(history.gpus[i], proc);
          document.getElementById(`gpu-usage-${i}`).textContent = proc != null ? proc.toFixed(0) : '--';
          if (proc != null) {
            maxGpuUtil = Math.max(maxGpuUtil ?? proc, proc);
          }
          // Compute memory GiB from Glances mem% and cached nvidia-smi total
          const gpuInfoEntry = gpuInfoData.find(gi => gi.index === i);
          const totalMiB = gpuInfoEntry?.memory_total;
          const totalGiB = Number.isFinite(totalMiB) && totalMiB > 0 ? totalMiB / 1024 : null;
          if (totalGiB != null) {
            gpuMemTotals[i] = totalGiB;
            if (totalGiB > maxGpuMemGiB) maxGpuMemGiB = totalGiB;
          }
          const usedGiB = memPercent != null && totalGiB != null ? memPercent / 100 * totalGiB : null;
          pushHistory(history.gpuMems[i], usedGiB);
          document.getElementById(`gpu-mem-used-${i}`).textContent = usedGiB != null ? usedGiB.toFixed(1) : '--';
          document.getElementById(`gpu-mem-total-${i}`).textContent = totalGiB != null ? totalGiB.toFixed(1) : '--';
          if (usedGiB != null) {
            maxGpuMem = Math.max(maxGpuMem ?? memPercent, memPercent);
          }
        });
        // Update GPU memory chart Y-axis max and store totals for tooltip
        charts.gpuMem.options.scales.y.max = Math.ceil(maxGpuMemGiB) || 24;
        charts.gpuMem._gpuMemTotals = gpuMemTotals;

        updateCardNoData('gpu', maxGpuUtil != null);
        updateCardNoData('gpu-mem', maxGpuMem != null);
        // Warn from available readings only; partial multi-GPU data stays useful.
        updateCardWarning('gpu', maxGpuUtil ?? 0, thresholds.gpu_util_percent);
        updateCardWarning('gpu-mem', maxGpuMem ?? 0, thresholds.gpu_memory_percent);
      } else {
        for (const id of ['gpu-value', 'gpu-mem-value']) {
          const el = document.getElementById(id);
          el.textContent = 'N/A';
          // textContent removes the spans; rebuild them when GPUs return.
          el.dataset.gpuCount = '0';
        }
        updateCardNoData('gpu', true);
        updateCardNoData('gpu-mem', true);
        updateCardWarning('gpu', 0, 0);
        updateCardWarning('gpu-mem', 0, 0);
      }
      charts.gpu.update();
      charts.gpuMem.update();
    } else {
      // Don't plot anything on failure
      updateCardNoData('gpu', false);
      updateCardNoData('gpu-mem', false);
    }
  });

  guard('network', () => {
    // Network (sum all interfaces, use rate)
    const netInEl = document.getElementById('net-in-value');
    const netOutEl = document.getElementById('net-out-value');
    if (network.ok) {
      // MB/s summed over interfaces, each capped on its own (config.json
      // limits; Glances' deltas occasionally come out absurd). The total is not
      // capped: bridges, veths and tunnels count the same traffic again.
      let inMBps = 0, outMBps = 0;
      if (Array.isArray(network.data)) {
        network.data.forEach(iface => {
          if (iface.interface_name && !iface.interface_name.startsWith('lo')) {
            inMBps += clampRate((iface.bytes_recv_rate_per_sec || 0) / 1e6, limits.network_mbps);
            outMBps += clampRate((iface.bytes_sent_rate_per_sec || 0) / 1e6, limits.network_mbps);
          }
        });
      }
      pushHistory(history.netIn, inMBps);
      pushHistory(history.netOut, outMBps);
      animateValue(netInEl, inMBps, '', 2);
      animateValue(netOutEl, outMBps, '', 2);
      charts.net.update();
      updateCardNoData('network', true);
      updateCardWarning('network', Math.max(inMBps, outMBps), thresholds.network_mbps);
    } else {
      updateCardNoData('network', false);
    }
  });

  guard('disk', () => {
    // Disk I/O (sum all disks, use rate)
    const diskReadEl = document.getElementById('disk-read-value');
    const diskWriteEl = document.getElementById('disk-write-value');
    if (diskio.ok) {
      // Until the drives cache has loaded at least once we can't tell mounted
      // devices from md/dm members, and counting every diskio row would count an
      // md-mirror write ~3x. Skip this tick rather than push a spurious spike into
      // the sparkline. A real empty fs response is cached as [] (still an Array),
      // which falls through to the count-all fallback below. Trade-off: if /fs is
      // persistently unreadable while diskio works, the card stays blank rather
      // than show an unattributable count-all total -- a blank beats a wrong number.
      const cachedDrives = getCache('drives');
      if (Array.isArray(cachedDrives)) {
        let diskReadRate = 0, diskWriteRate = 0;
        const drives = [];

        // Allowed disks from the cached fs data (same set the Drive Usage card uses)
        const deviceToMount = {};
        cachedDrives.forEach(fs => {
          if (fs.device_name) {
            // Extract disk name: "/dev/sda1" -> "sda1"
            const match = fs.device_name.match(/\/dev\/(.+)/);
            // Shortest mount names the device ('/' over a bind mount of it), as in server.py.
            if (match && (!deviceToMount[match[1]] || fs.mnt_point.length < deviceToMount[match[1]].length)) {
              deviceToMount[match[1]] = fs.mnt_point;
            }
          }
        });
        const allowedDisks = new Set(Object.keys(deviceToMount));

        if (Array.isArray(diskio.data)) {
          diskio.data.forEach(disk => {
            // The total counts the MOUNTED devices only: diskio rows are per block
            // device including partitions and md/dm layers, so every row would count
            // a write on an md mirror three times. No fs data: count every row.
            // Each device capped on its own, the total is their sum (as in history).
            if (allowedDisks.size === 0 || allowedDisks.has(disk.disk_name)) {
              diskReadRate += clampRate((disk.read_bytes_rate_per_sec || 0) / 1e6, limits.disk_io_mbps);
              diskWriteRate += clampRate((disk.write_bytes_rate_per_sec || 0) / 1e6, limits.disk_io_mbps);
            }
            // Collect per-drive data (only for drives shown in Drive Usage)
            if (disk.disk_name && allowedDisks.has(disk.disk_name)) {
              drives.push({
                name: deviceToMount[disk.disk_name] || disk.disk_name,
                readMBps: clampRate((disk.read_bytes_rate_per_sec || 0) / 1e6, limits.disk_io_mbps),
                writeMBps: clampRate((disk.write_bytes_rate_per_sec || 0) / 1e6, limits.disk_io_mbps)
              });
            }
          });
        }
        const readMBps = diskReadRate;    // MB/s, summed from capped devices
        const writeMBps = diskWriteRate;
        pushHistory(history.diskRead, readMBps);
        pushHistory(history.diskWrite, writeMBps);
        animateValue(diskReadEl, readMBps, '', 2);
        animateValue(diskWriteEl, writeMBps, '', 2);
        charts.disk.update();
        // no-data first: updateCardWarning bails while .no-data is set.
        updateCardNoData('disk', true);
        updateCardWarning('disk', Math.max(readMBps, writeMBps), thresholds.disk_io_mbps);

        // Per-drive disk I/O
        if (drives.length > 0) {
          // Initialize history arrays if drive set changed
          const newDriveNames = drives.map(d => d.name);
          if (drives.length !== diskDriveCount || newDriveNames.join(',') !== diskDriveNames.join(',')) {
            diskDriveCount = drives.length;
            diskDriveNames = newDriveNames;
            history.diskDriveReads = drives.map(() => Array(historyLength).fill(null));
            history.diskDriveWrites = drives.map(() => Array(historyLength).fill(null));

            // Recreate chart datasets: read (solid) and write (dashed) for each drive
            charts.diskDrives.data.datasets = [];
            drives.forEach((drive, i) => {
              const color = GPU_COLORS[i % GPU_COLORS.length];
              // Read dataset (solid line)
              charts.diskDrives.data.datasets.push({
                label: drive.name + ' Read',
                data: history.diskDriveReads[i],
                borderColor: color,
                backgroundColor: color + '20',
                fill: false,
                tension: 0.4,
                pointRadius: 0,
                pointHoverRadius: 4,
                borderWidth: 2
              });
              // Write dataset (dashed line)
              charts.diskDrives.data.datasets.push({
                label: drive.name + ' Write',
                data: history.diskDriveWrites[i],
                borderColor: color,
                backgroundColor: color + '10',
                fill: false,
                tension: 0.4,
                pointRadius: 0,
                pointHoverRadius: 4,
                borderWidth: 2,
                borderDash: [5, 5]
              });
            });

            // Update legend
            updateDiskDrivesLegend('disk-drives-legend', diskDriveNames);
          }

          // Push data for each drive
          drives.forEach((drive, i) => {
            pushHistory(history.diskDriveReads[i], drive.readMBps);
            pushHistory(history.diskDriveWrites[i], drive.writeMBps);
          });

          charts.diskDrives.update();
          updateCardNoData('disk-drives', true);
        } else {
          updateCardNoData('disk-drives', false);
        }
      }
    } else {
      updateCardNoData('disk', false);
      updateCardNoData('disk-drives', false);
    }
  });

  // Temps, fans and power share the collected arrays, so they are one unit.
  guard('temps', () => {
    // Sensors (temperatures and fans). Three independent sources feed these two
    // cards: Glances core sensors, GPU temps (/api/gpu), and the root
    // collector's IPMI snapshot. Each contributes only if it has data, and each
    // card is driven purely by whether its array ended up non-empty -- so a
    // failure of one source (e.g. Glances /sensors returns empty) never hides
    // the IPMI-only fan card when the snapshot is fresh.
    const temps = [];
    const fans = [];

    // Every temp row carries its own warning/critical line: a chip's psutil
    // limits when Glances has them (it emits null otherwise -- hence ??, never ||),
    // else the chassis threshold from config.json.
    if (sensors.ok && Array.isArray(sensors.data) && sensors.data.length > 0) {
      // A null reading is dropped, not drawn as a 0 °C / 0 RPM bar.
      sensors.data.filter(s => s.type === 'temperature_core' && s.value != null).forEach(s => temps.push({
        label: s.label, value: s.value,
        warning: s.warning ?? thresholds.temp_celsius, critical: s.critical ?? 90
      }));
      sensors.data.filter(s => s.type === 'fan_speed' && s.value != null).forEach(s => fans.push(s));
    }

    // Chassis fans, temps and power from the 60 s sensors snapshot (IPMI and
    // RAPL are root-only, so Glances cannot see them). Only a fresh snapshot is
    // drawn -- a stale/missing one is skipped so old values are never shown as
    // live -- and its own age (not the fetch time) goes in the three headers.
    // Match the server's five-minute limit, including the snapshot's age when
    // fetched, even if subsequent HTTP refreshes fail.
    const sen = getFreshSnapshot('sensors', 300000);
    // The driver's own per-GPU limits (nvidia-smi -q, via the same snapshot), by index.
    const gpuLimits = new Map(sen?.status === 'ok' ? (sen.gpus ?? []).map(g => [g.index, g.limits ?? {}]) : []);

    // GPU temps come from /api/gpu, so they render whenever gpu.ok -- independent
    // of whether Glances /sensors succeeded.
    if (gpu.ok) {
      const gpuArray = Array.isArray(gpu.data) ? gpu.data : (gpu.data ? [gpu.data] : []);
      gpuArray.forEach((g, i) => {
        if (g && g.temperature !== undefined && g.temperature !== null) {
          // Parse gpu_id (e.g. "nvidia0" -> 0) to match history recording
          const gpuId = g.gpu_id;
          let idx = i;
          if (typeof gpuId === 'string' && gpuId.startsWith('nvidia')) {
            const parsed = parseInt(gpuId.replace('nvidia', ''), 10);
            if (!isNaN(parsed)) idx = parsed;
          } else if (typeof gpuId === 'number') {
            idx = gpuId;
          }
          // Warn at the driver's max operating temp (where its software slowdown
          // starts), critical at its hardware slowdown temp. Either one missing (no
          // fresh sensors snapshot, or N/A: the P40 has no max operating temp) falls
          // back to datasheet limits by model substring; unlisted models get 80 / 90.
          const GPU_TEMP_LIMITS = { P40: [85, 95] };
          const [warn, crit] = Object.entries(GPU_TEMP_LIMITS).find(([m]) => (g.name || '').includes(m))?.[1] ?? [80, 90];
          const lim = gpuLimits.get(idx) ?? {};
          temps.push({ label: 'GPU ' + idx, value: g.temperature, warning: lim.max_op ?? warn, critical: lim.slowdown ?? crit });
        }
      });
    }

    const power = [];
    if (sen?.status === 'ok') {
      // GPU memory (HBM) temps: nvidia-smi only, so from the snapshot, placed right
      // after that GPU's core row. The memory has no throttle of its own: warn
      // 3 C under its max operating temp, critical at it. N/A (the P40) = no row.
      (sen.gpus ?? []).forEach(g => {
        if (g.mem_c == null) return;
        const top = g.limits?.mem_max_op;
        const at = temps.findIndex(t => t.label === 'GPU ' + g.index);
        temps.splice(at < 0 ? temps.length : at + 1, 0, {
          label: `GPU ${g.index} mem`, value: g.mem_c,
          warning: top != null ? top - 3 : 80, critical: top ?? 90
        });
      });
      (sen.temps ?? []).forEach(t => t.value != null && temps.push({ label: t.label, value: t.value, warning: thresholds.temp_celsius, critical: 90 }));
      (sen.fans ?? []).forEach(f => f.value != null && fans.push({ label: f.label, value: f.value }));
      // Watts rows only: the BMC total ("Pwr Consumption", the one non-RAPL Watts row)
      // and the RAPL CPU/DRAM rows. The same capture also carries PSU amps and volts.
      (sen.power ?? []).forEach(p => p.value != null && String(p.reading).endsWith('Watts') &&
        power.push({ label: p.label, value: p.value, total: p.entity !== 'rapl' }));
    }
    // Drive temperatures from the 15-min SMART snapshot, keyed by device. SAS
    // drives report a trip point; the others get spinning-rust defaults (50 / 60).
    // Its own block: fans and power must not depend on this slower snapshot.
    // Match the server's one-hour snapshot limit even if HTTP refetches fail.
    const shTemps = getFreshSnapshot('storageHealth', 3600000);
    if (shTemps) {
      (shTemps.drives ?? []).forEach(d => d.temperature_c != null && temps.push({
        label: d.device, value: d.temperature_c,
        warning: d.drive_trip_c != null ? d.drive_trip_c - 5 : 50, critical: d.drive_trip_c ?? 60
      }));
    }
    // GPU draw from nvidia-smi via /api/gpu-info, polled every 30 s (Glances' gpu plugin has no power).
    (getFreshCache('gpuInfo', 300000) ?? []).forEach(g => g.power_draw != null && power.push({ label: 'GPU ' + g.index, value: g.power_draw }));
    const sensorsAge = sensorsAgeText();
    ['fans-card', 'temps-card', 'power-card'].forEach(id => {
      const el = document.querySelector(`#${id} .cache-time`);
      if (el) el.textContent = sensorsAge;
    });

    // The per-core readings (e.g. 36 on a two-socket box) collapse into one "Cores" entry whose value
    // is the [min, max] span (a floating bar; tempsChartConfig); the rest stays.
    const { cores, rest: nonCores } = aggregateCoreTemps(temps);
    if (cores) {
      const first = temps.find(t => CORE_TEMP_LABEL.test(t.label));
      temps.length = 0;
      temps.push({ label: 'Cores', value: [cores.min, cores.max], warning: first.warning, critical: first.critical }, ...nonCores);
    }
    charts.temps._cores = cores;
    const hi = t => Array.isArray(t.value) ? t.value[1] : (t.value || 0);   // the reading that colours and warns

    // Temperatures chart
    const tempsValueEl = document.getElementById('temps-value');
    if (temps.length > 0) {
      charts.temps.data.labels = temps.map(t => abbreviateSensorLabel(t.label));
      charts.temps.data.datasets[0].data = temps.map(t => t.value ?? 0);
      charts.temps.data.datasets[0].backgroundColor = temps.map(t => getTempColor(hi(t), t.warning, t.critical));
      charts.temps.data.datasets[1].data = temps.map(t => t.label === 'Cores' ? cores.median : null);   // the median tick
      charts.temps.update();
      const maxTemp = Math.max(...temps.map(hi));
      tempsValueEl.style.color = '';
      tempsValueEl.textContent = maxTemp.toFixed(0) + '°C max';
      updateCardNoData('temps', true);
      // Orange when any sensor is at or over ITS OWN line (>= matches getTempColor),
      // not when the hottest sensor (usually a GPU under load) is over the chassis line.
      updateCardWarning('temps', temps.filter(t => hi(t) >= t.warning).length, 0);
    } else {
      updateCardNoData('temps', false);
    }

    // Fans chart (show the card only if some source produced fan data)
    const fansCard = document.getElementById('fans-card');
    if (fans.length > 0) {
      fansCard.style.display = '';
      const labels = fans.map(f => f.label);
      const values = fans.map(f => f.value || 0);
      charts.fans.data.labels = labels;
      charts.fans.data.datasets[0].data = values;
      charts.fans.update();
      const maxFan = Math.max(...values);
      document.getElementById('fans-value').textContent = maxFan.toFixed(0) + ' RPM max';
      updateCardNoData('fans', true);
    } else {
      // Hidden, not failed: a hidden card must not count in the header's "Partial" tooltip.
      fansCard.style.display = 'none';
    }

    // Power: one stacked bar of the BMC total. Measured parts (GPUs from
    // nvidia-smi, CPU/DRAM sockets from RAPL) are coloured segments; what is
    // left is "other" (drives, fans, board, PSU loss). Without a BMC total the
    // bar is just the measured parts. Scale edge and PSU line from config.json.
    const powerCard = document.getElementById('power-card');
    if (power.length > 0) {
      powerCard.style.display = '';
      const rank = l => l.startsWith('GPU') ? 0 : l.startsWith('CPU') ? 1 : l.startsWith('DRAM') ? 2 : 3;
      const parts = power.filter(p => !p.total).sort((a, b) => rank(a.label) - rank(b.label));
      const measured = parts.reduce((s, p) => s + p.value, 0);
      const totalRow = power.find(p => p.total);
      // The BMC total is read once a minute, the parts every 30 s; when the parts
      // briefly exceed it the bar is the parts, so the number is too.
      const total = totalRow ? Math.max(totalRow.value, measured) : measured;
      if (totalRow) parts.push({ label: 'other', value: total - measured });
      const maxW = thresholds.power_max_watts || 1200;
      const psuW = thresholds.power_psu_watts || 1100;
      const warnFrac = thresholds.power_warn_fraction || 0.8;
      const segClass = l => 'power-seg-' + l.toLowerCase().replace(/[^a-z0-9-]/g, '');   // "GPU 0" -> power-seg-gpu0
      const segs = parts.map(p =>
        `<div class="power-seg ${segClass(p.label)}" style="width:${Math.min(100, p.value / maxW * 100).toFixed(2)}%" title="${escapeHtml(p.label)} ${Math.round(p.value)} W"></div>`).join('');
      // Legend labels take their segment's colour (dimmed by CSS) so the bar reads without hovering.
      const legend = parts.map(p =>
        `<span style="color:${getCssColor(segColorVar(p.label))}"><i class="${segClass(p.label)}"></i>${escapeHtml(p.label)} <b>${Math.round(p.value)} W</b></span>`).join('');
      setHtml(document.getElementById('power-bar'),
        `<div class="power-bar"><div class="power-psu" style="left:${(psuW / maxW * 100).toFixed(2)}%" title="PSU ${psuW} W"></div>${segs}</div>` +
        `<div class="power-legend">${legend}</div>` +
        `<div class="power-meta">PSU ${psuW} W (dashed line) · scale ${maxW} W · warns at ${Math.round(warnFrac * 100)}% of PSU` +
        (totalRow ? '' : ' · no BMC total: measured parts only') + `</div>`);
      const valueEl = document.getElementById('power-value');
      if (totalRow) {
        const pct = total / psuW * 100;
        valueEl.textContent = `${Math.round(total)} W (${pct.toFixed(0)}%)`;
        valueEl.className = 'value' + (pct >= warnFrac * 100 ? ' text-warning' : '');
        updateCardWarning('power', total, psuW * warnFrac);
      } else {
        // Parts only (no BMC): a lower bound, which cannot be judged against the PSU.
        valueEl.textContent = `≥ ${Math.round(total)} W`;
        valueEl.className = 'value';
        updateCardWarning('power', 0, 0);
      }
      updateCardNoData('power', true);
    } else {
      powerCard.style.display = 'none';
    }
  });

  guard('load', () => {
    // System Load
    if (load.ok && load.data) {
      const cpuCores = load.data.cpucore || 1;
      const load1 = load.data.min1 || 0;
      const load5 = load.data.min5 || 0;
      const load15 = load.data.min15 || 0;

      const load1El = document.getElementById('load-1m');
      const load5El = document.getElementById('load-5m');
      const load15El = document.getElementById('load-15m');
      const loadValueEl = document.getElementById('load-value');

      load1El.textContent = load1.toFixed(2);
      load1El.className = 'load-num ' + getLoadClass(load1, cpuCores);
      load5El.textContent = load5.toFixed(2);
      load5El.className = 'load-num ' + getLoadClass(load5, cpuCores);
      load15El.textContent = load15.toFixed(2);
      load15El.className = 'load-num ' + getLoadClass(load15, cpuCores);

      loadValueEl.textContent = cpuCores + ' cores';
      updateCardNoData('load', true);
    } else {
      updateCardNoData('load', false);
    }
  });

  guard('ping', () => {
    // Ping (config-driven targets)
    const cfgTargets = dashboardConfig.ping_targets || DEFAULT_PING_TARGETS;
    if (ping.ok && ping.data.disabled) {
      updateCardNoData('ping', false);
      const noDataEl = document.querySelector('[data-card-id="ping"] .no-data-text');
      if (noDataEl) noDataEl.textContent = 'Disabled';
    } else if (ping.ok && ping.data.targets) {
      const targets = ping.data.targets;
      let worstMs = 0;
      cfgTargets.forEach((t, i) => {
        const data = targets[t.address];
        const ms = data?.latency_ms;
        if (history.pings[i]) pushHistory(history.pings[i], ms ?? null);
        const el = document.getElementById('ping-value-' + i);
        if (el) el.textContent = ms != null ? ms.toFixed(1) : '--';
        if (ms != null) worstMs = Math.max(worstMs, ms);
      });
      // This slot was actually measured (see history.pingGap).
      pushHistory(history.pingGap, false);
      updatePingLossBand();
      charts.ping.update();
      updateCardNoData('ping', true);
      updateCardWarning('ping', worstMs, thresholds.ping_ms);
    } else {
      updateCardNoData('ping', false);
    }
  });

  guard('processes', () => {
    // Store GPU processes for unified table joining
    if (gpuProcesses.ok && Array.isArray(gpuProcesses.data)) {
      lastGpuProcesses = gpuProcesses.data;
      lastGpuProcessesTime = Date.now();
    }

    // Unified Processes table (CPU, Memory, Network, GPU combined)
    if (processlist.ok && Array.isArray(processlist.data)) {
      if (!processlist.cached) {
        updateCache('processes', processlist.data);
      }
      renderUnifiedProcesses();
    } else {
      updateCardNoData('processes', false);
    }
  });

  // Update connection status based on all card states
  updateConnectionStatus();
}

// Startup may finish after Pause or Reconnect. Keep one timer per polling loop.
function startPolling() {
  if (!isConnected) return;
  if (pollingInterval === null) pollingInterval = setInterval(fetchData, pollInterval);
  if (slowPollingInterval === null) slowPollingInterval = setInterval(fetchSlowData, SLOW_POLL_INTERVAL);
}

// Toggle connection (pause/resume polling)
function toggleConnection() {
  const btn = document.getElementById('toggle-btn');
  const statusEl = document.getElementById('status');
  const statusText = statusEl.querySelector('.status-text');
  const spinner = statusEl.querySelector('.spinner');

  if (isConnected) {
    // Disconnect - stop polling
    clearInterval(pollingInterval);
    clearInterval(slowPollingInterval);
    pollingInterval = null;
    slowPollingInterval = null;
    disconnectTime = Date.now();
    isConnected = false;
    btn.textContent = 'Reconnect';
    btn.classList.add('disconnected');
    statusText.textContent = 'Paused';
    statusText.style.color = COLORS.errorLight;
    spinner.style.animationPlayState = 'paused';
  } else {
    // Reconnect - fill gaps with nulls for x-axis accuracy
    const missedIntervals = Math.floor((Date.now() - disconnectTime) / pollInterval);
    const fillCount = Math.min(missedIntervals, historyLength);
    fillHistoryGaps(fillCount);

    // Clear time-based caches to force fresh data on reconnect
    dataCache.drives.data = null;
    dataCache.drives.lastUpdate = null;
    dataCache.processes.data = null;
    dataCache.processes.lastUpdate = null;
    dataCache.networkProcesses.data = null;
    dataCache.networkProcesses.lastUpdate = null;
    dataCache.diskProcesses.data = null;
    dataCache.diskProcesses.lastUpdate = null;
    // Reconnect also refreshes metadata, while retaining the last readings
    // until their replacements arrive.
    for (const key of ['cpuInfo', 'systemInfo', 'gpuInfo']) dataCache[key].lastUpdate = null;

    // Update all charts to show the gaps
    charts.cpu.update();
    charts.cpuCoresSpread.update();
    charts.mem.update();
    charts.swap.update();
    charts.net.update();
    charts.disk.update();
    charts.gpu.update();
    charts.gpuMem.update();
    charts.ping.update();
    charts.diskDrives.update();

    // Resume polling
    isConnected = true;
    disconnectTime = null;
    fetchSlowData();
    fetchData();
    startPolling();
    btn.textContent = 'Disconnect';
    btn.classList.remove('disconnected');
    statusText.textContent = 'Connected';
    statusText.style.color = COLORS.success;
    spinner.style.animationPlayState = 'running';
  }
}

// Apply config to DOM (title, ping legend)
function applyConfigToDom() {
  const cfg = dashboardConfig;

  // Title: the placeholders in index.html say "Loading..."; a config without a
  // title must not leave that on screen.
  const h1 = document.getElementById('dashboard-title');
  if (h1) h1.textContent = cfg.title_emoji || cfg.title || 'Unknown Server, no config';
  document.title = cfg.title || cfg.title_emoji || 'Unknown Server, no config';

  // Ping targets — build header value and legend dynamically
  const targets = cfg.ping_targets || DEFAULT_PING_TARGETS;

  // Header value
  const pingValue = document.getElementById('ping-value');
  if (pingValue) {
    pingValue.innerHTML = targets.map((t, i) =>
      `<span class="color-ping-${i}">${escapeHtml(t.address)}</span> <span id="ping-value-${i}">--</span>`
    ).join(' ') + ' ms';
  }

  // Legend
  const pingLegend = document.getElementById('ping-legend');
  if (pingLegend) {
    pingLegend.innerHTML = targets.map((t, i) =>
      `<span class="legend-item"><span class="legend-dot bg-ping-${i}"></span>${escapeHtml(t.address)}</span>`
    ).join('');
  }

  // Initialize ping history arrays and chart datasets
  history.pings = targets.map(() => Array(historyLength).fill(null));
  const pingColors = targets.map((t, i) => getCssColor('--color-ping-' + i) || GPU_COLORS[i % GPU_COLORS.length]);
  charts.ping.data.datasets = targets.map((t, i) => ({
    label: t.address,
    data: history.pings[i],
    borderColor: pingColors[i],
    backgroundColor: pingColors[i] + '20',
    fill: true,
    tension: 0.4,
    pointRadius: 0,
    pointHoverRadius: 4,
    borderWidth: 2
  }));
  // Loss band (colored stripes) behind the lines, on a hidden 0..1 axis, plus the
  // shared latency+loss tooltip (latency-only rows; rolling %-up footer).
  charts.ping.data.datasets.unshift({
    label: 'loss', type: 'bar', yAxisID: 'yBand', order: 99,
    data: [], backgroundColor: [], borderWidth: 0, barPercentage: 1, categoryPercentage: 1
  });
  charts.ping.options.scales.yBand = { display: false, min: 0, max: 1 };
  charts.ping.options.plugins.tooltip = pingTooltipCfg(
    (i) => targets.map((t, k) => {
      const v = history.pings[k][i];
      return { name: t.address, color: pingColors[k], down: v == null && !history.pingGap[i], cur: v, min: null, max: null, pct: null };
    }),
    () => 'last ' + historyLength + ': ' + targets.map((t, k) => {
      const up = pingWinUp(history.pings[k], history.pingGap);
      return `${t.address} ${up == null ? '--' : up + '%'} up`;
    }).join('  ·  ')
  );
  charts.ping.update();
}

// Load config, discover endpoints, then start fetching data
loadConfig().then(async () => {
  applyConfigToDom();
  await fetchEndpointList();
  hideUnavailableCards();
  initProcessTableListeners();  // Set up event delegation once
  if (isConnected) {
    fetchSlowData();
    fetchData();
  }
}).finally(startPolling);

// Set up toggle button
document.getElementById('toggle-btn').addEventListener('click', toggleConnection);

// Set up poll interval selector
const pollSelectBtn = document.getElementById('poll-select');
const pollOptions = [
  { value: 1, label: '1s' },
  { value: 2, label: '2s' },
  { value: 5, label: '5s' },
];
pollSelectBtn.textContent = (pollInterval / 1000) + 's';
pollSelectBtn.addEventListener('click', () => {
  createDropdownPicker(pollSelectBtn, {
    options: pollOptions,
    currentValue: pollInterval / 1000,
    onSelect: (val) => {
      pollInterval = Math.round(val * 1000);
      pollSelectBtn.textContent = val + 's';
      localStorage.setItem('pollInterval', pollInterval);

      const timeCharts = [charts.cpu, charts.cpuCoresSpread, charts.mem, charts.swap, charts.gpu, charts.gpuMem, charts.net, charts.disk, charts.diskDrives, charts.ping];
      timeCharts.forEach(chart => chart.update());

      if (isConnected && pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = setInterval(fetchData, pollInterval);
      }
    },
    custom: { type: 'number', min: MIN_POLL_INTERVAL / 1000, max: 30, placeholder: 'secs' },
  });
});

// Set up process count selector
const processCountBtn = document.getElementById('process-count-select');
processCountBtn.textContent = topProcessesLimit;
updateProcessTableHeight();
processCountBtn.addEventListener('click', () => {
  createDropdownPicker(processCountBtn, {
    options: [5, 8, 10, 15, 20, 25].map(n => ({ value: n, label: String(n) })),
    currentValue: topProcessesLimit,
    onSelect: (val) => {
      topProcessesLimit = val;
      processCountBtn.textContent = val;
      localStorage.setItem('topProcessesLimit', topProcessesLimit);
      updateProcessTableHeight();
      const refreshBtn = document.querySelector('[data-refresh="processes"]');
      if (refreshBtn) refreshData('processes', refreshBtn);
    },
    custom: { type: 'int', min: 1, max: 50, placeholder: 'count' },
  });
});

// Set up history length selector
const historyLengthBtn = document.getElementById('history-length');
historyLengthBtn.textContent = historyLength;
historyLengthBtn.addEventListener('click', () => {
  createDropdownPicker(historyLengthBtn, {
    options: [30, 60, 120, 300].map(n => ({ value: n, label: String(n) })),
    currentValue: historyLength,
    onSelect: (val) => {
      historyLengthBtn.textContent = val;
      localStorage.setItem('historyLength', val);

      resizeHistory(val);

      const timeCharts = [charts.cpu, charts.cpuCoresSpread, charts.mem, charts.swap, charts.gpu, charts.gpuMem, charts.net, charts.disk, charts.diskDrives, charts.ping];
      timeCharts.forEach(chart => chart.update());
    },
    custom: { type: 'int', min: 10, max: 1000, placeholder: 'samples' },
  });
});

// Card drag-to-reorder functionality
(function() {
  const HOLD_DELAY = 200;
  let holdTimer = null;
  let dragSource = null;
  let dragHandle = null;
  let dragTarget = null;

  // Capture default card order from HTML
  const defaultCardOrder = Array.from(document.querySelectorAll('.grid .card[data-card-id]'))
    .map(c => c.dataset.cardId);

  // Restore saved card order
  function restoreCardOrder() {
    const saved = localStorage.getItem('cardOrder');
    if (!saved) return;
    try {
      const order = JSON.parse(saved);
      const grid = document.querySelector('.grid');
      const cards = Array.from(grid.querySelectorAll('.card[data-card-id]'));
      const cardMap = {};
      cards.forEach(c => cardMap[c.dataset.cardId] = c);
      order.forEach(id => {
        if (cardMap[id]) grid.appendChild(cardMap[id]);
      });
      // Cards added since the layout was saved keep their HTML position (at the
      // end), instead of being left in front of every re-appended card.
      cards.filter(c => !order.includes(c.dataset.cardId)).forEach(c => grid.appendChild(c));
    } catch (e) {
      console.warn('Failed to restore card order:', e);
    }
  }

  // Save current card order
  function saveCardOrder() {
    const cards = document.querySelectorAll('.grid .card[data-card-id]');
    const order = Array.from(cards).map(c => c.dataset.cardId);
    localStorage.setItem('cardOrder', JSON.stringify(order));
  }

  // Reset card order to default and reset history length
  function resetCardOrder() {
    localStorage.removeItem('cardOrder');
    const grid = document.querySelector('.grid');
    const cards = Array.from(grid.querySelectorAll('.card[data-card-id]'));
    const cardMap = {};
    cards.forEach(c => cardMap[c.dataset.cardId] = c);
    defaultCardOrder.forEach(id => {
      if (cardMap[id]) grid.appendChild(cardMap[id]);
    });

    // Reset history length to default
    const historyLengthInput = document.getElementById('history-length');
    historyLengthInput.textContent = DEFAULT_HISTORY_LENGTH;
    localStorage.removeItem('historyLength');
    resizeHistory(DEFAULT_HISTORY_LENGTH);

    const timeCharts = [charts.cpu, charts.cpuCoresSpread, charts.mem, charts.swap, charts.gpu, charts.gpuMem, charts.net, charts.disk, charts.diskDrives, charts.ping];
    timeCharts.forEach(chart => chart.update());
  }

  // Reset layout button handler
  document.getElementById('reset-layout-btn').addEventListener('click', resetCardOrder);

  // Swap two cards in the DOM
  function swapCards(a, b) {
    if (a === b) return;
    const grid = a.parentNode;
    const aNext = a.nextSibling;
    const bNext = b.nextSibling;
    if (aNext === b) {
      grid.insertBefore(b, a);
    } else if (bNext === a) {
      grid.insertBefore(a, b);
    } else {
      grid.insertBefore(a, bNext);
      grid.insertBefore(b, aNext);
    }
    saveCardOrder();
  }

  // Find card under mouse position
  function getCardAtPoint(x, y) {
    const cards = document.querySelectorAll('.grid .card[data-card-id]');
    for (const card of cards) {
      const rect = card.getBoundingClientRect();
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        return card;
      }
    }
    return null;
  }

  // Handle mouse move during drag
  function onMouseMove(e) {
    if (!dragSource) return;
    const card = getCardAtPoint(e.clientX, e.clientY);
    if (dragTarget && dragTarget !== card) {
      dragTarget.classList.remove('drag-over');
    }
    if (card && card !== dragSource) {
      card.classList.add('drag-over');
      dragTarget = card;
    } else {
      dragTarget = null;
    }
  }

  // Handle mouse up - end drag
  function onMouseUp() {
    clearTimeout(holdTimer);
    if (dragSource) {
      if (dragTarget) {
        dragTarget.classList.remove('drag-over');
        swapCards(dragSource, dragTarget);
      }
      dragSource.classList.remove('dragging');
      if (dragHandle) dragHandle.classList.remove('holding');
    }
    dragSource = null;
    dragHandle = null;
    dragTarget = null;
  }

  document.addEventListener('mousemove', onMouseMove);
  document.addEventListener('mouseup', onMouseUp);

  document.querySelectorAll('.drag-handle').forEach(handle => {
    const card = handle.closest('.card');

    handle.addEventListener('mousedown', e => {
      e.preventDefault();
      holdTimer = setTimeout(() => {
        handle.classList.add('holding');
        card.classList.add('dragging');
        dragSource = card;
        dragHandle = handle;
      }, HOLD_DELAY);
    });
  });

  restoreCardOrder();
  initMaxColumns();

  // Set card-specific tooltips on history and refresh buttons
  document.querySelectorAll('.card').forEach(card => {
    const name = card.querySelector('h2')?.textContent?.trim();
    if (!name) return;
    card.querySelectorAll('.history-btn').forEach(btn => {
      btn.title = `Show ${name} history`;
    });
    card.querySelectorAll('.refresh-btn').forEach(btn => {
      btn.title = `Refresh ${name}`;
    });
  });

  // Instant tooltips for [data-tip] elements (JS-positioned to escape overflow:hidden)
  const tipEl = document.createElement('div');
  tipEl.id = 'data-tip-tooltip';
  document.body.appendChild(tipEl);

  document.addEventListener('mouseenter', (e) => {
    // Capture-phase enter/leave also fire on document, which has no closest().
    const target = e.target instanceof Element ? e.target.closest('[data-tip]') : null;
    if (!target || !target.dataset.tip) return;
    tipEl.textContent = target.dataset.tip;
    tipEl.style.display = 'block';
    const rect = target.getBoundingClientRect();
    tipEl.style.left = rect.left + 'px';
    tipEl.style.top = (rect.top - tipEl.offsetHeight - 4) + 'px';
  }, true);

  document.addEventListener('mouseleave', (e) => {
    if (e.target instanceof Element && e.target.closest('[data-tip]')) tipEl.style.display = 'none';
  }, true);
})();

// Manual refresh handlers
async function refreshData(cacheKey, btn) {
  btn.classList.add('refreshing');
  // The cache is NOT cleared first: the fetch below is unconditional anyway, and
  // the fast poll draws fans/power/drive temps from the cache, so a cleared one
  // made those cards flash for a tick. updateCache on success replaces it.

  try {
    switch (cacheKey) {
      case 'gpuInfo':
        const gpuInfo = await safeFetch('/api/gpu-info', []);
        if (gpuInfo.ok) {
          updateCache('gpuInfo', gpuInfo.data);
          renderGpuInfo(gpuInfo.data);
        }
        break;

      case 'cpuInfo':
        const cpuInfo = await safeFetch('/api/cpu-info', {});
        if (cpuInfo.ok) {
          updateCache('cpuInfo', cpuInfo.data);
          renderCpuInfo(cpuInfo.data);
        }
        break;

      case 'systemInfo':
        const [sysInfo, uptime] = await Promise.all([
          safeFetch(API_BASE + '/system', {}),
          safeFetch(API_BASE + '/uptime', '')
        ]);
        if (sysInfo.ok) {
          updateCache('systemInfo', sysInfo.data);
          const uptimeStr = uptime.ok && uptime.data ? uptime.data.replace(/"/g, '') : '--';
          renderSystemInfo(sysInfo.data, uptimeStr);
        }
        break;

      case 'drives':
        const fs = await safeFetch(API_BASE + '/fs', []);
        if (fs.ok && Array.isArray(fs.data)) {
          updateCache('drives', fs.data);
          renderDrives(fs.data);
        }
        break;

      case 'ipInfo':
        const ipInfo = await safeFetch('/api/ip-info', {}, IP_INFO_TIMEOUT_MS);
        if (ipInfo.ok) {
          updateCache('ipInfo', ipInfo.data);
          renderIpInfo(ipInfo.data);
        }
        break;

      case 'mail':
        const mail = await safeFetch('/api/mail', {});
        if (mail.ok) {
          updateCache('mail', mail.data);
          renderMail(mail.data);
        } else if (mail.errorType !== 'unavailable') {
          document.getElementById('mail-content').innerHTML =
            '<div class="info-item text-error">Error loading mail</div>';
          updateCardNoData('mail', false);
        }
        break;

      case 'storageHealth':
        const storageHealth = await safeFetch('/api/storage-health', {});
        if (storageHealth.ok) {
          updateCache('storageHealth', storageHealth.data);
          renderHardware(storageHealth.data);
          renderDriveHealth(storageHealth.data);
        } else if (storageHealth.errorType !== 'unavailable') {
          document.getElementById('hardware-content').innerHTML =
            '<div class="info-item text-error">Error loading hardware health</div>';
          updateCardNoData('hardware', false);
          setHtml(document.getElementById('drive-health-content'),
            '<div class="info-item text-error">Error loading drive health</div>');
          updateCardNoData('drive-health', false);
        }
        break;

      case 'sensors': {
        // The fans / temps / power cards read the cache on the next fast poll.
        const sensorsSnap = await safeFetch('/api/sensors', {});
        if (sensorsSnap.ok) updateCache('sensors', sensorsSnap.data);
        break;
      }

      case 'processes':
        // Fetch all process data sources for unified table
        const [procs, gpuProcs] = await Promise.all([
          safeFetch('/api/processlist', []),
          safeFetch('/api/gpu-processes', [])
        ]);
        if (procs.ok && Array.isArray(procs.data)) {
          updateCache('processes', procs.data);
        }
        if (gpuProcs.ok && Array.isArray(gpuProcs.data)) {
          lastGpuProcesses = gpuProcs.data;
          lastGpuProcessesTime = Date.now();
        }
        renderUnifiedProcesses();
        break;
    }
  } finally {
    btn.classList.remove('refreshing');
  }
}

// Set up refresh button handlers
document.querySelectorAll('.refresh-btn').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    const cacheKey = btn.dataset.refresh;
    if (cacheKey) {
      refreshData(cacheKey, btn);
    }
  });
});

// Update cache timestamps periodically
const cacheDisplayInterval = setInterval(() => {
  Object.keys(dataCache).forEach(key => {
    const cardMap = {
      gpuInfo: 'gpu-info',
      cpuInfo: 'cpu-info',
      systemInfo: 'system-info',
      drives: 'drive-usage',
      processes: 'processes',
      ipInfo: 'ip-info',
      mail: 'mail'
      // not storageHealth: the hardware card's header shows the SNAPSHOT's age
      // (renderHardware), which this fetch-age ticker would overwrite every second.
    };
    const cardId = cardMap[key];
    if (cardId) {
      updateCacheDisplay(cardId, key);
    }
  });
}, 1000);

// Clean up intervals on page unload
window.addEventListener('beforeunload', () => {
  clearInterval(pollingInterval);
  clearInterval(slowPollingInterval);
  clearInterval(cacheDisplayInterval);
  networkCapture.stop();
  diskCapture.stop();
});

// ============================================================================
// History Feature
// ============================================================================

// Fetch history data from the server.
// Concurrent calls for the same time range share one request (e.g. "show all
// history" creates many cards at once — only one fetch should hit the server).
const historyFetchInflight = new Map();  // key -> Promise<data>  (concurrent-dedup)
// Per-range cache so multiple cards at the same range fetch once and share the
// result (every record carries all metrics). Shared object — READ ONLY across
// cards; never mutate data.records. Every fetch tops a cached window up
// (`since=`, only what is newer), so nothing drawn from the cache is stale.
const historyCache = new Map();          // key -> data
const HISTORY_CACHE_MAX = 16;            // bound growth from the free-form range input
// History reads can be larger/slower than a live poll (a long range over a big
// file), so allow more time than safeFetch's 5s default before giving up.
const HISTORY_FETCH_TIMEOUT_MS = 15000;
// A top-up asks from the server's clock at the last fetch minus this. A record's
// timestamp is taken before the GPU/sensor/process collection, so the manual and
// periodic recorders can append out of timestamp order by that collection time
// (seconds); the overlap re-asks for that stretch and the merge drops duplicates.
const HISTORY_SINCE_OVERLAP_SEC = 300;
let historyStaleWarned = false;      // stalled recorder: toast once, re-arm on recovery
let historyTruncatedWarned = false;  // truncated/skipped: toast once, re-armed by a forced refresh
let historySkippedWarned = false;

function _historyCacheSet(key, data) {
  historyCache.delete(key); historyCache.set(key, data);    // LRU: most recent last
  while (historyCache.size > HISTORY_CACHE_MAX) {
    historyCache.delete(historyCache.keys().next().value);  // evict oldest
  }
}

// Splice an incremental (`since=`) response onto the cached window: drop held
// records that aged out of the server's window (its cutoff, so clock skew is
// moot), add the new ones (skipping any already held), and sort by time. A new
// object: cards share the cached one. A truncated prefix stays relevant until
// it ages out of the requested window. A response without a cutoff
// did not honour since= (an older server process): it is the full window.
function mergeHistoryDelta(base, delta) {
  if (typeof delta.cutoff !== 'number') return delta;
  const cutoffMs = delta.cutoff * 1000;
  const held = new Set(base.records.map(r => r.timestamp));
  const records = base.records
    .filter(r => Date.parse(r.timestamp) >= cutoffMs)
    .concat((delta.records || []).filter(r => !held.has(r.timestamp)))
    .map(r => [Date.parse(r.timestamp), r])
    .sort((a, b) => a[0] - b[0])
    .map(([, r]) => r);
  // Once a held record predates the new cutoff, the omitted prefix is outside
  // the window. An empty cache cannot establish that boundary.
  const truncated = !!base.truncated && !base.records.some(r => Date.parse(r.timestamp) <= cutoffMs);
  return { ...delta, records, count: records.length, truncated };
}

// One history request, with success/failure preserved so callers can tell a
// real fetch failure apart from genuinely-empty history (both would otherwise
// look like records: []).
function requestHistory(hours, field, since) {
  let url = field ? `/api/history/field/${encodeURIComponent(field)}?hours=${hours}` : `/api/history?hours=${hours}`;
  if (since) url += `&since=${encodeURIComponent(since)}`;
  return safeFetch(url, { records: [], count: 0 }, HISTORY_FETCH_TIMEOUT_MS).then(response => {
    const data = response.data || { records: [], count: 0 };
    data.ok = response.ok;
    data.errorType = response.errorType;
    return data;
  });
}

// field: a heavy record field /api/history leaves out (HISTORY_METRICS[..].sideField);
// fetched alone from /api/history/field/<name>, cached under its own key.
// force (Refresh): don't join a fetch already in flight, and re-show the
// truncated / corrupt-lines toasts.
function fetchHistoryData(hours = defaultHistoryHours, { force = false, field = null } = {}) {
  const key = field ? `${field}:${hours}` : hours;
  // Share the request, not its failure policy: a toggle joining Refresh still
  // draws cached data on failure, while the Refresh caller sees the error.
  const forCaller = promise => force ? promise : promise.then(data =>
    data.ok ? data : (historyCache.get(key) || data));
  if (force) {
    historyTruncatedWarned = false;  // an explicit refresh re-surfaces a persistent truncation/skip
    historySkippedWarned = false;
  } else if (historyFetchInflight.has(key)) {
    // A card opened during a Refresh waits for the fresh data.
    return forCaller(historyFetchInflight.get(key));
  }
  const base = historyCache.get(key);
  // A backend that reports no clock (OpenWrt) can't be topped up: serve the
  // cached window; Refresh still refetches it in full.
  if (!force && base && typeof base.now !== 'number') return Promise.resolve(base);
  // Top up a cached window. Fetch in full when there is none, when the backend
  // doesn't report its clock (`now`: the OpenWrt one), or when the window has
  // corrupt lines (their count is only right for a whole-window read).
  const since = base && typeof base.now === 'number' && !(base.skipped_lines > 0)
    ? new Date((base.now - HISTORY_SINCE_OVERLAP_SEC) * 1000).toISOString() : null;
  const promise = requestHistory(hours, field, since)
    .then(data => {
      if (!data.ok || !since) return data;
      // A truncated delta leaves a gap; a clock rewind can also add an older
      // edge to the window that `since` excludes. Both need a full read.
      const rewound = (typeof data.now === 'number' && data.now < base.now) ||
        (typeof data.cutoff === 'number' && data.cutoff < base.cutoff);
      if (data.truncated || rewound) return requestHistory(hours, field, null);
      return mergeHistoryDelta(base, data);
    })
    .then(data => {
      // The toasts below are about the file as a whole; the main fetch raises them.
      if (data.ok && !field) {
        // Latch the corrupt-lines / truncated toasts so cache-miss refreshes
        // don't re-spam; reset when the condition clears (and on a forced refresh).
        if (data.skipped_lines > 0) {
          if (!historySkippedWarned) {
            console.warn(`History file has ${data.skipped_lines} corrupt lines (skipped)`);
            showError(`History: ${data.skipped_lines} corrupt lines skipped`);
            historySkippedWarned = true;
          }
        } else historySkippedWarned = false;
        if (data.truncated) {
          if (!historyTruncatedWarned) {
            showError(`History range too large — showing the most recent ${data.count} points`);
            historyTruncatedWarned = true;
          }
        } else historyTruncatedWarned = false;
        // Recorder-health: warn once if recording has stalled (so a dead recorder
        // is visible), and re-arm the warning once it recovers.
        if (data.recording) {
          if (data.recording.stale && !historyStaleWarned) {
            historyStaleWarned = true;
            const since = data.recording.last_success
              ? new Date(data.recording.last_success).toLocaleString()
              : 'never';
            const detail = data.recording.last_error ? `: ${data.recording.last_error}` : '';
            showError(`History recording stalled (last success ${since})${detail}`);
          } else if (!data.recording.stale) {
            historyStaleWarned = false;
          }
        }
      }
      // Cache successful reads only, and only while this is still the current
      // fetch for its key: one superseded by a Refresh, or dropped when the last
      // card closed (removeHistoryCard clears the in-flight map), must not
      // overwrite newer data or refill a cleared cache.
      if (data.ok && historyFetchInflight.get(key) === promise) _historyCacheSet(key, data);
      return data;
    })
    // Identity check: a forced refresh replaces the map entry with a newer
    // promise, and an older one settling later must not delete it.
    .finally(() => { if (historyFetchInflight.get(key) === promise) historyFetchInflight.delete(key); });
  historyFetchInflight.set(key, promise);
  return forCaller(promise);
}

// Markup for a failed history fetch — visibly distinct from the "No history
// data" empty state so a fetch failure never masquerades as "no data".
function historyErrorHTML(errorType) {
  const reason = errorType === 'timeout'
    ? 'The request timed out (the range may be very large).'
    : 'Could not reach the server.';
  return `
    <div class="no-history-data history-error">
      <p>History unavailable</p>
      <p class="hint">${reason} It will retry on the next refresh.</p>
    </div>
  `;
}

// Extract metric value from a history record
// rangeType: 'avg' | 'min' | 'max' - for metrics with hasMinMax
function extractHistoryValue(record, metric, rangeType = 'avg') {
  switch (metric) {
    case 'cpu':
      return record.cpu?.[rangeType] ?? null;
    case 'cpu_cores': {
      // Cross-core spread reuses the min/max-band renderer; the solid line
      // (rangeType 'avg') maps to the median.
      const c = record.cpu_cores;
      if (!c) return null;
      return rangeType === 'avg' ? (c.median ?? null) : (c[rangeType] ?? null);
    }
    case 'cpu_freq': {
      // Per-core clocks pooled over the burst: median line, min/max band (MHz).
      const f = record.cpu_freq;
      if (!f) return null;
      return rangeType === 'avg' ? (f.median ?? null) : (f[rangeType] ?? null);
    }
    case 'memory':
      return record.memory ? {
        percent: record.memory.percent,
        gb: record.memory.used_gb
      } : null;
    case 'network':
      // Return both in and out for dual charts
      return {
        in: record.network?.in_mbps?.[rangeType] ?? null,
        out: record.network?.out_mbps?.[rangeType] ?? null
      };
    case 'disk':
      return {
        read: record.disk_io?.read_mbps?.[rangeType] ?? null,
        write: record.disk_io?.write_mbps?.[rangeType] ?? null
      };
    case 'gpu':
      // Per-GPU utilization (dict like temps: { "GPU 0": value, ... })
      if (record.gpus && record.gpus.length > 0) {
        const result = {};
        record.gpus.forEach(g => {
          // ?? not || so an unavailable metric is a null gap, not a fake 0
          result['GPU ' + (g.index ?? 0)] = g.util_percent ?? null;
        });
        return result;
      }
      return null;
    case 'gpu-mem':
      // Per-GPU memory (array like drive-usage: [{ name, percent, gb }])
      if (record.gpus && record.gpus.length > 0) {
        return record.gpus.map(g => ({
          name: 'GPU ' + (g.index ?? 0),
          percent: g.mem_percent ?? null,
          gb: g.mem_used_gb ?? null
        }));
      }
      return null;
    case 'temps': {
      // Records keep every core; the chart shows the cores as one band.
      const t = record.temps;
      if (!t) return null;
      const { cores, rest } = aggregateCoreTemps(Object.entries(t).map(([label, value]) => ({ label, value })));
      const out = cores ? { 'Cores min': cores.min, 'Cores max': cores.max, 'Cores median': cores.median, 'Cores mean': cores.mean } : {};
      rest.forEach(e => { out[e.label] = e.value; });
      // The hottest reading of the record, whatever sensor it was (cores, board, GPUs).
      const all = Object.values(t).filter(v => typeof v === 'number' && Number.isFinite(v));
      out['Max'] = all.length ? Math.max(...all) : null;
      return out;
    }
    case 'fans':
    case 'power':
      return record[metric] ?? null;  // Full dict of sensor -> value
    case 'load':
      return record.load ? {
        min1: record.load.min1,
        min5: record.load.min5,
        min15: record.load.min15
      } : null;
    case 'swap':
      return record.swap ? {
        percent: record.swap.percent,
        gb: record.swap.used_gb
      } : null;
    case 'disk-drives':
      return record.disk_io?.drives ?? null;  // array of {name, read_mbps, write_mbps}
    case 'processes':
      return record.processes ?? null;  // array of {pid, name, cpu_percent, rss_mb, ...}, only when logging is on
    case 'drive-usage':
      // Return both percent and gb for each drive
      return record.drives?.map(d => ({
        mount: d.mount,
        percent: d.percent,
        gb: d.used_gb
      })) ?? null;
    case 'ping': {
      if (!record.ping) return null;
      const result = {};
      for (const [addr, data] of Object.entries(record.ping)) {
        result[addr] = data?.[rangeType] ?? null;
      }
      return result;
    }
    default:
      return null;
  }
}

// Create a history chart
// unitType: 'percent' | 'gb' - for metrics with hasUnitToggle
// The BMC's whole-box reading in the power snapshot; every other Watts row is a part of it.
const POWER_TOTAL_KEY = 'Pwr Consumption';

// mode: the card's modeToggle (config.js HISTORY_METRICS) when it is on, else ''. 'stack':
// power sources filled on top of each other; 'range': the fans as one min/max band.
function createHistoryChart(canvasId, metric, records, unitType = 'percent', hours = 24, mode = '') {
  const ctx = document.getElementById(canvasId);
  const metricInfo = HISTORY_METRICS[metric];
  const isDual = metricInfo.dual;
  const isTriple = metricInfo.triple;
  const isMultiLine = metricInfo.multiLine;
  const isPerDevice = metricInfo.perDevice;
  const selectedField = metricInfo.fieldSelect
    ? (metricInfo.fieldSelect.find(f => f.key === unitType) || metricInfo.fieldSelect[0]) : null;
  const displayUnit = selectedField ? selectedField.unit
    : metricInfo.hasUnitToggle ? (unitType === 'gb' ? metricInfo.altUnit : metricInfo.unit) : metricInfo.unit;

  // Extract data
  const labels = records.map(r => new Date(r.timestamp));

  let datasets;
  let showLegend = isDual || isTriple || isMultiLine || isPerDevice;

  if (isTriple) {
    // Triple-line chart (e.g., system load with 1m/5m/15m)
    const tripleLabels = metricInfo.labels || ['Line 1', 'Line 2', 'Line 3'];
    const tripleColors = metricInfo.colors || ['#f59e0b', '#eab308', '#ca8a04'];
    datasets = tripleLabels.map((label, i) => {
      const data = records.map(r => {
        const v = extractHistoryValue(r, metric);
        if (!v) return null;
        // For load: min1, min5, min15
        if (metric === 'load') {
          return i === 0 ? v.min1 : i === 1 ? v.min5 : v.min15;
        }
        return null;
      });
      return {
        label: label,
        data: data,
        borderColor: tripleColors[i],
        backgroundColor: tripleColors[i] + '20',
        fill: false,
        tension: 0,
        pointRadius: 2,
        pointHoverRadius: 6,
        borderWidth: 2,
        spanGaps: false
      };
    });
  } else if (isPerDevice) {
    // Per-device charts (disk-drives, drive-usage)
    // Collect all device names across all records
    const deviceNames = new Set();
    if (metric === 'disk-drives' || metric === 'drive-usage') {
      records.forEach(r => {
        const v = extractHistoryValue(r, metric);
        if (v && Array.isArray(v)) {
          v.forEach(d => {
            const name = metric === 'disk-drives' ? d.name : d.mount;
            if (name) deviceNames.add(name);
          });
        }
      });
    }

    const devices = Array.from(deviceNames);
    const colors = GPU_COLORS; // Reuse GPU colors for variety

    if (metric === 'disk-drives') {
      // Two lines per drive (read/write)
      datasets = [];
      devices.forEach((device, i) => {
        const readData = records.map(r => {
          const v = extractHistoryValue(r, metric);
          if (!v || !Array.isArray(v)) return null;
          const d = v.find(x => x.name === device);
          return d?.read_mbps?.avg ?? null;
        });
        const writeData = records.map(r => {
          const v = extractHistoryValue(r, metric);
          if (!v || !Array.isArray(v)) return null;
          const d = v.find(x => x.name === device);
          return d?.write_mbps?.avg ?? null;
        });
        datasets.push({
          label: `${device} Read`,
          data: readData,
          borderColor: colors[i % colors.length],
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false
        });
        datasets.push({
          label: `${device} Write`,
          data: writeData,
          borderColor: colors[i % colors.length],
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          borderDash: [5, 5],
          spanGaps: false
        });
      });
    } else if (metric === 'drive-usage') {
      // One line per drive (percentage or GB based on unitType)
      datasets = devices.map((device, i) => {
        const data = records.map(r => {
          const v = extractHistoryValue(r, metric);
          if (!v || !Array.isArray(v)) return null;
          const d = v.find(x => x.mount === device);
          return d?.[unitType] ?? null;
        });
        return {
          label: device,
          data: data,
          borderColor: colors[i % colors.length],
          backgroundColor: colors[i % colors.length] + '20',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false
        };
      });
    } else if (metric === 'processes') {
      // One line per process NAME (same-named pids summed: two wasmtime workers
      // are one line), for the 8 names with the largest total over the range;
      // more would only bury the legend. A record where the name is not among
      // the logged top-N is a gap, not a zero. The field comes from the card's
      // dropdown (its key rides in unitType); rows are sparse, a missing key is a gap.
      const key = selectedField.key;
      const perRecord = records.map(r => {
        const v = extractHistoryValue(r, metric);
        if (!Array.isArray(v)) return null;
        const sums = {};
        v.forEach(p => { if (p?.name && p[key] != null) sums[p.name] = (sums[p.name] || 0) + p[key]; });
        return sums;
      });
      const totals = {};
      perRecord.forEach(s => s && Object.entries(s).forEach(([n, x]) => { totals[n] = (totals[n] || 0) + x; }));
      const names = Object.keys(totals).sort((a, b) => totals[b] - totals[a]).slice(0, 8);
      datasets = names.map((name, i) => ({
        label: name,
        data: perRecord.map(s => s ? (s[name] ?? null) : null),
        borderColor: colors[i % colors.length],
        backgroundColor: 'transparent',
        fill: false,
        tension: 0,
        pointRadius: 2,
        pointHoverRadius: 6,
        borderWidth: 2,
        spanGaps: false
      }));
    } else if (metric === 'gpu') {
      // One line per GPU (dict format like temps: { "GPU 0": value, ... })
      const gpuNames = new Set();
      records.forEach(r => {
        const v = extractHistoryValue(r, metric);
        if (v && typeof v === 'object' && !Array.isArray(v)) {
          Object.keys(v).forEach(k => gpuNames.add(k));
        }
      });
      const gpus = Array.from(gpuNames);
      datasets = gpus.map((gpuName, i) => {
        const data = records.map(r => {
          const v = extractHistoryValue(r, metric);
          return v?.[gpuName] ?? null;
        });
        return {
          label: gpuName,
          data: data,
          borderColor: colors[i % colors.length],
          backgroundColor: colors[i % colors.length] + '20',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false
        };
      });
    } else if (metric === 'gpu-mem') {
      // One line per GPU (array format like drive-usage: [{ name, percent, gb }])
      const gpuNames = new Set();
      records.forEach(r => {
        const v = extractHistoryValue(r, metric);
        if (v && Array.isArray(v)) {
          v.forEach(g => { if (g.name) gpuNames.add(g.name); });
        }
      });
      const gpus = Array.from(gpuNames);
      datasets = gpus.map((gpuName, i) => {
        const data = records.map(r => {
          const v = extractHistoryValue(r, metric);
          if (!v || !Array.isArray(v)) return null;
          const g = v.find(x => x.name === gpuName);
          return g?.[unitType] ?? null;
        });
        return {
          label: gpuName,
          data: data,
          borderColor: colors[i % colors.length],
          backgroundColor: colors[i % colors.length] + '20',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false
        };
      });
    } else if (metric === 'temps' || metric === 'fans' || metric === 'power') {
      // One line per sensor. Temps: extractHistoryValue has already collapsed
      // the CPU cores into Cores min/max/median/mean, drawn as a band (min and
      // max dashed with a fill between, median solid, mean dotted). Fans in
      // range mode: the same band over all fans (Fans min/max/median/mean).
      // Power in stacked mode: every source except the BMC total plus the
      // remainder as "other", filled on top of each other; the stack's top edge
      // is then the BMC total, or the measured sum on the rare slot where that
      // exceeds it, so no separate total line.
      // Extract each record once (temps sorts ~40 core readings per call) and
      // index the result per dataset, instead of sensors x records extractions.
      let values = records.map(r => extractHistoryValue(r, metric));
      const band = metric === 'temps' ? 'Cores' : metric === 'fans' && mode === 'range' ? 'Fans' : null;
      if (band === 'Fans') {
        values = values.map(v => {
          const s = v && rangeStats(Object.values(v));
          return s ? { 'Fans min': s.min, 'Fans max': s.max, 'Fans median': s.median, 'Fans mean': s.mean } : null;
        });
      }
      const sensorNames = new Set();
      values.forEach(v => {
        if (v && typeof v === 'object' && !Array.isArray(v)) {
          Object.keys(v).forEach(k => sensorNames.add(k));
        }
      });
      const stack = metric === 'power' && mode === 'stack';
      let sensors = Array.from(sensorNames);
      if (stack) {
        const rank = l => l.startsWith('GPU') ? 0 : l.startsWith('CPU') ? 1 : l.startsWith('DRAM') ? 2 : 3;
        sensors = sensors.filter(s => s !== POWER_TOTAL_KEY).sort((a, b) => rank(a) - rank(b)).concat('other');
      }
      // Band traces first, max right after min: its fill targets the previous dataset.
      const BAND_ORDER = band ? ['min', 'max', 'median', 'mean'].map(s => `${band} ${s}`) : [];
      if (band) sensors = BAND_ORDER.filter(s => sensorNames.has(s)).concat(sensors.filter(s => !BAND_ORDER.includes(s)));
      const BAND_STYLE = band ? {
        [`${band} min`]: { borderDash: [4, 4], borderWidth: 1, pointRadius: 0 },
        [`${band} max`]: { borderDash: [4, 4], borderWidth: 1, pointRadius: 0, fill: '-1' },
        [`${band} mean`]: { borderDash: [2, 3], borderWidth: 1, pointRadius: 0 },
      } : {};
      datasets = sensors.map((sensor, i) => {
        const data = values.map(v => {
          if (sensor === 'other') {
            if (!v || v[POWER_TOTAL_KEY] == null) return null;
            const parts = Object.entries(v).filter(([k]) => k !== POWER_TOTAL_KEY).reduce((s, [, x]) => s + (x ?? 0), 0);
            return Math.max(0, v[POWER_TOTAL_KEY] - parts);
          }
          return v?.[sensor] ?? null;
        });
        const color = band && sensor.startsWith(band + ' ') ? colors[0] : colors[i % colors.length];
        return {
          label: sensor,
          data: data,
          borderColor: color,
          backgroundColor: stack ? color + '60' : sensor === `${band} max` ? color + '15' : 'transparent',
          fill: stack ? (i === 0 ? 'origin' : '-1') : false,
          tension: 0,
          pointRadius: stack ? 0 : 2,
          pointHoverRadius: 6,
          borderWidth: stack ? 1 : 2,
          spanGaps: false,
          ...(BAND_STYLE[sensor] || {})
        };
      });
    }
  } else if (isDual) {
    const label1 = metric === 'network' ? 'In' : 'Read';
    const label2 = metric === 'network' ? 'Out' : 'Write';
    const key1 = metric === 'network' ? 'in' : 'read';
    const key2 = metric === 'network' ? 'out' : 'write';

    const data1 = records.map(r => {
      const v = extractHistoryValue(r, metric);
      return v ? v[key1] : null;
    });
    const data2 = records.map(r => {
      const v = extractHistoryValue(r, metric);
      return v ? v[key2] : null;
    });

    if (metricInfo.hasMinMax) {
      // Extract min/max for both lines
      const min1 = records.map(r => {
        const v = extractHistoryValue(r, metric, 'min');
        return v ? v[key1] : null;
      });
      const max1 = records.map(r => {
        const v = extractHistoryValue(r, metric, 'max');
        return v ? v[key1] : null;
      });
      const min2 = records.map(r => {
        const v = extractHistoryValue(r, metric, 'min');
        return v ? v[key2] : null;
      });
      const max2 = records.map(r => {
        const v = extractHistoryValue(r, metric, 'max');
        return v ? v[key2] : null;
      });

      // Dataset order: line1 min, line1 max (fill to min), line1 avg, line2 min, line2 max (fill to min), line2 avg
      datasets = [
        // Line 1 (In/Read) min/max/avg
        {
          label: label1 + ' Min',
          data: min1,
          borderColor: metricInfo.colors[0],
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 1,
          borderDash: [4, 4],
          spanGaps: false,
          hidden: true,
          rangeType: 'min'
        },
        {
          label: label1 + ' Max',
          data: max1,
          borderColor: metricInfo.colors[0],
          backgroundColor: metricInfo.colors[0] + '15',
          fill: false, // Filled between min/max when both enabled
          tension: 0,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 1,
          borderDash: [4, 4],
          spanGaps: false,
          hidden: true,
          rangeType: 'max'
        },
        {
          label: label1,
          data: data1,
          borderColor: metricInfo.colors[0],
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false,
          rangeType: 'avg'
        },
        // Line 2 (Out/Write) min/max/avg
        {
          label: label2 + ' Min',
          data: min2,
          borderColor: metricInfo.colors[1],
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 1,
          borderDash: [4, 4],
          spanGaps: false,
          hidden: true,
          rangeType: 'min'
        },
        {
          label: label2 + ' Max',
          data: max2,
          borderColor: metricInfo.colors[1],
          backgroundColor: metricInfo.colors[1] + '15',
          fill: false, // Filled between min/max when both enabled
          tension: 0,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 1,
          borderDash: [4, 4],
          spanGaps: false,
          hidden: true,
          rangeType: 'max'
        },
        {
          label: label2,
          data: data2,
          borderColor: metricInfo.colors[1],
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false,
          rangeType: 'avg'
        }
      ];
    } else {
      datasets = [
        {
          label: label1,
          data: data1,
          borderColor: metricInfo.colors[0],
          backgroundColor: metricInfo.colors[0] + '20',
          fill: true,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false
        },
        {
          label: label2,
          data: data2,
          borderColor: metricInfo.colors[1],
          backgroundColor: metricInfo.colors[1] + '20',
          fill: true,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false
        }
      ];
    }
  } else if (isMultiLine) {
    // N-line chart (e.g., ping with config-driven targets)
    const pt = dashboardConfig.ping_targets || DEFAULT_PING_TARGETS;
    const colors = pt.map((_, i) => getCssColor('--color-ping-' + i) || GPU_COLORS[i % GPU_COLORS.length]);

    if (metricInfo.hasMinMax) {
      datasets = [];
      pt.forEach((t, i) => {
        const avg = records.map(r => { const v = extractHistoryValue(r, metric); return v ? v[t.address] : null; });
        const min = records.map(r => { const v = extractHistoryValue(r, metric, 'min'); return v ? v[t.address] : null; });
        const max = records.map(r => { const v = extractHistoryValue(r, metric, 'max'); return v ? v[t.address] : null; });
        datasets.push(
          { label: t.address + ' Min', data: min, borderColor: colors[i], backgroundColor: 'transparent', fill: false, tension: 0, pointRadius: 0, pointHoverRadius: 4, borderWidth: 1, borderDash: [4, 4], spanGaps: false, hidden: true, rangeType: 'min' },
          { label: t.address + ' Max', data: max, borderColor: colors[i], backgroundColor: colors[i] + '15', fill: false, tension: 0, pointRadius: 0, pointHoverRadius: 4, borderWidth: 1, borderDash: [4, 4], spanGaps: false, hidden: true, rangeType: 'max' },
          { label: t.address, data: avg, borderColor: colors[i], backgroundColor: 'transparent', fill: false, tension: 0, pointRadius: 2, pointHoverRadius: 6, borderWidth: 2, spanGaps: false, rangeType: 'avg' }
        );
      });
    } else {
      datasets = pt.map((t, i) => {
        const data = records.map(r => { const v = extractHistoryValue(r, metric); return v ? v[t.address] : null; });
        return { label: t.address, data, borderColor: colors[i], backgroundColor: colors[i] + '20', fill: true, tension: 0, pointRadius: 2, pointHoverRadius: 6, borderWidth: 2, spanGaps: false };
      });
    }
    // Ping loss band: faint fill (partial) + colored stripes (full outage), one
    // colour per target. Read the RAW record (extractHistoryValue drops loss);
    // absent targets contribute no loss (a plain gap, not a false failure).
    if (metric === 'ping') {
      const slotLosses = records.map(r =>
        pt.map(t => pingLoss(r && r.ping ? r.ping[t.address] : undefined) || 0)
      );
      datasets.push({
        label: 'loss', type: 'bar', yAxisID: 'yBand', order: 99,
        borderWidth: 0, barPercentage: 1, categoryPercentage: 1,
        data: slotLosses.map(ls => ls.some(l => l > 0) ? 1 : null),
        backgroundColor: slotLosses.map(ls => ls.some(l => l > 0)
          ? pingBandPattern(pt.map((t, i) => ({ color: colors[i], loss: ls[i] })))
          : 'rgba(0,0,0,0)')
      });
    }
  } else {
    // For metrics with unit toggle, extract the selected unit (percent or gb)
    const data = records.map(r => {
      const v = extractHistoryValue(r, metric);
      if (metricInfo.hasUnitToggle && v && typeof v === 'object') {
        return v[unitType] ?? null;
      }
      return v;
    });

    if (metricInfo.hasMinMax) {
      // For metrics with min/max, create three datasets: min, max (with fill to min), avg
      const minData = records.map(r => extractHistoryValue(r, metric, 'min'));
      const maxData = records.map(r => extractHistoryValue(r, metric, 'max'));
      // Some metrics (e.g. cpu_cores) name their centre line (median) and show
      // the min/max band by default rather than hiding it behind the toggles.
      const showBand = !!metricInfo.showMinMaxByDefault;
      const centerLabel = metricInfo.centerLabel
        ? metricInfo.name + ' ' + metricInfo.centerLabel
        : metricInfo.name;
      datasets = [
        {
          label: metricInfo.name + ' Min',
          data: minData,
          borderColor: metricInfo.color,
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 1,
          borderDash: [4, 4],
          spanGaps: false,
          hidden: !showBand,
          rangeType: 'min'
        },
        {
          label: metricInfo.name + ' Max',
          data: maxData,
          borderColor: metricInfo.color,
          backgroundColor: metricInfo.color + '15',
          fill: showBand ? '-1' : false, // Filled between min/max when both enabled
          tension: 0,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 1,
          borderDash: [4, 4],
          spanGaps: false,
          hidden: !showBand,
          rangeType: 'max'
        },
        {
          label: centerLabel,
          data: data,
          borderColor: metricInfo.color,
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 2,
          pointHoverRadius: 6,
          borderWidth: 2,
          spanGaps: false,
          rangeType: 'avg'
        }
      ];
      showLegend = true;
    } else {
      datasets = [{
        label: metricInfo.name,
        data: data,
        borderColor: metricInfo.color,
        backgroundColor: metricInfo.color + '20',
        fill: true,
        tension: 0,
        pointRadius: 2,
        pointHoverRadius: 6,
        borderWidth: 2,
        spanGaps: false
      }];
    }
  }

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    // Hover picks the nearest time and lists every visible trace there (the band
    // lines and the stacked areas have no points to hit, so a point-based hit
    // test could never show them).
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: showLegend, position: 'top', labels: { color: CHART_TICK.color, boxWidth: 12 } },
      tooltip: {
        filter: (item) => item.parsed.y != null,
        callbacks: {
          title: (ctx) => new Date(ctx[0].parsed.x).toLocaleString(),
          label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}${displayUnit}`,
          // Stacked power: the rows are the parts, so add their sum.
          footer: (items) => metric === 'power' && mode === 'stack' && items.length > 1
            ? `total ${items.reduce((s, it) => s + it.parsed.y, 0).toFixed(0)}${displayUnit}` : ''
        }
      }
    },
    scales: {
      x: {
        type: 'time',
        time: {
          unit: hours < 1 ? 'minute' : hours > 48 ? 'day' : 'hour',
          displayFormats: { minute: 'HH:mm', hour: 'HH:mm', day: 'MMM d' }
        },
        ticks: { ...CHART_TICK, maxRotation: 0 },
        grid: CHART_GRID
      },
      y: { min: 0, stacked: metric === 'power' && mode === 'stack', ticks: CHART_TICK, grid: CHART_GRID }
    },
    animation: { duration: 0 }
  };
  // Ping: hidden band axis + shared colored-stripe tooltip (loss/latency table).
  if (metric === 'ping') {
    options.scales.yBand = { display: false, min: 0, max: 1 };
    options.plugins.legend.labels.filter = (it) => it.text !== 'loss';
    const ptp = dashboardConfig.ping_targets || DEFAULT_PING_TARGETS;
    const cols = ptp.map((_, i) => getCssColor('--color-ping-' + i) || GPU_COLORS[i % GPU_COLORS.length]);
    options.plugins.tooltip = pingTooltipCfg(
      (i) => ptp.map((t, k) => {
        const e = records[i] && records[i].ping ? records[i].ping[t.address] : undefined;
        const loss = pingLoss(e);
        return {
          name: t.address, color: cols[k], down: loss >= 1,
          cur: e && e.avg != null ? e.avg : null, min: e ? e.min : null, max: e ? e.max : null,
          pct: loss == null ? null : Math.round((1 - loss) * 100)
        };
      }),
      null,
      (i) => new Date(records[i].timestamp).toLocaleString()
    );
  }
  return new Chart(ctx, { type: 'line', data: { labels, datasets }, options });
}

// Create a history card for a metric
async function createHistoryCard(metric, hours = defaultHistoryHours) {
  // Check if card already exists
  if (historyCards.has(metric)) {
    // Just update the data
    await loadHistoryData(metric, hours);
    return;
  }

  const metricInfo = HISTORY_METRICS[metric];
  if (!metricInfo) return;

  const cardId = `history-${metric}-${Date.now()}`;
  const canvasId = `chart-${cardId}`;

  // Create card element
  const card = document.createElement('div');
  card.className = 'card card-full-width history-card loading';
  card.dataset.historyMetric = metric;
  card.dataset.historyId = cardId;

  // Build datalist options for presets
  const datalistId = `history-presets-${cardId}`;
  const presetOptions = HISTORY_TIME_RANGES.map(r =>
    `<option value="${r.value}" label="${r.label}">`
  ).join('');

  // Build min/max toggles if applicable
  const rangeChecked = metricInfo.showMinMaxByDefault ? ' checked' : '';
  const rangeToggles = metricInfo.hasMinMax ? `
        <span class="history-range-toggles">
          <label class="range-toggle"><input type="checkbox" data-range="min"${rangeChecked}> min</label>
          <label class="range-toggle"><input type="checkbox" data-range="max"${rangeChecked}> max</label>
        </span>` : '';

  // Build unit toggle if applicable (GB/% toggle for memory metrics)
  const unitToggle = metricInfo.hasUnitToggle ? `
        <button class="unit-toggle-btn" data-unit="percent" title="Toggle ${escapeHtml(metricInfo.altUnit.trim())}/${escapeHtml(metricInfo.unit.trim())}">${escapeHtml(metricInfo.unit.trim())}</button>` : '';
  const modeToggle = metricInfo.modeToggle ? `
        <button class="mode-toggle-btn" title="${escapeHtml(metricInfo.modeToggle.title)}">${escapeHtml(metricInfo.modeToggle.mode)}</button>` : '';
  // Which per-process stat to draw (HISTORY_METRICS[..].fieldSelect).
  const fieldSelect = metricInfo.fieldSelect ? `
        <select class="field-select" title="Which stat to draw">${metricInfo.fieldSelect.map(f =>
          `<option value="${escapeHtml(f.key)}">${escapeHtml(f.label)}</option>`).join('')}</select>` : '';
  // Trace selection: "none" then a legend click is the quick way to look at one trace.
  const traceButtons = `
        <button class="trace-btn" data-traces="none" title="Hide every trace (click a legend entry to show one)">none</button>
        <button class="trace-btn" data-traces="all" title="Show every trace">all</button>`;

  card.innerHTML = `
    <div class="drag-handle"></div>
    <div class="card-header">
      <h2>${metricInfo.name} History</h2>
      <div class="history-controls">
        ${rangeToggles}${unitToggle}${modeToggle}${fieldSelect}${traceButtons}<label class="history-range-label">hours:</label>
        <input type="number" class="history-range-input" data-metric="${metric}"
               value="${hours}" min="0.1" step="any" max="8760" list="${datalistId}" title="Time range in hours">
        <datalist id="${datalistId}">${presetOptions}</datalist>
        <button class="history-refresh-btn" title="Refresh">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 2.5v4h-4"/><path d="M2.5 13.5v-4h4"/><path d="M3.5 6a5.5 5.5 0 0 1 9.2-1.5l.8 1"/><path d="M12.5 10a5.5 5.5 0 0 1-9.2 1.5l-.8-1"/></svg>
        </button>
        <button class="history-close-btn" title="Close">×</button>
      </div>
    </div>
    <div class="chart-container history-chart-container">
      <canvas id="${canvasId}"></canvas>
    </div>
  `;

  // Append to grid
  document.querySelector('.grid').appendChild(card);

  // Set up event listeners
  card.querySelector('.history-close-btn').addEventListener('click', () => {
    removeHistoryCard(metric);
  });

  card.querySelector('.history-refresh-btn').addEventListener('click', refreshAllHistory);

  // Debounced input handler for custom time range
  // Create entry early so we can store debounce timer for cleanup.
  // loadSeq: monotonic per-card counter; loadHistoryData drops any response
  // that is no longer the newest request for this card (see there).
  // unitType: 'percent' | 'gb' for hasUnitToggle metrics, the selected field's key for fieldSelect ones.
  const entry = { card, chart: null, debounceTimer: null, unitType: metricInfo.fieldSelect ? metricInfo.fieldSelect[0].key : 'percent', mode: '', loadSeq: 0 };
  historyCards.set(metric, entry);

  const rangeInput = card.querySelector('.history-range-input');

  const handleRangeChange = async () => {
    const value = parseFloat(rangeInput.value);
    if (isNaN(value) || value < 0.1) {
      rangeInput.classList.add('invalid');
      return;
    }
    rangeInput.classList.remove('invalid');
    const clampedValue = Math.min(Math.max(value, 0.1), 8760);
    if (clampedValue !== value) {
      rangeInput.value = clampedValue;
    }
    await loadHistoryData(metric, clampedValue);
  };

  // A change (Enter / blur) also cancels the pending debounced load of the same value.
  rangeInput.addEventListener('change', () => { clearTimeout(entry.debounceTimer); handleRangeChange(); });

  rangeInput.addEventListener('input', () => {
    clearTimeout(entry.debounceTimer);
    entry.debounceTimer = setTimeout(handleRangeChange, 500);
  });

  // Set up min/max toggle handlers if applicable
  if (metricInfo.hasMinMax) {
    const toggles = card.querySelectorAll('.range-toggle input');
    const minToggle = card.querySelector('.range-toggle input[data-range="min"]');
    const maxToggle = card.querySelector('.range-toggle input[data-range="max"]');

    const updateRangeVisibility = () => {
      if (!entry.chart) return;
      const showMin = minToggle.checked;
      const showMax = maxToggle.checked;

      // Update visibility and fill for all datasets
      entry.chart.data.datasets.forEach((ds, i) => {
        if (ds.rangeType === 'min') {
          entry.chart.setDatasetVisibility(i, showMin);
        } else if (ds.rangeType === 'max') {
          entry.chart.setDatasetVisibility(i, showMax);
          // Only fill between min and max when both are enabled
          ds.fill = (showMin && showMax) ? '-1' : false;
        }
      });
      entry.chart.update();
    };

    toggles.forEach(toggle => {
      toggle.addEventListener('change', updateRangeVisibility);
    });
  }

  // Set up unit toggle handler if applicable (GB/% toggle)
  if (metricInfo.hasUnitToggle) {
    const unitBtn = card.querySelector('.unit-toggle-btn');
    unitBtn.addEventListener('click', async () => {
      // Toggle between percent and gb
      const currentUnit = unitBtn.dataset.unit;
      const newUnit = currentUnit === 'percent' ? 'gb' : 'percent';
      unitBtn.dataset.unit = newUnit;
      unitBtn.textContent = (newUnit === 'percent' ? metricInfo.unit : metricInfo.altUnit).trim();
      entry.unitType = newUnit;
      // loadHistoryData reads entry.unitType when it rebuilds the chart.
      const hours = parseFloat(card.querySelector('.history-range-input').value) || defaultHistoryHours;
      await loadHistoryData(metric, hours);
    });
  }

  if (metricInfo.fieldSelect) {
    card.querySelector('.field-select').addEventListener('change', async (e) => {
      entry.unitType = e.target.value;
      const hours = parseFloat(card.querySelector('.history-range-input').value) || defaultHistoryHours;
      await loadHistoryData(metric, hours);
    });
  }

  if (metricInfo.modeToggle) {
    const modeBtn = card.querySelector('.mode-toggle-btn');
    modeBtn.addEventListener('click', async () => {
      entry.mode = entry.mode ? '' : metricInfo.modeToggle.mode;
      modeBtn.classList.toggle('active', !!entry.mode);
      const hours = parseFloat(rangeInput.value) || defaultHistoryHours;
      await loadHistoryData(metric, hours);
    });
  }

  card.querySelectorAll('.trace-btn').forEach(btn => btn.addEventListener('click', () => {
    if (!entry.chart) return;
    const show = btn.dataset.traces === 'all';
    entry.chart.data.datasets.forEach((_, i) => entry.chart.setDatasetVisibility(i, show));
    // Tick the min/max boxes and fire `change`, so updateRangeVisibility reasserts
    // the band's `fill` (setting .checked alone does not, leaving an unshaded band).
    card.querySelectorAll('.range-toggle input').forEach(cb => {
      cb.checked = show;
      cb.dispatchEvent(new Event('change'));
    });
    entry.chart.update();
  }));

  // Initial load. Marking the button active here (not inside loadHistoryData)
  // keeps it tied to "card exists", which removeHistoryCard undoes.
  await loadHistoryData(metric, hours);
  if (historyCards.get(metric) !== entry) return;   // closed while the first fetch was in flight
  card.classList.remove('loading');
  const historyBtn = document.querySelector(`[data-history-metric="${metric}"]`);
  if (historyBtn) historyBtn.classList.add('active');
}

// Load/refresh history data for an existing card. This is the ONLY place a
// history chart is created or destroyed, so the staleness gate below covers
// every path (initial load, hours input, unit toggle, refresh).
async function loadHistoryData(metric, hours, data) {
  const entry = historyCards.get(metric);
  if (!entry) return;
  const mySeq = ++entry.loadSeq;

  if (!data) data = await fetchHistoryData(hours, { field: HISTORY_METRICS[metric].sideField || null });

  // Stale-response gate. Requests for one card can resolve out of order (a
  // 720 h fetch is slower than the 1 h fetch typed after it), and a load that
  // started before the card was closed/reopened must not touch the new card.
  // A per-`hours` counter cannot express this — each request is the newest for
  // its own range — so the counter is per card.
  if (historyCards.get(metric) !== entry || entry.loadSeq !== mySeq) return;

  // Transient fetch failure: keep the existing chart (don't flash an error box
  // over good data on a periodic refresh) and just toast; only show the error
  // box when there's no chart to preserve.
  if (!data.ok) {
    if (entry.chart) {
      showError(`History refresh failed (${data.errorType || 'error'}) — showing last data`);
    } else {
      entry.card.querySelector('.chart-container').innerHTML = historyErrorHTML(data.errorType);
    }
    return;
  }

  if (data.records.length === 0) {
    entry.card.querySelector('.chart-container').innerHTML = `
      <div class="no-history-data">
        <p>No history data for this time range</p>
        <p class="hint">Data is recorded periodically. Use POST /api/history/record to trigger a manual recording.</p>
      </div>
    `;
    if (entry.chart) {
      entry.chart.destroy();
      entry.chart = null;
    }
    return;
  }

  // Destroy and recreate chart to ensure all datasets, axes, and scales
  // are correct for the new time range. The user's trace selection (legend
  // clicks, "none") is kept across the rebuild, matched by dataset label.
  const hiddenByLabel = new Map();
  if (entry.chart) {
    entry.chart.data.datasets.forEach((ds, i) => hiddenByLabel.set(ds.label, !entry.chart.isDatasetVisible(i)));
    entry.chart.destroy();
  }
  const canvasId = `chart-${entry.card.dataset.historyId}`;
  entry.card.querySelector('.chart-container').innerHTML = `<canvas id="${canvasId}"></canvas>`;
  entry.chart = createHistoryChart(canvasId, metric, data.records, entry.unitType || 'percent', hours, entry.mode);
  if (hiddenByLabel.size) {
    entry.chart.data.datasets.forEach((ds, i) => {
      if (hiddenByLabel.has(ds.label)) entry.chart.setDatasetVisibility(i, !hiddenByLabel.get(ds.label));
    });
    entry.chart.update();
  }

  // Restore min/max toggle state. The rebuilt chart starts from the metric's
  // config default (metrics with showMinMaxByDefault start VISIBLE), so this
  // must run unconditionally — gating it on "a box is checked" let an
  // unchecked-by-the-user band reappear on every refresh.
  const metricInfo = HISTORY_METRICS[metric];
  if (metricInfo.hasMinMax) {
    const minToggle = entry.card.querySelector('.range-toggle input[data-range="min"]');
    const maxToggle = entry.card.querySelector('.range-toggle input[data-range="max"]');
    entry.chart.data.datasets.forEach((ds, i) => {
      if (ds.rangeType === 'min') entry.chart.setDatasetVisibility(i, !!minToggle?.checked);
      else if (ds.rangeType === 'max') {
        entry.chart.setDatasetVisibility(i, !!maxToggle?.checked);
        ds.fill = (minToggle?.checked && maxToggle?.checked) ? '-1' : false;
      }
    });
    entry.chart.update();
  }
}

// Remove a history card
function removeHistoryCard(metric) {
  const entry = historyCards.get(metric);
  if (entry) {
    // Clear pending debounce timer to prevent memory leak
    if (entry.debounceTimer) clearTimeout(entry.debounceTimer);
    if (entry.chart) entry.chart.destroy();
    entry.card.remove();
    historyCards.delete(metric);
    // Last card closed → drop the shared cache so a later "show" refetches.
    // In-flight fetches go too, so a card reopened meanwhile starts a fresh
    // fetch that is cached, instead of joining one whose result is discarded.
    if (historyCards.size === 0) { historyCache.clear(); historyFetchInflight.clear(); }

    // Remove active state from history button
    const historyBtn = document.querySelector(`[data-history-metric="${metric}"]`);
    if (historyBtn) historyBtn.classList.remove('active');
  }
}

// Show history for all metrics
function showAllHistory() {
  const metrics = Object.keys(HISTORY_METRICS);
  metrics.forEach(metric => {
    if (!historyCards.has(metric)) {
      createHistoryCard(metric);
    }
  });
}

// Hide all history cards
function hideAllHistory() {
  const metrics = Array.from(historyCards.keys());
  metrics.forEach(metric => {
    removeHistoryCard(metric);
  });
}

// Set up history button handlers (event delegation)
document.querySelector('.grid').addEventListener('click', (e) => {
  const historyBtn = e.target.closest('.history-btn');
  if (historyBtn) {
    const metric = historyBtn.dataset.historyMetric;

    // Fire-and-forget: record current data (server rate-limits to 1/min)
    fetch('/api/history/record', { method: 'POST' }).catch(e => console.warn('history record failed:', e));

    if (historyCards.has(metric)) {
      removeHistoryCard(metric);
    } else {
      createHistoryCard(metric);
    }
  }
});

// Set up global history controls
document.getElementById('show-all-history-btn')?.addEventListener('click', () => {
  // Fire-and-forget: record current data (server rate-limits to 1/min)
  fetch('/api/history/record', { method: 'POST' }).catch(e => console.warn('history record failed:', e));
  showAllHistory();
});
document.getElementById('hide-all-history-btn')?.addEventListener('click', hideAllHistory);
async function refreshAllHistory() {
  const toolbarBtn = document.getElementById('refresh-history-btn');
  const cardBtns = document.querySelectorAll('.history-refresh-btn');
  const allBtns = [toolbarBtn, ...cardBtns].filter(Boolean);

  allBtns.forEach(b => {
    b.classList.remove('refresh-error');
    if (b.dataset.originalTitle) { b.title = b.dataset.originalTitle; delete b.dataset.originalTitle; }
    b.classList.add('refreshing');
  });

  try {
    fetch('/api/history/record', { method: 'POST' }).catch(e => console.warn('history record failed:', e));
    // One fetch per distinct (range, side field): a side-field card cannot share
    // the main result, which leaves its field out.
    const groups = new Map();
    for (const [metric, entry] of historyCards) {
      const hours = parseFloat(entry.card.querySelector('.history-range-input')?.value) || defaultHistoryHours;
      const field = HISTORY_METRICS[metric].sideField || null;
      const key = `${field || ''}|${hours}`;
      if (!groups.has(key)) groups.set(key, { hours, field, cards: [] });
      groups.get(key).cards.push({ metric, entry, loadSeq: ++entry.loadSeq });
    }
    await Promise.all([...groups.values()].map(async ({ hours, field, cards }) => {
      // Top the cached window up from the server (only records newer than it
      // holds), then redraw every card in the group from the one shared result.
      const data = await fetchHistoryData(hours, { force: true, field });
      // A reopened card or a newer load/Refresh owns its own result. Also skip
      // ranges edited while their debounced load is still pending.
      const current = cards.filter(({ metric, entry, loadSeq }) =>
        historyCards.get(metric) === entry && entry.loadSeq === loadSeq &&
        (parseFloat(entry.card.querySelector('.history-range-input')?.value) || defaultHistoryHours) === hours);
      await Promise.all(current.map(({ metric }) => loadHistoryData(metric, hours, data)));
    }));
  } catch (err) {
    console.error('History refresh failed:', err);
    const msg = `Refresh failed: ${err.message || err}`;
    allBtns.forEach(b => { b.classList.add('refresh-error'); b.dataset.originalTitle = b.title; b.title = msg; });
  } finally {
    allBtns.forEach(b => b.classList.remove('refreshing'));
  }
}
document.getElementById('refresh-history-btn')?.addEventListener('click', refreshAllHistory);

// History Default Time Range Picker (clock button)
document.getElementById('history-default-time-btn')?.addEventListener('click', (e) => {
  createDropdownPicker(e.currentTarget, {
    options: HISTORY_TIME_RANGES.map(r => ({ value: r.value, label: r.label })),
    currentValue: defaultHistoryHours,
    onSelect: (val) => {
      defaultHistoryHours = val;
      localStorage.setItem('defaultHistoryHours', val);
      // And every open card follows (the shared fetch cache dedupes the request).
      for (const [metric, entry] of historyCards) {
        const input = entry.card.querySelector('.history-range-input');
        if (input) input.value = val;
        loadHistoryData(metric, val);
      }
    },
    custom: { type: 'number', min: 0.1, max: 8760, placeholder: 'hours' },
  });
});

// ============================================================================
// History Settings Menu (middle-click on "History" label)
// ============================================================================

let historySettingsPopup = null;

function createHistorySettingsPopup() {
  if (historySettingsPopup) {
    closeHistorySettingsPopup();
    return;
  }

  const popup = document.createElement('div');
  popup.className = 'history-settings-popup';
  popup.innerHTML = `
    <div class="history-settings-header">History Settings</div>
    <div class="history-settings-row">
      <label class="history-settings-label">Record every (s)</label>
      <input type="number" class="history-settings-input" id="hs-interval"
             min="10" max="86400" placeholder="600">
    </div>
    <div class="history-settings-row">
      <label class="history-settings-label">Sample duration (s)</label>
      <input type="number" class="history-settings-input" id="hs-duration"
             min="1" max="300" placeholder="10">
    </div>
    <div class="history-settings-row">
      <label class="history-settings-label">Min between rec. (s)</label>
      <input type="number" class="history-settings-input" id="hs-min-interval"
             min="0" max="3600" placeholder="60">
    </div>
    <div class="history-settings-row">
      <label class="history-settings-label">Ping enabled</label>
      <input type="checkbox" class="history-settings-checkbox" id="hs-ping-enabled" checked>
    </div>
    <div class="history-settings-row">
      <label class="history-settings-label" title="Record the top N processes by CPU and by memory (plus any using a GPU) in each history record">Log top processes</label>
      <input type="checkbox" class="history-settings-checkbox" id="hs-processes-enabled">
    </div>
    <div class="history-settings-row">
      <label class="history-settings-label">Top N processes</label>
      <input type="number" class="history-settings-input" id="hs-process-count"
             min="1" max="50" placeholder="10">
    </div>
    <div class="history-settings-row">
      <label class="history-settings-label">Password</label>
      <input type="password" class="history-settings-input" id="hs-password"
             placeholder="Required">
    </div>
    <div class="history-settings-buttons">
      <button class="history-settings-btn history-settings-btn--reset" id="hs-reset">
        Reset to Default
      </button>
      <button class="history-settings-btn history-settings-btn--confirm" id="hs-confirm">
        Confirm
      </button>
    </div>
    <div class="history-settings-status" id="hs-status"></div>
  `;

  document.body.appendChild(popup);
  historySettingsPopup = popup;

  // Position below the History label
  const label = document.querySelector('.history-controls-label');
  const rect = label.getBoundingClientRect();
  popup.style.top = (rect.bottom + 6) + 'px';
  popup.style.left = rect.left + 'px';

  // Defaults in the HTML are not the server's settings. Keep the form disabled
  // until this popup's read succeeds so an early Confirm cannot overwrite them.
  setHistorySettingsDisabled(popup, true);
  popup.querySelector('#hs-status').textContent = 'Loading settings...';
  loadHistorySettings(popup);

  // Wire up buttons
  popup.querySelector('#hs-confirm').addEventListener('click', () => submitHistorySettings(popup));
  popup.querySelector('#hs-reset').addEventListener('click', () => resetHistorySettings(popup));

  // Close on outside click (delayed to avoid immediate trigger)
  setTimeout(() => {
    if (historySettingsPopup === popup) document.addEventListener('mousedown', historySettingsOutsideClick);
  }, 0);
}

function closeHistorySettingsPopup(popup = historySettingsPopup) {
  if (popup && historySettingsPopup === popup) {
    popup.remove();
    historySettingsPopup = null;
    document.removeEventListener('mousedown', historySettingsOutsideClick);
  }
}

function historySettingsOutsideClick(e) {
  if (historySettingsPopup && !historySettingsPopup.contains(e.target)) {
    closeHistorySettingsPopup();
  }
}

function setHistorySettingsDisabled(popup, disabled) {
  popup.querySelectorAll('input, button').forEach(el => { el.disabled = disabled; });
}

function fillHistorySettings(popup, data) {
  popup.querySelector('#hs-interval').value = data.record_interval;
  popup.querySelector('#hs-duration').value = data.sample_duration;
  popup.querySelector('#hs-min-interval').value = data.min_record_interval;
  popup.querySelector('#hs-ping-enabled').checked = data.ping_enabled;
  popup.querySelector('#hs-processes-enabled').checked = !!data.processes_enabled;
  popup.querySelector('#hs-process-count').value = data.process_count ?? '';
}

async function loadHistorySettings(popup = historySettingsPopup) {
  if (!popup) return;
  const { data, ok } = await safeFetch('/api/history/settings', {});
  if (historySettingsPopup !== popup) return;
  const status = popup.querySelector('#hs-status');
  if (!ok) {
    status.textContent = 'Could not load settings. Close and reopen to retry.';
    status.className = 'history-settings-status history-settings-status--error';
    return;
  }
  fillHistorySettings(popup, data);
  popup.settingsDefaults = data.defaults;
  setHistorySettingsDisabled(popup, false);
  status.textContent = '';

  if (!data.password_configured) {
    status.textContent = 'No password configured on server';
    status.className = 'history-settings-status history-settings-status--error';
  }
}

async function submitHistorySettings(popup = historySettingsPopup) {
  if (!popup || historySettingsPopup !== popup || popup.querySelector('#hs-confirm').disabled) return;
  const interval = popup.querySelector('#hs-interval').value;
  const duration = popup.querySelector('#hs-duration').value;
  const minInterval = popup.querySelector('#hs-min-interval').value;
  const password = popup.querySelector('#hs-password').value;
  const status = popup.querySelector('#hs-status');

  if (!password) {
    status.textContent = 'Password is required';
    status.className = 'history-settings-status history-settings-status--error';
    return;
  }

  const body = { password };
  if (interval) body.record_interval = parseInt(interval);
  if (duration) body.sample_duration = parseInt(duration);
  if (minInterval !== '') body.min_record_interval = parseInt(minInterval);
  body.ping_enabled = popup.querySelector('#hs-ping-enabled').checked;
  body.processes_enabled = popup.querySelector('#hs-processes-enabled').checked;
  const processCount = popup.querySelector('#hs-process-count').value;
  if (processCount) body.process_count = parseInt(processCount);

  setHistorySettingsDisabled(popup, true);
  status.textContent = 'Saving settings...';
  status.className = 'history-settings-status history-settings-status--info';
  let saved = false;
  try {
    const resp = await fetch('/api/history/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (historySettingsPopup !== popup) return;

    if (resp.ok) {
      saved = true;
      status.textContent = 'Settings updated';
      status.className = 'history-settings-status history-settings-status--success';
      popup.querySelector('#hs-password').value = '';
      fillHistorySettings(popup, data);
      setTimeout(() => closeHistorySettingsPopup(popup), 1200);
    } else {
      const msg = data.details ? data.details.join(', ') : data.error;
      status.textContent = msg;
      status.className = 'history-settings-status history-settings-status--error';
    }
  } catch (e) {
    if (historySettingsPopup !== popup) return;
    status.textContent = 'Network error';
    status.className = 'history-settings-status history-settings-status--error';
  } finally {
    if (historySettingsPopup === popup && !saved) setHistorySettingsDisabled(popup, false);
  }
}

function resetHistorySettings(popup = historySettingsPopup) {
  if (!popup || historySettingsPopup !== popup || popup.querySelector('#hs-confirm').disabled || !popup.settingsDefaults) return;
  // The initial GET includes defaults, so Reset needs no second request that
  // could finish after the user edits or closes this form.
  fillHistorySettings(popup, popup.settingsDefaults);
  const status = popup.querySelector('#hs-status');
  status.textContent = 'Defaults loaded \u2014 enter password and confirm';
  status.className = 'history-settings-status history-settings-status--info';
}

// Middle-click on History label opens settings popup
document.querySelector('.history-controls-label')?.addEventListener('auxclick', (e) => {
  if (e.button === 1) {
    e.preventDefault();
    createHistorySettingsPopup();
  }
});

// Suppress browser auto-scroll on middle-click
document.querySelector('.history-controls-label')?.addEventListener('mousedown', (e) => {
  if (e.button === 1) {
    e.preventDefault();
  }
});
