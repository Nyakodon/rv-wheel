"""Interactive calibration + button binding."""
from __future__ import annotations

import copy
import select
import sys
import time
from typing import Iterable

from evdev import ecodes as e

from .bindings import Binding
from .config import ACTIONS
from .devices import WheelLike, axis_name, detect_wheel_degrees, key_name

AXIS_TRIGGER = 0.40   # an axis must move this far (fraction of its range) to count as "that one"
AXIS_SETTLE_S = 0.6   # ... then we watch this long to see how far it really goes


class Console:
    def pause(self, msg: str) -> str:
        return input(f"\n{msg}\n  > press Enter when ready: ").strip().lower()

    def degrees(self, wheel: WheelLike) -> int | None:
        """Ask for the wheel's rotation range. Enter keeps auto-detection / the 900 default."""
        found = detect_wheel_degrees(wheel.path)
        hint = f"detected {found}" if found else "not reported by the driver, 900 is assumed"
        ans = input(f"\nWHEEL ROTATION: what is your wheel set to, lock to lock, in degrees?  ({hint})\n"
                    "  The in-game steering wheel is turned 1:1 with it.  Enter = keep this: ").strip()
        try:
            val = int(ans)
        except ValueError:
            return None
        return val if 40 <= val <= 3600 else None

    # --- waiting for input (separate so tests can drive the capture logic without a terminal) ------------------
    def _wait(self, devices: list, timeout: float | None):
        """-> (enter_pressed, [devices with data])"""
        ready, _, _ = select.select([d.fd for d in devices] + [sys.stdin], [], [], timeout)
        if sys.stdin in ready:
            sys.stdin.readline()
            return True, []
        return False, [d for d in devices if d.fd in ready]

    def binding(self, devices: list, msg: str, ignore=frozenset()) -> Binding | None:
        """Wait for ONE input: a button press, or an axis pushed (nearly) all the way, on any of `devices`.
        devices[0] is the main wheel (stored without a device name); Enter on the keyboard skips."""
        names = [None] + [d.name for d in devices[1:]]
        print(f"\n{msg}\n  > press a button, or push an axis all the way, on the wheel or any other device "
              "(or Enter to skip): ", end="", flush=True)
        for d in devices:
            d.read()  # drop stale events
        base = {(i, c): d.abs_value(c) for i, d in enumerate(devices) for c in d.abs_codes()
                if not (e.ABS_HAT0X <= c <= e.ABS_HAT3Y)}
        while True:
            skipped, ready = self._wait(devices, None)
            if skipped:
                print("skipped")
                return None
            for d in ready:
                i = devices.index(d)
                for ev in d.read():
                    if ev.type == e.EV_KEY and ev.value == 1 and ev.code >= 0x100:
                        b = Binding(names[i], "key", ev.code)
                    elif ev.type == e.EV_ABS and (i, ev.code) in base and (names[i], ev.code) not in ignore:
                        info = d.abs_info(ev.code)
                        span = max(1, info.max - info.min)
                        if abs(ev.value - base[(i, ev.code)]) < AXIS_TRIGGER * span:
                            continue
                        b = self._settle_axis(d, names[i], ev.code, base[(i, ev.code)], ev.value)
                    else:
                        continue
                    print(b.describe(key_name, axis_name))
                    return b

    def _settle_axis(self, dev, name, code, rest, first) -> Binding:
        """The axis just crossed the trigger distance: watch a moment to find how far it really goes."""
        far, deadline = first, time.monotonic() + AXIS_SETTLE_S
        while (left := deadline - time.monotonic()) > 0:
            _, ready = self._wait([dev], left)
            for ev in (dev.read() if ready else ()):
                if ev.type == e.EV_ABS and ev.code == code and abs(ev.value - rest) > abs(far - rest):
                    far = ev.value
        return Binding(name, "axis", code, rest, far)


def _is_hat(code: int) -> bool:
    return e.ABS_HAT0X <= code <= e.ABS_HAT3Y


def _snapshot(wheel: WheelLike) -> dict[int, int]:
    return {c: wheel.abs_value(c) for c in wheel.abs_codes() if not _is_hat(c)}


def _most_moved(wheel: WheelLike, before: dict[int, int], after: dict[int, int],
                exclude: Iterable[int] = ()) -> tuple[int | None, float]:
    best, best_frac = None, 0.0
    for code, v0 in before.items():
        if code in exclude:
            continue
        info = wheel.abs_info(code)
        span = max(1, info.max - info.min)
        frac = abs(after[code] - v0) / span
        if frac > best_frac:
            best, best_frac = code, frac
    return best, best_frac


def setup_wizard(wheel: WheelLike, cfg: dict, io, others=()) -> dict:
    """`others`: more devices (a stalk, a handbrake ...) that buttons / axes may be bound on."""
    cfg = copy.deepcopy(cfg)
    used: set[int] = set()
    devices = [wheel, *others]

    print(f"\nSetting up: {wheel.name}")
    if others:
        print("Also watching for bindings: " + ", ".join(d.name for d in others))

    # --- steering -----------------------------------------------------------
    while True:
        io.pause("STEERING 1/3: centre the wheel and take your feet off the pedals.")
        base = _snapshot(wheel)
        io.pause("STEERING 2/3: turn the wheel to the position that should count as FULL LEFT and hold it there.\n"
                 "  (a lot less than the wheel's maximum is fine - e.g. 1 turn each way)")
        left = _snapshot(wheel)
        axis, frac = _most_moved(wheel, base, left)
        if axis is None or frac < 0.1:
            print("  no axis moved enough - try again.")
            continue
        io.pause("STEERING 3/3: now turn to FULL RIGHT and hold it there.")
        right = _snapshot(wheel)
        if (left[axis] - base[axis]) * (right[axis] - base[axis]) >= 0 or abs(right[axis] - left[axis]) < 1:
            print("  right wasn't on the opposite side of centre from left - try again.")
            continue
        break
    cfg["steer"].update(axis=axis, left=left[axis], right=right[axis], center=base[axis])
    used.add(axis)
    print(f"  steering = {axis_name(axis)} (left {left[axis]}, centre {base[axis]}, right {right[axis]})")
    degrees = io.degrees(wheel) if hasattr(io, "degrees") else None
    cfg["steer"]["wheel_degrees"] = degrees          # None = auto-detect each run, else 900
    if degrees:
        print(f"  wheel rotation = {degrees} degrees")

    # --- pedals -------------------------------------------------------------
    for name, optional in (("throttle", False), ("brake", False), ("clutch", True)):
        while True:
            hint = "  (type s then Enter to skip - you can still bind a button to it)" if optional else ""
            ans = io.pause(f"{name.upper()} pedal: press and release it a couple of times, then leave it RELEASED.{hint}")
            if optional and ans == "s":
                print("  skipped")
                break
            rest = _snapshot(wheel)
            io.pause(f"{name.upper()} pedal: press it ALL THE WAY DOWN and hold it.")
            full = _snapshot(wheel)
            axis, frac = _most_moved(wheel, rest, full, exclude=used)
            if axis is None or frac < 0.3:
                print("  no new axis moved enough - try again.")
                continue
            cfg["pedals"][name].update(axis=axis, rest=rest[axis], full=full[axis])
            used.add(axis)
            print(f"  {name} = {axis_name(axis)} (released {rest[axis]}, pressed {full[axis]})")
            break

    # --- buttons ------------------------------------------------------------
    print("\nBINDINGS: bind a button OR an axis (e.g. an analog handbrake lever) - on the wheel or on any other\n"
          "connected device (stalk, button box ...) - to each action. Enter on the keyboard skips one.")
    taken: dict[tuple, str] = {}
    ignore = {(None, c) for c in used}            # the steering / pedal axes are not bindable by accident

    def bind(action) -> None:
        cfg["buttons"][action.name] = []
        while True:
            b = io.binding(devices, f"{action.name.upper()}: {action.desc}", ignore)
            if b is None:
                return
            binding = b if isinstance(b, Binding) else Binding.parse(b)
            if binding.identity() in taken:
                print(f"  {binding.describe(key_name, axis_name)} is already bound to {taken[binding.identity()]}"
                      " - pick another or skip.")
                continue
            cfg["buttons"][action.name] = [binding.to_config()]
            taken[binding.identity()] = action.name
            return

    basic = [a for a in ACTIONS if not a.name.startswith(("gear_", "transmission", "reset"))]
    extra = [a for a in ACTIONS if a not in basic]
    for a in basic:
        bind(a)
    if io.pause("Also bind the gear / transmission / reset buttons (in-game mod only)?  type y to bind, or just Enter to skip.") == "y":
        for a in extra:
            bind(a)
    return cfg
