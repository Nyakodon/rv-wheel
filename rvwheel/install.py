"""Install / remove the RVWheel mod (and UE4SS, which it runs on) in the game folder."""
from __future__ import annotations

import io
import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

MOD_NAME = "RVWheel"                      # kept for compatibility
MODS = ("RVWheel",)                       # mods shipped in mod/
# other, independent mods that may share the same UE4SS install (never touched, but they keep UE4SS alive)
SIBLING_MODS = ("RVRestore",)
GAME_DIRNAME = "Ride"
UE4SS_API = "https://api.github.com/repos/UE4SS-RE/RE-UE4SS/releases/tags/experimental-latest"
LAUNCH_OPTION = 'WINEDLLOVERRIDES="dwmapi=n,b" %command%'
MOD_DIR = Path(__file__).resolve().parent.parent / "mod"
SOURCE_MOD = MOD_DIR / MOD_NAME


class InstallError(Exception):
    pass


def steam_libraries(steam_root: Path | None = None) -> list[Path]:
    roots = [steam_root] if steam_root else [Path.home() / ".local/share/Steam", Path.home() / ".steam/steam"]
    libs: list[Path] = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                libs.append(Path(m.group(1)))
        if (root / "steamapps").is_dir():
            libs.append(root)
    seen, out = set(), []
    for lib in libs:
        if lib not in seen:
            seen.add(lib)
            out.append(lib)
    return out


def find_game(steam_root: Path | None = None) -> Path | None:
    for lib in steam_libraries(steam_root):
        cand = lib / "steamapps" / "common" / GAME_DIRNAME
        if (cand / "Ride" / "Binaries" / "Win64").is_dir():
            return cand
    return None


def game_running(proc: Path = Path("/proc")) -> bool:
    for cmd in proc.glob("[0-9]*/cmdline"):
        try:
            if b"Ride-Win64-Shipping" in cmd.read_bytes():
                return True
        except OSError:
            continue
    return False


def win64_dir(game_dir: Path) -> Path:
    w = game_dir / "Ride" / "Binaries" / "Win64"
    if not w.is_dir():
        raise InstallError(f"{game_dir} does not look like the game folder (no Ride/Binaries/Win64)")
    return w


def download_ue4ss(opener: Callable = urllib.request.urlopen) -> bytes:
    """Latest UE4SS experimental build (the stable v3.0.1 predates Unreal 5.6 support)."""
    with opener(urllib.request.Request(UE4SS_API, headers={"User-Agent": "rv-wheel"}), timeout=30) as r:
        release = json.load(r)
    for asset in release.get("assets", []):
        name = asset["name"]
        if re.fullmatch(r"UE4SS_v[\w.\-]+\.zip", name) and not name.startswith("zDEV"):
            with opener(urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "rv-wheel"}),
                        timeout=120) as r:
                return r.read()
    raise InstallError("no UE4SS zip found in the experimental-latest release; pass --ue4ss-zip")


def _extract_ue4ss(data: bytes, dest: Path) -> list[str]:
    done = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if "dwmapi.dll" not in names or not any(n.startswith("ue4ss/UE4SS.dll") for n in names):
            raise InstallError("that zip does not look like a UE4SS release (no dwmapi.dll / ue4ss/UE4SS.dll)")
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise InstallError(f"unsafe path in zip: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.name == "UE4SS-settings.ini" and target.exists():
                continue          # keep the user's settings
            if "/Mods/" in "/" + info.filename and target.exists() and target.name in ("mods.txt", "mods.json"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
            done.append(info.filename)
    return done


def _select(mods) -> tuple[str, ...]:
    if mods in (None, "all"):
        return MODS
    chosen = (mods,) if isinstance(mods, str) else tuple(mods)
    bad = [m for m in chosen if m not in MODS]
    if bad:
        raise InstallError(f"unknown mod {bad[0]!r}; choose from {', '.join(MODS)} or all")
    return chosen


def install(game_dir: Path, ue4ss_zip: Path | None = None, running: Callable[[], bool] = game_running,
            download: Callable[[], bytes] = download_ue4ss, force: bool = False, log=print, mods=None) -> None:
    w = win64_dir(game_dir)
    chosen = _select(mods)
    if running() and not force:
        raise InstallError("the game is running - close it first (files in use would not load until restart anyway)")
    for name in chosen:
        if not (MOD_DIR / name).is_dir():
            raise InstallError(f"mod sources not found at {MOD_DIR / name}")
    have_ue4ss = (w / "dwmapi.dll").exists() and (w / "ue4ss" / "UE4SS.dll").exists()
    if not have_ue4ss:
        log("UE4SS not found in the game folder, installing it ...")
        data = ue4ss_zip.read_bytes() if ue4ss_zip else download()
        n = len(_extract_ue4ss(data, w))
        log(f"  installed UE4SS ({n} files)")
    else:
        log("UE4SS already present - leaving it as is")
    mods_dir = w / "ue4ss" / "Mods"
    for name in chosen:
        dest = mods_dir / name
        user_cfg = {}
        for cfg in dest.glob("Scripts/*_config.lua") if dest.exists() else []:
            user_cfg[cfg.name] = cfg.read_bytes()          # keep the user's edited settings across updates
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(MOD_DIR / name, dest)
        for fname, data in user_cfg.items():
            (dest / "Scripts" / fname).write_bytes(data)
        log(f"installed the {name} mod into {dest}")
    log("")
    log("One manual step: in Steam, right-click the game -> Properties -> Launch Options, and set:")
    log(f"    {LAUNCH_OPTION}")
    if "RVWheel" in chosen:
        log("Then start `./rv_wheel.py mod` before (or while) playing.")


def uninstall(game_dir: Path, everything: bool = False, running: Callable[[], bool] = game_running,
              force: bool = False, log=print, mods=None) -> None:
    w = win64_dir(game_dir)
    chosen = _select(mods)
    if running() and not force:
        raise InstallError("the game is running - close it first")
    for name in chosen:
        dest = w / "ue4ss" / "Mods" / name
        if dest.exists():
            shutil.rmtree(dest)
            log(f"removed {dest}")
    if everything:
        in_use = [m for m in (*MODS, *SIBLING_MODS) if (w / "ue4ss" / "Mods" / m).exists()]
        if in_use:
            log(f"kept UE4SS: still used by {', '.join(in_use)}")
            return
        for p in (w / "dwmapi.dll", w / "ue4ss"):
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        log("removed UE4SS; you can also clear the Steam launch option")
