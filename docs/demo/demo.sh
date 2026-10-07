#!/usr/bin/env bash
# The 60-second Keelgate demo. Every command here is real and runs offline; nothing is mocked.
#
#   bash docs/demo/demo.sh              # paced for a screen recording (about 60 seconds)
#   PACE=0 bash docs/demo/demo.sh       # no pauses: how the test suite checks it still works
#
# Record it as a GIF with docs/demo/demo.tape (VHS) or asciinema + agg: see docs/demo/README.md.
set -euo pipefail

PACE="${PACE:-1}"
pause() { awk -v s="$1" -v p="$PACE" 'BEGIN { if (s * p > 0) system("sleep " s * p) }'; }
say()   { printf '\n\033[1;36m# %s\033[0m\n' "$*"; pause 2; }
show()  { printf '\033[1;32m$\033[0m %s\n' "$*"; pause 0.8; }

printf '\033[1mKeelgate\033[0m  the LLM proposes; deterministic code decides\n'
pause 3

say "Install"
show "pip install keelgate"          # shown, not run: this demo uses the checkout you are in
pause 1.5

say "1. Every action meets a gate: allowed, denied, or held for a human"
show "keelgate quickstart"
keelgate quickstart | sed -n '5,21p'
pause 7

say "2. Every decision lands in a tamper-evident log. Edit one record and it fails."
show "keelgate quickstart --tamper | tail -n 6"
keelgate quickstart --tamper | tail -n 6
pause 7

say "3. Long runs survive a crash and never repeat a write"
show "python examples/research_loop.py"
python examples/research_loop.py | sed -n '3,12p'
pause 7

say "4. 36 red-team cases assume the model is fooled. Does the harness still hold?"
show "keelgate eval run --suite redteam,unit,trajectory"
keelgate eval run --suite redteam,unit,trajectory --out "${TMPDIR:-/tmp}/keelgate-demo-eval" | sed -n '3,16p'
pause 8

say "github.com/anilatambharii/keelgate"
pause 4
