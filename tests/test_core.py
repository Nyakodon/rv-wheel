"""Offline tests for the wheel -> virtual pad path. No real device and no /dev/uinput is touched."""
import errno
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import Clock, Ev, FakeWheel, calibrated_cfg, g29_like, hshift_cfg

from evdev import AbsInfo, ecodes as e

from rvwheel import config, devices, mapping, pad, runtime, wizard
from rvwheel.model import WheelModel


class SteerMath(unittest.TestCase):
    def setUp(self):
        self.s = calibrated_cfg()["steer"]

    def test_centre_left_right(self):
        self.assertEqual(mapping.steer_norm(32767, self.s), 0.0)
        self.assertEqual(mapping.steer_norm(16384, self.s), -1.0)
        self.assertEqual(mapping.steer_norm(49150, self.s), 1.0)

    def test_clamps_beyond_lock(self):
        self.assertEqual(mapping.steer_norm(0, self.s), -1.0)
        self.assertEqual(mapping.steer_norm(65535, self.s), 1.0)

    def test_halfway(self):
        self.assertAlmostEqual(mapping.steer_norm(32767 + (49150 - 32767) // 2, self.s), 0.5, places=3)

    def test_inverted_axis_polarity(self):
        s = dict(self.s, left=49150, right=16384)  # wheel reports high when turned left
        self.assertEqual(mapping.steer_norm(49150, s), -1.0)
        self.assertEqual(mapping.steer_norm(16384, s), 1.0)

    def test_asymmetric_centre(self):
        s = dict(self.s, center=30000, left=10000, right=60000)
        self.assertEqual(mapping.steer_norm(10000, s), -1.0)
        self.assertEqual(mapping.steer_norm(60000, s), 1.0)
        self.assertAlmostEqual(mapping.steer_norm(20000, s), -0.5)
        self.assertAlmostEqual(mapping.steer_norm(45000, s), 0.5)

    def test_deadband_gain_expo(self):
        s = dict(self.s)
        self.assertEqual(mapping.steer_norm(32767 + 100, s), 0.0)  # 0.6% < 1.5% deadband
        s["gain"] = 2.0
        self.assertAlmostEqual(mapping.steer_norm(32767 + (49150 - 32767) // 4, s), 0.5, places=2)
        s["gain"], s["expo"] = 1.0, 2.0
        self.assertAlmostEqual(mapping.steer_norm(32767 + (49150 - 32767) // 2, s), 0.25, places=2)

    def test_game_deadzone_compensation(self):
        self.assertEqual(mapping.apply_game_deadzone(0.0, 0.2), 0.0)
        self.assertAlmostEqual(mapping.apply_game_deadzone(1.0, 0.2), 1.0)
        v = mapping.apply_game_deadzone(0.02, 0.2)
        self.assertGreater(v, 0.2)  # lands outside the game's deadzone
        self.assertAlmostEqual(mapping.apply_game_deadzone(-0.02, 0.2), -v)
        self.assertEqual(mapping.apply_game_deadzone(0.3, 0.0), 0.3)

    def test_degenerate_span_does_not_crash(self):
        s = dict(self.s, left=32767, right=40000)
        self.assertEqual(mapping.steer_norm(20000, s), 0.0)


class PedalMath(unittest.TestCase):
    def test_both_polarities(self):
        inv = {"rest": 255, "full": 0}
        self.assertEqual(mapping.pedal_norm(255, inv), 0.0)
        self.assertEqual(mapping.pedal_norm(0, inv), 1.0)
        self.assertAlmostEqual(mapping.pedal_norm(51, inv), 0.8)
        nor = {"rest": 0, "full": 1023}
        self.assertEqual(mapping.pedal_norm(0, nor), 0.0)
        self.assertEqual(mapping.pedal_norm(1023, nor), 1.0)
        self.assertEqual(mapping.pedal_norm(5000, nor), 1.0)
        self.assertEqual(mapping.pedal_norm(-5, nor), 0.0)

    def test_degenerate(self):
        self.assertEqual(mapping.pedal_norm(10, {"rest": 5, "full": 5}), 0.0)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.w = g29_like()
        self.clock = Clock()
        self.cfg = calibrated_cfg()
        self.m = WheelModel(self.cfg, self.clock)
        self.m.resync(self.w)

    def test_idle_state(self):
        st = self.m.state()
        self.assertEqual((st.steer, st.throttle, st.brake, st.clutch), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(st.held, frozenset())
        self.assertEqual(st.active, frozenset())  # nothing moved yet: the wheel owns nothing

    def test_axes_buttons_and_hat(self):
        self.m.process([Ev(e.EV_ABS, e.ABS_X, 49150), Ev(e.EV_ABS, e.ABS_Y, 0), Ev(e.EV_ABS, e.ABS_RZ, 128),
                        Ev(e.EV_KEY, e.BTN_TRIGGER, 1), Ev(e.EV_ABS, e.ABS_HAT0X, -1),
                        Ev(e.EV_SYN, e.SYN_REPORT, 0)], self.w)
        st = self.m.state()
        self.assertEqual(st.steer, 1.0)
        self.assertEqual(st.throttle, 1.0)
        self.assertAlmostEqual(st.brake, (255 - 128) / 255)
        self.assertEqual(st.held, frozenset({"handbrake"}))
        self.assertEqual(st.hat, (-1, 0))
        self.assertEqual(st.active, frozenset({"steer", "throttle", "brake"}))

    def test_press_counts_count_only_new_presses(self):
        self.m.process([Ev(e.EV_KEY, e.BTN_THUMB2, 1), Ev(e.EV_KEY, e.BTN_THUMB2, 2), Ev(e.EV_KEY, e.BTN_THUMB2, 0),
                        Ev(e.EV_KEY, e.BTN_THUMB2, 1), Ev(e.EV_KEY, e.BTN_BASE, 1)], self.w)  # BASE: unbound
        counts = dict(self.m.state().counts)
        self.assertEqual(counts["lights"], 2)
        self.assertEqual(counts["horn"], 0)

    def test_held_release(self):
        self.m.process([Ev(e.EV_KEY, e.BTN_THUMB, 1)], self.w)
        self.assertEqual(self.m.state().held, frozenset({"horn"}))
        self.m.process([Ev(e.EV_KEY, e.BTN_THUMB, 0)], self.w)
        self.assertEqual(self.m.state().held, frozenset())

    def test_clutch_pedal_threshold_and_hysteresis(self):
        def held_clutch(v):
            self.m.process([Ev(e.EV_ABS, e.ABS_Z, v)], self.w)
            return "clutch" in self.m.state().held
        self.assertFalse(held_clutch(255))
        self.assertFalse(held_clutch(140))   # 45% travel
        self.assertTrue(held_clutch(120))    # 53%
        self.assertTrue(held_clutch(135))    # 47%: inside hysteresis, stays on
        self.assertFalse(held_clutch(180))   # 29%: released

    def test_axis_ownership_expires_after_hold(self):
        self.m.process([Ev(e.EV_ABS, e.ABS_Y, 100)], self.w)
        self.assertIn("throttle", self.m.state().active)
        self.m.process([Ev(e.EV_ABS, e.ABS_Y, 255)], self.w)   # pedal released
        self.assertIn("throttle", self.m.state().active)        # just moved: still owned
        self.clock.advance(self.cfg["mod"]["active_hold_s"] + 0.1)
        self.assertNotIn("throttle", self.m.state().active)     # idle at rest: keyboard/pad may take over

    def test_axis_stays_owned_while_away_from_rest(self):
        self.m.process([Ev(e.EV_ABS, e.ABS_Y, 0)], self.w)
        self.clock.advance(60)
        self.assertIn("throttle", self.m.state().active)        # held down, not moving

    def test_sensor_noise_is_not_movement(self):
        self.m.state()
        self.clock.advance(5)
        self.m.process([Ev(e.EV_ABS, e.ABS_Y, 254)], self.w)    # 1 LSB of pedal jitter
        self.assertNotIn("throttle", self.m.state().active)

    def test_syn_dropped_resyncs_from_device(self):
        self.w.set(e.ABS_X, 49150)
        self.w.keys = {e.BTN_TRIGGER}
        self.m.process([Ev(e.EV_SYN, e.SYN_DROPPED, 0), Ev(e.EV_ABS, e.ABS_X, 0), Ev(e.EV_SYN, e.SYN_REPORT, 0)], self.w)
        st = self.m.state()
        self.assertEqual(st.steer, 1.0)
        self.assertEqual(st.held, frozenset({"handbrake"}))

    def test_missing_axis_in_raw_is_safe(self):
        self.m.raw.clear()
        st = self.m.state()
        self.assertEqual((st.steer, st.throttle), (0.0, 0.0))


class HShifterTests(unittest.TestCase):
    def setUp(self):
        self.w, self.clock = g29_like(), Clock()
        self.m = WheelModel(hshift_cfg(), self.clock)
        self.m.resync(self.w)
        self.press = lambda i, v=1: self.m.process([Ev(e.EV_KEY, e.BTN_BASE + i, v)], self.w)

    def test_no_shifter_bound_means_the_feature_is_off(self):
        m = WheelModel(calibrated_cfg(), self.clock)
        self.assertFalse(m.state().hshift)

    def test_held_gear_is_reported_and_release_means_neutral(self):
        st = self.m.state()
        self.assertEqual((st.hshift, st.gear), (True, ""))        # nothing held
        self.press(2)                                              # into 3rd: the button stays held
        self.assertEqual(self.m.state().gear, "gear_3")
        self.press(2, 0)                                           # out of gear -> back through neutral
        self.assertEqual(self.m.state().gear, "")
        self.press(5)
        self.assertEqual(self.m.state().gear, "gear_r")

    def test_two_buttons_held_picks_the_most_recent(self):
        self.press(0); self.press(3)                               # a glitchy shifter reports 1st and 4th
        self.assertEqual(self.m.state().gear, "gear_4")
        self.press(3, 0)
        self.assertEqual(self.m.state().gear, "gear_1")

    def test_link_carries_the_shifter_state(self):
        from rvwheel import link
        self.press(1)
        text = link.encode_state(self.m.state(), 1, 7)
        self.assertIn("hshift=1", text); self.assertIn("gear=gear_2", text)
        self.press(1, 0)
        self.assertIn("gear=\n", link.encode_state(self.m.state(), 2, 7))


class WheelAngleTests(unittest.TestCase):
    """The physical wheel angle that drives the in-game steering wheel 1:1."""

    def make(self, **steer):
        cfg = calibrated_cfg()
        cfg["steer"].update(steer)
        w, m = g29_like(), None
        m = WheelModel(cfg, Clock())
        m.resync(w)
        return m, w

    def at(self, m, w, raw):
        m.process([Ev(e.EV_ABS, e.ABS_X, raw)], w)
        return m.state()

    def test_default_is_900_degrees_over_the_whole_axis(self):
        m, w = self.make()
        self.assertEqual(m.wheel_degrees(), 900)
        st = self.at(m, w, 32767 + 16384)                       # a quarter of the 65535 axis span to the right
        self.assertAlmostEqual(st.wheel_deg, 225.0, delta=0.1)
        self.assertTrue(st.visual)
        self.assertAlmostEqual(self.at(m, w, 32767 - 16384).wheel_deg, -225.0, delta=0.1)
        self.assertEqual(self.at(m, w, 32767).wheel_deg, 0.0)

    def test_it_does_not_depend_on_where_you_set_full_lock_in_setup(self):
        a, wa = self.make(left=30000, right=35000)             # a tiny calibrated lock...
        b, wb = self.make(left=0, right=65535)                 # ...and the whole range
        self.assertAlmostEqual(self.at(a, wa, 40000).wheel_deg, self.at(b, wb, 40000).wheel_deg, places=6)

    def test_polarity_right_is_always_positive(self):
        m, w = self.make(left=49150, right=16384)             # this wheel reports LOW values when turned right
        self.assertGreater(self.at(m, w, 20000).wheel_deg, 0)   # 20000 is to the right here
        self.assertLess(self.at(m, w, 45000).wheel_deg, 0)

    def test_degrees_priority_config_then_driver_then_900(self):
        m, w = self.make()
        m.auto_degrees = 540
        self.assertEqual(m.wheel_degrees(), 540)                # detected from the driver
        self.assertAlmostEqual(self.at(m, w, 32767 + 16384).wheel_deg, 135.0, delta=0.1)
        m.cfg["steer"]["wheel_degrees"] = 1080
        self.assertEqual(m.wheel_degrees(), 1080)               # explicit config wins
        self.assertAlmostEqual(self.at(m, w, 32767 + 16384).wheel_deg, 270.0, delta=0.1)

    def test_visual_scale_and_switches(self):
        m, w = self.make(visual_scale=0.5)
        self.assertAlmostEqual(self.at(m, w, 32767 + 16384).wheel_deg, 112.5, delta=0.1)
        m, w = self.make(wheel_visual=False)
        self.assertFalse(self.at(m, w, 32767 + 16384).visual)
        m, w = self.make(camera_follow=False)
        self.assertFalse(m.state().camera)
        m, w = self.make()
        self.assertTrue(m.state().camera)

    def test_unknown_axis_range_disables_the_visual_instead_of_guessing(self):
        m, w = self.make()
        m.axis_range.clear()
        self.assertFalse(m.state().visual)
        self.assertEqual(m.state().wheel_deg, 0.0)

    def test_link_carries_it(self):
        from rvwheel import link
        m, w = self.make()
        text = link.encode_state(self.at(m, w, 32767 - 16384), 1, 1)
        self.assertIn("wheel_deg=-225.00", text); self.assertIn("visual=1", text); self.assertIn("camera=1", text)

    def test_old_configs_without_the_new_keys_keep_working(self):
        cfg = calibrated_cfg()
        for k in ("wheel_degrees", "wheel_visual", "visual_scale", "camera_follow"):
            cfg["steer"].pop(k)
        m = WheelModel(cfg, Clock()); m.resync(g29_like())
        self.assertEqual(m.wheel_degrees(), 900); self.assertTrue(m.state().visual)


class PadTests(unittest.TestCase):
    def test_to_pad_mapping(self):
        w, cfg = g29_like(), calibrated_cfg()
        m = WheelModel(cfg, Clock())
        m.resync(w)
        m.process([Ev(e.EV_ABS, e.ABS_X, 49150), Ev(e.EV_ABS, e.ABS_Y, 0), Ev(e.EV_ABS, e.ABS_RZ, 128),
                   Ev(e.EV_KEY, e.BTN_TRIGGER, 1), Ev(e.EV_KEY, e.BTN_THUMB, 1), Ev(e.EV_KEY, e.BTN_TOP, 1),
                   Ev(e.EV_ABS, e.ABS_HAT0X, -1), Ev(e.EV_ABS, e.ABS_HAT0Y, 1)], w)
        st = pad.to_pad(m.state(), cfg)
        self.assertEqual(st.x, 32767)
        self.assertEqual(st.rt, 255)
        self.assertEqual(st.lt, round((255 - 128) / 255 * 255))
        # gear_up has no gamepad button, so it must not show up on the virtual pad
        self.assertEqual(st.buttons, frozenset({e.BTN_A, e.BTN_THUMBR}))
        self.assertEqual((st.hat_x, st.hat_y), (-1, 1))
        cfg["hat"] = False
        self.assertEqual(pad.to_pad(m.state(), cfg).hat_x, 0)

    def test_game_deadzone_applied_on_pad_only(self):
        w, cfg = g29_like(), calibrated_cfg()
        cfg["steer"]["game_deadzone"] = 0.2
        m = WheelModel(cfg, Clock()); m.resync(w)
        m.process([Ev(e.EV_ABS, e.ABS_X, 32767 + 1000)], w)
        ws = m.state()
        self.assertLess(ws.steer, 0.1)                       # the mod path sees the true value
        self.assertGreater(pad.to_pad(ws, cfg).x / pad.STICK_MAX, 0.2)

    def test_pad_actions_are_exactly_the_documented_ones(self):
        self.assertEqual(set(config.PAD_ACTIONS), {"handbrake", "horn", "engine", "lights", "exit", "park",
                                                   "clutch", "menu", "back"})
        self.assertEqual(len(set(config.PAD_BUTTONS)), len(config.PAD_BUTTONS))  # no two actions share a button

    class FakeUI:
        def __init__(self): self.log = []
        def write(self, t, c, v): self.log.append((t, c, v))
        def syn(self): self.log.append("syn")
        def close(self): self.log.append("close")

    def test_only_changes_are_written_and_syn_batches(self):
        ui = self.FakeUI(); sink = pad.UInputSink(ui)
        sink.emit(pad.NEUTRAL)
        self.assertEqual(ui.log, [])
        sink.emit(pad.PadState(x=1000, rt=255, buttons=frozenset({e.BTN_A})))
        self.assertEqual(ui.log, [(e.EV_ABS, e.ABS_X, 1000), (e.EV_ABS, e.ABS_RZ, 255), (e.EV_KEY, e.BTN_A, 1), "syn"])
        ui.log.clear()
        sink.emit(pad.PadState(x=1000, rt=255, buttons=frozenset({e.BTN_A})))
        self.assertEqual(ui.log, [])
        sink.emit(pad.PadState(x=-5, rt=255))
        self.assertEqual(ui.log, [(e.EV_ABS, e.ABS_X, -5), (e.EV_KEY, e.BTN_A, 0), "syn"])

    def test_close_centres_then_closes(self):
        ui = self.FakeUI(); sink = pad.UInputSink(ui)
        sink.emit(pad.PadState(x=900, lt=10))
        ui.log.clear()
        sink.close()
        self.assertEqual(ui.log, [(e.EV_ABS, e.ABS_X, 0), (e.EV_ABS, e.ABS_Z, 0), "syn", "close"])

    def test_virtual_pad_capabilities_are_a_valid_xbox360_layout(self):
        caps = pad.uinput_capabilities()
        abs_map = dict(caps[e.EV_ABS])
        for code in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY):
            self.assertEqual((abs_map[code].min, abs_map[code].max), (-32768, 32767))
        for code in (e.ABS_Z, e.ABS_RZ):
            self.assertEqual((abs_map[code].min, abs_map[code].max), (0, 255))
        self.assertEqual((abs_map[e.ABS_HAT0X].min, abs_map[e.ABS_HAT0X].max), (-1, 1))
        for code in (e.BTN_A, e.BTN_B, e.BTN_X, e.BTN_Y, e.BTN_TL, e.BTN_TR, e.BTN_START, e.BTN_SELECT, e.BTN_THUMBR):
            self.assertIn(code, caps[e.EV_KEY])


class Classify(unittest.TestCase):
    A = AbsInfo(0, 0, 255, 0, 0, 0)

    def caps(self, keys=(), rel=False, extra_abs=()):
        c = {e.EV_ABS: [(e.ABS_X, self.A), *((a, self.A) for a in extra_abs)], e.EV_KEY: list(keys)}
        if rel:
            c[e.EV_REL] = [e.REL_X]
        return c

    def test_wheels(self):
        self.assertEqual(devices.classify("Logitech G29 Driving Force Racing Wheel", self.caps([e.BTN_TRIGGER])), "wheel")
        self.assertEqual(devices.classify("Thrustmaster T300RS", self.caps([e.BTN_TRIGGER])), "wheel")
        self.assertEqual(devices.classify("Unknown HID", self.caps([e.BTN_WHEEL])), "wheel")

    def test_gamepad_is_joystick_not_wheel(self):
        self.assertEqual(devices.classify("Xbox Wireless Controller", self.caps([e.BTN_A])), "joystick")

    def test_rejects_non_wheels(self):
        self.assertIsNone(devices.classify("Some Mouse", self.caps([e.BTN_LEFT], rel=True)))
        self.assertIsNone(devices.classify("libvirtualhid Mouse (Absolute)", self.caps([e.BTN_LEFT])))
        self.assertIsNone(devices.classify("Keyboard", self.caps([e.KEY_A])))
        self.assertIsNone(devices.classify(config.PAD_NAME, self.caps([e.BTN_A])))  # never grab our own pad
        self.assertIsNone(devices.classify("No X axis", {e.EV_ABS: [(e.ABS_Y, self.A)], e.EV_KEY: []}))

    def test_names_are_plain_strings(self):
        self.assertEqual(devices.key_name(e.BTN_A), "BTN_A")
        self.assertEqual(devices.key_name(e.BTN_TRIGGER), "BTN_TRIGGER")
        self.assertEqual(devices.key_name(e.KEY_A), "KEY_A")
        self.assertEqual(devices.axis_name(e.ABS_X), "ABS_X")
        self.assertEqual(devices.key_name(99999), "code 99999")


class Config(unittest.TestCase):
    def test_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "c.json"
            self.assertEqual(config.load_config(p), config.DEFAULT_CONFIG)
            cfg = calibrated_cfg()
            config.save_config(p, cfg)
            self.assertEqual(config.load_config(p), cfg)
            p.write_text('{"steer": {"gain": 1.5}, "bogus": 1, "ffb": {"gain": 0.3}, "buttons": {"nope": [1]}}')
            got = config.load_config(p)
            self.assertEqual(got["steer"]["gain"], 1.5)
            self.assertEqual(got["steer"]["expo"], 1.0)
            self.assertEqual(got["ffb"]["gain"], 0.3)
            self.assertEqual(got["ffb"]["max_level"], config.DEFAULT_CONFIG["ffb"]["max_level"])
            self.assertNotIn("bogus", got)
            p.write_text("{not json")
            with self.assertRaises(config.ConfigError):
                config.load_config(p)

    def test_old_v1_config_still_loads_with_new_actions_defaulted(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"buttons": {"handbrake": [288], "horn": [289]}}')
            got = config.load_config(p)
            self.assertEqual(got["buttons"]["handbrake"], [288])
            self.assertEqual(got["buttons"]["gear_up"], [])

    def test_require_configured(self):
        with self.assertRaises(config.ConfigError):
            config.require_configured(config.DEFAULT_CONFIG)
        config.require_configured(calibrated_cfg())

    def test_every_action_has_a_default_button_slot(self):
        self.assertEqual(set(config.DEFAULT_CONFIG["buttons"]), {a.name for a in config.ACTIONS})


class ScriptedIO:
    """Stands in for the Console: each pause() performs the next scripted user action on the fake wheel."""

    def __init__(self, pauses, buttons=()):
        self.pauses, self.buttons = iter(pauses), iter(buttons)
        self.prompts = []

    def pause(self, msg):
        self.prompts.append(msg)
        return next(self.pauses)() or ""

    def binding(self, devices, msg, ignore=frozenset()):
        self.prompts.append(msg)
        return next(self.buttons)

    def degrees(self, wheel):
        return getattr(self, "wheel_degrees", None)


class Wizard(unittest.TestCase):
    def test_full_setup_against_fake_g29(self):
        w = g29_like()
        S = lambda code, v: (lambda: w.set(code, v))
        nothing = lambda: None
        io = ScriptedIO(
            pauses=[
                S(e.ABS_X, 32767),                        # steering 1: centred, pedals up
                S(e.ABS_X, 20000),                        # steering 2: full left
                S(e.ABS_X, 45000),                        # steering 3: full right
                S(e.ABS_X, 32767),                        # throttle: released (user recentred the wheel)
                S(e.ABS_Y, 0),                            # throttle: floored
                S(e.ABS_Y, 255),                          # brake: released
                S(e.ABS_RZ, 0),                           # brake: floored
                S(e.ABS_RZ, 255),                         # clutch: released
                S(e.ABS_Z, 0),                            # clutch: floored
                lambda: "y",                              # yes, also bind gears etc.
            ],
            # basic order: handbrake, horn, engine, lights, exit, park, clutch, menu, back
            buttons=[e.BTN_TRIGGER, None, e.BTN_THUMB, e.BTN_TRIGGER,   # lights: duplicate of handbrake -> re-ask
                     e.BTN_THUMB2, None, None, None, None, None]        # exit, park.. skipped
            + [e.BTN_TOP] + [None] * 10,                                 # gear_up bound, rest skipped
        )
        cfg = wizard.setup_wizard(w, config.DEFAULT_CONFIG, io)
        st = cfg["steer"]
        self.assertEqual((st["axis"], st["center"], st["left"], st["right"]), (e.ABS_X, 32767, 20000, 45000))
        self.assertEqual(cfg["pedals"]["throttle"], {"axis": e.ABS_Y, "rest": 255, "full": 0})
        self.assertEqual(cfg["pedals"]["brake"], {"axis": e.ABS_RZ, "rest": 255, "full": 0})
        self.assertEqual(cfg["pedals"]["clutch"], {"axis": e.ABS_Z, "rest": 255, "full": 0})
        b = cfg["buttons"]
        self.assertEqual(b["handbrake"], [e.BTN_TRIGGER])
        self.assertEqual(b["horn"], [])
        self.assertEqual(b["engine"], [e.BTN_THUMB])
        self.assertEqual(b["lights"], [e.BTN_THUMB2])   # BTN_TRIGGER was rejected as already used
        self.assertEqual(b["gear_up"], [e.BTN_TOP])
        config.require_configured(cfg)
        m = WheelModel(cfg, Clock()); w.set(e.ABS_X, 45000); m.resync(w)
        self.assertEqual(m.state().steer, 1.0)

    def test_gear_prompts_are_optional(self):
        w = g29_like()
        S = lambda code, v: (lambda: w.set(code, v))
        io = ScriptedIO(
            pauses=[S(e.ABS_X, 32767), S(e.ABS_X, 20000), S(e.ABS_X, 45000), S(e.ABS_Y, 255), S(e.ABS_Y, 0),
                    S(e.ABS_RZ, 255), S(e.ABS_RZ, 0), lambda: "s", lambda: ""],   # clutch skipped, no gear binding
            buttons=[None] * 9,
        )
        cfg = wizard.setup_wizard(w, config.DEFAULT_CONFIG, io)
        self.assertIsNone(cfg["pedals"]["clutch"]["axis"])
        self.assertEqual(cfg["buttons"]["gear_1"], [])
        self.assertFalse(any("GEAR_1" in p for p in io.prompts))

    def test_wizard_retries_when_nothing_moves(self):
        w = g29_like()
        nothing = lambda: None
        io = ScriptedIO(
            pauses=[nothing, nothing,                                   # centre; "left" with nothing moved -> retry
                    nothing, lambda: w.set(e.ABS_X, 10000),             # centre again; left
                    lambda: w.set(e.ABS_X, 50000),                      # right
                    nothing, lambda: w.set(e.ABS_Y, 0),                 # throttle
                    lambda: w.set(e.ABS_Y, 255), lambda: w.set(e.ABS_RZ, 0),   # brake
                    lambda: "s", lambda: ""],                           # clutch skipped; no gears
            buttons=[None] * 9,
        )
        cfg = wizard.setup_wizard(w, config.DEFAULT_CONFIG, io)
        self.assertEqual((cfg["steer"]["left"], cfg["steer"]["right"]), (10000, 50000))


class RunLoop(unittest.TestCase):
    def test_serve_pad_end_to_end(self):
        w = g29_like()
        stop = threading.Event()
        emitted = []

        class Sink:
            def emit(s, st):
                emitted.append(st)
                if st.x == 32767 and st.rt == 255:
                    stop.set()

        w.push(Ev(e.EV_ABS, e.ABS_X, 49150), Ev(e.EV_ABS, e.ABS_Y, 0), Ev(e.EV_SYN, e.SYN_REPORT, 0))
        runtime.serve_pad(w, calibrated_cfg(), Sink(), stop)
        self.assertTrue(w.grabbed)
        self.assertEqual(emitted[0], pad.NEUTRAL)
        self.assertEqual(emitted[-2].x, 32767)
        self.assertEqual(emitted[-1], pad.NEUTRAL)   # released on the way out

    def test_serve_rejects_wrong_device(self):
        w = FakeWheel({e.ABS_Y: (0, 0, 255)})
        with self.assertRaises(config.ConfigError):
            runtime.serve_pad(w, calibrated_cfg(), None, threading.Event())

    def test_run_loop_waits_then_survives_unplug_and_replug(self):
        cfg = calibrated_cfg()
        stop = threading.Event()
        emitted = []

        class Sink:
            def emit(s, st):
                emitted.append(st)
                if st.x == 32767:   # the replugged wheel's first event -> done
                    stop.set()

        class Unplugged(FakeWheel):
            def read(self):
                raise OSError(errno.ENODEV, "gone")

        dying = Unplugged(g29_like().axes)
        dying.push(Ev(e.EV_SYN, e.SYN_REPORT, 0))   # makes the fd readable -> read() raises ENODEV
        back = g29_like()
        back.push(Ev(e.EV_ABS, e.ABS_X, 49150))
        offers, quiet_flags = iter([None, dying, back]), []

        def opener(device, quiet):
            quiet_flags.append(quiet)
            return next(offers, None)

        real_wait = stop.wait
        stop.wait = lambda timeout=None: real_wait(0.01)   # don't sleep 2 s in a test
        runtime.run_loop(cfg, lambda w: runtime.serve_pad(w, cfg, Sink(), stop), stop, opener)
        self.assertEqual(quiet_flags[:3], [False, True, False])  # "waiting" is only logged once
        self.assertIn(32767, [st.x for st in emitted])
        self.assertEqual(emitted[-1], pad.NEUTRAL)
        self.assertGreaterEqual(emitted.count(pad.NEUTRAL), 3)


if __name__ == "__main__":
    unittest.main()
