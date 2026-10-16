#!/usr/bin/env bash
# Launch an ISOLATED second instance of "RV There Yet?" for mod development.
#
#  * game files : reflink copy at $T/Ride (UE4SS lives only there)
#  * Wine prefix: copy at $T/compat/pfx   (separate saves/registry)
#  * display    : headless gamescope, so no window ever appears on the desktop or steals focus
#  * network    : none (loopback only) so it never logs in alongside the real session
#  * pid ns     : everything inside dies with this script; nothing outside is visible/killable
#
# usage: test-instance.sh [extra game args...]      stop with Ctrl+C / kill this script's PID
set -euo pipefail
T=${RV_TEST_DIR:-/mnt/nvme-storage/rv-mod-test}
PROTON=${PROTON:-"$HOME/.local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/proton"}
STEAM=${STEAM_ROOT:-$HOME/.local/share/Steam}
RES_W=${RES_W:-1280}; RES_H=${RES_H:-720}
mkdir -p "$T/logs"

export RV_TEST_DIR="$T"   # lets the dev REPL mod find its folder
export STEAM_COMPAT_DATA_PATH="$T/compat"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$STEAM"
export STEAM_COMPAT_INSTALL_PATH="$T/Ride"
export STEAM_COMPAT_APP_ID=3949040 SteamAppId=3949040 SteamGameId=3949040
export STEAM_COMPAT_FLAGS=search-cwd
export WINEDLLOVERRIDES="dwmapi=n,b"   # load UE4SS's proxy dll
export PROTON_LOG=1 PROTON_LOG_DIR="$T/logs"
export PROTON_ENABLE_WAYLAND=0
unset MANGOHUD DISPLAY WAYLAND_DISPLAY LD_PRELOAD

exec bwrap --dev-bind / / --proc /proc --unshare-pid --unshare-net --tmpfs /tmp/.X11-unix --die-with-parent -- \
  gamescope --backend headless -W "$RES_W" -H "$RES_H" -w "$RES_W" -h "$RES_H" -- \
  "$PROTON" run "$T/Ride/Ride/Binaries/Win64/Ride-Win64-Shipping.exe" Ride \
  -windowed -ResX="$RES_W" -ResY="$RES_H" -nosplash -nosound "$@"
