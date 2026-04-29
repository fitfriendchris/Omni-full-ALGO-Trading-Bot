#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  restart_clean.sh — full clean restart of the OMNI-ICT autonomy stack.
#
#  What it does, in order:
#    1. Unloads the LaunchAgent (so launchd stops trying to respawn anything)
#    2. Tells the existing watchdog to stop its children (graceful)
#    3. Force-kills any stragglers matching omni-ict service names
#    4. Rotates oversized log files (anything > 5 MB)
#    5. Removes stale state: watchdog_state.json, telegram_bot.pid
#    6. Reloads the LaunchAgent → watchdog spawns the full service graph
#    7. Tails the watchdog log for ~10 s to show fresh startup health
#
#  Safe to run repeatedly. No-ops the stops if nothing is running.
# ═══════════════════════════════════════════════════════════════════════════

set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.omni.ict.autonomy.plist"
LABEL="com.omni.ict.autonomy"
LOG_DIR="$ROOT/logs"
PY="$ROOT/venv/bin/python3"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
say()  { echo -e "${CYAN}${BOLD}► $*${RESET}"; }
ok()   { echo -e "${GREEN}✓ $*${RESET}"; }
warn() { echo -e "${YELLOW}⚠ $*${RESET}"; }
err()  { echo -e "${RED}✗ $*${RESET}"; }

# ── 1. Stop launchd ────────────────────────────────────────────────────────
say "Step 1/7  unload LaunchAgent"
if [[ -f "$PLIST" ]]; then
    launchctl unload "$PLIST" 2>/dev/null && ok "unloaded $LABEL" \
        || warn "$LABEL was not loaded"
else
    warn "plist not installed at $PLIST — install it with: cp com.omni.ict.autonomy.plist ~/Library/LaunchAgents/"
fi

# ── 1b. Sync source plist → installed plist if they differ ─────────────────
SRC_PLIST="$ROOT/com.omni.ict.autonomy.plist"
if [[ -f "$SRC_PLIST" ]]; then
    if [[ ! -f "$PLIST" ]] || ! cmp -s "$SRC_PLIST" "$PLIST"; then
        mkdir -p "$(dirname "$PLIST")"
        cp "$SRC_PLIST" "$PLIST"
        ok "synced source plist → $PLIST"
    fi
fi

# ── 2. Ask watchdog to stop children gracefully ────────────────────────────
say "Step 2/7  graceful watchdog --stop"
if [[ -x "$PY" ]]; then
    "$PY" "$ROOT/python/watchdog.py" --stop --grace 5 2>/dev/null || true
    ok "watchdog --stop sent"
else
    warn "venv python not found at $PY — skipping graceful stop"
fi
sleep 2

# ── 3. Force-kill stragglers ───────────────────────────────────────────────
say "Step 3/7  reap stragglers"
KILLED_ANY=0
for name in watchdog.py telegram_bot.py omni_bridge.py orchestrator.py auto_trader.py "uvicorn server:app"; do
    # pgrep -f matches against the full command line
    pids=$(pgrep -f "$name" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill -TERM $pids 2>/dev/null || true
        KILLED_ANY=1
        echo "    sent SIGTERM to $name → pids: $pids"
    fi
done
if [[ "$KILLED_ANY" == "1" ]]; then
    sleep 3
    # Anything still alive gets KILL'd
    for name in watchdog.py telegram_bot.py omni_bridge.py orchestrator.py auto_trader.py "uvicorn server:app"; do
        pids=$(pgrep -f "$name" 2>/dev/null || true)
        if [[ -n "$pids" ]]; then
            # shellcheck disable=SC2086
            kill -KILL $pids 2>/dev/null || true
            warn "force-killed stubborn $name → pids: $pids"
        fi
    done
fi
ok "process cleanup done"

# ── 4. Rotate oversized logs ───────────────────────────────────────────────
say "Step 4/7  rotate oversized logs (>5 MB)"
mkdir -p "$LOG_DIR/archive"
ROTATED=0
for f in "$LOG_DIR"/*.log; do
    [[ -f "$f" ]] || continue
    sz=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
    if [[ "$sz" -gt 5242880 ]]; then
        ts=$(date +%Y%m%d-%H%M%S)
        base=$(basename "$f")
        mv "$f" "$LOG_DIR/archive/${base}.${ts}"
        : > "$f"
        echo "    rotated $base ($((sz / 1024 / 1024)) MB → archive/${base}.${ts})"
        ROTATED=$((ROTATED + 1))
    fi
done
[[ "$ROTATED" -eq 0 ]] && ok "no logs needed rotation" || ok "rotated $ROTATED log(s)"

# ── 5. Clear stale state ───────────────────────────────────────────────────
say "Step 5/7  clear stale state files"
rm -f "$LOG_DIR/watchdog_state.json" && echo "    removed watchdog_state.json"
rm -f "$LOG_DIR/telegram_bot.pid"    && echo "    removed telegram_bot.pid (singleton lock)"
# Reset alerts log so a fresh "max_restarts_exceeded" entry is meaningful
if [[ -f "$LOG_DIR/alerts.json" ]]; then
    sz=$(stat -f%z "$LOG_DIR/alerts.json" 2>/dev/null || stat -c%s "$LOG_DIR/alerts.json" 2>/dev/null || echo 0)
    if [[ "$sz" -gt 32768 ]]; then
        mv "$LOG_DIR/alerts.json" "$LOG_DIR/archive/alerts.$(date +%Y%m%d-%H%M%S).json"
        echo '[]' > "$LOG_DIR/alerts.json"
        echo "    archived alerts.json"
    fi
fi
ok "state cleared"

# ── 6. Quick sanity checks before launching ────────────────────────────────
say "Step 6/7  preflight"
[[ -x "$PY" ]] || { err "venv python missing at $PY — run setup.sh first"; exit 2; }
[[ -f "$ROOT/python/rules.json" ]] || { err "rules.json missing"; exit 2; }
[[ -f "$ROOT/.env" ]] || warn ".env missing (Telegram token won't be loaded)"
ok "preflight ok"

# ── 7. Reload LaunchAgent (or run watchdog directly) ───────────────────────
say "Step 7/7  start watchdog"
if [[ -f "$PLIST" ]]; then
    launchctl load "$PLIST" && ok "LaunchAgent loaded → watchdog spawning services"
else
    warn "no plist installed; starting watchdog directly via nohup"
    nohup "$PY" "$ROOT/python/watchdog.py" > "$LOG_DIR/launchagent.out.log" 2>> "$LOG_DIR/launchagent.err.log" &
    echo $! > "$LOG_DIR/watchdog.pid"
    ok "watchdog started (pid=$(cat "$LOG_DIR/watchdog.pid"))"
fi

echo ""
say "Watching watchdog.log for 10 s — Ctrl-C anytime to stop tailing"
sleep 1
( tail -F "$LOG_DIR/watchdog.log" 2>/dev/null & TAIL_PID=$!
  sleep 10
  kill "$TAIL_PID" 2>/dev/null || true
) || true

echo ""
ok "restart_clean.sh complete."
echo "  status:        $PY $ROOT/python/watchdog.py --status"
echo "  service logs:  tail -f $LOG_DIR/{server,orchestrator,auto_trader,telegram_bot}.log"
echo "  data sanity:   $PY $ROOT/python/diag_omni_data.py"
