#!/usr/bin/env python3
"""
killswitch.py  |  OMNI ICT Protocol v27.0
Protective kill switch + telemetry + manual override bridge.

§14 rule: VOORUTGANG > VEILIGHEID + WINSTGENERATIE
"""
from __future__ import annotations
import json, os, sys, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("killswitch")

OMNI = Path.home() / "Omni-full-ALGO-Trading-Bot"
STATE_PATH = OMNI / "python" / "trader_state.json"
MEM_PATH   = OMNI / "python" / "operational_memory.json"
OVERRIDE   = OMNI / "shared"  / "manual_override.json"
HALT_FILE  = OMNI / "python" / "HALT"
LOG_DIR    = OMNI / "logs"

# ── Telegram notification helper (minimal, no circular import) ──
def _telegram(msg: str):
    try:
        import requests
        token = os.getenv("OMNI_TELEGRAM_TOKEN","")
        chat  = os.getenv("OMNI_TELEGRAM_CHAT_ID","5786598754")
        if token:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                timeout=5,
            )
    except Exception as e:
        log.error(f"Telegram fail: {e}")

# ── §14 Halts ──
def should_halt(state: dict, equity: float) -> tuple[bool, str]:
    """Returns (halt, reason). If True, bot must NOT place new orders."""
    # 1. Consecutive loss tier halts
    loss_streak = state.get("loss_streak", 0)
    week_profit = sum(float(t.get("profit",0)) for t in state.get("week_trades", []))

    # Tier 1
    if loss_streak >= 2 and week_profit < 0 and equity <= 100:
        return True, "HALT_TIER_1: -0.5% DD @ <$100 + overheat"

    # Tier 2
    if -5 <= week_profit < 0 or loss_streak >= 2:
        return True, "HALT_TIER_2: week loss or two consecutive"

    # Tier 3 (critical)
    if equity <= 100 and (state.get("trading_halted") or loss_streak >= 3):
        return True, "HALT_TIER_3: <=$100 + halted OR 3+ losses"

    # Tier 4 (post-close)
    close_time = state.get("last_close_time")
    if close_time:
        ago = (datetime.now(timezone.utc) - datetime.fromisoformat(close_time)).total_seconds() / 60
        if 10 <= ago <= 30 and equity > 100 and week_profit >= 0:
            return False, ""  # allowed, just scored
        if 10 <= ago <= 30 and equity > 100 and week_profit < 0:
            return True, f"HALT_TIER_4: post-close calm {ago:.0f}m, DD {week_profit:.2f}"
        if ago == 2 and loss_streak >= 2 and equity < 100 and week_profit < 5:
            return True, "HALT_TIER_4_EXTREME: 2m post-close, overheat, <$100, week <$5"

    return False, ""

# ── §16 Manual override file parser ──
def read_override() -> dict:
    if OVERRIDE.exists():
        try:
            return json.loads(OVERRIDE.read_text())
        except Exception:
            return {}
    return {}

def active_override() -> tuple[bool, str]:
    ovr = read_override()
    if not ovr.get("active"):
        return False, ""
    return True, ovr.get("command", "")

# ── Run guard (called before every evaluation) ──
def guard(equity: float, state_file: Path = None) -> dict:
    if state_file is None:
        state_file = STATE_PATH
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    halt, reason = should_halt(state, equity)
    ovr_act, ovr_cmd = read_override().get("active"), read_override().get("command","")
    return {
        "halt": halt,
        "reason": reason,
        "override_active": bool(ovr_act),
        "override_command": ovr_cmd,
        "equity": equity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ── §17 STATE PERSISTENCE (called by auto_trader after every trade) ──
def persist_peak(ticket: str, peak_r: float, state_file: Path = None):
    if state_file is None:
        state_file = STATE_PATH
    try:
        st = json.loads(state_file.read_text()) if state_file.exists() else {}
        st.setdefault("peak_r_by_ticket", {})[ticket] = peak_r
        state_file.write_text(json.dumps(st, indent=2))
    except Exception:
        pass

def persist_partial(ticket: str, label: str, state_file: Path = None):
    if state_file is None:
        state_file = STATE_PATH
    try:
        st = json.loads(state_file.read_text()) if state_file.exists() else {}
        st.setdefault("partial_close_status", {})[ticket] = label
        state_file.write_text(json.dumps(st, indent=2))
    except Exception:
        pass

def get_telemetry(state_file: Path = None) -> dict:
    if state_file is None:
        state_file = STATE_PATH
    if not state_file.exists():
        return {}
    try:
        st = json.loads(state_file.read_text())
        return {
            "peak_r": st.get("peak_r_by_ticket", {}),
            "partial_status": st.get("partial_close_status", {}),
            "halt_flags": st.get("halt_flags", {}),
            "loss_streak": st.get("loss_streak", 0),
            "equity": st.get("equity", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return {}

# ═════════════════════════════════════════════════════════════════
#  MAIN — when run standalone for testing / simulation
# ═════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    equity = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    if equity <= 0:
        print("Usage: killswitch.py <equity>")
        sys.exit(1)
    res = guard(equity)
    print(json.dumps(res, indent=2))
