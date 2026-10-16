-- DEV ONLY: tiny live REPL for exploring the game from outside.
-- Drops: <dir>/in.lua  -> executed on the game thread  -> <dir>/out.txt
-- RV_TEST_DIR (set by tools/test-instance.sh) is a Linux path; Wine sees the Linux root as Z:\
local BASE = os.getenv("RV_TEST_DIR") or "/mnt/nvme-storage/rv-mod-test"
local DIR = "Z:" .. BASE:gsub("/", "\\") .. "\\repl\\"
local outbuf = {}
local function out(...)
    local t = {}
    for i = 1, select('#', ...) do t[#t + 1] = tostring((select(i, ...))) end
    outbuf[#outbuf + 1] = table.concat(t, " ")
end

LoopAsync(400, function()
    local f = io.open(DIR .. "in.lua", "r")
    if not f then return false end
    local code = f:read("a")
    f:close()
    os.remove(DIR .. "in.lua")
    ExecuteInGameThread(function()
        outbuf = {}
        local env = setmetatable({ print = out }, { __index = _G })
        local fn, err = load(code, "repl", "t", env)
        if not fn then
            out("COMPILE ERROR: " .. tostring(err))
        else
            local ok, res = xpcall(fn, debug.traceback)
            if not ok then out("ERROR: " .. tostring(res)) elseif res ~= nil then out("=> " .. tostring(res)) end
        end
        local o = io.open(DIR .. "out.tmp", "w")
        o:write(table.concat(outbuf, "\n") .. "\n")
        o:close()
        os.remove(DIR .. "out.txt")
        os.rename(DIR .. "out.tmp", DIR .. "out.txt")
    end)
    return false
end)
print("[RVRepl] ready\n")
