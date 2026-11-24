#!/bin/sh
# Compatibility entry point for installations made before the Lua directory rename.
# Older updaters re-execute this path after copying a release into the checkout.
exec sh "$(dirname "$0")/../lua/install-openwrt.sh" "$@"
