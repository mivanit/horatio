-- Simple JSONL history recording. Records piggyback on API requests at most
-- every RECORD_INTERVAL (config history.record_interval), with a cron POST to
-- /api/history/record twice a day as the floor (see install.sh).
-- No RRD dependency — snapshots metrics and appends JSON lines to a file.
--
-- uhttpd forks a child per request, so nothing in this module survives between
-- requests; the "last recorded" timestamp lives in a tmpfs file instead.

local M = {}

local config = {}
local metrics = nil

local HISTORY_FILE = "/tmp/dashboard-history.jsonl"
local LAST_RECORD_FILE = "/tmp/dashboard-last-record"  -- epoch of the last append
local RECORD_INTERVAL = 600   -- max one piggyback record per this many seconds
local MIN_MANUAL_INTERVAL = 60
local INITIAL_TAIL_READ = 1048576  -- 1 MiB tail window, doubled until it covers the range

function M.init(cfg, metrics_module, dashboard_dir)
    config = cfg or {}
    metrics = metrics_module
    local hist_cfg = config.history or {}
    RECORD_INTERVAL = hist_cfg.record_interval or 600
    if dashboard_dir then
        local logs_dir = dashboard_dir .. "/logs"
        os.execute("mkdir -p " .. logs_dir)
        HISTORY_FILE = logs_dir .. "/history.jsonl"
    end
end

local function round(n, decimals)
    local mult = 10 ^ (decimals or 2)
    return math.floor(n * mult + 0.5) / mult
end

-- Median of a non-empty list of numbers; averages the middle pair on even
-- counts, like Python's statistics.median. Sorts a copy.
local function median(list)
    local sorted = {}
    for i, v in ipairs(list) do sorted[i] = v end
    table.sort(sorted)
    local n = #sorted
    if n % 2 == 1 then return sorted[(n + 1) / 2] end
    return (sorted[n / 2] + sorted[n / 2 + 1]) / 2
end

-- Build a snapshot record from current metrics
local function build_record()
    local now = os.time()
    local record = {
        timestamp = os.date("!%Y-%m-%dT%H:%M:%S+00:00", now),
        duration_sec = 1
    }

    -- CPU and network share one 1 s sampling window
    local sample = metrics.sample()
    local cpu = sample.cpu
    record.cpu = {avg = cpu.total, min = cpu.total, max = cpu.total}
    if #cpu.cores > 0 then
        local lo, hi = cpu.cores[1], cpu.cores[1]
        for _, v in ipairs(cpu.cores) do
            if v < lo then lo = v end
            if v > hi then hi = v end
        end
        record.cpu_cores = {min = lo, median = round(median(cpu.cores), 2), max = hi}
    end

    -- Memory
    local mem = metrics.mem()
    if mem.total and mem.total > 0 then
        record.memory = {
            used_gb = round(mem.used / (1024^3), 2),
            total_gb = round(mem.total / (1024^3), 2),
            percent = mem.percent
        }
    end

    -- Swap
    local swap = metrics.memswap()
    if swap.total and swap.total > 0 then
        record.swap = {
            used_gb = round(swap.used / (1024^3), 2),
            total_gb = round(swap.total / (1024^3), 2),
            percent = swap.percent
        }
    end

    -- Network
    local rx, tx = 0, 0
    for _, iface in ipairs(sample.network) do
        rx = rx + (iface.bytes_recv_rate_per_sec or 0)
        tx = tx + (iface.bytes_sent_rate_per_sec or 0)
    end
    local rx_mb, tx_mb = round(rx / 1e6, 2), round(tx / 1e6, 2)
    record.network = {
        in_mbps = {avg = rx_mb, min = rx_mb, max = rx_mb},
        out_mbps = {avg = tx_mb, min = tx_mb, max = tx_mb}
    }

    -- Drives
    local fs = metrics.fs()
    if #fs > 0 then
        local drives = {}
        for _, d in ipairs(fs) do
            drives[#drives + 1] = {
                mount = d.mnt_point,
                used_gb = round(d.used / (1024^3), 2),
                total_gb = round(d.size / (1024^3), 2),
                percent = d.percent
            }
        end
        record.drives = drives
    end

    -- Temperature
    local sensors = metrics.sensors()
    if #sensors > 0 then
        local temps = {}
        for _, s in ipairs(sensors) do
            if s.type == "temperature_core" then temps[s.label] = s.value end
        end
        if next(temps) then record.temps = temps end
    end

    -- Load
    local load_data = metrics.load()
    record.load = {min1 = load_data.min1, min5 = load_data.min5, min15 = load_data.min15}

    -- Ping. Record an entry for every target (one ping/cycle, so loss is 0 or 1):
    -- success -> {avg,min,max,loss=0}; timeout -> {loss=1.0} (avg omitted = full
    -- outage), matching the Python backend so the loss band works on the router too.
    local ping = metrics.ping()
    if ping.targets then
        local pr = {}
        for addr, data in pairs(ping.targets) do
            if data.latency_ms then
                pr[addr] = {avg = data.latency_ms, min = data.latency_ms, max = data.latency_ms, loss = 0}
            else
                pr[addr] = {loss = 1.0}
            end
        end
        if next(pr) then record.ping = pr end
    end

    return record
end

-- Append a record to the JSONL file.
-- Returns ok, err so callers can report a failed write instead of claiming
-- success: a read-only or full filesystem must not surface as {status = "ok"}.
local function append_record(record)
    local jsonc = require("luci.jsonc")
    local json_line = jsonc.stringify(record)
    if not json_line then
        local err = "failed to serialize record to JSON"
        io.stderr:write("[dashboard] history append FAILED: " .. err .. "\n")
        return false, err
    end
    local f, open_err = io.open(HISTORY_FILE, "a")
    if not f then
        local err = "cannot open " .. HISTORY_FILE .. ": " .. tostring(open_err)
        io.stderr:write("[dashboard] history append FAILED: " .. err .. "\n")
        return false, err
    end
    f:write(json_line .. "\n")
    f:close()
    return true
end

local function read_last_record_time()
    local f = io.open(LAST_RECORD_FILE, "r")
    if not f then return 0 end
    local line = f:read("*l")
    f:close()
    local epoch = line and line:match("^%d+")
    return epoch and tonumber(epoch) or 0
end

local function write_last_record_time(now)
    local f = io.open(LAST_RECORD_FILE, "w")
    if f then
        f:write(now .. "\n")
        f:close()
    end
end

-- Called on every API request (handler.lua); records if RECORD_INTERVAL has
-- elapsed since the last append. The request that trips this pays ~1 s of
-- sampling plus the pings before it is answered.
function M.maybe_record()
    if not metrics then return end
    local now = os.time()
    local elapsed = now - read_last_record_time()
    if elapsed >= 0 and elapsed < RECORD_INTERVAL then return end
    local ok, record = pcall(build_record)
    if not ok then
        io.stderr:write("[dashboard] history recording FAILED: " .. tostring(record) .. "\n")
        return
    end
    if append_record(record) then write_last_record_time(now) end
end

-- POST /api/history/record (cron and manual). Rate-limited via the same
-- timestamp file.
function M.record_now()
    if not metrics then return {error = "not initialized"} end
    local now = os.time()
    local elapsed = now - read_last_record_time()
    if elapsed >= 0 and elapsed < MIN_MANUAL_INTERVAL then
        return {status = "skipped", reason = "rate_limited", retry_after = MIN_MANUAL_INTERVAL - elapsed}
    end
    local ok, record = pcall(build_record)
    if not ok then return {error = "Recording failed", detail = tostring(record)} end
    local written, write_err = append_record(record)
    if not written then
        return {error = "Recording failed", detail = write_err}
    end
    write_last_record_time(now)
    return {status = "ok", record = record}
end

-- Compute local-to-UTC offset in seconds (positive if east of UTC)
local function tz_offset()
    local now = os.time()
    return now - os.time(os.date("!*t", now))
end

-- Read history (GET /api/history?hours=N)
-- Records are appended chronologically, so read a window from the end of the
-- file and double it until the oldest record in the window predates the cutoff.
function M.get(hours)
    hours = math.max(0.1, math.min(hours or 24, 8760))
    local cutoff = os.time() - (hours * 3600)
    local offset = tz_offset()

    local f = io.open(HISTORY_FILE, "rb")
    if not f then return {records = {}, count = 0, skipped_lines = 0} end

    local jsonc = require("luci.jsonc")
    local size = f:seek("end")
    local read_size = INITIAL_TAIL_READ

    while true do
        local start = math.max(0, size - read_size)
        f:seek("set", start)
        local data = f:read(size - start) or ""
        if start > 0 then
            -- Drop the (likely partial) line the window starts in
            local nl = data:find("\n", 1, true)
            data = nl and data:sub(nl + 1) or ""
        end

        local records = {}
        local first_ts = nil
        local skipped = 0
        for line in data:gmatch("[^\n]+") do
            line = line:gsub("\r$", "")
            if line ~= "" then
                local ts = nil
                local record = jsonc.parse(line)
                if type(record) == "table" and record.timestamp then
                    -- Parse ISO timestamp (UTC) to epoch for filtering
                    local y, mo, d, h, mi, s = record.timestamp:match(
                        "(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
                    if y then
                        -- os.time interprets as local time, add offset to get UTC epoch
                        ts = os.time({year=tonumber(y), month=tonumber(mo),
                            day=tonumber(d), hour=tonumber(h), min=tonumber(mi), sec=tonumber(s)})
                            + offset
                    end
                end
                if ts then
                    if not first_ts then first_ts = ts end
                    if ts >= cutoff then
                        records[#records + 1] = record
                    end
                else
                    -- Corrupt line: count it so the API can surface it
                    skipped = skipped + 1
                end
            end
        end

        if start == 0 or (first_ts and first_ts < cutoff) then
            f:close()
            if skipped > 0 then
                io.stderr:write("[dashboard] history read: skipped " .. skipped .. " malformed lines\n")
            end
            return {records = records, count = #records, skipped_lines = skipped}
        end
        read_size = read_size * 2
    end
end

function M.get_settings()
    return {
        record_interval = RECORD_INTERVAL,
        sample_duration = 1,
        sample_count = 1,
        min_record_interval = MIN_MANUAL_INTERVAL,
        ping_enabled = true,
        defaults = {
            record_interval = RECORD_INTERVAL,
            sample_duration = 1,
            sample_count = 1,
            min_record_interval = MIN_MANUAL_INTERVAL,
            ping_enabled = true
        },
        password_configured = false
    }
end

return M
