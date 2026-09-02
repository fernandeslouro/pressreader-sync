local http = require("socket.http")
local rapidjson = require("rapidjson")
local socket = require("socket")
local socketutil = require("socketutil")

local Client = {}
Client.__index = Client

local MAX_ATTEMPTS = 3
local RETRY_DELAY_SECONDS = 0.5
local RETRYABLE_HTTP_CODES = {
    [408] = true, [425] = true, [429] = true,
    [500] = true, [502] = true, [503] = true, [504] = true,
}
local RETRYABLE_ERROR_PARTS = {
    "wantread", "wantwrite", "timeout", "closed", "connection reset",
    "connection refused", "network is unreachable", "host is unreachable",
    "temporary failure", "not known", "unexpected eof",
}

local function requestError(code, status)
    return tostring(status or code or "network unreachable")
end

local function isRetryableFailure(code, status)
    local numeric_code = tonumber(code)
    if numeric_code then return RETRYABLE_HTTP_CODES[numeric_code] == true end

    local message = requestError(code, status):lower()
    for error_index, part in ipairs(RETRYABLE_ERROR_PARTS) do
        if message:find(part, 1, true) then return true end
    end
    return false
end

local function finalRequestError(code, status, attempts)
    local err = requestError(code, status)
    if attempts > 1 then
        return string.format("temporary network failure after %d attempts: %s", attempts, err)
    end
    return err
end

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

function Client:_performRequest(request, block_timeout, total_timeout)
    socketutil:set_timeout(block_timeout, total_timeout)
    local request_ok, result, code, _, status = pcall(http.request, request)
    socketutil:reset_timeout()

    if not request_ok then return nil, result end
    if result == nil then return nil, code or status or "network unreachable" end
    return code, status
end

function Client:_waitBeforeRetry(attempt)
    socket.sleep(RETRY_DELAY_SECONDS * attempt)
end

function Client:get(path)
    if self.base_url == "" then
        return nil, "bridge URL is not configured"
    end

    for attempt = 1, MAX_ATTEMPTS do
        local sink = {}
        local code, status = self:_performRequest({
            url = self:_url(path),
            method = "GET",
            headers = self:_headers(),
            sink = socketutil.table_sink(sink),
        }, socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)

        if code == 200 then
            local payload, err = rapidjson.decode(table.concat(sink))
            if not payload then
                return nil, "invalid response: " .. tostring(err)
            end
            return payload
        end
        if attempt == MAX_ATTEMPTS or not isRetryableFailure(code, status) then
            return nil, finalRequestError(code, status, attempt)
        end
        self:_waitBeforeRetry(attempt)
    end
end

function Client:post(path)
    if self.base_url == "" then
        return nil, "bridge URL is not configured"
    end

    for attempt = 1, MAX_ATTEMPTS do
        local sink = {}
        local headers = self:_headers()
        headers["Content-Length"] = "0"
        local code, status = self:_performRequest({
            url = self:_url(path),
            method = "POST",
            headers = headers,
            sink = socketutil.table_sink(sink),
        }, socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)

        if code == 200 or code == 202 then
            local payload, err = rapidjson.decode(table.concat(sink))
            if not payload then
                return nil, "invalid response: " .. tostring(err)
            end
            return payload
        end
        if attempt == MAX_ATTEMPTS or not isRetryableFailure(code, status) then
            return nil, finalRequestError(code, status, attempt)
        end
        self:_waitBeforeRetry(attempt)
    end
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

function Client:triggerAutomation()
    return self:post("/v1/automation/run")
end

function Client:download(issue, destination)
    if self.base_url == "" then
        return nil, "bridge URL is not configured"
    end

    for attempt = 1, MAX_ATTEMPTS do
        local handle, open_err = io.open(destination, "wb")
        if not handle then return nil, open_err end

        local code, status = self:_performRequest({
            url = self:_url(issue.download_url),
            method = "GET",
            headers = self:_headers(),
            sink = socketutil.file_sink(handle),
        }, socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_TOTAL_TIMEOUT)
        pcall(handle.close, handle)

        if code == 200 then return true end
        os.remove(destination)
        if attempt == MAX_ATTEMPTS or not isRetryableFailure(code, status) then
            return nil, finalRequestError(code, status, attempt)
        end
        self:_waitBeforeRetry(attempt)
    end
end

Client.cleanBaseUrl = cleanBaseUrl
Client.urlEncode = urlEncode
Client.isRetryableFailure = isRetryableFailure

return Client
