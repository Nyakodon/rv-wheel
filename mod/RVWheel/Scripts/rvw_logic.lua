local M = {}

local cfg_ok, CFG = pcall(require, "rvw_config")
if not cfg_ok or type(CFG) ~= "table" then CFG = {} end
local DIR = CFG.dir or "Z:\\dev\\shm\\rv-wheel\\"
local STALE = CFG.stale_after or 0.5
local TEL_PERIOD = CFG.telemetry_period or (1 / 30)
local VERBOSE = CFG.verbose or false

local STATE_PATH, TEL_PATH, TEL_TMP = DIR .. "state.txt", DIR .. "telemetry.txt", DIR .. "telemetry.tmp"
local BASE = "/Game/Ride/Vehicle/Blueprints/BP_VehicleBase.BP_VehicleBase_C"

local function log(msg) print("[RVWheel] " .. msg .. "\n") end
local logged = {}
local function log_once(key, msg)
    if not logged[key] then
        logged[key] = true
        log(msg)
    end
end

-- Shifter positions (measured in game): 0 park, 1 reverse, 2 neutral, 3 drive.
local SHIFTER_REVERSE, SHIFTER_NEUTRAL, SHIFTER_DRIVE = 1, 2, 3

-- ---------------------------------------------------------------- state link
local function clamp(v, lo, hi)
    if v < lo then return lo elseif v > hi then return hi end
    return v
end

local function valid(o)
    if o == nil then return false end
    local ok, r = pcall(function() return o:IsValid() end)
    return ok and r == true
end

local function read_file(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local s = f:read("a")
    f:close()
    return s
end

local function parse_state(text)
    local kv = {}
    for k, v in text:gmatch("([%w_%.]+)=([^\n]*)") do kv[k] = v end
    if kv.rvw ~= "1" or kv["end"] ~= "1" then return nil end -- other version, or a torn write
    local st = {
        session = kv.session, seq = tonumber(kv.seq) or 0,
        steer = clamp(tonumber(kv.steer) or 0, -1, 1),
        throttle = clamp(tonumber(kv.throttle) or 0, 0, 1),
        brake = clamp(tonumber(kv.brake) or 0, 0, 1),
        clutch = clamp(tonumber(kv.clutch) or 0, 0, 1),
        active = {}, held = {}, counts = {},
        hshift = kv.hshift == "1", gear = kv.gear or "",
        wheel_deg = tonumber(kv.wheel_deg) or 0, visual = kv.visual == "1", camera = kv.camera ~= "0",
    }
    for name in (kv.active or ""):gmatch("[^,]+") do st.active[name] = true end
    for name in (kv.held or ""):gmatch("[^,]+") do st.held[name] = true end
    for k, v in pairs(kv) do
        local a = k:match("^n%.(.+)$")
        if a then st.counts[a] = tonumber(v) or 0 end
    end
    return st
end

local S = { -- runtime state (reset by M.reset)
    st = nil, last_seq = nil, last_seq_t = 0, last_poll = -1,
    base_counts = nil, session = nil, asserted = {}, tel_t = -1, seq_out = 0, gear_last = nil, cam_yaw = nil,
    last_yaw = nil, last_yaw_t = nil, enabled = true, last_call = -1,
}

function M.reset()
    -- (S.last_seq / S.last_seq_t are deliberately kept: a silent daemon must stay "stale" across a re-entry)
    S.st, S.base_counts, S.session, S.gear_last, S.cam_yaw = nil, nil, nil, nil, nil
    S.asserted, S.last_yaw, S.last_yaw_t = {}, nil, nil
end

-- Returns the newest *fresh* wheel state, or nil if the daemon is not running / stalled.
local function poll()
    local now = os.clock()
    if now - S.last_poll < 0.004 and S.st then return S.st end -- at most ~250 reads/s
    S.last_poll = now
    local text = read_file(STATE_PATH)
    local st = text and parse_state(text)
    if st then
        if st.seq ~= S.last_seq then
            S.last_seq, S.last_seq_t = st.seq, now
        end
        S.st = st
    end
    if not S.st or now - S.last_seq_t > STALE then
        S.st = nil
        return nil
    end
    return S.st
end

-- --------------------------------------------------------- applying actions
local function safe(name, fn, ...)
    local ok, err = pcall(fn, ...)
    if not ok then log_once("err:" .. name .. tostring(err), name .. " failed: " .. tostring(err)) end
    return ok
end

local function say(msg) if VERBOSE then log(msg) end end

local PRESS = {}

function PRESS.engine(veh) say("engine"); veh:ToggleEngine() end
function PRESS.lights(veh)
    -- same three steps as the game's own light key: off / low / high beam
    local step = ((veh.LightStep or 0) + 1) % 3
    say("lights -> " .. step)
    veh:Server_SetLightStep(step)
    safe("PlayLight_SwitchSFX", function() veh:PlayLight_SwitchSFX() end)
end
function PRESS.exit(veh) say("exit"); veh:TryToExitVehicle() end
function PRESS.park(veh) say("park"); veh:HandleParkInput() end
function PRESS.reset(veh) say("reset"); veh:RequestFixVehicle() end
function PRESS.transmission(veh) say("transmission"); veh:CycleTransmissionMode() end
function PRESS.gear_up(veh) say("gear up"); veh:GearUp() end
function PRESS.gear_down(veh) say("gear down"); veh:GearDown() end
function PRESS.gear_n(veh) say("neutral"); veh:SetShifterPosition(SHIFTER_NEUTRAL) end
function PRESS.gear_r(veh)
    say("reverse")
    veh:SetShifterPosition(SHIFTER_REVERSE)
    if not veh.AutomaticTransmission then veh:SetManualGear(1) end
end
local function select_gear(veh, n)
    say("gear " .. n)
    veh:SetShifterPosition(SHIFTER_DRIVE)
    if not veh.AutomaticTransmission then veh:SetManualGear(n) end -- automatic picks its own gear
end
for n = 1, 5 do PRESS["gear_" .. n] = function(veh) select_gear(veh, n) end end

-- With an H-pattern shifter the engaged gear's button stays held: that state (not the presses) drives the gearbox,
-- and releasing every gear button means neutral.
local GEAR_BUTTON = { gear_1 = true, gear_2 = true, gear_3 = true, gear_4 = true, gear_5 = true, gear_r = true }
local function apply_gear(veh, st)
    if not st.hshift then
        S.gear_last = nil
        return
    end
    local g = st.gear or ""
    if S.gear_last == nil then
        S.gear_last = g
        if g == "" then return end -- first sight: do not force neutral on a car the player is driving normally
    elseif g == S.gear_last then
        return
    end
    S.gear_last = g
    if g == "" then
        say("shifter released -> neutral")
        veh:SetShifterPosition(SHIFTER_NEUTRAL)
    else
        PRESS[g](veh)
    end
end

-- "hold" actions: act on the edge (pressed / released) only, so keyboard use is never disturbed.
local HOLD = {
    handbrake = function(veh, down)
        say("handbrake " .. tostring(down))
        veh:SetHandbrakeInput(down)
        safe("handbrake sfx", function()
            if down then veh:PlayHandbrake_InPlaceSFX() else veh:PlayHandbrake_OutOfPlaceSFX() end
        end)
    end,
    horn = function(veh, down) say("horn " .. tostring(down)); veh:Server_Honk(down) end,
    clutch = function(veh, down) say("clutch " .. tostring(down)); veh:SetClutchState(down) end,
}

local function release_holds(veh)
    for name, fn in pairs(HOLD) do
        if S.asserted[name] then
            S.asserted[name] = nil
            safe("release " .. name, fn, veh, false)
        end
    end
end

local function apply_events(veh, st)
    -- press counters: fire once per new press; the first sight of a session only records a baseline
    if S.session ~= st.session or not S.base_counts then
        S.session, S.base_counts = st.session, {}
        for a, n in pairs(st.counts) do S.base_counts[a] = n end
    else
        for a, n in pairs(st.counts) do
            local before = S.base_counts[a] or 0
            if n > before then
                local fn = PRESS[a]
                if fn and not (st.hshift and GEAR_BUTTON[a]) then
                    for _ = 1, math.min(n - before, 3) do safe("press " .. a, fn, veh) end
                end
            end
            S.base_counts[a] = n
        end
    end
    for name, fn in pairs(HOLD) do
        local down = st.held[name] == true
        if down ~= (S.asserted[name] == true) then
            S.asserted[name] = down or nil
            safe("hold " .. name, fn, veh, down)
        end
    end
end

local function apply_axes(veh, st)
    if st.active.steer then veh:SetSteeringInput(st.steer) end
    if st.active.throttle then veh:SetThrottleInput(st.throttle) end
    if st.active.brake then veh:SetBrakeInput(st.brake) end
end

-- The view is attached to the *controller's* rotation, and the game only turns it along with the vehicle for its own
-- steering inputs - which a wheel bypasses. While the wheel steers, carry the controller's yaw by however much the
-- vehicle turned, so the camera stays where it was relative to the vehicle (mouse look still works on top).
local CAMERA_MAX_STEP = 45 -- degrees per frame; anything larger is a teleport / reset, not driving
local function follow_camera(veh)
    local yaw = veh:K2_GetActorRotation().Yaw
    local last = S.cam_yaw
    S.cam_yaw = yaw
    if last == nil then return end
    local d = ((yaw - last) + 540) % 360 - 180
    if d == 0 or math.abs(d) > CAMERA_MAX_STEP then return end
    local pc = veh:GetController()
    if not valid(pc) then return end
    local cr = pc:GetControlRotation()
    pc:SetControlRotation({ Pitch = cr.Pitch, Yaw = cr.Yaw + d, Roll = cr.Roll })
end

-- ---------------------------------------------------------------- telemetry
local function write_telemetry(veh, wheel_on)
    local now = os.clock()
    if now - S.tel_t < TEL_PERIOD then return end
    local dt = S.tel_t > 0 and (now - S.tel_t) or nil
    S.tel_t = now

    local vel, fwd, right = veh:GetVelocity(), veh:GetActorForwardVector(), veh:GetActorRightVector()
    local speed = (vel.X * fwd.X + vel.Y * fwd.Y + vel.Z * fwd.Z) * 0.036   -- cm/s -> km/h
    local lat = (vel.X * right.X + vel.Y * right.Y + vel.Z * right.Z) * 0.036
    local yaw = veh:K2_GetActorRotation().Yaw
    local yaw_rate = 0
    if S.last_yaw and dt and dt > 0 then
        yaw_rate = (((yaw - S.last_yaw) + 540) % 360 - 180) / dt
    end
    S.last_yaw = yaw

    -- the vehicle's own `Slip` stays near 0 in practice, so also derive a slide-angle proxy from velocity:
    -- sideways/forward speed ratio, scaled so ~18 degrees of slide reads as 1.0 (fully sliding)
    local aspeed = math.abs(speed)
    local slide = 0
    if aspeed > 3 then slide = math.min(1.5, math.abs(lat) / math.max(aspeed, 10) * 3) end
    local slip = math.max(veh.Slip or 0, slide)

    S.seq_out = S.seq_out + 1
    local f = io.open(TEL_TMP, "w")
    if not f then return end
    f:write(string.format(
        "rvw=1\nseq=%d\ndriving=1\nspeed=%.3f\nlat=%.3f\nyaw=%.2f\nrpm=%.0f\nsteer=%.4f\nslip=%.4f\ntorque=%.2f\n" ..
        "gear=%d\nshifter=%d\nengine=%d\nwheel_on=%d\nend=1\n",
        S.seq_out, speed, lat, yaw_rate, veh.RPM or 0, veh.Steering or 0, slip, veh.Torque or 0,
        veh.CurrentGear or 0, veh.Shifter or 0, veh.EngineRunning and 1 or 0, wheel_on and 1 or 0))
    f:close()
    os.remove(TEL_PATH)             -- Wine's rename() cannot overwrite
    os.rename(TEL_TMP, TEL_PATH)
end

-- --------------------------------------------------------------------- hooks
local function step(veh)
    if not veh:IsLocallyControlled() then return end
    local now = os.clock()
    if now - S.last_call > 0.5 then -- (re)entered the vehicle, or came back from a pause: start clean
        M.reset()
    end
    S.last_call = now

    local st = S.enabled and poll() or nil
    if not st then
        if next(S.asserted) then release_holds(veh) end
        S.base_counts = nil
        return
    end
    apply_axes(veh, st)
    apply_events(veh, st)
    safe("gear", apply_gear, veh, st)
    if st.camera and st.active.steer then
        safe("camera", follow_camera, veh)
    else
        S.cam_yaw = nil
    end
    local wheel_on = st.active.steer or st.active.throttle or st.active.brake
    write_telemetry(veh, wheel_on == true)
end

function M.on_update(veh)
    local ok, err = pcall(step, veh)
    if not ok then log_once("step" .. tostring(err), "error: " .. tostring(err)) end
end

-- The cabin's steering wheel is a bone driven 1:1 by the animation blueprint's SteeringAngle (degrees), which the game
-- fills with the TYRE angle (+-30 at full lock), so it barely turns. While the wheel steers, show your real angle.
function M.on_anim(ai)
    local st = S.st
    if not st or not st.visual or not st.active.steer then return end
    local owner = ai:GetOwningActor()
    if not valid(owner) or not owner:IsLocallyControlled() then return end
    ai.SteeringAngle = st.wheel_deg
end

function M.on_unpossessed(veh)
    -- leaving the vehicle: let go of anything we were holding (handbrake, horn, clutch)
    pcall(release_holds, veh)
    M.reset()
    os.remove(TEL_PATH)
end

function M.toggle()
    S.enabled = not S.enabled
    log("wheel input " .. (S.enabled and "ENABLED" or "DISABLED (keyboard / gamepad only)"))
end

local installed, anim_installed = false, false
local ANIM_HOOK = "/Game/Ride/Vehicle/Interior/BPA_Truck_01.BPA_Truck_01_C:BlueprintUpdateAnimation"

local function try_install()
    local hooks = {
        -- (each UE4SS mod has its own Lua state, so RVW is this mod's global, set by main.lua)
        { BASE .. ":UpdateThrottleAndSteering", function(self) RVW.on_update(self:get()) end },
        { BASE .. ":ReceiveUnpossessed", function(self) RVW.on_unpossessed(self:get()) end },
    }
    for _, h in ipairs(hooks) do
        local ok, err = pcall(RegisterHook, h[1], h[2])
        if not ok then return false, err end
    end
    return true
end

function M.install()
    pcall(function() RegisterKeyBind(Key.F8, function() M.toggle() end) end)
    local tries, anim_tries = 0, 0
    LoopAsync(1000, function()
        ExecuteInGameThread(function()
            if not installed then
                local ok, err = try_install()
                tries = tries + 1
                if ok then
                    installed = true
                    log("ready (state dir " .. DIR .. "); press F8 to toggle wheel input")
                elseif tries == 1 or tries % 30 == 0 then
                    log("waiting for the vehicle class to load: " .. tostring(err))
                end
            end
            -- optional: only the 1:1 cabin steering wheel needs it, and its class may load later than the vehicle's
            if not anim_installed and anim_tries < 1800 then
                anim_tries = anim_tries + 1
                if pcall(RegisterHook, ANIM_HOOK, function(self) RVW.on_anim(self:get()) end) then
                    anim_installed = true
                    log("cabin steering wheel hook ready")
                end
            end
        end)
        return installed and (anim_installed or anim_tries >= 1800)
    end)
end

return M
