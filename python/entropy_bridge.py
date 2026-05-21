#!/usr/bin/env python3
"""
entropy_bridge.py — Bridges entropy STDV+OTE signals into the OMNI pipeline.

Runs alongside orchestrator.py. When entropy detects a manipulation leg
with valid confluence on XAUUSD, writes a standard Signal to signals.json
that execution_agent can trade.

Usage: python3 entropy_bridge.py --loop
"""

import sys, json, os, re, time, argparse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "agents"))

from entropy_signal_agent import EntropySignalAgent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SIGNALS_PATH = PROJECT_ROOT / "shared" / "signals.json"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def read_signals() -> dict:
    try:
        with open(SIGNALS_PATH) as f:
            return json.load(f)
    except:
        return {"version": "1.0", "generated_at": "", "signals": [], "trail_proposals": {}}


def write_signals(signals: list):
    data = {
        "version": "1.0",
        "generated_at": datetime.now().isoformat(),
        "signals": signals,
        "trail_proposals": {},
    }
    with open(SIGNALS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def entropy_to_standard(signal: dict) -> dict:
    """Convert entropy signal format to OMNI standard Signal format."""
    
    sid = f"XAUUSD-M5-{int(time.time() * 1000000)}-{signal['direction'].upper()}"
    
    return {
        "id": sid,
        "ts": datetime.now().isoformat(),
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "direction": signal["direction"].upper(),
        "entry_type": "sweep_choch",
        "entry_price": signal["entry"],
        "sl": signal["sl"],
        "tp": signal["tp"],
        "confidence": min(0.95, 0.60 + signal["rr"] * 0.05),
        "reasons": [
            f"Entropy STDV+OTE confluence ({signal['confluence']})",
            f"Manipulation sweep at {signal.get('setup', 'unknown')}",
            f"R:R {signal['rr']}:1 | RISK_PCT=2.0",
        ],
        "scale_action": "HOLD",
        "scale_mult": 1.0,
        "htf_bias": signal["direction"].upper(),
        "source": "entropy-stdv-ote",
        "expires_at": (datetime.now() + timedelta(hours=6)).isoformat(),
        "re_emit_count": 0,
        "metadata": {
            "entropy": {
                "setup": signal.get("setup", "entropy_stdv_ote"),
                "confluence": signal.get("confluence", ""),
                "current_price": signal.get("current_price", 0),
                "distance_to_entry_pct": signal.get("distance_to_entry_pct", 0),
                "manipulation_time": signal.get("time", ""),
            }
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=30, help="Check interval seconds")
    args = parser.parse_args()
    
    agent = EntropySignalAgent()
    log("Entropy Bridge started — scanning XAUUSD M5 for manipulation legs")
    
    last_sent_id = None
    
    while True:
        try:
            signals = agent.run_sync()
            
            if signals:
                for sig in signals:
                    # Skip duplicates
                    sig_id = f"{sig['symbol']}-{sig['direction']}-{sig['entry']}"
                    if sig_id == last_sent_id:
                        continue
                    
                    # Convert to standard format
                    std = entropy_to_standard(sig)
                    
                    # Read current signals file
                    current = read_signals()
                    
                    # Append (keep last 10 signals max)
                    current["signals"].insert(0, std)
                    current["signals"] = current["signals"][:10]
                    
                    write_signals(current["signals"])
                    
                    log(f"🎯 ENTROPY SIGNAL WRITTEN: XAUUSD {std['direction']} @ {std['entry_price']} | SL:{std['sl']} TP:{std['tp']} | R:R {sig['rr']}:1")
                    last_sent_id = sig_id
            
            if not args.loop:
                break
                
            time.sleep(args.interval)
            
        except KeyboardInterrupt:
            log("Shutting down...")
            break
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
