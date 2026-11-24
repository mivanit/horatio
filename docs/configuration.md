# Configuration

The Python installer creates the per-machine, gitignored `config.json` from
[`config.example.json`](../config.example.json) and keeps it on subsequent installs.
Restart `horatio` after editing server settings; reload the page for display changes.
The [Lua backend](../lua/README.md) uses `www/config.json`; follow its instructions
for editing and restarting. Backend-specific settings are marked below.

**The dashboard serves `config.json` to every visitor. Put no passwords or tokens
in it, including in target URLs.** The root collector has its own configuration at
[`/etc/horatio/config.json`](../collector/README.md#config-etchoratioconfigjson).

## General settings

| Key | Meaning |
|---|---|
| `title`, `title_emoji` | Page title and heading. |
| `ping_targets` | `[{"address": "host"}]`, pinged every poll and recorded to history. |

## Service checks

Python backend only. `http_targets` is a list of `{name, url}` objects for the
Services card and its outage history. Absent or `[]` disables checks and hides the
card, which is the default.

A target is up when its request, after redirects, returns a status below 400.
An optional `"status": 401`, for example, requires that exact status instead.
`"insecure": true` skips the TLS certificate check for a self-signed certificate.
A failed request is repeated once after 2 seconds before it counts as down.

| Key | Meaning | Default | Range |
|---|---|---|---|
| `services.check_interval` | Seconds between check rounds. | 60 | 10–3600 |
| `services.timeout` | Maximum total seconds for one request. | 5 | 1–30 |

Each round appends one line to `logs/services.jsonl`, which is never rotated or
deleted: about 0.1 MB a day per target at a 60-second interval.

To watch another Horatio, use its `/api/version`. A Horatio that is down checks
nothing, so its own downtime shows as “no checks”, not as an outage. Have two boxes
watch each other. See the [API reference](api.md) for current results and outage
history responses.

## History

| Key | Meaning | Default |
|---|---|---|
| `history.record_interval` | Seconds between records. | 600 |
| `history.min_record_interval` | Python: minimum seconds between manual “record now” requests; `0` disables the limit. | 60 |
| `history.sample_duration`, `history.sample_count` | Python: average `sample_count` samples over `sample_duration` seconds per record. | 10 seconds, 5 samples |
| `history.max_gap_seconds` | Leave a gap in charts when records are further apart than this; `0` joins everything. | 3600 |
| `history.max_hours` | Python: largest history window any read serves, in hours. | 8760 |
| `history.max_file_mb` | Lua: history file size that triggers archiving its older half. | 16 |
| `history.processes_enabled`, `history.process_count` | Python: record processes, and how many top processes to select by CPU and by memory. | `false`, 10 |

The Lua backend uses one one-second sample and a fixed 60-second manual-record
limit; `sample_duration`, `sample_count`, and `min_record_interval` do not configure
those values. Its timer/cron scheduling is described in the
[Lua guide](../lua/README.md).

### Read limits and rotation

On Python, `history.max_hours` accepts 0.1–8760 hours and clamps larger requests to
`/api/history`, `/api/history/field/<name>`, and `/text`. Lower it on a small box:
a year-long window parses up to the 64 MB history tail into memory.

On Lua, once `logs/history.jsonl` exceeds `history.max_file_mb`, its older half is
gzipped beside it and the live file retains its newest half. The default 16 MiB
holds some months of records. Rotation runs in a background process so a large
file cannot time out a request. It needs free space for both the compressed
archive and retained half, and never deletes history. Failures are logged to
syslog and retried after the next record. History requests read at most the newest
4 MiB and report `truncated` when the requested range does not fit.

For log locations and restoring combined histories, see
[operations](operations.md#merging-history).

### Process history

With `history.processes_enabled`, each Python history record includes the top
`process_count` processes by CPU and by memory, plus any holding GPU memory.
Rows always have `pid` and `name`; when known and non-zero, they also include
`ppid`, `create_time`, `user`, `status`, `threads`, `cpu_percent`, `rss_mb`,
`disk_read_kbps`, `disk_write_kbps`, `gpu_mem_mib` with `gpus`, and
`gpu_util_percent`.

At a count of 10 this roughly doubles a record's size, so the 64 MB history tail
covers fewer days. Process data is retrieved separately through the
[history field API](api.md).

### Runtime recording settings

On Python, recording settings can also be changed from the page with the dashboard
password. Those changes revert to `config.json` on restart. Lua does not support
runtime settings changes; edit `www/config.json` and restart its service.

## Card thresholds

`thresholds.*_percent`, `thresholds.temp_celsius`, `thresholds.network_mbps`,
`thresholds.disk_io_mbps`, and `thresholds.ping_ms` control orange-card warnings.
The [example config](../config.example.json) lists the defaults.

For the power bar:

| Key | Meaning |
|---|---|
| `thresholds.power_psu_watts` | Dashed PSU line; absent means no line and no PSU warning. |
| `thresholds.power_max_watts` | Bar's right edge; absent means PSU watts plus 10%, or, without a PSU, 1.5 times the reading rounded up to 50 W. |
| `thresholds.power_warn_fraction` | Orange threshold as a fraction of the PSU rating. |

## Sensors

### Labels and temperature limits

`sensor_labels` gives display names to temperature, fan, and power sensors, for
example `{"loc1": "I350 NIC"}`. For Python's Linux hwmon fans, use the stable `id`
returned by `/api/4/sensors` as the key. New fan history uses that physical identity;
old name-based history remains readable. Other history records keep the sensor's
own name. `temp_labels` is the older setting and is still read; `sensor_labels`
wins where both name a sensor.

`temp_thresholds` sets individual limits in °C for sensors that run hot by design,
for example `{"I350 NIC": 95}`. Use the sensor's own name or its
`sensor_labels`/`temp_labels` name; the card's short forms “Pkg”, “NVMe”, and “Cores”
are not names. The sensor turns orange at this limit instead of its source's limit
or `thresholds.temp_celsius`.

In Python's `/text`, the Temperatures line's `max` is coloured by the sensor nearest
its own limit, and a GPU row's temperature by `GPU N`'s limit. The history `MaxT`
row still uses `thresholds.temp_celsius`.

GPU fans report a percentage, not RPM. The Fans card draws them as bars against a
second, 0–100% axis. Their `gpu_fans` history appears as dashed lines against a
percentage axis inside the plot.

### Fan rules

Python backend only. `fans` contains rules keyed by a stable fan `id` from
`/api/4/sensors` (for example `@platform/nct6775.656/nct6775/fan1`), or by a
`sensor_labels` alias for that id, plus a `default` rule. Rules merge
default → alias → id, identically when a fan is present or missing. The server
reads them at startup.

- Below `min_rpm` is an alarm; `"on_low": "note"` makes it a note.
- `"min_rpm": null` disables an inherited minimum.
- `hide_if_zero` defaults to true. It hides a zero reading only if the fan has no
  minimum and has not been seen turning.

An explicitly ruled missing fan keeps its alarm. Unreadable fans covered only by
the default rule remain unchecked in alarm history. Old Linux display-name rules
must be updated to ids: ordinal names can move to another device and are never
silently reassigned. Without a stable physical path, the reading stays visible
with a setup note and its rules are not evaluated.

### Hidden sensors

Python backend only. `sensor_hide` lists regular expressions for sensors to leave
out everywhere: cards, history, and fan rules. They match from the start of a
sensor's own name, stable fan id, or `sensor_labels` name, for example
`["unknown.*", "nouveau"]`. Use stable fan ids or aliases of ids to keep exclusions
effective after a device disappears.

The list is empty by default. Glances' `hide=unknown.*` was never in effect because
Horatio ran Glances without a config file. The server reads `sensor_hide` at startup.

## ZFS

Python backend only. `zfs_pools` names pools that must exist; each missing pool is
an alarm. Pools are also expected once seen (remembered in `logs/zfs-pools.json`),
or when a disk's label says `zfs_member`.

`zfs_scrub_max_days` sets the oldest a finished scrub may be. The default is 38 days:
Ubuntu scrubs on the second Sunday of each month, up to 35 days apart. Read-only
`zpool status` needs no root where `/dev/zfs` is mode 0666, as with zfsutils-linux's
udev rule. The server reads both settings at startup.

## Rate caps

`limits.network_mbps` and `limits.disk_io_mbps` are ceilings in **MB/s** for each
network interface and each drive. Defaults are 1250 (10 Gbit/s) and 15000,
respectively. They guard against occasional absurd Glances rates. A sample above
its cap counts as the cap, so a spike is shortened, not removed.

Totals are sums of the capped interfaces or drives and are not capped themselves.
Bridges, veths, and tunnels can count the same traffic more than once. Caps apply
to live cards and to Python history and `/text`; they do not alter raw
`/api/4/*` endpoints or Lua history.

A Python record that hit a cap lists it under `rate_capped`: `metric`, `interface`
or `device` plus `drive`, `samples` of `of`, `raw_max`, and `cap`. The server also
logs it. Each record notes the caps it was taken under in `rate_caps`.

The Python server reads limits at startup; restart after changing them. The page
reads them on load. Records without `rate_caps` (recorded before caps existed) are
clamped when read. Records with it are served as recorded, so a changed cap applies
to new records only.

## Browser settings and themes

Poll interval, chart length, grid columns, card order, and theme live in the browser.

The toolbar's Theme picker lists the CSS files in `themes/`. Horatio ships `light`,
`classic`, `high-contrast`, `print` (also for e-ink), and `phosphor`. Any other
`themes/<name>.css` you add appears too. A theme overrides color, font, and shape
variables in `static/style.css` and can restyle any selector. See
[the theme guide](../themes/README.md) for authoring instructions suitable for
handing to an agent along with a description of the desired look. Lua installations
must rerun their installer after adding or updating themes.
