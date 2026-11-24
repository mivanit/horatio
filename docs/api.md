# API reference

This reference describes the Python backend. The [Lua backend differences](#lua-backend-differences)
apply on both ordinary Linux and OpenWrt. For terminal usage, installation, and
password setup, see [operations](operations.md).

## Authentication

Most endpoints are unauthenticated, including the dashboard, config, live metrics,
and history queries. These Python endpoints require the dashboard password:

| Endpoint | Where to pass the password |
|---|---|
| `GET /api/history/raw` | `X-Dashboard-Password` header, or `password` query parameter |
| `POST /api/history/settings` | `password` field in the JSON body, alongside the settings |
| `POST /api/alarms/retire` | `password` field in the JSON body, alongside the occurrence identifiers |
| `POST /api/alarms/review` | `password` field in the JSON body, alongside the occurrence and evidence identifiers |

These endpoints return 503 when no password is configured. Setting a password does
not protect the other endpoints; see the [security model](operations.md#access-and-security).

## Metrics and host information

| Endpoint | Response |
|---|---|
| `GET /api/4/<plugin>` | Glances REST API for `cpu`, `percpu`, `mem`, `memswap`, `gpu`, `network`, `diskio`, `load`, `uptime`, `system`, and `fs`; other plugins return 404. The sensor and filesystem exceptions are below. |
| `GET /api/4/fs` | Glances filesystems, deduplicated by device so bind mounts collapse. |
| `GET /api/processlist` | The process list used by the UI, with arguments stripped. |
| `GET /api/list-endpoints` | Implemented API routes (relative to `/api`) and `backend: "python"`; a listed route does not imply its collector or data is available. |
| `GET /api/version` | `{version, build}`: the running release and the commit `install.sh` installed on a git checkout (`null` otherwise). |
| `GET /api/cpu-info` | CPU details from `/proc/cpuinfo`. |
| `GET /api/gpu-processes` | Per-(GPU, pid) memory, plus `gpu_util_percent` and `gpu_mem_util_percent` from `nvidia-smi pmon`. Utilization is `null` when the driver had no sample for that pid, pmon is unavailable or slow, or the GPU is beyond pmon's first four. |
| `GET /api/network-processes` | Per-process network bandwidth from nethogs. |
| `GET /api/disk-processes` | Per-process disk I/O from pidstat. |
| `GET /api/ping` | Pings to the configured targets. |
| `GET /api/ip-info` | LAN, public, and Tailscale IP addresses. |
| `GET /api/mail` | `count` and the newest 10 `{date_epoch, subject}` entries; always 200, with `status` of `ok`, `missing`, or `error`. |

### `GET /api/4/sensors`

Linux hwmon readings served by the dashboard in Glances' sensor shape: `type`
(`temperature_core`, `fan_speed`, or `power`), `label`, `value`, `unit`, `warning`,
and `critical`. Power rows also carry `total`, true only for a whole-machine reading.

Temperature labels preserve Glances' names so history continues. Fan labels are
`<chip> fan<N>` or the kernel's label; fan rows also expose stable `id` values used
by the configured `fans` rules. See [sensor configuration](configuration.md#sensors).

### `GET /api/gpu-info`

NVIDIA GPU details, including `fan_percent`: a percentage of the fan's maximum,
since nvidia-smi gives no RPM; `null` on a passive card. History records GPU fans
as `gpu_fans`, for example `{"GPU 0 fan": 28}`. See
[sensor configuration](configuration.md#sensors) for how GPU fans are displayed.

## Hardware and alarms

The [collector reference](../collector/README.md) describes the snapshots, raw
`alarm_items`/`checked` contract, and collection rules. These endpoints add the
dashboard's status and alarm-history information.

For copyable API and file examples, occurrence selection, and error handling,
see [alarm actions for agents](alarm-actions.md).

### `GET /api/storage-health`

The 15-minute collector snapshot: `alarms`, `info`, drives, md, ECC, SEL, backup,
failed units, and journal errors. Always returns 200, with `status` of `ok`,
`stale`, `missing`, or `error`, and `age_sec`. A source the box does not have
(such as a BMC or ECC reporting) is `null` with one `info` note.

Also includes the server's `sensor_alarms`, `sensor_notes`, and `sensor_alarm_held`
regardless of the collector's state, plus its own ZFS readings:
`zfs: {status, error?, pools: [{name, state, errors, data_errors, scan}]}`,
where the ZFS `status` is `ok`, `error`, or `absent`.

### `GET /api/sensors`

The root collector's 60-second snapshot: `fans`, `temps`, `power`, `gpus`, and
`alarms`, with the same status/age fields as storage health. It is stale after
300 seconds. GPU rows include nvidia-smi temperatures, driver limits, thermal
slowdown, and ECC. Power rows carry `unit` and `total` (true for the BMC's
whole-machine reading).

### `GET /api/alarms`

Current concerns and when they began:

| Field | Meaning |
|---|---|
| `active` | `{source, key, text, value, since, initial}` rows from the sources below. |
| `sources` | Each source's `status`. A status other than `ok` means unknown, never clear. |
| `held` | Previously observed alarms that could not be re-checked, such as a failing drive that is asleep. |
| `reviewed` | Journal or BMC event evidence already reviewed by an operator. New evidence returns it to `active`. |
| `notes` | The server's setup hints and other notes, such as a reboot pending under a week or a scrub in progress. |
| `recent` | The newest 50 lines of `logs/events.jsonl`. |
| `retirement` | Metadata advertising whether retirement is supported and a password is configured. |
| `review` | Metadata advertising whether evidence review is supported and a password is configured. |

Held rows include `unchecked_since` (epoch seconds). Reviewable rows include
`evidence`, an array of opaque underlying-entry tokens. Reviewed rows also carry
`reviewed: {timestamp, note, evidence}`; `timestamp` is the review's epoch time.
Use the current row's `evidence` when submitting a review, not an older review's
tokens. Both capability objects contain `supported` and `password_configured`
booleans. Availability does not guarantee an individual row is actionable.

The sources are `state` (the 15-minute snapshot), `sensors` (the 60-second
snapshot, counted only while fresh), `dashboard` (fan rules and host checks), and
`services` (a down target, counted while the checks are fresh).

`since` is epoch seconds from the server's alarm history; for a service outage it
is the first failed check. `initial: true` means an alarm was already present when
history began, so its real start is unknown. With collectors too old to supply
alarm keys, `key` and `since` are `null`.

See [alarms](operations.md#alarms) for the host checks, lifecycle, and interpretation
of stale or held findings; [configuration](configuration.md) owns the configurable rules.

### `POST /api/alarms/retire`

Close a held occurrence with a JSON body containing
`{source, key, since, unchecked_since, password, note?}`. The optional `note` is a
string of at most 2,000 characters, saved with the `retired` event. Copy the
occurrence identifiers from `GET /api/alarms`. Returns 409 if that occurrence changed or is no longer held,
and 503 when no password is configured. The GET response's `retirement` metadata
advertises availability. See [operations](operations.md#alarms) for the UI workflow
and what retirement means.

Notes are readable through alarm history by anyone with dashboard access; the
password protects writing them, not reading them.

### `POST /api/alarms/review`

Mark existing journal or BMC event evidence as reviewed, using
`{source, key, since, evidence, password, note?}`. Copy `evidence`, an array of
opaque entry identifiers, from the current alarm. The optional note has the same
limit as retirement. The server checks the occurrence and fresh evidence before
saving; a changed occurrence or evidence returns 409. Live conditions such as a
stopped fan cannot be reviewed away. Reviewed evidence stays in history and is
listed separately from current concerns. A new underlying log entry makes it
unreviewed again; older entries aging out do not.

Both actions return HTTP 200 with `{status: "retired" | "reviewed", source, key}`
on success, or a JSON `{error}` on rejection. They are occurrence-specific:
repeating a successful action can return 409. After a timeout, inspect current
state and history before retrying. See the
[agent guide's error table](alarm-actions.md#verify-file-results-and-handle-failures)
for validation, authentication, conflict, and storage errors.

Local agents can submit the same actions through request files without HTTP or a
dashboard password; see [alarm actions for agents](alarm-actions.md). Directly
editing the server-owned state or event log is not a supported action interface.

### `GET /api/alarms/history`

Takes `hours` (default 24), returning `{cutoff, now, events, truncated, skipped}`.
`cutoff` and `now` are epoch seconds. Events are sorted by observation time and
retain their log fields, including notes and occurrence identifiers. This is
hardware/host alarm history; service outages have their own history endpoint.

The reader scans at most 4 MiB from the end of `logs/events.jsonl`. `truncated`
means earlier bytes were not inspected, so relevant events may be missing;
`skipped` counts unusable records. A missing file means no recorded history,
whereas an unreadable file is an error. An empty event list does not mean the
machine was healthy: an ongoing fault may produce no events in the range.

`cleared` means the source checked the alarm and no longer reported it under the
current rules. This can follow a healthy reading, a rule change, a counter reset
or old log entries aging out; it does not establish that hardware was repaired.
`retired` means an operator closed an unchecked alarm, and `expired` means
evidence remained unavailable. `reviewed` records an operator review;
`reopened` records new evidence after review, or
review becoming inapplicable because complete evidence is unavailable
(`reason: "evidence_unavailable"`).
These are observations, not a continuous health trace. `initial: true` means
the fault was already present when first observed, and `after_boot: true` on a
clear event indicates it cleared after a reboot.

## Service checks

Configure targets with [`http_targets`](configuration.md).

### `GET /api/services`

Top-level `stale: true` means no check round has finished for several intervals;
the card and `/text` show the old results as stale. Each target has:

| Field | Meaning |
|---|---|
| `ok` | Whether the check passed; `null` before its first check. |
| `status`, `ms`, `error`, `checked` | HTTP status, request duration, error, and check time. |
| `down_since` | Epoch of the first failed check in the current outage, at most a week back; `null` when up. |
| `uptime` | `{up, checks}` counts over the last `1`, `24`, and `168` hours. `checks: 0` means nothing was checked in that window. |

### `GET /api/services/history`

The outage grid, taking `hours` (default 24). For each target, `count` pairs of
`[passed, failed]` checks, one per `bucket_sec` seconds from `start`, with at most
300 buckets. `[0, 0]` means no checks in that bucket. `truncated: true` means the
log could not be read back as far as `start`.

## History and recording settings

### `GET /api/history`

History records for `hours` (default 24), without the heavy fields `temps` and
`processes`, or `ip`. Where cpufreq sysfs exists, records also carry `cpu_freq`:
min/median/avg/max MHz over every core and burst sample, plus the hardware range.

`since=<timestamp>` (URL-encoded) returns only newer records. Responses include
`cutoff` (the window start, for trimming records a client already holds) and `now`
(the server's clock, for the next `since`), both epoch seconds. With `since`,
`truncated: true` means the read stopped before reaching `since`: refetch without
it. `skipped_lines` counts unreadable lines in everything the server parsed,
which can extend further back than the records returned.

See [history configuration](configuration.md) for recording intervals, read limits,
and rate caps, and [operations](operations.md) for stored files and merging history.

### `GET /api/history/field/{name}`

One heavy field over the range, with `records` shaped as
`[{timestamp, <name>}]`: `temps` for every sensor, or `processes` for per-process
rows when process logging is enabled. The corresponding history cards request
these fields separately. Takes `hours` and `since` like `/api/history`.

### Recording and settings endpoints

| Endpoint | Behavior |
|---|---|
| `POST /api/history/record` | Record now, rate-limited. |
| `GET /api/history/raw` | Download `history.jsonl` as recorded, including rates capped when each record was taken, without re-capping. Requires a password. |
| `GET /api/history/settings` | Current recording settings. |
| `POST /api/history/settings` | Update recording settings; JSON body with settings and `password`. Changes revert to `config.json` on restart. |

See [authentication](#authentication) for password transport and
[configuration](configuration.md) for setting meanings.

## Dashboard and terminal endpoints

| Endpoint | Response |
|---|---|
| `GET /` | Dashboard UI. |
| `GET /config.json` | Config as written, including `http_targets` URLs. Do not put passwords or tokens in those URLs. |
| `GET /themes/index.json` | Theme names for the picker, from `themes/*.css`. |
| `GET /text` | Console report; query options below. |
| `GET /tui.py` | Live terminal client, served so it runs without a checkout. |
| `GET /help` | Plain-text guide to `/text` and the JSON API, written for people and agents. |

For example: `curl "http://<host>:8080/text?width=$COLUMNS"`.

| `/text` query | Values or default |
|---|---|
| `view` | `latest`, `history`, `both` |
| `style` | `unicode`, `ascii`, `simple` |
| `color` | `auto`, `on`, `256`, `16`, `off` |
| `hours` | Defaults to 1–3 based on width and record interval |
| `width` | Defaults to 80 |
| `height`, `live`, `table`, `rows` | Further rendering options; `/help` gives usage on the running server |

See [operations](operations.md) for using the live terminal client.

## Lua backend differences

Both Lua installers use the same API implementation. See the [Lua guide](../lua/README.md)
for installation, configuration paths, and service behavior.

- `/api/4/*` reads Linux directly, without Glances. Supported names are `cpu`,
  `percpu`, `mem`, `memswap`, `network`, `fs`, `sensors`, `load`, `uptime`, and
  `system`; sensors provide temperatures. GPU, fan, power, host-alarm,
  root-collector, service-check, and process endpoints described above are absent.
- `/api/cpu-info`, `/api/ip-info`, `/api/ping`, and `/api/version` are available;
  version `build` is always `null`.
- `/api/list-endpoints` returns the Lua routes as `endpoints`, plus `backend: "lua"`
  and `history_metrics` for the supported history charts.
- Temperature readings include a history `key` derived from physical topology
  where available. Readings without a reliable, unique identity remain visible
  live but have `key: false` and are omitted from history with a log diagnostic.
- History reads accept `hours` but do not implement `since`, `cutoff`, or `now`.
  They read at most the newest 4 MiB and report `truncated` when the range does not
  fit. The only `/api/history/field/{name}` route is `temps`. Lua history does not
  apply the Python rate caps.
- `POST /api/history/record` and `GET /api/history/settings` are available.
  `POST /api/history/settings` returns 501: edit `www/config.json` and restart
  instead. `/api/history/raw` is absent.
- `GET /api/history/tick` lets the installed timer or cron job trigger recording
  when due; it returns `{status: "ok"}`.
- The static dashboard, config, and theme index are served from `www/`.
  `/text`, `/tui.py`, and `/help` are Python-only. Lua HTTP has no authentication.
