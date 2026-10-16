#!/usr/bin/env python3
"""rv-wheel: steering-wheel support for "RV There Yet?" on Linux.

  list      list wheel-like input devices
  setup     guided calibration + button binding
  monitor   live view of the virtual-pad output (creates nothing)
  run       virtual Xbox-360 gamepad mode (no game modification)
  mod       in-game mod mode: feeds the RVWheel UE4SS mod, plays force feedback
  ffb-test  gentle force-feedback test

See README.md.
"""
import sys

from rvwheel.cli import main

if __name__ == "__main__":
    sys.exit(main())
