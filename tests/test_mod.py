"""Offline tests for the in-game-mod path: link protocol, force feedback, simulator, serve loop."""
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import Clock, Ev, calibrated_cfg, g29_like

from evdev import ecodes as e, ff

from rvwheel import config, ffb, link, runtime, sim
from rvwheel.model import WheelModel, WheelState


def tel(**kw):
    kw.setdefault("seq", 1)
    kw.setdefault("driving", True)
    return link.Telemetry(**kw)


class LinkTests(unittest.TestCase):
    def test_state_encoding_is_plain_key_value(self):
        ws = WheelState(steer=-0.25, throttle=0.5, held=frozenset({"horn", "handbrake"}),
                        counts=(("engine", 0), ("lights", 3)), active=frozenset({"steer"}))
        kv = dict(line.split("=", 1) for line in link.encode_state(ws, seq=7, session=99).splitlines())
        self.assertEqual(kv["rvw"], "1")
        self.assertEqual(kv["end"], "1")
        self.assertEqual((kv["seq"], kv["session"]), ("7", "99"))
        self.assertEqual(kv["steer"], "-0.25000")
        self.assertEqual(kv["held"], "handbrake,horn")
        self.assertEqual(kv["active"], "steer")
        self.assertEqual(kv["n.lights"], "3")
        self.assertNotIn("n.engine", kv)   # zero counts are omitted

    def test_parse_telemetry(self):
        t = link.parse_telemetry("rvw=1\nseq=5\ndriving=1\nspeed=-12.5\nrpm=3000.2\ngear=2\nshifter=3\nengine=1\nextra=ignored\nend=1\n")
        self.assertEqual((t.seq, t.driving, t.speed, t.gear, t.shifter, t.engine), (5, True, -12.5, 2, 3, True))
        self.assertEqual(t.slip, 0.0)   # missing keys default
        self.assertIsNone(link.parse_telemetry("rvw=2\nseq=1\nend=1\n"))          # wrong protocol version
        self.assertIsNone(link.parse_telemetry("seq=1\nend=1\n"))         # no header
        self.assertIsNone(link.parse_telemetry("rvw=1\nseq=1\nspeed=12"))  # torn write: no terminator
        self.assertIsNone(link.parse_telemetry("rvw=1\nspeed=abc\nend=1\n"))      # garbage number
        self.assertIsNone(link.parse_telemetry(""))

    def test_modlink_write_read_and_staleness(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock()
            ml = link.ModLink(Path(d) / "x", clock)
            self.assertIsNone(ml.read_telemetry())                          # nothing there yet
            ml.write_state(WheelState())
            ml.write_state(WheelState(steer=0.5))
            text = ml.state_path.read_text()
            self.assertIn("seq=2", text)
            self.assertIn("steer=0.50000", text)
            self.assertEqual([p.name for p in ml.dir.iterdir()], ["state.txt"])  # no temp file left behind
            ml.telemetry_path.write_text("rvw=1\nseq=1\ndriving=1\nspeed=10\nend=1\n")
            self.assertEqual(ml.read_telemetry().speed, 10.0)
            clock.advance(0.4)
            self.assertIsNotNone(ml.read_telemetry())                       # same seq but still young
            clock.advance(0.4)
            self.assertIsNone(ml.read_telemetry())                          # seq not advancing: game stopped writing
            ml.telemetry_path.write_text("rvw=1\nseq=2\ndriving=1\nspeed=11\nend=1\n")
            self.assertEqual(ml.read_telemetry().speed, 11.0)               # fresh again
            ml.close()
            self.assertFalse(ml.state_path.exists())

    def test_session_differs_between_instances(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(link.ModLink(d).session, link.ModLink(d).session)


class FFModelTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cfg = copy.deepcopy(config.DEFAULT_CONFIG["ffb"])
        self.cfg["gain"], self.cfg["max_level"] = 1.0, 1.0   # tests that care override these
        self.m = ffb.FFModel(self.cfg, self.clock)

    def run_for(self, seconds, t, step=1 / 60):
        f = None
        n = int(seconds / step)
        for i in range(n):
            self.clock.advance(step)
            f = self.m.compute(t(i) if callable(t) else t)
        return f

    def test_no_telemetry_means_no_force(self):
        self.assertEqual(self.run_for(0.5, None), ffb.NO_FORCES)

    def test_not_driving_means_no_force(self):
        self.assertEqual(self.run_for(0.5, tel(driving=False, speed=80)), ffb.NO_FORCES)

    def test_spring_grows_with_speed(self):
        slow = self.run_for(1.0, tel(speed=0.0))
        fast = self.run_for(1.5, tel(speed=90.0))
        self.assertGreater(fast.spring, 0.9)
        self.assertLess(slow.spring, 0.2)
        self.assertGreater(slow.damper, fast.damper)    # more friction when parked

    def test_slip_lightens_the_wheel(self):
        grip = self.run_for(1.5, tel(speed=70.0, slip=0.0))
        slide = self.run_for(1.5, tel(speed=70.0, slip=1.0))
        self.assertLess(slide.spring, grip.spring * 0.6)

    def test_slide_pushes_toward_slide_direction_and_invert_flips(self):
        right = self.run_for(1.0, tel(speed=70.0, lat=15.0))
        self.assertGreater(right.constant, 0.2)
        left = self.run_for(1.0, tel(speed=70.0, lat=-15.0))
        self.assertLess(left.constant, -0.2)
        self.cfg["invert"] = True
        flipped = self.run_for(1.0, tel(speed=70.0, lat=15.0))
        self.assertLess(flipped.constant, -0.2)

    def test_forces_never_snap(self):
        self.run_for(0.5, tel(speed=70.0, lat=0.0))
        self.clock.advance(1 / 60)
        f = self.m.compute(tel(speed=70.0, lat=20.0, seq=2))
        self.assertLess(abs(f.constant), ffb.FFModel.RATE_CONSTANT / 60 + 1e-9)   # at most one slew step

    def test_gain_and_cap(self):
        self.cfg["gain"], self.cfg["max_level"] = 0.5, 0.3
        f = self.run_for(2.0, tel(speed=100.0, lat=20.0, rpm=7000, engine=True))
        for v in (f.spring, f.damper, abs(f.constant), f.vib):
            self.assertLessEqual(v, 0.3 + 1e-9)
        self.assertAlmostEqual(f.spring, 0.3)          # would be 0.5 before the cap

    def test_fades_out_when_telemetry_stops(self):
        self.run_for(1.5, tel(speed=90.0, lat=10.0))
        f = self.run_for(0.1, None)
        self.assertGreater(f.spring, 0.0)              # fading, not cut
        f = self.run_for(0.6, None)
        self.assertEqual(f, ffb.NO_FORCES)

    def test_impact_kicks_vibration_then_decays(self):
        n = {"seq": 0}
        def cruising(i):                               # telemetry arrives at 30 Hz: seq advances every 2nd step
            n["seq"] = i // 2 + 1
            return tel(speed=60.0, seq=n["seq"])
        self.run_for(1.0, cruising)
        last = n["seq"]
        self.clock.advance(1 / 60)
        self.m.compute(tel(speed=60.0, seq=last))
        self.clock.advance(1 / 60)
        f = self.m.compute(tel(speed=10.0, seq=last + 1))   # 50 km/h lost in one 33 ms sample
        self.clock.advance(1 / 60)
        f = self.m.compute(tel(speed=10.0, seq=last + 1))
        self.assertGreater(f.vib, 0.3)
        f = self.run_for(2.0, tel(speed=10.0, seq=last + 1))
        self.assertLess(f.vib, 0.05)

    def test_ordinary_braking_is_not_an_impact(self):
        seq = {"n": 0}
        def t(i):
            seq["n"] += 1
            return tel(speed=max(0.0, 60.0 - 30.0 * (i / 60.0)), seq=seq["n"], engine=False)   # 30 km/h/s
        f = self.run_for(1.5, t)
        self.assertLess(f.vib, 0.01)

    def test_first_sample_after_reentering_is_never_an_impact(self):
        self.run_for(1.0, tel(speed=80.0, seq=1))
        self.run_for(1.0, None)                        # on foot for a while
        self.clock.advance(1 / 30)
        f = self.m.compute(tel(speed=0.0, seq=50))     # climbs into a parked RV
        self.assertLess(f.vib, 0.01)

    def test_engine_vibration_scales_with_rpm_and_is_band_limited(self):
        idle = self.run_for(1.0, tel(engine=True, rpm=900.0))
        high = self.run_for(1.0, tel(engine=True, rpm=7000.0))
        self.assertGreater(high.vib, idle.vib)
        self.assertLess(high.vib, 0.15)
        self.assertTrue(10.0 <= idle.vib_hz <= 38.0 and 10.0 <= high.vib_hz <= 38.0)
        off = self.run_for(1.0, tel(engine=False, rpm=0.0))
        self.assertEqual(off.vib, 0.0)


class FakeFFDev:
    """Behaves like a real wheel's kernel interface: a limited number of effect slots (ENOSPC when full), and -
    like older python-evdev versions - it does NOT write the new effect's id back into the effect object."""

    def __init__(self, caps=(e.FF_CONSTANT, e.FF_SPRING, e.FF_DAMPER, e.FF_PERIODIC, e.FF_SINE, e.FF_GAIN, e.FF_AUTOCENTER),
                 max_effects=16, writeback=False):
        self.caps, self.next_id = list(caps), 0
        self.max_effects, self.writeback, self.slots = max_effects, writeback, set()
        self.uploads, self.writes, self.erased = [], [], []
        self.fail_with = None

    def capabilities(self, absinfo=False): return {e.EV_FF: self.caps}

    def upload_effect(self, eff, writeback_id=True):
        if self.fail_with is not None:
            raise OSError(self.fail_with, "boom")
        new_id = eff.id
        if eff.id < 0:                                   # creating a new effect needs a free slot
            if len(self.slots) >= self.max_effects:
                raise OSError(28, "No space left on device")
            new_id, self.next_id = self.next_id, self.next_id + 1
            self.slots.add(new_id)
        elif eff.id not in self.slots:
            raise OSError(22, "Invalid argument")
        if self.writeback:
            eff.id = new_id
        self.uploads.append({"id": new_id, "type": eff.type, "dir": eff.direction, "len": eff.ff_replay.length,
                             "level": eff.u.ff_constant_effect.level,
                             "cond": (eff.u.ff_condition_effect[0].right_coeff, eff.u.ff_condition_effect[0].left_coeff,
                                      eff.u.ff_condition_effect[0].right_saturation),
                             "period": eff.u.ff_periodic_effect.period, "mag": eff.u.ff_periodic_effect.magnitude})
        return new_id

    def write(self, t, c, v): self.writes.append((t, c, v))
    def erase_effect(self, i): self.erased.append(i); self.slots.discard(i)


class EvdevFFTests(unittest.TestCase):
    def make(self, ac=None, **kw):
        self.dev, self.clock, self.logs = FakeFFDev(**kw), Clock(), []
        cfg = copy.deepcopy(config.DEFAULT_CONFIG["ffb"])
        cfg["autocenter"] = ac
        return ffb.EvdevFF(self.dev, cfg, self.clock, log=self.logs.append)

    def test_init_sets_gain_and_optionally_autocenter(self):
        self.make()
        self.assertEqual(self.dev.writes, [(e.EV_FF, e.FF_GAIN, 0xFFFF)])
        self.make(ac=0.0)
        self.assertIn((e.EV_FF, e.FF_AUTOCENTER, 0), self.dev.writes)
        self.make(ac=0.5)
        self.assertIn((e.EV_FF, e.FF_AUTOCENTER, 0x7FFF), self.dev.writes)

    def test_idle_forces_upload_nothing(self):
        out = self.make()
        out.update(ffb.NO_FORCES)
        self.assertEqual(self.dev.uploads, [])

    def test_upload_start_update_and_throttle(self):
        out = self.make()
        out.update(ffb.FFForces(spring=0.5, constant=-0.25))
        types = {u["type"] for u in self.dev.uploads}
        self.assertEqual(types, {e.FF_SPRING, e.FF_CONSTANT})
        starts = [w for w in self.dev.writes if w[0] == e.EV_FF and w[1] not in (e.FF_GAIN,) and w[2] == 1]
        self.assertEqual(len(starts), 2)                                 # each effect started exactly once
        const = next(u for u in self.dev.uploads if u["type"] == e.FF_CONSTANT)
        self.assertEqual(const["level"], int(-0.25 * 32767))
        self.assertEqual(const["dir"], 0x4000)
        self.assertEqual(const["len"], 0)                                # plays until stopped
        spring = next(u for u in self.dev.uploads if u["type"] == e.FF_SPRING)
        self.assertEqual(spring["cond"], (16383, 16383, 32767))

        n = len(self.dev.uploads)
        out.update(ffb.FFForces(spring=0.505, constant=-0.25))           # below MIN_DELTA, and young
        self.assertEqual(len(self.dev.uploads), n)
        out.update(ffb.FFForces(spring=0.7, constant=-0.25))             # real change: re-upload same id, no new start
        self.assertEqual(len(self.dev.uploads), n + 1)
        self.assertEqual(self.dev.uploads[-1]["id"], spring["id"])
        self.assertEqual(len([w for w in self.dev.writes if w[2] == 1 and w[1] != e.FF_GAIN]), 2)
        out.update(ffb.FFForces(spring=0.7, constant=-0.25))             # unchanged: nothing sent, effects play on
        self.assertEqual(len(self.dev.uploads), n + 1)

    def test_never_leaks_effect_slots_even_if_the_library_does_not_write_the_id_back(self):
        """Regression: with an older python-evdev every update allocated a new effect -> ENOSPC on a real wheel."""
        for writeback in (False, True):
            out = self.make(writeback=writeback, max_effects=4)         # a wheel with just 4 slots
            for i in range(300):
                out.update(ffb.FFForces(spring=(i % 50) / 50, damper=(i % 30) / 30, constant=((i % 40) - 20) / 40,
                                        vib=(i % 20) / 100, vib_hz=20))
            self.assertLessEqual(len(self.dev.slots), 4, writeback)
            self.assertEqual(self.logs, [], "no effect may be refused")
            self.assertEqual(len({u["id"] for u in self.dev.uploads}), 4)

    def test_a_refused_effect_is_dropped_not_fatal(self):
        out = self.make(max_effects=1)                                   # room for one effect only
        out.update(ffb.FFForces(constant=0.4, spring=0.5, damper=0.2, vib=0.1))
        self.assertTrue(out.supported["constant"])
        self.assertFalse(out.supported["spring"] or out.supported["damper"] or out.supported["vib"])
        self.assertEqual(len(self.logs), 3)
        self.assertIn("refused the 'spring'", self.logs[0])
        n = len(self.dev.uploads)
        out.update(ffb.FFForces(constant=-0.4, spring=0.9))              # keeps working, never retries the refused ones
        self.assertEqual(len(self.dev.uploads), n + 1)
        self.assertEqual(len(self.logs), 3)

    def test_wheel_unplugged_is_not_swallowed(self):
        out = self.make()
        out.update(ffb.FFForces(constant=0.4))
        self.dev.fail_with = 19                                          # ENODEV
        with self.assertRaises(OSError):
            out.update(ffb.FFForces(constant=-0.4))

    def test_vibration_effect_encoding(self):
        out = self.make()
        out.update(ffb.FFForces(vib=0.1, vib_hz=25.0))
        v = next(u for u in self.dev.uploads if u["type"] == e.FF_PERIODIC)
        self.assertEqual(v["period"], 40)
        self.assertEqual(v["mag"], int(0.1 * 32767))

    def test_unsupported_effects_are_skipped(self):
        out = self.make(caps=(e.FF_CONSTANT,))
        out.update(ffb.FFForces(spring=0.5, damper=0.5, vib=0.5, constant=0.4))
        self.assertEqual({u["type"] for u in self.dev.uploads}, {e.FF_CONSTANT})
        self.assertEqual(out.supported, {"constant": True, "spring": False, "damper": False, "vib": False})

    def test_stop_stops_and_erases_everything(self):
        out = self.make()
        out.update(ffb.FFForces(spring=0.5, constant=0.4))
        ids = {w[1] for w in self.dev.writes if w[2] == 1 and w[1] != e.FF_GAIN}
        out.stop()
        self.assertEqual(set(self.dev.erased), ids)
        self.assertEqual({w[1] for w in self.dev.writes if w[2] == 0}, ids)
        out.stop()                                                       # idempotent
        self.assertEqual(len(self.dev.erased), len(ids))


class SimTests(unittest.TestCase):
    def test_demo_scenario_through_the_real_model(self):
        clock = Clock(50.0)
        cfg = sim.sim_config()
        w = sim.SimWheel(sim.demo, cfg, clock=clock)
        try:
            m = WheelModel(cfg, clock)
            m.resync(w)
            self.assertEqual(m.state().steer, 0.0)
            seen = {"steer": 0.0, "throttle": 0.0, "brake": 0.0, "clutch": 0.0}
            presses = set()
            for step in range(0, 2400):                  # 24 s at 10 ms
                clock.advance(0.01)
                m.process(w.read(), w)
                st = m.state()
                for k in seen:
                    seen[k] = max(seen[k], abs(getattr(st, k)))
                presses |= {a for a, n in st.counts if n}
            self.assertGreater(seen["steer"], 0.6)
            self.assertAlmostEqual(seen["throttle"], 0.55, places=2)
            self.assertAlmostEqual(seen["brake"], 1.0, places=2)
            self.assertEqual(seen["clutch"], 1.0)
            self.assertTrue({"engine", "gear_1", "gear_2", "lights", "park"} <= presses, presses)
        finally:
            w.close()

    def test_sim_config_is_complete_and_consistent(self):
        cfg = sim.sim_config()
        config.require_configured(cfg)
        codes = [c for cs in cfg["buttons"].values() for c in cs]
        self.assertEqual(len(codes), len(set(codes)))     # every action on its own button
        self.assertEqual(set(cfg["buttons"]), {a.name for a in config.ACTIONS})


class ServeModTests(unittest.TestCase):
    class FakeFF:
        supported = {"constant": True}
        def __init__(self, dev, cfg): self.updates, self.stopped = [], False
        def update(self, f): self.updates.append(f)
        def stop(self): self.stopped = True

    def run_serve(self, wheel, cfg, ml, make_ff, seconds=0.35):
        stop = threading.Event()
        errs = []
        def go():
            try:
                runtime.serve_mod(wheel, cfg, ml, stop, make_ff)
            except Exception as ex:   # noqa: BLE001
                errs.append(ex)
        t = threading.Thread(target=go)
        t.start()
        return stop, t, errs

    def test_feeds_state_and_plays_ff_from_telemetry(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = calibrated_cfg(); cfg["mod"]["rate_hz"] = 100
            made = []
            def make_ff(dev, c):
                made.append(self.FakeFF(dev, c)); return made[-1]
            w = g29_like(ff=object())                    # truthy ff device
            ml = link.ModLink(d)
            stop, t, errs = self.run_serve(w, cfg, ml, make_ff)
            w.push(Ev(e.EV_ABS, e.ABS_X, 49150), Ev(e.EV_KEY, e.BTN_THUMB2, 1), Ev(e.EV_SYN, e.SYN_REPORT, 0))
            time.sleep(0.15)
            ml.telemetry_path.write_text("rvw=1\nseq=1\ndriving=1\nspeed=80\nlat=10\nend=1\n")
            time.sleep(0.3)
            stop.set(); t.join(3)
            self.assertFalse(t.is_alive()); self.assertEqual(errs, [])
            text = ml.state_path.read_text()
            self.assertIn("steer=1.00000", text)
            self.assertIn("n.lights=1", text)
            self.assertTrue(w.grabbed)
            ff_out = made[0]
            self.assertTrue(ff_out.stopped)
            self.assertTrue(any(f.spring > 0 for f in ff_out.updates))         # telemetry reached the FFB model
            self.assertTrue(all(f == ffb.NO_FORCES for f in ff_out.updates[:3]))  # and it started from silence

    def test_no_ff_device_still_feeds_the_mod(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = calibrated_cfg(); cfg["mod"]["rate_hz"] = 100
            w = g29_like()                                # ff_device() -> None
            ml = link.ModLink(d)
            stop, t, errs = self.run_serve(w, cfg, ml, self.FakeFF)
            w.push(Ev(e.EV_ABS, e.ABS_Y, 0))
            time.sleep(0.25)
            stop.set(); t.join(3)
            self.assertEqual(errs, [])
            self.assertIn("throttle=1.00000", ml.state_path.read_text())

    def test_ffb_disabled_in_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = calibrated_cfg(); cfg["mod"]["rate_hz"] = 100; cfg["ffb"]["enabled"] = False
            made = []
            w = g29_like(ff=object())
            stop, t, errs = self.run_serve(w, cfg, link.ModLink(d), lambda dev, c: made.append(1))
            time.sleep(0.15); stop.set(); t.join(3)
            self.assertEqual((made, errs), ([], []))

    def test_rate_is_respected_and_does_not_burst(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = calibrated_cfg(); cfg["mod"]["rate_hz"] = 50
            w, ml = g29_like(), link.ModLink(d)
            stop, t, errs = self.run_serve(w, cfg, ml, None)
            time.sleep(1.0); stop.set(); t.join(3)
            self.assertEqual(errs, [])
            self.assertTrue(35 <= ml.seq <= 60, ml.seq)  # ~50 writes in a second, never hundreds

    def test_serve_mod_with_sim_wheel(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = sim.sim_config(); cfg["mod"]["rate_hz"] = 100
            w = sim.SimWheel(sim.demo, cfg, clock=lambda c=iter(x / 100 for x in range(10**6)): next(c) + 3.5)
            ml = link.ModLink(d)
            stop, t, errs = self.run_serve(w, cfg, ml, None)
            time.sleep(0.5); stop.set(); t.join(3)
            w.close()
            self.assertEqual(errs, [])
            self.assertGreater(ml.seq, 10)


if __name__ == "__main__":
    unittest.main()
