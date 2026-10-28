# Python tools/dependencies come from uv.lock. Linux tests also need Node.js.
# The explicit OpenWrt target needs Lua 5.1, Nixio, gzip, and timezone data.
UV ?= uv
LUA ?= lua5.1
PYTEST_ARGS ?=

.DEFAULT_GOAL := check
.PHONY: format check-format typing test test-linux test-openwrt test-all check check-all

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

test-all: test-linux test-openwrt

check: check-format typing test

check-all: check-format typing test-all
