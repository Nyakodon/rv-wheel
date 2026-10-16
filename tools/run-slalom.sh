#!/usr/bin/env bash
# dev: run the 'slalom' sim scenario against the test instance while recording telemetry to $1
cd "$(dirname "$0")/.."
python3 tools/record-telemetry.py "$1" 48 &
exec ./rv_wheel.py mod --sim slalom --no-ffb
