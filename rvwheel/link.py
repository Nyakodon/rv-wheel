"""File protocol between this daemon and the in-game Lua mod (RVWheel).

Two small `key=value` text files in one directory (default /dev/shm/rv-wheel, i.e. RAM):

  state.txt      daemon -> mod   wheel axes, held actions, press counters      (rewritten ~60 Hz)
  telemetry.txt  mod -> daemon   vehicle speed/rpm/slip/... for force feedback  (rewritten ~30 Hz)

Each file is replaced as a whole (temp + rename; the Lua side must delete first because Wine's rename
cannot overwrite) and ends with `end=1`, so a reader that catches a half-written file simply ignores it.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model import WheelState

PROTOCOL = 1


def _fmt(x: float) -> str:
    return f"{x + 0.0:.5f}"   # "+ 0.0" turns -0.0 into 0.0


def encode_state(ws: WheelState, seq: int, session: int) -> str:
    lines = [
        f"rvw={PROTOCOL}",
        f"session={session}",
        f"seq={seq}",
        f"steer={_fmt(ws.steer)}",
        f"throttle={_fmt(ws.throttle)}",
        f"brake={_fmt(ws.brake)}",
        f"clutch={_fmt(ws.clutch)}",
        f"active={','.join(sorted(ws.active))}",
        f"held={','.join(sorted(ws.held))}",
        f"hshift={1 if ws.hshift else 0}",
        f"gear={ws.gear}",
        f"wheel_deg={ws.wheel_deg:.2f}",
        f"visual={1 if ws.visual else 0}",
        f"camera={1 if ws.camera else 0}",
    ]
    lines += [f"n.{a}={n}" for a, n in ws.counts if n]
    lines.append("end=1")   # readers reject a file without this terminator (torn write)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Telemetry:
    seq: int = 0
    driving: bool = False   # the local player is in a vehicle
    speed: float = 0.0      # km/h, signed along the vehicle's forward axis
    lat: float = 0.0        # km/h sideways (positive = sliding to the right)
    yaw: float = 0.0        # deg/s (positive = turning right)
    rpm: float = 0.0
    steer: float = 0.0      # actual steering applied by the vehicle, -1..1
    slip: float = 0.0       # tyre slip as reported by the vehicle
    torque: float = 0.0
    gear: int = 0
    shifter: int = 0        # 0 park, 1 reverse, 2 neutral, 3 drive
    engine: bool = False
    wheel_on: bool = False  # the mod is currently applying wheel input


_FLOATS = {"speed", "lat", "yaw", "rpm", "steer", "slip", "torque"}
_INTS = {"seq", "gear", "shifter"}
_BOOLS = {"driving", "engine", "wheel_on"}


def parse_telemetry(text: str) -> Telemetry | None:
    kv = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            kv[k.strip()] = v.strip()
    if kv.get("rvw") != str(PROTOCOL) or kv.get("end") != "1":
        return None
    out = {}
    try:
        for k, v in kv.items():
            if k in _FLOATS:
                out[k] = float(v)
            elif k in _INTS:
                out[k] = int(float(v))
            elif k in _BOOLS:
                out[k] = v in ("1", "true")
    except ValueError:
        return None
    return Telemetry(**out)


class ModLink:
    """Daemon side of the link."""

    STALE_S = 0.6

    def __init__(self, directory: str | os.PathLike, clock: Callable[[], float] = time.monotonic):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o755)
        self.state_path = self.dir / "state.txt"
        self.telemetry_path = self.dir / "telemetry.txt"
        self.clock = clock
        self.session = random.getrandbits(31)
        self.seq = 0
        self._last_tel_seq: int | None = None
        self._last_tel_change = 0.0
        self._last_tel: Telemetry | None = None
        self._stale_reported = True

    def write_state(self, ws: WheelState) -> None:
        self.seq += 1
        tmp = self.dir / "state.tmp"
        tmp.write_text(encode_state(ws, self.seq, self.session))
        os.replace(tmp, self.state_path)

    def read_telemetry(self) -> Telemetry | None:
        """Latest telemetry, or None if the mod isn't writing (game closed / not in a level)."""
        now = self.clock()
        try:
            tel = parse_telemetry(self.telemetry_path.read_text())
        except OSError:
            tel = None
        if tel is not None and tel.seq != self._last_tel_seq:
            self._last_tel_seq = tel.seq
            self._last_tel_change = now
            self._last_tel = tel
        if self._last_tel is None or now - self._last_tel_change > self.STALE_S:
            return None
        return self._last_tel

    def close(self) -> None:
        for p in (self.state_path, self.dir / "state.tmp"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
