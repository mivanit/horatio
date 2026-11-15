-- CPU, rate, sensor, filesystem, ping and public IP fixtures; no native modules,
-- network, processes or file writes: every file and command is answered here.
-- Usage: lua5.1 tests/openwrt/test_metrics.lua
local script_dir = (arg and arg[0] or ""):match("(.*)[/\\][^/\\]*$") or "."
script_dir = script_dir .. "/../../openwrt"
local metrics = dofile(script_dir .. "/metrics.lua")
local real_open, real_popen, real_rename, real_remove = io.open, io.popen, os.rename, os.remove
local files, replies, http_calls = {}, {}, 0
local cache_path = "/tmp/dashboard-public-ip"
local rates_path = "/tmp/dashboard-rates"
local readable = {
    ["/proc/cpuinfo"] = true, ["/sys/devices/system/cpu/cpu0/cache/index3/size"] = true,
    [cache_path] = true, [rates_path] = true,
    ["/proc/stat"] = true, ["/proc/net/dev"] = true, ["/proc/uptime"] = true,
    ["/proc/sys/kernel/hostname"] = true, ["/proc/sys/kernel/osrelease"] = true,
    ["/etc/openwrt_release"] = true, ["/etc/os-release"] = true,
}

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
    if mode == "w" then
        -- The rates file is written under a name of the writer's own, then renamed.
        assert(path == cache_path or path == rates_path .. ".42", "unexpected write: " .. path)
        files[path] = ""
        return {
            write = function(_, value) files[path] = files[path] .. value; return true end,
            close = function() return true end,
        }
    end
    assert(readable[path] or path:match("^/sys/class/thermal/") or path:match("^/sys/class/hwmon/"),
        "unexpected file: " .. path)
    return files[path] and reader(files[path]) or nil
end
os.rename = function(from, to)
    assert(from == rates_path .. ".42" and to == rates_path, "unexpected rename: " .. from)
    files[to], files[from] = files[from], nil
    return true
end
os.remove = function(path) error("unexpected remove: " .. path) end

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
local function check(condition, label, detail)
    assert(condition, detail and (label .. " (" .. detail .. ")") or label)
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
    .. "/dev/sda1            ext4        1048576 0 1048576 0% /mnt/gone\n"
replies = {
    ["df -k -T"] = df_header
        .. "/dev/mapper/encrypted-storage\n"
        .. "                     ext4        8388608 4194304 4194304 50% /mnt/data\n"
        .. other_drives,
    ["df -P -k -T"] = df_header
        .. "/dev/mapper/encrypted-storage ext4 8388608 4194304 4194304 50% /mnt/data\n"
        .. other_drives,
}
-- metrics.fs requires nixio at call time; a fake keyed by mount stands in for it.
-- Unknown mounts fail the way nixio does: nil, errno, message.
-- realpath resolves the device-mapper symlink; no process is started per row
-- (a `readlink` would be an unexpected command above).
package.loaded.nixio = {fs = {statvfs = function(mount)
    local st = ({["/mnt/data"] = {files = 1000, ffree = 750}, ["/"] = {files = 0, ffree = 0}})[mount]
    if st then return st end
    return nil, 2, "No such file or directory"
end, realpath = function(path)
    if path == "/dev/sda1" then return nil, 2, "No such file or directory" end  -- unplugged
    return ({["/dev/mapper/encrypted-storage"] = "/dev/dm-0"})[path] or path
end}}
local drives = metrics.fs()
package.loaded.nixio = nil
check(#drives == 3, "long and short device names survive df parsing; tmpfs stays excluded")
check(drives[1].device_name == "/dev/dm-0" and drives[1].mnt_point == "/mnt/data"
    and drives[1].size == 8589934592 and drives[1].used == 4294967296
    and drives[1].percent == 50,
    "long device retains identity, mount and exact 8 GiB capacity")
check(drives[2].device_name == "/dev/mmcblk0p1" and drives[2].mnt_point == "/",
    "short device parsing is unchanged")
check(drives[1].inodes_used == 250 and drives[1].inodes_percent == 25,
    "statvfs inode counts become inodes_used and inodes_percent")
check(drives[2].inodes_used == nil and drives[2].inodes_percent == nil,
    "a filesystem with no inode table (files == 0) reports no inode usage")
check(drives[3].mnt_point == "/mnt/gone" and drives[3].inodes_used == nil and drives[3].inodes_percent == nil,
    "a failed statvfs leaves the row without inode usage")

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

-- Rates: measured from the previous request's counters, with no wait inside the
-- request. `sleeps` counts the 1 s windows; the fake sleep is what moves time on.
local sleeps, on_sleep = 0, nil
local globbed = {}
package.loaded.nixio = {
    nanosleep = function(seconds)
        assert(seconds == 1, "window length")
        sleeps = sleeps + 1
        if on_sleep then on_sleep() end
    end,
    getpid = function() return 42 end,
    fs = {glob = function(pattern)
        local list, i = globbed[pattern], 0
        assert(list, "unexpected glob: " .. pattern)
        return function() i = i + 1; return list[i] end, #list
    end},
}
local function counters(uptime, busy, idle, rx, tx)
    files["/proc/uptime"] = string.format("%.2f 0.00\n", uptime)
    files["/proc/stat"] = string.format(
        "cpu  %d 0 0 %d 0 0 0\ncpu0 %d 0 0 %d 0 0 0\ncpu1 0 0 0 %d 0 0 0\nintr 7 8\n",
        busy, idle, busy, math.floor(idle / 2), idle - math.floor(idle / 2))
    files["/proc/net/dev"] = "Inter-|   Receive\n face |bytes\n"
        .. "    lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n"
        .. string.format("  eth0: %d 0 0 0 0 0 0 0 %d 0 0 0 0 0 0 0\n", rx, tx)
end

counters(100, 1000, 9000, 0, 0)
on_sleep = function() counters(101, 1050, 9050, 3000, 500) end
local cpu_rate = metrics.cpu()
check(sleeps == 1 and cpu_rate.total == 50, "the first request measures one 1 s window",
    "sleeps=" .. sleeps .. " total=" .. tostring(cpu_rate.total))
check(files[rates_path] ~= nil and files[rates_path]:match("\nend\n$") ~= nil,
    "the measurement is stored for the requests that follow")
local cores, net = metrics.percpu(), metrics.network()
check(sleeps == 1 and #cores == 2 and cores[1].cpu_number == 0 and cores[1].total == 66.7
    and cores[2].total == 0, "per-core rates of the same poll reuse it without waiting")
check(#net == 1 and net[1].interface_name == "eth0" and net[1].bytes_recv_rate_per_sec == 3000
    and net[1].bytes_sent_rate_per_sec == 500, "network rates of the same poll reuse it; lo is left out")

on_sleep = function() error("a poll with recent counters must not wait") end
counters(103, 1150, 9150, 7000, 1500)
cpu_rate = metrics.cpu()
check(cpu_rate.total == 50, "the next poll measures from the stored counters to now")
net = metrics.network()
check(net[1].bytes_recv_rate_per_sec == 2000 and net[1].bytes_sent_rate_per_sec == 500,
    "network rates divide by the time between polls (2 s), not by a fixed window")
counters(103.5, 1200, 9200, 7000, 1500)
check(metrics.cpu().total == 50 and metrics.network()[1].bytes_recv_rate_per_sec == 2000,
    "a request within a second gets the stored rates, not a half-second window")

local function falls_back(label)
    local before = sleeps
    local total = metrics.cpu().total
    check(sleeps == before + 1 and total == 25, label, "sleeps=" .. (sleeps - before) .. " total=" .. tostring(total))
end
counters(200, 5000, 20000, 9000, 9000)   -- the dashboard was closed for a while
on_sleep = function() counters(201, 5025, 20075, 9000, 9000) end
falls_back("counters older than the limit are not measured against")
counters(5, 10, 40, 0, 0)                 -- rebooted: uptime and counters start over
on_sleep = function() counters(6, 35, 115, 0, 0) end
falls_back("a reboot (uptime behind the stored time) starts a new window")
counters(8, 20, 80, 0, 0)                 -- counters behind the stored ones
on_sleep = function() counters(9, 45, 155, 0, 0) end
falls_back("counters that went backwards start a new window")
files[rates_path] = files[rates_path]:gsub("end\n$", "")
counters(11, 100, 300, 0, 0)
on_sleep = function() counters(12, 125, 375, 0, 0) end
falls_back("a half-written rates file reads as missing")
local before = sleeps
counters(13, 200, 600, 0, 0)
on_sleep = function() counters(14, 225, 675, 0, 0) end
check(metrics.sample().cpu.total == 25 and sleeps == before + 1,
    "a history sample always measures its own 1 s window")
on_sleep = nil

-- Sensors and system info are read from /sys and /proc, with no `ls` or `uname`.
replies = {}
globbed = {
    ["/sys/class/thermal/thermal_zone*"] = {"/sys/class/thermal/thermal_zone1", "/sys/class/thermal/thermal_zone0"},
    ["/sys/class/hwmon/hwmon*/temp*_input"] = {
        "/sys/class/hwmon/hwmon1/temp2_input", "/sys/class/hwmon/hwmon0/temp1_input",
        "/sys/class/hwmon/hwmon1/temp1_input"},
}
files["/sys/class/thermal/thermal_zone0/temp"] = "41500\n"
files["/sys/class/thermal/thermal_zone0/type"] = "cpu-thermal\n"
files["/sys/class/thermal/thermal_zone1/temp"] = "0\n"   -- an unpopulated zone
files["/sys/class/hwmon/hwmon0/temp1_input"] = "52000\n"
files["/sys/class/hwmon/hwmon0/name"] = "mt7915_phy0\n"
files["/sys/class/hwmon/hwmon1/temp1_input"] = "38250\n"
files["/sys/class/hwmon/hwmon1/temp1_label"] = "board\n"
local sensors = metrics.sensors()
check(#sensors == 3 and sensors[1].label == "cpu-thermal" and sensors[1].value == 41.5
    and sensors[2].label == "mt7915_phy0 1" and sensors[2].value == 52
    and sensors[3].label == "board" and sensors[3].value == 38.3,
    "thermal zones and hwmon inputs are listed in order, labelled, unread ones skipped")
globbed = {["/sys/class/thermal/thermal_zone*"] = {}, ["/sys/class/hwmon/hwmon*/temp*_input"] = {}}
check(#metrics.sensors() == 0, "a router with no sensors reports none")
package.loaded.nixio = nil

files["/proc/sys/kernel/hostname"] = "router\n"
files["/proc/sys/kernel/osrelease"] = "6.6.73\n"
files["/etc/openwrt_release"] = "DISTRIB_DESCRIPTION='OpenWrt 24.10.0'\n"
local system = metrics.system_info()
check(system.hostname == "router" and system.os_version == "6.6.73"
    and system.linux_distro == "OpenWrt 24.10.0", "system info comes from /proc and /etc alone")

-- Pings run at once, each under the timeout guard; an unsafe address is never run.
local started, read_back = {}, {}
local mock_popen = io.popen
io.popen = function(command)
    if command:match("ping") then
        assert(command:match("^timeout 5 ping %-c 1 %-W 2 "), "ping runs under the guard: " .. command)
        assert(#read_back == 0, "every ping starts before any is waited for")
        started[#started + 1] = command
        local pipe = mock_popen(command)
        local read = pipe.read
        pipe.read = function(...) read_back[#read_back + 1] = command; return read(...) end
        return pipe
    end
    return mock_popen(command)
end
replies = {
    ["ping -c 1 -W 2 192.0.2.1"] = "64 bytes from 192.0.2.1: seq=0 ttl=64 time=1.234 ms\n",
    ["ping -c 1 -W 2 gateway.example"] = "",
}
metrics.init({ping_targets = {{address = "192.0.2.1"}, {address = "gateway.example"}, {address = "-c 9; reboot"}}})
local pings = metrics.ping()
check(#started == 2 and #read_back == 2, "one ping per safe target, all started together")
check(pings.targets["192.0.2.1"].latency_ms == 1.23 and pings.targets["gateway.example"].error == "timeout"
    and pings.targets["-c 9; reboot"] == nil, "replies map to latency, silence to a timeout")
metrics.init({})
check(metrics.ping().disabled == true, "no targets configured disables ping")

io.open, io.popen, os.rename, os.remove = real_open, real_popen, real_rename, real_remove
print("All " .. checks .. " metrics checks passed")
