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

local function publicationNameFromDownloadedFilename(name)
    return name:match("^%d%d%d%d%-%d%d%-%d%d %- (.+)%-[%da-fA-F]+%.[%w]+$")
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
    Dispatcher:registerAction("pressreader_sync_remove_old", {
        category = "none", event = "PressReaderSyncRemoveOld",
        title = _("PressReader Sync: remove old editions"), general = true,
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
                text = _("Downloaded publications"),
                callback = function() self:showDownloads() end,
            },
            {
                text = _("Remove old editions"),
                callback = function() self:onPressReaderSyncRemoveOld() end,
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
    for publication_index, publication in ipairs(publications) do
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
        onMenuSelect = function(menu_widget, item) self:downloadAndOpen(item.issue, publication) end,
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

    local client = self:client()
    local summary = { downloaded = 0, skipped = 0, failed = 0, errors = {} }
    for publication_index, publication in ipairs(publications) do
        local progress = InfoMessage:new{
            text = T(_("Publication %1 of %2\n%3\n\nChecking and downloading the latest edition…"),
                publication_index, #publications, publication.title),
        }
        UIManager:show(progress)
        UIManager:forceRePaint()
        local item_ok, outcome, item_err = pcall(function()
            local issue, latest_err = client:latest(publication.id)
            if not issue then
                return nil, latest_err
            end
            local destination = self:destinationFor(issue)
            local attributes = lfs.attributes(destination)
            local expected_size = tonumber(issue.size_bytes)
            local complete = attributes and attributes.mode == "file"
                and (not expected_size or attributes.size == expected_size)
            if complete then
                self:rememberDownload(issue, publication, destination, attributes.modification)
                return "skipped"
            end
            local downloaded, download_err = client:download(issue, destination)
            if not downloaded then
                return nil, download_err
            end
            self:rememberDownload(issue, publication, destination, os.time())
            return "downloaded"
        end)
        UIManager:close(progress)

        if not item_ok then
            item_err = outcome
            outcome = nil
        end
        if outcome == "downloaded" then
            summary.downloaded = summary.downloaded + 1
        elseif outcome == "skipped" then
            summary.skipped = summary.skipped + 1
        else
            summary.failed = summary.failed + 1
            table.insert(summary.errors, T(_("%1: %2"), publication.title,
                tostring(item_err or _("Unknown error"))))
        end
    end

    local text = T(_("Latest editions: %1 downloaded, %2 already present, %3 failed."),
        summary.downloaded, summary.skipped, summary.failed)
    if #summary.errors > 0 then
        text = text .. "\n\n" .. table.concat(summary.errors, "\n")
    end
    UIManager:show(InfoMessage:new{
        text = text,
        icon = summary.failed > 0 and "notice-warning" or nil,
    })
end

function PressReaderSync:destinationFor(issue)
    local extension = (issue.format or "pdf"):lower():gsub("[^a-z0-9]", "")
    local stem = safeFilename(issue.title or issue.date or "edition")
    local suffix = tostring(issue.id or "edition"):sub(1, 8)
    return self:downloadDirectory() .. "/" .. stem .. "-" .. suffix .. "." .. extension
end

function PressReaderSync:downloadAndOpen(issue, publication)
    local destination = self:destinationFor(issue)
    local attributes = lfs.attributes(destination)
    local expected_size = tonumber(issue.size_bytes)
    if attributes and attributes.mode == "file"
        and (not expected_size or attributes.size == expected_size) then
        self:rememberDownload(issue, publication, destination, attributes.modification)
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
            self:rememberDownload(issue, publication, destination, os.time())
            self:openFile(destination)
        end
    end)
end

function PressReaderSync:downloadRecords()
    local records = self.settings:readSetting("download_records", {})
    return type(records) == "table" and records or {}
end

function PressReaderSync:rememberDownload(issue, publication, path, downloaded_at)
    local records = self:downloadRecords()
    local key = tostring(issue.id or path)
    records[key] = {
        issue_id = issue.id,
        issue_title = issue.title,
        issue_date = issue.date,
        format = issue.format,
        size_bytes = issue.size_bytes,
        publication_id = publication and publication.id or issue.publication_id,
        publication_title = publication and publication.title or issue.publication_title,
        path = path,
        downloaded_at = tonumber(downloaded_at) or os.time(),
    }
    self.settings:saveSetting("download_records", records)
    self.updated = true
end

function PressReaderSync:forgetDownload(path)
    local records = self:downloadRecords()
    local changed = false
    for key, record in pairs(records) do
        if type(record) == "table" and record.path == path then
            records[key] = nil
            changed = true
        end
    end
    if changed then
        self.settings:saveSetting("download_records", records)
        self.updated = true
    end
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

function PressReaderSync:retentionCount()
    local count = tonumber(self.settings:readSetting("editions_to_keep", 2))
    if not count or count < 1 then return 2 end
    return math.floor(count)
end

function PressReaderSync:oldEditionCandidates()
    local directory = self:downloadDirectory()
    local groups = {}
    local tracked_paths = {}
    if lfs.attributes(directory, "mode") ~= "directory" then
        return {}, 0
    end

    for _, record in pairs(self:downloadRecords()) do
        if type(record) == "table" and record.path and record.publication_title then
            local attributes = lfs.attributes(record.path)
            if attributes and attributes.mode == "file" then
                local publication_key = tostring(record.publication_id or record.publication_title)
                groups[publication_key] = groups[publication_key] or {}
                table.insert(groups[publication_key], {
                    name = record.issue_title or record.path:match("([^/]+)$") or record.path,
                    path = record.path,
                    modified = attributes.modification or 0,
                    issue_date = tostring(record.issue_date or ""),
                })
                tracked_paths[record.path] = true
            end
        end
    end
    for name in lfs.dir(directory) do
        if name ~= "." and name ~= ".." then
            local publication_name = publicationNameFromDownloadedFilename(name)
            local path = directory .. "/" .. name
            local attributes = not tracked_paths[path] and publication_name and lfs.attributes(path)
            if attributes and attributes.mode == "file" then
                local publication_key = "legacy:" .. publication_name
                groups[publication_key] = groups[publication_key] or {}
                table.insert(groups[publication_key], {
                    name = name, path = path, modified = attributes.modification or 0,
                    issue_date = name:match("^(%d%d%d%d%-%d%d%-%d%d)") or "",
                })
            end
        end
    end

    local keep = self:retentionCount()
    local candidates = {}
    local affected_publications = 0
    local current_file = self.ui.document and self.ui.document.file
    for _, editions in pairs(groups) do
        table.sort(editions, function(first, second)
            if first.issue_date ~= second.issue_date then return first.issue_date > second.issue_date end
            if first.name == second.name then return first.modified > second.modified end
            return first.name > second.name
        end)
        local publication_affected = false
        for edition_index = keep + 1, #editions do
            if editions[edition_index].path ~= current_file then
                table.insert(candidates, editions[edition_index])
                publication_affected = true
            end
        end
        if publication_affected then
            affected_publications = affected_publications + 1
        end
    end
    return candidates, affected_publications
end

function PressReaderSync:onPressReaderSyncRemoveOld()
    local candidates, affected_publications = self:oldEditionCandidates()
    if #candidates == 0 then
        UIManager:show(InfoMessage:new{
            text = T(_("Nothing to remove. Each publication already has at most %1 downloaded editions."),
                self:retentionCount()),
        })
        return
    end
    UIManager:show(ConfirmBox:new{
        text = T(_("Remove %1 old edition files from %2 publications?\n\nThe newest %3 editions of each publication will be kept."),
            #candidates, affected_publications, self:retentionCount()),
        ok_text = _("Remove old editions"),
        ok_callback = function() self:removeOldEditions(candidates) end,
    })
end

function PressReaderSync:removeOldEditions(candidates)
    local modules_ok, DocSettings, ReadCollection, ReadHistory = pcall(function()
        return require("docsettings"), require("readcollection"), require("readhistory")
    end)
    if not modules_ok then
        self:showError(DocSettings)
        return
    end
    local removed = 0
    local errors = {}
    for candidate_index, candidate in ipairs(candidates) do
        local ok, err = os.remove(candidate.path)
        if ok then
            removed = removed + 1
            self:forgetDownload(candidate.path)
            local housekeeping_ok, housekeeping_err = pcall(function()
                DocSettings.updateLocation(candidate.path)
                ReadHistory:removeItemByPath(candidate.path)
                ReadCollection:removeItem(candidate.path)
            end)
            if not housekeeping_ok then
                table.insert(errors, candidate.name .. ": " .. tostring(housekeeping_err))
            end
        else
            table.insert(errors, candidate.name .. ": " .. tostring(err or _("Unknown error")))
        end
    end
    local text = T(_("Removed %1 old edition files."), removed)
    if #errors > 0 then
        text = text .. "\n\n" .. table.concat(errors, "\n")
    end
    UIManager:show(InfoMessage:new{
        text = text,
        icon = #errors > 0 and "notice-warning" or nil,
    })
end

function PressReaderSync:showDownloads()
    local directory = self:downloadDirectory()
    local available = {}
    if lfs.attributes(directory, "mode") == "directory" then
        for name in lfs.dir(directory) do
            if name ~= "." and name ~= ".." then
                local path = directory .. "/" .. name
                local attrs = lfs.attributes(path)
                if attrs and attrs.mode == "file" then
                    available[path] = { name = name, attributes = attrs }
                end
            end
        end
    end

    local groups_by_id = {}
    local tracked_paths = {}
    local records = self:downloadRecords()
    local records_changed = false
    local stale_record_keys = {}
    for key, record in pairs(records) do
        local file = type(record) == "table" and available[record.path]
        if not file then
            table.insert(stale_record_keys, key)
        elseif record.publication_title and record.publication_title ~= "" then
            tracked_paths[record.path] = true
            local group_id = tostring(record.publication_id or record.publication_title)
            local group = groups_by_id[group_id]
            if not group then
                group = {
                    title = record.publication_title,
                    latest_date = "",
                    last_downloaded = 0,
                    editions = {},
                }
                groups_by_id[group_id] = group
            end
            local downloaded_at = tonumber(record.downloaded_at)
                or file.attributes.modification or 0
            local issue_date = tostring(record.issue_date or "")
            group.last_downloaded = math.max(group.last_downloaded, downloaded_at)
            if issue_date > group.latest_date then group.latest_date = issue_date end
            table.insert(group.editions, {
                text = record.issue_title or file.name,
                mandatory = string.format("%s · %s · %s", issue_date,
                    tostring(record.format or ""):upper(), readableSize(file.attributes.size)),
                path = record.path,
                downloaded_at = downloaded_at,
            })
        end
    end
    for _, key in ipairs(stale_record_keys) do
        records[key] = nil
        records_changed = true
    end
    if records_changed then
        self.settings:saveSetting("download_records", records)
        self.updated = true
    end

    local other_group
    for path, file in pairs(available) do
        if not tracked_paths[path] then
            if not other_group then
                other_group = {
                    title = _("Other downloads"), latest_date = "",
                    last_downloaded = 0, editions = {}, is_other = true,
                }
            end
            local modified = file.attributes.modification or 0
            other_group.last_downloaded = math.max(other_group.last_downloaded, modified)
            table.insert(other_group.editions, {
                text = file.name,
                mandatory = readableSize(file.attributes.size),
                path = path,
                downloaded_at = modified,
            })
        end
    end

    local groups = {}
    for _, group in pairs(groups_by_id) do table.insert(groups, group) end
    if other_group then table.insert(groups, other_group) end
    if #groups == 0 then
        UIManager:show(InfoMessage:new{ text = _("No downloaded editions yet.") })
        return
    end

    for _, group in ipairs(groups) do
        table.sort(group.editions, function(first, second)
            if first.downloaded_at == second.downloaded_at then return first.text < second.text end
            return first.downloaded_at > second.downloaded_at
        end)
    end
    table.sort(groups, function(first, second)
        if first.is_other ~= second.is_other then return not first.is_other end
        if first.last_downloaded == second.last_downloaded then return first.title < second.title end
        return first.last_downloaded > second.last_downloaded
    end)

    local items = {}
    for _, group in ipairs(groups) do
        table.insert(items, {
            text = group.title,
            mandatory = group.latest_date ~= "" and group.latest_date or nil,
            group = group,
        })
    end
    local menu
    menu = Menu:new{
        title = _("Downloaded publications"), item_table = items,
        is_popout = false, is_borderless = true, title_bar_fm_style = true,
        onMenuSelect = function(menu_widget, item) self:showDownloadedEditions(item.group) end,
    }
    self.active_menu = menu
    UIManager:show(menu)
end

function PressReaderSync:showDownloadedEditions(group)
    local items = group.editions
    local menu
    menu = Menu:new{
        title = group.title, item_table = items,
        is_popout = false, is_borderless = true, title_bar_fm_style = true,
        onMenuSelect = function(menu_widget, item) self:openFile(item.path) end,
        onMenuHold = function(menu_widget, item)
            UIManager:show(ConfirmBox:new{
                text = T(_("Delete %1?"), item.text), ok_text = _("Delete"),
                ok_callback = function()
                    local removed, err = os.remove(item.path)
                    if not removed then
                        self:showError(err)
                        return
                    end
                    self:forgetDownload(item.path)
                    for index, row in ipairs(items) do
                        if row.path == item.path then table.remove(items, index) break end
                    end
                    if #items == 0 then UIManager:close(menu) else menu:switchItemTable(nil, items) end
                end,
            })
            return true
        end,
    }
    if self.active_menu then UIManager:close(self.active_menu) end
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
            { description = _("Editions to keep per publication"), text = tostring(self:retentionCount()), hint = "2" },
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
                    local editions_to_keep = tonumber(fields[4])
                    if not editions_to_keep or editions_to_keep < 1
                        or editions_to_keep ~= math.floor(editions_to_keep) then
                        self:showError(_("Editions to keep must be a whole number of at least 1"))
                        return
                    end
                    self.settings:saveSetting("base_url", base_url)
                    self.settings:saveSetting("token", fields[2])
                    self.settings:saveSetting("download_dir", fields[3])
                    self.settings:saveSetting("editions_to_keep", editions_to_keep)
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
