"""
orb_signal_adapter.py — Bridge validated NY-Open ORB strategy into OMNI signal pipeline.

This module wraps the battle-tested ny_orb_strategy.py rules and emits OMNI-compatible
Signal objects. When enabled in rules.json, the orchestrator pulls ORB signals
instead of (or alongside) the ICT/SMC confluence engine.

Why this exists:
- The ICT/SMC confluence engine showed NEGATIVE expectancy (-44.8%) in honest backtests.
- ORB showed POSITIVE expectancy: PF 2.05, +344% OOS, 33% win rate, 4R winners.
- This adapter lets OMNI use the proven strategy without discarding the existing infra.

Usage in rules.json:
    "orb_signals": {
        "enabled": true,
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "risk_pct": 0.02,
        "rr": 4.0,
        "range_bars": 4,
        "body_atr": 0.5,
        "stop_cap_atr": 2.5,
        "open_hour_utc": 13,
        "long_only": true,
        "paper_mode": true   # <-- set false when ready for live
    }
"""
from __future__ import annotations
import os, sys, json, traceback
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Import the validated strategy (single source of truth)
from ny_orb_strategy import scan_h1, _atr

# ─── Signal envelope (matches existing OMNI pipeline) ──────────────────────────

@dataclass
class ORBSignal:
    symbol: str
    direction: str      # "BUY" | "SELL"
    entry: float
    sl: float
    tp: float
    rr: float
    size: float         # fractional lots
    reason: str
    confidence: float   # 0.0–1.0
    timestamp: str        # ISO UTC
    paper: bool         # if True, no real orders

# ─── Bar fetch (pluggable) ────────────────────────────────────────────────────

def fetch_h1_yfinance(symbol: str = "GC=F", period: str = "6d") -> Optional[List[dict]]:
    """Pull H1 bars from yfinance (gold futures proxy for XAUUSD)."""
    try:
        import yfinance as yf
        d = yf.Ticker(symbol).history(period=period, interval="60m")
        if not len(d):
            return None
        d.index = d.index.tz_convert("UTC")
        bars = []
        for ts, r in d.iterrows():
            bars.append({
                "open": float(r["Open"]),
                "high": float(r["High"]),
                "low": float(r["Low"]),
                "close": float(r["Close"]),
                "hour": ts.hour,
                "date": str(ts.date()),
                "ts": str(ts)
            })
        return bars
    except Exception as e:
        return None

def fetch_h1_from_omni_data(json_path: Path = None) -> Optional[List[dict]]:
    """Pull H1 bars from MT5 omni_data.json if available."""
    if json_path is None:
        json_path = Path("~/Omni-full-ALGO-Trading-Bot/shared/omni_data.json").expanduser()
    try:
        data = json.load(open(json_path))
        # Expects data["bars"]["XAUUSD"]["H1"] or similar
        bars = data.get("bars", {}).get("XAUUSD", {}).get("H1", [])
        if not bars:
            return None
        out = []
        for b in bars:
            ts = b.get("time", "")
            if isinstance(ts, str) and "T" in ts:
                hour = int(ts.split("T")[1].split(":")[0])
            else:
                hour = 0
            out.append({
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "hour": hour,
                "date": ts.split("T")[0] if isinstance(ts, str) else "",
                "ts": str(ts)
            })
        return out
    except Exception as e:
        return None

# ─── Signal generation ────────────────────────────────────────────────────────

def generate_orb_signal(cfg: dict, bars: Optional[List[dict]] = None) -> Optional[ORBSignal]:
    """
    Generate one ORB signal for the current day if a valid breakout just closed.
    Returns None if no signal, or if already signaled today.
    """
    if not cfg.get("enabled", False):
        return None

    symbol = cfg.get("symbol", "XAUUSD")
    risk_pct = cfg.get("risk_pct", 0.02)
    rr = cfg.get("rr", 4.0)
    range_bars = cfg.get("range_bars", 4)
    body_atr = cfg.get("body_atr", 0.5)
    stop_cap_atr = cfg.get("stop_cap_atr", 2.5)
    open_hour_utc = cfg.get("open_hour_utc", 13)
    paper = cfg.get("paper_mode", True)

    # Get bars if not provided
    if bars is None:
        bars = fetch_h1_yfinance()
        if bars is None:
            bars = fetch_h1_from_omni_data()
    if not bars:
        return None

    # Group by day
    from collections import defaultdict
    by_day = defaultdict(list)
    for b in bars:
        by_day[b["date"]].append(b)

    # Only consider today
    today_str = max(by_day.keys())
    today = sorted(by_day[today_str], key=lambda x: x["ts"])

    # Need enough bars for range + 1 trigger bar
    if len(today) < range_bars + 1:
        return None

    # Compute ATRs from full history
    full_bars = sorted(bars, key=lambda x: x["ts"])
    atr5 = _atr([{"high": b["high"], "low": b["low"], "close": b["close"]} for b in full_bars[-7:]], 5)
    atr14 = _atr([{"high": b["high"], "low": b["low"], "close": b["close"]} for b in full_bars[-16:]], 14)

    # Build strategy config
    strat_cfg = {
        "open_hour_utc": open_hour_utc,
        "range_bars": range_bars,
        "rr": rr,
        "body_atr": body_atr,
        "stop_cap_atr": stop_cap_atr
    }

    # Scan
    sig = scan_h1(today, strat_cfg, atr5=atr5, atr14=atr14)
    if not sig:
        return None

    # Compute position size (risk-based)
    sd = sig.entry - sig.sl
    if sd <= 0:
        return None
    # Assume $10K account for sizing — adjust as needed
    account = 10000.0
    risk_usd = account * risk_pct
    units = risk_usd / sd

    return ORBSignal(
        symbol=symbol,
        direction=sig.direction,
        entry=round(sig.entry, 2),
        sl=round(sig.sl, 2),
        tp=round(sig.tp, 2),
        rr=sig.rr,
        size=round(units / 100, 2),  # rough lot estimate (100 oz/standard lot)
        reason=sig.reason,
        confidence=0.75,  # ORB has proven edge but 33% win rate
        timestamp=datetime.now(timezone.utc).isoformat(),
        paper=paper
    )

# ─── OMNI-compatible JSON output ────────────────────────────────────────────

def signal_to_json(sig: ORBSignal) -> dict:
    """Convert ORBSignal to the same format as dual_tf_selector signals."""
    return {
        "id": f"orb_{sig.timestamp[:10]}_{sig.symbol}",
        "symbol": sig.symbol,
        "timeframe": "H1",
        "direction": sig.direction,
        "entry": sig.entry,
        "sl": sig.sl,
        "tp": sig.tp,
        "rr": sig.rr,
        "size": sig.size,
        "confidence": sig.confidence,
        "reason": sig.reason,
        "source": "ny_orb_strategy",
        "paper": sig.paper,
        "timestamp": sig.timestamp,
        "status": "pending"
    }

# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    from datetime import datetime, timezone

    parser = argparse.ArgumentParser(description="Generate ORB signal")
    parser.add_argument("--config", default="", help="JSON config string or file path")
    parser.add_argument("--paper", action="store_true", default=True)
    args = parser.parse_args()

    cfg = {"enabled": True, "paper_mode": args.paper}
    if args.config:
        p = Path(args.config)
        if p.exists():
            cfg.update(json.load(open(p)))
        else:
            try:
                cfg.update(json.loads(args.config))
            except Exception:
                pass

    sig = generate_orb_signal(cfg)
    if sig:
        print(json.dumps(signal_to_json(sig), indent=2))
    else:
        print(json.dumps({"signal": None, "status": "no_breakout"}))
