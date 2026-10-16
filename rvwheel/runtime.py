"""Run loops: wheel -> virtual pad, or wheel <-> in-game mod (+ force feedback)."""
from __future__ import annotations

import errno
import select
import signal
import sys
import threading
import time
from typing import Callable

from .config import ConfigError, LOG_PREFIX, PAD_NAME
from .devices import WheelLike, axis_name, detect_wheel_degrees, find_wheel, open_matching
from .ffb import EvdevFF, FFModel
from .link import ModLink
from .model import WheelModel
from .pad import NEUTRAL, to_pad


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr, flush=True)


def _check_axes(wheel: WheelLike, cfg: dict) -> None:
    needed = (cfg["steer"]["axis"], *(p["axis"] for p in cfg["pedals"].values()))
    have = set(wheel.abs_codes())
    missing = [a for a in needed if a is not None and a not in have]
    if missing:
        raise ConfigError(f"{wheel.name} has no axis {[axis_name(m) for m in missing]} - wrong device or stale config?")


def _grab(wheel: WheelLike, cfg: dict) -> None:
    if cfg["grab"]:
        try:
            wheel.grab()
        except OSError as ex:
            log(f"could not grab {wheel.path} ({ex}); the game may also see the raw wheel")


def _setup_degrees(model: WheelModel, wheel: WheelLike) -> None:
    """Work out the wheel's physical rotation range once: config > driver (sysfs `range`) > 900."""
    model.auto_degrees = detect_wheel_degrees(wheel.path)
    if model.cfg["steer"].get("wheel_degrees"):
        src = "from the config"
    elif model.auto_degrees:
        src = "detected from the driver"
    else:
        src = "default; set steer.wheel_degrees if your wheel is set to something else"
    log(f"wheel rotation: {model.wheel_degrees()} degrees lock to lock ({src})")


class ExtraDevices:
    """Other devices that buttons / axes are bound on (a stalk, a handbrake, a button box ...).

    They are opened by name when they exist, retried every few seconds when they don't (they may be plugged in later
    or unplugged mid-game), and their events go to the same model as the wheel's."""

    RETRY_S = 2.0

    def __init__(self, model: WheelModel, cfg: dict, main: WheelLike, opener=None,
                 clock: Callable[[], float] = time.monotonic):
        self.model, self.cfg, self.main, self.clock = model, cfg, main, clock
        self.opener = opener or (lambda want, exclude: open_matching(want, exclude))
        self.want = model.extra_devices()
        self.devs: dict[str, WheelLike] = {}
        self._next_try: dict[str, float] = {}
        self._warned: set[str] = set()

    def refresh(self) -> None:
        now = self.clock()
        for want in self.want:
            if want in self.devs or now < self._next_try.get(want, 0.0):
                continue
            self._next_try[want] = now + self.RETRY_S
            dev = self.opener(want, (self.main.path,))
            if dev is None:
                if want not in self._warned:
                    self._warned.add(want)
                    log(f"waiting for '{want}' (its bound buttons / axes are inactive until it is plugged in)")
                continue
            if self.cfg["grab"]:
                try:
                    dev.grab()
                except OSError:
                    pass
            self.devs[want] = dev
            self.model.resync(dev, want)
            self._warned.discard(want)
            log(f"also reading '{want}' ({dev.path}) for button / axis bindings")

    def fds(self) -> list[int]:
        return [d.fd for d in self.devs.values()]

    def pump(self, fd: int) -> bool:
        for want, dev in list(self.devs.items()):
            if dev.fd != fd:
                continue
            try:
                self.model.process(dev.read(), dev, want)
            except OSError as ex:
                if ex.errno not in (errno.ENODEV, errno.EIO):
                    raise
                log(f"'{want}' was unplugged")
                self._drop(want)
            return True
        return False

    def _drop(self, want: str) -> None:
        dev = self.devs.pop(want, None)
        self.model.drop_device(want)
        self._next_try[want] = self.clock() + self.RETRY_S
        if dev is not None:
            dev.close()

    def close(self) -> None:
        for want in list(self.devs):
            self._drop(want)


def _pump(model: WheelModel, wheel: WheelLike, extras: ExtraDevices, timeout: float) -> bool:
    """Wait up to `timeout` for input from the wheel or any bound device; feed it to the model."""
    ready, _, _ = select.select([wheel.fd, *extras.fds()], [], [], max(0.0, timeout))
    got = False
    for fd in ready:
        if fd == wheel.fd:
            model.process(wheel.read(), wheel)
            got = True
        else:
            got = extras.pump(fd) or got
    return got


def serve_pad(wheel: WheelLike, cfg: dict, sink, stop: threading.Event, extra_opener=None) -> None:
    _check_axes(wheel, cfg)
    _grab(wheel, cfg)
    model = WheelModel(cfg)
    model.resync(wheel)
    extras = ExtraDevices(model, cfg, wheel, extra_opener)
    sink.emit(to_pad(model.state(), cfg))
    log(f"bridging {wheel.name} ({wheel.path}) -> {PAD_NAME}")
    try:
        while not stop.is_set():
            extras.refresh()
            if _pump(model, wheel, extras, 0.25):
                sink.emit(to_pad(model.state(), cfg))
    finally:
        extras.close()
        sink.emit(NEUTRAL)


def serve_mod(wheel: WheelLike, cfg: dict, link: ModLink, stop: threading.Event,
              make_ff: Callable | None = EvdevFF, clock: Callable[[], float] = time.monotonic,
              extra_opener=None) -> None:
    """Feed the in-game mod with wheel state, and play force feedback from its telemetry."""
    _check_axes(wheel, cfg)
    _grab(wheel, cfg)
    model = WheelModel(cfg, clock)
    model.resync(wheel)
    _setup_degrees(model, wheel)
    extras = ExtraDevices(model, cfg, wheel, extra_opener, clock)
    ffmodel, ffout = FFModel(cfg["ffb"], clock), None
    if cfg["ffb"]["enabled"] and make_ff is not None:
        dev = wheel.ff_device()
        if dev is None:
            log("this wheel exposes no force feedback; running without it")
        else:
            ffout = make_ff(dev, cfg["ffb"])
            if not any(ffout.supported.values()):
                log("wheel has force feedback but none of the effect types we use; running without it")
                ffout = None
    log(f"feeding the in-game mod from {wheel.name} ({wheel.path}) via {link.dir}"
        f"{'' if ffout is None else ' + force feedback'}")
    period = 1.0 / max(1, cfg["mod"]["rate_hz"])
    next_t = clock()
    try:
        while not stop.is_set():
            extras.refresh()
            _pump(model, wheel, extras, min(next_t - clock(), 0.25))
            now = clock()
            if now >= next_t:
                next_t = max(next_t + period, now - period)  # never burst to catch up
                link.write_state(model.state())
                if ffout is not None:
                    ffout.update(ffmodel.compute(link.read_telemetry()))
    finally:
        extras.close()
        if ffout is not None:
            ffout.stop()


def run_loop(cfg: dict, serve: Callable[[WheelLike], None], stop: threading.Event,
             opener: Callable[[str | None, bool], WheelLike | None] = lambda d, q: find_wheel(d, q, log)) -> None:
    waited = False
    while not stop.is_set():
        wheel = opener(cfg["device"], waited)
        if wheel is None:
            if not waited:
                log("no wheel found - waiting for one to be plugged in (Ctrl+C to quit)")
                waited = True
            stop.wait(2.0)
            continue
        waited = False
        try:
            serve(wheel)
        except OSError as ex:
            if ex.errno not in (errno.ENODEV, errno.EIO):
                raise
            log("wheel disconnected - waiting for it to come back")
        finally:
            wheel.close()


def install_stop_signals(stop: threading.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
