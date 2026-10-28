-- System metrics from /proc and /sys for OpenWrt.
-- No external dependencies — reads Linux virtual filesystems directly.

local M = {}

local config = {}

-- uhttpd forks a child per request and the child exits afterwards, so no
-- module-level state survives between requests. Rate metrics therefore
-- measure their own window inside the request (see M.sample), and the
-- public-IP cache lives in a tmpfs file.
local PUBLIC_IP_FILE = "/tmp/dashboard-public-ip"
local PUBLIC_IP_TTL = 600     -- 10 minutes

local has_timeout = false

function M.init(cfg)
    config = cfg or {}
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
    if days > 0 then
        return string.format("%dd %d:%02d:%02d", days, hours, mins, s)
    end
    return string.format("%d:%02d:%02d", hours, mins, s)
end

function M.system_info()
    local hostname = (read_line("/proc/sys/kernel/hostname") or "openwrt"):gsub("%s+$", "")
    local uname = (run_cmd_timeout(5, "uname -r") or ""):gsub("%s+$", "")

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
-- Rate-based endpoints: snapshot, sleep 1 s, snapshot, diff.
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

-- One 1 s measurement window serving CPU total, per-core and network rates.
-- Returns {cpu = {total = pct, cores = {pct, ...}}, network = {iface, ...}}
-- where network entries have the same shape /4/network always returned.
function M.sample()
    local stat_a, net_a = stat_snapshot(), net_snapshot()
    os.execute("sleep 1")   -- return value ignored: it differs between Lua 5.1 and 5.2+
    local stat_b, net_b = stat_snapshot(), net_snapshot()

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

function M.cpu()
    return {total = M.sample().cpu.total}
end

function M.percpu()
    local cores = {}
    for i, pct in ipairs(M.sample().cpu.cores) do
        cores[i] = {cpu_number = i - 1, total = pct}
    end
    return cores
end

function M.network()
    return M.sample().network
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
        fusectl=1, efivarfs=1
    }

    local filesystems = {}
    local first = true
    for line in output:gmatch("[^\n]+") do
        if first then first = false  -- skip header
        else
            local dev, fstype, size, used, _, pct, mount =
                line:match("^(%S+)%s+(%S+)%s+(%d+)%s+(%d+)%s+(%d+)%s+(%d+)%%%s+(.+)$")
            if dev and mount and not skip_types[fstype] then
                local resolved = (run_cmd("readlink -f '" .. dev:gsub("'", "'\\''") .. "'") or dev):gsub("%s+$", "")
                filesystems[#filesystems + 1] = {
                    device_name = resolved,
                    mnt_point = mount,
                    used = (tonumber(used) or 0) * 1024,  -- 1-KiB blocks -> bytes
                    size = (tonumber(size) or 0) * 1024,
                    percent = tonumber(pct) or 0
                }
            end
        end
    end
    return filesystems
end

function M.sensors()
    local sensors = {}

    -- Thermal zones
    local tz_list = run_cmd("ls -d /sys/class/thermal/thermal_zone* 2>/dev/null")
    if tz_list then
        for tz_path in tz_list:gmatch("%S+") do
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
    end

    -- hwmon sensors
    local hwmon_list = run_cmd("ls -d /sys/class/hwmon/hwmon* 2>/dev/null")
    if hwmon_list then
        for hw_path in hwmon_list:gmatch("%S+") do
            local temp_files = run_cmd("ls " .. hw_path .. "/temp*_input 2>/dev/null")
            if temp_files then
                for temp_file in temp_files:gmatch("%S+") do
                    local temp_str = read_line(temp_file)
                    if temp_str then
                        local temp = tonumber(temp_str)
                        if temp and temp > 0 then
                            local label = read_line(temp_file:gsub("_input$", "_label"))
                            if not label then
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

    local result = {targets = {}}
    for _, t in ipairs(targets) do
        local addr = validate_addr(t.address)
        if addr then
            local output = run_cmd("ping -c 1 -W 2 " .. addr)
            local ms = output and output:match("time=(%d+%.?%d*)")
            if ms then
                result.targets[addr] = {latency_ms = round(tonumber(ms), 2)}
            else
                result.targets[addr] = {error = "timeout"}
            end
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
        local v4 = run_cmd("wget -qO- -T 3 https://api.ipify.org")
        if v4 then
            v4 = v4:gsub("%s+", "")
            if v4 ~= "" then public.ipv4 = v4 end
        end
        -- api64 is dual-stack and may return IPv4 when IPv6 is unavailable.
        local address = run_cmd("wget -qO- -T 3 https://api64.ipify.org")
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
