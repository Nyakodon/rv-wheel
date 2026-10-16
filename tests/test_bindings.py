"""Bindings on other devices (a stalk, a button box) and on axes (an analog handbrake)."""
import unittest

from helpers import Clock, Ev, FakeWheel, calibrated_cfg, g29_like

from evdev import ecodes as e

from rvwheel.bindings import Binding, BindingError, parse_all
from rvwheel.model import WheelModel

STALK = "MOZA Multi-function Stalk"
HANDBRAKE = "MOZA HBP Handbrake"


def stalk():
    return FakeWheel({}, name=STALK)


def handbrake(rest=0):
    return FakeWheel({e.ABS_Z: (rest, 0, 4095)}, name=HANDBRAKE)


def model(buttons, wheel=None):
    cfg = calibrated_cfg()
    cfg["buttons"].update(buttons)
    m = WheelModel(cfg, Clock())
    m.resync(wheel or g29_like())
    return m


class BindingParsing(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(Binding.parse(288), Binding(None, "key", 288))                         # the original format
        self.assertEqual(Binding.parse({"dev": STALK, "key": 289}), Binding(STALK, "key", 289))
        self.assertEqual(Binding.parse({"key": 5}), Binding(None, "key", 5))
        b = Binding.parse({"dev": HANDBRAKE, "axis": 2, "rest": 0, "full": 4095})
        self.assertEqual((b.dev, b.kind, b.code, b.at), (HANDBRAKE, "axis", 2, 0.5))

    def test_rejects_nonsense(self):
        for bad in (True, "x", None, {}, {"axis": 1}, {"axis": 1, "rest": 5, "full": 5}, [1],
                    {"axis": 1, "rest": 0, "full": 9, "at": 0.99}, {"axis": 1, "rest": 0, "full": 9, "at": 0.0}):
            with self.assertRaises(BindingError, msg=repr(bad)):
                Binding.parse(bad)
        with self.assertRaises(BindingError):
            parse_all([288, {"nonsense": 1}])

    def test_config_round_trip_keeps_old_format_for_plain_buttons(self):
        for raw in (288, {"dev": STALK, "key": 289},
                    {"dev": HANDBRAKE, "axis": 2, "rest": 0, "full": 4095, "at": 0.5},
                    {"axis": 3, "rest": 255, "full": 0, "at": 0.4}):
            self.assertEqual(Binding.parse(raw).to_config(), raw)
        self.assertIsInstance(Binding(None, "key", 7).to_config(), int)

    def test_axis_travel_any_polarity(self):
        up = Binding.parse({"axis": 1, "rest": 0, "full": 100})
        down = Binding.parse({"axis": 1, "rest": 255, "full": 0})
        self.assertEqual((up.travel(0), up.travel(50), up.travel(500), up.travel(-5)), (0.0, 0.5, 1.0, 0.0))
        self.assertEqual((down.travel(255), down.travel(0)), (0.0, 1.0))
        self.assertAlmostEqual(down.travel(127), 0.5, places=2)


class OtherDeviceButtons(unittest.TestCase):
    def test_a_stalk_button_acts_like_a_wheel_button(self):
        s = stalk()
        m = model({"lights": [{"dev": STALK, "key": e.BTN_TRIGGER}, e.BTN_THUMB2]})   # stalk OR a wheel button
        m.resync(s, STALK)
        self.assertEqual(m.extra_devices(), [STALK])
        m.process([Ev(e.EV_KEY, e.BTN_TRIGGER, 1)], s, STALK)
        st = m.state()
        self.assertIn("lights", st.held)
        self.assertEqual(dict(st.counts)["lights"], 1)
        m.process([Ev(e.EV_KEY, e.BTN_TRIGGER, 0)], s, STALK)
        self.assertNotIn("lights", m.state().held)
        m.process([Ev(e.EV_KEY, e.BTN_THUMB2, 1)], g29_like())                         # the wheel button still works
        self.assertEqual(dict(m.state().counts)["lights"], 2)

    def test_same_code_on_different_devices_is_not_confused(self):
        m = model({"horn": [{"dev": STALK, "key": 300}], "engine": [300]})              # code 300 on stalk vs on wheel
        s = stalk(); m.resync(s, STALK)
        m.process([Ev(e.EV_KEY, 300, 1)], s, STALK)
        self.assertEqual(m.state().held, frozenset({"horn"}))
        m.process([Ev(e.EV_KEY, 300, 0)], s, STALK)
        m.process([Ev(e.EV_KEY, 300, 1)], g29_like())                                    # main wheel (dev None)
        self.assertEqual(m.state().held, frozenset({"engine"}))

    def test_buttons_of_unbound_devices_are_ignored(self):
        m = model({"horn": [{"dev": STALK, "key": 300}]})
        m.process([Ev(e.EV_KEY, 300, 1)], stalk(), "Some Keyboard Thing")
        self.assertEqual(m.state().held, frozenset())

    def test_unplugging_releases_held_buttons_without_a_phantom_press(self):
        s = stalk()
        m = model({"horn": [{"dev": STALK, "key": 300}]}); m.resync(s, STALK)
        m.process([Ev(e.EV_KEY, 300, 1)], s, STALK)
        self.assertIn("horn", m.state().held)
        m.drop_device(STALK)
        self.assertNotIn("horn", m.state().held)
        self.assertEqual(dict(m.state().counts)["horn"], 1)

    def test_resync_picks_up_buttons_already_held(self):
        s = stalk(); s.keys = {300}
        m = model({"horn": [{"dev": STALK, "key": 300}]}); m.resync(s, STALK)
        self.assertIn("horn", m.state().held)
        self.assertEqual(dict(m.state().counts)["horn"], 0)             # holding on connect is not a press

    def test_syn_dropped_resyncs_only_that_device(self):
        s = stalk()
        m = model({"horn": [{"dev": STALK, "key": 300}]}); m.resync(s, STALK)
        s.keys = {300}
        m.process([Ev(e.EV_SYN, e.SYN_DROPPED, 0), Ev(e.EV_KEY, 300, 0), Ev(e.EV_SYN, e.SYN_REPORT, 0)], s, STALK)
        self.assertIn("horn", m.state().held)                           # state re-read from the device, junk ignored
        w = g29_like()
        m.process([Ev(e.EV_KEY, e.BTN_THUMB, 1)], w)                    # the main wheel was never in "dropped" mode
        self.assertTrue(m.down)


class AxisBindings(unittest.TestCase):
    BIND = {"dev": HANDBRAKE, "axis": e.ABS_Z, "rest": 0, "full": 4095}

    def test_analog_handbrake_lever(self):
        hb = handbrake()
        m = model({"handbrake": [self.BIND]}); m.resync(hb, HANDBRAKE)
        self.assertEqual(m.state().held, frozenset())
        m.process([Ev(e.EV_ABS, e.ABS_Z, 1000)], hb, HANDBRAKE)                   # 24%: not yet
        self.assertNotIn("handbrake", m.state().held)
        m.process([Ev(e.EV_ABS, e.ABS_Z, 2100)], hb, HANDBRAKE)                   # past 50%
        self.assertIn("handbrake", m.state().held)
        self.assertEqual(dict(m.state().counts)["handbrake"], 1)
        m.process([Ev(e.EV_ABS, e.ABS_Z, 4095)], hb, HANDBRAKE)                   # pulled all the way: still one press
        self.assertEqual(dict(m.state().counts)["handbrake"], 1)
        m.process([Ev(e.EV_ABS, e.ABS_Z, 0)], hb, HANDBRAKE)
        self.assertNotIn("handbrake", m.state().held)

    def test_hysteresis_stops_jitter_at_the_threshold_from_retriggering(self):
        hb = handbrake()
        m = model({"handbrake": [self.BIND]}); m.resync(hb, HANDBRAKE)
        for v in (2100, 2000, 2060, 1990, 2070, 2010):                              # wobble around 50%
            m.process([Ev(e.EV_ABS, e.ABS_Z, v)], hb, HANDBRAKE)
        self.assertEqual(dict(m.state().counts)["handbrake"], 1)
        self.assertIn("handbrake", m.state().held)
        m.process([Ev(e.EV_ABS, e.ABS_Z, 1500)], hb, HANDBRAKE)                   # below 50% - 10%: released
        self.assertNotIn("handbrake", m.state().held)
        m.process([Ev(e.EV_ABS, e.ABS_Z, 2100)], hb, HANDBRAKE)
        self.assertEqual(dict(m.state().counts)["handbrake"], 2)

    def test_inverted_axis_and_custom_threshold(self):
        hb = handbrake(rest=4095)
        m = model({"handbrake": [{"dev": HANDBRAKE, "axis": e.ABS_Z, "rest": 4095, "full": 0, "at": 0.25}]})
        m.resync(hb, HANDBRAKE)
        self.assertEqual(m.state().held, frozenset())                                # resting position = not held
        m.process([Ev(e.EV_ABS, e.ABS_Z, 3000)], hb, HANDBRAKE)                   # 27% of the travel
        self.assertIn("handbrake", m.state().held)

    def test_axis_already_pushed_at_connect_is_held_but_not_pressed(self):
        hb = handbrake(); hb.set(e.ABS_Z, 4000)
        m = model({"handbrake": [self.BIND]}); m.resync(hb, HANDBRAKE)
        self.assertIn("handbrake", m.state().held)
        self.assertEqual(dict(m.state().counts)["handbrake"], 0)

    def test_an_unused_axis_of_the_main_wheel_can_be_bound_too(self):
        m = model({"handbrake": [{"axis": e.ABS_Z, "rest": 255, "full": 0}]})       # the wheel's own clutch-like axis
        m.process([Ev(e.EV_ABS, e.ABS_Z, 60)], g29_like())
        self.assertIn("handbrake", m.state().held)

    def test_axis_and_button_bindings_combine(self):
        hb = handbrake(); m = model({"handbrake": [self.BIND, e.BTN_TRIGGER]}); m.resync(hb, HANDBRAKE)
        m.process([Ev(e.EV_KEY, e.BTN_TRIGGER, 1)], g29_like())
        self.assertIn("handbrake", m.state().held)
        m.process([Ev(e.EV_KEY, e.BTN_TRIGGER, 0)], g29_like())
        m.process([Ev(e.EV_ABS, e.ABS_Z, 3000)], hb, HANDBRAKE)
        self.assertIn("handbrake", m.state().held)

    def test_unplugging_a_lever_releases_it(self):
        hb = handbrake(); m = model({"handbrake": [self.BIND]}); m.resync(hb, HANDBRAKE)
        m.process([Ev(e.EV_ABS, e.ABS_Z, 4000)], hb, HANDBRAKE)
        m.drop_device(HANDBRAKE)
        self.assertNotIn("handbrake", m.state().held)
        hb.set(e.ABS_Z, 4000)
        m.resync(hb, HANDBRAKE)                                                     # replugged, lever still pulled
        self.assertIn("handbrake", m.state().held)

    def test_old_style_configs_are_untouched(self):
        m = model({})
        self.assertEqual(m.extra_devices(), [])
        m.process([Ev(e.EV_KEY, e.BTN_TRIGGER, 1)], g29_like())
        self.assertEqual(m.state().held, frozenset({"handbrake"}))


class WizardWithOtherDevices(unittest.TestCase):
    def run_wizard(self, bindings, degrees=None, others=()):
        from test_core import ScriptedIO
        from rvwheel import config, wizard
        w = g29_like()
        S = lambda code, v: (lambda: w.set(code, v))
        io = ScriptedIO(
            pauses=[S(e.ABS_X, 32767), S(e.ABS_X, 20000), S(e.ABS_X, 45000), S(e.ABS_Y, 255), S(e.ABS_Y, 0),
                    S(e.ABS_RZ, 255), S(e.ABS_RZ, 0), lambda: "s", lambda: ""],
            buttons=bindings + [None] * 30)
        io.wheel_degrees = degrees
        return wizard.setup_wizard(w, config.DEFAULT_CONFIG, io, others), io

    def test_bindings_from_any_device_are_stored_and_work(self):
        # order: handbrake, horn, engine, lights, exit ...
        axis = Binding(HANDBRAKE, "axis", e.ABS_Z, 0, 4095)
        cfg, io = self.run_wizard([axis, Binding(STALK, "key", 300), e.BTN_TRIGGER, Binding(STALK, "key", 301)],
                                  others=[stalk(), handbrake()])
        b = cfg["buttons"]
        self.assertEqual(b["handbrake"], [{"dev": HANDBRAKE, "axis": e.ABS_Z, "rest": 0, "full": 4095, "at": 0.5}])
        self.assertEqual(b["horn"], [{"dev": STALK, "key": 300}])
        self.assertEqual(b["engine"], [e.BTN_TRIGGER])                                  # main wheel button: plain int
        m = WheelModel(cfg, Clock()); m.resync(g29_like())
        self.assertEqual(m.extra_devices(), sorted([STALK, HANDBRAKE]))
        s, hb = stalk(), handbrake(); m.resync(s, STALK); m.resync(hb, HANDBRAKE)
        m.process([Ev(e.EV_KEY, 301, 1)], s, STALK)
        m.process([Ev(e.EV_ABS, e.ABS_Z, 3000)], hb, HANDBRAKE)
        self.assertEqual(m.state().held, frozenset({"lights", "handbrake"}))

    def test_same_physical_input_cannot_be_bound_twice(self):
        # handbrake <- stalk 300 (ok); horn <- stalk 300 (already taken, asked again) <- stalk 302 (ok)
        cfg, io = self.run_wizard([Binding(STALK, "key", 300), Binding(STALK, "key", 300), Binding(STALK, "key", 302)])
        self.assertEqual(cfg["buttons"]["handbrake"], [{"dev": STALK, "key": 300}])
        self.assertEqual(cfg["buttons"]["horn"], [{"dev": STALK, "key": 302}])

    def test_the_same_code_on_a_different_device_is_a_different_input(self):
        cfg, _ = self.run_wizard([Binding(STALK, "key", 300), Binding(None, "key", 300)])
        self.assertEqual(cfg["buttons"]["handbrake"], [{"dev": STALK, "key": 300}])
        self.assertEqual(cfg["buttons"]["horn"], [300])

    def test_wheel_rotation_is_stored(self):
        cfg, _ = self.run_wizard([], degrees=540)
        self.assertEqual(cfg["steer"]["wheel_degrees"], 540)
        cfg, _ = self.run_wizard([], degrees=None)
        self.assertIsNone(cfg["steer"]["wheel_degrees"])                     # stays auto-detected


class CaptureLogic(unittest.TestCase):
    """Console.binding without a terminal: the waiting is replaced, the decision logic is the real one."""

    def setUp(self):
        from rvwheel import wizard
        self.wizard = wizard
        wizard.AXIS_SETTLE_S = 0.05

        class Con(wizard.Console):
            enter = False
            pending = ()
            def _wait(self, devices, timeout):
                for dev, events in self.pending:        # delivered now: after the drain of stale events
                    dev.queue.extend(events)
                self.pending = ()
                if self.enter:
                    return True, []
                ready = [d for d in devices if d.queue]
                if not ready and timeout is None:
                    raise AssertionError("would wait forever for input")
                if not ready:
                    import time; time.sleep(min(timeout or 0, 0.01))
                return False, ready
        self.con = Con()
        self.main, self.s, self.hb = g29_like(), stalk(), handbrake()
        self.devices = [self.main, self.s, self.hb]

    def tearDown(self):
        self.wizard.AXIS_SETTLE_S = 0.6

    def test_button_on_another_device(self):
        self.con.pending = [(self.s, [Ev(e.EV_KEY, 300, 1)])]
        b = self.con.binding(self.devices, "x")
        self.assertEqual(b, Binding(STALK, "key", 300))

    def test_button_on_the_main_wheel_has_no_device(self):
        self.con.pending = [(self.main, [Ev(e.EV_KEY, e.BTN_TRIGGER, 1)])]
        self.assertEqual(self.con.binding(self.devices, "x"), Binding(None, "key", e.BTN_TRIGGER))

    def test_releases_and_keyboard_keys_do_not_count(self):
        self.con.pending = [(self.s, [Ev(e.EV_KEY, 300, 0), Ev(e.EV_KEY, e.KEY_A, 1), Ev(e.EV_KEY, 301, 2), Ev(e.EV_KEY, 302, 1)])]
        self.assertEqual(self.con.binding(self.devices, "x"), Binding(STALK, "key", 302))

    def test_axis_must_move_far_and_the_farthest_point_is_remembered(self):
        self.con.pending = [(self.hb, [Ev(e.EV_ABS, e.ABS_Z, 300), Ev(e.EV_ABS, e.ABS_Z, 900)])]   # wobble: < 40% of 4095
        self.assertIsNone(self._try_with_enter_when_idle())
        self.con.pending = [(self.hb, [Ev(e.EV_ABS, e.ABS_Z, 1000), Ev(e.EV_ABS, e.ABS_Z, 2000)])]  # 49%: triggers
        b = self.con.binding(self.devices, "x")
        self.assertEqual((b.dev, b.kind, b.code, b.rest), (HANDBRAKE, "axis", e.ABS_Z, 0))
        self.assertEqual(b.full, 2000)                                                       # what we saw at the trigger
        self.hb.set(e.ABS_Z, 0)

    def test_axis_keeps_the_farthest_value_seen_while_settling(self):
        self.con.pending = [(self.hb, [Ev(e.EV_ABS, e.ABS_Z, 2000)])]
        orig = self.con._wait
        def feeding(devices, timeout):
            if timeout is not None and not self.hb.queue:
                self.hb.queue = [Ev(e.EV_ABS, e.ABS_Z, 4000), Ev(e.EV_ABS, e.ABS_Z, 3900)]
            return orig(devices, timeout)
        self.con._wait = feeding
        self.assertEqual(self.con.binding(self.devices, "x").full, 4000)

    def test_ignored_axes_and_hats_never_trigger(self):
        self.con.pending = [(self.main, [Ev(e.EV_ABS, e.ABS_X, 0), Ev(e.EV_ABS, e.ABS_Y, 0), Ev(e.EV_ABS, e.ABS_HAT0X, 1)])]   # steering, pedal, hat
        b = self._try_with_enter_when_idle(ignore={(None, e.ABS_X), (None, e.ABS_Y)})
        self.assertIsNone(b)

    def test_enter_skips(self):
        self.con.enter = True
        self.assertIsNone(self.con.binding(self.devices, "x"))

    def _try_with_enter_when_idle(self, ignore=frozenset()):
        """Run binding(); once all queued events are consumed and nothing triggered, press Enter."""
        orig = self.con._wait
        def waiter(devices, timeout):
            if timeout is None and not any(d.queue for d in devices):
                return True, []
            return orig(devices, timeout)
        self.con._wait = waiter
        try:
            return self.con.binding(self.devices, "x", ignore)
        finally:
            self.con._wait = orig


class DiscoveryTests(unittest.TestCase):
    A = (e.ABS_X, None)

    def caps(self, keys=(), abs_codes=(), rel=False):
        c = {e.EV_KEY: list(keys), e.EV_ABS: [(a, None) for a in abs_codes]}
        if rel:
            c[e.EV_REL] = [e.REL_X]
        return c

    def test_what_counts_as_a_controller(self):
        from rvwheel import devices as d
        self.assertTrue(d.is_controller("MOZA Multi-function Stalk", self.caps(keys=range(e.BTN_TRIGGER, e.BTN_TRIGGER + 8))))
        self.assertTrue(d.is_controller("button box", self.caps(keys=[e.BTN_TRIGGER_HAPPY1, e.BTN_TRIGGER_HAPPY2])))
        self.assertTrue(d.is_controller("pedals", self.caps(abs_codes=[e.ABS_X, e.ABS_Y, e.ABS_Z])))
        self.assertTrue(d.is_controller("Xbox pad", self.caps(keys=[e.BTN_A, e.BTN_B], abs_codes=[e.ABS_X])))
        self.assertTrue(d.is_controller("Weird Racing Wheel", self.caps(keys=[e.BTN_LEFT, e.BTN_WHEEL])))
        self.assertFalse(d.is_controller("Keyboard", self.caps(keys=[e.KEY_A, e.BTN_TRIGGER])))
        self.assertFalse(d.is_controller("Mouse", self.caps(keys=[e.BTN_LEFT], rel=True)))
        self.assertFalse(d.is_controller("Tablet", self.caps(keys=[e.BTN_TOUCH], abs_codes=[e.ABS_X, e.ABS_Y])))
        self.assertFalse(d.is_controller("Power Button", self.caps(keys=[e.KEY_POWER])))
        self.assertFalse(d.is_controller("HDMI jack", {e.EV_SW: [1]}))
        self.assertFalse(d.is_controller("only a d-pad hat", self.caps(abs_codes=[e.ABS_HAT0X, e.ABS_HAT0Y])))
        from rvwheel.config import PAD_NAME
        self.assertFalse(d.is_controller(PAD_NAME, self.caps(keys=[e.BTN_A], abs_codes=[e.ABS_X])))

    def test_open_matching(self):
        from rvwheel import devices as d
        scan = lambda: [("/dev/input/event7", "MOZA R9 Base"), ("/dev/input/event9", "MOZA Multi-function Stalk"),
                        ("/dev/input/event11", "MOZA Multi-function Stalk Copy")]
        opener = lambda path: path
        om = lambda want, ex=(): d.open_matching(want, ex, scan, opener)
        self.assertEqual(om("MOZA Multi-function Stalk"), "/dev/input/event9")          # exact beats substring
        self.assertEqual(om("multi-function"), "/dev/input/event9")                      # substring, any case
        self.assertEqual(om("/dev/input/event11"), "/dev/input/event11")                  # explicit path
        self.assertIsNone(om("nonexistent"))
        self.assertEqual(om("MOZA", ex=("/dev/input/event7",)), "/dev/input/event9")     # never the main wheel
        self.assertIsNone(d.open_matching("x", (), scan, lambda p: (_ for _ in ()).throw(OSError(13, "denied"))))


class ExtraDevicePool(unittest.TestCase):
    def setUp(self):
        from rvwheel import runtime
        self.runtime, self.clock = runtime, Clock()
        self.stalk, self.logs = stalk(), []
        self.present = {STALK: self.stalk}
        self.opened = []
        self.model = model({"horn": [{"dev": STALK, "key": 300}]})
        self.main = g29_like()
        self.cfg = self.model.cfg
        self._log = runtime.log
        runtime.log = self.logs.append
        self.pool = runtime.ExtraDevices(self.model, self.cfg, self.main, self.opener, self.clock)

    def tearDown(self):
        self.runtime.log = self._log

    def opener(self, want, exclude):
        self.opened.append((want, exclude))
        return self.present.get(want)

    def test_opens_reads_grabs_and_logs(self):
        self.pool.refresh()
        self.assertIn(STALK, self.pool.devs)
        self.assertTrue(self.stalk.grabbed)
        self.assertEqual(self.opened[0][1], (self.main.path,))               # the main wheel is excluded from the search
        self.stalk.push(Ev(e.EV_KEY, 300, 1))
        self.assertTrue(self.pool.pump(self.stalk.fd))
        self.assertIn("horn", self.model.state().held)
        self.assertTrue(any("also reading" in m for m in self.logs))

    def test_missing_device_is_retried_not_spammed(self):
        self.present.clear()
        self.pool.refresh(); self.pool.refresh()
        self.assertEqual(len(self.opened), 1)                                # not retried before RETRY_S
        self.assertEqual(sum("waiting for" in m for m in self.logs), 1)
        self.clock.advance(2.1); self.pool.refresh(); self.clock.advance(2.1); self.pool.refresh()
        self.assertEqual(len(self.opened), 3)
        self.assertEqual(sum("waiting for" in m for m in self.logs), 1)      # warned once only
        self.present[STALK] = self.stalk                                     # plugged in later
        self.clock.advance(2.1); self.pool.refresh()
        self.assertIn(STALK, self.pool.devs)

    def test_unplug_releases_and_replug_recovers(self):
        import errno as _errno
        self.pool.refresh()
        self.stalk.push(Ev(e.EV_KEY, 300, 1)); self.pool.pump(self.stalk.fd)
        self.assertIn("horn", self.model.state().held)
        def boom():
            raise OSError(_errno.ENODEV, "gone")
        self.stalk.read = boom
        self.assertTrue(self.pool.pump(self.stalk.fd))
        self.assertNotIn(STALK, self.pool.devs)
        self.assertNotIn("horn", self.model.state().held)
        self.assertTrue(any("unplugged" in m for m in self.logs))
        self.present[STALK] = stalk()                                        # a new device node appears
        self.clock.advance(2.1); self.pool.refresh()
        self.assertIn(STALK, self.pool.devs)

    def test_other_read_errors_are_not_swallowed(self):
        self.pool.refresh()
        def boom():
            raise OSError(13, "permission denied")
        self.stalk.read = boom
        with self.assertRaises(OSError):
            self.pool.pump(self.stalk.fd)

    def test_close_releases_everything(self):
        self.pool.refresh(); self.pool.close()
        self.assertEqual(self.pool.devs, {})
        self.assertEqual(self.pool.fds(), [])

    def test_no_bindings_on_other_devices_means_no_pool_at_all(self):
        m = model({})
        pool = self.runtime.ExtraDevices(m, m.cfg, self.main, lambda *a: self.fail("must not open anything"), self.clock)
        pool.refresh()
        self.assertEqual(pool.fds(), [])


class LoopsWithExtraDevices(unittest.TestCase):
    def run_loop(self, target, *args, seconds=0.4, **kw):
        import threading
        import time as _t
        stop, errs = threading.Event(), []
        def go():
            try:
                target(*args, stop, **kw)
            except Exception as ex:   # noqa: BLE001
                errs.append(ex)
        t = threading.Thread(target=go); t.start()
        return stop, t, errs

    def test_pad_mode_stalk_button_and_analog_handbrake(self):
        import time
        from rvwheel import pad, runtime
        main, s, hb = g29_like(), stalk(), handbrake()
        cfg = calibrated_cfg()
        cfg["buttons"]["horn"] = [{"dev": STALK, "key": 300}]
        cfg["buttons"]["handbrake"] = [{"dev": HANDBRAKE, "axis": e.ABS_Z, "rest": 0, "full": 4095}]
        seen = []
        class Sink:
            def emit(self, st): seen.append(st)
        devs = {STALK: s, HANDBRAKE: hb}
        stop, t, errs = self.run_loop(lambda *a, **k: runtime.serve_pad(main, cfg, Sink(), a[-1], extra_opener=lambda w, ex: devs.get(w)))
        time.sleep(0.15)
        s.push(Ev(e.EV_KEY, 300, 1)); time.sleep(0.1)
        hb.push(Ev(e.EV_ABS, e.ABS_Z, 3500)); time.sleep(0.15)
        stop.set(); t.join(3)
        self.assertEqual(errs, [])
        self.assertTrue(any(e.BTN_THUMBR in st.buttons for st in seen), "horn (R3) from the stalk")
        self.assertTrue(any(e.BTN_A in st.buttons for st in seen), "handbrake (A) from the analog lever")
        self.assertEqual(seen[-1], pad.NEUTRAL)

    def test_mod_mode_sends_them_to_the_game(self):
        import tempfile
        import time
        from rvwheel import link, runtime
        main, s, hb = g29_like(), stalk(), handbrake()
        cfg = calibrated_cfg(); cfg["mod"]["rate_hz"] = 100; cfg["ffb"]["enabled"] = False
        cfg["buttons"]["lights"] = [{"dev": STALK, "key": 301}]
        cfg["buttons"]["handbrake"] = [{"dev": HANDBRAKE, "axis": e.ABS_Z, "rest": 0, "full": 4095}]
        devs = {STALK: s, HANDBRAKE: hb}
        with tempfile.TemporaryDirectory() as d:
            ml = link.ModLink(d)
            stop, t, errs = self.run_loop(lambda *a, **k: runtime.serve_mod(main, cfg, ml, a[-1], None, extra_opener=lambda w, ex: devs.get(w)))
            time.sleep(0.15)
            s.push(Ev(e.EV_KEY, 301, 1)); hb.push(Ev(e.EV_ABS, e.ABS_Z, 4000)); time.sleep(0.3)
            text = ml.state_path.read_text()
            stop.set(); t.join(3)
        self.assertEqual(errs, [])
        self.assertIn("n.lights=1", text)
        self.assertIn("held=handbrake", text)


if __name__ == "__main__":
    unittest.main()
