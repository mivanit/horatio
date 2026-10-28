-- CGI response tests; no OpenWrt services or native modules are required.
-- Usage: lua5.1 tests/openwrt/test_handler.lua
package.preload["luci.jsonc"] = function()
    return {parse = function() return {} end}
end
package.preload["metrics"] = function()
    return {
        init = function() end,
        load = function() return {min1 = 0} end,
        cpu = function() error("injected metric failure") end,
        mem = function() return {total = 8589934592, used = 4294967296} end,
        memswap = function() return {total = 2147483648, used = 0} end,
        fs = function() return {{mnt_point = "/mnt/data", size = 8589934592, used = 4294967296}} end,
    }
end
package.preload["history"] = function()
    return {init = function() end, maybe_record = function() end}
end

local output = {}
uhttpd = {send = function(s) output[#output + 1] = s end}
local script_dir = (arg and arg[0] or ""):match("(.*)[/\\][^/\\]*$") or "."
script_dir = script_dir .. "/../../openwrt"
dofile(script_dir .. "/handler.lua")

local failed = 0
for _, case in ipairs({
    {"GET", "/4/load", 200, "OK"},
    {"GET", "/missing", 404, "Not Found"},
    {"GET", "/history/record", 405, "Method Not Allowed"},
    {"POST", "/history/settings", 501, "Not Implemented"},
    {"GET", "/4/cpu", 500, "Internal Server Error"},
    -- Byte totals cross OpenWrt Lua's signed 32-bit integer boundary. These
    -- must remain exact JSON numbers for memory, swap, and storage cards.
    {"GET", "/4/mem", 200, "OK", {'"total":8589934592', '"used":4294967296'}},
    {"GET", "/4/memswap", 200, "OK", {'"total":2147483648', '"used":0'}},
    {"GET", "/4/fs", 200, "OK", {'"size":8589934592', '"used":4294967296'}},
}) do
    output = {}
    handle_request({REQUEST_METHOD = case[1], PATH_INFO = case[2]})
    local response = table.concat(output)
    -- uhttpd accepts the status only when a space follows its three digits.
    local code, reason = response:match("^Status: (%d%d%d) ([^\r\n]+)\r\n")
    local ok = tonumber(code) == case[3] and reason == case[4]
        and response:find("Content-Type: application/json\r\n", 1, true)
    local body = response:match("\r\n\r\n(.*)") or ""
    -- A failure after the first headers must not masquerade as HTTP 200.
    ok = ok and not body:find("Status:", 1, true)
    for _, fragment in ipairs(case[5] or {}) do
        ok = ok and body:find(fragment, 1, true) ~= nil
    end
    if not ok then failed = failed + 1 end
    print((ok and "PASS " or "FAIL ") .. case[1] .. " " .. case[2]
        .. " -> " .. tostring(code) .. " " .. tostring(reason))
end
os.exit(failed == 0 and 0 or 1)
