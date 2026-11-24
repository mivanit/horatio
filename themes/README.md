# Themes

A theme is one CSS file in this folder, `themes/<name>.css`. The **Theme** picker
in the toolbar lists every theme here; each browser remembers its own choice
(localStorage key `horatioTheme`). "default" means no theme file.

- **Name**: letters, digits, `_` and `-` only, and not `default`. Other files
  are ignored.
- **Loading**: the page loads the theme right after `static/style.css` and
  before any script runs, because the scripts read colors from CSS once at
  startup. Choosing a theme therefore reloads the page.
- **Adding one**: on Linux, drop the file here; it shows up in the picker
  straight away. On OpenWrt, add it here and rerun `lua/install-openwrt.sh`, which
  copies themes into `www/themes/` and writes the list (symlinks are copied as
  files, since uhttpd won't serve them).
- **Iterating**: a plain reload shows an edit. The Linux server sends themes
  with `Cache-Control: no-cache`; on OpenWrt, rerunning the installer gives
  theme URLs a new version stamp.
- **Shipped examples**: `light.css` changes only variables. The others also
  restyle selectors: `classic.css` (a 1990s desktop), `high-contrast.css`
  (black and white, heavy edges), `print.css` (black ink on white, no motion;
  for reports, printing and e-ink), and `phosphor.css` (a green-screen
  terminal with scanlines and glowing chart lines).

## Variables

The full list, grouped and commented, is the `:root` block at the top of
[`static/style.css`](../static/style.css); that block is the reference. A
theme overrides any of them in its own `:root`. Every color in the stylesheet
comes from these variables. Tinted backgrounds (warning and error highlights)
are mixed from the status colors with `color-mix()`, so they follow
automatically.

Groups:

- **Backgrounds** (`--bg-*`), **borders** (`--border-*`), **text** (`--text-*`).
- **Status** (`--color-success`, `--color-error`, `--color-warning`, ...).
- **Chart series** (`--color-download`, `--color-gpu`, `--color-ping-0..2`, ...).
- **Typography**: `--font-family` (the page and the chart text).
- **Shape**: `--radius-xs` to `--radius-xl`, `--shadow-color`.
- **Charts**: `--chart-font-size` and `--chart-radius`, unitless numbers
  (write `12`, not `12px`).
- **Value scales** (`--scale-*`): the colors for "how much" on the CPU core
  bars and the process table's numbers. Load, CPU and rates blend
  `--scale-low` → `--scale-mid` → `--scale-high`; a rate under 1 KB/s is
  `--scale-idle`; CPU time and threads blend `--scale-info-low` →
  `--scale-info-high`; memory uses `--scale-mem-0` to `--scale-mem-7`, one per
  size step. Blends need colors the canvas can read as RGB (hex, `rgb()`,
  names).
- **`color-scheme`**: `dark` by default; a light theme sets `light` so that
  native inputs and scrollbars match.

Any CSS color syntax works (`#abc`, `rgb()`, names, `color-mix()`): the
charts normalise colors before adding transparency.

## What only variables reach

- **Charts** are drawn on canvas, so selectors can't style them. Lines,
  fills, grid lines, ticks, tooltips and chart fonts follow the chart-series,
  `--border-color`, `--text-*`, `--bg-card`, `--font-family` and `--chart-*`
  variables.
- **Colors set inline from JS**: legend dots, drive usage bars, the power
  legend, and status text all follow variables. A selector can still override
  them, but needs `!important`.

## Selectors

The stylesheet uses classes, almost no ids, and no `!important`; fonts are
set only on `body` (everything else inherits). Useful groups when restyling
shapes rather than colors:

- **Cards**: `.card`, `.card-header`, `.card h2`, `.card-header .value`,
  `.chart-container`, `.history-card`.
- **Toolbar**: `.top-controls`, `.toolbar-left`, `.toolbar-right`, `h1`,
  `.poll-label`, `.status`.
- **Buttons**: `.reset-btn`, `.dropdown-trigger`, `.dropdown-trigger-small`,
  `.toggle-btn`, `.history-btn`, `.history-icon-btn`, `.history-close-btn`,
  `.history-refresh-btn`, `.history-settings-btn`, `.mode-toggle-btn`,
  `.refresh-btn`, `.refresh-btn-small`, `.trace-btn`, `.unit-toggle-btn`,
  `.range-toggle`.
- **Popups and tooltips**: `.dropdown-picker` (with `.dropdown-picker-item`),
  `.history-settings-popup`, `.status-tooltip`, `#data-tip-tooltip`,
  `#ping-tooltip`, `.toast`.
- **Inputs**: `input`, `select`, `.dropdown-picker-input`,
  `.history-range-input`, `.history-settings-input`.
- **Tables and bars**: `.table-container`, `.data-table th`, `.data-table td`,
  `.drive-usage-bar-container`, `.power-bar`, `.hw-pill`.

To square every corner, including round dots and pills,
`*, *::before, *::after { border-radius: 0 !important; }` is fine.

## Not themeable

- The favicon.
- The terminal view (`/text`, `tui.py`).
