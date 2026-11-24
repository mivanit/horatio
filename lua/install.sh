#!/bin/sh
# Compatibility entry point for releases before the platform-specific name.
# Older updaters re-execute this path after copying a release into the checkout.
exec sh "$(dirname "$0")/install-openwrt.sh" "$@"
