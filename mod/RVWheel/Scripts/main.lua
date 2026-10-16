-- RVWheel: steering-wheel support for "RV There Yet?"
-- Runs inside the game (UE4SS). The companion program `rv_wheel.py mod` reads the wheel and plays force
-- feedback; this mod applies the wheel to the player's vehicle and reports vehicle telemetry back.
local ok, logic = pcall(require, "rvw_logic")
if not ok then
    print("[RVWheel] failed to load: " .. tostring(logic) .. "\n")
    return
end
RVW = logic -- global, so a dev REPL can hot-reload it
logic.install()
