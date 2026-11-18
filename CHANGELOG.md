# Changelog

Releases are `vX.Y.Z` git tags (README: Versions and updates). Add changes under
`## [Unreleased]`; `make release V=X.Y.Z` turns that section into the release.

## [Unreleased]

- History charts no longer draw a line across a stretch with no records longer than
  `history.max_gap_seconds` (default one hour; `0` restores the old behaviour).
- Service checks: when no round has finished for several intervals, `/api/services`
  says `stale: true` and the card and `/text` show the results as stale, not as the
  last "up". An unexpected error in a check is logged with its traceback.
- Hardware collector: a `backup_timer` that names no existing unit is an alarm at once
  (`collector: backup-timer: no such timer`).
- Dashboard: a failed GPU or ping request shows its card again when the card was hidden,
  so the header never counts a failure you cannot see.
- Dashboard: a slow or failed public-IP lookup no longer holds back the drive, mail,
  hardware, fan and power cards or the process captures.
- Dashboard: the GPU cards hide on a box with no GPU, and turn red with a gap in the
  chart when GPUs that were there stop reporting. The Ping card hides when ping is
  turned off instead of reading as a failure. Long sensor names keep their start and end
  instead of losing their start. The Services card no longer flashes on load.
- Service checks: a target with a required 3xx `status` is no longer followed past it;
  an unexpected error in one check no longer freezes every target; `/text` no longer
  fails now and then while checks run.
- `/text`: a bad or empty `width`, `height`, `rows` or `hours` gets the usage line or
  the default, not a JSON error.
- History settings: values that are not numbers or booleans are refused instead of
  crashing or silently turning ping or process logging off. An unreadable password
  file is logged.
- History records are stamped before the public-IP lookup, so they are not late during
  an internet outage.
- Hardware collector: a backup timer with no trigger on record within 30 h of boot is a
  note ("has not fired since boot"), not an alarm; systemd keeps the last trigger across
  a reboot only for `Persistent=true` timers. The installer turns the timers on even
  when the first run reports a crash.
- OpenWrt: the read-only system image (`/rom`) is no longer listed as a full drive;
  the installer refreshes opkg's package lists when it has to install something;
  history rotation syncs to flash before replacing the file.
- `tui.py`: no back-to-back refetching when the server is slower than the interval.
- Service uptime: `http_targets` in `config.json` lists URLs the server requests every
  minute (`services.check_interval`). A new Services card shows each one up or down with
  its uptime over the last hour, day and week, and its history card shows outages as a
  grid over time. Results are logged to `logs/services.jsonl`; `/api/services`,
  `/api/services/history` and a `Services` line in `/text` serve them. Off until targets
  are configured; Linux server only.
- Hardware collector: a part of the 15-min run that crashes (a bug, or a value it cannot
  read) is now a `collector: <part> crashed` alarm, and every other part still reports.
  Before, one crash left the whole snapshot stale and hid every other alarm with it.
  The run still exits 1, so root is still mailed.
- Hardware collector: the backup timer's last trigger is read correctly on systemd
  versions that ignore `--timestamp=unix` for `systemctl show` (seen on 255). That
  date reached `int()` and crashed every run.

## [0.1.0] - 2026-11-15

- `scripts/history-merge.sh`: merge the history of several boxes, or a box's archives,
  into one file. Inputs are never changed and an existing output is never overwritten.
- Security: Glances 4.5.6 or newer and urllib3 2.8.0. The newer Glances no longer
  depends on `python-jose`, `ecdsa`, `pyasn1` or `cryptography`.
- OpenWrt `--update`: an update interrupted partway no longer reads as "Already at"
  on the next run.
- OpenWrt installer: installs `coreutils-timeout` when it can, so a hung `df` or
  `tailscale` call cannot stall the dashboard.
- Demo build: refuses an output directory whose replaced names would contain the
  checkout.

## [0.0.1] - 2026-11-15

- Dashboard: live CPU, memory, swap, GPU, network, disk I/O, temperature, power and ping
  cards with history charts, from a FastAPI server wrapping Glances, `nvidia-smi`,
  `ping`, `nethogs` and `pidstat`.
- History recording to `logs/history.jsonl`, with `/api/history` and per-field reads.
- Optional root hardware collector: SMART, RAID, ECC, IPMI fans/temps/power, RAPL.
- `/text` console report and the `tui.py` live terminal view.
- OpenWrt backend in Lua serving the same frontend. The installer upgrades uhttpd with
  its Lua plugin: a plugin newer than uhttpd left the dashboard unstarted.
- Themes picked from the toolbar: light, classic, high-contrast, print (also for e-ink)
  and phosphor, or your own CSS file in `themes/` (see `themes/README.md`).
- Demo site (`docs/`, deployed to GitHub Pages): the real frontend against a fake
  in-browser backend; `make demo` previews it locally.
- Versioning: `VERSION`, `/api/version`, the version in the page footer, and
  `install.sh --update` on Linux and OpenWrt.
