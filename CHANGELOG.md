# Changelog

Releases are `vX.Y.Z` git tags (README: Versions and updates). Add changes under
`## [Unreleased]`; `make release V=X.Y.Z` turns that section into the release.

## [Unreleased]

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
