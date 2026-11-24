#!/bin/sh
# Build a private Lua/uhttpd runtime for ordinary Linux. No system installation.
# Usage: sh scripts/setup-lua-runtime.sh PREFIX
# Build requirements: C toolchain, Make, CMake, pkg-config, curl, tar,
# sha256sum, and libcrypt development files. TLS terminates at a reverse proxy.
set -eu

[ "$#" -eq 1 ] || { echo "usage: $0 PREFIX" >&2; exit 2; }
case "$1" in ''|/|*[[:space:]]*) echo "runtime prefix must be a non-root path without whitespace" >&2; exit 2;; esac
case "$(basename "$1")" in .|..) echo "runtime prefix must name a dedicated directory" >&2; exit 2;; esac
mkdir -p "$(dirname "$1")"
runtime_parent=$(cd "$(dirname "$1")" && pwd)
runtime_prefix="$runtime_parent/$(basename "$1")"
[ ! -L "$runtime_prefix" ] || { echo "runtime prefix must not be a symlink" >&2; exit 2; }
if [ -e "$runtime_prefix" ]; then
    [ -d "$runtime_prefix" ] && [ -f "$runtime_prefix/SOURCES" ] && [ -x "$runtime_prefix/bin/uhttpd" ] || {
        echo "refusing to replace a directory that is not an installed Lua runtime: $runtime_prefix" >&2
        exit 2
    }
fi
for tool in cc make cmake pkg-config curl tar sha256sum; do
    command -v "$tool" >/dev/null || { echo "missing build tool: $tool" >&2; exit 1; }
done

runtime_work=$(mktemp -d "${TMPDIR:-/tmp}/horatio-lua-build.XXXXXX")
runtime_next=$(mktemp -d "$runtime_parent/.lua-runtime-next.XXXXXX")
runtime_backup=""
runtime_check_pid=""
cleanup() {
    if [ -n "$runtime_check_pid" ]; then
        kill "$runtime_check_pid" 2>/dev/null || true
        wait "$runtime_check_pid" 2>/dev/null || true
    fi
    if [ -n "$runtime_backup" ] && [ -e "$runtime_backup" ] && [ ! -e "$runtime_prefix" ]; then
        mv "$runtime_backup" "$runtime_prefix"
    fi
    rm -rf "$runtime_work" "$runtime_next"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
jobs=${JOBS:-2}
stage="$runtime_work/stage"
mkdir -p "$stage/lib" "$stage/include" "$runtime_next/bin" "$runtime_next/libexec" \
    "$runtime_next/lib/lua/5.1/luci" "$runtime_next/share/lua/5.1" "$runtime_next/share/licenses"

# Revisions and archive digests are fixed: building never follows a branch/tag.
lua_revision=5.1.5
json_revision=41a55cfcedb54d9c1874f2f0eb07b504091d7e37
ubox_revision=a396f38a8568a1d670c240465cd8d24fdfdf6463
uhttpd_revision=93b05e0b65d91c23c5aa8fdde378df1f174ec71d
luci_revision=75a306df84a020c57a631ef5b4c02ace56d93387
fetch() {
    curl --fail --location --retry 2 --silent --show-error "$2" -o "$runtime_work/$1.tar.gz"
    printf '%s  %s\n' "$3" "$runtime_work/$1.tar.gz" | sha256sum -c -
    tar -xzf "$runtime_work/$1.tar.gz" -C "$runtime_work"
}
fetch lua "https://www.lua.org/ftp/lua-$lua_revision.tar.gz" \
    2640fc56a795f29d28ef15e13c34a47e223960b0240e8cb0a82d9b0738695333
fetch json-c "https://codeload.github.com/json-c/json-c/tar.gz/$json_revision" \
    a090046b2d24901eb8dca4d370c28991299e8c7cfb698b44e09ce809b1ff20b4
fetch libubox "https://codeload.github.com/openwrt/libubox/tar.gz/$ubox_revision" \
    ba9c06eecb95aac050140719635073bd997d10d9c111df12f1e787b2940419dd
fetch uhttpd "https://codeload.github.com/openwrt/uhttpd/tar.gz/$uhttpd_revision" \
    02bc6a3fe2c5a6bf0ff9f375175849f5fc2e05e9770f2c195ac3f42a05a6d11c
fetch luci "https://codeload.github.com/openwrt/luci/tar.gz/$luci_revision" \
    9365b8ce335bdc5b41ac12155bbb85a4161b8835a992c4cda44ad73b4660e2d5

lua_src="$runtime_work/lua-$lua_revision"
luci_src="$runtime_work/luci-$luci_revision/libs"
make -C "$lua_src/src" -j"$jobs" all \
    MYCFLAGS='-fPIC -DLUA_USE_POSIX -DLUA_USE_DLOPEN' MYLIBS=-ldl MYLDFLAGS=-Wl,-E
cp "$lua_src/src/"*.h "$stage/include/"
cc -shared -Wl,-soname,liblua5.1.so -o "$stage/lib/liblua5.1.so" \
    -Wl,--whole-archive "$lua_src/src/liblua.a" -Wl,--no-whole-archive -lm -ldl
cp "$lua_src/src/lua" "$runtime_next/libexec/lua"

cmake -S "$runtime_work/json-c-$json_revision" -B "$runtime_work/json-build" \
    -DCMAKE_INSTALL_PREFIX="$stage" -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DBUILD_STATIC_LIBS=OFF \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build "$runtime_work/json-build" -j "$jobs"
cmake --install "$runtime_work/json-build"
export PKG_CONFIG_PATH="$stage/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
cmake -S "$runtime_work/libubox-$ubox_revision" -B "$runtime_work/ubox-build" \
    -DCMAKE_INSTALL_PREFIX="$stage" -DCMAKE_PREFIX_PATH="$stage" \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_LUA=OFF -DBUILD_EXAMPLES=OFF
cmake --build "$runtime_work/ubox-build" -j "$jobs"
cmake --install "$runtime_work/ubox-build"
cmake -S "$runtime_work/uhttpd-$uhttpd_revision" -B "$runtime_work/uhttpd-build" \
    -DCMAKE_INSTALL_PREFIX="$stage" -DCMAKE_PREFIX_PATH="$stage" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS="-I$stage/include" \
    -DTLS_SUPPORT=OFF -DUBUS_SUPPORT=OFF -DUCODE_SUPPORT=OFF -DLUA_SUPPORT=ON \
    -DLUA_CFLAGS="-I$stage/include" -DLUA_LIBS="$stage/lib/liblua5.1.so"
cmake --build "$runtime_work/uhttpd-build" -j "$jobs"
cmake --install "$runtime_work/uhttpd-build"

make -C "$luci_src/luci-lib-nixio/src" -j"$jobs" compile FPIC=-fPIC \
    LUA_CFLAGS="-I$stage/include" NIXIO_LDFLAGS="-shared -L$stage/lib -llua5.1 -lm -ldl -lcrypt"
# LuCI also targets OpenWrt's patched Lua with a separate integer type. Stock
# 5.1 has only doubles; jsonc's following numeric branch handles integral values.
printf '#define lua_isinteger(L, index) 0\n' > "$runtime_work/lua-compat.h"
make -C "$luci_src/luci-lib-jsonc/src" -j"$jobs" compile FPIC=-fPIC \
    LUA_CFLAGS="-I$stage/include" JSONC_CFLAGS="-std=gnu99 -I$stage/include -include $runtime_work/lua-compat.h" \
    JSONC_LDFLAGS="-L$stage/lib -llua5.1 -lm -ljson-c"
cp -a "$stage/lib/"*.so* "$runtime_next/lib/"
cp "$stage/bin/uhttpd" "$runtime_next/libexec/uhttpd"
cp "$luci_src/luci-lib-nixio/src/nixio.so" "$runtime_next/lib/lua/5.1/"
cp "$luci_src/luci-lib-jsonc/src/jsonc.so" "$runtime_next/lib/lua/5.1/luci/"
cp -R "$luci_src/luci-lib-nixio/root/usr/lib/lua/". "$runtime_next/share/lua/5.1/"

# Resolve the prefix at launch, so the verified staging tree can be renamed.
for program in lua uhttpd; do
    cat > "$runtime_next/bin/$program" <<'SH'
#!/bin/sh
runtime_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export LD_LIBRARY_PATH="$runtime_dir/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LUA_PATH="$runtime_dir/share/lua/5.1/?.lua;$runtime_dir/share/lua/5.1/?/init.lua;;"
export LUA_CPATH="$runtime_dir/lib/lua/5.1/?.so;;"
exec "$runtime_dir/libexec/$(basename -- "$0")" "$@"
SH
    chmod 755 "$runtime_next/bin/$program"
done
cp "$lua_src/COPYRIGHT" "$runtime_next/share/licenses/lua"
cp "$runtime_work/json-c-$json_revision/COPYING" "$runtime_next/share/licenses/json-c"
cp "$luci_src/luci-lib-nixio/LICENSE" "$runtime_next/share/licenses/nixio"
printf 'Lua %s\njson-c %s\nlibubox %s\nuhttpd %s\nLuCI %s\n' \
    "$lua_revision" "$json_revision" "$ubox_revision" "$uhttpd_revision" "$luci_revision" \
    > "$runtime_next/SOURCES"

"$runtime_next/bin/lua" -e 'assert(_VERSION == "Lua 5.1"); assert(require("nixio").getpid()); assert(require("nixio.fs").stat("/")); local j=require("luci.jsonc"); local v=j.parse(j.stringify({ok=true, bytes=8589934592})); assert(v.ok and v.bytes == 8589934592)'
printf 'function handle_request(env) end\n' > "$runtime_work/smoke.lua"
"$runtime_next/bin/uhttpd" -h "$runtime_next" -p 127.0.0.1:0 -D -S -l /api -L "$runtime_work/smoke.lua" \
    -f > "$runtime_work/uhttpd-check.log" 2>&1 &
runtime_check_pid=$!
sleep 1
if ! kill -0 "$runtime_check_pid" 2>/dev/null; then
    cat "$runtime_work/uhttpd-check.log" >&2
    wait "$runtime_check_pid" || true
    exit 1
fi
kill "$runtime_check_pid"
wait "$runtime_check_pid" || true
runtime_check_pid=""

# Keep the installed runtime intact until all builds and loading checks pass.
if [ -e "$runtime_prefix" ]; then
    runtime_backup=$(mktemp -d "$runtime_parent/.lua-runtime-old.XXXXXX")
    rmdir "$runtime_backup"
    mv "$runtime_prefix" "$runtime_backup"
fi
mv "$runtime_next" "$runtime_prefix"
if [ -n "$runtime_backup" ]; then rm -rf "$runtime_backup"; runtime_backup=""; fi
echo "Lua runtime ready: $runtime_prefix/bin/uhttpd"
