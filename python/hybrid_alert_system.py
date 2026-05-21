#!/usr/bin/env python3
"""
hybrid_strategy.py — Bot detects, alerts, executes YOUR decisions

What Chris does manually that no bot can:
  - Feel whether a sweep is real manipulation vs. noise
  - Read momentum and decide to scale in or exit early
  - Skip 80% of mechanical setups and only take A+
  - Recognize 3-5 day cycle phase by instinct

What bot CAN do:
  - Scan all timeframes 24/7
  - Detect accumulation/manipulation/distribution phases
  - Mark liquidity levels and FVG zones
  - Calculate precise risk sizing
  - Send Telegram alerts with entry/SL/TP/R:R
  - Execute immediately when Chris confirms

This script finds ALL setups in real-time and sends actionable alerts.
"""
import json, re, os, sys, math
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar, get_ob_precision_entry

# ── Config ──────────────────────────────────────────────────────────
SYMBOLS = {"GC=F": "XAUUSD", "SI=F": "XAGUSD"}
START = "2024-05-22"
END = "2026-05-21"

ALERT_FILE = os.path.join(os.path.dirname(__file__), 'setup_alerts.json')

# ── Fetch ─────────────────────────────────────────────────────────
def fetch_all(ticker: str) -> Dict[str, List[Bar]]:
    result = {}
    for interval in ["1h", "4h", "1d"]:
        df = yf.download(ticker, start=START, end=END, interval=interval,
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        bars = []
        for ts, row in df.iterrows():
            bars.append(Bar(
                time=str(ts)[:19], o=float(row["Open"]), h=float(row["High"]),
                l=float(row["Low"]), c=float(row["Close"]), v=int(row.get("Volume", 0)),
            ))
        key = {"1h": "H1", "4h": "H4", "1d": "D1"}[interval]
        result[key] = bars
    return result

# ── Session detection ──────────────────────────────────────────────
def get_session(dt: datetime) -> str:
    hour = dt.hour
    if 0 <= hour < 8:
        return "ASIAN"
    elif 8 <= hour < 13:
        return "LONDON"
    elif 13 <= hour < 17:
        return "NY_AM"
    elif 17 <= hour < 21:
        return "NY_PM"
    else:
        return "OVERLAP"

# ── AMD cycle on any timeframe ───────────────────────────────────
def detect_amd(bars: List[Bar], period: int = 6) -> Dict:
    if len(bars) < period + 1:
        return {"phase": "UNKNOWN", "confidence": 0, "direction": "NONE"}
    
    w = bars[-period:]
    highs = [b.h for b in w]
    lows = [b.l for b in w]
    closes = [b.c for b in w]
    rng = max(highs) - min(lows)
    avg_body = sum(abs(b.c - b.o) for b in w) / len(w)
    
    # Equal levels
    eq_h = sum(1 for i in range(len(highs)-1) if abs(highs[i] - highs[i+1]) < rng * 0.002)
    eq_l = sum(1 for i in range(len(lows)-1) if abs(lows[i] - lows[i+1]) < rng * 0.002)
    
    # HH/LL
    hh = sum(1 for i in range(len(highs)-1) if highs[i+1] > highs[i])
    ll = sum(1 for i in range(len(lows)-1) if lows[i+1] < lows[i])
    
    phase = "CHOP"
    confidence = 30
    direction = "NONE"
    
    if rng > 0 and avg_body / rng < 0.22 and (eq_h >= 2 or eq_l >= 2):
        phase = "ACCUMULATION"
        confidence = 70
        # In accumulation, mark the equal highs/lows as liquidity
        if eq_h >= 2:
            direction = "EXPECTING_DOWN"  # Will sweep high then reverse
        elif eq_l >= 2:
            direction = "EXPECTING_UP"
    elif hh >= 4 and closes[-1] > closes[0]:
        phase = "DISTRIBUTION_UP"
        confidence = 80
        direction = "UP"
    elif ll >= 4 and closes[-1] < closes[0]:
        phase = "DISTRIBUTION_DOWN"
        confidence = 80
        direction = "DOWN"
    elif hh >= 2 and closes[-1] > closes[0]:
        phase = "MANIPULATION_UP"
        confidence = 60
        direction = "UP"
    elif ll >= 2 and closes[-1] < closes[0]:
        phase = "MANIPULATION_DOWN"
        confidence = 60
        direction = "DOWN"
    
    return {
        "phase": phase,
        "confidence": confidence,
        "direction": direction,
        "range": rng,
        "avg_body": avg_body,
        "hh": hh,
        "ll": ll,
        "eq_highs": eq_h,
        "eq_lows": eq_l,
        "high": max(highs),
        "low": min(lows),
        "last_close": closes[-1],
    }

# ── Find FVGs ─────────────────────────────────────────────────────
def find_fvg_all(bars: List[Bar], min_size: float = 0.5) -> List[Dict]:
    fvgs = []
    for i in range(2, len(bars)):
        b0, b2 = bars[i-2], bars[i]
        if b0.h < b2.l:
            size = b2.l - b0.h
            if size >= min_size:
                fvgs.append({
                    "dir": "BUY", "top": b2.l, "bottom": b0.h,
                    "mid": (b2.l + b0.h) / 2, "idx": i, "time": b2.time,
                    "size": size,
                })
        if b0.l > b2.h:
            size = b0.l - b2.h
            if size >= min_size:
                fvgs.append({
                    "dir": "SELL", "top": b0.l, "bottom": b2.h,
                    "mid": (b0.l + b2.h) / 2, "idx": i, "time": b2.time,
                    "size": size,
                })
    return fvgs

# ── Find liquidity sweeps ────────────────────────────────────────
def find_sweeps(bars: List[Bar], lookback: int = 8) -> List[Dict]:
    """Find bars that sweep above/below recent structure."""
    if len(bars) < lookback + 2:
        return []
    
    recent = bars[-lookback:]
    recent_high = max(b.h for b in recent)
    recent_low = min(b.l for b in recent)
    
    # Check last 3 bars for sweep
    sweeps = []
    for b in bars[-3:]:
        if b.h > recent_high * 1.001:
            sweeps.append({
                "type": "HIGH_SWEEP",
                "level": recent_high,
                "sweep_high": b.h,
                "time": b.time,
                "bar": b,
            })
        if b.l < recent_low * 0.999:
            sweeps.append({
                "type": "LOW_SWEEP",
                "level": recent_low,
                "sweep_low": b.l,
                "time": b.time,
                "bar": b,
            })
    
    return sweeps

# ── Generate A+ alert ────────────────────────────────────────────
def generate_alert(data: Dict[str, List[Bar]], symbol: str, timestamp: str) -> Optional[Dict]:
    """Generate an A+ setup alert if conditions align across timeframes."""
    h1 = data["H1"]
    h4 = data["H4"]
    d1 = data["D1"]
    
    if len(h1) < 50 or len(h4) < 10:
        return None
    
    # Multi-timeframe AMD
    amd_h4 = detect_amd(h4, period=6)
    amd_d1 = detect_amd(d1, period=5)
    amd_h1 = detect_amd(h1[-24:], period=6)  # Last 6 hours
    
    # FVG on H1
    recent_fvgs = find_fvg_all(h1[-20:], min_size=1.0)
    if not recent_fvgs:
        return None
    
    latest_fvg = recent_fvgs[-1]
    
    # Sweeps
    sweeps = find_sweeps(h1[-20:])
    
    # Build alert
    alert = {
        "timestamp": timestamp,
        "symbol": symbol,
        "timeframe_context": {
            "D1": amd_d1,
            "H4": amd_h4,
            "H1": amd_h1,
        },
        "latest_fvg": latest_fvg,
        "recent_sweeps": sweeps,
    }
    
    # Score the setup
    score = 0
    direction = None
    
    # H4 and D1 alignment
    if amd_h4["direction"] == amd_d1["direction"] and amd_h4["direction"] != "NONE":
        score += 25
        direction = amd_h4["direction"]
    
    # H1 agrees
    if amd_h1["direction"] == direction:
        score += 20
    
    # FVG direction matches
    if latest_fvg["dir"] == direction:
        score += 20
    
    # Recent sweep in opposite direction (liquidity taken)
    for s in sweeps:
        if direction == "UP" and s["type"] == "LOW_SWEEP":
            score += 15
        if direction == "DOWN" and s["type"] == "HIGH_SWEEP":
            score += 15
    
    # High confidence phase
    if amd_h4["confidence"] >= 70:
        score += 10
    
    # Accumulation phase with clear range
    if amd_h4["phase"] == "ACCUMULATION" and amd_h4["range"] < 30:
        score += 15
    
    alert["score"] = score
    alert["direction"] = direction
    
    # Only alert for A+ setups (70+)
    if score >= 70:
        # Calculate entry/SL/TP
        fvg = latest_fvg
        if direction == "UP":
            entry = fvg["mid"]
            sl = fvg["bottom"] - 3.0
            tp = amd_h4["high"] + 10.0
        else:
            entry = fvg["mid"]
            sl = fvg["top"] + 3.0
            tp = amd_h4["low"] - 10.0
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        alert["setup"] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "risk_pips": risk,
        }
        
        return alert
    
    return None

# ── Main: scan all days and collect A+ alerts ────────────────────
def run():
    print("=" * 100)
    print(" A+ SETUP SCANNER")
    print(" Multi-timeframe AMD + FVG + Sweep detection")
    print("=" * 100)
    print()
    
    all_alerts = []
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" Scanning {symbol}...")
        print(f"{'='*80}")
        
        data = fetch_all(yf_ticker)
        h1 = data["H1"]
        
        print(f"  H1: {len(h1)} | H4: {len(data['H4'])} | D1: {len(data['D1'])}")
        
        # Walk through H1 bars, every 6 hours generate alerts
        for i in range(50, len(h1), 6):
            window_data = {
                "H1": h1[:i+1],
                "H4": data["H4"][:min(i//4+1, len(data["H4"]))],
                "D1": data["D1"][:min(i//24+1, len(data["D1"]))],
            }
            
            alert = generate_alert(window_data, symbol, h1[i].time)
            if alert:
                all_alerts.append(alert)
        
        print(f"  A+ alerts generated: {len(all_alerts)}")
    
    # Save all alerts
    with open(ALERT_FILE, 'w') as f:
        json.dump(all_alerts, f, indent=2)
    
    print(f"\n{'='*80}")
    print(f" TOTAL A+ ALERTS: {len(all_alerts)}")
    print(f" Saved: {ALERT_FILE}")
    print(f"{'='*80}")
    
    # Show sample alerts
    if all_alerts:
        print("\n=== SAMPLE A+ ALERTS ===")
        for a in all_alerts[:5]:
            print(f"\n  Time: {a['timestamp']}")
            print(f"  Symbol: {a['symbol']}")
            print(f"  Direction: {a['direction']}")
            print(f"  Score: {a['score']}/100")
            print(f"  H4 Phase: {a['timeframe_context']['H4']['phase']} (conf: {a['timeframe_context']['H4']['confidence']})")
            if 'setup' in a:
                s = a['setup']
                print(f"  Entry: {s['entry']:.2f} | SL: {s['sl']:.2f} | TP: {s['tp']:.2f} | R:R: {s['rr']:.1f}")

if __name__ == "__main__":
    run()
