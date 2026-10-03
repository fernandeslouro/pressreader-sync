-- Run from the repository root with: lua tests/test_client.lua
local function check(options)
    local current_dns, cached_dns = 'first-network', 'first-network'
    local requests, refreshes, sleeps = 0, 0, 0
    local function refresh()
        refreshes = refreshes + 1
        if options.refresh_error then error('resolver unavailable') end
        cached_dns = current_dns
        return 0
    end
    local symbols = {}
    if options.symbol then symbols[options.symbol] = refresh end
    setmetatable(symbols, {__index=function() error('symbol unavailable') end})
    package.loaded.ffi = nil
    package.preload.ffi = function()
        if options.no_ffi then error('ffi unavailable') end
        return {os=options.os or 'Linux', C=symbols, cdef=function()
            if options.declaration_error then error('declaration unavailable') end
        end}
    end
    local function request(transport, req)
        requests = requests + 1
        assert(transport == (req.url:match('^https://') and 'https' or 'http'))
        if options.require_refresh and cached_dns ~= current_dns then
            return nil, 'temporary failure in name resolution'
        end
        if options.retry and requests == 1 then
            current_dns = 'third-network'
            return nil, 'connection reset'
        end
        assert(req.sink('payload'))
        assert(req.sink(nil))
        return 1, 200, {}, 'HTTP/1.1 200 OK'
    end
    package.loaded['socket.http'] = {request=function(req) return request('http',req) end}
    package.loaded['ssl.https'] = {request=function(req) return request('https',req) end}
    package.loaded.socket = {sleep=function() sleeps=sleeps+1 end}
    package.loaded.rapidjson = {decode=function(body)
        assert(body=='payload')
        return {publications={{id='publication'}}}
    end}
    package.loaded.socketutil = {
        set_timeout=function() end, reset_timeout=function() end,
        table_sink=function(t) return function(chunk) if chunk then t[#t+1]=chunk end return 1 end end,
        file_sink=function(f) return function(chunk) if chunk then f:write(chunk) else f:close() end return 1 end end,
    }
    local Client=dofile('pressreadersync.koplugin/client.lua')
    local client=Client:new{base_url='https://bridge.example'}
    current_dns='second-network'
    assert(client:publications(), 'request failed after network change')
    assert(sleeps==(options.retry and 1 or 0))
    client.base_url='http://bridge.example'
    current_dns='fourth-network'
    assert(client:publications(), 'HTTP request failed after network change')
    current_dns='fifth-network'
    local path=os.tmpname()
    assert(client:download({download_url='https://bridge.example/edition'},path))
    local f=assert(io.open(path)); assert(f:read('*a')=='payload'); f:close(); os.remove(path)
    if options.require_refresh then assert(refreshes==requests,'every attempt must refresh DNS') end
end

check{symbol='__res_init',require_refresh=true}
check{symbol='res_init',require_refresh=true,retry=true}
check{no_ffi=true}
check{os='OSX'}
check{}
check{declaration_error=true}
check{symbol='__res_init',refresh_error=true}
print('PASS: DNS refresh across network changes, retries, HTTPS downloads, and optional resolver APIs')
