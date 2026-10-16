"""Virtual Xbox 360 gamepad output (uinput) - the `run` mode."""
from __future__ import annotations

from dataclasses import dataclass

from evdev import AbsInfo, UInput, ecodes as e

from .config import PAD_ACTIONS, PAD_BUTTONS, PAD_NAME
from .devices import key_name
from .mapping import apply_game_deadzone
from .model import WheelState

STICK_MAX = 32767
TRIGGER_MAX = 255


@dataclass(frozen=True)
class PadState:
    x: int = 0
    lt: int = 0
    rt: int = 0
    hat_x: int = 0
    hat_y: int = 0
    buttons: frozenset = frozenset()


NEUTRAL = PadState()


def to_pad(ws: WheelState, cfg: dict) -> PadState:
    """Wheel state -> virtual gamepad: steering on the left stick, pedals on the triggers."""
    x = apply_game_deadzone(ws.steer, cfg["steer"]["game_deadzone"])
    buttons = {PAD_ACTIONS[a].pad_code for a in ws.held if a in PAD_ACTIONS}
    hat = ws.hat if cfg["hat"] else (0, 0)
    return PadState(
        x=int(round(x * STICK_MAX)),
        lt=int(round(ws.brake * TRIGGER_MAX)),
        rt=int(round(ws.throttle * TRIGGER_MAX)),
        hat_x=hat[0],
        hat_y=hat[1],
        buttons=frozenset(buttons),
    )


def uinput_capabilities() -> dict:
    stick = AbsInfo(value=0, min=-32768, max=STICK_MAX, fuzz=0, flat=0, resolution=0)
    trig = AbsInfo(value=0, min=0, max=TRIGGER_MAX, fuzz=0, flat=0, resolution=0)
    hat = AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)
    return {
        e.EV_KEY: sorted({*PAD_BUTTONS, e.BTN_MODE, e.BTN_THUMBL}),
        e.EV_ABS: [
            (e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_RX, stick), (e.ABS_RY, stick),
            (e.ABS_Z, trig), (e.ABS_RZ, trig),
            (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat),
        ],
    }


class UInputSink:
    """Emits PadState changes on a virtual Xbox 360 controller."""

    def __init__(self, ui=None):
        # 045e:028e is the wired Xbox 360 pad, which SDL/Proton recognise out of the box.
        self.ui = ui or UInput(
            uinput_capabilities(), name=PAD_NAME, vendor=0x045E, product=0x028E,
            version=0x0110, bustype=e.BUS_USB,
        )
        self.last = NEUTRAL

    def emit(self, s: PadState) -> None:
        p, ui, changed = self.last, self.ui, False
        for code, new, old in (
            (e.ABS_X, s.x, p.x), (e.ABS_Z, s.lt, p.lt), (e.ABS_RZ, s.rt, p.rt),
            (e.ABS_HAT0X, s.hat_x, p.hat_x), (e.ABS_HAT0Y, s.hat_y, p.hat_y),
        ):
            if new != old:
                ui.write(e.EV_ABS, code, new)
                changed = True
        for code in PAD_BUTTONS:
            new, old = code in s.buttons, code in p.buttons
            if new != old:
                ui.write(e.EV_KEY, code, int(new))
                changed = True
        if changed:
            ui.syn()
        self.last = s

    def close(self) -> None:
        try:
            self.emit(NEUTRAL)
        finally:
            self.ui.close()


class PrintSink:
    def emit(self, s: PadState) -> None:
        names = ",".join(sorted(key_name(c) for c in s.buttons))
        print(f"\rsteer {s.x:+6d}  throttle {s.rt:3d}  brake {s.lt:3d}  hat {s.hat_x:+d},{s.hat_y:+d}  [{names:<28}]",
              end="", flush=True)

    def close(self) -> None:
        print()
