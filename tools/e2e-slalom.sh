#!/usr/bin/env bash
# dev: wait for the test instance, load the level, enter the RV, run the slalom sim and record telemetry to $1
cd "$(dirname "$0")/.."
OUT=${1:?csv path}
./tools/wait-repl.sh 120 || exit 1
./tools/repl.py -f tools/lua/open-level.lua
for i in $(seq 1 60); do
  sleep 2
  r=$(REPL_TIMEOUT=3 ./tools/repl.py 'local v=FindAllOf("BP_Vehicle_Winnebago_01_C"); print("vehicles", v and #v or 0)' 2>/dev/null)
  echo "$r" | grep -q "vehicles [1-9]" && break
done
sleep 3
./tools/repl.py -f tools/lua/enter-vehicle.lua
sleep 2
echo "starting slalom"
python3 tools/record-telemetry.py "$OUT" 50 &
timeout 52 ./rv_wheel.py mod --sim slalom --no-ffb
echo "slalom finished"
