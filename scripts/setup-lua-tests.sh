#!/bin/sh
# Build the native test dependencies in a fresh directory, without installing them.
# Requires a C toolchain, Make, libcrypt development files, curl, Git, and tar.
set -eu

mkdir -p "${1:?Usage: sh scripts/setup-lua-tests.sh BUILD_DIRECTORY}"
native_dir=$(cd "$1" && pwd)
curl -fL https://www.lua.org/ftp/lua-5.1.5.tar.gz -o "$native_dir/lua.tar.gz"
printf '%s  %s\n' \
    2640fc56a795f29d28ef15e13c34a47e223960b0240e8cb0a82d9b0738695333 \
    "$native_dir/lua.tar.gz" | sha256sum -c -
tar -xzf "$native_dir/lua.tar.gz" -C "$native_dir"
make -C "$native_dir/lua-5.1.5/src" all \
    MYCFLAGS='-fPIC -DLUA_USE_POSIX -DLUA_USE_DLOPEN' \
    MYLIBS=-ldl MYLDFLAGS=-Wl,-E

git init "$native_dir/luci"
git -C "$native_dir/luci" remote add origin https://github.com/openwrt/luci.git
git -C "$native_dir/luci" fetch --depth 1 --filter=blob:none origin \
    75a306df84a020c57a631ef5b4c02ace56d93387
git -C "$native_dir/luci" sparse-checkout set libs/luci-lib-nixio
git -C "$native_dir/luci" checkout --detach FETCH_HEAD
make -C "$native_dir/luci/libs/luci-lib-nixio/src" compile FPIC=-fPIC \
    LUA_CFLAGS="-I$native_dir/lua-5.1.5/src" \
    NIXIO_LDFLAGS='-shared -lm -ldl -lcrypt'
