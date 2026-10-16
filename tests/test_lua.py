"""Runs the plain-Lua tests of the in-game mod (needs a `lua5.4`/`lua` binary) and cross-checks the wire format."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from rvwheel import link

ROOT = Path(__file__).resolve().parent.parent
LUA = shutil.which("lua5.4") or shutil.which("lua")


@unittest.skipUnless(LUA, "no lua interpreter installed")
class LuaModTests(unittest.TestCase):
    def test_lua_logic_and_wire_format(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([LUA, str(ROOT / "tests/lua/test_logic.lua"), d, str(ROOT / "mod/RVWheel/Scripts")],
                               capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 failed", r.stdout)
        # what the Lua mod writes must be readable by the Python daemon
        text = r.stdout.split("TELEMETRY_BEGIN\n")[1].split("TELEMETRY_END")[0]
        t = link.parse_telemetry(text)
        self.assertIsNotNone(t)
        self.assertTrue(t.driving and t.engine and t.wheel_on)
        self.assertAlmostEqual(t.speed, 100.0, delta=0.01)
        self.assertEqual((t.gear, t.shifter, t.rpm), (2, 3, 3000.0))

    def test_every_press_action_the_daemon_can_send_has_a_lua_handler(self):
        from rvwheel.config import ACTIONS
        src = (ROOT / "mod/RVWheel/Scripts/rvw_logic.lua").read_text()
        for a in ACTIONS:
            if a.pad_code is not None and a.name in ("menu", "back"):
                continue                      # virtual-pad only
            self.assertTrue(f"PRESS.{a.name}" in src or f'PRESS["{a.name}"]' in src or f"HOLD" in src and f"{a.name} = function" in src
                            or (a.name.startswith("gear_") and a.name[5:].isdigit()),
                            f"no Lua handler for action {a.name}")


if __name__ == "__main__":
    unittest.main()
