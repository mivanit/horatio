#!/usr/bin/env bash
# Assemble the static demo: the real index.html + static/ from this checkout,
# Chart.js at the version install.sh pins, and docs/demo/fake-api.js, which
# answers the frontend's API calls in the browser. Usage:
#   docs/build-demo.sh [outdir]      (default: _site)
# Only outdir's index.html, static/, themes/ and demo/ are replaced, so outdir can be
# docs/ itself (`make demo`; those copies are gitignored there).
# HORATIO_DEMO_SKIP_CHARTJS=1 skips the Chart.js download (the offline tests).
set -euo pipefail
# Byte-order globbing, so themes/index.json lists names as server.py's sorted() does.
export LC_ALL=C

# Physical paths (pwd -P), so a symlinked spelling of the checkout or docs/ still
# matches the guards below.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUT="${1:-_site}"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -P)"
# The checkout's own index.html and static/ are the sources, never the output.
[ "$OUT" != "$ROOT" ] || { echo "error: outdir is the checkout itself" >&2; exit 1; }
rm -rf "$OUT/index.html" "$OUT/static" "$OUT/themes"
cp "$ROOT/index.html" "$OUT/index.html"
cp -RL "$ROOT/static" "$OUT/static"
# Themes, and the list the picker fetches (server.py serves it from the dir).
mkdir -p "$OUT/themes"
theme_names=""
for theme in "$ROOT"/themes/*.css; do
  [ -f "$theme" ] || continue
  name="$(basename "$theme" .css)"
  [[ "$name" =~ ^[A-Za-z0-9_-]+$ && "$name" != default ]] || continue
  cp -L "$theme" "$OUT/themes/$name.css"
  theme_names="$theme_names${theme_names:+, }\"$name\""
done
printf '[%s]\n' "$theme_names" > "$OUT/themes/index.json"
rm -f "$OUT/static/chart.umd.min.js"
if [ "$OUT" != "$ROOT/docs" ]; then
  rm -rf "$OUT/demo"
  cp -R "$ROOT/docs/demo" "$OUT/demo"
fi

if [ "${HORATIO_DEMO_SKIP_CHARTJS:-}" != 1 ]; then
  # The version and checksum live in install.sh only, so the two cannot drift.
  version="$(sed -n 's/^CHARTJS_VERSION="\(.*\)"$/\1/p' "$ROOT/install.sh")"
  sha256="$(sed -n 's/^CHARTJS_SHA256="\(.*\)"$/\1/p' "$ROOT/install.sh")"
  [ -n "$version" ] && [ -n "$sha256" ] || { echo "error: Chart.js pin not found in install.sh" >&2; exit 1; }
  chartjs="$OUT/static/chart.umd.min.js"
  # A checkout that ran install.sh already has the pinned file; else download it.
  if [ -f "$ROOT/static/chart.umd.min.js" ] \
    && echo "${sha256}  $ROOT/static/chart.umd.min.js" | sha256sum -c --status -; then
    cp "$ROOT/static/chart.umd.min.js" "$chartjs"
  else
    curl -fsSL -o "$chartjs" "https://cdn.jsdelivr.net/npm/chart.js@${version}/dist/chart.umd.min.js"
  fi
  echo "${sha256}  $chartjs" | sha256sum -c --status - \
    || { echo "error: Chart.js checksum mismatch" >&2; exit 1; }
fi

# The site lives under a project path (/horatio/), so asset URLs become relative,
# and the fake backend loads before any frontend script. The API URLs stay
# absolute: fake-api.js intercepts them before they reach the network.
html="$OUT/index.html"
grep -q '"/static/' "$html" || { echo "error: no /static/ references in index.html" >&2; exit 1; }
grep -q '<script src="/static/chart.umd.min.js"></script>' "$html" \
  || { echo "error: Chart.js script tag not found in index.html" >&2; exit 1; }
# Portable sed: no -i, and the inserted tag shares Chart.js's line.
sed \
  -e 's#<script src="/static/chart.umd.min.js"></script>#<script src="demo/fake-api.js"></script>&#' \
  -e 's#"/static/#"static/#g' \
  "$html" > "$html.tmp"
mv "$html.tmp" "$html"
if grep -q '"/static/' "$html"; then
  echo "error: absolute /static/ references left in $html" >&2
  exit 1
fi
echo "Demo built in $OUT"
