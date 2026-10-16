#!/usr/bin/env python3
"""dev: summarise a telemetry CSV recorded by record-telemetry.py (ranges help tune force feedback)."""
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
print("samples:", len(rows), " duration: %.1fs" % float(rows[-1]["t"]))
f = lambda k: [float(r[k]) for r in rows]
for k in ("speed", "lat", "yaw", "slip", "torque", "steer", "rpm"):
    v = f(k); print(f"{k:7s} min {min(v):9.2f}  max {max(v):9.2f}  p95|x| {sorted(abs(x) for x in v)[int(len(v) * .95)]:9.2f}")
print("gears seen:", sorted({r['gear'] for r in rows}), " shifter:", sorted({r['shifter'] for r in rows}))
step = max(1, len(rows) // 28)
print("   t   speed    lat    yaw   slip  steer gear")
for r in rows[::step]:
    print(f"{float(r['t']):5.1f} {float(r['speed']):7.1f} {float(r['lat']):6.1f} {float(r['yaw']):6.1f} {float(r['slip']):6.2f} {float(r['steer']):6.2f} {r['gear']:>4}")
