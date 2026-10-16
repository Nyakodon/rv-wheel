"""Command line."""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from evdev import InputDevice

from .config import ConfigError, default_config_path, load_config, require_configured, save_config
from .devices import Wheel, find_wheel, scan_controllers, scan_devices
from .ffb import EvdevFF, FFForces
from .link import ModLink
from .pad import PrintSink, UInputSink
from .runtime import install_stop_signals, log, run_loop, serve_mod, serve_pad
from .wizard import Console, setup_wizard


def _pick_wheel(args, cfg: dict):
    wheel = find_wheel(args.device or cfg["device"], log=log)
    if wheel is None:
        cands = scan_devices()
        if cands:
            print("No wheel auto-detected. Candidate devices:", file=sys.stderr)
            for path, name, kind in cands:
                print(f"  {path}  {name}  ({kind})", file=sys.stderr)
            print("Re-run with --device <path or part of the name>.", file=sys.stderr)
        else:
            print("No wheel/joystick-like input device found. Is it plugged in, and are you in the 'input' group?",
                  file=sys.stderr)
        raise SystemExit(1)
    return wheel


def cmd_list(args, cfg) -> int:
    cands = scan_devices()
    wheelish = {p for p, _, _ in cands}
    others = [(p, n) for p, n in scan_controllers() if p not in wheelish]
    if not cands and not others:
        print("no wheel / joystick / controller input devices found")
        return 1
    for path, name, kind in cands:
        print(f"{'wheel   ' if kind == 'wheel' else 'joystick'}  {path}  {name}")
    for path, name in others:
        print(f"other     {path}  {name}      (usable for button / axis bindings)")
    return 0


def cmd_setup(args, cfg) -> int:
    wheel = _pick_wheel(args, cfg)
    others = []                    # every other controller, so buttons / axes on them can be bound too
    for path, _name in scan_controllers():
        if path != wheel.path:
            try:
                others.append(Wheel(InputDevice(path)))
            except OSError:
                pass
    try:
        new = setup_wizard(wheel, cfg, Console(), others)
    except (EOFError, KeyboardInterrupt):
        print("\nsetup cancelled - nothing saved")
        return 1
    finally:
        wheel.close()
        for d in others:
            d.close()
    if args.device:
        new["device"] = args.device
    save_config(args.config, new)
    print(f"\nsaved {args.config}\nnext: `rv_wheel.py monitor` to check the values, then `rv_wheel.py mod` (in-game mod)"
          " or `rv_wheel.py run` (virtual gamepad).")
    return 0


def _opener(args):
    return lambda dev, quiet: find_wheel(args.device or dev, quiet, log)


def cmd_monitor(args, cfg) -> int:
    require_configured(cfg)
    cfg = {**cfg, "grab": False}
    stop, sink = threading.Event(), PrintSink()
    install_stop_signals(stop)
    try:
        run_loop(cfg, lambda w: serve_pad(w, cfg, sink, stop), stop, _opener(args))
    finally:
        sink.close()
    return 0


def cmd_run(args, cfg) -> int:
    require_configured(cfg)
    try:
        sink = UInputSink()
    except (PermissionError, FileNotFoundError) as ex:
        print(f"cannot create the virtual gamepad ({ex}). /dev/uinput must be writable "
              "(your user in the 'input' group, and `modprobe uinput`).", file=sys.stderr)
        return 1
    stop = threading.Event()
    install_stop_signals(stop)
    try:
        run_loop(cfg, lambda w: serve_pad(w, cfg, sink, stop), stop, _opener(args))
    finally:
        sink.close()
    return 0


def cmd_mod(args, cfg) -> int:
    from .sim import SCENARIOS, SimWheel, sim_config

    if args.sim:
        if args.sim not in SCENARIOS:
            print(f"unknown scenario {args.sim!r}; choose from {', '.join(SCENARIOS)}", file=sys.stderr)
            return 2
        cfg = sim_config(cfg)
        opener = lambda dev, quiet: SimWheel(SCENARIOS[args.sim], cfg)
        log(f"SIMULATED wheel, scenario '{args.sim}' (no real device is read)")
    else:
        require_configured(cfg)
        opener = _opener(args)
    if args.no_ffb:
        cfg["ffb"]["enabled"] = False
    link = ModLink(args.dir or cfg["mod"]["dir"])
    stop = threading.Event()
    install_stop_signals(stop)
    try:
        run_loop(cfg, lambda w: serve_mod(w, cfg, link, stop, EvdevFF), stop, opener)
    finally:
        link.close()
    return 0


def cmd_ffb_test(args, cfg) -> int:
    """Gentle force-feedback self-test so you can confirm direction and strength."""
    wheel = _pick_wheel(args, cfg)
    dev = wheel.ff_device()
    if dev is None:
        print(f"{wheel.name} does not report force-feedback support.")
        return 1
    ffcfg = cfg["ffb"]
    out = EvdevFF(dev, ffcfg)
    print(f"supported effects: {', '.join(k for k, v in out.supported.items() if v) or 'none'}")
    print("Hold the wheel lightly. Stopping with Ctrl+C is always safe.")
    sign = -1.0 if ffcfg["invert"] else 1.0
    steps = [
        ("a gentle push to the LEFT", FFForces(constant=-0.20 * sign)),
        ("a gentle push to the RIGHT", FFForces(constant=0.20 * sign)),
        ("centring spring (turn the wheel and let go)", FFForces(spring=0.30, damper=0.1)),
        ("engine-like vibration", FFForces(vib=0.10, vib_hz=22.0)),
    ]
    stop = threading.Event()
    install_stop_signals(stop)
    try:
        for label, forces in steps:
            if stop.is_set():
                break
            print(f"  -> {label}")
            end = time.monotonic() + 2.5
            while time.monotonic() < end and not stop.is_set():
                out.update(forces)
                time.sleep(0.05)
            out.update(FFForces())
            time.sleep(0.4)
        print("If LEFT/RIGHT felt swapped, set \"invert\": true in the \"ffb\" section of the config.")
    finally:
        out.stop()
        wheel.close()
    return 0


def _game_dir(args) -> Path:
    from . import install
    game = Path(args.game_dir) if args.game_dir else install.find_game()
    if game is None:
        raise install.InstallError("could not find the game in your Steam libraries; pass --game-dir "
                                   "<.../steamapps/common/Ride>")
    return game


def cmd_install_mod(args, cfg) -> int:
    from . import install
    try:
        install.install(_game_dir(args), Path(args.ue4ss_zip) if args.ue4ss_zip else None, force=args.force,
                        mods=args.mod)
    except install.InstallError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    return 0


def cmd_uninstall_mod(args, cfg) -> int:
    from . import install
    try:
        install.uninstall(_game_dir(args), everything=args.all, force=args.force, mods=args.mod)
    except install.InstallError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rv_wheel.py", description=__doc__)
    ap.add_argument("--config", type=Path, default=default_config_path(), help="config file (default: %(default)s)")
    ap.add_argument("--device", help="wheel to use: /dev/input/eventN or part of its name")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list wheel/joystick-like input devices").set_defaults(fn=cmd_list)
    sub.add_parser("setup", help="interactive calibration + button binding").set_defaults(fn=cmd_setup)
    sub.add_parser("monitor", help="show what would be sent to the virtual pad, without creating it").set_defaults(fn=cmd_monitor)
    sub.add_parser("run", help="virtual gamepad mode: no game modification needed").set_defaults(fn=cmd_run)
    m = sub.add_parser("mod", help="in-game mod mode: feed the RVWheel UE4SS mod + force feedback")
    m.add_argument("--dir", help="exchange directory shared with the mod (default from config)")
    m.add_argument("--no-ffb", action="store_true", help="disable force feedback")
    m.add_argument("--sim", metavar="SCENARIO", help="use a scripted fake wheel instead of hardware (e.g. demo)")
    m.set_defaults(fn=cmd_mod)
    sub.add_parser("ffb-test", help="play gentle test forces on the wheel").set_defaults(fn=cmd_ffb_test)
    i = sub.add_parser("install-mod", help="install UE4SS + the RVWheel mod into the game folder (game must be closed)")
    i.add_argument("--game-dir", help="path to .../steamapps/common/Ride (default: found via Steam)")
    i.add_argument("--ue4ss-zip", help="use this UE4SS zip instead of downloading the latest experimental build")
    i.add_argument("--mod", default="all", metavar="NAME",
                   help="RVWheel (the only mod in this package; default)")
    i.add_argument("--force", action="store_true", help="install even if the game appears to be running")
    i.set_defaults(fn=cmd_install_mod)
    u = sub.add_parser("uninstall-mod", help="remove the RVWheel mod (and with --all, UE4SS too)")
    u.add_argument("--game-dir")
    u.add_argument("--all", action="store_true", help="also remove UE4SS")
    u.add_argument("--mod", default="all", metavar="NAME", help="RVWheel (default)")
    u.add_argument("--force", action="store_true")
    u.set_defaults(fn=cmd_uninstall_mod)
    args = ap.parse_args(argv)
    try:
        return args.fn(args, load_config(args.config))
    except ConfigError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
