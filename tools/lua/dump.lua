-- usage: set CLASSES = { "/Game/...Class", ... } before including; prints props + functions (incl. params) of each class, own members only.
local function ptype(p)
    local ok, c = pcall(function() return p:GetClass():GetFName():ToString() end)
    return ok and c or "?"
end
local function matches(name, filter)
    if not filter then return true end
    name = name:lower()
    if type(filter) == "string" then return name:find(filter, 1, true) ~= nil end
    for _, f in ipairs(filter) do if name:find(f, 1, true) then return true end end
    return false
end
function dumpclass(path, filter)
    local c = StaticFindObject(path)
    if not c or not c:IsValid() then print("## NOT FOUND", path) return end
    print("## " .. c:GetFullName())
    local sup = c:GetSuperStruct()
    print("   super: " .. (sup and sup:IsValid() and sup:GetFullName() or "none"))
    c:ForEachProperty(function(p)
        local n = p:GetFName():ToString()
        if matches(n, filter) then print(string.format("   P %-42s %s", n, ptype(p))) end
    end)
    c:ForEachFunction(function(f)
        local n = f:GetFName():ToString()
        if matches(n, filter) then
            local ps = {}
            f:ForEachProperty(function(p) ps[#ps + 1] = p:GetFName():ToString() .. ":" .. ptype(p) end)
            print(string.format("   F %-42s (%s)", n, table.concat(ps, ", ")))
        end
    end)
end
