#!/usr/bin/env python3
"""
protocol_gate.py — Standalone signal interception layer for the OMNI ICT bot.

WHAT IT DOES:
  1. Reads a candidate signal JSON from /shared/protocol_signal_in.json
  2. Loads live MT5 data + account state
  3. Runs protocol_evaluator.evaluate() against ALL §1-§18rules
  4. Writes verdict to /shared/protocol_verdict.json
     { "trade": true/false, "reason": "...", "risk": {...}, "confidence": 8, ... }

HOW THE BOT USES IT:
  Before execute_setup(), the bot (or a wrapper) runs:
      python protocol_gate.py < /shared/candidate_signal.json
  Then reads /shared/protocol_verdict.json.
  If trade == false → skip signal.
  If trade == true  → pass risk params to place_order().

COMMAND-LINE:
    python protocol_gate.py --signal candidate.json --out verdict.json
    python protocol_gate.py --test          # dry-run with empty signal
"""
from __future__ import annotations
import json, os, sys, logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("protocol_gate")

OMNI = Path.home() / "Omni-full-ALGO-Trading-Bot"
SIG_IN   = OMNI / "shared" / "protocol_signal_in.json"
VERDICT  = OMNI / "shared" / "protocol_verdict.json"
MT5_DATA = (Path.home() / "Library/Application Support"
            / "net.metaquotes.wine.metatrader5/drive_c/users/user"
            / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json")

# ── Import protocol evaluator ──
_OMNI_PY = OMNI / "python"
sys.path.insert(0, str(_OMNI_PY))
try:
    from protocol_evaluator import evaluate
    _EVAL_OK = True
except Exception as e:
    log.error(f"Failed to import protocol_evaluator: {e}")
    _EVAL_OK = False
    _EVAL_ERR = str(e)


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def run_gate(signal: dict | None = None, out_path: Path = VERDICT) -> dict:
    if signal is None:
        signal = _load_json(SIG_IN)
    if not signal:
        verdict = {"trade": False, "reason": "EMPTY_SIGNAL", "timestamp": datetime.now(timezone.utc).isoformat()}
        out_path.write_text(json.dumps(verdict, indent=2))
        return verdict

    if not _EVAL_OK:
        verdict = {"trade": False, "reason": f"EVAL_IMPORT_FAIL: {_EVAL_ERR}", "timestamp": datetime.now(timezone.utc).isoformat()}
        out_path.write_text(json.dumps(verdict, indent=2))
        return verdict

    mt5 = _load_json(MT5_DATA)
    account = mt5.get("account", {})
    equity = float(account.get("equity", account.get("balance", 0)))

    # Also inject chart bars / formation_status from MT5 if available
    sym = signal.get("symbol", "")
    charts = mt5.get("charts", {})
    if sym and sym in charts:
        signal.setdefault("formation_status", charts[sym].get("formation_status", "UNKNOWN"))
        signal.setdefault("sweep_confirmed",   charts[sym].get("sweep", False))
        signal.setdefault("fvg_present",       charts[sym].get("fvg", False))
        signal.setdefault("ob_unmitigated",    charts[sym].get("ob_unmitigated", False))
        signal.setdefault("pda_zone",          charts[sym].get("pda_zone", ""))

    result = evaluate(signal, mt5, account)
    result["timestamp"] = datetime.now(timezone.utc).isoformat()

    # ── §16 Manual override bridge ──
    if result.get("override_active"):
        log.warning(f"MANUAL OVERRIDE ACTIVE: {result.get('override_command')} — trade={result['trade']}")

    out_path.write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OMNI ICT Protocol Gate")
    parser.add_argument("--signal", type=Path, default=SIG_IN, help="Input signal JSON")
    parser.add_argument("--out",    type=Path, default=VERDICT,   help="Output verdict JSON")
    parser.add_argument("--test",   action="store_true",         help="Dry-run with empty signal")
    parser.add_argument("--verbose", action="store_true",         help="Print full verdict")
    args = parser.parse_args()

    if not _EVAL_OK:
        print(json.dumps({"trade": False, "reason": f"EVAL_IMPORT_FAIL: {_EVAL_ERR}"}, indent=2))
        sys.exit(2)

    if args.test:
        v = run_gate({"symbol": "XAUUSD", "direction": "BUY", "entry": 3300, "sl": 3250, "tp": 3400, "session": "LONDON", "aggression": "normal", "h4_bias": "BULLISH", "ob_unmitigated": True, "fvg_present": True, "sweep_confirmed": True}, args.out)
    else:
        v = run_gate(None, args.out)
    print(json.dumps(v, indent=2))
