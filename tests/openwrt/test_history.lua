-- Unit tests for history.lua reading, recording, and rotation.
--
-- Requires Lua 5.1 and native nixio (as installed on OpenWrt). luci.jsonc is
-- shimmed with a minimal parser sufficient for the flat fixture records.
-- Writer tests fork real processes because lockf ownership is per-process.
--
-- Usage: lua5.1 tests/openwrt/test_history.lua
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
-- (all the fixtures need). Check delimiters and unescaped fixture strings so
-- two fused records cannot be accepted as one string-valued record.
package.preload["luci.jsonc"] = function()
    local J = {}
    function J.parse(s)
        if type(s) ~= "string" then return nil end
        local body = s:match("^%s*{(.*)}%s*$")
        if not body then return nil end
        local t = {}
        while not body:match("^%s*$") do
            local k, value_start = body:match('^%s*"([^"\\%c]*)"%s*:%s*()')
            if not k then return nil end
            body = body:sub(value_start)
            local str, value_end = body:match('^"([^"\\%c]*)"()')
            if value_end then
                t[k] = str
            else
                local raw
                raw, value_end = body:match("^([^,%s]+)()")
                local n = tonumber(raw)
                if n == nil then return nil end
                t[k] = n
            end
            body = body:sub(value_end)
            if body:match("^%s*$") then break end
            body = body:match("^%s*,%s*(.+)$")
            if not body then return nil end
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
script_dir = script_dir .. "/../../openwrt"
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

-- 18. A full/unwritable tmpfs must not erase a previous timestamp or let every
-- poll record. Manual appends that already succeeded remain acknowledged, with
-- a warning about the failed timestamp update rather than a false append error.
for _, failure in ipairs({"open", "full-device", "close", "rename"}) do
    for _, method in ipairs({"maybe_record", "record_now"}) do
        local h, m, path, dir = fixture("stamp-" .. failure .. "-" .. method)
        local stamp = dir .. "/dashboard-last-record"
        local previous = tostring(os.time() - 3600) .. "\n"
        write(stamp, previous)
        local open, rename, sample = io.open, os.rename, m.sample
        local sampled, injected = 0, false
        m.sample = function() sampled = sampled + 1; return sample() end
        io.open = function(name, mode)
            if name == stamp .. ".tmp" and mode == "w" then
                injected = true
                if failure == "open" then return nil, "injected stamp open failure" end
                if failure == "full-device" then return open("/dev/full", "w") end
                local f, err = open(name, mode)
                if f and failure == "close" then
                    return wrap_file(f, {close = function()
                        f:close()
                        return nil, "injected stamp close failure"
                    end})
                end
                return f, err
            end
            return open(name, mode)
        end
        os.rename = function(src, dest)
            if failure == "rename" and dest == stamp then
                injected = true
                return nil, "injected stamp rename failure"
            end
            return rename(src, dest)
        end
        local ok, result = pcall(h[method])
        h.maybe_record() -- another ordinary poll must not add a record either
        io.open, os.rename = open, rename
        local expected = method == "record_now" and 1 or 0
        local acknowledged = method ~= "record_now" or (result.status == "ok"
            and result.record ~= nil and result.warning == "History timestamp update failed")
        local temp = io.open(stamp .. ".tmp", "r")
        local no_temp = temp == nil
        if temp then temp:close() end
        check(ok and injected and acknowledged and sampled == expected and count_records(path) == expected
            and contents(stamp) == previous and no_temp,
            method .. " preserves the previous timestamp on " .. failure .. " failure")
        local recovered = in_child(function() assert(h.record_now().status == "ok") end)
        check(recovered and count_records(path) == expected + 1 and contents(stamp) ~= previous,
            method .. " releases the lock and can recover from timestamp " .. failure .. " failure")
    end
end

-- 19. A short write or interrupted process may leave an unterminated final
-- line. Keep every existing byte and make the next acknowledged record readable.
for _, kind in ipairs({"complete", "partial-json", "partial-utf8"}) do
    local h, _, path, dir = fixture("torn-" .. kind)
    local tail = line(0.5):sub(1, -2)
    if kind == "partial-json" then tail = '{"timestamp":"' .. iso(0.5) .. '","v":' end
    if kind == "partial-utf8" then
        tail = '{"timestamp":"' .. iso(0.5) .. '","pad":"' .. string.char(0xe2, 0x82)
    end
    local original = line(1) .. tail
    write(path, original)
    local result = h.record_now()
    local after = original .. "\n" .. jsonc.stringify(result.record) .. "\n"
    local parsed = h.get(24)
    local expected = kind == "complete" and 3 or 2
    check(result.status == "ok" and contents(path) == after
        and parsed.count == expected and parsed.skipped_lines == (kind == "complete" and 0 or 1),
        kind .. " tail is preserved byte-for-byte and the next record is readable")
    os.remove(dir .. "/dashboard-last-record")
    result = h.record_now()
    parsed = h.get(24)
    check(result.status == "ok" and contents(path) == after .. jsonc.stringify(result.record) .. "\n"
        and parsed.count == expected + 1,
        kind .. " recovery leaves subsequent appends readable without extra separators")
end

-- Repeated interruptions must not alter earlier valid or partial lines either.
do
    local h, _, path, dir = fixture("torn-repeated")
    write(path, line(1) .. '{"timestamp":')
    local expected, succeeded = contents(path), true
    for i = 1, 2 do
        local result = h.record_now()
        expected = expected .. "\n" .. jsonc.stringify(result.record) .. "\n"
        succeeded = succeeded and result.status == "ok" and contents(path) == expected
        os.remove(dir .. "/dashboard-last-record")
        if i == 1 then
            local f = assert(io.open(path, "ab"))
            assert(f:write('{"v":')); assert(f:close())
            expected = expected .. '{"v":'
        end
    end
    local parsed = h.get(24)
    check(succeeded and parsed.count == 3 and parsed.skipped_lines == 2,
        "repeated torn tails retain all bytes and every acknowledged record")
end

-- 20. Uncertain inspection must not write even a separator. A retry in another
-- process also verifies that inspection failures release the writer lock.
for _, failure in ipairs({"size", "tail-seek", "read", "eof", "append-seek"}) do
    local h, _, path = fixture("inspect-" .. failure)
    local original = line(1):sub(1, -2)
    write(path, original)
    local open, injected, closed, wrote, seeks = io.open, false, false, false, 0
    io.open = function(name, mode)
        local f, err = open(name, mode)
        if f and name == path and mode == "a+" then
            return wrap_file(f, {
                seek = function(_, ...)
                    seeks = seeks + 1
                    local target = ({size = 1, ["tail-seek"] = 2, ["append-seek"] = 3})[failure]
                    if seeks == target then injected = true; return nil, "injected seek failure" end
                    return f:seek(...)
                end,
                read = function(_, ...)
                    if failure == "read" or failure == "eof" then
                        injected = true
                        if failure == "read" then return nil, "injected read failure" end
                        return nil
                    end
                    return f:read(...)
                end,
                write = function(_, ...) wrote = true; return f:write(...) end,
                close = function() closed = true; return f:close() end,
            })
        end
        return f, err
    end
    local ok, result = pcall(h.record_now)
    io.open = open
    local unchanged = contents(path) == original
    local recovered = in_child(function() assert(h.record_now().status == "ok") end)
    check(ok and injected and closed and not wrote and result.error ~= nil
        and unchanged and recovered and h.get(24).count == 2,
        failure .. " inspection failure preserves the tail and permits a safe retry")
end

do
    local h, _, path = fixture("torn-serialization")
    local original, stringify = line(1) .. '{"v":', jsonc.stringify
    write(path, original)
    jsonc.stringify = function() return nil end
    local result = h.record_now()
    jsonc.stringify = stringify
    check(result.error ~= nil and contents(path) == original,
        "serialization fails before any separator is appended to a torn tail")
end

-- 21. Installation paths may contain spaces and shell quotes. Initialization
-- must create the intended directory and leave any existing history intact.
do
    local h, m, path, dir = fixture("router's checkout")
    local result = h.record_now()
    local original = contents(path)
    check(result.status == "ok" and h.get(24).count == 1,
        "a quoted installation path can create and read its first history record")
    h.init({}, m, dir, dir)
    check(result.status == "ok" and contents(path) == original,
        "reinitializing a quoted installation path preserves existing history bytes")
end

os.execute("rm -rf " .. quote(base))
print(string.format("\n%d passed, %d failed\n", passes, fails))
os.exit(fails == 0 and 0 or 1)
