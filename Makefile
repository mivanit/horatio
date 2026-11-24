# Python tools/dependencies come from uv.lock. Linux tests also need Node.js.
# The explicit OpenWrt target needs Lua 5.1, Nixio, gzip, and timezone data.
UV ?= uv
LUA ?= lua5.1
PYTEST_ARGS ?=

.DEFAULT_GOAL := check
.PHONY: format check-format typing test test-linux test-openwrt test-openwrt-container test-all check check-all demo demo-docs release clean

format:
	$(UV) run --locked ruff check --fix .
	$(UV) run --locked ruff format .

check-format:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

typing:
	$(UV) run --locked ty check

test: test-linux

# Pytest fixtures isolate app imports and live servers from local config/history.
test-linux:
	$(UV) run --locked pytest $(PYTEST_ARGS)

# These use real kernel locks/processes; a missing Nixio is an error, not a skip.
# Override LUA and/or export LUA_CPATH when using a local Lua/Nixio build.
test-openwrt:
	LUA="$(LUA)" $(UV) run --locked pytest tests/openwrt $(PYTEST_ARGS)

# Real OpenWrt in a container (docker; CONTAINER=podman to switch): install this
# checkout, then --update to the newest release. Needs network.
test-openwrt-container:
	scripts/openwrt-container-test.sh

test-all: test-linux test-openwrt

check: check-format typing test

check-all: check-format typing test-all

# The static demo (docs/README.md): the real frontend with a fake in-browser backend.
# demo-docs writes the frontend copies into docs/ (gitignored); demo also serves them.
demo-docs:
	docs/build-demo.sh docs

demo: demo-docs
	python3 -m http.server -d docs 8000

# Cut a release: make release V=1.2.0 (README: Versions and updates). VERSION changes
# only here, so main's VERSION is always the newest release.
release:
	@test -n "$(V)" || { echo "usage: make release V=X.Y.Z"; exit 1; }
	@echo "$(V)" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+' \
		|| { echo "error: V must be X.Y.Z (digits, no leading v), got '$(V)'"; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(V)" >/dev/null \
		|| { echo "error: tag v$(V) already exists"; exit 1; }
	@git diff --quiet HEAD -- VERSION CHANGELOG.md \
		|| { echo "error: VERSION or CHANGELOG.md has uncommitted changes"; exit 1; }
	@awk '/^## \[Unreleased\]$$/ {u=1; next} u && /^## \[/ {exit} u && NF {found=1} END {exit !found}' CHANGELOG.md \
		|| { echo "error: CHANGELOG.md has no '## [Unreleased]' section with entries"; exit 1; }
	echo "$(V)" > VERSION
	sed -i "s/^## \[Unreleased\]$$/## [Unreleased]\n\n## [$(V)] - $$(date +%F)/" CHANGELOG.md
	git commit -m "Release v$(V)" VERSION CHANGELOG.md
	git tag -a "v$(V)" -m "v$(V)"
	@echo "now: git push origin main v$(V)"

dep:
	uv sync

clean: 
	rm -rf __pycache__ .pytest_cache .ruff_cache


