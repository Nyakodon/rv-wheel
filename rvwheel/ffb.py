"""Force feedback: vehicle telemetry -> wheel forces.

FFModel   pure maths: Telemetry -> FFForces (smoothed, rate-limited, capped)
EvdevFF   uploads/updates the corresponding Linux force-feedback effects on the wheel

Safety rules baked in: every force is scaled by `gain`, hard-capped by `max_level`, slew-rate limited
(no sudden snaps), and faded to zero whenever telemetry stops (game closed, menu, on foot).
"""
from __future__ import annotations

import errno
import math
import sys
import time
from dataclasses import dataclass
from typing import Callable

from evdev import ecodes as e, ff

from .link import Telemetry
from .mapping import clamp

MAX_LEVEL = 32767


@dataclass(frozen=True)
class FFForces:
    spring: float = 0.0     # 0..1 centring stiffness
    damper: float = 0.0     # 0..1 resistance to wheel motion
    constant: float = 0.0   # -1..1, positive pushes the wheel right
    vib: float = 0.0        # 0..1 vibration amplitude (engine / impacts)
    vib_hz: float = 20.0


NO_FORCES = FFForces()


def _smoothstep(x: float) -> float:
    x = clamp(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _approach(cur: float, target: float, max_step: float) -> float:
    if target > cur:
        return min(target, cur + max_step)
    return max(target, cur - max_step)


class FFModel:
    # max change per second of each channel (units of the 0..1 / -1..1 range)
    RATE_SPRING = 3.0
    RATE_DAMPER = 3.0
    RATE_CONSTANT = 6.0
    RATE_VIB_UP = 40.0
    RATE_VIB_DOWN = 10.0
    FADE_OUT = 0.3        # seconds to fade to zero when telemetry disappears
    SLIP_REF = 1.0        # slip value that counts as "fully sliding"
    # Scales measured from recorded drives of the RV (heavy: ~30 km/h cruise, sideways drift stays under ~6 km/h)
    SPEED_REF = 40.0      # km/h at which self-centring is at full weight
    LAT_REF = 6.0         # km/h of sideways drift that gives full slide feedback
    IMPACT_DECEL = 100.0  # km/h/s of deceleration above which we call it an impact
    IMPACT_SPAN = 300.0

    def __init__(self, cfg: dict, clock: Callable[[], float] = time.monotonic):
        self.c = cfg
        self.clock = clock
        self.cur = NO_FORCES
        self._last_t: float | None = None
        self._last_tel_seq: int | None = None
        self._prev_speed = 0.0
        self._prev_tel_t = 0.0
        self._impact = 0.0

    def _target(self, tel: Telemetry, now: float) -> FFForces:
        c = self.c
        speed = abs(tel.speed)
        sf = _smoothstep(speed / self.SPEED_REF)
        grip = 1.0 - 0.6 * clamp(abs(tel.slip) / self.SLIP_REF, 0.0, 1.0)

        spring = c["spring"] * (0.10 + 0.90 * sf) * grip
        damper = c["damper"] * (0.05 + 0.18 * (1.0 - sf))
        constant = c["self_align"] * clamp(tel.lat / self.LAT_REF, -1.0, 1.0) * 0.6 * sf

        # impacts: a large deceleration between two *new* telemetry samples
        if tel.seq != self._last_tel_seq:
            dt = now - self._prev_tel_t
            if self._last_tel_seq is not None and 0.0 < dt < 0.25:
                decel = (abs(self._prev_speed) - speed) / dt   # km/h per second, positive = slowing down
                kick = clamp((decel - self.IMPACT_DECEL) / self.IMPACT_SPAN, 0.0, 1.0)
                self._impact = max(self._impact, kick)
            self._prev_speed, self._prev_tel_t, self._last_tel_seq = tel.speed, now, tel.seq
        engine = 0.0
        hz = 20.0
        if tel.engine:
            engine = c["engine"] * 0.12 * (0.4 + 0.6 * clamp(tel.rpm / 7000.0, 0.0, 1.0))
            hz = clamp(tel.rpm / 60.0 / 2.0, 10.0, 38.0)
        impact = c["impact"] * 0.9 * self._impact
        if impact > engine:
            engine, hz = impact, 26.0
        return FFForces(spring, damper, constant, engine, hz)

    def compute(self, tel: Telemetry | None) -> FFForces:
        """Advance one step and return the forces to play now."""
        now = self.clock()
        dt = 0.0 if self._last_t is None else clamp(now - self._last_t, 0.0, 0.1)
        self._last_t = now
        self._impact *= math.exp(-dt / 0.25)

        if tel is None or not tel.driving:
            # fade out; forget history so the first sample after re-entering can't look like an impact
            self._last_tel_seq = None
            self._impact = 0.0
            fade = dt / self.FADE_OUT
            cur = self.cur
            self.cur = FFForces(_approach(cur.spring, 0, fade), _approach(cur.damper, 0, fade),
                                _approach(cur.constant, 0, fade * 2), _approach(cur.vib, 0, fade * 2), cur.vib_hz)
            return self._finish(self.cur)

        t = self._target(tel, now)
        cur = self.cur
        self.cur = FFForces(
            _approach(cur.spring, t.spring, self.RATE_SPRING * dt),
            _approach(cur.damper, t.damper, self.RATE_DAMPER * dt),
            _approach(cur.constant, t.constant, self.RATE_CONSTANT * dt),
            _approach(cur.vib, t.vib, (self.RATE_VIB_UP if t.vib > cur.vib else self.RATE_VIB_DOWN) * dt),
            t.vib_hz,
        )
        return self._finish(self.cur)

    def _finish(self, f: FFForces) -> FFForces:
        """gain / cap / direction applied last, so internal state stays in 'natural' units."""
        c = self.c
        g, cap = c["gain"], c["max_level"]
        sign = -1.0 if c["invert"] else 1.0
        return FFForces(
            spring=clamp(f.spring * g, 0.0, cap),
            damper=clamp(f.damper * g, 0.0, cap),
            constant=clamp(f.constant * g * sign, -cap, cap),
            vib=clamp(f.vib * g, 0.0, cap),
            vib_hz=f.vib_hz,
        )


def _warn(msg: str) -> None:
    print(f"[rv-wheel] {msg}", file=sys.stderr, flush=True)


class EvdevFF:
    """Plays FFForces on a wheel through Linux evdev force feedback.

    Robust against what real wheels do: a wheel only has a handful of effect slots (and another program may be
    using some), so we keep exactly one effect per kind and *modify* it in place; if the wheel refuses an effect we
    drop that kind and carry on instead of crashing."""

    MIN_DELTA = 0.015     # don't re-upload an effect for changes smaller than this (effects play until stopped)
    PASSTHROUGH = (errno.ENODEV, errno.EIO)   # the wheel went away: let the caller's reconnect logic handle it

    def __init__(self, dev, cfg: dict, clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = _warn):
        self.dev, self.cfg, self.clock, self.log = dev, cfg, clock, log
        caps = set(dev.capabilities().get(e.EV_FF, []))
        self.supported = {
            "constant": e.FF_CONSTANT in caps,
            "spring": e.FF_SPRING in caps,
            "damper": e.FF_DAMPER in caps,
            "vib": e.FF_PERIODIC in caps and e.FF_SINE in caps,
        }
        self.effects: dict[str, ff.Effect] = {}
        self.sent: dict[str, tuple[float, float]] = {}   # name -> (value, time)
        if e.FF_GAIN in caps:
            dev.write(e.EV_FF, e.FF_GAIN, 0xFFFF)  # we scale everything ourselves
        ac = cfg["autocenter"]
        if ac is not None and e.FF_AUTOCENTER in caps:
            dev.write(e.EV_FF, e.FF_AUTOCENTER, int(clamp(ac, 0.0, 1.0) * 0xFFFF))

    # --- effect construction ---------------------------------------------
    @staticmethod
    def _cond(level: float) -> ff.Condition:
        sat = int(clamp(level, 0.0, 1.0) * 0xFFFF)
        coeff = int(clamp(level, 0.0, 1.0) * MAX_LEVEL)
        return ff.Condition(right_saturation=sat, left_saturation=sat, right_coeff=coeff, left_coeff=coeff,
                            deadband=0, center=0)

    def _build(self, name: str, f: FFForces) -> ff.Effect:
        eff = ff.Effect()
        eff.id = self.effects[name].id if name in self.effects else -1
        eff.ff_replay.length = 0      # 0 = until stopped
        eff.ff_replay.delay = 0
        if name == "constant":
            eff.type = e.FF_CONSTANT
            eff.direction = 0x4000
            eff.u.ff_constant_effect.level = int(clamp(f.constant, -1.0, 1.0) * MAX_LEVEL)
        elif name in ("spring", "damper"):
            eff.type = e.FF_SPRING if name == "spring" else e.FF_DAMPER
            level = f.spring if name == "spring" else f.damper
            eff.u.ff_condition_effect[0] = self._cond(level)
            eff.u.ff_condition_effect[1] = self._cond(level)
        else:  # vibration
            eff.type = e.FF_PERIODIC
            eff.direction = 0x4000
            p = eff.u.ff_periodic_effect
            p.waveform = e.FF_SINE
            p.period = int(clamp(1000.0 / max(f.vib_hz, 1.0), 5, 1000))
            p.magnitude = int(clamp(f.vib, 0.0, 1.0) * MAX_LEVEL)
            p.offset = 0
            p.phase = 0
        return eff

    def _value(self, name: str, f: FFForces) -> float:
        return {"constant": f.constant, "spring": f.spring, "damper": f.damper, "vib": f.vib}[name]

    def update(self, f: FFForces) -> None:
        now = self.clock()
        for name, ok in self.supported.items():
            if not ok:
                continue
            value = self._value(name, f)
            prev = self.sent.get(name)
            if prev is not None and abs(value - prev[0]) < self.MIN_DELTA:
                continue
            if prev is None and abs(value) < 1e-4:
                continue  # never started and nothing to play
            eff = self._build(name, f)
            started = name in self.effects
            try:
                ff_id = self.dev.upload_effect(eff)
                # Older python-evdev versions do NOT write the kernel's id back into the effect; without this every
                # update would allocate a brand-new effect until the wheel runs out of slots (ENOSPC).
                if isinstance(ff_id, int) and ff_id >= 0:
                    eff.id = ff_id
                if eff.id < 0:
                    raise OSError(errno.EINVAL, "the wheel gave no effect id")
                self.effects[name] = eff
                if not started:
                    self.dev.write(e.EV_FF, eff.id, 1)
            except OSError as ex:
                if ex.errno in self.PASSTHROUGH:
                    raise
                self._reject(name, ex)
                continue
            self.sent[name] = (value, now)

    def _reject(self, name: str, ex: OSError) -> None:
        """The wheel refused this effect (no free slot, unsupported parameters ...): forget it, keep the rest."""
        eff = self.effects.pop(name, None)
        if eff is not None and eff.id >= 0:
            try:
                self.dev.write(e.EV_FF, eff.id, 0)
                self.dev.erase_effect(eff.id)
            except OSError:
                pass
        self.supported[name] = False
        self.sent.pop(name, None)
        self.log(f"the wheel refused the '{name}' force-feedback effect ({ex.strerror or ex}); continuing without it")

    def stop(self) -> None:
        for eff in self.effects.values():
            for fn in (lambda: self.dev.write(e.EV_FF, eff.id, 0), lambda: self.dev.erase_effect(eff.id)):
                try:
                    fn()
                except OSError:
                    pass
        self.effects.clear()
        self.sent.clear()
