"""A scripted fake wheel, for testing the whole chain (daemon -> in-game mod -> force feedback)
without hardware.   `rv_wheel.py mod --sim demo`"""
from __future__ import annotations

import copy
import math
import os
import threading
import time
from typing import Callable

from evdev import AbsInfo, InputEvent, ecodes as e

from .config import ACTIONS, DEFAULT_CONFIG

# A G29-like layout: wheel 0..65535, pedals 255 = released / 0 = floored.
AXIS_RANGES = {
    e.ABS_X: (0, 65535), e.ABS_Y: (0, 255), e.ABS_Z: (0, 255), e.ABS_RZ: (0, 255),
    e.ABS_HAT0X: (-1, 1), e.ABS_HAT0Y: (-1, 1),
}
BUTTON_CODES = {a.name: e.BTN_TRIGGER + i for i, a in enumerate(ACTIONS)}


def sim_config(base: dict | None = None) -> dict:
    """Calibrated config matching SimWheel (keeps the user's ffb / mod / tuning sections)."""
    cfg = copy.deepcopy(base or DEFAULT_CONFIG)
    cfg["steer"].update(axis=e.ABS_X, left=16384, right=49150, center=32767)
    cfg["pedals"]["throttle"].update(axis=e.ABS_Y, rest=255, full=0)
    cfg["pedals"]["brake"].update(axis=e.ABS_RZ, rest=255, full=0)
    cfg["pedals"]["clutch"].update(axis=e.ABS_Z, rest=255, full=0)
    cfg["buttons"] = {name: [code] for name, code in BUTTON_CODES.items()}
    cfg["device"] = None
    return cfg


class Controls:
    """Normalised sim controls at one instant."""
    def __init__(self, steer=0.0, throttle=0.0, brake=0.0, clutch=0.0, buttons=()):
        self.steer, self.throttle, self.brake, self.clutch, self.buttons = steer, throttle, brake, clutch, set(buttons)


def _taps(t: float, schedule: dict[str, list[float]], width: float = 0.25) -> set[str]:
    return {name for name, times in schedule.items() if any(t0 <= t < t0 + width for t0 in times)}


def _gear(t: float, spans: list[tuple[float, float, str]]) -> set[str]:
    """An H-shifter: the engaged gear's button is HELD for as long as the gear is in."""
    return {name for a, b, name in spans if a <= t < b}


def demo(t: float) -> Controls:
    """~30 s: gear 1, drive with swerves, gear 2, lights, horn, neutral, brake, handbrake, park, engine off."""
    taps = _taps(t, {"engine": [0.5, 28.0], "lights": [7.0, 8.0], "horn": [10.0, 10.6], "park": [24.0]})
    held = taps | _gear(t, [(3.0, 9.0, "gear_1"), (9.0, 14.0, "gear_2")])   # released at 14 s -> neutral
    if 18.0 <= t < 19.5:
        held.add("handbrake")
    if t < 3.0:
        return Controls(buttons=held)
    if t < 14.0:
        ramp = min(1.0, (t - 3.0) / 2.0)
        steer = 0.7 * math.sin((t - 4.0) * 0.9) if t >= 4.0 else 0.0
        return Controls(steer=steer, throttle=0.55 * ramp, clutch=1.0 if 8.8 <= t < 9.4 else 0.0, buttons=held)
    if t < 18.0:
        return Controls(brake=min(1.0, (t - 14.0) / 1.0), buttons=held)
    return Controls(buttons=held)


def slalom(t: float) -> Controls:
    """Hard driving for force-feedback tuning: gears 1-3, full throttle, then full-lock swerves and a stop."""
    taps = _taps(t, {"engine": [0.5]}) | _gear(t, [(2.5, 6.5, "gear_1"), (6.5, 11.0, "gear_2"), (11.0, 99.0, "gear_3")])
    if t < 3.0:
        return Controls(buttons=taps)
    if t < 30.0:
        steer = 0.0 if t < 13.0 else math.copysign(1.0, math.sin((t - 13.0) * 1.3)) * min(1.0, (t - 13.0) / 0.4)
        if t >= 13.0:
            steer = max(-1.0, min(1.0, 1.4 * math.sin((t - 13.0) * 1.3)))
        return Controls(steer=steer, throttle=1.0, buttons=taps)
    return Controls(brake=1.0, buttons=taps)


def crash(t: float) -> Controls:
    """Dev: drive straight ahead at full throttle (engine must already be running) to hit something."""
    taps = _gear(t, [(0.5, 4.0, "gear_1"), (4.0, 99.0, "gear_2")])
    return Controls(throttle=min(1.0, t / 1.5), buttons=taps) if t < 20.0 else Controls(brake=1.0)


def drive(t: float) -> Controls:
    """Dev: gear 1, steady throttle, slow sine steering (engine must already be running). For camera / steering tests."""
    held = _gear(t, [(0.5, 99.0, "gear_1")])
    steer = 0.8 * math.sin((t - 3.0) * 0.6) if t >= 3.0 else 0.0
    return Controls(steer=steer, throttle=0.45 if t >= 1.0 else 0.0, buttons=held)


def shifter(t: float) -> Controls:
    """Dev: an H-shifter in use - gear 1 held, released (neutral), gear 2 held, released, reverse held, released."""
    return Controls(buttons=_gear(t, [(1.0, 5.0, "gear_1"), (7.0, 11.0, "gear_2"), (13.0, 17.0, "gear_r")]))


SCENARIOS: dict[str, Callable[[float], Controls]] = {"demo": demo, "slalom": slalom, "crash": crash, "drive": drive,
                                                     "shifter": shifter}


class SimWheel:
    name = "Simulated G29"
    path = "/dev/input/sim-wheel"

    def __init__(self, scenario: Callable[[float], Controls] = demo, cfg: dict | None = None,
                 rate_hz: int = 100, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg or sim_config()
        self.scenario, self.clock = scenario, clock
        self.t0 = clock()
        self._rest = {c: (hi if c in (e.ABS_Y, e.ABS_Z, e.ABS_RZ) else (lo + hi) // 2 if c == e.ABS_X else 0)
                      for c, (lo, hi) in AXIS_RANGES.items()}
        self.vals = dict(self._rest)
        self.keys: set[int] = set()
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)
        self.fd = self.r
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._tick, args=(1.0 / rate_hz,), daemon=True)
        self._thr.start()

    def _tick(self, period: float) -> None:
        while not self._stop.wait(period):
            try:
                os.write(self.w, b"x")
            except OSError:
                return

    def _target(self) -> tuple[dict[int, int], set[int]]:
        c = self.scenario(self.clock() - self.t0)
        s, p = self.cfg["steer"], self.cfg["pedals"]
        vals = dict(self._rest)
        half = (s["right"] - s["center"])
        vals[e.ABS_X] = int(round(s["center"] + max(-1.0, min(1.0, c.steer)) * half))
        for ctrl, name in ((c.throttle, "throttle"), (c.brake, "brake"), (c.clutch, "clutch")):
            pd = p[name]
            vals[pd["axis"]] = int(round(pd["rest"] + max(0.0, min(1.0, ctrl)) * (pd["full"] - pd["rest"])))
        keys = {code for name, code in BUTTON_CODES.items() if name in c.buttons}
        return vals, keys

    # --- WheelLike ---------------------------------------------------------
    def read(self) -> list:
        try:
            while os.read(self.r, 4096):
                pass
        except BlockingIOError:
            pass
        vals, keys = self._target()
        evs, ts = [], int(self.clock() * 1e6)
        mk = lambda t, c, v: InputEvent(ts // 1_000_000, ts % 1_000_000, t, c, v)
        for c, v in vals.items():
            if v != self.vals[c]:
                evs.append(mk(e.EV_ABS, c, v))
        for code in sorted(keys - self.keys):
            evs.append(mk(e.EV_KEY, code, 1))
        for code in sorted(self.keys - keys):
            evs.append(mk(e.EV_KEY, code, 0))
        self.vals, self.keys = vals, keys
        if evs:
            evs.append(mk(e.EV_SYN, e.SYN_REPORT, 0))
        return evs

    def abs_codes(self): return list(AXIS_RANGES)
    def abs_info(self, c): lo, hi = AXIS_RANGES[c]; return AbsInfo(self.vals[c], lo, hi, 0, 0, 0)
    def abs_value(self, c): return self.vals[c]
    def pressed(self): return set(self.keys)
    def grab(self): pass
    def ff_device(self): return None

    def close(self) -> None:
        self._stop.set()
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass
