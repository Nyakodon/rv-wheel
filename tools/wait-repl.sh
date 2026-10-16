#!/usr/bin/env bash
# wait until the dev REPL in the test instance answers (prints the world name), up to $1 seconds
cd "$(dirname "$0")/.."
for i in $(seq 1 "${1:-90}"); do
  if out=$(REPL_TIMEOUT=2 ./tools/repl.py 'print(require("UEHelpers").GetWorld():GetFullName())' 2>/dev/null) && [ -n "$out" ]; then echo "$out"; exit 0; fi
  sleep 2
done
echo "REPL never answered" >&2; exit 1
