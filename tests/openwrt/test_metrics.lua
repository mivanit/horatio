-- CPU, filesystem and public IP fixtures; no native modules, network or file writes.
-- Usage: lua5.1 tests/openwrt/test_metrics.lua
local script_dir = (arg and arg[0] or ""):match("(.*)[/\\][^/\\]*$") or "."
script_dir = script_dir .. "/../../openwrt"
local metrics = dofile(script_dir .. "/metrics.lua")
local real_open, real_popen = io.open, io.popen
local files, replies, http_calls = {}, {}, 0
local cache_path = "/tmp/dashboard-public-ip"

local function reader(text)
    return {
        read = function(_, format)
            if format == "*l" then return text:match("^([^\n]*)") end
            return text
        end,
        close = function() return true end,
    }
end

io.open = function(path, mode)
    assert(path == "/proc/cpuinfo" or path == "/sys/devices/system/cpu/cpu0/cache/index3/size"
        or path == cache_path, "unexpected file: " .. path)
    if mode == "w" then
        assert(path == cache_path, "unexpected write: " .. path)
        files[path] = ""
        return {
            write = function(_, value) files[path] = files[path] .. value end,
            close = function() return true end,
        }
    end
    return files[path] and reader(files[path]) or nil
end

io.popen = function(command)
    command = command:gsub("^timeout %d+ ", ""):gsub(" 2>/dev/null$", "")
    if command == "command -v timeout" then return reader("/usr/bin/timeout\n") end
    if command == "ip -j addr show" or command == "ip addr show" or command == "tailscale ip" then
        return reader("")
    end
    assert(replies[command] ~= nil, "unexpected command: " .. command)
    http_calls = http_calls + 1
    return reader(replies[command])
end
metrics.init({})

local checks = 0
local function check(condition, label)
    assert(condition, label)
    checks = checks + 1
    print("PASS " .. label)
end

-- Core IDs repeat on each socket and across SMT siblings; field order varies.
local blocks = {}
for socket = 0, 1 do
    for core = 0, 1 do
        for thread = 0, 1 do
            local topology = thread == 0
                and ("physical id : " .. socket .. "\ncore id : " .. core)
                or ("core id : " .. core .. "\nphysical id : " .. socket)
            blocks[#blocks + 1] = "processor : " .. #blocks
                .. "\nmodel name : Example CPU\ncache size : 4096 KB\n" .. topology
        end
    end
end
files["/proc/cpuinfo"] = table.concat(blocks, "\n\n") -- no trailing newline
files["/sys/devices/system/cpu/cpu0/cache/index3/size"] = "32M\n"
local cpu = metrics.cpu_info()
check(cpu.physical_cores == 4 and cpu.logical_cores == 8,
    "two sockets count distinct cores and collapse SMT siblings regardless of key order")
check(cpu.model == "Example CPU" and cpu.cache_size == "4096 KB" and cpu.l3_cache == "32M",
    "CPU model and caches survive topology parsing")

files["/proc/cpuinfo"] = (table.concat(blocks, "\n\n"):gsub("physical id : %d\n?", ""))
cpu = metrics.cpu_info()
check(cpu.physical_cores == 2 and cpu.logical_cores == 8,
    "missing socket IDs retain the existing unique-core fallback")

files["/proc/cpuinfo"] = "Processor : ARM CPU\nprocessor : 0\n\nprocessor : 1\n"
cpu = metrics.cpu_info()
check(cpu.physical_cores == 2 and cpu.logical_cores == 2 and cpu.model == "ARM CPU",
    "CPUs without topology IDs fall back to logical count")
files["/proc/cpuinfo"] = nil
cpu = metrics.cpu_info()
check(cpu.physical_cores == 1 and cpu.logical_cores == 1, "missing cpuinfo keeps safe defaults")

-- BusyBox wraps long device names unless -P requests one row per filesystem.
-- Preserve the real output shapes for both commands so removing -P fails here.
local df_header = "Filesystem           Type       1024-blocks Used Available Capacity Mounted on\n"
local other_drives = "/dev/mmcblk0p1        ext4        1048576 262144 786432 25% /\n"
    .. "tmpfs                tmpfs       524288 0 524288 0% /tmp\n"
replies = {
    ["df -k -T"] = df_header
        .. "/dev/mapper/encrypted-storage\n"
        .. "                     ext4        8388608 4194304 4194304 50% /mnt/data\n"
        .. other_drives,
    ["df -P -k -T"] = df_header
        .. "/dev/mapper/encrypted-storage ext4 8388608 4194304 4194304 50% /mnt/data\n"
        .. other_drives,
    ["readlink -f '/dev/mapper/encrypted-storage'"] = "/dev/dm-0\n",
    ["readlink -f '/dev/mmcblk0p1'"] = "/dev/mmcblk0p1\n",
}
local drives = metrics.fs()
check(#drives == 2, "long and short device names survive df parsing; tmpfs stays excluded")
check(drives[1].device_name == "/dev/dm-0" and drives[1].mnt_point == "/mnt/data"
    and drives[1].size == 8589934592 and drives[1].used == 4294967296
    and drives[1].percent == 50,
    "long device retains identity, mount and exact 8 GiB capacity")
check(drives[2].device_name == "/dev/mmcblk0p1" and drives[2].mnt_point == "/",
    "short device parsing is unchanged")

local function public_ip(first, dual)
    files[cache_path] = nil
    http_calls = 0
    replies = {
        ["wget -qO- -T 3 https://api.ipify.org"] = first,
        ["wget -qO- -T 3 https://api64.ipify.org"] = dual,
    }
    return metrics.ip_info().public
end

local public = public_ip("", "203.0.113.8\n")
check(public.ipv4 == "203.0.113.8" and public.ipv6 == nil and public.error == nil,
    "dual-stack provider supplies IPv4 when the first provider fails")
check(files[cache_path]:match(" 203%.0%.113%.8 %-\n$") ~= nil,
    "fallback IPv4 is cached in the IPv4 field")
local cached = metrics.ip_info().public
check(cached.ipv4 == public.ipv4 and cached.ipv6 == nil and http_calls == 2,
    "cached IPv4 is reused without another public lookup")

public = public_ip("203.0.113.8\n", "2001:db8::8\n")
check(public.ipv4 == "203.0.113.8" and public.ipv6 == "2001:db8::8",
    "dual-stack lookup retains both actual address families")
public = public_ip("203.0.113.8", "203.0.113.9")
check(public.ipv4 == "203.0.113.8" and public.ipv6 == nil,
    "a second IPv4 address does not become IPv6 or overwrite the first")
public = public_ip("", "2001:db8::8")
check(public.ipv4 == nil and public.ipv6 == "2001:db8::8",
    "IPv6-only lookup remains IPv6")
public = public_ip("", "")
check(public.ipv4 == nil and public.ipv6 == nil and public.error == "lookup failed",
    "both providers failing still reports failure")

io.open, io.popen = real_open, real_popen
print("All " .. checks .. " metrics checks passed")
