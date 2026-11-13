# Changelog

Releases are `vX.Y.Z` git tags; `./install.sh --update` moves a box to the newest one
(README: Versions and updates). Versions follow [Semantic Versioning](https://semver.org):

- **MAJOR**: something on the box has to change by hand, or stops reading back: a
  `config.json` key renamed or removed, the history file format, the `/api/*` responses
  the frontend or scripts rely on, or a step the installer can't do itself. The entry
  says what to do.
- **MINOR**: new features, cards, endpoints or config keys; existing setups keep working.
- **PATCH**: fixes.

Add each change under `## [Unreleased]` as it lands; `scripts/release.sh` turns that
section into the release.

## [Unreleased]

## [0.1.0] - 2026-11-12

First versioned release.

- Dashboard: live CPU, memory, swap, GPU, network, disk I/O, temperature, power and ping
  cards with history charts, from a FastAPI server wrapping Glances, `nvidia-smi`,
  `ping`, `nethogs` and `pidstat`.
- History recording to `logs/history.jsonl`, with `/api/history` and per-field reads.
- Optional root hardware collector: SMART, RAID, ECC, IPMI fans/temps/power, RAPL.
- `/text` console report and the `tui.py` live terminal view.
- OpenWrt backend in Lua serving the same frontend.
- Versioning: `VERSION`, `/api/version`, the version in the page footer with a reload
  prompt after an update, and `install.sh --update` on Linux and OpenWrt.
