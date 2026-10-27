# Python tools/dependencies come from uv.lock; Node.js and Git must be installed.
# Native Lua tests also require Lua 5.1, Nixio, gzip, and timezone data.
UV ?= uv
LUA ?= lua5.1

.DEFAULT_GOAL := check
.PHONY: format check-format typing test test-linux test-openwrt check

format:
	$(UV) run --locked ruff check --fix .
	$(UV) run --locked ruff format .

check-format:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

typing:
	$(UV) run --locked ty check

test: test-linux test-openwrt

# test.py includes the frontend, privacy, installer, and collector fixtures.
# Its live server records history, so always give it a disposable source copy.
test-linux:
	$(UV) run --locked python scripts/test-linux.py

# These use real kernel locks/processes; a missing Nixio is an error, not a skip.
# Override LUA and/or export LUA_CPATH when using a local Lua/Nixio build.
test-openwrt:
	$(LUA) -e 'assert(_VERSION == "Lua 5.1", "Lua 5.1 is required"); require("nixio")'
	TZ=America/New_York $(LUA) -e 'assert(os.date("%z", 1704067200) == "-0500" and os.date("%z", 1719792000) == "-0400", "America/New_York timezone data is required")'
	$(LUA) openwrt/test_handler.lua
	$(LUA) openwrt/test_metrics.lua
	TZ=UTC $(LUA) openwrt/test_history.lua
	TZ=America/New_York $(LUA) openwrt/test_history.lua

check: check-format typing test
