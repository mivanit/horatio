#!/usr/bin/env bash
# Cut a release (README: Versions and updates):
#   scripts/release.sh X.Y.Z
# Sets VERSION and pyproject.toml's version, relocks, turns CHANGELOG.md's
# Unreleased section into "## [X.Y.Z] - <today>", commits "Release vX.Y.Z" and
# tags vX.Y.Z. When VERSION already says X.Y.Z with a dated section (the first
# release), it re-dates that section to today (committing "Release vX.Y.Z" if
# the date changed) and tags. Pushing is left to you; the pushed tag makes CI publish
# the GitHub release once the checks pass.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
die() { echo "Error: $*" >&2; exit 1; }

new="${1:-}"
new="${new#v}"
[[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || die "usage: scripts/release.sh X.Y.Z"
[ "$(git symbolic-ref --quiet --short HEAD)" = main ] || die "release from main."
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "commit or stash local changes first."
! git rev-parse --quiet --verify "refs/tags/v$new" > /dev/null || die "v$new already exists."
old="$(cat VERSION)"

if [ "$new" != "$old" ]; then
    [ "$(printf '%s\n%s\n' "${old%%-*}" "${new%%-*}" | sort -V | tail -n 1)" = "${new%%-*}" ] \
        || die "v$new is older than the current v$old."
    # The release notes are the Unreleased section: refuse an empty one.
    awk '/^## \[Unreleased\]/ { on = 1; next } on && /^## \[/ { exit } on && NF { found = 1 }
         END { exit !found }' CHANGELOG.md \
        || die "CHANGELOG.md's Unreleased section is empty: describe the release there first."
    printf '%s\n' "$new" > VERSION
    sed -i "s/^version = \"$old\"\$/version = \"$new\"/" pyproject.toml
    uv lock --quiet
    sed -i "s/^## \[Unreleased\]\$/## [Unreleased]\n\n## [$new] - $(date +%F)/" CHANGELOG.md
    uv run --locked pytest tests/unit/test_version.py -q
    git add VERSION pyproject.toml uv.lock CHANGELOG.md
    git commit --quiet -m "Release v$new"
else
    # The section was dated when it was written; the release is dated the day it
    # is tagged. Portable sed (no -i), dots in the version matched literally.
    today="$(date +%F)"
    grep -q "^## \[${new//./\\.}\] - " CHANGELOG.md || die "CHANGELOG.md has no dated [$new] section."
    if ! grep -q "^## \[${new//./\\.}\] - $today\$" CHANGELOG.md; then
        sed "s/^## \[${new//./\\.}\] - .*\$/## [$new] - $today/" CHANGELOG.md > CHANGELOG.md.tmp
        mv CHANGELOG.md.tmp CHANGELOG.md
        uv run --locked pytest tests/unit/test_version.py -q
        git add CHANGELOG.md
        git commit --quiet -m "Release v$new"
    else
        uv run --locked pytest tests/unit/test_version.py -q
    fi
fi
git tag -a "v$new" -m "v$new"
echo "Tagged v$new. Publish it with:"
echo "  git push origin main v$new"
