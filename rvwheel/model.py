"""Wheel state model: raw evdev events -> normalised controls, held actions, press counters."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from evdev import ecodes as e

from .bindings import AXIS_HYSTERESIS, Binding, parse_all
from .config import ACTIONS, DEFAULT_WHEEL_DEGREES, GEAR_ACTIONS
from .devices import WheelLike
from .mapping import clamp, pedal_norm, steer_norm

# movement below this (in normalised units) is treated as sensor noise for the "active" logic
_NOISE = 0.004
_STEER_ACTIVE = 0.02
_PEDAL_ACTIVE = 0.02


@dataclass(frozen=True)
class WheelState:
    steer: float = 0.0             # -1..1, negative = left
    throttle: float = 0.0          # 0..1
    brake: float = 0.0
    clutch: float = 0.0
    held: frozenset = frozenset()  # action names currently held (clutch pedal included)
    counts: tuple = ()             # ((action, cumulative presses), ...) sorted by action
    hat: tuple = (0, 0)
    active: frozenset = frozenset()  # axes the wheel currently owns: steer/throttle/brake/clutch
    hshift: bool = False           # an H-pattern shifter is bound: "gear" below is authoritative, "" = neutral
    gear: str = ""                 # the held gear button (gear_1 .. gear_5, gear_r) or "" when none is held
    wheel_deg: float = 0.0         # physical wheel angle from centre in degrees, positive = right (x visual_scale)
    visual: bool = False           # drive the in-game steering wheel with wheel_deg
    camera: bool = True            # turn the view with the vehicle while the wheel steers


class WheelModel:
    CLUTCH_HYSTERESIS = 0.08

    def __init__(self, cfg: dict, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.raw: dict[int, int] = {}
        self.down: set[int] = set()
        self.counts: dict[str, int] = {a.name: 0 for a in ACTIONS}
        self.clutch_on = False
        self._dropped = False  # (legacy flag; per-device tracking is in _dropped_devs)
        self._press_seq = 0
        self._last_press: dict[str, int] = {}
        self.axis_range: dict[int, tuple[int, int]] = {}
        self.auto_degrees: int | None = None     # lock-to-lock rotation detected from the driver, if it says
        self._last_value: dict[str, float] = {}
        self._last_change: dict[str, float] = {}
        # Bindings: action -> [Binding]. A binding is a button or an axis, on the main wheel (dev None) or on
        # any other device (dev = its name). Axes act as buttons once pushed past a threshold.
        self.bindings: dict[str, list[Binding]] = {a: parse_all(v) for a, v in cfg["buttons"].items()}
        self._key_actions: dict[tuple, list[str]] = {}      # (dev, key code) -> actions
        self._axis_bindings: dict[tuple, list[tuple[str, Binding]]] = {}   # (dev, axis code) -> [(action, binding)]
        for action, blist in self.bindings.items():
            for b in blist:
                if b.kind == "key":
                    self._key_actions.setdefault((b.dev, b.code), []).append(action)
                else:
                    self._axis_bindings.setdefault((b.dev, b.code), []).append((action, b))
        self.xraw: dict[tuple, int] = {}           # axis values of the other devices: (dev, code) -> value
        self.xdown: set[tuple] = set()             # keys held on the other devices: (dev, code)
        self._axis_on: dict[tuple, bool] = {}      # (action, binding) -> axis currently pushed past its threshold
        self._dropped_devs: set = set()

    def extra_devices(self) -> list[str]:
        """Names of the other devices that bindings refer to (the daemon must read them too)."""
        return sorted({b.dev for blist in self.bindings.values() for b in blist if b.dev is not None})

    # --- input -----------------------------------------------------------
    def resync(self, wheel: WheelLike, dev: str | None = None) -> None:
        """(Re)read a device's complete state. dev=None is the main wheel, else the other device's name."""
        raw = {c: wheel.abs_value(c) for c in wheel.abs_codes()}
        down = set(wheel.pressed())
        if dev is None:
            self.raw, self.down = raw, down
            axis = self.cfg["steer"]["axis"]
            if axis in self.raw:
                try:
                    info = wheel.abs_info(axis)
                    self.axis_range[axis] = (info.min, info.max)
                except (OSError, KeyError):
                    pass
        else:
            self.xraw = {k: v for k, v in self.xraw.items() if k[0] != dev}
            self.xraw.update({(dev, c): v for c, v in raw.items()})
            self.xdown = {k for k in self.xdown if k[0] != dev} | {(dev, c) for c in down}
        for (d, code) in list(self._axis_bindings):
            if d == dev:
                self._axis_changed(dev, code, count=False)   # adopt the current positions without "pressing"

    def drop_device(self, dev: str) -> None:
        """An other-device was unplugged: nothing of it stays held, and unplugging is not a press."""
        self.xraw = {k: v for k, v in self.xraw.items() if k[0] != dev}
        self.xdown = {k for k in self.xdown if k[0] != dev}
        for key in [k for k in self._axis_on if k[1].dev == dev]:
            self._axis_on[key] = False

    def _axis_changed(self, dev: str | None, code: int, count: bool = True) -> None:
        value = self.raw.get(code) if dev is None else self.xraw.get((dev, code))
        if value is None:
            return
        for action, b in self._axis_bindings.get((dev, code), ()):
            t = b.travel(value)
            prev = self._axis_on.get((action, b), False)
            on = True if t >= b.at else False if t < b.at - AXIS_HYSTERESIS else prev
            if on and not prev and count:
                self.counts[action] += 1
                self._press_seq += 1
                self._last_press[action] = self._press_seq
            self._axis_on[(action, b)] = on

    def wheel_degrees(self) -> int:
        """Lock-to-lock rotation of the wheel: config, else what the driver reports, else 900."""
        return int(self.cfg["steer"].get("wheel_degrees") or self.auto_degrees or DEFAULT_WHEEL_DEGREES)

    def process(self, events: Iterable, wheel: WheelLike, dev: str | None = None) -> None:
        for ev in events:
            if ev.type == e.EV_SYN:
                if ev.code == e.SYN_DROPPED:
                    self._dropped_devs.add(dev)
                elif ev.code == e.SYN_REPORT and dev in self._dropped_devs:
                    self._dropped_devs.discard(dev)
                    self.resync(wheel, dev)
            elif dev in self._dropped_devs:
                continue  # kernel says: discard until the next SYN_REPORT
            elif ev.type == e.EV_ABS:
                if dev is None:
                    self.raw[ev.code] = ev.value
                else:
                    self.xraw[(dev, ev.code)] = ev.value
                if (dev, ev.code) in self._axis_bindings:
                    self._axis_changed(dev, ev.code)
            elif ev.type == e.EV_KEY:
                if ev.value == 1:  # new press (2 = autorepeat, not a press)
                    for action in self._key_actions.get((dev, ev.code), ()):
                        self.counts[action] += 1
                        self._press_seq += 1
                        self._last_press[action] = self._press_seq
                if dev is None:
                    (self.down.add if ev.value else self.down.discard)(ev.code)
                else:
                    (self.xdown.add if ev.value else self.xdown.discard)((dev, ev.code))

    # --- output ----------------------------------------------------------
    def _pedal(self, name: str) -> float:
        p = self.cfg["pedals"][name]
        if p["axis"] is None or p["axis"] not in self.raw:
            return 0.0
        return pedal_norm(self.raw[p["axis"]], p)

    def _note(self, name: str, value: float, resting: bool, now: float) -> bool:
        """Track movement; True while the wheel 'owns' this axis."""
        last = self._last_value.get(name)
        if last is None:  # first sample: just remember it, that is not "movement"
            self._last_value[name] = value
            self._last_change[name] = -1e9
        elif abs(value - last) > _NOISE:
            self._last_value[name] = value
            self._last_change[name] = now
        hold = self.cfg["mod"]["active_hold_s"]
        return (not resting) or (now - self._last_change.get(name, -1e9) < hold)

    def state(self) -> WheelState:
        cfg, now = self.cfg, self.clock()
        s = cfg["steer"]
        steer = steer_norm(self.raw[s["axis"]], s) if s["axis"] in self.raw else 0.0
        throttle, brake, clutch = self._pedal("throttle"), self._pedal("brake"), self._pedal("clutch")

        thr = cfg["clutch_threshold"]
        if clutch >= thr:
            self.clutch_on = True
        elif clutch < thr - self.CLUTCH_HYSTERESIS:
            self.clutch_on = False

        held = set()
        for action, blist in self.bindings.items():
            for b in blist:
                if b.kind == "key":
                    down = (b.code in self.down) if b.dev is None else ((b.dev, b.code) in self.xdown)
                else:
                    down = self._axis_on.get((action, b), False)
                if down:
                    held.add(action)
                    break
        if self.clutch_on:
            held.add("clutch")

        active = {n for n, v, rest in (
            ("steer", steer, abs(steer) < _STEER_ACTIVE),
            ("throttle", throttle, throttle < _PEDAL_ACTIVE),
            ("brake", brake, brake < _PEDAL_ACTIVE),
            ("clutch", clutch, clutch < _PEDAL_ACTIVE),
        ) if self._note(n, v, rest, now)}

        hat = (int(clamp(self.raw.get(e.ABS_HAT0X, 0), -1, 1)), int(clamp(self.raw.get(e.ABS_HAT0Y, 0), -1, 1)))
        # H-shifter: whichever gear button is held now (the most recently pressed if a glitchy shifter reports two)
        engaged = [a for a in GEAR_ACTIONS if a in held]
        gear = max(engaged, key=lambda a: self._last_press.get(a, 0)) if engaged else ""
        hshift = any(self.bindings.get(a) for a in GEAR_ACTIONS)

        # physical angle of the wheel, for the in-game steering wheel
        deg, visual = 0.0, False
        rng = self.axis_range.get(s["axis"])
        if s["axis"] in self.raw and rng and rng[1] > rng[0] and s["center"] is not None and s["right"] is not None:
            sign = 1.0 if s["right"] > s["center"] else -1.0
            deg = sign * (self.raw[s["axis"]] - s["center"]) / (rng[1] - rng[0]) * self.wheel_degrees()
            deg *= s.get("visual_scale", 1.0)
            visual = bool(s.get("wheel_visual", True))
        return WheelState(steer, throttle, brake, clutch, frozenset(held),
                          tuple(sorted(self.counts.items())), hat, frozenset(active), hshift, gear, deg, visual,
                          bool(s.get("camera_follow", True)))
