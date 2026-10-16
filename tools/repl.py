#!/usr/bin/env python3
"""Send Lua to the dev REPL mod running in the isolated test instance and print the result.
usage: repl.py 'print(1+1)'      |      repl.py -f script.lua"""
import os, sys, time
D = os.environ.get("RV_TEST_DIR", "/mnt/nvme-storage/rv-mod-test") + "/repl"
code = open(sys.argv[2]).read() if sys.argv[1] == "-f" else sys.argv[1]
timeout = float(os.environ.get("REPL_TIMEOUT", "20"))
out = f"{D}/out.txt"
if os.path.exists(out): os.remove(out)
tmp = f"{D}/in.tmp"; open(tmp, "w").write(code); os.rename(tmp, f"{D}/in.lua")
t0 = time.time()
while time.time() - t0 < timeout:
    if os.path.exists(out):
        time.sleep(0.05); print(open(out).read(), end=""); sys.exit(0)
    time.sleep(0.1)
print("(no reply - is the test instance running and past the main menu load?)", file=sys.stderr); sys.exit(1)
