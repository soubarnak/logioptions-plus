#!/bin/bash
# Long-lived native agent + real Options+ UI for interactive driving over CDP.
# Needs the Logitech payload unpacked outside the repo:
#   OPL_ASAR       app.asar of the Options+ frontend
#   OPL_DATA_DIR   logioptionsplus depot data/ (strings, device catalogue)
#   OPL_DEPOT_DIR  directory with one unpacked depot per device (mx_master_3s/ ...)
# Optional: OPL_STATE_DIR (default: fresh mktemp dir), OPL_SHOT (screenshot base path),
#           CDP_PORT (default 9340).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
: "${OPL_ASAR:?} ${OPL_DATA_DIR:?} ${OPL_DEPOT_DIR:?}"
export OPL_ASAR OPL_DATA_DIR OPL_DEPOT_DIR OPL_UI_LOGGING=1
export OPL_STATE_DIR=${OPL_STATE_DIR:-$(mktemp -d)} OPL_SHOT=${OPL_SHOT:-/tmp/opl-shot.png}
LOG=${OPL_LOG_DIR:-$(dirname "$OPL_SHOT")}
SOCK=/tmp/logitech_kiros_agent-$(printf %s "$(id -un)" | md5sum | cut -d' ' -f1)
rm -f "$SOCK"
cd "$ROOT"
OPL_LOG_LEVEL=DEBUG python3 -m opl_agent.main >"$LOG/agent.log" 2>&1 & AG=$!
sleep 2
electron35 --remote-debugging-port="${CDP_PORT:-9340}" --no-sandbox "$HERE" >"$LOG/ui.log" 2>&1 & UI=$!
echo "AG=$AG UI=$UI" >"$LOG/pids"
wait $UI; kill $AG 2>/dev/null
