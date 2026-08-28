local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Dispatcher = require("dispatcher")
local InfoMessage = require("ui/widget/infomessage")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local MultiInputDialog = require("ui/widget/multiinputdialog")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local lfs = require("libs/libkoreader-lfs")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template

local Client = require("client")

local PressReaderSync = WidgetContainer:extend{
    name = "pressreadersync",
    is_doc_only = false,
    settings_file = DataStorage:getSettingsDir() .. "/pressreadersync.lua",
    settings = nil,
    updated = nil,
    active_menu = nil,
}

local function readableSize(bytes)
    bytes = tonumber(bytes) or 0
    if bytes >= 1024 * 1024 then
        return string.format("%.1f MB", bytes / (1024 * 1024))
    elseif bytes >= 1024 then
        return string.format("%.0f KB", bytes / 1024)
    end
    return string.format("%d B", bytes)
end

local function safeFilename(value)
    value = (value or "edition"):gsub("[/\\:*?\"<>|%c]", "_")
    value = value:gsub("^%.*", ""):gsub("%s+$", "")
    return value ~= "" and value or "edition"
end

function PressReaderSync:init()
    self.settings = LuaSettings:open(self.settings_file)
    self:onDispatcherRegisterActions()
    self.ui.menu:registerToMainMenu(self)
end

function PressReaderSync:onDispatcherRegisterActions()
    Dispatcher:registerAction("pressreader_sync_browse", {
        category = "none", event = "PressReaderSyncBrowse", title = _("PressReader Sync: browse publications"), general = true,
    })
    Dispatcher:registerAction("pressreader_sync_download_all_latest", {
        category = "none", event = "PressReaderSyncDownloadAllLatest",
        title = _("PressReader Sync: download all latest editions"), general = true,
    })
end

function PressReaderSync:addToMainMenu(menu_items)
    menu_items.pressreader_sync = {
        text = _("PressReader Sync"),
        sorting_hint = "search",
        sub_item_table = {
            {
                text = _("Browse publications"),
                callback = function() self:onPressReaderSyncBrowse() end,
            },
            {
                text = _("Download all latest editions"),
                callback = function() self:onPressReaderSyncDownloadAllLatest() end,
            },
            {
                text = _("Downloaded editions"),
                callback = function() self:showDownloads() end,
            },
            {
                text = _("Synchronization status"),
                callback = function() self:showSynchronizationStatus() end,
            },
            {
                text = _("Settings"),
                callback = function() self:showSettings() end,
            },
            {
                text = _("About PressReader Sync"),
                callback = function()
                    UIManager:show(InfoMessage:new{
                        text = _([[PressReader Sync is an unofficial PressReader integration for KOReader. It downloads authorised PDF, EPUB, CBZ, and DJVU files from your own bridge and does not decrypt PressReader files or bypass licensing.]]),
                    })
                end,
            },
        },
    }
end

function PressReaderSync:client()
    return Client:new{
        base_url = self.settings:readSetting("base_url", ""),
        token = self.settings:readSetting("token", ""),
    }
end

function PressReaderSync:downloadDirectory()
    return self.settings:readSetting("download_dir", DataStorage:getDataDir() .. "/pressreader-sync")
end

function PressReaderSync:withNetwork(callback)
    NetworkMgr:runWhenOnline(callback)
end

function PressReaderSync:showError(err)
    UIManager:show(InfoMessage:new{
        text = T(_("PressReader Sync could not complete the request:\n%1"), tostring(err or _("Unknown error"))),
        icon = "notice-warning",
    })
end

function PressReaderSync:showBusy(text, action)
    local message = InfoMessage:new{ text = text }
    UIManager:show(message)
    UIManager:forceRePaint()
    local ok, result, err = pcall(action)
    UIManager:close(message)
    if not ok then
        self:showError(result)
        return nil
    end
    if result == nil then
        self:showError(err)
        return nil
    end
    return result
end

function PressReaderSync:onPressReaderSyncBrowse()
    self:withNetwork(function()
        local publications = self:showBusy(_("Loading publications…"), function()
            return self:client():publications()
        end)
        if publications then self:showPublications(publications) end
    end)
end

function PressReaderSync:showPublications(publications)
    if #publications == 0 then
        UIManager:show(InfoMessage:new{ text = _("No publications found. Add files to the bridge library and try again.") })
        return
    end
    local items = {}
    for _, publication in ipairs(publications) do
        table.insert(items, {
            text = publication.title,
            mandatory = T(_("%1 · %2 editions"), publication.latest_date or "", publication.issue_count or 0),
            publication = publication,
        })
    end
    local menu
    menu = Menu:new{
        title = _("PressReader Sync publications"),
        subtitle = _("Tap to browse editions"),
        item_table = items,
        is_popout = false,
        is_borderless = true,
        title_bar_fm_style = true,
        onMenuSelect = function(menu_widget, item)
            self:loadIssues(item.publication)
        end,
    }
    self.active_menu = menu
    UIManager:show(menu)
end

function PressReaderSync:loadIssues(publication)
    self:withNetwork(function()
        local issues = self:showBusy(_("Loading editions…"), function()
            return self:client():issues(publication.id)
        end)
        if issues then self:showIssues(publication, issues) end
    end)
end

function PressReaderSync:showIssues(publication, issues)
    if #issues == 0 then
        UIManager:show(InfoMessage:new{ text = _("No editions found for this publication.") })
        return
    end
    local items = {}
    for issue_index, issue in ipairs(issues) do
        table.insert(items, {
            text = issue.title,
            mandatory = string.format("%s · %s · %s", issue.date or "", (issue.format or ""):upper(), readableSize(issue.size_bytes)),
            issue = issue,
        })
    end
    local menu
    menu = Menu:new{
        title = publication.title,
        subtitle = _("Tap to download and read"),
        item_table = items,
        is_popout = false,
        is_borderless = true,
        title_bar_fm_style = true,
        onMenuSelect = function(menu_widget, item) self:downloadAndOpen(item.issue) end,
    }
    if self.active_menu then UIManager:close(self.active_menu) end
    self.active_menu = menu
    UIManager:show(menu)
end

function PressReaderSync:onPressReaderSyncDownloadAllLatest()
    self:withNetwork(function()
        local publications = self:showBusy(_("Loading publications…"), function()
            return self:client():publications()
        end)
        if not publications then return end
        if #publications == 0 then
            UIManager:show(InfoMessage:new{ text = _("No publications found. Add files to the bridge library and try again.") })
            return
        end
        UIManager:show(ConfirmBox:new{
            text = T(_("Download the most recent edition of all %1 publications?"), #publications),
            ok_text = _("Download all"),
            ok_callback = function() self:downloadAllLatest(publications) end,
        })
    end)
end

function PressReaderSync:downloadAllLatest(publications)
    local ok, mkdir_err = util.makePath(self:downloadDirectory())
    if not ok then
        self:showError(mkdir_err or _("Could not create the download folder"))
        return
    end

    local result = self:showBusy(_("Downloading latest editions…"), function()
        local client = self:client()
        local summary = { downloaded = 0, skipped = 0, failed = 0, errors = {} }
        for _, publication in ipairs(publications) do
            local issue, latest_err = client:latest(publication.id)
            if not issue then
                summary.failed = summary.failed + 1
                table.insert(summary.errors, T(_("%1: %2"), publication.title, tostring(latest_err or _("Unknown error"))))
            else
                local destination = self:destinationFor(issue)
                local attributes = lfs.attributes(destination)
                local expected_size = tonumber(issue.size_bytes)
                local complete = attributes and attributes.mode == "file"
                    and (not expected_size or attributes.size == expected_size)
                if complete then
                    summary.skipped = summary.skipped + 1
                else
                    local downloaded, download_err = client:download(issue, destination)
                    if downloaded then
                        summary.downloaded = summary.downloaded + 1
                    else
                        summary.failed = summary.failed + 1
                        table.insert(summary.errors, T(_("%1: %2"), publication.title, tostring(download_err or _("Unknown error"))))
                    end
                end
            end
        end
        return summary
    end)
    if not result then return end

    local text = T(_("Latest editions: %1 downloaded, %2 already present, %3 failed."),
        result.downloaded, result.skipped, result.failed)
    if #result.errors > 0 then
        text = text .. "\n\n" .. table.concat(result.errors, "\n")
    end
    UIManager:show(InfoMessage:new{
        text = text,
        icon = result.failed > 0 and "notice-warning" or nil,
    })
end

function PressReaderSync:destinationFor(issue)
    local extension = (issue.format or "pdf"):lower():gsub("[^a-z0-9]", "")
    local stem = safeFilename(issue.title or issue.date or "edition")
    local suffix = tostring(issue.id or "edition"):sub(1, 8)
    return self:downloadDirectory() .. "/" .. stem .. "-" .. suffix .. "." .. extension
end

function PressReaderSync:downloadAndOpen(issue)
    local destination = self:destinationFor(issue)
    local attributes = lfs.attributes(destination)
    local expected_size = tonumber(issue.size_bytes)
    if attributes and attributes.mode == "file"
        and (not expected_size or attributes.size == expected_size) then
        self:openFile(destination)
        return
    end
    self:withNetwork(function()
        local ok, mkdir_err = util.makePath(self:downloadDirectory())
        if not ok then
            self:showError(mkdir_err or _("Could not create the download folder"))
            return
        end
        local downloaded = self:showBusy(T(_("Downloading %1…"), issue.title or _("edition")), function()
            return self:client():download(issue, destination)
        end)
        if downloaded then
            self:openFile(destination)
        end
    end)
end

function PressReaderSync:openFile(path)
    if self.active_menu then
        UIManager:close(self.active_menu)
        self.active_menu = nil
    end
    if self.ui.document then
        self.ui:switchDocument(path)
    else
        self.ui:openFile(path)
    end
end

function PressReaderSync:showDownloads()
    local directory = self:downloadDirectory()
    local items = {}
    if lfs.attributes(directory, "mode") == "directory" then
        for name in lfs.dir(directory) do
            if name ~= "." and name ~= ".." then
                local path = directory .. "/" .. name
                local attrs = lfs.attributes(path)
                if attrs and attrs.mode == "file" then
                    table.insert(items, { text = name, mandatory = readableSize(attrs.size), path = path, modified = attrs.modification })
                end
            end
        end
    end
    table.sort(items, function(a, b) return a.modified > b.modified end)
    if #items == 0 then
        UIManager:show(InfoMessage:new{ text = _("No downloaded editions yet.") })
        return
    end
    local menu
    menu = Menu:new{
        title = _("Downloaded editions"), item_table = items,
        is_popout = false, is_borderless = true, title_bar_fm_style = true,
        onMenuSelect = function(menu_widget, item) self:openFile(item.path) end,
        onMenuHold = function(menu_widget, item)
            UIManager:show(ConfirmBox:new{
                text = T(_("Delete %1?"), item.text), ok_text = _("Delete"),
                ok_callback = function()
                    os.remove(item.path)
                    for index, row in ipairs(items) do
                        if row.path == item.path then table.remove(items, index) break end
                    end
                    if #items == 0 then UIManager:close(menu) else menu:switchItemTable(nil, items) end
                end,
            })
            return true
        end,
    }
    self.active_menu = menu
    UIManager:show(menu)
end

function PressReaderSync:showSettings()
    local dialog
    dialog = MultiInputDialog:new{
        title = _("PressReader Sync settings"),
        fields = {
            { description = _("Bridge URL"), text = self.settings:readSetting("base_url", ""), hint = "http://192.168.1.20:8787" },
            { description = _("Access token"), text = self.settings:readSetting("token", ""), hint = _("Same token as the bridge") },
            { description = _("Download folder"), text = self:downloadDirectory(), hint = DataStorage:getDataDir() .. "/pressreader-sync" },
        },
        buttons = {
            {
                { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
                { text = _("Test"), callback = function()
                    local fields = dialog:getFields()
                    self:testSettings(fields[1], fields[2])
                end },
                { text = _("Save"), callback = function()
                    local fields = dialog:getFields()
                    local base_url = Client.cleanBaseUrl(fields[1])
                    if base_url ~= "" and not base_url:match("^https?://[^/]+") then
                        self:showError(_("Bridge URL must start with http:// or https://"))
                        return
                    end
                    self.settings:saveSetting("base_url", base_url)
                    self.settings:saveSetting("token", fields[2])
                    self.settings:saveSetting("download_dir", fields[3])
                    self.updated = true
                    UIManager:close(dialog)
                end },
            },
        },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function PressReaderSync:showSynchronizationStatus()
    self:withNetwork(function()
        local status = self:showBusy(_("Checking synchronization…"), function()
            return self:client():status()
        end)
        if not status then return end
        local automation = status.automation
        local text = T(_("Library: %1 publications, %2 editions"),
            status.publication_count or 0, status.issue_count or 0)
        if automation then
            text = text .. "\n\n" .. T(_("Automation: %1"), automation.state or _("unknown"))
            if automation.finished_at and automation.finished_at ~= "" then
                text = text .. "\n" .. T(_("Last check: %1"), automation.finished_at)
            end
            text = text .. "\n" .. T(_("Last run: %1 exported, %2 unchanged"),
                automation.exported or 0, automation.skipped or 0)
            if automation.errors and #automation.errors > 0 then
                text = text .. "\n\n" .. _("Error:") .. "\n" .. tostring(automation.errors[1])
            end
        else
            text = text .. "\n\n" .. _("No automation worker is connected.")
        end
        UIManager:show(InfoMessage:new{ text = text })
    end)
end

function PressReaderSync:testSettings(base_url, token)
    self:withNetwork(function()
        local status = self:showBusy(_("Testing bridge…"), function()
            local test_client = Client:new{ base_url = base_url, token = token }
            return test_client:status()
        end)
        if status then
            UIManager:show(InfoMessage:new{
                text = T(_("Connected: %1 publications, %2 editions"), status.publication_count or 0, status.issue_count or 0),
                timeout = 3,
            })
        end
    end)
end

function PressReaderSync:onFlushSettings()
    if self.updated then
        self.settings:flush()
        self.updated = nil
    end
end

return PressReaderSync
