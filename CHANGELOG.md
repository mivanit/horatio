# Changelog

Releases are `vX.Y.Z` git tags (README: Versions and updates). Add changes under
`## [Unreleased]`; `make release V=X.Y.Z` turns that section into the release.

## [Unreleased]

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
