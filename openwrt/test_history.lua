-- Unit tests for history.lua reading, recording, and rotation.
--
-- Requires Lua 5.1 and native nixio (as installed on OpenWrt). luci.jsonc is
-- shimmed with a minimal parser sufficient for the flat fixture records.
-- Writer tests fork real processes because lockf ownership is per-process.
--
-- Usage: lua5.1 openwrt/test_history.lua
-- Also run it under a DST zone, e.g. TZ=America/New_York in summer (test 9).

local passes, fails = 0, 0

local function check(cond, label, detail)
    local suffix = detail and ("  " .. detail) or ""
    if cond then
        passes = passes + 1
        print("  PASS  " .. label .. suffix)
    else
        fails = fails + 1
        print("  FAIL  " .. label .. suffix)
    end
end

-- Minimal luci.jsonc shim: parses flat objects with string/number values
-- (all the fixtures need). Returns nil for anything else, like the real
-- library does for invalid input.
package.preload["luci.jsonc"] = function()
    local J = {}
    function J.parse(s)
        if type(s) ~= "string" then return nil end
        local body = s:match("^%s*{(.*)}%s*$")
        if not body then return nil end
        local t = {}
        for k, v in body:gmatch('"([^"]-)"%s*:%s*([^,]+)') do
            v = v:match("^%s*(.-)%s*$")
            local str = v:match('^"(.*)"$')
            if str then
                t[k] = str
            else
                local n = tonumber(v)
                if n == nil then return nil end
                t[k] = n
            end
        end
        if next(t) == nil then return nil end
        return t
    end
    function J.stringify()
        error("stringify not needed in this test")
    end
    return J
end

local script_dir = (arg and arg[0] or ""):match("(.*)[/\\][^/\\]*$") or "."
package.path = script_dir .. "/?.lua;" .. package.path
local history = require("history")

local base = os.tmpname()  -- unique per run (os.time() collided between quick runs)
os.remove(base)
history.init({}, nil, base)  -- sets HISTORY_FILE = base .. "/logs/history.jsonl"
local HIST = base .. "/logs/history.jsonl"

local function write_file(content)
    local f = assert(io.open(HIST, "w"))
    f:write(content)
    f:close()
end

local function iso(age_hours)
    return os.date("!%Y-%m-%dT%H:%M:%S+00:00", os.time() - math.floor(age_hours * 3600))
end

local function line(age_hours, pad)
    local s = '{"timestamp": "' .. iso(age_hours) .. '", "v": 1'
    if pad then s = s .. ', "pad": "' .. string.rep("x", pad) .. '"' end
    return s .. "}\n"
end

print("\nhistory.lua unit tests\n")

-- 1. Missing file
local r = history.get(24)
check(r.count == 0 and r.skipped_lines == 0, "missing file -> 0 records")

-- 2. Empty file
write_file("")
r = history.get(24)
check(r.count == 0 and r.skipped_lines == 0, "empty file -> 0 records")

-- 3. Old + in-range records
do
    local parts = {}
    for i = 0, 9 do parts[#parts + 1] = line(100 - i) end  -- 100h..91h old
    for i = 0, 4 do parts[#parts + 1] = line(5 - i) end    -- 5h..1h old
    write_file(table.concat(parts))
end
r = history.get(24)
check(r.count == 5 and r.skipped_lines == 0,
    "old + in-range -> only in-range", "count=" .. r.count)
local chronological = true
for i = 2, #r.records do
    if r.records[i].timestamp < r.records[i - 1].timestamp then chronological = false end
end
check(chronological, "records are chronological")

-- 4. Torn last line (no trailing newline) is skipped and counted
write_file(line(30) .. line(2) .. '{"timestamp": "2026-')
r = history.get(24)
check(r.count == 1 and r.skipped_lines == 1,
    "torn last line skipped and counted",
    "count=" .. r.count .. " skipped=" .. r.skipped_lines)

-- 5. Malformed mid-file lines are skipped and counted, neighbors kept
write_file(line(3) .. "not json\n" .. "42\n" .. line(2))
r = history.get(24)
check(r.count == 2 and r.skipped_lines == 2,
    "malformed mid-file lines skipped and counted",
    "count=" .. r.count .. " skipped=" .. r.skipped_lines)

-- 6. A far-past record between in-range ones is excluded, not truncating
write_file(line(4) .. line(3) .. line(90000) .. line(2) .. line(1))
r = history.get(24)
check(r.count == 4 and r.skipped_lines == 0,
    "ancient mid-tail record excluded without truncation", "count=" .. r.count)

-- 7. Doubling: in-range records extend beyond the 1 MiB initial window
do
    local parts = {}
    for i = 1, 4000 do parts[#parts + 1] = line(20 * (4000 - i) / 4000 + 0.5, 400) end
    write_file(table.concat(parts))
end
r = history.get(24)
check(r.count == 4000 and r.skipped_lines == 0,
    "window doubles until range is covered (>1MiB in-range)", "count=" .. r.count)

-- 8. Record larger than the initial window (no newline in window)
write_file(line(30) .. line(1, 1200000))
r = history.get(24)
check(r.count == 1 and r.skipped_lines == 0
    and r.records[1].pad and #r.records[1].pad == 1200000,
    "record larger than window parsed after growth", "count=" .. r.count)

-- 9. A 1 h window holds the records from the last hour. Run under a DST zone
--    (TZ=America/New_York in summer) to exercise it: os.time read the record
--    fields with DST while the offset was computed without, an hour early.
do
    local parts = {}
    for _, m in ipairs({70, 40, 30, 20, 10}) do parts[#parts + 1] = line(m / 60) end
    write_file(table.concat(parts))
end
r = history.get(1)
check(r.count == 4, "1 h window holds the last hour's records (TZ=" .. tostring(os.getenv("TZ")) .. ")",
    "count=" .. r.count)

-- Keep the writer fixtures small; only the locking and filesystem operations
-- are real. loadfile works on Lua 5.1 without a string-reader compatibility shim.
local nixio = require("nixio")
local function metrics_stub()
    return {
        sample = function() return {cpu = {total = 1, cores = {}}, network = {}} end,
        mem = function() return {} end, memswap = function() return {} end,
        fs = function() return {} end, sensors = function() return {} end,
        load = function() return {min1 = 0, min5 = 0, min15 = 0} end,
        ping = function() return {} end,
    }
end
local jsonc = require("luci.jsonc")
jsonc.stringify = function(t)
    return '{"timestamp": "' .. t.timestamp .. '", "v": 1}'
end
local function quote(s) return "'" .. s:gsub("'", "'\\''") .. "'" end
local function contents(path)
    if not path then return "" end
    local f = io.open(path, "rb")
    if not f then return "" end
    local data = f:read("*a") or ""
    f:close()
    return data
end
local function write(path, data)
    local f = assert(io.open(path, "wb"))
    assert(f:write(data))
    assert(f:close())
end
local function count_records(path)
    local _, n = contents(path):gsub("\n", "")
    return n
end
local function fixture(name)
    local dir, m = base .. "/" .. name, metrics_stub()
    local h = assert(loadfile(script_dir .. "/history.lua"))()
    h.init({}, m, dir, dir)
    return h, m, dir .. "/logs/history.jsonl", dir
end
local function busy(result)
    return type(result) == "table" and result.status == "skipped" and result.reason == "busy"
end
local function in_child(fn)
    -- Flush before fork so a child's normal exit cannot duplicate prior output.
    io.stdout:flush(); io.stderr:flush()
    local pid = assert(nixio.fork())
    if pid == 0 then
        local ok, err = pcall(fn)
        if not ok then io.stderr:write("child assertion: " .. tostring(err) .. "\n") end
        os.exit(ok and 0 or 1)
    end
    local waited, state, code = nixio.waitpid(pid)
    return waited == pid and state == "exited" and code == 0
end
local function archives(dir)
    local p = assert(io.popen("ls " .. quote(dir .. "/logs") .. "/history-*.jsonl.gz 2>/dev/null"))
    local paths = {}
    for path in p:lines() do paths[#paths + 1] = path end
    p:close()
    return paths
end
local function archived(path)
    local p = assert(io.popen("gzip -dc " .. quote(path)))
    local data = p:read("*a") or ""
    p:close()
    return data
end
local function rotation_input()
    local parts = {}
    for i = 1, 60 do parts[#parts + 1] = line(10 - i / 10, 40) end
    return table.concat(parts)
end
local function wrap_file(f, overrides)
    return setmetatable({}, {__index = function(_, key)
        if overrides[key] then return overrides[key] end
        return function(_, ...) return f[key](f, ...) end
    end})
end

-- 10. A storage limit does not bound read-time memory. Instrument the actual
-- reads, with a small test cap, and check both covered and truncated windows.
do
    local h, _, path = fixture("read-cap")
    local parts = {}
    for i = 1, 100 do parts[#parts + 1] = line(1, 100) end
    write(path, table.concat(parts))
    h.max_read_bytes = 2048
    local open, largest = io.open, 0
    io.open = function(name, mode)
        local f, err = open(name, mode)
        if f and name == path and mode == "rb" then
            return wrap_file(f, {read = function(_, n)
                if type(n) == "number" then largest = math.max(largest, n) end
                return f:read(n)
            end})
        end
        return f, err
    end
    local ok, result = pcall(h.get, 24)
    io.open = open
    check(ok and result.truncated == true and result.count > 0 and result.count < 100
        and largest <= h.max_read_bytes,
        "history reads stop at the byte cap and report truncation", "largest read=" .. largest)
    write(path, line(48, 5000) .. line(30) .. line(1))
    result = h.get(24)
    check(result.count == 1 and result.truncated == false,
        "a capped tail covering the cutoff is complete")
    write(path, line(1, 5000))
    result = h.get(24)
    check(result.count == 0 and result.truncated == true,
        "a record larger than the read cap is reported as truncated")
end

-- 11. Test both entry points with a real competing process at the sampling
-- boundary. The contender must not sample, append, or steal a live lock.
for _, method in ipairs({"record_now", "maybe_record"}) do
    local h, m, path, dir = fixture("sample-" .. method)
    local sample, blocked = m.sample, false
    m.sample = function()
        blocked = in_child(function()
            m.sample = function() error("contender sampled under another process's lock") end
            assert(busy(h.record_now()))
            h.maybe_record()
            assert(count_records(path) == 0)
        end)
        return sample()
    end
    h[method]()
    m.sample = sample
    check(blocked and count_records(path) == 1,
        method .. " holds the kernel lock throughout sampling")
    h.maybe_record()
    check(count_records(path) == 1, method .. " permits only one piggyback record per interval")
    os.remove(dir .. "/dashboard-last-record")
    local retried = in_child(function() assert(h.record_now().status == "ok") end)
    check(retried and count_records(path) == 2,
        "another process records after " .. method .. " releases the lock")
end

-- 12. Kill a recorder while it holds the lock. No timestamp/PID cleanup or
-- waiting period should be needed: the kernel releases ownership on death.
do
    local h, m, path = fixture("killed-writer")
    local reader, writer = assert(nixio.pipe())
    io.stdout:flush(); io.stderr:flush()
    local pid = assert(nixio.fork())
    if pid == 0 then
        reader:close()
        m.sample = function()
            writer:write("R"); writer:close()
            nixio.nanosleep(30)  -- parent kills immediately after the ready byte
            error("parent failed to kill the test recorder")
        end
        h.record_now()
        os.exit(1)
    end
    writer:close()
    local ready = reader:read(1)
    reader:close()
    local held = ready == "R" and busy(h.record_now())
    nixio.kill(pid, 9)
    local waited, state = nixio.waitpid(pid)
    local retry = h.record_now()
    check(held and waited == pid and state == "signaled" and retry.status == "ok"
        and count_records(path) == 1,
        "a killed recorder releases its kernel lock immediately")
end

-- 13. The original loss occurred after copying the tail but before rename.
-- A child tries both recording paths exactly there; archive + live must retain
-- every acknowledged record, including one recorded after the rotation ends.
do
    local h, m, path, dir = fixture("rotate-race")
    local original = rotation_input()
    write(path, original)
    h.max_file_bytes = 3000
    local rename, boundaries, blocked = os.rename, 0, false
    os.rename = function(src, dest)
        if dest == path then
            boundaries = boundaries + 1
            blocked = in_child(function()
                m.sample = function() error("contender sampled during rotation") end
                assert(busy(h.record_now()))
                h.maybe_record()
                assert(contents(path):sub(1, #original) == original)
            end)
        end
        return rename(src, dest)
    end
    local ok, result = pcall(h.record_now)
    os.rename = rename
    local list = archives(dir)
    local old = #list == 1 and archived(list[1]) or ""
    local appended = ok and result.record and jsonc.stringify(result.record) .. "\n" or ""
    check(ok and result.status == "ok" and not result.warning and boundaries == 1 and blocked,
        "manual and piggyback writers cannot enter the rotation commit boundary")
    check(#list == 1 and old .. contents(path) == original .. appended,
        "rotation preserves every record across the archive and live file")
    os.remove(dir .. "/dashboard-last-record")
    local retry = h.record_now()
    check(retry.status == "ok" and old .. contents(path) == original .. appended
        .. jsonc.stringify(retry.record) .. "\n",
        "a writer blocked by rotation can retry without losing either record")
end

-- 14. Failed samples, serialization, and appends release the actual kernel
-- lock. Retrying in a child prevents same-process lock ownership masking leaks.
for _, failure in ipairs({"sampling", "serialization", "full-device"}) do
    local h, m, path = fixture("failure-" .. failure)
    local sample, stringify = m.sample, jsonc.stringify
    if failure == "sampling" then
        m.sample = function() error("injected sample failure") end
    elseif failure == "serialization" then
        jsonc.stringify = function() error("injected JSON failure") end
    else
        os.execute("ln -s /dev/full " .. quote(path))
    end
    local ok, result = pcall(h.record_now)
    m.sample, jsonc.stringify = sample, stringify
    if failure == "full-device" then os.remove(path) end
    local retried = in_child(function() assert(h.record_now().status == "ok") end)
    check(ok and result.error ~= nil and retried and count_records(path) == 1,
        failure .. " failure is reported and releases ownership for another process")
end

-- 15. An append is already durable when rotation starts. Every failure below
-- must preserve that append, roll back its new archive, close its handles, and
-- report a warning instead of encouraging a duplicate manual retry.
for _, failure in ipairs({"read-error", "early-eof", "tail-read-error", "tail-open", "tail-write", "tail-close", "rename"}) do
    local h, _, path, dir = fixture("rotation-" .. failure)
    local original = rotation_input()
    write(path, original)
    h.max_file_bytes = 3000
    local open, rename, injected, closed = io.open, os.rename, false, false
    local numeric_reads = 0
    io.open = function(name, mode)
        if failure == "tail-open" and name == path .. ".tmp" then
            injected = true
            return nil, "injected tail open failure"
        end
        local f, err = open(name, mode)
        if f and name == path and mode == "rb"
            and (failure == "read-error" or failure == "early-eof" or failure == "tail-read-error") then
            return wrap_file(f, {read = function(_, n)
                if type(n) == "number" then numeric_reads = numeric_reads + 1 end
                local target = failure == "tail-read-error" and 2 or 1
                if type(n) == "number" and numeric_reads == target then
                    injected = true
                    if failure ~= "early-eof" then return nil, "injected source read failure" end
                    return nil
                end
                return f:read(n)
            end})
        elseif f and name == path .. ".tmp" then
            return wrap_file(f, {
                write = function(_, data)
                    if failure == "tail-write" then injected = true; return nil, "injected tail write failure" end
                    return f:write(data)
                end,
                close = function()
                    closed = true
                    local ok, err = f:close()
                    if failure == "tail-close" then injected = true; return nil, "injected tail close failure" end
                    return ok, err
                end,
            })
        end
        return f, err
    end
    os.rename = function(src, dest)
        if failure == "rename" and dest == path then
            injected = true
            return nil, "injected rename failure"
        end
        return rename(src, dest)
    end
    local ok, result = pcall(h.record_now)
    io.open, os.rename = open, rename
    local appended = ok and result.record and jsonc.stringify(result.record) .. "\n" or ""
    local temp = io.open(path .. ".tmp", "rb")
    local temp_absent = temp == nil
    if temp then temp:close() end
    check(ok and injected and result.status == "ok" and result.warning ~= nil
        and contents(path) == original .. appended and #archives(dir) == 0 and temp_absent
        and (failure ~= "tail-write" or closed),
        failure .. " during rotation preserves the append and removes archive/temp leftovers")
    local limited = in_child(function()
        local res = h.record_now()
        assert(res.status == "skipped" and res.reason == "rate_limited")
    end)
    check(limited, failure .. " retains the successful append's rate limit and releases the lock")
    os.remove(dir .. "/dashboard-last-record")
    local retry = h.record_now()
    local list = archives(dir)
    check(retry.status == "ok" and not retry.warning and #list == 1
        and archived(list[1]) .. contents(path) == original .. appended
            .. jsonc.stringify(retry.record) .. "\n",
        failure .. " can recover without duplicating an archive or losing records")
end

-- 16. Repeated rotations in one second must not overwrite an earlier archive.
do
    local h, _, path, dir = fixture("archive-collision")
    local date = os.date
    os.date = function(fmt, when)
        if fmt == "!-%Y%m%dT%H%M%SZ" then return date(fmt, 946684800) end
        return date(fmt, when)
    end
    h.max_file_bytes = 3000
    local original = rotation_input()
    write(path, original)
    local first = h.record_now()
    local list = archives(dir)
    local first_path = list[1]
    local first_bytes = first_path and contents(first_path) or ""
    local old_tail = contents(path)
    local extra = rotation_input()
    write(path, old_tail .. extra)
    os.remove(dir .. "/dashboard-last-record")
    local second = h.record_now()
    os.date = date
    list = archives(dir)
    local second_path
    for _, file in ipairs(list) do if file ~= first_path then second_path = file end end
    check(first.status == "ok" and second.status == "ok" and #list == 2
        and contents(first_path) == first_bytes and second_path ~= nil
        and archived(first_path) .. archived(second_path) .. contents(path)
            == original .. jsonc.stringify(first.record) .. "\n" .. extra
                .. jsonc.stringify(second.record) .. "\n",
        "two rotations in one second keep both archives without overwriting data")
end

-- 17. Exercise real gzip failures, including a pipe that closes before the
-- prefix fits into its buffer. Restore the usual CGI SIGPIPE disposition in
-- the child: requiring nixio once in a parent must not mask a broken pipe.
for _, failure in ipairs({"missing", "failing"}) do
    local h, _, path, dir = fixture("gzip-" .. failure)
    local original = rotation_input() .. string.rep(line(1, 2000), 100)
    write(path, original)
    h.max_file_bytes = 3000
    local bin = dir .. "/bin"
    os.execute("mkdir " .. quote(bin))
    if failure == "failing" then
        write(bin .. "/gzip", "#!/bin/sh\nexit 1\n")
        os.execute("chmod +x " .. quote(bin .. "/gzip"))
    end
    local survived = in_child(function()
        local old_path = os.getenv("PATH")
        assert(nixio.setenv("PATH", bin))
        assert(nixio.signal(nixio.const.SIGPIPE, "default"))
        local result = h.record_now()
        assert(nixio.setenv("PATH", old_path))
        assert(result.status == "ok" and result.warning ~= nil)
        assert(contents(path) == original .. jsonc.stringify(result.record) .. "\n")
        assert(#archives(dir) == 0)
    end)
    check(survived, failure .. " gzip returns a warning and preserves the appended history")
    os.remove(dir .. "/dashboard-last-record")
    local retry = h.record_now()
    check(retry.status == "ok" and not retry.warning,
        failure .. " gzip releases the lock so a later rotation succeeds")
end

os.execute("rm -rf " .. quote(base))
print(string.format("\n%d passed, %d failed\n", passes, fails))
os.exit(fails == 0 and 0 or 1)
