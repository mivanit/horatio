# Changelog

Releases are `vX.Y.Z` git tags (see [versions and updates](docs/operations.md#versions-and-updates)). Add changes under
`## [Unreleased]`; `make release V=X.Y.Z` turns that section into the release.

## [Unreleased]

- Service uptime counting runs off the event loop for the services, alarms, and
  terminal endpoints. Uptime remains successful checks divided by total checks;
  periods without checks do not count as downtime.
- History recording uses a separate persistent Glances connection pool from
  interactive requests. Startup, scheduled, and manual records share that pool;
  clients close after background tasks finish, including on startup failure.
- **Upgrade requirement:** run `./install.sh` after pulling this release and
  before restarting the Python service. The service now launches
  `python/server.py`; there is no root-level compatibility launcher.
- Held alarms can be manually retired with the dashboard password. Retirement
  is recorded in the event history, survives restarts and does not suppress a
  newly observed fault. Active alarms cannot be retired.
- Journal faults hidden by the display limit remain active and cannot be retired.
  An unreadable SMART self-test log no longer clears an earlier self-test alarm.
- GPU alarms use PCI addresses rather than display indices. USB/HID and I2C fan
  identities preserve physical topology across bus renumbering; legacy alarm
  keys remain held until explicitly retired. Existing fan display aliases keep
  working, and fans lacking durable identity still report current low readings.
- ZFS spare inventory no longer prevents error recovery. Older power records
  exclude PSU inputs from stacked components; Lua temperature history keeps
  distinct physical channels instead of overwriting equal labels, including
  across USB/HID and I2C bus renumbering.
- History setting changes are serialized; missing GPU process mappings stay
  unknown. Host-check responses use one cached result. Optional unconfigured
  collectors are shown neutrally, while missing expected collectors still warn.
- Lua history controls reflect supported features. `/api/list-endpoints` now
  identifies the backend as `"lua"` instead of `"openwrt"`.
- Mail cache invalidation includes permissions and ownership. Installer test
  timeouts allow for slower hosts without changing production timeouts.

- The Lua installers are named `lua/install-linux.sh` and `lua/install-openwrt.sh`.
  The former `lua/install.sh` and `openwrt/install.sh` entrypoints are removed;
  older installations need a manual source update followed by the new installer
  (see [migration instructions](lua/README.md#openwrt)). On Linux/systemd, native
  dependencies are built in the checkout, the service runs as
  the installing user, and a systemd timer records history while the dashboard is closed.

- Source layout: Python modules live in `python/`, and the lightweight Lua backend
  lives in `lua/`. Config, credentials, logs and frontend assets stay at the root;
  the served `/tui.py` URL stays the same. The installer updates the Python service
  entrypoint. The OpenWrt updater can still install older releases using their
  original installer names.
- The static demo includes GPU fan percentages alongside chassis RPM, exercising
  both fan-chart axes in the live view and history. Native backend tests are named
  `test-python` and `test-lua`; previous Make target names remain aliases.
- Alarm recovery requires readable evidence for each rule. Missing BMC/GPU,
  SMART, RAID and memory readings retain earlier alarms; per-device fan and ECC
  alarms survive partial device inventories. NVIDIA driver mismatches remain
  alarms even when the installed library version cannot be inferred.
- Linux fan rules now use physical device/channel ids from `/api/4/sensors`, or
  aliases of those ids. Existing display-name rules need a configuration update;
  they cannot silently move to another fan. Defaults still apply to all fans.
- Hardware findings and history remain visible without the main health snapshot.
  Host/ZFS checks refresh in the tracker, with pending/stale status in the API.
  Starting or canceling a scrub does not clear an overdue scrub alarm, and older
  ZFS versions retain discovered pools for missing-pool detection.
- Power normalization rejects incomplete PSU sums. Stacked history handles known
  legacy total labels per record, without rewriting old history files.
- The server raises its own host alarms, in `/api/alarms` and on the Hardware card:
  `/run/reboot-required` (a note, an alarm after a week), an NVIDIA kernel module that
  no longer matches the installed library (at once; the text says whether a reboot fixes
  it), and ZFS pools (`zfs.py`: not ONLINE, device or data errors, never scrubbed, last
  scrub older than `zfs_scrub_max_days` (38), an expected pool missing, or state
  unreadable while pools are expected). New config keys `zfs_pools`,
  `zfs_scrub_max_days`, `sensor_hide`.
- `GET /api/alarms` gains `notes`; **`GET /api/sensor-alarms` is removed** (its alarms
  were already in `/api/alarms`). Update login scripts that read it.
- GPU fans (percent of maximum) on the Fans card against a second axis, and in history
  as `gpu_fans`; `/api/gpu-info` gains `fan_percent`.
- Fans: a fan that stops after turning stays on the card at 0 RPM; a ruled fan that
  cannot be read is an alarm; rules and alarm text use `sensor_labels` names; a
  non-number `min_rpm` is a note. Fan names follow the device, not probe order.
- Power: the history picks the first BMC total like the live card and never stacks a
  second total; more BMC total names; per-PSU inputs are not parts; APU package power
  is not counted twice. Temperatures are sorted like Glances did.
- Alarm history: keys with a `#2` suffix clear; a held alarm needs 3 unchecked snapshots
  as well as 30 days to expire; the server's own alarms it cannot re-check are shown as
  held; a failing sensor read no longer stops the collectors' tracking.
- New `GET /api/alarms`: everything wrong right now in one list (both collector
  snapshots, the server's own fan rules and any service that is down), each with since
  when, plus the newest lines of the event log. Until now the two snapshots each had their own `alarms`
  list and a reader of one missed the other. `GET /api/services` gains `down_since` per
  target: when its current outage began.
- Dashboard: the Hardware health card says since when each issue has been there
  (`since 2026-11-21 17:50 (2 d)`, or `seen since …` when it predates the alarm
  history), keeps showing an alarm that could not be re-checked (`not re-checked since
  …`; the header then reads `N unchecked` instead of `all clear`), and lists the last
  five alarm changes under "Recent changes". Several lines of one kind in a row share
  one `check:` hint. The Services card shows when an outage began
  (`down (timeout) since …`). All of it appears once the collectors and the server are
  updated; an older one of either simply shows what it showed before.
- OpenWrt installer (apk): `apk update` runs first (its index is lost at every reboot; a
  failure is a warning), and `uhttpd` is upgraded in one transaction with LuCI's uhttpd
  plugins (`uhttpd-mod-ubus`, `uhttpd-mod-ucode`) when the router has them. Until now only `uhttpd-mod-lua` was named, so a router
  with LuCI kept an old `uhttpd-mod-ubus` beside the new server and LuCI stopped
  answering until that package was upgraded by hand.
- OpenWrt: `/api/4/uptime` uses the Linux backend's format, `1 day, 20:14:03` (was
  `1d 20:14:03`), so a reader of the API needs one parser for both.
- Hardware collector: every alarm now has a stable identity. Both snapshots carry
  `alarm_items` (a `key` for each alarm string, plus a `value` for fault counters such as
  reallocated sectors and ECC counts) and `checked` (what this run really evaluated, so a
  reader can tell an alarm that cleared from one that was merely not looked at: a drive in
  standby, a BMC that timed out, a section that crashed). Drives are keyed by serial, since
  `sdX` letters move between boots. The alarm strings are unchanged. Also new: `schema`
  and `boot_id`. Keys are listed in `collector/README.md`.
- Alarms have a history. The server follows every alarm, whoever decides it (the two
  collectors' by their keys, and its own fan rules), and appends a line to
  `logs/events.jsonl` when one is `raised`, `changed` in value (`8 -> 9` reallocated
  sectors) or `cleared`; nothing is written while it just stays. An alarm that could not
  be re-checked (a failing drive that is asleep) is kept instead of being reported as
  gone, and the 60 s snapshot's alarms and the fan rules must be absent three times in a
  row to clear, so a value on its threshold does not flap. The log also records a `boot`
  and a `gap` when a source, or the server itself, was not running. The collectors keep
  no state, as before. README: "What is kept in `logs/`".
- Hardware collector: the journal alarm is now one line per distinct error-level message,
  `journal: <unit>: <message> (N in 24h, first …, last …)`, oldest first and at most 8,
  instead of `journal: N error-level messages … latest: <one of them>`, which could hide
  an important message behind a harmless later one. New config key `journal_ignore`
  (a list of regexes) drops noise particular to a box.
- Hardware collector: the ATA attribute alarm is one line per attribute
  (`sde: reallocated=8`, `sde: pending=2`) instead of one joined line, and new config key
  `smart_baseline` (`{serial: {attribute: count}}`) accepts what a drive already has: the
  alarm returns as `sde: reallocated=9 (baseline 8)` only when the count grows.
- Hardware collector: `state.json` has `boot`, with how the previous boot ended (`clean`,
  its last log time and kernel). An unclean end (power cut, hang) is an `info` note for a day.
- Power card without a whole-machine reading (no BMC or meter): with `power_psu_watts`
  set, the measured parts warn once they alone pass the warning line, since the machine
  then draws at least that much. The meta line names the line in watts.
- `./install.sh -h` / `--help` prints the installer's options and exits, from any user,
  without changing anything.
- Sensors on any Linux box: the dashboard reads Linux hwmon itself (`hwmon.py`, no root,
  no collector) instead of Glances' sensors plugin, which is now switched off. It serves
  `/api/4/sensors` in the same shape, so the cards, the OpenWrt backend and history read
  one format, and temperature names are exactly Glances' (history and `temp_labels`
  continue).
  - Fans: every chip's fans, including boards whose fan chip keeps its readings under
    `hwmonN/device/` (Fintek, older ITE), which Glances dropped whenever another chip
    (e.g. the GPU) had a fan. Named `<chip> fan<N>` or by the kernel's label.
  - Power: chip readings join the power bar and history, e.g. an AMD FX/Opteron CPU's
    own estimate as "CPU (est.)", an ACPI power meter as the whole machine's draw.
    Battery and charger "power" (a charge rate) is left out.
  - Disconnected and faulty inputs (fault or disabled flags, -128 °C) are dropped
    without renaming the sensors after them; backwards BIOS limits are put in order.
  - config.json `fans`: per-fan `min_rpm` (an alarm, or a note with `"on_low": "note"`)
    and `hide_if_zero` (default on: empty fan headers no longer show as 0 RPM fans),
    plus a `default` rule. Alarms appear on the Hardware card, also on a box without the
    collector, and at `/api/sensor-alarms` for login scripts.
  - Hints on the Hardware card: a desktop with no fan readings is told to run
    `sensors-detect` and load its Super I/O driver; a VM is told it has no sensors.
  - config.json `sensor_labels` renames any sensor (fans and power too);
    `temp_labels` is still read.
- Power card: which reading is the whole machine is said by the data, never guessed from
  names: the collector marks the BMC's row `total` and every row's `unit`, hwmon rows say
  `total`, and history records name it in `power_total` (older records: the BMC's
  "Pwr Consumption", as before). The BMC wins over an hwmon power meter. Segment colours
  follow what a part is (CPU, DRAM, GPU), so "CPU (est.)" is coloured as a CPU.
- Power card: the dashed PSU line and its warning appear only when `power_psu_watts` is
  set, and without `power_max_watts` the bar scales to the reading (a 60 W desktop gets
  a 100 W bar). The example config no longer ships 1200 W / 1100 W, which every new box
  copied: remove them from your `config.json` if they are not your machine's.

- Hardware card: a box without a BMC or without ECC reporting no longer shows
  "BMC log, memory events: 0 lifetime" or "ECC since boot: 0 corrected", which read as
  healthy counts of something that is not being measured. The collector writes `null`
  for those sources with one note each ("no BMC …", "no ECC reporting (no DIMMs under
  EDAC): memory errors are not monitored"), and the Memory section's `check:` hint names
  only the tools for what is present.

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
