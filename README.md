# rv-wheel — steering-wheel support for *RV There Yet?* (Linux / Proton)

(The separate **[rv-restore](https://github.com/Nyakodon/rv-restore)** package — restore RV / players / items on
checkpoint load — is independent of this one and can be installed alongside it; they share UE4SS and never touch each
other's files.)

---------------------------------------------------------------------------------------------------

Two ways to drive the RV with a wheel. Pick one.

| | **mod mode** (`mod`) — full featured | **virtual gamepad** (`run`) — simple |
|---|---|---|
| Steering / pedals / clutch | ✔ direct to the vehicle | ✔ via a fake Xbox controller |
| Handbrake, horn, engine, lights, park, exit | ✔ | ✔ (except nothing extra) |
| Paddle shifting, direct gear 1-5 / R / N, auto⇄manual, reset vehicle | ✔ | ✘ |
| **Force feedback** from the vehicle (centring that grows with speed, slide, engine, crashes) | ✔ | ✘ |
| Wheel only takes over an axis while you use it (keyboard still works) | ✔ | ✘ |
| Changes game files | **yes** (UE4SS + the mod; reversible) | **no** |
| Needs a Steam launch option | **yes** | no |

Everything below uses information read from the game's own data (input mappings, Blueprint classes) and
verified against a live copy of the game — see *What was verified* at the end.

---------------------------------------------------------------------------------------------------

## Mod mode

```
 wheel ──evdev──▶ rv_wheel.py mod ──/dev/shm/rv-wheel/state.txt──▶ RVWheel (UE4SS Lua, in the game)
                       ▲                                                 │ drives the vehicle with the
                       └──── force feedback ◀── telemetry.txt ◀──────────┘ game's own control functions
```

The in-game mod hooks the game's per-frame `UpdateThrottleAndSteering` on the player's own vehicle and calls the same
functions the keyboard/gamepad controls call (`SetSteeringInput`, `SetThrottleInput`, `ToggleEngine`, `GearUp`, ...).
That keeps it multiplayer-correct (the game replicates those calls itself) and it adds no cheats: no speed, damage or
physics changes, nothing that the normal controls can't do.

### Install (game must be closed)

```sh
cd ~/misc/games/rv-wheel
./rv_wheel.py setup            # once: calibrate the wheel + bind buttons (no game needed)
./rv_wheel.py install-mod      # installs UE4SS + the RVWheel mod into the game folder
```

Then in Steam: right-click the game → Properties → **Launch Options**:

```
WINEDLLOVERRIDES="dwmapi=n,b" %command%
```

(That makes Wine load UE4SS's `dwmapi.dll`.) `install-mod` refuses to run while the game is running, and leaves an
existing UE4SS and its settings alone. Remove everything again with `./rv_wheel.py uninstall-mod --all` and clear the
launch option.

### Play

```sh
./rv_wheel.py mod              # leave it running; start / stop it any time, before or after the game
```

Drive as usual. Steering, throttle, brake and clutch come from the wheel **while you are using them**; an axis you aren't
touching stays with the keyboard / gamepad. **F8** (in game) toggles wheel input off/on.

### Bindings (made in `setup`, any subset)

During `setup`, every action takes the **first input you give it**: a button press, or an axis pushed all the way
(e.g. an analog handbrake lever) — on the wheel **or on any other connected device** (a Moza multi-function stalk,
a button box, a separate handbrake ...). Other devices are found automatically (`./rv_wheel.py list` shows them as
`other`), are picked up by name, and may be plugged in or out at any time while `mod` / `run` is going.

| action | does |
|---|---|
| `handbrake` `horn` `clutch` | held while pressed (an axis counts as pressed once past 50 % of its travel) |
| `engine` `lights` `exit` `park` | the game's own toggle / cycle (lights: off → low → high) |
| `gear_up` `gear_down` | the game's own shift up / down (paddles) |
| `gear_1` … `gear_5` `gear_r` `gear_n` | pick the gear / reverse / neutral directly |
| `transmission` | manual ⇄ automatic |
| `reset` | the game's "fix vehicle" |

**H-pattern shifter:** if you bind `gear_1` … `gear_5` / `gear_r`, they are treated as a real H-shifter: the button of
the engaged gear stays held, and **releasing them all puts the car in neutral**.

In `~/.config/rv-wheel/config.json` a binding is a number (a button on the wheel — the original format), or:
`{"dev": "MOZA Multi-function Stalk", "key": 289}` (a button on another device) or
`{"dev": "MOZA HBP", "axis": 2, "rest": 0, "full": 4095, "at": 0.5}` (an axis; `at` = how far counts as pressed).
`dev` is the device name (or part of it, or a `/dev/input/eventN` path).

### Steering wheel, camera

* The **in-cabin steering wheel turns 1:1 with your hands** while the wheel steers (the game's own wheel only turns
  ±30°, the tyre angle). It needs your wheel's rotation range: `setup` asks for it; otherwise it is taken from the
  config (`steer.wheel_degrees`), then from the kernel driver where it reports one (`range`), else 900. The tyres
  still use the full-lock you set in `setup`. `steer.visual_scale` (1.0) scales the in-game wheel, `steer.wheel_visual: false`
  turns the feature off.
* The **camera follows the vehicle** as it turns while the wheel steers (the game only does that for its own steering
  inputs). `steer.camera_follow: false` turns it off.

### Force feedback

Needs a wheel with Linux force feedback (Logitech G25/27/29/920/923, Thrustmaster T150/T300, Fanatec, ... via the
kernel drivers). Computed from the vehicle's real telemetry:

* **centring spring** — light when parked, firm at speed, lighter while sliding
* **damper** — a little friction at standstill
* **slide feedback** — a push toward the direction the RV is drifting
* **engine vibration** (follows RPM) and **impact kicks** on collisions

Safety: everything is scaled by `ffb.gain`, hard-capped by `ffb.max_level`, rate-limited (no sudden snaps) and fades to
zero within 0.3 s when the game stops talking to the daemon. `./rv_wheel.py ffb-test` plays gentle test forces so you can
check strength and that left/right are not swapped (`ffb.invert`).

### No wheel handy? `--sim`

`./rv_wheel.py mod --sim demo` drives the RV with a scripted fake wheel (gears, swerves, lights, horn, brake, handbrake,
park, engine). Handy to check that an install works. `--sim slalom` is a harder run.

---------------------------------------------------------------------------------------------------

## Virtual gamepad mode (no game changes)

```sh
./rv_wheel.py setup && ./rv_wheel.py monitor   # check values; creates nothing
./rv_wheel.py run                              # creates "RV Wheel Virtual Pad" (Xbox 360 layout)
```

wheel → left stick X · throttle → RT · brake → LT · clutch → LB · buttons → A (handbrake) B (lights) X (exit) Y (park)
RB (engine) R3 (horn) Start/Back · wheel d-pad → d-pad. If steering feels dead around the centre, set
`steer.game_deadzone` to `0.2` (the game may apply Unreal's default 0.2 stick deadzone).

---------------------------------------------------------------------------------------------------

## Commands

```
list          wheel-like input devices          setup       calibration + button binding
monitor       live view of pad output           run         virtual gamepad mode
mod           in-game mod mode (+ force feedback; --sim SCENARIO, --no-ffb, --dir)
ffb-test      gentle force-feedback test
install-mod / uninstall-mod [--all] [--game-dir] [--ue4ss-zip]
```

Requirements: Linux, `python-evdev`, read access to the wheel's `/dev/input/event*` and (pad mode) write access to
`/dev/uinput` — your user being in the `input` group covers both. `--device "G29"` picks a wheel by name/path.

## Config (`~/.config/rv-wheel/config.json`)

`steer.gain` (>1: less rotation for full lock) · `steer.expo` · `steer.deadband` · `steer.wheel_degrees` ·
`steer.wheel_visual` · `steer.visual_scale` · `steer.camera_follow` · `clutch_threshold` ·
`mod.dir` / `mod.rate_hz` / `mod.active_hold_s` · `ffb.*` (`gain` `max_level` `invert` `spring` `damper` `self_align`
`engine` `impact` `autocenter`). `mod.dir` must match `dir` in the mod's `Scripts/rvw_config.lua`
(default `/dev/shm/rv-wheel` ⇄ `Z:\dev\shm\rv-wheel\`; the game's Steam container shares `/dev/shm` with the host).

## Troubleshooting

* *Nothing happens in game* — is `./rv_wheel.py mod` running? Does the log (`.../Win64/ue4ss/UE4SS.log`) contain
  `[RVWheel] ready`? If not, the launch option is missing or UE4SS did not load.
* *Wheel works but forces feel reversed* — `ffb.invert: true`.
* *Forces too strong/weak* — `ffb.gain`; to disable, `--no-ffb`.
* *"the wheel refused the '...' force-feedback effect"* — the wheel has few effect slots (or another program, e.g. the
  base's own software, is using them). That effect kind is skipped and the rest keeps working; close the other program
  to get everything.
* *Another program also grabs the wheel* — set `"grab": false` or stop it.

---------------------------------------------------------------------------------------------------

## Tests

```sh
python3 -m unittest discover -s tests          # ~90 tests: maths, model, link protocol, FFB, installer, the Lua mod
```

Offline: a fake wheel, a fake force-feedback device and a mock vehicle; no device or game is touched
(the Lua tests need a `lua5.4` binary and skip without one).

## What was verified (and what was not)

Verified against a **live, isolated copy of the game** (headless, offline, separate Wine prefix): UE4SS loads on this
game's Unreal 5.6; the mod installs through UE4SS's normal startup path; the daemon's scripted wheel drove the real RV
through engine start, gear selection, throttle, steering (positive = right), brake, handbrake, lights (three real
intensity steps), horn, clutch, park and engine-off; telemetry flowed back and was parsed; recorded drives (including a
crash) were replayed through the force-feedback model.

**Not verifiable without hardware / your session:** behaviour with a physical wheel (pedal polarity and button codes are
calibrated by `setup`, not assumed), the real force-feedback feel and direction on your wheel (use `ffb-test`, tune
`ffb.*`), and your real game install (the installer was only exercised on the copy and in unit tests).
