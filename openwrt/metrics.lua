-- System metrics from /proc and /sys for OpenWrt.
-- No external dependencies — reads Linux virtual filesystems directly.

local M = {}

local config = {}

-- uhttpd forks a child per request and the child exits afterwards, so no
-- module-level state survives between requests. What must outlive a request
-- lives in tmpfs files: the counters rate metrics are measured against (see
-- M.rates) and the public-IP cache.
local PUBLIC_IP_FILE = "/tmp/dashboard-public-ip"
local PUBLIC_IP_TTL = 600     -- 10 minutes
local RATES_FILE = "/tmp/dashboard-rates"
-- Younger than this (s): reuse the stored rates. Well under the UI's fastest
-- poll (1 s), so only the requests of one poll share a measurement.
local RATES_FRESH = 0.5
-- Older than this (s): too old to measure against. Over twice the UI's slowest
-- poll (30 s), so no poll interval falls back to waiting; a dashboard reopened
-- after longer shows the last second, not an average over its absence.
local RATES_STALE = 65

local has_timeout = false

-- state_dir: where the tmpfs state files go (tests point it elsewhere).
function M.init(cfg, state_dir)
    config = cfg or {}
    if state_dir then RATES_FILE = state_dir .. "/dashboard-rates" end
    -- Check if 'timeout' command is available for guarding blocking commands
    local p = io.popen("command -v timeout 2>/dev/null", "r")
    if p then
        local result = p:read("*a")
        p:close()
        has_timeout = (result and result:match("%S") ~= nil)
    end
    if not has_timeout then
        io.stderr:write("[dashboard] WARNING: 'timeout' command not found — hung commands (df, tailscale) will block all requests\n")
    end
end

------------------------------------------------------------------------
-- Helpers
------------------------------------------------------------------------

local function read_file(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local content = f:read("*a")
    f:close()
    return content
end

local function read_line(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local line = f:read("*l")
    f:close()
    return line
end

-- Run a command with pre-validated arguments only.
-- Never pass user-controlled strings directly — use validate_addr() first.
--
-- stderr is discarded deliberately, not by oversight: on Lua 5.1 (what OpenWrt
-- ships) io.popen's close() returns a bare boolean with no exit status, and
-- merging stderr via 2>&1 would corrupt the text every caller parses. There is
-- therefore no way to tell "tool failed" from "tool printed nothing" here, so
-- CALLERS MUST TREAT EMPTY OUTPUT AS FAILURE rather than as a real zero.
local function run_cmd(cmd)
    local p = io.popen(cmd .. " 2>/dev/null", "r")
    if not p then return nil end
    local output = p:read("*a")
    p:close()
    return output
end

-- Run a command with a timeout guard (falls back to no timeout if unavailable)
local function run_cmd_timeout(seconds, cmd)
    if has_timeout then
        return run_cmd("timeout " .. seconds .. " " .. cmd)
    end
    return run_cmd(cmd)
end

-- Run several commands at once: all are started before any output is read, so
-- the wait is the slowest one's, not their sum. Returns outputs in order (nil
-- where a command could not be started). `seconds` is the timeout guard, as
-- run_cmd_timeout; nil for none.
local function run_cmds(seconds, cmds)
    local prefix = (seconds and has_timeout) and ("timeout " .. seconds .. " ") or ""
    local pipes, outputs = {}, {}
    for i, cmd in ipairs(cmds) do
        pipes[i] = io.popen(prefix .. cmd .. " 2>/dev/null", "r") or false
    end
    for i, p in ipairs(pipes) do
        if p then
            outputs[i] = p:read("*a")
            p:close()
        end
    end
    return outputs
end

-- Validate that a string is a safe hostname/IP (alphanumeric, dots, hyphens, colons)
local function validate_addr(s)
    if not s or s == "" then return nil end
    if s:sub(1, 1) == "-" then return nil end  -- never let an address pose as a command flag
    if s:match("^[%w%.%-:]+$") then return s end
    return nil
end

local function round(n, decimals)
    decimals = decimals or 2
    local mult = 10 ^ decimals
    return math.floor(n * mult + 0.5) / mult
end

local function get_time()
    local content = read_file("/proc/uptime")
    if content then return tonumber(content:match("^(%S+)")) end
    return os.time()
end

-- Parse /proc/meminfo into {MemTotal=bytes, ...}
local function parse_meminfo()
    local content = read_file("/proc/meminfo")
    if not content then return {} end
    local info = {}
    for key, val in content:gmatch("(%S+):%s+(%d+)") do
        info[key] = tonumber(val) * 1024  -- /proc/meminfo reports kB
    end
    return info
end

------------------------------------------------------------------------
-- Snapshot endpoints (no state needed)
------------------------------------------------------------------------

function M.load()
    local content = read_line("/proc/loadavg")
    if not content then
        return {min1 = 0, min5 = 0, min15 = 0, cpucore = 1}
    end
    local m1, m5, m15 = content:match("^(%S+)%s+(%S+)%s+(%S+)")
    -- Count CPU cores
    local cpucore = 0
    local stat = read_file("/proc/stat")
    if stat then
        for _ in stat:gmatch("\ncpu%d+") do cpucore = cpucore + 1 end
    end
    if cpucore == 0 then cpucore = 1 end
    return {
        min1 = tonumber(m1) or 0,
        min5 = tonumber(m5) or 0,
        min15 = tonumber(m15) or 0,
        cpucore = cpucore
    }
end

function M.uptime()
    local content = read_line("/proc/uptime")
    if not content then return "unknown" end
    local secs = math.floor(tonumber(content:match("^(%S+)")) or 0)
    local days = math.floor(secs / 86400)
    local hours = math.floor((secs % 86400) / 3600)
    local mins = math.floor((secs % 3600) / 60)
    local s = secs % 60
    -- The same text as the Linux backend, whose /api/4/uptime is Glances
    -- printing a Python timedelta: "20:14:03", "1 day, 20:14:03",
    -- "2 days, 0:05:00". One format, so a reader of the API needs one parser.
    local clock = string.format("%d:%02d:%02d", hours, mins, s)
    if days > 0 then
        return string.format("%d day%s, %s", days, days == 1 and "" or "s", clock)
    end
    return clock
end

function M.system_info()
    local hostname = (read_line("/proc/sys/kernel/hostname") or "openwrt"):gsub("%s+$", "")
    -- What `uname -r` prints, without starting a process.
    local uname = (read_line("/proc/sys/kernel/osrelease") or ""):gsub("%s+$", "")

    local distro = ""
    local release = read_file("/etc/openwrt_release")
    if release then
        distro = release:match("DISTRIB_DESCRIPTION='(.-)'")
            or release:match('DISTRIB_DESCRIPTION="(.-)"') or ""
    end
    if distro == "" then
        local os_release = read_file("/etc/os-release")
        if os_release then
            distro = os_release:match('PRETTY_NAME="(.-)"') or ""
        end
    end

    return {
        hostname = hostname,
        os_name = "Linux",
        os_version = uname,
        linux_distro = distro
    }
end

function M.cpu_info()
    local content = read_file("/proc/cpuinfo")
    if not content then
        return {model = "", physical_cores = 1, logical_cores = 1, cache_size = "", l3_cache = ""}
    end

    local model = ""
    local physical_ids = {}
    local logical = 0
    local cache_size = ""

    -- Core IDs repeat across sockets; collect both IDs before counting a block.
    for block in (content .. "\n\n"):gmatch("(.-)\n%s*\n") do
        local socket, core = "", nil
        for line in block:gmatch("[^\n]+") do
            local key, val = line:match("^(.-)%s*:%s*(.+)")
            if key then
                key = key:gsub("%s+$", "")
                if (key == "model name" or key == "Processor") and model == "" then
                    model = val
                elseif key == "physical id" then
                    socket = val
                elseif key == "core id" then
                    core = val
                elseif key == "processor" then
                    logical = logical + 1
                elseif key == "cache size" and cache_size == "" then
                    cache_size = val
                end
            end
        end
        if core then physical_ids[socket .. ":" .. core] = true end
    end

    if logical == 0 then logical = 1 end
    local physical = 0
    for _ in pairs(physical_ids) do physical = physical + 1 end
    if physical == 0 then physical = logical end

    return {
        model = model,
        physical_cores = physical,
        logical_cores = logical,
        cache_size = cache_size,
        l3_cache = read_line("/sys/devices/system/cpu/cpu0/cache/index3/size") or ""
    }
end

function M.mem()
    local info = parse_meminfo()
    local total = info.MemTotal or 0
    local available = info.MemAvailable
        or ((info.MemFree or 0) + (info.Buffers or 0) + (info.Cached or 0))
    local used = total - available
    return {
        used = used,
        total = total,
        percent = total > 0 and round(used / total * 100, 1) or 0
    }
end

function M.memswap()
    local info = parse_meminfo()
    local total = info.SwapTotal or 0
    local used = total - (info.SwapFree or 0)
    return {
        used = used,
        total = total,
        percent = total > 0 and round(used / total * 100, 1) or 0
    }
end

------------------------------------------------------------------------
-- Rate-based endpoints: the change in /proc counters between two snapshots.
------------------------------------------------------------------------

-- Parse /proc/stat into {all = {idle, total}, cores = {{idle, total}, ...}}.
-- The label is stripped before scanning for numbers so "cpu3" does not
-- contribute a spurious leading field.
local function stat_snapshot()
    local content = read_file("/proc/stat")
    if not content then return nil end
    local snap = {cores = {}}
    for label, rest in content:gmatch("(cpu%d*)%s+([^\n]+)") do
        local fields = {}
        for v in rest:gmatch("%d+") do fields[#fields + 1] = tonumber(v) end
        local idle = (fields[4] or 0) + (fields[5] or 0)
        local total = 0
        for _, v in ipairs(fields) do total = total + v end
        local entry = {idle = idle, total = total}
        if label == "cpu" then snap.all = entry
        else snap.cores[#snap.cores + 1] = entry end
    end
    return snap
end

local function busy_pct(a, b)
    if not a or not b then return 0 end
    local d_total = b.total - a.total
    local d_idle = b.idle - a.idle
    if d_total <= 0 then return 0 end
    return round((d_total - d_idle) / d_total * 100, 1)
end

-- Parse /proc/net/dev into {time = uptime_secs, ifaces = {{name, rx, tx}, ...}}
local function net_snapshot()
    local content = read_file("/proc/net/dev")
    if not content then return nil end
    local snap = {time = get_time(), ifaces = {}}
    for line in content:gmatch("[^\n]+") do
        local name, rest = line:match("^%s*(%S+):%s*(.*)")
        if name and name ~= "lo" then
            local fields = {}
            for v in rest:gmatch("%d+") do fields[#fields + 1] = tonumber(v) end
            snap.ifaces[#snap.ifaces + 1] = {name = name, rx = fields[1] or 0, tx = fields[9] or 0}
        end
    end
    return snap
end

-- Rates between two snapshot pairs: {cpu = {total = pct, cores = {pct, ...}},
-- network = {iface, ...}}, network entries shaped as /4/network serves them.
local function measure(stat_a, net_a, stat_b, net_b)
    local cpu = {total = 0, cores = {}}
    if stat_a and stat_b then
        cpu.total = busy_pct(stat_a.all, stat_b.all)
        for i = 1, math.min(#stat_a.cores, #stat_b.cores) do
            cpu.cores[i] = busy_pct(stat_a.cores[i], stat_b.cores[i])
        end
    end

    local network = {}
    if net_a and net_b then
        local dt = net_b.time - net_a.time
        local prev = {}
        for _, e in ipairs(net_a.ifaces) do prev[e.name] = e end
        for _, e in ipairs(net_b.ifaces) do
            local p = prev[e.name]
            local rx_rate, tx_rate = 0, 0
            if p and dt > 0 then
                rx_rate = math.max(0, (e.rx - p.rx) / dt)
                tx_rate = math.max(0, (e.tx - p.tx) / dt)
            end
            network[#network + 1] = {
                interface_name = e.name,
                bytes_recv_rate_per_sec = round(rx_rate, 1),
                bytes_sent_rate_per_sec = round(tx_rate, 1)
            }
        end
    end

    return {cpu = cpu, network = network}
end

-- Snapshot, wait 1 s, snapshot. Returns the rates and the second snapshots.
local function sample_window()
    local stat_a, net_a = stat_snapshot(), net_snapshot()
    -- nanosleep where Nixio is present (always on OpenWrt): `sleep` is a process.
    local ok, nixio = pcall(require, "nixio")
    if ok and nixio.nanosleep then nixio.nanosleep(1)
    else os.execute("sleep 1") end  -- return value ignored: it differs between Lua 5.1 and 5.2+
    local stat_b, net_b = stat_snapshot(), net_snapshot()
    return measure(stat_a, net_a, stat_b, net_b), stat_b, net_b
end

-- One 1 s measurement window serving CPU total, per-core and network rates:
-- what a history record wants, whenever it is taken. Live requests use M.rates.
function M.sample()
    return (sample_window())
end

-- The rates file: the counters of the last measurement, when it was taken
-- (uptime seconds) and the rates it produced. One line per item, "end" last so
-- a file caught half-written reads as missing.
local function read_rates()
    local content = read_file(RATES_FILE)
    if not content or not content:match("\nend\n$") then return nil end
    local state = {
        stat = {cores = {}}, net = {ifaces = {}},
        result = {cpu = {total = 0, cores = {}}, network = {}}
    }
    for line in content:gmatch("[^\n]+") do
        local kind, rest = line:match("^(%S+)%s*(.*)$")
        if kind == "time" then
            state.time = tonumber(rest)
            state.net.time = state.time
        elseif kind == "cpu" or kind == "core" then
            local idle, total = rest:match("^(%d+) (%d+)$")
            if not idle then return nil end
            local entry = {idle = tonumber(idle), total = tonumber(total)}
            if kind == "cpu" then state.stat.all = entry
            else state.stat.cores[#state.stat.cores + 1] = entry end
        elseif kind == "net" then
            local name, rx, tx = rest:match("^(%S+) (%d+) (%d+)$")
            if not name then return nil end
            state.net.ifaces[#state.net.ifaces + 1] = {name = name, rx = tonumber(rx), tx = tonumber(tx)}
        elseif kind == "rcpu" then
            state.result.cpu.total = tonumber(rest) or 0
        elseif kind == "rcore" then
            state.result.cpu.cores[#state.result.cpu.cores + 1] = tonumber(rest) or 0
        elseif kind == "rnet" then
            local name, rx, tx = rest:match("^(%S+) (%S+) (%S+)$")
            if not name then return nil end
            state.result.network[#state.result.network + 1] = {
                interface_name = name,
                bytes_recv_rate_per_sec = tonumber(rx) or 0,
                bytes_sent_rate_per_sec = tonumber(tx) or 0
            }
        end
    end
    if not state.time or not state.stat.all then return nil end
    return state
end

-- Best effort: without the file every request measures its own 1 s window.
local function write_rates(stat, net, result)
    if not stat or not stat.all or not net then return end
    local lines = {
        string.format("time %.2f", net.time),
        string.format("cpu %.0f %.0f", stat.all.idle, stat.all.total)
    }
    for _, c in ipairs(stat.cores) do
        lines[#lines + 1] = string.format("core %.0f %.0f", c.idle, c.total)
    end
    for _, e in ipairs(net.ifaces) do
        lines[#lines + 1] = string.format("net %s %.0f %.0f", e.name, e.rx, e.tx)
    end
    lines[#lines + 1] = "rcpu " .. result.cpu.total
    for _, pct in ipairs(result.cpu.cores) do lines[#lines + 1] = "rcore " .. pct end
    for _, e in ipairs(result.network) do
        lines[#lines + 1] = "rnet " .. e.interface_name .. " "
            .. e.bytes_recv_rate_per_sec .. " " .. e.bytes_sent_rate_per_sec
    end
    lines[#lines + 1] = "end\n"
    -- Requests run at once; a name of this request's own, then rename, so no
    -- reader sees another's half-written file.
    local ok, nixio = pcall(require, "nixio")
    local tmp = RATES_FILE .. "." .. ((ok and nixio.getpid) and nixio.getpid() or "tmp")
    local f = io.open(tmp, "w")
    if not f then return end
    local written = f:write(table.concat(lines, "\n"))
    local closed = f:close()
    if not written or not closed or not os.rename(tmp, RATES_FILE) then os.remove(tmp) end
end

-- CPU and network rates for the live endpoints, measured from the previous
-- request's counters to now, the way top does: no waiting inside the request.
-- (Each of /4/cpu, /4/percpu and /4/network used to sleep 1 s, on every poll.)
--   * A measurement under RATES_FRESH old is returned as is: the three
--     endpoints of one poll share it, and no window is too short to mean much.
--   * No usable previous counters (first request, the dashboard was closed, a
--     reboot): one 1 s window, as before.
function M.rates()
    local prev = read_rates()
    local age = prev and (get_time() - prev.time)
    if prev and age >= 0 and age < RATES_FRESH then return prev.result end

    local result, stat, net
    if prev and age >= 0 and age <= RATES_STALE then
        stat, net = stat_snapshot(), net_snapshot()
        local usable = stat and stat.all and net
            and stat.all.total > prev.stat.all.total and net.time > prev.time
        if usable then result = measure(prev.stat, prev.net, stat, net) end
    end
    if not result then result, stat, net = sample_window() end
    write_rates(stat, net, result)
    return result
end

function M.cpu()
    return {total = M.rates().cpu.total}
end

function M.percpu()
    local cores = {}
    for i, pct in ipairs(M.rates().cpu.cores) do
        cores[i] = {cpu_number = i - 1, total = pct}
    end
    return cores
end

function M.network()
    return M.rates().network
end

------------------------------------------------------------------------
-- Complex endpoints
------------------------------------------------------------------------

function M.fs()
    -- `-P -k -T` (one row per filesystem, 1-KiB blocks, fs type) works with
    -- BusyBox and GNU df; -P keeps long device names on the same line. The
    -- GNU-only `-B1` (raw bytes) isn't supported by OpenWrt's default BusyBox df,
    -- so it silently produced nothing there. We convert KiB -> bytes below.
    local output = run_cmd_timeout(5, "df -P -k -T")
    -- Empty output means df failed/timed out or is missing (io.popen gives us no
    -- exit status here, and empty stdout reads back as ""); surface it to the
    -- server log instead of returning a silently-blank Drive Usage card.
    if not output or output:match("^%s*$") then
        io.stderr:write("metrics: `df -P -k -T` produced no output — Drive Usage unavailable\n")
        return {}
    end

    local skip_types = {
        tmpfs=1, devtmpfs=1, proc=1, sysfs=1, devpts=1, debugfs=1,
        cgroup=1, cgroup2=1, pstore=1, securityfs=1, hugetlbfs=1,
        mqueue=1, bpf=1, tracefs=1, binfmt_misc=1, configfs=1,
        fusectl=1, efivarfs=1,
        squashfs=1  -- /rom, the read-only system image: always 100% full
    }

    -- Required here, not at load, so tests can swap in a fake via package.loaded.
    local nixio = require("nixio")
    local filesystems = {}
    local first = true
    for line in output:gmatch("[^\n]+") do
        if first then first = false  -- skip header
        else
            local dev, fstype, size, used, _, pct, mount =
                line:match("^(%S+)%s+(%S+)%s+(%d+)%s+(%d+)%s+(%d+)%s+(%d+)%%%s+(.+)$")
            if dev and mount and not skip_types[fstype] then
                -- Symlinks resolved (/dev/mapper/x -> /dev/dm-0), as readlink -f
                -- would, without a process per row. A name that is not a path
                -- (ubi0:rootfs_data, overlayfs:/overlay) is kept as df printed it.
                local resolved = nixio.fs.realpath(dev) or dev
                -- df has no inode columns without BusyBox's FEATURE_DF_FANCY; statvfs
                -- always works. files == 0 means no fixed inode table (jffs2, ubifs).
                local st = nixio.fs.statvfs(mount)
                local inodes_used = st and st.files > 0 and st.files - st.ffree or nil
                filesystems[#filesystems + 1] = {
                    device_name = resolved,
                    mnt_point = mount,
                    used = (tonumber(used) or 0) * 1024,  -- 1-KiB blocks -> bytes
                    size = (tonumber(size) or 0) * 1024,
                    percent = tonumber(pct) or 0,
                    inodes_used = inodes_used,
                    inodes_percent = inodes_used and round(inodes_used / st.files * 100, 1)
                }
            end
        end
    end
    return filesystems
end

-- Paths matching a glob pattern, sorted (as `ls` lists them), without a
-- process. Required here, not at load, so tests can swap in a fake.
local function glob(pattern)
    local paths = {}
    local iter = require("nixio").fs.glob(pattern)
    if iter then
        for path in iter do paths[#paths + 1] = path end
    end
    table.sort(paths)
    return paths
end

function M.sensors()
    local sensors = {}

    -- Thermal zones
    for _, tz_path in ipairs(glob("/sys/class/thermal/thermal_zone*")) do
        local temp_str = read_line(tz_path .. "/temp")
        if temp_str then
            local temp = tonumber(temp_str)
            if temp and temp > 0 then
                local label = read_line(tz_path .. "/type")
                    or ("zone" .. (tz_path:match("thermal_zone(%d+)") or ""))
                sensors[#sensors + 1] = {
                    type = "temperature_core",
                    label = label,
                    value = round(temp / 1000, 1)
                }
            end
        end
    end

    -- hwmon sensors
    for _, temp_file in ipairs(glob("/sys/class/hwmon/hwmon*/temp*_input")) do
        local temp_str = read_line(temp_file)
        if temp_str then
            local temp = tonumber(temp_str)
            if temp and temp > 0 then
                local label = read_line((temp_file:gsub("_input$", "_label")))
                if not label then
                    local hw_path = temp_file:match("^(.*)/[^/]+$")
                    local hwmon_name = read_line(hw_path .. "/name") or "hwmon"
                    local idx = temp_file:match("temp(%d+)_input") or ""
                    label = hwmon_name .. " " .. idx
                end
                sensors[#sensors + 1] = {
                    type = "temperature_core",
                    label = label,
                    value = round(temp / 1000, 1)
                }
            end
        end
    end

    return sensors
end

------------------------------------------------------------------------
-- Command-based endpoints
------------------------------------------------------------------------

function M.ping()
    local targets = config.ping_targets
    if not targets or #targets == 0 then
        return {disabled = true}
    end

    local addrs, cmds = {}, {}
    for _, t in ipairs(targets) do
        local addr = validate_addr(t.address)
        if addr then
            addrs[#addrs + 1] = addr
            cmds[#cmds + 1] = "ping -c 1 -W 2 " .. addr
        end
    end
    -- All targets at once. The guard bounds what -W does not: a name that will
    -- not resolve.
    local outputs = run_cmds(5, cmds)
    local result = {targets = {}}
    for i, addr in ipairs(addrs) do
        local ms = outputs[i] and outputs[i]:match("time=(%d+%.?%d*)")
        if ms then
            result.targets[addr] = {latency_ms = round(tonumber(ms), 2)}
        else
            result.targets[addr] = {error = "timeout"}
        end
    end
    return result
end

function M.ip_info()
    local result = {lan = {}, public = {}}

    -- LAN IPs: try ip -j, fall back to text parsing
    local ok_json = false
    local ip_json = run_cmd_timeout(5, "ip -j addr show")
    if ip_json and ip_json:match("^%[") then
        local ok, jsonc = pcall(require, "luci.jsonc")
        if ok then
            local ifaces = jsonc.parse(ip_json)
            if ifaces then
                ok_json = true
                for _, iface in ipairs(ifaces) do
                    local name = iface.ifname
                    if name and name ~= "lo" then
                        local ipv4s, ipv6s = {}, {}
                        for _, ai in ipairs(iface.addr_info or {}) do
                            local addr = ai["local"]
                            if addr then
                                if ai.family == "inet" then ipv4s[#ipv4s + 1] = addr
                                elseif ai.family == "inet6" and not addr:match("^fe80") then
                                    ipv6s[#ipv6s + 1] = addr
                                end
                            end
                        end
                        if #ipv4s > 0 or #ipv6s > 0 then
                            local entry = {interface = name}
                            if #ipv4s == 1 then entry.ipv4 = ipv4s[1]
                            elseif #ipv4s > 1 then entry.ipv4 = ipv4s end
                            if #ipv6s == 1 then entry.ipv6 = ipv6s[1]
                            elseif #ipv6s > 1 then entry.ipv6 = ipv6s end
                            result.lan[#result.lan + 1] = entry
                        end
                    end
                end
            end
        end
    end

    -- Fallback: parse ip addr show text output
    if not ok_json then
        local ip_text = run_cmd_timeout(5, "ip addr show")
        if ip_text then
            local cur_iface, ipv4s, ipv6s = nil, {}, {}
            local function flush()
                if cur_iface and cur_iface ~= "lo" and (#ipv4s > 0 or #ipv6s > 0) then
                    local entry = {interface = cur_iface}
                    if #ipv4s == 1 then entry.ipv4 = ipv4s[1]
                    elseif #ipv4s > 1 then entry.ipv4 = ipv4s end
                    if #ipv6s == 1 then entry.ipv6 = ipv6s[1]
                    elseif #ipv6s > 1 then entry.ipv6 = ipv6s end
                    result.lan[#result.lan + 1] = entry
                end
            end
            for line in ip_text:gmatch("[^\n]+") do
                local iface_name = line:match("^%d+:%s+(%S+):")
                if iface_name then
                    flush()
                    cur_iface = iface_name
                    ipv4s, ipv6s = {}, {}
                else
                    local v4 = line:match("inet (%d+%.%d+%.%d+%.%d+)")
                    if v4 then ipv4s[#ipv4s + 1] = v4 end
                    local v6 = line:match("inet6 (%S+)")
                    if v6 and not v6:match("^fe80") then ipv6s[#ipv6s + 1] = v6 end
                end
            end
            flush()
        end
    end

    -- Public IP, cached in a tmpfs file: "<epoch> <ipv4|-> <ipv6|->".
    -- A missing, stale or unparseable file simply means "look it up again".
    local now = os.time()
    local public = nil
    local line = read_line(PUBLIC_IP_FILE)
    if line then
        local epoch, v4, v6 = line:match("^(%d+)%s+(%S+)%s+(%S+)")
        if epoch and (now - tonumber(epoch)) <= PUBLIC_IP_TTL then
            public = {}
            if v4 ~= "-" then public.ipv4 = v4 end
            if v6 ~= "-" then public.ipv6 = v6 end
        end
    end
    if not public then
        public = {}
        -- Both lookups at once: 3 s at worst, not 6.
        local lookups = run_cmds(nil, {
            "wget -qO- -T 3 https://api.ipify.org",
            "wget -qO- -T 3 https://api64.ipify.org"
        })
        local v4 = lookups[1]
        if v4 then
            v4 = v4:gsub("%s+", "")
            if v4 ~= "" then public.ipv4 = v4 end
        end
        -- api64 is dual-stack and may return IPv4 when IPv6 is unavailable.
        local address = lookups[2]
        if address then
            address = address:gsub("%s+", "")
            if address:find(":") then
                public.ipv6 = address
            elseif address ~= "" and not public.ipv4 then
                public.ipv4 = address
            end
        end
        local f = io.open(PUBLIC_IP_FILE, "w")
        if f then
            f:write(now .. " " .. (public.ipv4 or "-") .. " " .. (public.ipv6 or "-") .. "\n")
            f:close()
        end
    end
    if public.ipv4 then result.public.ipv4 = public.ipv4 end
    if public.ipv6 then result.public.ipv6 = public.ipv6 end
    if not public.ipv4 and not public.ipv6 then
        result.public.error = "lookup failed"
    end

    -- Tailscale (optional)
    local ts_out = run_cmd_timeout(5, "tailscale ip")
    if ts_out and ts_out ~= "" then
        result.tailscale = {}
        for line in ts_out:gmatch("%S+") do
            if line:find(":") then result.tailscale.ipv6 = line
            else result.tailscale.ipv4 = line end
        end
    end

    return result
end

return M
