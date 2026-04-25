#!/bin/bash
# start_autonomous.command — launch OMNI-ICT in supervised autonomous mode.
#
# Double-click from Finder (on macOS) to run. This script:
#   1. cd's into the project directory (wherever this file lives).
#   2. Activates the venv if present (venv/bin/activate).
#   3. Ensures logs/ exists.
#   4. Starts python/watchdog.py, which spawns:
#        - server          (uvicorn, http://127.0.0.1:8787)
#        - orchestrator    (one cycle per 60s)
#        - auto_trader     (MT5 executor)
#      and restarts any that exit with back-off.
#   5. Keeps running until you close the Terminal window (watchdog traps
#      SIGTERM and shuts down children gracefully).
#
# To stop cleanly from anywhere: `python python/watchdog.py --stop`
# To inspect:                    `python python/watchdog.py --status`
#
# For unattended (boot-time) startup, see com.omni.ict.autonomy.plist and
# OMNI_AUTONOMY.md for LaunchAgent install instructions.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "── OMNI-ICT autonomous mode ──"
echo "project: $DIR"
echo "time:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Activate venv if present
if [ -f "$DIR/venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$DIR/venv/bin/activate"
  echo "venv:    $DIR/venv"
fi

# Ensure log directory
mkdir -p "$DIR/logs"

# Pre-flight: verify rules.json is valid JSON
python3 - <<'PY'
import json, sys, pathlib
p = pathlib.Path("python/rules.json")
try:
    json.loads(p.read_text(encoding="utf-8"))
    print(f"rules:   {p} (ok)")
except Exception as e:
    print(f"rules:   {p} INVALID: {e}", file=sys.stderr)
    sys.exit(1)
PY

echo "starting watchdog (Ctrl-C to stop)..."
exec python3 "$DIR/python/watchdog.py"
