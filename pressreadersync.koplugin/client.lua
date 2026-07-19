local http = require("socket.http")
local ltn12 = require("ltn12")
local rapidjson = require("rapidjson")
local socket = require("socket")
local socketutil = require("socketutil")

local Client = {}
Client.__index = Client

local function cleanBaseUrl(value)
    value = (value or ""):gsub("%s+$", ""):gsub("^%s+", "")
    return value:gsub("/+$", "")
end

local function urlEncode(value)
    return (tostring(value):gsub("([^%w%-_%.~])", function(char)
        return string.format("%%%02X", string.byte(char))
    end))
end

function Client:new(options)
    options = options or {}
    return setmetatable({
        base_url = cleanBaseUrl(options.base_url),
        token = options.token or "",
    }, self)
end

function Client:_url(path)
    if path:match("^https?://") then
        return path
    end
    return self.base_url .. (path:sub(1, 1) == "/" and path or "/" .. path)
end

function Client:_headers()
    local headers = {
        ["Accept"] = "application/json",
        ["Accept-Encoding"] = "identity",
    }
    if self.token ~= "" then
        headers["Authorization"] = "Bearer " .. self.token
    end
    return headers
end

function Client:get(path)
    if self.base_url == "" then
        return nil, "bridge URL is not configured"
    end
    local sink = {}
    socketutil:set_timeout(socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)
    local code, _, status = socket.skip(1, http.request{
        url = self:_url(path),
        method = "GET",
        headers = self:_headers(),
        sink = socketutil.table_sink(sink),
    })
    socketutil:reset_timeout()
    if code ~= 200 then
        return nil, status or code or "network unreachable"
    end
    local payload, err = rapidjson.decode(table.concat(sink))
    if not payload then
        return nil, "invalid response: " .. tostring(err)
    end
    return payload
end

function Client:publications()
    local payload, err = self:get("/v1/publications")
    return payload and payload.publications, err
end

function Client:issues(publication_id)
    local payload, err = self:get("/v1/publications/" .. urlEncode(publication_id) .. "/issues")
    return payload and payload.issues, err
end

function Client:latest(publication_id)
    local payload, err = self:get("/v1/latest?publication=" .. urlEncode(publication_id))
    return payload and payload.issue, err
end

function Client:status()
    return self:get("/v1/status")
end

function Client:download(issue, destination)
    if self.base_url == "" then
        return nil, "bridge URL is not configured"
    end
    local handle, open_err = io.open(destination, "wb")
    if not handle then
        return nil, open_err
    end
    socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_TOTAL_TIMEOUT)
    local code, _, status = socket.skip(1, http.request{
        url = self:_url(issue.download_url),
        method = "GET",
        headers = self:_headers(),
        sink = socketutil.file_sink(handle),
    })
    socketutil:reset_timeout()
    if code ~= 200 then
        os.remove(destination)
        return nil, status or code or "network unreachable"
    end
    return true
end

Client.cleanBaseUrl = cleanBaseUrl
Client.urlEncode = urlEncode

return Client
