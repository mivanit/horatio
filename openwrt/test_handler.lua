-- CGI response tests; no OpenWrt services or native modules are required.
-- Usage: lua5.1 openwrt/test_handler.lua
package.preload["luci.jsonc"] = function()
    return {parse = function() return {} end}
end
package.preload["metrics"] = function()
    return {
        init = function() end,
        load = function() return {min1 = 0} end,
        cpu = function() error("injected metric failure") end,
    }
end
package.preload["history"] = function()
    return {init = function() end, maybe_record = function() end}
end

local output = {}
uhttpd = {send = function(s) output[#output + 1] = s end}
local script_dir = (arg and arg[0] or ""):match("(.*)[/\\][^/\\]*$") or "."
dofile(script_dir .. "/handler.lua")

local failed = 0
for _, case in ipairs({
    {"GET", "/4/load", 200, "OK"},
    {"GET", "/missing", 404, "Not Found"},
    {"GET", "/history/record", 405, "Method Not Allowed"},
    {"POST", "/history/settings", 501, "Not Implemented"},
    {"GET", "/4/cpu", 500, "Internal Server Error"},
}) do
    output = {}
    handle_request({REQUEST_METHOD = case[1], PATH_INFO = case[2]})
    local response = table.concat(output)
    -- uhttpd accepts the status only when a space follows its three digits.
    local code, reason = response:match("^Status: (%d%d%d) ([^\r\n]+)\r\n")
    local ok = tonumber(code) == case[3] and reason == case[4]
        and response:find("Content-Type: application/json\r\n", 1, true)
    if not ok then failed = failed + 1 end
    print((ok and "PASS " or "FAIL ") .. case[1] .. " " .. case[2]
        .. " -> " .. tostring(code) .. " " .. tostring(reason))
end
os.exit(failed == 0 and 0 or 1)
