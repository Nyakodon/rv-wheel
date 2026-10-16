#!/usr/bin/env python3
"""dev: record the mod's telemetry file to CSV.   record-telemetry.py out.csv seconds"""
import pathlib, sys, time
KEYS = "seq speed lat yaw rpm steer slip torque gear shifter engine wheel_on".split()
out = open(sys.argv[1], "w"); end = time.time() + float(sys.argv[2])
p = pathlib.Path("/dev/shm/rv-wheel/telemetry.txt"); last = None; t0 = time.time()
out.write("t," + ",".join(KEYS) + "\n")
while time.time() < end:
    try:
        kv = dict(l.split("=", 1) for l in p.read_text().split("\n") if "=" in l)
    except OSError:
        kv = {}
    if kv.get("end") == "1" and kv.get("seq") != last:
        last = kv["seq"]
        out.write(f"{time.time() - t0:.2f}," + ",".join(kv[k] for k in KEYS) + "\n"); out.flush()
    time.sleep(0.02)
