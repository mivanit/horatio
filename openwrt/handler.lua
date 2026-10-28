-- OpenWrt Dashboard API handler for uhttpd
-- Serves system metrics as JSON, matching the Glances-based API contract
-- so the existing frontend works unchanged.

-- debug.getinfo(1,"S").source returns the source file path prefixed with "@",
-- e.g. "@/root/horatio/openwrt/handler.lua".
-- :match("@(.*/)" strips the "@" prefix and filename, leaving the directory:
--   "/root/horatio/openwrt/"
-- We prepend this to package.path so require() finds metrics.lua and
-- history.lua next to this file, regardless of uhttpd's working directory.
local script_dir = debug.getinfo(1, "S").source:match("@(.*/)")
package.path = script_dir .. "?.lua;" .. package.path

local jsonc = require("luci.jsonc")
local metrics = require("metrics")
local history = require("history")

local dashboard_dir = script_dir .. ".."

-- Config loading
local function load_config()
    local f = io.open(dashboard_dir .. "/config.json", "r")
    if not f then return {} end
    local content = f:read("*a")
    f:close()
    return jsonc.parse(content) or {}
end

local config = load_config()
metrics.init(config)
history.init(config, metrics, dashboard_dir)

-- JSON encoder: use luci.jsonc for decoding, but we need a custom encoder
-- because luci.jsonc.stringify doesn't guarantee field ordering and may
-- not handle all our edge cases (NaN, empty arrays vs objects).
local json = {}

local function json_escape(s)
    return s:gsub('\\', '\\\\'):gsub('"', '\\"')
             :gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
             :gsub('[\1-\31]', function(c) return string.format('\\u%04x', c:byte()) end)
end

function json.encode(val)
    local t = type(val)
    if val == nil then return "null"
    elseif t == "boolean" then return val and "true" or "false"
    elseif t == "number" then
        if val ~= val or val == math.huge or val == -math.huge then return "null" end
        if val == math.floor(val) and math.abs(val) < 1e15 then
            -- OpenWrt's Lua uses 32-bit integers; byte totals can exceed 2 GiB.
            return string.format("%.0f", val)
        end
        return string.format("%.2f", val)
    elseif t == "string" then
        return '"' .. json_escape(val) .. '"'
    elseif t == "table" then
        local is_array = (#val > 0) or next(val) == nil
        if is_array and #val == 0 then return "[]" end
        if is_array then
            local parts = {}
            for i = 1, #val do parts[i] = json.encode(val[i]) end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local parts = {}
            for k, v in pairs(val) do
                parts[#parts + 1] = '"' .. json_escape(tostring(k)) .. '":' .. json.encode(v)
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    end
    return "null"
end

-- Parse query string into table
local function parse_query(qs)
    local params = {}
    if not qs or qs == "" then return params end
    for pair in qs:gmatch("[^&]+") do
        local key, val = pair:match("^(.-)=(.*)$")
        if key then params[key] = val end
    end
    return params
end

-- Send JSON response
local status_text = {
    [200] = "OK", [404] = "Not Found", [405] = "Method Not Allowed",
    [500] = "Internal Server Error", [501] = "Not Implemented"
}
local function send_json(data, status)
    status = status or 200
    -- uhttpd ignores a CGI Status header without the space/reason phrase.
    uhttpd.send("Status: " .. status .. " " .. (status_text[status] or "Unknown") .. "\r\n")
    uhttpd.send("Content-Type: application/json\r\n")
    uhttpd.send("Cache-Control: no-cache\r\n")
    uhttpd.send("\r\n")
    uhttpd.send(json.encode(data))
end

-- Route table
local routes = {}

-- Live metrics
routes["/4/cpu"]     = function() send_json(metrics.cpu()) end
routes["/4/percpu"]  = function() send_json(metrics.percpu()) end
routes["/4/mem"]     = function() send_json(metrics.mem()) end
routes["/4/memswap"] = function() send_json(metrics.memswap()) end
routes["/4/network"] = function() send_json(metrics.network()) end
routes["/4/fs"]      = function() send_json(metrics.fs()) end
routes["/4/sensors"] = function() send_json(metrics.sensors()) end
routes["/4/load"]    = function() send_json(metrics.load()) end
routes["/4/uptime"]  = function() send_json(metrics.uptime()) end
routes["/4/system"]  = function() send_json(metrics.system_info()) end

-- Static info
routes["/cpu-info"]  = function() send_json(metrics.cpu_info()) end
routes["/ip-info"]   = function() send_json(metrics.ip_info()) end
routes["/ping"]      = function() send_json(metrics.ping()) end

-- History
routes["/history"] = function(env)
    local params = parse_query(env.QUERY_STRING)
    local hours = tonumber(params.hours) or 24
    send_json(history.get(hours))
end

-- One record field over the range, [{timestamp, <name>}], like the Python
-- backend's /api/history/field/<name>: the frontend loads temps only from here.
routes["/history/field/temps"] = function(env)
    local params = parse_query(env.QUERY_STRING)
    local r = history.get(tonumber(params.hours) or 24)
    local slim = {}
    for i, rec in ipairs(r.records) do slim[i] = {timestamp = rec.timestamp, temps = rec.temps} end
    send_json({field = "temps", records = slim, count = #slim,
        skipped_lines = r.skipped_lines, truncated = r.truncated})
end

routes["/history/settings"] = function(env)
    if env.REQUEST_METHOD == "POST" then
        send_json({
            error = "Settings update not supported",
            detail = "The OpenWrt backend does not support runtime settings changes. Edit config.json and restart the service."
        }, 501)
    else
        send_json(history.get_settings())
    end
end

routes["/history/record"] = function(env)
    if env.REQUEST_METHOD == "POST" then
        send_json(history.record_now())
    else
        send_json({error = "Method not allowed"}, 405)
    end
end

-- Meta
routes["/list-endpoints"] = function()
    local endpoint_list = {}
    for path, _ in pairs(routes) do
        endpoint_list[#endpoint_list + 1] = path
    end
    table.sort(endpoint_list)
    send_json({ endpoints = endpoint_list, backend = "openwrt" })
end

-- Main request handler (called by uhttpd for every /api/* request)
function handle_request(env)
    -- Piggyback: record history if interval has elapsed
    history.maybe_record()

    local path = env.PATH_INFO or ""
    local handler = routes[path]
    if handler then
        local ok, err = pcall(handler, env)
        if not ok then
            send_json({error = "Internal server error", detail = tostring(err)}, 500)
        end
    else
        send_json({error = "Not found"}, 404)
    end
end
