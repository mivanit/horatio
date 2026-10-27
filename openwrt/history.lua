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
local RECORD_LOCK = "/tmp/dashboard-history.lock"      -- stable inode; never unlink a kernel lockfile
local RECORD_INTERVAL = 600   -- max one piggyback record per this many seconds
local MIN_MANUAL_INTERVAL = 60
local INITIAL_TAIL_READ = 1048576  -- 1 MiB tail window, doubled until it covers the range
-- Past this (config history.max_file_mb) the older part of the file is gzipped
-- to history-<UTC time>.jsonl.gz beside it and the file keeps its newest half.
M.max_file_bytes = 256 * 1048576
M.max_read_bytes = 4 * 1048576  -- bound per-request parsing and response memory on routers

-- state_dir: where the tmpfs state files go (tests point it elsewhere).
function M.init(cfg, metrics_module, dashboard_dir, state_dir)
    config = cfg or {}
    metrics = metrics_module
    if state_dir then
        LAST_RECORD_FILE = state_dir .. "/dashboard-last-record"
        RECORD_LOCK = state_dir .. "/dashboard-history.lock"
    end
    local hist_cfg = config.history or {}
    RECORD_INTERVAL = hist_cfg.record_interval or 600
    M.max_file_bytes = (hist_cfg.max_file_mb or 256) * 1048576
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

-- os.execute's result: 0 on Lua 5.1, true on 5.2+.
local function sh(cmd)
    local r = os.execute(cmd)
    return r == true or r == 0
end

local function shell_quote(value)
    return "'" .. value:gsub("'", "'\\''") .. "'"
end

-- Open after uhttpd forks the request. POSIX locks are process-owned and are
-- released by the kernel on exit, including SIGKILL; no stale-owner reaper.
local function with_record_lock(fn)
    local nixio = require("nixio")
    local lock, code, err = nixio.open(RECORD_LOCK, "w", "600") -- same inode on every open
    if not lock then return {error = "Recording failed", detail = tostring(err or code)} end
    local acquired
    acquired, code, err = lock:lock("tlock")
    if not acquired then
        lock:close()
        if code == nixio.const.EAGAIN or code == nixio.const.EACCES then
            return {status = "skipped", reason = "busy", retry_after = 1}
        end
        return {error = "Recording failed", detail = tostring(err or code)}
    end
    local ok, result = pcall(fn)
    local closed, close_code, close_err = lock:close()
    if not ok then return {error = "Recording failed", detail = tostring(result)} end
    if not closed then
        result.warning = result.warning or "History lock close failed"
        result.detail = tostring(close_err or close_code)
    end
    return result
end

local rotate_locked

-- Append a record to the JSONL file.
-- The caller holds RECORD_LOCK until any rotation has finished.
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
    local f, open_err = io.open(HISTORY_FILE, "a+")
    if not f then
        local err = "cannot open " .. HISTORY_FILE .. ": " .. tostring(open_err)
        io.stderr:write("[dashboard] history append FAILED: " .. err .. "\n")
        return false, err
    end
    -- A failed/interrupted append can leave a partial final line. Preserve it,
    -- but separate the next record so that one bad line cannot swallow it.
    -- Inspect through the same locked append handle; uncertainty must not write.
    local function inspection_failed(err)
        f:close()
        return false, "cannot inspect history tail: " .. tostring(err)
    end
    local previous_size, inspect_err = f:seek("end")
    if not previous_size then return inspection_failed(inspect_err) end
    local separator = ""
    if previous_size > 0 then
        local position, seek_err = f:seek("set", previous_size - 1)
        if not position then return inspection_failed(seek_err) end
        local last, read_err = f:read(1)
        if read_err or not last or #last ~= 1 then
            return inspection_failed(read_err or "unexpected end of history file")
        end
        if last ~= "\n" then separator = "\n" end
        -- An update stream needs a seek between reading and writing.
        position, seek_err = f:seek("end")
        if not position then return inspection_failed(seek_err) end
    end
    local written, write_err = f:write(separator .. json_line .. "\n")
    local size, seek_err = f:seek("end")
    local closed, close_err = f:close()
    if not written or not size or not closed then
        return false, tostring(write_err or seek_err or close_err)
    end
    if size > M.max_file_bytes then
        local rotated, rotation_err = rotate_locked()
        if not rotated then return true, rotation_err end
    end
    return true
end

-- Every byte must arrive. EOF before n bytes and read errors are failures,
-- even if gzip can make a valid archive from the incomplete input it received.
local function copy_exact(src, out, n)
    while n > 0 do
        local chunk, read_err = src:read(math.min(n, 65536))
        if read_err then return false, read_err end
        if not chunk or chunk == "" then return false, "unexpected end of history file" end
        local written, write_err = out:write(chunk)
        if not written then return false, write_err end
        n = n - #chunk
    end
    return true
end

-- Reserve a name exclusively: two rotations in one second, or a clock reset,
-- must not overwrite an earlier archive. Only this attempt's file is removed
-- on failure; the numeric suffix is used only for existing names.
local function reserve_archive()
    local nixio = require("nixio")
    local flags = nixio.open_flags("wronly", "creat", "excl")
    local stem = HISTORY_FILE:gsub("%.jsonl$", "") .. os.date("!-%Y%m%dT%H%M%SZ")
    local suffix = 0
    while true do
        local path = stem .. (suffix == 0 and "" or "-" .. suffix) .. ".jsonl.gz"
        local f, code, err = nixio.open(path, flags, "600")
        if f then
            local closed, close_code, close_err = f:close()
            if closed then return path end
            os.remove(path)
            return nil, close_err or close_code
        end
        if code ~= nixio.const.EEXIST then return nil, err or code end
        suffix = suffix + 1
    end
end

-- Gzip everything but the newest max_file_bytes / 2 (whole lines) to an archive
-- beside the file, then atomically replace the live file with its newest part.
-- The writer lock is already held. Until rename succeeds, every failure keeps
-- the live file and removes this attempt's archive and incomplete replacement.
rotate_locked = function()
    local f, gz, out, archive
    local tmp, tmp_created = HISTORY_FILE .. ".tmp", false
    local function checked(value, err)
        if not value then error(tostring(err or "history I/O failed"), 0) end
        return value
    end
    local ok, err = pcall(function()
        f = checked(io.open(HISTORY_FILE, "rb"))
        local size = checked(f:seek("end"))
        if size <= M.max_file_bytes then return end
        checked(f:seek("set", size - math.floor(M.max_file_bytes / 2)))
        checked(f:read("*l"))
        local split = checked(f:seek())
        if split <= 0 or split >= size then return end

        -- uhttpd resets SIGPIPE in its worker. A failed gzip must become a
        -- checked write error so this attempt's files can be cleaned up.
        local nixio = require("nixio")
        local ignored, _, signal_err = nixio.signal(nixio.const.SIGPIPE, "ign")
        checked(ignored, signal_err)
        archive = checked(reserve_archive())
        gz = checked(io.popen("gzip > " .. shell_quote(archive), "w"))
        checked(f:seek("set", 0))
        checked(copy_exact(f, gz, split))
        checked(gz:flush())
        local pipe = gz
        gz = nil
        checked(pipe:close())
        checked(sh("gzip -t " .. shell_quote(archive) .. " 2>/dev/null"), "archive verification failed")

        out = checked(io.open(tmp, "wb"))
        tmp_created = true
        checked(copy_exact(f, out, size - split))
        checked(out:flush())
        local tail = out
        out = nil
        checked(tail:close())
        local source = f
        f = nil
        checked(source:close())
        checked(os.rename(tmp, HISTORY_FILE))
        tmp_created = false
    end)
    for _, handle in pairs({f, gz, out}) do
        local closed, close_err = handle:close()
        if not closed and ok then ok, err = false, close_err end
    end
    if not ok then
        if archive then os.remove(archive) end
        if tmp_created then os.remove(tmp) end
    end
    return ok, err
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
    -- The writer lock covers this replacement. A full tmpfs must neither erase
    -- the previous valid stamp nor silently disable the recording interval.
    local tmp = LAST_RECORD_FILE .. ".tmp"
    local f, err = io.open(tmp, "w")
    if not f then return false, err end
    local written, write_err = f:write(now .. "\n")
    local flushed, flush_err = f:flush()
    local closed, close_err = f:close()
    if not written or not flushed or not closed then
        os.remove(tmp)
        return false, write_err or flush_err or close_err
    end
    local renamed, rename_err = os.rename(tmp, LAST_RECORD_FILE)
    if not renamed then os.remove(tmp) end
    return renamed, rename_err
end

local function due(now, interval)
    local elapsed = now - read_last_record_time()
    return not (elapsed >= 0 and elapsed < interval)
end

-- Check the interval inside the writer lock, then retain it through the
-- append and any rotation. Piggyback claims still stamp before sampling so a
-- failed sample waits for the next interval; manual records stamp on success.
local function record(interval, stamp_before)
    return with_record_lock(function()
        local now = os.time()
        local elapsed = now - read_last_record_time()
        if elapsed >= 0 and elapsed < interval then
            return {status = "skipped", reason = "rate_limited", retry_after = interval - elapsed}
        end
        if stamp_before then
            local stamped, stamp_err = write_last_record_time(now)
            if not stamped then
                return {error = "Recording failed", detail = "Cannot save recording time: " .. tostring(stamp_err)}
            end
        end
        local snapshot = build_record()
        local written, write_err = append_record(snapshot)
        if not written then return {error = "Recording failed", detail = write_err} end
        local stamped, stamp_err = true, nil
        if not stamp_before then stamped, stamp_err = write_last_record_time(now) end
        local result = {status = "ok", record = snapshot}
        if write_err then
            result.warning, result.detail = "History rotation failed", tostring(write_err)
            io.stderr:write("[dashboard] history rotation FAILED: " .. result.detail .. "\n")
        end
        if not stamped then
            result.warning = (result.warning and result.warning .. "; " or "") .. "History timestamp update failed"
            result.detail = (result.detail and result.detail .. "; " or "") .. tostring(stamp_err)
            io.stderr:write("[dashboard] history timestamp update FAILED: " .. tostring(stamp_err) .. "\n")
        end
        return result
    end)
end

-- Called on every API request (handler.lua); records if RECORD_INTERVAL has
-- elapsed since the last append. The request that trips this pays ~1 s of
-- sampling plus the pings before it is answered. A failed record is not
-- retried until the next interval.
function M.maybe_record()
    if not metrics then return end
    if not due(os.time(), RECORD_INTERVAL) then return end
    local result = record(RECORD_INTERVAL, true)
    if result.error then
        io.stderr:write("[dashboard] history recording FAILED: " .. tostring(result.detail) .. "\n")
    end
end

-- POST /api/history/record (cron and manual). Rate-limited via the same
-- timestamp file.
function M.record_now()
    if not metrics then return {error = "not initialized"} end
    return record(MIN_MANUAL_INTERVAL, false)
end

-- Compute local-to-UTC offset in seconds (positive if east of UTC)
local function tz_offset()
    local now = os.time()
    return now - os.time(os.date("!*t", now))
end

-- Read history (GET /api/history?hours=N)
-- Records are appended chronologically, so read a window from the end of the
-- file and double it until the oldest record predates the cutoff, or the
-- router's read cap is reached. truncated tells the client the range is partial.
function M.get(hours)
    hours = math.max(0.1, math.min(hours or 24, 8760))
    local cutoff = os.time() - (hours * 3600)
    local offset = tz_offset()

    local f = io.open(HISTORY_FILE, "rb")
    if not f then return {records = {}, count = 0, skipped_lines = 0, truncated = false} end

    local jsonc = require("luci.jsonc")
    local size, size_err = f:seek("end")
    if not size then f:close() error(tostring(size_err)) end
    local read_size = math.min(INITIAL_TAIL_READ, M.max_read_bytes)

    while true do
        local start = math.max(0, size - read_size)
        local position, seek_err = f:seek("set", start)
        if not position then f:close() error(tostring(seek_err)) end
        local data, read_err = f:read(size - start)
        if read_err then f:close() error(tostring(read_err)) end
        data = data or ""
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
                if type(record) == "table" and type(record.timestamp) == "string" then
                    -- Parse ISO timestamp (UTC) to epoch for filtering
                    local y, mo, d, h, mi, s = record.timestamp:match(
                        "(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
                    if y then
                        -- os.time reads the fields as local time; add the offset for
                        -- the UTC epoch. isdst=false like tz_offset's os.date("!*t"):
                        -- without it a DST zone parsed every record an hour early.
                        ts = os.time({year=tonumber(y), month=tonumber(mo),
                            day=tonumber(d), hour=tonumber(h), min=tonumber(mi), sec=tonumber(s),
                            isdst=false})
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

        local covered = start == 0 or (first_ts and first_ts < cutoff)
        if covered or read_size >= M.max_read_bytes then
            f:close()
            if skipped > 0 then
                io.stderr:write("[dashboard] history read: skipped " .. skipped .. " malformed lines\n")
            end
            return {records = records, count = #records, skipped_lines = skipped, truncated = not covered}
        end
        read_size = math.min(read_size * 2, M.max_read_bytes)
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
