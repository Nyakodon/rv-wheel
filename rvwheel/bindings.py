"""What an action can be bound to: a button OR an axis, on the main wheel OR on any other device.

Stored in the config as, per action, a list of:
    289                                        a button on the main wheel (the original format - still valid)
    {"dev": "MOZA Multi-function Stalk", "key": 289}
    {"dev": "MOZA HBP Handbrake", "axis": 2, "rest": 0, "full": 4095, "at": 0.5}     an axis acts as a button once
                                                                                    it is pushed past `at` (0..1)
`dev` is the device's name (stable across replugging; a /dev/input/eventN path also works); no `dev` = the main wheel.
"""
from __future__ import annotations

from dataclasses import dataclass

AXIS_ON_DEFAULT = 0.5
AXIS_HYSTERESIS = 0.1


class BindingError(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    dev: str | None          # None = the main wheel
    kind: str                # "key" | "axis"
    code: int
    rest: int = 0            # axis only
    full: int = 0
    at: float = AXIS_ON_DEFAULT

    @staticmethod
    def parse(raw) -> "Binding":
        if isinstance(raw, bool):
            raise BindingError(f"invalid binding {raw!r}")
        if isinstance(raw, int):
            return Binding(None, "key", raw)
        if isinstance(raw, dict):
            dev = raw.get("dev") or None
            if "key" in raw:
                return Binding(dev, "key", int(raw["key"]))
            if "axis" in raw:
                rest, full = int(raw.get("rest", 0)), int(raw.get("full", 0))
                if rest == full:
                    raise BindingError(f"axis binding {raw!r} has no travel (rest == full)")
                at = float(raw.get("at", AXIS_ON_DEFAULT))
                if not 0.05 <= at <= 0.95:
                    raise BindingError(f"axis binding {raw!r}: 'at' must be between 0.05 and 0.95")
                return Binding(dev, "axis", int(raw["axis"]), rest, full, at)
        raise BindingError(f"invalid binding {raw!r}")

    def to_config(self):
        """Smallest config form: a bare int for a main-wheel button."""
        if self.kind == "key":
            return self.code if self.dev is None else {"dev": self.dev, "key": self.code}
        d = {"axis": self.code, "rest": self.rest, "full": self.full, "at": self.at}
        return ({"dev": self.dev} | d) if self.dev is not None else d

    def travel(self, raw_value: int) -> float:
        """Axis position as 0..1 between `rest` and `full` (any polarity)."""
        t = (raw_value - self.rest) / (self.full - self.rest)
        return 0.0 if t < 0.0 else 1.0 if t > 1.0 else t

    def identity(self) -> tuple:
        """What physical input this is (two bindings with the same identity compete for one button/axis)."""
        return (self.dev, self.kind, self.code)

    def describe(self, name_of_key, name_of_axis) -> str:
        what = name_of_key(self.code) if self.kind == "key" else f"{name_of_axis(self.code)} past {int(self.at * 100)}%"
        return f"{what} on {self.dev}" if self.dev else what


def parse_all(raw_list) -> list[Binding]:
    out = []
    for item in raw_list or []:
        try:
            out.append(Binding.parse(item))
        except (BindingError, TypeError, ValueError):
            raise BindingError(f"invalid binding in config: {item!r}")
    return out
