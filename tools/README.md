# tools/ — development helpers (not needed to use the mod)

These drive an **isolated second copy of the game** so the mods can be tested without touching your real install.
They are specific to the author's setup (Proton-CachyOS, gamescope, bubblewrap, a btrfs reflink copy of the game) and
are provided as a reference, not as a supported feature.

| file | what |
|---|---|
| `test-instance.sh` | launch the copy at `$RV_TEST_DIR` (default `/mnt/nvme-storage/rv-mod-test`): headless gamescope, no network, own PID namespace and Wine prefix |
| `devrepl/` | a UE4SS mod that runs Lua files dropped into `$RV_TEST_DIR/repl/` (install it only into the test copy) |
| `repl.py`, `wait-repl.sh` | send Lua to that REPL / wait until it answers |
| `lua/*.lua` | snippets for it: open the level, enter the RV, class dumper, vehicle/item snapshots |
| `record-telemetry.py`, `analyze-telemetry.py`, `replay-ffb.py` | record the mod's telemetry, summarise it, replay it through the force-feedback model |
| `e2e-slalom.sh`, `run-slalom.sh` | scripted end-to-end runs with the simulated wheel |

Never point these at a running game you care about; the launcher uses its own prefix and copy on purpose.
