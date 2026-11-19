# Changelog

Releases are `vX.Y.Z` git tags (README: Versions and updates). Add changes under
`## [Unreleased]`; `make release V=X.Y.Z` turns that section into the release.

## [Unreleased]

## [0.2.1] - 2026-11-18

- History cards line up: every chart and the Services grid show the whole requested
  range on a plot of the same width, with the same time ticks, so one moment is at the
  same place on every card. A box with less history than the range shows it at the
  right of a partly empty chart instead of stretched across it. In the Services grid
  the target's name now sits on its row and the uptime is to the left. The name is a
  link to the target's address, and each cell's tooltip names the target and says
  how many of its checks failed.
- Ping history is no longer squeezed into the middle of its chart.
- History time axes have ticks at round times (every 5 minutes for an hour, every few
  hours for a day, with the date at midnight) instead of one label for a short range.
  History y axes name their unit and shorten large numbers (`12.5k`, `2M`).

## [0.2.0] - 2026-11-18

- OpenWrt `--update` (these apply to updates made after this release: a router updates
  with the script it already has): `--update vX.Y.Z` always downloads and reinstalls,
  the current version included, so an update that stopped partway can be repaired or
  undone; a cut-off download is refused before anything is unpacked; the temporary
  folder of an update that was killed is removed by the next one. If the install step
  of an update fails, run `sh openwrt/install.sh`.
- OpenWrt installer: failures of `opkg update` and of the optional `coreutils-timeout`
  install are shown instead of hidden; when `network.lan.ipaddr` is not set, the
  installer names the address it guessed and how to override it.
- OpenWrt: a `www/config.json` that is not valid JSON is reported in `logread` (the
  backend runs on defaults); a quoted or non-numeric `history.record_interval` or
  `history.max_file_mb` no longer breaks the backend.
- `scripts/history-merge.sh`: a failed write (full disk) under BusyBox is an error, not
  a shorter merge reported as a success.
- Hardware collector: a failing `systemctl` call while checking that the backup timer
  exists is an alarm (`collector: backup-timer: ...`), not "no run on record".
- Service checks: with a timeout longer than the check interval, a healthy checker with
  one hanging target no longer reads as stale for a few seconds each round.
- `/text`: a GPU row's temperature is coloured by `temp_thresholds["GPU N"]` when set.
- `tui.py`: an address without `http://` works.
- History charts: on long ranges a single record between two outages is drawn as a
  point instead of nothing.
- Dashboard: a failed services request clears the "all up" header and the red border
  of the last good answer. Sensor names of 7 characters fit under their bars on the
  smallest card.

- `temp_labels` in `config.json` gives temperature sensors display names on the card and
  in its history chart (records keep the original names). `temp_thresholds` gives single
  sensors their own limit, on the card and in `/text`'s Temperatures line.
- History charts no longer draw a line across a stretch with no records longer than
  `history.max_gap_seconds` (default one hour; `0` restores the old behaviour).
- Service checks: when no round has finished for several intervals, `/api/services`
  says `stale: true` and the card and `/text` show the results as stale, not as the
  last "up". This also covers a first round that never finishes after a start with no
  log. An unexpected error in a check is logged with its traceback.
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
