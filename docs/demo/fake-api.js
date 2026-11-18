// Fake Horatio backend for the static demo (docs/README.md). Loaded before the
// real frontend, it replaces fetch() and answers every /api/* and /config.json
// request with synthetic data for an invented homelab box. Values come from
// deterministic smooth noise over wall-clock time, so live polls drift and
// /api/history serves the same series the live view would have recorded.
(function () {
  'use strict';

  // ---- smooth deterministic noise ----------------------------------------
  // Integer hash -> [0, 1).
  function hash(seed, i) {
    let h = (seed ^ Math.imul(i | 0, 0x9e3779b1)) >>> 0;
    h = Math.imul(h ^ (h >>> 16), 0x85ebca6b) >>> 0;
    h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35) >>> 0;
    return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
  }
  const seeds = new Map();
  function seedOf(key) {
    let s = seeds.get(key);
    if (s === undefined) {
      s = 2166136261;
      for (let i = 0; i < key.length; i++) s = Math.imul(s ^ key.charCodeAt(i), 16777619) >>> 0;
      seeds.set(key, s);
    }
    return s;
  }
  // Value noise in [0, 1]: one random value per `period` seconds, smoothstepped between.
  function noise(key, t, period) {
    const x = t / period, i = Math.floor(x), f = x - i, s = seedOf(key);
    const a = hash(s, i), b = hash(s, i + 1), u = f * f * (3 - 2 * f);
    return a + (b - a) * u;
  }
  // Slow drift plus fast jitter, in [0, 1].
  const wander = (key, t, slow = 900, fast = 12) => 0.75 * noise(key + ':s', t, slow) + 0.25 * noise(key + ':f', t, fast);
  // Daily load cycle in [0, 1], busiest mid-afternoon (local time).
  function diurnal(t) {
    const d = new Date(t * 1000), hour = d.getHours() + d.getMinutes() / 60;
    return 0.5 - 0.5 * Math.cos(2 * Math.PI * (hour - 3) / 24);
  }
  // The most recent local 02:40 at or before t: when the nightly backup finished.
  function lastBackupEnd(t) {
    const d = new Date(t * 1000);
    d.setHours(2, 40, 0, 0);
    if (d.getTime() / 1000 > t) d.setDate(d.getDate() - 1);
    return d.getTime() / 1000;
  }
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const r1 = v => Math.round(v * 10) / 10;
  const r2 = v => Math.round(v * 100) / 100;
  const GiB = 1024 ** 3;

  // ---- the fake host ------------------------------------------------------
  const THREADS = 16;
  const MEM_TOTAL = 64 * GiB, SWAP_TOTAL = 8 * GiB;
  const GPUS = [
    { name: 'NVIDIA GeForce RTX 3090', memMiB: 24576, limitW: 350, idleW: 22, maxOp: 90, slowdown: 98 },
    { name: 'NVIDIA GeForce RTX 3060', memMiB: 12288, limitW: 170, idleW: 12, maxOp: 93, slowdown: 100 },
  ];
  const PING_TARGETS = ['1.1.1.1', '192.168.1.1'];
  // Filesystems Glances reports, and the block devices behind them.
  const FILESYSTEMS = [
    { device: 'nvme0n1p2', mnt: '/', fs: 'ext4', size: 1.8e12, used: 0.71e12, inodes: 120e6 },
    { device: 'md0', mnt: '/mnt/storage', fs: 'ext4', size: 7.9e12, used: 5.6e12, inodes: 240e6 },
    { device: 'sdc1', mnt: '/mnt/backup', fs: 'ext4', size: 3.9e12, used: 1.45e12, inodes: 240e6 },
  ];
  // diskio rows include the md members, which the frontend must not double count.
  const DISKIO = ['nvme0n1', 'nvme0n1p1', 'nvme0n1p2', 'sda', 'sdb', 'md0', 'sdc', 'sdc1'];
  const BOOT = Math.floor(Date.now() / 1000) - (23 * 86400 + 4 * 3600 + 17 * 60);
  const PROCS = [
    { pid: 48211, name: 'python3', path: '/home/alice/venvs/ml/bin/python3 train.py --config runs/vit-b16.yaml', user: 'alice', cpu: 6.1, rss: 9.2, thr: 38, gpu: [0, 18800], gpuWork: true },
    { pid: 1873, name: 'jellyfin', path: '/usr/lib/jellyfin/bin/jellyfin', user: 'jellyfin', cpu: 1.4, rss: 1.1, thr: 42, gpu: [1, 640] },
    { pid: 2290, name: 'ollama', path: '/usr/local/bin/ollama serve', user: 'ollama', cpu: 0.6, rss: 2.3, thr: 24, gpu: [1, 5300] },
    { pid: 1022, name: 'postgres', path: '/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main', user: 'postgres', cpu: 0.9, rss: 0.6, thr: 1 },
    { pid: 1554, name: 'syncthing', path: '/usr/bin/syncthing serve --no-browser', user: 'alice', cpu: 0.7, rss: 0.25, thr: 21 },
    { pid: 988, name: 'dockerd', path: '/usr/bin/dockerd -H fd://', user: 'root', cpu: 0.3, rss: 0.12, thr: 30 },
    { pid: 1201, name: 'glances', path: '/opt/horatio/.venv/bin/python -m glances -w --disable-webui', user: 'horatio', cpu: 0.8, rss: 0.09, thr: 6 },
    { pid: 1199, name: 'uvicorn', path: '/opt/horatio/.venv/bin/python server.py', user: 'horatio', cpu: 0.4, rss: 0.07, thr: 5 },
    { pid: 1460, name: 'smbd', path: '/usr/sbin/smbd --foreground --no-process-group', user: 'root', cpu: 0.1, rss: 0.03, thr: 1 },
    { pid: 905, name: 'tailscaled', path: '/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state', user: 'root', cpu: 0.2, rss: 0.05, thr: 14 },
    { pid: 1302, name: 'nginx', path: 'nginx: worker process', user: 'www-data', cpu: 0.05, rss: 0.01, thr: 1 },
    { pid: 3317, name: 'restic', path: '/usr/bin/restic backup /mnt/storage --exclude-caches', user: 'root', cpu: 0.0, rss: 0.0, thr: 9, backup: true },
  ];

  // Everything that moves, at time t (epoch seconds). Live polls and history share it.
  // A history record asks for its own t several times (snapshots, processes): keep the last.
  let lastT = null, lastState = null;
  function state(t) {
    if (t !== lastT) { lastState = computeState(t); lastT = t; }
    return lastState;
  }
  function computeState(t) {
    const d = diurnal(t);
    // The training job runs in multi-hour stretches; the backup nightly 02:00-02:40.
    const training = noise('job', t, 5400) > 0.3;
    const hour = new Date(t * 1000).getHours() + new Date(t * 1000).getMinutes() / 60;
    const backup = hour >= 2 && hour < 2.67;

    const cores = [];
    for (let i = 0; i < THREADS; i++) {
      let v = 2 + 30 * d * wander('core' + i, t, 600, 6) + 6 * noise('spike' + i, t, 3) ** 4 * 10;
      if (training && i < 3) v += 55 + 25 * noise('train' + i, t, 8);   // the data loader pins a few cores
      if (backup && i === 5) v += 60;
      cores.push(r1(clamp(v, 0.3, 100)));
    }
    const cpu = r1(cores.reduce((s, v) => s + v, 0) / THREADS);

    const memUsed = MEM_TOTAL * (0.22 + 0.12 * d + 0.05 * wander('mem', t, 1800, 60) + (training ? 0.16 : 0));
    const swapUsed = SWAP_TOTAL * (0.04 + 0.03 * noise('swap', t, 7200));

    const gpus = GPUS.map((g, i) => {
      const busy = i === 0 ? training : noise('gpu1busy', t, 1200) > 0.55;
      const util = busy ? 78 + 20 * wander('gpuu' + i, t, 300, 4) : 2 * wander('gpuidle' + i, t, 60, 5);
      const memMiB = i === 0 ? (training ? 19200 : 420) : 6100 + (busy ? 900 : 0);
      const temp = 34 + (busy ? 36 : 4) * (0.8 + 0.2 * wander('gput' + i, t, 600, 30));
      const power = g.idleW + (busy ? (g.limitW - g.idleW) * (0.75 + 0.2 * wander('gpup' + i, t, 120, 4)) : 3 * noise('gpupi' + i, t, 20));
      return { util: r1(util), memMiB: Math.round(memMiB), memPct: r1(memMiB / g.memMiB * 100), temp: Math.round(temp), power: r1(power) };
    });

    // MB/s, bursty: a cube of noise is mostly low with occasional peaks.
    const netIn = 0.05 + 6 * d * noise('netin', t, 40) ** 3 + 0.4 * noise('netinf', t, 5) + (backup ? 0.5 : 0);
    const netOut = 0.03 + 2.5 * noise('netout', t, 60) ** 4 + 0.2 * noise('netoutf', t, 5) + (backup ? 38 : 0);
    const disk = {
      'nvme0n1p2': [0.2 + 30 * noise('nvr', t, 30) ** 5 + (training ? 45 * wander('nvtr', t, 120, 3) : 0), 0.5 + 6 * noise('nvw', t, 20) ** 3],
      'md0': [0.1 + 8 * noise('mdr', t, 50) ** 4 + (backup ? 160 : 0), 0.2 + 4 * noise('mdw', t, 80) ** 4],
      'sdc1': [0.05 * noise('bkr', t, 30), 0.02 + (backup ? 120 * (0.8 + 0.2 * noise('bkw', t, 6)) : 0)],
    };

    const pkg = 36 + 0.45 * cpu + 3 * wander('pkg', t, 300, 10);
    const coreTemps = [];
    for (let i = 0; i < THREADS / 2; i++) {
      coreTemps.push(Math.round(pkg - 4 + (cores[i] + cores[i + THREADS / 2]) / 2 * 0.15 + 3 * noise('ct' + i, t, 20)));
    }
    const fans = {
      'FAN1': Math.round((900 + 14 * pkg + 200 * noise('fan1', t, 40)) / 75) * 75,
      'FAN2': Math.round((850 + 12 * pkg + 200 * noise('fan2', t, 40)) / 75) * 75,
      'FAN3': Math.round((1100 + 10 * Math.max(...gpus.map(g => g.temp)) + 150 * noise('fan3', t, 40)) / 75) * 75,
    };
    const cpuW = 28 + 1.6 * cpu * (0.9 + 0.2 * noise('cpuw', t, 10));
    const dramW = 7 + 0.04 * cpu + 2 * noise('dramw', t, 30);
    const totalW = cpuW + dramW + gpus.reduce((s, g) => s + g.power, 0) + 68 + 6 * noise('otherw', t, 60);

    const runnable = cores.reduce((s, v) => s + v, 0) / 100;
    const load = {
      min1: r2(runnable * (1 + 0.15 * noise('l1', t, 30))),
      min5: r2(runnable * (0.95 + 0.1 * noise('l5', t, 300))),
      min15: r2(runnable * (0.9 + 0.1 * noise('l15', t, 900))),
    };

    // Rare, short packet loss on the WAN target.
    const lossy = noise('loss', t, 600) > 0.93;
    const ping = {
      '1.1.1.1': { ms: 11 + 4 * wander('pwan', t, 600, 4) + (lossy ? 25 : 0), loss: lossy ? 0.2 : 0 },
      '192.168.1.1': { ms: 0.3 + 0.25 * wander('plan', t, 600, 4), loss: 0 },
    };
    return { t, d, training, backup, cores, cpu, memUsed, swapUsed, gpus, netIn, netOut, disk, pkg, coreTemps, fans, cpuW, dramW, totalW, load, ping };
  }

  // ---- live endpoints -----------------------------------------------------
  const now = () => Date.now() / 1000;
  const iso = t => new Date(t * 1000).toISOString();

  function fsRows(t) {
    return FILESYSTEMS.map((f, i) => {
      // /mnt/storage grows slowly (with a daily ripple of downloads); the others barely move.
      const used = f.used * (1 + (i === 1 ? 0.002 * noise('grow', t, 86400) : 0.0005 * noise('grow' + i, t, 3600)));
      const inodesUsed = Math.round(f.inodes * (0.03 + 0.01 * i));
      return {
        device_name: '/dev/' + f.device, fs_type: f.fs, mnt_point: f.mnt,
        size: f.size, used, free: f.size - used, percent: r1(used / f.size * 100),
        inodes_used: inodesUsed, inodes_percent: r1(inodesUsed / f.inodes * 100), key: 'mnt_point',
      };
    });
  }

  function diskioRows(s) {
    const MB = 1e6;
    const rate = name => {
      if (s.disk[name]) return s.disk[name];
      if (name === 'nvme0n1') return s.disk['nvme0n1p2'];
      if (name === 'sdc') return s.disk['sdc1'];
      if (name === 'sda' || name === 'sdb') return [s.disk.md0[0] / 2, s.disk.md0[1]];   // RAID1: reads split, writes mirrored
      return [0, 0];
    };
    return DISKIO.map(name => {
      const [r, w] = rate(name);
      return { disk_name: name, read_bytes_rate_per_sec: Math.round(r * MB), write_bytes_rate_per_sec: Math.round(w * MB), key: 'disk_name' };
    });
  }

  function storageHealth(t) {
    const s = state(t);
    return {
      status: 'ok', age_sec: Math.round(t % 900), detail: null,
      alarms: [],
      info: ['sdc: last extended self-test 9 days ago'],
      drives: [
        { device: 'nvme0n1', kind: 'SSD', model: 'Samsung SSD 980 PRO 2TB', size_bytes: 2.0e12, serial: 'S6B0NX0T000000', passed: true,
          temperature_c: Math.round(38 + 0.1 * s.cpu + 2 * noise('nvt', t, 600)), holds: ['nvme0n1p1 -> /boot/efi', 'nvme0n1p2 -> /'] },
        { device: 'sda', kind: 'HDD', model: 'WDC WD80EFZZ-68BTXN0', size_bytes: 8.0e12, serial: 'WD-CA0000AA', passed: true,
          temperature_c: Math.round(36 + 2 * noise('sdat', t, 1800)), holds: ['md0 -> /mnt/storage'] },
        { device: 'sdb', kind: 'HDD', model: 'WDC WD80EFZZ-68BTXN0', size_bytes: 8.0e12, serial: 'WD-CA0000AB', passed: true,
          temperature_c: Math.round(37 + 2 * noise('sdbt', t, 1800)), holds: ['md0 -> /mnt/storage'] },
        // The warm one: sits in the drive cage behind the GPUs.
        { device: 'sdc', kind: 'HDD', model: 'ST4000VN006-3CW104', size_bytes: 4.0e12, serial: 'ZW600000', passed: true,
          temperature_c: Math.round(49 + 3 * noise('sdct', t, 1800)), holds: ['sdc1 -> /mnt/backup'] },
      ],
      md: [{ name: 'md0', level: 'raid1', array_state: 'clean', degraded: 0, sync_action: 'idle',
        members: [{ name: 'sda', state: ['in_sync'], errors: 0 }, { name: 'sdb', state: ['in_sync'], errors: 0 }] }],
      edac: { uptime_seconds: Math.round(t - BOOT), ce_total: 0, ue_total: 0 },
      backup: { timer: 'restic-backup.timer', age_hours: (t - lastBackupEnd(t)) / 3600 },
    };
  }

  function sensorsSnapshot(t) {
    const s = state(t);
    return {
      status: 'ok', age_sec: Math.round(t % 60), detail: null, alarms: [],
      temps: [
        { label: 'Inlet Temp', value: Math.round(23 + 2 * noise('inlet', t, 3600)) },
        { label: 'Exhaust Temp', value: Math.round(30 + 0.08 * s.totalW) },
      ],
      fans: Object.entries(s.fans).map(([label, value]) => ({ label, value })),
      power: [
        { label: 'Pwr Consumption', value: Math.round(s.totalW), reading: `${Math.round(s.totalW)} Watts`, entity: 'bmc' },
        { label: 'CPU 0', value: r1(s.cpuW), reading: `${r1(s.cpuW)} Watts`, entity: 'rapl' },
        { label: 'DRAM 0', value: r1(s.dramW), reading: `${r1(s.dramW)} Watts`, entity: 'rapl' },
        { label: 'PSU1 Current', value: r1(s.totalW / 230), reading: `${r1(s.totalW / 230)} Amps`, entity: 'bmc' },
      ],
      gpus: GPUS.map((g, i) => ({ index: i, mem_c: null, limits: { max_op: g.maxOp, slowdown: g.slowdown, mem_max_op: null } })),
    };
  }

  function processList(t) {
    const s = state(t);
    return PROCS.map(p => {
      let cpu = p.cpu * (0.6 + 0.8 * noise('pc' + p.pid, t, 20));
      if (p.gpuWork) cpu = s.training ? 4 + 3 * noise('pct', t, 10) : 0.1;
      if (p.backup) cpu = s.backup ? 5 + noise('pcb', t, 5) : 0;
      const rss = p.rss * GiB * (p.gpuWork && !s.training ? 0.1 : 1);
      if (p.backup && !s.backup) return null;
      return {
        pid: p.pid, name: p.name, path: p.path, username: p.user, status: cpu > 3 ? 'R' : 'S',
        cpu_percent: r1(cpu), memory_percent: r2(rss / MEM_TOTAL * 100), memory_info: { rss: Math.round(rss), vms: Math.round(rss * 3) },
        num_threads: p.thr, cpu_times: { user: Math.round((t - BOOT) * p.cpu / 100 * THREADS * 0.7), system: Math.round((t - BOOT) * p.cpu / 100 * THREADS * 0.2) },
      };
    }).filter(Boolean);
  }

  function gpuProcesses(t) {
    const s = state(t);
    return PROCS.filter(p => p.gpu && (!p.gpuWork || s.training)).map(p => {
      const [idx, mib] = p.gpu, g = s.gpus[idx];
      const util = p.gpuWork ? g.util : g.util > 10 ? r1(g.util * (p.name === 'ollama' ? 0.8 : 0.2)) : null;
      return { pid: p.pid, name: p.name, process_name: p.path.split(' ')[0], path: p.path, gpu_index: idx, gpu_memory_mib: mib,
        gpu_util_percent: util, gpu_mem_util_percent: util == null ? null : Math.round(util * 0.6) };
    });
  }

  function networkProcesses(t) {
    const s = state(t);
    const rows = [
      { pid: 1554, name: 'syncthing', sent_kbps: 40 * noise('nsy', t, 30), recv_kbps: 900 * noise('nsyr', t, 30) ** 2 },
      { pid: 1873, name: 'jellyfin', sent_kbps: 2400 * noise('njf', t, 120) ** 2, recv_kbps: 12 * noise('njfr', t, 30) },
      { pid: 905, name: 'tailscaled', sent_kbps: 6 + 20 * noise('nts', t, 30), recv_kbps: 4 + 15 * noise('ntsr', t, 30) },
      { pid: 1302, name: 'nginx', sent_kbps: 30 * noise('nng', t, 30), recv_kbps: 3 * noise('nngr', t, 30) },
    ];
    if (s.backup) rows.push({ pid: 3317, name: 'restic', sent_kbps: s.netOut * 1000, recv_kbps: 20 });
    return rows.map(r => ({ ...r, sent_kbps: r1(r.sent_kbps), recv_kbps: r1(r.recv_kbps) }));
  }

  function diskProcesses(t) {
    const s = state(t);
    const rows = [
      { pid: 1022, name: 'postgres', read_kbps: 80 * noise('dpg', t, 30), write_kbps: 300 * noise('dpgw', t, 30) },
      { pid: 1554, name: 'syncthing', read_kbps: 600 * noise('dsy', t, 30) ** 3, write_kbps: 200 * noise('dsyw', t, 30) ** 2 },
      { pid: 1201, name: 'glances', read_kbps: 0, write_kbps: 4 * noise('dgl', t, 30) },
    ];
    if (s.training) rows.push({ pid: 48211, name: 'python3', read_kbps: s.disk['nvme0n1p2'][0] * 1000, write_kbps: 50 });
    if (s.backup) rows.push({ pid: 3317, name: 'restic', read_kbps: s.disk.md0[0] * 1000, write_kbps: s.disk.sdc1[1] * 1000 });
    return rows.map(r => ({ ...r, read_kbps: r1(r.read_kbps), write_kbps: r1(r.write_kbps) }));
  }

  function uptimeText(t) {
    const sec = Math.floor(t - BOOT), days = Math.floor(sec / 86400), rest = sec % 86400;
    const hh = Math.floor(rest / 3600), mm = String(Math.floor(rest % 3600 / 60)).padStart(2, '0'), ss = String(rest % 60).padStart(2, '0');
    return `${days} days, ${hh}:${mm}:${ss}`;
  }

  // ---- history ------------------------------------------------------------
  const RECORD_INTERVAL = 600, SAMPLE_COUNT = 5, SAMPLE_DURATION = 10, HISTORY_DAYS = 30;
  const stats = v => ({ avg: r2(v.reduce((a, b) => a + b, 0) / v.length), min: r2(Math.min(...v)), max: r2(Math.max(...v)) });
  const median = v => { const s = [...v].sort((a, b) => a - b), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

  // One record as server.py's build_history_record writes it, from a burst of samples ending at t.
  function historyRecord(t) {
    const burst = [];
    for (let i = SAMPLE_COUNT - 1; i >= 0; i--) burst.push(state(t - i * SAMPLE_DURATION / SAMPLE_COUNT));
    const s = burst[burst.length - 1];
    const coreAvgs = s.cores.map((_, i) => burst.reduce((a, b) => a + b.cores[i], 0) / burst.length);
    const freqs = burst.flatMap(b => b.cores.map(c => 1400 + 32 * c));
    const fs = fsRows(t);
    const temps = {};
    s.coreTemps.forEach((v, i) => { temps['Core ' + i] = v; });
    temps['Package id 0'] = Math.round(s.pkg);
    s.gpus.forEach((g, i) => { temps['GPU ' + i] = g.temp; });
    sensorsSnapshot(t).temps.forEach(r => { temps[r.label] = r.value; });
    storageHealth(t).drives.forEach(d => { temps[d.device] = d.temperature_c; });
    const ping = {};
    for (const addr of PING_TARGETS) {
      const v = burst.map(b => b.ping[addr].ms), loss = s.ping[addr].loss;
      ping[addr] = { ...stats(v), loss };
    }
    const mountOf = name => FILESYSTEMS.find(f => f.device === name).mnt;
    return {
      timestamp: iso(t),
      duration_sec: SAMPLE_DURATION,
      cpu: stats(burst.map(b => b.cpu)),
      cpu_cores: { min: r2(Math.min(...coreAvgs)), median: r2(median(coreAvgs)), max: r2(Math.max(...coreAvgs)) },
      cpu_freq: { min: r1(Math.min(...freqs)), median: r1(median(freqs)), avg: r1(freqs.reduce((a, b) => a + b, 0) / freqs.length), max: r1(Math.max(...freqs)), min_mhz: 800, max_mhz: 4900 },
      memory: { used_gb: r2(s.memUsed / GiB), total_gb: r2(MEM_TOTAL / GiB), percent: r1(s.memUsed / MEM_TOTAL * 100) },
      swap: { used_gb: r2(s.swapUsed / GiB), total_gb: r2(SWAP_TOTAL / GiB), percent: r1(s.swapUsed / SWAP_TOTAL * 100) },
      network: { in_mbps: stats(burst.map(b => b.netIn)), out_mbps: stats(burst.map(b => b.netOut)) },
      disk_io: {
        read_mbps: stats(burst.map(b => Object.values(b.disk).reduce((a, d) => a + d[0], 0))),
        write_mbps: stats(burst.map(b => Object.values(b.disk).reduce((a, d) => a + d[1], 0))),
        drives: Object.keys(s.disk).sort().map(name => ({
          name: mountOf(name),
          read_mbps: stats(burst.map(b => b.disk[name][0])),
          write_mbps: stats(burst.map(b => b.disk[name][1])),
        })),
      },
      rate_caps: { network_mbps: 1250, disk_io_mbps: 15000 },
      drives: fs.map(f => ({ mount: f.mnt_point, used_gb: r2(f.used / GiB), total_gb: r2(f.size / GiB), percent: f.percent, inodes_percent: f.inodes_percent, inodes_used: f.inodes_used })),
      gpus: s.gpus.map((g, i) => ({ index: i, util_percent: g.util, mem_used_gb: r2(g.memMiB / 1024), mem_total_gb: r2(GPUS[i].memMiB / 1024), mem_percent: g.memPct, throttle_sw_thermal_s: 0, throttle_hw_thermal_s: 0 })),
      fans: s.fans,
      temps,
      power: { 'Pwr Consumption': Math.round(s.totalW), 'CPU 0': r1(s.cpuW), 'DRAM 0': r1(s.dramW), ...Object.fromEntries(s.gpus.map((g, i) => ['GPU ' + i, g.power])) },
      load: s.load,
      processes: historyProcesses(t),
      ping,
    };
  }

  function historyProcesses(t) {
    const gpuByPid = new Map(gpuProcesses(t).map(g => [g.pid, g]));
    const disk = new Map(diskProcesses(t).map(d => [d.pid, d]));
    return processList(t).slice(0, 10).map(p => {
      const g = gpuByPid.get(p.pid), dk = disk.get(p.pid);
      const row = { pid: p.pid, ppid: 1, create_time: BOOT + 60, name: p.name, user: p.username, status: p.status,
        cpu_percent: p.cpu_percent, rss_mb: r1(p.memory_info.rss / 1024 ** 2), threads: p.num_threads };
      if (dk) { row.disk_read_kbps = dk.read_kbps; row.disk_write_kbps = dk.write_kbps; }
      if (g) { row.gpu_mem_mib = g.gpu_memory_mib; row.gpus = [g.gpu_index]; if (g.gpu_util_percent) row.gpu_util_percent = g.gpu_util_percent; }
      return row;
    });
  }

  // Records are built once per grid slot and kept: a 30-day window is ~4300 of them.
  const recordCache = new Map();
  function recordsBetween(from, to) {
    const out = [];
    const first = Math.max(Math.ceil(from / RECORD_INTERVAL), Math.ceil((now() - HISTORY_DAYS * 86400) / RECORD_INTERVAL));
    for (let slot = first; slot * RECORD_INTERVAL <= to; slot++) {
      let rec = recordCache.get(slot);
      if (!rec) { rec = historyRecord(slot * RECORD_INTERVAL); recordCache.set(slot, rec); }
      if (slot * RECORD_INTERVAL > from) out.push(rec);
    }
    return out;
  }

  const SIDE_FIELDS = ['processes', 'temps'];
  function history(query, field) {
    const t = now();
    // Clamped like the server's history.max_hours (CONFIG.history below).
    const hours = clamp(Number(query.get('hours')) || 24, 0.1, 24 * HISTORY_DAYS);
    const cutoff = t - hours * 3600;
    const since = query.get('since') ? Date.parse(query.get('since')) / 1000 : null;
    const records = recordsBetween(since != null && since > cutoff ? since : cutoff, t);
    const body = { count: records.length, skipped_lines: 0, truncated: false, cutoff, now: t };
    if (field) {
      body.field = field;
      body.records = records.map(r => ({ timestamp: r.timestamp, [field]: r[field] }));
    } else {
      body.records = records.map(r => {
        const slim = { ...r };
        SIDE_FIELDS.forEach(k => delete slim[k]);
        return slim;
      });
      const last = Math.floor(t / RECORD_INTERVAL) * RECORD_INTERVAL;
      body.recording = { last_success: iso(last), last_error: null, interval_sec: RECORD_INTERVAL, stale: false };
    }
    return body;
  }

  const HISTORY_SETTINGS = {
    record_interval: RECORD_INTERVAL, sample_duration: SAMPLE_DURATION, min_record_interval: 60,
    ping_enabled: true, processes_enabled: true, process_count: 10,
  };

  // ---- service checks (http_targets) ---------------------------------------
  const SERVICES = [
    { name: 'photos', url: 'https://photos.demo.invalid/api/server/ping' },
    { name: 'files', url: 'https://files.demo.invalid/' },
    { name: 'docs site', url: 'http://docs.demo.invalid:8000/' },
  ];
  const SERVICES_INTERVAL = 60;
  // Whether service i passed the check at epoch second t: photos is down for 40
  // minutes at 03:00 UTC every third day, the docs site drops about one check
  // in 500, files never fails.
  function serviceUp(i, t) {
    if (i === 0) return !(Math.floor(t / 86400) % 3 === 0 && t % 86400 >= 10800 && t % 86400 < 13200);
    if (i === 2) return (Math.floor(t / SERVICES_INTERVAL) * 2654435761) % 4294967296 / 4294967296 >= 0.002;
    return true;
  }
  function servicesSnapshot(t) {
    const last = Math.floor(t / SERVICES_INTERVAL) * SERVICES_INTERVAL;
    return { interval: SERVICES_INTERVAL, now: t, targets: SERVICES.map((s, i) => {
      const uptime = {};
      for (const hours of [1, 24, 168]) {
        let up = 0, checks = 0;
        for (let at = last; at > t - hours * 3600; at -= SERVICES_INTERVAL) { checks++; up += serviceUp(i, at); }
        uptime[hours] = { up, checks };
      }
      const ok = serviceUp(i, last);
      return { ...s, ok, status: ok ? 200 : null, ms: ok ? 20 + 15 * i + Math.round(8 * noise('svc' + i, t, 60)) : null,
               error: ok ? null : 'ConnectError', checked: new Date(last * 1000).toISOString(), uptime };
    }) };
  }
  // The server's bucket plan (server.py services_bucket_plan): at most 300 buckets.
  function servicesHistory(q, t) {
    const hours = clamp(Number(q.get('hours')) || 24, 0.1, 24 * HISTORY_DAYS);
    const bucket = Math.max(1, Math.ceil(hours * 3600 / 300 / SERVICES_INTERVAL)) * SERVICES_INTERVAL;
    const count = Math.max(1, Math.ceil(hours * 3600 / bucket));
    const start = (Math.floor(t / bucket) + 1) * bucket - count * bucket;
    const targets = {};
    SERVICES.forEach((s, i) => {
      targets[s.name] = Array.from({ length: count }, (_, b) => {
        const pair = [0, 0];
        for (let at = start + b * bucket; at < start + (b + 1) * bucket && at <= t; at += SERVICES_INTERVAL) pair[serviceUp(i, at) ? 0 : 1]++;
        return pair;
      });
    });
    return { start, bucket_sec: bucket, count, now: t, truncated: false, targets };
  }

  // ---- routing ------------------------------------------------------------
  const CONFIG = {
    title: 'Horatio demo',
    title_emoji: '🔭 Horatio demo',
    ping_targets: PING_TARGETS.map(address => ({ address })),
    http_targets: SERVICES,
    history: { ...HISTORY_SETTINGS, sample_count: SAMPLE_COUNT, max_hours: 24 * HISTORY_DAYS },
    limits: { network_mbps: 1250, disk_io_mbps: 15000 },
    thresholds: {
      cpu_percent: 50, memory_percent: 75, gpu_util_percent: 80, gpu_memory_percent: 80, temp_celsius: 60,
      power_max_watts: 1200, power_psu_watts: 1000, power_warn_fraction: 0.8, network_mbps: 25,
      disk_percent: 80, disk_io_mbps: 200, swap_percent: 10, ping_ms: 100,
    },
  };

  const ENDPOINTS = [
    '/4/cpu', '/4/percpu', '/4/mem', '/4/memswap', '/4/gpu', '/4/network', '/4/diskio', '/4/sensors',
    '/4/load', '/4/uptime', '/4/system', '/4/fs',
    '/cpu-info', '/gpu-info', '/ip-info', '/ping', '/gpu-processes', '/disk-processes', '/network-processes',
    '/processlist', '/history', '/history/settings', '/history/record', '/history/field/temps',
    '/history/field/processes', '/mail', '/storage-health', '/sensors', '/services', '/services/history',
    '/version', '/list-endpoints',
  ];

  // Path (after /api) -> (query, t) => body, served with status 200.
  const GET = {
    '/list-endpoints': () => ({ endpoints: ENDPOINTS, backend: 'python' }),
    '/version': () => ({ version: 'demo', build: null }),
    '/4/cpu': (q, t) => ({ total: state(t).cpu, cpucore: THREADS }),
    '/4/percpu': (q, t) => state(t).cores.map((total, cpu_number) => ({ cpu_number, total, key: 'cpu_number' })),
    '/4/mem': (q, t) => { const u = state(t).memUsed; return { total: MEM_TOTAL, used: u, free: MEM_TOTAL - u, percent: r1(u / MEM_TOTAL * 100) }; },
    '/4/memswap': (q, t) => { const u = state(t).swapUsed; return { total: SWAP_TOTAL, used: u, free: SWAP_TOTAL - u, percent: r1(u / SWAP_TOTAL * 100) }; },
    '/4/gpu': (q, t) => state(t).gpus.map((g, i) => ({ gpu_id: 'nvidia' + i, name: GPUS[i].name, proc: g.util, mem: g.memPct, temperature: g.temp, fan_speed: null, key: 'gpu_id' })),
    '/4/network': (q, t) => {
      const s = state(t);
      return [
        { interface_name: 'lo', bytes_recv_rate_per_sec: 41000, bytes_sent_rate_per_sec: 41000 },
        { interface_name: 'enp5s0', bytes_recv_rate_per_sec: Math.round(s.netIn * 1e6), bytes_sent_rate_per_sec: Math.round(s.netOut * 1e6) },
        { interface_name: 'tailscale0', bytes_recv_rate_per_sec: Math.round(2000 * noise('tsr', t, 10)), bytes_sent_rate_per_sec: Math.round(1500 * noise('tss', t, 10)) },
      ].map(r => ({ ...r, key: 'interface_name' }));
    },
    '/4/diskio': (q, t) => diskioRows(state(t)),
    '/4/sensors': (q, t) => {
      const s = state(t);
      return [
        ...s.coreTemps.map((value, i) => ({ label: 'Core ' + i, type: 'temperature_core', value, warning: 80, critical: 100, unit: 'C' })),
        { label: 'Package id 0', type: 'temperature_core', value: Math.round(s.pkg), warning: 80, critical: 100, unit: 'C' },
        { label: 'Composite', type: 'temperature_core', value: storageHealth(t).drives[0].temperature_c, warning: 82, critical: 85, unit: 'C' },
      ];
    },
    '/4/load': (q, t) => ({ ...state(t).load, cpucore: THREADS }),
    '/4/uptime': (q, t) => uptimeText(t),
    '/4/system': () => ({ hostname: 'demo-box', os_name: 'Linux', linux_distro: 'Ubuntu 24.04.1 LTS', os_version: '6.8.0-48-generic', platform: '64bit', hr_name: 'Ubuntu 24.04.1 LTS 64bit' }),
    '/4/fs': (q, t) => fsRows(t),
    '/cpu-info': () => ({ model: 'AMD Ryzen 7 5800X 8-Core Processor', physical_cores: 8, logical_cores: THREADS, l3_cache: '32 MiB' }),
    '/gpu-info': (q, t) => state(t).gpus.map((g, i) => ({
      index: i, name: GPUS[i].name, utilization: g.util, temperature: g.temp, memory_used: g.memMiB, memory_total: GPUS[i].memMiB,
      power_draw: g.power, power_limit: GPUS[i].limitW, driver_version: '550.120', cuda_version: '12.4',
    })),
    '/ip-info': () => ({
      lan: [{ interface: 'enp5s0', ipv4: '192.168.1.20', ipv6: 'fd12:3456:789a::20' }],
      public: { ipv4: '203.0.113.42' },
      tailscale: { ipv4: '100.101.102.103' },
    }),
    '/ping': (q, t) => {
      const s = state(t), targets = {};
      // A lossy stretch drops about one live poll in five.
      for (const addr of PING_TARGETS) targets[addr] = { latency_ms: s.ping[addr].loss && noise('drop' + addr, t, 2) < 0.2 ? null : r2(s.ping[addr].ms) };
      return { targets };
    },
    '/processlist': (q, t) => processList(t),
    '/gpu-processes': (q, t) => gpuProcesses(t),
    '/network-processes': (q, t) => networkProcesses(t),
    '/disk-processes': (q, t) => diskProcesses(t),
    '/mail': (q, t) => {
      const done = Math.round(lastBackupEnd(t));
      const msgs = [
        { subject: 'Cron <root@demo-box> /usr/local/bin/prune-snapshots', date_epoch: done + 1200 },
        { subject: 'restic-backup: completed, 41.2 GiB added', date_epoch: done },
        { subject: 'smartd: weekly self-test passed on /dev/sda', date_epoch: done - 4 * 86400 + 3600 },
      ].filter(m => m.date_epoch <= t);
      return { status: 'ok', path: '/var/mail/horatio', count: msgs.length, unreadable: 0, messages: msgs };
    },
    '/storage-health': (q, t) => storageHealth(t),
    '/sensors': (q, t) => sensorsSnapshot(t),
    '/services': (q, t) => servicesSnapshot(t),
    '/services/history': (q, t) => servicesHistory(q, t),
    '/history': q => history(q, null),
    '/history/settings': () => ({ ...HISTORY_SETTINGS, password_configured: true, defaults: { ...HISTORY_SETTINGS, processes_enabled: false } }),
  };
  SIDE_FIELDS.forEach(f => { GET['/history/field/' + f] = q => history(q, f); });

  // Path (after /api) -> () => [status, body].
  const POST = {
    '/history/record': () => [200, { status: 'skipped', reason: 'demo' }],
    '/history/settings': () => [403, { error: 'This is a read-only demo: settings cannot be saved.' }],
  };

  // Capture endpoints take as long as the capture would on a real box.
  const DELAY_MS = { '/network-processes': q => 1000 * clamp(Number(q.get('duration')) || 3, 1, 60),
                     '/disk-processes': q => 1000 * clamp(Number(q.get('duration')) || 1, 1, 10) };

  // Glances serves uptime as a JSON string too, so every body is JSON.
  const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

  function wait(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));
      const id = setTimeout(resolve, ms);
      signal?.addEventListener('abort', () => { clearTimeout(id); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
    });
  }

  // Answer a request: [status, body]. Exposed for the test suite.
  function route(method, path, query) {
    if (path === '/config.json') return method === 'GET' ? [200, CONFIG] : [405, { error: 'Method not allowed' }];
    if (!path.startsWith('/api/')) return null;
    const sub = path.slice(4);
    if (method === 'POST') return POST[sub] ? POST[sub]() : [404, { error: 'Not found' }];
    return GET[sub] ? [200, GET[sub](query, now())] : [404, { error: 'Not found' }];
  }

  const realFetch = globalThis.fetch && globalThis.fetch.bind(globalThis);
  globalThis.fetch = async function (input, init = {}) {
    const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(href, globalThis.location?.href || 'http://demo.invalid/');
    const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    const answer = route(method, url.pathname, url.searchParams);
    if (!answer) return realFetch(input, init);
    if (answer[0] === 404) console.warn('demo: no fake for', method, url.pathname);
    const delay = DELAY_MS[url.pathname.slice(4)];
    await wait(delay ? delay(url.searchParams) : 15 + 40 * Math.random(), init.signal);
    return json(answer[0], answer[1]);
  };
  globalThis.HoratioDemo = { route, ENDPOINTS };

  // A small badge so nobody mistakes the numbers for a real machine.
  if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
      const badge = document.createElement('a');
      badge.href = 'https://github.com/mivanit/horatio';
      badge.textContent = 'Demo · synthetic data · GitHub ↗';
      badge.title = 'This page runs the real Horatio frontend against a fake in-browser backend.';
      Object.assign(badge.style, {
        position: 'fixed', right: '12px', bottom: '12px', zIndex: 1000, padding: '6px 10px',
        font: '12px system-ui, sans-serif', textDecoration: 'none', borderRadius: '6px',
        background: 'var(--bg-elevated)', color: 'var(--text-secondary)', border: '1px solid var(--color-accent)',
      });
      document.body.appendChild(badge);
    });
  }
})();
