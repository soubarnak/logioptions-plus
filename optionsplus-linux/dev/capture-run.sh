#!/bin/sh
# Dev-only: start tap + UI for a capture run against the Wine agent bridge on port 47011.
S="$1"
cd "$(dirname "$0")/.." || exit 1
setsid python3 -I dev/tap.py 47011 "$S/run/capture.jsonl" > "$S/run/tap.log" 2>&1 < /dev/null &
sleep 1
OPL_ASAR="$S/run/app.asar" setsid electron35 --remote-debugging-port=9333 "$PWD/bootstrap" > "$S/run/ui.log" 2>&1 < /dev/null &
