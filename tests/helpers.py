"""Shared test fixtures: a fake wheel (implements devices.WheelLike) and calibrated configs."""
import copy
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evdev import AbsInfo, InputEvent, ecodes as e

from rvwheel.config import DEFAULT_CONFIG

Ev = lambda t, c, v: InputEvent(0, 0, t, c, v)


class FakeWheel:
    name = "Fake G29 Racing Wheel"
    path = "/dev/input/eventFAKE"

    def __init__(self, axes, keys=(), ff=None, name=None):
        if name:
            self.name = name
        self.axes = dict(axes)  # code -> (value, min, max)
        self.keys = set(keys)
        self.queue = []
        self.r, self.w = os.pipe()
        self.fd = self.r
        self.grabbed = False
        self.ff = ff

    def abs_codes(self): return list(self.axes)
    def abs_info(self, c): v, lo, hi = self.axes[c]; return AbsInfo(v, lo, hi, 0, 0, 0)
    def abs_value(self, c): return self.axes[c][0]
    def pressed(self): return set(self.keys)
    def grab(self): self.grabbed = True
    def ff_device(self): return self.ff
    def close(self):
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass

    def set(self, code, value):
        _, lo, hi = self.axes[code]
        self.axes[code] = (value, lo, hi)

    def push(self, *events):
        self.queue.extend(events)
        os.write(self.w, b"x")

    def read(self):
        out, self.queue = self.queue, []
        return out


def g29_like(**kw):
    # wheel 0..65535 (centre 32767), pedals 255 = released / 0 = floored
    return FakeWheel({
        e.ABS_X: (32767, 0, 65535), e.ABS_Y: (255, 0, 255), e.ABS_Z: (255, 0, 255),
        e.ABS_RZ: (255, 0, 255), e.ABS_HAT0X: (0, -1, 1), e.ABS_HAT0Y: (0, -1, 1),
    }, **kw)


def calibrated_cfg():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["steer"].update(axis=e.ABS_X, left=16384, right=49150, center=32767)
    cfg["pedals"]["throttle"].update(axis=e.ABS_Y, rest=255, full=0)
    cfg["pedals"]["brake"].update(axis=e.ABS_RZ, rest=255, full=0)
    cfg["pedals"]["clutch"].update(axis=e.ABS_Z, rest=255, full=0)
    cfg["buttons"]["handbrake"] = [e.BTN_TRIGGER]
    cfg["buttons"]["horn"] = [e.BTN_THUMB]
    cfg["buttons"]["lights"] = [e.BTN_THUMB2]
    cfg["buttons"]["gear_up"] = [e.BTN_TOP]
    return cfg


def hshift_cfg():
    """calibrated_cfg() plus an H-pattern shifter on six buttons."""
    cfg = calibrated_cfg()
    for i, a in enumerate(("gear_1", "gear_2", "gear_3", "gear_4", "gear_5", "gear_r")):
        cfg["buttons"][a] = [e.BTN_BASE + i]
    return cfg


class Clock:
    """Manually advanced clock."""
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt
