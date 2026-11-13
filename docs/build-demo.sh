#!/usr/bin/env bash
# Assemble the static demo: the real index.html + static/ from this checkout,
# Chart.js at the version install.sh pins, and docs/demo/fake-api.js, which
# answers the frontend's API calls in the browser. Usage:
#   docs/build-demo.sh [outdir]      (default: _site)
# HORATIO_DEMO_SKIP_CHARTJS=1 skips the Chart.js download (the offline tests).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-_site}"

rm -rf "$OUT"
mkdir -p "$OUT"
cp "$ROOT/index.html" "$OUT/index.html"
cp -RL "$ROOT/static" "$OUT/static"
rm -f "$OUT/static/chart.umd.min.js"
cp -R "$ROOT/docs/demo" "$OUT/demo"

if [ "${HORATIO_DEMO_SKIP_CHARTJS:-}" != 1 ]; then
  # The version and checksum live in install.sh only, so the two cannot drift.
  version="$(sed -n 's/^CHARTJS_VERSION="\(.*\)"$/\1/p' "$ROOT/install.sh")"
  sha256="$(sed -n 's/^CHARTJS_SHA256="\(.*\)"$/\1/p' "$ROOT/install.sh")"
  [ -n "$version" ] && [ -n "$sha256" ] || { echo "error: Chart.js pin not found in install.sh" >&2; exit 1; }
  curl -fsSL -o "$OUT/static/chart.umd.min.js" \
    "https://cdn.jsdelivr.net/npm/chart.js@${version}/dist/chart.umd.min.js"
  echo "${sha256}  $OUT/static/chart.umd.min.js" | sha256sum -c --status - \
    || { echo "error: Chart.js checksum mismatch" >&2; exit 1; }
fi

# The site lives under a project path (/horatio/), so asset URLs become relative,
# and the fake backend loads before any frontend script. The API URLs stay
# absolute: fake-api.js intercepts them before they reach the network.
html="$OUT/index.html"
grep -q '"/static/' "$html" || { echo "error: no /static/ references in index.html" >&2; exit 1; }
grep -q '<script src="/static/chart.umd.min.js"></script>' "$html" \
  || { echo "error: Chart.js script tag not found in index.html" >&2; exit 1; }
sed -i \
  -e 's#<script src="/static/chart.umd.min.js"></script>#<script src="demo/fake-api.js"></script>\n  &#' \
  -e 's#"/static/#"static/#g' \
  "$html"
if grep -q '"/static/' "$html"; then
  echo "error: absolute /static/ references left in $html" >&2
  exit 1
fi
echo "Demo built in $OUT"
