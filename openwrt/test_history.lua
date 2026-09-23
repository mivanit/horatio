-- Unit tests for history.lua M.get tail-reading.
--
-- Runs on plain Lua 5.1+ with no dependencies: luci.jsonc is shimmed with a
-- minimal parser sufficient for the flat fixture records used here.
--
-- Usage: lua5.1 test_history.lua
--   (or, where no standalone lua exists: luatex --luaonly test_history.lua)

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

local base = "/tmp/dashboard-history-test-" .. os.time()
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

print("\nhistory.lua M.get unit tests\n")

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

os.execute("rm -rf '" .. base .. "'")

print(string.format("\n%d passed, %d failed\n", passes, fails))
os.exit(fails == 0 and 0 or 1)
