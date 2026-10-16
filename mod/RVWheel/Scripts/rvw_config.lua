-- Optional settings. Edit and restart the game.
return {
    -- Must match "mod.dir" in ~/.config/rv-wheel/config.json (default /dev/shm/rv-wheel).
    -- Written as a Windows path: Wine maps Z:\ to the Linux filesystem root.
    dir = "Z:\\dev\\shm\\rv-wheel\\",
    -- How long (seconds of game CPU time) without a new wheel update before the wheel is considered gone.
    stale_after = 0.5,
    -- Seconds between telemetry writes (drives force feedback).
    telemetry_period = 1 / 30,
    -- Log every action the wheel triggers.
    verbose = false,
}
