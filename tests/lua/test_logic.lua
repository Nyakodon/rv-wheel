-- Plain-Lua tests for mod/RVWheel/Scripts/rvw_logic.lua against a mock vehicle.
-- usage: lua5.4 tests/lua/test_logic.lua <scratch dir> [path/to/Scripts]
local DIR = assert(arg[1], "scratch dir required"):gsub("/$", "") .. "/"
local SCRIPTS = arg[2] or "mod/RVWheel/Scripts"
package.path = SCRIPTS .. "/?.lua;" .. package.path

-- ---- stub the UE4SS environment ------------------------------------------------
local NOW = 100.0
os.clock = function() return NOW end
local printed = {}
_G.print = function(...) printed[#printed + 1] = table.concat({ ... }, " ") end
local hooks = {}
_G.RegisterHook = function(path, fn) hooks[path] = fn; return 1, 1 end
_G.LoopAsync = function(ms, fn) return fn() end
_G.ExecuteInGameThread = function(fn) fn() end
_G.RegisterKeyBind = function() end
_G.Key = { F8 = 1 }
package.preload["rvw_config"] = function()
    return { dir = DIR, stale_after = 0.5, telemetry_period = 0, verbose = false }
end

local M = dofile(SCRIPTS .. "/rvw_logic.lua")

-- ---- helpers --------------------------------------------------------------------
local fails, total = 0, 0
local function check(cond, msg)
    total = total + 1
    if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end
end
local function eq(a, b, msg) check(a == b, msg .. " (got " .. tostring(a) .. ", want " .. tostring(b) .. ")") end

local seq = 0
local function write_state(t)
    seq = seq + 1
    t = t or {}
    local lines = { "rvw=1", "session=" .. (t.session or 1), "seq=" .. (t.seq or seq),
        string.format("steer=%.5f", t.steer or 0), string.format("throttle=%.5f", t.throttle or 0),
        string.format("brake=%.5f", t.brake or 0), string.format("clutch=%.5f", t.clutch or 0),
        "active=" .. table.concat(t.active or {}, ","), "held=" .. table.concat(t.held or {}, ","),
        "hshift=" .. (t.hshift and "1" or "0"), "gear=" .. (t.gear or "") }
    for a, n in pairs(t.counts or {}) do lines[#lines + 1] = "n." .. a .. "=" .. n end
    lines[#lines + 1] = "wheel_deg=" .. (t.wheel_deg or 0)
    lines[#lines + 1] = "visual=" .. (t.visual and "1" or "0")
    lines[#lines + 1] = "camera=" .. (t.camera == false and "0" or "1")
    if not t.torn then lines[#lines + 1] = "end=1" end
    local f = assert(io.open(DIR .. "state.txt", "w"))
    f:write(table.concat(lines, "\n") .. "\n")
    f:close()
end
local function clear_state() os.remove(DIR .. "state.txt") end

local function new_vehicle(props)
    local calls = {}
    local returns = {
        IsLocallyControlled = true,
        GetVelocity = { X = 2777.8, Y = 0, Z = 0 }, -- 100 km/h forward
        GetActorForwardVector = { X = 1, Y = 0, Z = 0 },
        GetActorRightVector = { X = 0, Y = 1, Z = 0 },
        K2_GetActorRotation = { Yaw = 10.0 },
        IsValid = true,
    }
    local v = { LightStep = 0, AutomaticTransmission = false, RPM = 3000, Steering = 0.25, Slip = 0.1,
                Torque = 50, CurrentGear = 2, Shifter = 3, EngineRunning = true, throw = {} }
    for k, val in pairs(props or {}) do v[k] = val end
    v.calls = calls
    v.__returns = returns
    return setmetatable(v, { __index = function(_, name)
        return function(_, ...)
            calls[#calls + 1] = { name = name, args = { ... } }
            if v.throw[name] then error("boom " .. name) end
            return returns[name]
        end
    end })
end
local function called(v, name)
    local n, last = 0, nil
    for _, c in ipairs(v.calls) do if c.name == name then n = n + 1; last = c.args end end
    return n, last
end
local QUERIES = { IsLocallyControlled = true, GetVelocity = true, GetActorForwardVector = true,
                  GetActorRightVector = true, K2_GetActorRotation = true }
local function effects(v) -- calls that change the game (queries excluded)
    local out = {}
    for _, c in ipairs(v.calls) do if not QUERIES[c.name] then out[#out + 1] = c end end
    return out
end
local function reset_calls(v) for i = #v.calls, 1, -1 do v.calls[i] = nil end end
local function frame(v) NOW = NOW + 0.01; M.on_update(v) end

local function fresh()
    M.reset()
    package.loaded["rvw_logic"] = nil
    clear_state(); os.remove(DIR .. "telemetry.txt")
    NOW = NOW + 10                      -- gap > 0.5 s => the mod treats it as a new session
end

-- ---- tests ----------------------------------------------------------------------
do -- 1. axes: only the ones the wheel owns are overridden
    fresh()
    local v = new_vehicle()
    write_state({ steer = -0.4, throttle = 0.7, brake = 0.0, active = { "steer", "throttle" } })
    frame(v)
    local n, a = called(v, "SetSteeringInput"); eq(n, 1, "steer applied"); eq(a[1], -0.4, "steer value")
    n, a = called(v, "SetThrottleInput"); eq(n, 1, "throttle applied"); eq(a[1], 0.7, "throttle value")
    eq(called(v, "SetBrakeInput"), 0, "brake NOT touched when the wheel does not own it (keyboard still works)")
end

do -- 2. nothing active => the mod leaves every channel to the game
    fresh()
    local v = new_vehicle()
    write_state({ steer = 0.0, active = {} })
    frame(v)
    eq(called(v, "SetSteeringInput") + called(v, "SetThrottleInput") + called(v, "SetBrakeInput"), 0, "idle wheel is invisible")
end

do -- 3. no daemon => no calls, no telemetry, no errors
    fresh()
    local v = new_vehicle()
    frame(v); frame(v)
    eq(#effects(v), 0, "no state file: vehicle untouched")
    check(io.open(DIR .. "telemetry.txt") == nil, "no telemetry without a daemon")
end

do -- 4. torn / wrong-version files are ignored
    fresh()
    local v = new_vehicle()
    write_state({ steer = 1, active = { "steer" }, torn = true }); frame(v)
    eq(#effects(v), 0, "torn write ignored")
    local f = io.open(DIR .. "state.txt", "w"); f:write("rvw=2\nseq=1\nend=1\n"); f:close(); frame(v)
    eq(#effects(v), 0, "other protocol version ignored")
end

do -- 5. stale daemon (seq stops advancing): release the wheel, drop held actions
    fresh()
    local v = new_vehicle()
    write_state({ seq = 900000, held = { "handbrake", "horn" }, active = { "steer" } })
    frame(v)
    eq(called(v, "SetHandbrakeInput"), 1, "handbrake asserted")
    reset_calls(v)
    NOW = NOW + 0.3; frame(v)           -- same seq, still young
    eq(called(v, "SetHandbrakeInput"), 0, "no repeat while held")
    reset_calls(v)
    NOW = NOW + 0.4; frame(v)           -- same seq, older than stale_after
    local n, a = called(v, "SetHandbrakeInput")
    eq(n, 1, "handbrake released when the daemon went away"); eq(a[1], false, "released = false")
    n, a = called(v, "Server_Honk"); eq(a[1], false, "horn released too")
    eq(called(v, "SetSteeringInput"), 0, "no axis writes while stale")
end

do -- 6. press counters: baseline first, then exactly one action per new press
    fresh()
    local v = new_vehicle()
    write_state({ counts = { engine = 4, lights = 1 }, active = {} }); frame(v)
    eq(#effects(v), 0, "presses made before we looked are ignored (baseline)")
    write_state({ counts = { engine = 5, lights = 1 } }); frame(v)
    eq(called(v, "ToggleEngine"), 1, "one new engine press -> one toggle")
    eq(called(v, "Server_SetLightStep"), 0, "unchanged counter does nothing")
    write_state({ counts = { engine = 5, lights = 1 } }); frame(v); frame(v)
    eq(called(v, "ToggleEngine"), 1, "no repeat on later frames")
    write_state({ counts = { engine = 7, lights = 1 } }); frame(v)
    eq(called(v, "ToggleEngine"), 3, "two quick presses between frames -> two toggles")
    write_state({ counts = { engine = 100, lights = 1 } }); frame(v)
    eq(called(v, "ToggleEngine"), 6, "burst is capped at 3 per frame")
end

do -- 7. daemon restart (new session id) must not replay old counters, nor fire phantom presses
    fresh()
    local v = new_vehicle()
    write_state({ session = 1, counts = { lights = 5 } }); frame(v)
    write_state({ session = 2, counts = { lights = 1 } }); frame(v)    -- restarted: counter went DOWN
    eq(#effects(v), 0, "restart is silent")
    write_state({ session = 2, counts = { lights = 2 } }); frame(v)
    eq(called(v, "Server_SetLightStep"), 1, "press after restart works")
end

do -- 8. hold actions act on edges only
    fresh()
    local v = new_vehicle()
    write_state({ held = {} }); frame(v)
    write_state({ held = { "handbrake", "horn", "clutch" } }); frame(v); frame(v); frame(v)
    local n, a = called(v, "SetHandbrakeInput"); eq(n, 1, "handbrake once"); eq(a[1], true, "handbrake on")
    n, a = called(v, "Server_Honk"); eq(n, 1, "horn once"); eq(a[1], true, "horn on")
    n, a = called(v, "SetClutchState"); eq(n, 1, "clutch once"); eq(a[1], true, "clutch down")
    write_state({ held = { "horn" } }); frame(v)
    n, a = called(v, "SetHandbrakeInput"); eq(n, 2, "handbrake edge on release"); eq(a[1], false, "handbrake off")
    n, a = called(v, "SetClutchState"); eq(a[1], false, "clutch up")
    eq(called(v, "Server_Honk"), 1, "horn still held: no extra call")
    eq(called(v, "PlayHandbrake_InPlaceSFX"), 1, "handbrake sound in")
    eq(called(v, "PlayHandbrake_OutOfPlaceSFX"), 1, "handbrake sound out")
end

do -- 9. gear selection
    fresh()
    local v = new_vehicle()
    write_state({ counts = {} }); frame(v)
    write_state({ counts = { gear_3 = 1 } }); frame(v)
    local _, a = called(v, "SetShifterPosition"); eq(a[1], 3, "gear_3 selects Drive")
    _, a = called(v, "SetManualGear"); eq(a[1], 3, "gear_3 selects gear 3")
    reset_calls(v)
    write_state({ counts = { gear_3 = 1, gear_r = 1 } }); frame(v)
    _, a = called(v, "SetShifterPosition"); eq(a[1], 1, "reverse position")
    _, a = called(v, "SetManualGear"); eq(a[1], 1, "reverse uses gear ratio 1")
    reset_calls(v)
    write_state({ counts = { gear_3 = 1, gear_r = 1, gear_n = 1 } }); frame(v)
    _, a = called(v, "SetShifterPosition"); eq(a[1], 2, "neutral position")
    eq(called(v, "SetManualGear"), 0, "neutral sets no gear")
    reset_calls(v)
    write_state({ counts = { gear_3 = 1, gear_r = 1, gear_n = 1, gear_up = 1, gear_down = 2, park = 1, transmission = 1,
                              reset = 1, exit = 1 } }); frame(v)
    eq(called(v, "GearUp"), 1, "gear up"); eq(called(v, "GearDown"), 2, "gear down x2")
    eq(called(v, "HandleParkInput"), 1, "park"); eq(called(v, "CycleTransmissionMode"), 1, "transmission")
    eq(called(v, "RequestFixVehicle"), 1, "reset"); eq(called(v, "TryToExitVehicle"), 1, "exit")
end

do -- 10. automatic transmission picks its own gear
    fresh()
    local v = new_vehicle({ AutomaticTransmission = true })
    write_state({ counts = {} }); frame(v)
    write_state({ counts = { gear_4 = 1 } }); frame(v)
    local _, a = called(v, "SetShifterPosition"); eq(a[1], 3, "Drive still selected")
    eq(called(v, "SetManualGear"), 0, "manual gear not forced in automatic mode")
end

do -- 11. lights cycle off -> low -> high -> off using the game's own step function
    fresh()
    local v = new_vehicle()
    write_state({ counts = {} }); frame(v)
    local cnt, expect = 0, { 1, 2, 0 }
    for i = 1, 3 do
        cnt = cnt + 1
        write_state({ counts = { lights = cnt } }); frame(v)
        local _, a = called(v, "Server_SetLightStep")
        eq(a[1], expect[i], "light step " .. i)
        v.LightStep = a[1]                  -- the game applies it
    end
end

do -- 12. telemetry content + wheel_on flag
    fresh()
    local v = new_vehicle()
    write_state({ active = { "throttle" } }); frame(v); frame(v)
    local f = assert(io.open(DIR .. "telemetry.txt")); local t = f:read("a"); f:close()
    check(t:find("rvw=1", 1, true) and t:find("end=1", 1, true), "telemetry framed")
    check(t:find("speed=100.00", 1, true), "speed in km/h (100)")
    check(t:find("wheel_on=1", 1, true), "wheel_on set while the wheel owns an axis")
    check(t:find("engine=1", 1, true) and t:find("gear=2", 1, true) and t:find("shifter=3", 1, true), "drivetrain fields")
    _G.TELEMETRY_SAMPLE = t
    write_state({ active = {} }); NOW = NOW + 0.1; frame(v)
    f = assert(io.open(DIR .. "telemetry.txt")); t = f:read("a"); f:close()
    check(t:find("wheel_on=0", 1, true), "wheel_on cleared when idle")
end

do -- 13. vehicles we are not driving are ignored
    fresh()
    local v = new_vehicle()
    v.IsLocallyControlled = nil
    local mt = getmetatable(v); local inner = mt.__index
    mt.__index = function(t, k) if k == "IsLocallyControlled" then return function() return false end end return inner(t, k) end
    write_state({ steer = 1, active = { "steer" } }); frame(v)
    eq(#effects(v), 0, "remote vehicle untouched")
end

do -- 14. leaving the vehicle releases everything and removes telemetry
    fresh()
    local v = new_vehicle()
    write_state({ held = { "handbrake", "horn" }, active = { "steer" } }); frame(v); frame(v)
    check(io.open(DIR .. "telemetry.txt") ~= nil, "telemetry exists while driving")
    reset_calls(v)
    M.on_unpossessed(v)
    local _, a = called(v, "SetHandbrakeInput"); eq(a and a[1], false, "handbrake released on exit")
    _, a = called(v, "Server_Honk"); eq(a and a[1], false, "horn released on exit")
    check(io.open(DIR .. "telemetry.txt") == nil, "telemetry removed on exit (force feedback fades out)")
end

do -- 15. an error inside the game's API must never escape into the game's frame
    fresh()
    local v = new_vehicle()
    v.throw = { SetSteeringInput = true, ToggleEngine = true }
    write_state({ steer = 1, active = { "steer" }, counts = {} }); frame(v)
    write_state({ steer = 1, active = { "steer" }, counts = { engine = 1 } })
    local ok = pcall(frame, v)
    check(ok, "errors are contained")
    check(#printed > 0, "and logged")
end

do -- 16. F8 toggles wheel input off/on
    fresh()
    local v = new_vehicle()
    write_state({ steer = 1, active = { "steer" } }); frame(v)
    eq(called(v, "SetSteeringInput"), 1, "enabled by default")
    M.toggle(); reset_calls(v); write_state({ steer = 1, active = { "steer" } }); frame(v)
    eq(called(v, "SetSteeringInput"), 0, "disabled: hands off")
    M.toggle(); write_state({ steer = 1, active = { "steer" } }); frame(v)
    eq(called(v, "SetSteeringInput"), 1, "re-enabled")
end

do -- 17. out-of-range values are clamped
    fresh()
    local v = new_vehicle()
    write_state({ steer = 7, throttle = -3, brake = 9, active = { "steer", "throttle", "brake" } }); frame(v)
    local _, a = called(v, "SetSteeringInput"); eq(a[1], 1, "steer clamped")
    _, a = called(v, "SetThrottleInput"); eq(a[1], 0, "throttle clamped low")
    _, a = called(v, "SetBrakeInput"); eq(a[1], 1, "brake clamped high")
end

do -- 19. H-pattern shifter: the held gear button drives the gearbox, releasing all of them is neutral
    fresh()
    local v = new_vehicle()
    write_state({ hshift = true, gear = "", counts = {} }); frame(v)
    eq(#effects(v), 0, "nothing held at first sight: no forced neutral")
    write_state({ hshift = true, gear = "gear_3", counts = { gear_3 = 1 } }); frame(v)
    local _, a = called(v, "SetShifterPosition"); eq(a[1], 3, "3rd selects Drive")
    _, a = called(v, "SetManualGear"); eq(a[1], 3, "...and gear 3")
    eq(called(v, "SetManualGear"), 1, "the press counter did not select the gear a second time")
    write_state({ hshift = true, gear = "gear_3", counts = { gear_3 = 1 } }); frame(v); frame(v)
    eq(called(v, "SetShifterPosition"), 1, "holding the gear changes nothing")
    reset_calls(v)
    write_state({ hshift = true, gear = "", counts = { gear_3 = 1 } }); frame(v)
    _, a = called(v, "SetShifterPosition"); eq(a and a[1], 2, "released -> neutral")
    eq(called(v, "SetManualGear"), 0, "neutral sets no gear")
    reset_calls(v)
    write_state({ hshift = true, gear = "gear_r", counts = { gear_3 = 1, gear_r = 1 } }); frame(v)
    _, a = called(v, "SetShifterPosition"); eq(a[1], 1, "reverse")
    reset_calls(v)
    write_state({ hshift = true, gear = "gear_2", counts = { gear_3 = 1, gear_r = 1, gear_2 = 1 } }); frame(v)
    _, a = called(v, "SetManualGear"); eq(a[1], 2, "straight from reverse into 2nd")
end

do -- 20. without an H-shifter nothing changes: gear buttons are plain presses, release does not touch the gearbox
    fresh()
    local v = new_vehicle()
    write_state({ hshift = false, gear = "", counts = {} }); frame(v)
    write_state({ hshift = false, gear = "", counts = { gear_2 = 1 } }); frame(v)
    local _, a = called(v, "SetManualGear"); eq(a[1], 2, "press still selects the gear")
    reset_calls(v)
    write_state({ hshift = false, gear = "", counts = { gear_2 = 1 } }); frame(v)
    eq(#effects(v), 0, "and nothing happens on release")
end

local function with_controller(v)
    local ctrl = { yaw = 100, sets = {} }
    function ctrl:IsValid() return true end
    function ctrl:GetControlRotation() return { Pitch = 1, Yaw = self.yaw, Roll = 0 } end
    function ctrl:SetControlRotation(r) self.sets[#self.sets + 1] = r; self.yaw = r.Yaw end
    v.__returns.GetController = ctrl
    return ctrl
end
local function set_yaw(v, y) v.__returns.K2_GetActorRotation = { Yaw = y } end

do -- 21. camera follow: the view is carried along with the vehicle while the wheel steers
    fresh()
    local v = new_vehicle(); local ctrl = with_controller(v)
    set_yaw(v, 10); write_state({ active = { "steer" } }); frame(v)
    eq(#ctrl.sets, 0, "first frame only records the heading")
    set_yaw(v, 25); write_state({ active = { "steer" } }); frame(v)
    eq(#ctrl.sets, 1, "the view is turned"); eq(ctrl.sets[1].Yaw, 115, "by exactly the vehicle's turn (15 deg)")
    eq(ctrl.sets[1].Pitch, 1, "pitch untouched"); eq(ctrl.sets[1].Roll, 0, "roll untouched")
    set_yaw(v, 355); write_state({ active = { "steer" } }); frame(v)       -- 25 -> 355 is -30 deg (wraps through 0)
    eq(ctrl.sets[#ctrl.sets].Yaw, 85, "wrap-around handled (turned left 30)")
    local n = #ctrl.sets
    set_yaw(v, 100); write_state({ active = { "steer" } }); frame(v)       -- 355 -> 100: a jump (reset / teleport)
    eq(#ctrl.sets, n, "a huge single-frame jump is not followed")
    set_yaw(v, 101); write_state({ active = { "steer" } }); frame(v)
    eq(#ctrl.sets, n + 1, "and normal following resumes right after")
end

do -- 22. camera follow only while the wheel is steering, and only if enabled
    fresh()
    local v = new_vehicle(); local ctrl = with_controller(v)
    set_yaw(v, 10); write_state({ active = {} }); frame(v)
    set_yaw(v, 30); write_state({ active = {} }); frame(v)
    eq(#ctrl.sets, 0, "keyboard / gamepad steering: the game's own camera handling, no double turn")
    write_state({ active = { "steer" } }); frame(v)                         -- wheel takes over: new baseline
    set_yaw(v, 40); write_state({ active = { "steer" } }); frame(v)
    eq(#ctrl.sets, 1, "follows once the wheel is steering")
    set_yaw(v, 90); write_state({ active = {} }); frame(v)                  -- wheel released: baseline dropped
    set_yaw(v, 91); write_state({ active = { "steer" } }); frame(v)
    eq(#ctrl.sets, 1, "no catch-up jump for what happened while the wheel was idle")
    fresh(); v = new_vehicle(); ctrl = with_controller(v)
    set_yaw(v, 10); write_state({ active = { "steer" }, camera = false }); frame(v)
    set_yaw(v, 40); write_state({ active = { "steer" }, camera = false }); frame(v)
    eq(#ctrl.sets, 0, "camera_follow = false is respected")
    fresh(); v = new_vehicle(); v.__returns.GetController = nil
    set_yaw(v, 10); write_state({ active = { "steer" } }); frame(v)
    local ok = pcall(function() set_yaw(v, 20); write_state({ active = { "steer" } }); frame(v) end)
    check(ok, "no controller: no crash")
end

do -- 23. the cabin steering wheel shows the real wheel angle
    fresh()
    local v = new_vehicle()
    local ai = { SteeringAngle = 12.5, GetOwningActor = function() return v end }
    write_state({ active = { "steer" }, wheel_deg = 137.5, visual = true }); frame(v)
    M.on_anim(ai); eq(ai.SteeringAngle, 137.5, "1:1 with the hands")
    write_state({ active = { "steer" }, wheel_deg = -450, visual = true }); frame(v)
    M.on_anim(ai); eq(ai.SteeringAngle, -450, "beyond the tyre lock, to full wheel travel")
    ai.SteeringAngle = 12.5
    write_state({ active = {}, wheel_deg = 90, visual = true }); frame(v)
    M.on_anim(ai); eq(ai.SteeringAngle, 12.5, "wheel idle: the game's own value stays")
    write_state({ active = { "steer" }, wheel_deg = 90, visual = false }); frame(v)
    M.on_anim(ai); eq(ai.SteeringAngle, 12.5, "wheel_visual = false is respected")
    write_state({ active = { "steer" }, wheel_deg = 90, visual = true }); frame(v)
    local remote = new_vehicle(); remote.IsLocallyControlled = nil
    getmetatable(remote).__index = (function(old) return function(t, k)
        if k == "IsLocallyControlled" then return function() return false end end return old(t, k) end end)(getmetatable(remote).__index)
    local ai2 = { SteeringAngle = 3, GetOwningActor = function() return remote end }
    M.on_anim(ai2); eq(ai2.SteeringAngle, 3, "another player's RV is never touched")
    NOW = NOW + 5; M.on_update(v)                                              -- daemon went silent: state stale
    ai.SteeringAngle = 12.5; M.on_anim(ai); eq(ai.SteeringAngle, 12.5, "stale daemon: hands off")
end

do -- 18. install registers the game hooks (through the RVW global so reloads work)
    RVW = M
    M.install()
    local base = "/Game/Ride/Vehicle/Blueprints/BP_VehicleBase.BP_VehicleBase_C:"
    check(hooks[base .. "UpdateThrottleAndSteering"] ~= nil, "per-frame hook registered")
    check(hooks[base .. "ReceiveUnpossessed"] ~= nil, "unpossess hook registered")
end

if _G.TELEMETRY_SAMPLE then
    io.write("TELEMETRY_BEGIN\n", _G.TELEMETRY_SAMPLE, "TELEMETRY_END\n")
end
io.write(string.format("lua tests: %d checks, %d failed\n", total, fails))
os.exit(fails == 0 and 0 or 1)
