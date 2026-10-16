"""Actions, defaults and the on-disk config."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path

from evdev import ecodes as e

PAD_NAME = "RV Wheel Virtual Pad"
LOG_PREFIX = "[rv-wheel]"


@dataclass(frozen=True)
class Action:
    name: str
    kind: str            # "hold": state matters (handbrake); "press": each press is an event (lights)
    pad_code: int | None  # virtual gamepad button for `run` (pad) mode; None = in-game-mod only
    desc: str


# Order = order asked in `setup`.
ACTIONS: tuple[Action, ...] = (
    Action("handbrake", "hold", e.BTN_A, "handbrake"),
    Action("horn", "hold", e.BTN_THUMBR, "horn"),
    Action("engine", "press", e.BTN_TR, "toggle engine"),
    Action("lights", "press", e.BTN_B, "cycle headlights"),
    Action("exit", "press", e.BTN_X, "exit the vehicle"),
    Action("park", "press", e.BTN_Y, "park / neutral"),
    Action("clutch", "hold", e.BTN_TL, "clutch (optional: the clutch pedal already does this)"),
    Action("menu", "press", e.BTN_START, "pause menu (virtual-pad mode only)"),
    Action("back", "press", e.BTN_SELECT, "back (virtual-pad mode only)"),
    Action("gear_up", "press", None, "shift up - paddle (in-game mod only)"),
    Action("gear_down", "press", None, "shift down - paddle (in-game mod only)"),
    Action("gear_1", "press", None, "gear 1 (in-game mod only)"),
    Action("gear_2", "press", None, "gear 2 (in-game mod only)"),
    Action("gear_3", "press", None, "gear 3 (in-game mod only)"),
    Action("gear_4", "press", None, "gear 4 (in-game mod only)"),
    Action("gear_5", "press", None, "gear 5 (in-game mod only)"),
    Action("gear_r", "press", None, "reverse (in-game mod only)"),
    Action("gear_n", "press", None, "neutral (in-game mod only)"),
    Action("transmission", "press", None, "switch manual / automatic transmission (in-game mod only)"),
    Action("reset", "press", None, "reset / fix vehicle (in-game mod only)"),
)
ACTION_BY_NAME = {a.name: a for a in ACTIONS}
# H-pattern shifter buttons: the button of the engaged gear stays held, none held = neutral
GEAR_ACTIONS = ("gear_1", "gear_2", "gear_3", "gear_4", "gear_5", "gear_r")
PAD_ACTIONS = {a.name: a for a in ACTIONS if a.pad_code is not None}
PAD_BUTTONS = [a.pad_code for a in PAD_ACTIONS.values()]

DEFAULT_WHEEL_DEGREES = 900

DEFAULT_CONFIG: dict = {
    "device": None,  # path (/dev/input/eventN) or name substring; null = auto-detect
    "grab": True,  # take exclusive access so the game only sees the virtual pad / mod
    "steer": {
        "axis": None,
        "left": None,  # raw value at the position that should count as full left
        "right": None,
        "center": None,
        "gain": 1.0,  # >1 = less wheel rotation needed for full lock
        "expo": 1.0,  # >1 = softer around centre
        "deadband": 0.015,  # ignore jitter around centre (fraction of full lock)
        # Virtual-pad mode only. The game may apply Enhanced Input's default 0.2 stick
        # deadzone to steering; set this to 0.2 to compensate if you feel a dead spot.
        "game_deadzone": 0.0,
        # --- in-game steering wheel (mod mode) -------------------------------------------------------
        # Physical rotation of your wheel, lock to lock, in degrees (900 for most wheels). null = auto-detect
        # (Linux exposes it as `range` for some wheels), else 900. The in-cabin wheel is then turned to the
        # SAME angle as your hands (1:1); the front tyres keep following the calibrated lock above.
        "wheel_degrees": None,
        "wheel_visual": True,    # drive the in-cabin wheel 1:1 (false = leave the game's own, tiny, rotation)
        "visual_scale": 1.0,     # 1.0 = exactly your angle; 0.5 = the in-game wheel turns half as far; ...
        "camera_follow": True,   # turn the view with the vehicle while the wheel steers
    },
    "pedals": {
        "throttle": {"axis": None, "rest": None, "full": None},
        "brake": {"axis": None, "rest": None, "full": None},
        "clutch": {"axis": None, "rest": None, "full": None},
    },
    "clutch_threshold": 0.5,  # pedal travel at which the clutch counts as pressed
    "hat": True,  # pass the wheel d-pad (ABS_HAT0) through (virtual-pad mode)
    "buttons": {a.name: [] for a in ACTIONS},  # action -> [wheel key codes]
    "mod": {
        "dir": "/dev/shm/rv-wheel",  # exchange directory shared with the in-game mod
        "rate_hz": 60,  # state update rate
        "active_hold_s": 1.0,  # an axis stays "wheel-owned" this long after it last moved
    },
    "ffb": {
        "enabled": True,
        "gain": 0.6,  # overall strength 0..1
        "max_level": 0.85,  # hard cap on any single effect, 0..1
        "invert": False,  # flip force direction if the wheel pushes the wrong way (see ffb-test)
        "spring": 1.0,  # centring weight multiplier
        "damper": 1.0,
        "self_align": 1.0,  # lateral / slide feedback multiplier
        "engine": 0.6,  # engine vibration multiplier
        "impact": 1.0,  # collision kick multiplier
        "autocenter": None,  # hardware autocenter 0..1 while running; null = leave untouched
    },
}


class ConfigError(Exception):
    pass


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "rv-wheel" / "config.json"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in over.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            # `buttons` is keyed by action name: keep only known actions (drops stale/typo keys)
            out[key] = _merge(out[key], val)
        elif key in out:
            out[key] = val
    return out


def load_config(path: Path) -> dict:
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        return _merge(DEFAULT_CONFIG, json.loads(path.read_text()))
    except (OSError, ValueError) as ex:
        raise ConfigError(f"cannot read {path}: {ex}") from ex


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)


def require_configured(cfg: dict) -> None:
    s = cfg["steer"]
    if None in (s["axis"], s["left"], s["right"], s["center"]):
        raise ConfigError("steering is not calibrated - run `rv_wheel.py setup` first")
    if s["left"] == s["right"]:
        raise ConfigError("steering calibration is degenerate (left == right) - rerun setup")
