"""Finding and reading the wheel (evdev)."""
from __future__ import annotations

import re
from typing import Protocol

from evdev import AbsInfo, InputDevice, ecodes as e, list_devices

from .config import PAD_NAME, LOG_PREFIX  # noqa: F401  (LOG_PREFIX re-exported for convenience)

_RANGE_ALIASES = {"BTN_JOYSTICK", "BTN_GAMEPAD", "BTN_MISC", "BTN_MOUSE", "BTN_DIGI", "BTN_TRIGGER_HAPPY"}


def _first(name) -> str:
    """ecodes reverse maps give a tuple/list for aliased codes, e.g. (BTN_JOYSTICK, BTN_TRIGGER):
    prefer a specific name over the generic range-start alias."""
    if not isinstance(name, (list, tuple)):
        return name
    return next((n for n in name if n not in _RANGE_ALIASES), name[0])


def key_name(code: int) -> str:
    return _first(e.keys.get(code) or f"code {code}")


def axis_name(code: int) -> str:
    return _first(e.ABS.get(code, f"axis {code}"))


class WheelLike(Protocol):
    name: str
    path: str
    fd: int

    def abs_codes(self) -> list[int]: ...
    def abs_info(self, code: int) -> AbsInfo: ...
    def abs_value(self, code: int) -> int: ...
    def pressed(self) -> set[int]: ...
    def read(self) -> list: ...
    def grab(self) -> None: ...
    def close(self) -> None: ...
    def ff_device(self): ...  # the evdev device to upload force-feedback effects to, or None


class Wheel:
    """Thin adapter over evdev.InputDevice (also the seam used by the tests)."""

    def __init__(self, dev: InputDevice):
        self.dev = dev
        self.name = dev.name
        self.path = dev.path
        self.fd = dev.fd

    def abs_codes(self) -> list[int]:
        return [c for c, _ in self.dev.capabilities(absinfo=True).get(e.EV_ABS, [])]

    def abs_info(self, code: int) -> AbsInfo:
        return self.dev.absinfo(code)

    def abs_value(self, code: int) -> int:
        return self.dev.absinfo(code).value

    def pressed(self) -> set[int]:
        return set(self.dev.active_keys())

    def read(self) -> list:
        try:
            return list(self.dev.read())
        except BlockingIOError:
            return []

    def grab(self) -> None:
        self.dev.grab()

    def ff_device(self):
        return self.dev if e.EV_FF in self.dev.capabilities() else None

    def close(self) -> None:
        try:
            self.dev.close()
        except OSError:
            pass


_WHEEL_NAME = re.compile(
    r"wheel|racing|driving force|\bg(?:25|27|29|920|923)\b|\bdfgt\b|thrustmaster|fanatec|moza|simagic"
    r"|\btmx\b|\bt(?:150|248|300|500|818)|gt force|simucube|cammus|\bpxn\b|\bffb\b",
    re.I,
)


def classify(name: str, caps: dict) -> str | None:
    """'wheel', 'joystick' or None (not a candidate) from a device's name + capabilities."""
    if name == PAD_NAME:
        return None
    abs_codes = {c for c, _ in caps.get(e.EV_ABS, [])}
    keys = set(caps.get(e.EV_KEY, []))
    if e.ABS_X not in abs_codes or e.EV_REL in caps or e.KEY_A in keys:
        return None  # mice, keyboards, tablets, touchpads...
    looks_like_wheel = e.ABS_WHEEL in abs_codes or e.BTN_WHEEL in keys or bool(_WHEEL_NAME.search(name))
    if keys & {e.BTN_LEFT, e.BTN_TOUCH, e.BTN_TOOL_PEN} and not looks_like_wheel:
        return None  # absolute pointers / tablets
    if looks_like_wheel:
        return "wheel"
    return "joystick"


def scan_devices() -> list[tuple[str, str, str]]:
    """[(path, name, kind)] for every wheel/joystick-like evdev device."""
    found = []
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        try:
            kind = classify(dev.name, dev.capabilities(absinfo=True))
        finally:
            dev.close()
        if kind:
            found.append((path, dev.name, kind))
    return found


def find_wheel(device: str | None, quiet: bool = False, log=print) -> Wheel | None:
    cands = scan_devices()
    chosen = None
    if device:
        want = device.lower()
        for path, name, _ in cands:
            if path == device or want in name.lower():
                chosen = path
                break
        if chosen is None and device.startswith("/dev/"):
            chosen = device  # explicit path: trust the user
    else:
        wheels = [p for p, _, k in cands if k == "wheel"]
        if len(wheels) > 1 and not quiet:
            log(f"multiple wheels found, using {wheels[0]} (set \"device\" in the config to choose)")
        chosen = wheels[0] if wheels else None
    if chosen is None:
        return None
    try:
        return Wheel(InputDevice(chosen))
    except OSError as ex:
        log(f"cannot open {chosen}: {ex}")
        return None


def detect_wheel_degrees(event_path: str, sysfs: str = "/sys/class/input") -> int | None:
    """The wheel's configured rotation range (lock to lock, degrees) if its kernel driver exposes it.

    Logitech / Fanatec / Thrustmaster style drivers publish a `range` attribute on the HID device, a few levels
    above the input node. Returns None when the driver says nothing (the caller then uses config, or 900)."""
    import os

    here = os.path.realpath(os.path.join(sysfs, os.path.basename(event_path)))
    for _ in range(8):
        here = os.path.dirname(here)
        if here in ("/", "/sys", "/sys/devices"):
            break
        try:
            with open(os.path.join(here, "range")) as f:
                val = int(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            continue
        if 40 <= val <= 3600:
            return val
    return None


def is_controller(name: str, caps: dict) -> bool:
    """Anything that can supply buttons / axes for bindings: wheels, stalks, button boxes, handbrakes, pedals, pads.

    Broader than classify(): a stalk or button box may have buttons only (no ABS_X). Keyboards, mice, tablets,
    audio/power keys and our own virtual pad are excluded."""
    if name == PAD_NAME:
        return False
    keys = set(caps.get(e.EV_KEY, []))
    if e.EV_REL in caps or e.KEY_A in keys:
        return False
    wheel_like = bool(_WHEEL_NAME.search(name)) or e.BTN_WHEEL in keys
    if keys & {e.BTN_LEFT, e.BTN_TOUCH, e.BTN_TOOL_PEN} and not wheel_like:
        return False
    has_buttons = any(0x100 <= k <= 0x15F or 0x2C0 <= k <= 0x2E7 for k in keys)   # BTN_MISC..BTN_GEAR_UP, TRIGGER_HAPPY
    has_axes = any(not (e.ABS_HAT0X <= c <= e.ABS_HAT3Y) for c, _ in caps.get(e.EV_ABS, []))
    return has_buttons or has_axes


def scan_controllers() -> list[tuple[str, str]]:
    """[(path, name)] of every controller-like evdev device (see is_controller)."""
    found = []
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        try:
            ok = is_controller(dev.name, dev.capabilities(absinfo=True))
        finally:
            dev.close()
        if ok:
            found.append((path, dev.name))
    return found


def open_matching(want: str, exclude_paths: tuple = (), scan=scan_controllers, opener=None) -> "Wheel | None":
    """Open the controller called `want` (exact name, else case-insensitive substring; or a /dev/input path)."""
    opener = opener or (lambda p: Wheel(InputDevice(p)))
    cands = [(p, n) for p, n in scan() if p not in exclude_paths]
    pick = next((p for p, n in cands if p == want), None) \
        or next((p for p, n in cands if n == want), None) \
        or next((p for p, n in cands if want.lower() in n.lower()), None)
    if pick is None:
        return None
    try:
        return opener(pick)
    except OSError:
        return None
