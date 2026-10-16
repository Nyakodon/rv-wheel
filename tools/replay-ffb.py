#!/usr/bin/env python3
"""dev: replay a recorded telemetry CSV through the force-feedback model and print the forces over time."""
import copy, csv, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from rvwheel import config, ffb, link

rows = list(csv.DictReader(open(sys.argv[1])))
cfg = copy.deepcopy(config.DEFAULT_CONFIG["ffb"])
now = [0.0]
m = ffb.FFModel(cfg, lambda: now[0])
print("    t  speed    lat   slip | spring damper  const    vib  hz")
step = max(1, len(rows) // 40)
peak_vib = 0.0
for i, r in enumerate(rows):
    now[0] = float(r["t"])
    t = link.Telemetry(seq=int(r["seq"]), driving=True, speed=float(r["speed"]), lat=float(r["lat"]),
                       yaw=float(r["yaw"]), rpm=float(r["rpm"]), slip=float(r["slip"]),
                       engine=r["engine"] == "1", gear=int(r["gear"]))
    f = m.compute(t)
    peak_vib = max(peak_vib, f.vib)
    if i % step == 0:
        print(f"{now[0]:5.1f} {t.speed:6.1f} {t.lat:6.1f} {t.slip:5.2f} | {f.spring:6.2f} {f.damper:6.2f} {f.constant:+6.2f} {f.vib:6.3f} {f.vib_hz:3.0f}")
print("peak vibration level:", round(peak_vib, 3))
