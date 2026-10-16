import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers  # noqa: F401  (sets sys.path)

from rvwheel import install


def make_zip(extra=None, with_dll=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if with_dll:
            z.writestr("dwmapi.dll", b"proxy")
            z.writestr("ue4ss/UE4SS.dll", b"core")
        z.writestr("ue4ss/UE4SS-settings.ini", b"default-settings")
        z.writestr("ue4ss/Mods/mods.txt", b"Keybinds : 1\n")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.game = Path(self.tmp.name) / "Ride"
        (self.game / "Ride" / "Binaries" / "Win64").mkdir(parents=True)
        self.w = self.game / "Ride" / "Binaries" / "Win64"
        self.logs = []
        self.log = self.logs.append

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, **kw):
        kw.setdefault("running", lambda: False)
        kw.setdefault("download", lambda: make_zip())
        install.install(self.game, log=self.log, **kw)

    def test_fresh_install_puts_ue4ss_and_mod_in_place(self):
        self.run_install()
        self.assertEqual((self.w / "dwmapi.dll").read_bytes(), b"proxy")
        self.assertTrue((self.w / "ue4ss" / "UE4SS.dll").exists())
        mod = self.w / "ue4ss" / "Mods" / "RVWheel"
        for f in ("enabled.txt", "Scripts/main.lua", "Scripts/rvw_logic.lua", "Scripts/rvw_config.lua"):
            self.assertTrue((mod / f).exists(), f)
        self.assertIn('WINEDLLOVERRIDES="dwmapi=n,b" %command%', "\n".join(self.logs))

    def test_unknown_mod_is_rejected(self):
        with self.assertRaises(install.InstallError):
            self.run_install(mods="NoSuchMod")

    def test_reinstall_keeps_the_users_edited_mod_settings(self):
        self.run_install()
        cfg = self.w / "ue4ss" / "Mods" / "RVWheel" / "Scripts" / "rvw_config.lua"
        cfg.write_text("return { verbose = true }")
        (cfg.parent / "rvw_logic.lua").write_text("stale code")
        self.run_install()
        self.assertEqual(cfg.read_text(), "return { verbose = true }")                      # settings survive
        self.assertNotEqual((cfg.parent / "rvw_logic.lua").read_text(), "stale code")       # code is updated

    def test_coexists_with_the_independent_rvrestore_mod(self):
        sibling = self.w / "ue4ss" / "Mods" / "RVRestore"
        (sibling / "Scripts").mkdir(parents=True)
        (sibling / "Scripts" / "rvr_logic.lua").write_text("sibling code")
        self.run_install()                                                    # installing/updating RVWheel ...
        self.assertEqual((sibling / "Scripts" / "rvr_logic.lua").read_text(), "sibling code")   # ... leaves it alone
        install.uninstall(self.game, everything=True, running=lambda: False, log=self.log)
        self.assertFalse((self.w / "ue4ss" / "Mods" / "RVWheel").exists())
        self.assertTrue((self.w / "dwmapi.dll").exists())                     # UE4SS kept: RVRestore still needs it
        self.assertTrue((sibling / "Scripts" / "rvr_logic.lua").exists())
        self.assertIn("kept UE4SS", "\n".join(self.logs))

    def test_refuses_while_game_is_running(self):
        with self.assertRaises(install.InstallError):
            self.run_install(running=lambda: True)
        self.assertEqual(list(self.w.iterdir()), [])          # nothing was touched
        self.run_install(running=lambda: True, force=True)    # explicit override only

    def test_existing_ue4ss_is_left_alone_and_settings_survive_reinstall(self):
        self.run_install()
        (self.w / "ue4ss" / "UE4SS-settings.ini").write_bytes(b"my-settings")
        (self.w / "ue4ss" / "Mods" / "RVWheel" / "Scripts" / "rvw_logic.lua").write_text("stale")
        self.run_install(download=lambda: (_ for _ in ()).throw(AssertionError("must not download again")))
        self.assertEqual((self.w / "ue4ss" / "UE4SS-settings.ini").read_bytes(), b"my-settings")
        self.assertNotEqual((self.w / "ue4ss" / "Mods" / "RVWheel" / "Scripts" / "rvw_logic.lua").read_text(), "stale")

    def test_settings_survive_ue4ss_reextract(self):
        (self.w / "ue4ss").mkdir()
        (self.w / "ue4ss" / "UE4SS-settings.ini").write_bytes(b"my-settings")
        self.run_install()                                    # dwmapi missing -> extracts, but keeps the ini
        self.assertEqual((self.w / "ue4ss" / "UE4SS-settings.ini").read_bytes(), b"my-settings")

    def test_rejects_wrong_or_malicious_zip(self):
        with self.assertRaises(install.InstallError):
            self.run_install(download=lambda: make_zip(with_dll=False))
        with self.assertRaises(install.InstallError):
            self.run_install(download=lambda: make_zip({"../escape.txt": b"x"}))
        self.assertFalse((Path(self.tmp.name) / "escape.txt").exists())

    def test_local_zip_option(self):
        z = Path(self.tmp.name) / "u.zip"
        z.write_bytes(make_zip())
        self.run_install(ue4ss_zip=z, download=lambda: (_ for _ in ()).throw(AssertionError("no download")))
        self.assertTrue((self.w / "dwmapi.dll").exists())

    def test_not_a_game_folder(self):
        with self.assertRaises(install.InstallError):
            install.install(Path(self.tmp.name) / "nope", running=lambda: False, log=self.log)

    def test_uninstall(self):
        self.run_install()
        install.uninstall(self.game, running=lambda: False, log=self.log)
        self.assertFalse((self.w / "ue4ss" / "Mods" / "RVWheel").exists())
        self.assertTrue((self.w / "dwmapi.dll").exists())     # UE4SS stays unless asked
        install.uninstall(self.game, everything=True, running=lambda: False, log=self.log)
        self.assertFalse((self.w / "dwmapi.dll").exists())
        self.assertFalse((self.w / "ue4ss").exists())
        with self.assertRaises(install.InstallError):
            install.uninstall(self.game, running=lambda: True, log=self.log)

    def test_download_picks_the_right_asset(self):
        zdata = make_zip()
        api = json.dumps({"assets": [
            {"name": "zDEV-UE4SS_v3.0.1-1.zip", "browser_download_url": "http://x/dev"},
            {"name": "zCustomGameConfigs.zip", "browser_download_url": "http://x/cfg"},
            {"name": "UE4SS_v3.0.1-1176-gabc.zip", "browser_download_url": "http://x/good"},
        ]}).encode()
        fetched = []

        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(req, timeout=0):
            fetched.append(req.full_url)
            return R(api if "api.github.com" in req.full_url else zdata)

        self.assertEqual(install.download_ue4ss(opener), zdata)
        self.assertEqual(fetched[-1], "http://x/good")

    def test_game_running_detection_reads_proc(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "123").mkdir(); (p / "123" / "cmdline").write_bytes(b"bash\0-c\0ls")
            self.assertFalse(install.game_running(p))
            (p / "456").mkdir(); (p / "456" / "cmdline").write_bytes(b"Z:\\x\\Ride-Win64-Shipping.exe\0Ride")
            self.assertTrue(install.game_running(p))

    def test_library_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            steam = Path(d) / "Steam"
            (steam / "steamapps").mkdir(parents=True)
            lib2 = Path(d) / "lib2"
            (lib2 / "steamapps" / "common" / "Ride" / "Ride" / "Binaries" / "Win64").mkdir(parents=True)
            (steam / "steamapps" / "libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n "0" {{ "path" "{steam}" }}\n "1" {{ "path" "{lib2}" }}\n}}\n')
            self.assertEqual(install.find_game(steam), lib2 / "steamapps" / "common" / "Ride")


if __name__ == "__main__":
    unittest.main()
