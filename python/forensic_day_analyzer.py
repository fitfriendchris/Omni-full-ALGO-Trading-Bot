#!/usr/bin/env python3
"""
forensic_day_analyzer.py — Day-by-day pattern extraction from real MT5 data.

Reads every day, every bar, finds the exact ICT setups, and extracts
what ACTUALLY worked vs. what failed. Repeating patterns → strategy rules.
"""

import json, sys, os
from datetime import datetime, timedelta
from dataclasses import dataclass
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import (
    Bar, detect_sweep_high, detect_sweep_low,
    find_bearish_ob, find_bullish_ob,
    find_equal_highs, find_equal_lows,
    _calc_atr,
)

# ── Load data ────────────────────────────────────────────────────────
JSON_PATH = "/Users/yuhfriendchris/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"

with open(JSON_PATH, "r", encoding="utf-8") as f:
    raw = f.read()
import re
raw = re.sub(r',\s*([\]\}])', r'\1', raw)
data = json.loads(raw)

# ── Parse bars (correct MT5 format) ─────────────────────────────────
def parse_bars(symbol: str, tf: str) -> List[Bar]:
    bars = []
    chart_data = data.get("charts", {}).get(symbol, {}).get(tf, [])
    for item in chart_data:
        bars.append(Bar(
            time=item.get("t", "").replace(".", "-"),  # 2026.05.21 → 2026-05-21
            o=item.get("o", 0.0),
            h=item.get("h", 0.0),
            l=item.get("l", 0.0),
            c=item.get("c", 0.0),
            v=item.get("v", 0),
        ))
    return sorted(bars, key=lambda b: b.time)

# ── EMA ──────────────────────────────────────────────────────────────
def ema(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k = 2.0 / (period + 1)
    val = prices[0]
    for p in prices[1:]:
        val = p * k + val * (1 - k)
    return val

# ── Session detection ─────────────────────────────────────────────────
def get_session(bar_time: str) -> str:
    """Returns ASIAN, LONDON, NY, or OVERLAP based on UTC hour."""
    try:
        dt = datetime.strptime(bar_time, "%Y-%m-%d %H:%M:%S")
        hour = dt.hour
        if 0 <= hour < 8:
            return "ASIAN"
        elif 8 <= hour < 13:
            return "LONDON"
        elif 13 <= hour < 17:
            return "NY"
        elif 17 <= hour < 21:
            return "NY_PM"
        else:
            return "OVERLAP"
    except:
        return "UNKNOWN"

# ── Analyze a single day ────────────────────────────────────────────
@dataclass
class DayAnalysis:
    date: str
    symbol: str
    d1_open: float
    d1_high: float
    d1_low: float
    d1_close: float
    d1_range: float
    d1_body_pct: float
    prev_day_close: float
    gap: float
    ema20: float
    ema200: float
    ema800: float
    
    # Structure
    swing_highs: List[float]
    swing_lows: List[float]
    equal_highs: List[float]
    equal_lows: List[float]
    
    # Sessions
    asian_high: float
    asian_low: float
    asian_range: float
    london_high: float
    london_low: float
    london_range: float
    ny_high: float
    ny_low: float
    ny_range: float
    
    # Setups detected
    setups: List[Dict]
    
    # What actually happened after each setup
    outcomes: List[Dict]
    
    # Pattern classification
    day_type: str  # TREND_UP, TREND_DOWN, RANGE, BREAKOUT_UP, BREAKDOWN, REVERSAL
    manipulation_detected: bool
    reversal_detected: bool

from dataclasses import dataclass

# ── Detect day type ───────────────────────────────────────────────────
def classify_day(d1_bars: List[Bar], idx: int) -> str:
    if idx < 5:
        return "INITIAL"
    
    cur = d1_bars[idx]
    prev = d1_bars[idx-1]
    prev2 = d1_bars[idx-2]
    prev3 = d1_bars[idx-3]
    
    # Range
    avg_range = sum(b.h - b.l for b in d1_bars[idx-5:idx]) / 5
    cur_range = cur.h - cur.l
    
    # Body
    body = abs(cur.c - cur.o)
    body_pct = body / cur_range if cur_range > 0 else 0
    
    # Swing comparison
    hh = cur.h > prev.h and prev.h > prev2.h
    hl = cur.l > prev.l and prev.l > prev2.l
    lh = cur.h < prev.h and prev.h < prev2.h
    ll = cur.l < prev.l and prev.l < prev2.l
    
    # Reversal check
    reversal_up = prev.c < prev.o and cur.c > cur.o and body_pct > 0.5
    reversal_down = prev.c > prev.o and cur.c < cur.o and body_pct > 0.5
    
    if hh and hl and body_pct > 0.4:
        return "TREND_UP"
    elif lh and ll and body_pct > 0.4:
        return "TREND_DOWN"
    elif cur_range > avg_range * 1.5 and cur.c > cur.o:
        return "BREAKOUT_UP"
    elif cur_range > avg_range * 1.5 and cur.c < cur.o:
        return "BREAKDOWN"
    elif reversal_up:
        return "REVERSAL_UP"
    elif reversal_down:
        return "REVERSAL_DOWN"
    elif body_pct < 0.3:
        return "RANGE"
    else:
        return "MIXED"

# ── Analyze H1 bars for a day ────────────────────────────────────────
def analyze_h1_day(h1_bars: List[Bar], date: str, symbol: str) -> Dict:
    """Deep analysis of a single trading day on H1."""
    day_bars = [b for b in h1_bars if b.time[:10] == date]
    if len(day_bars) < 8:
        return None
    
    # Session ranges
    asian = [b for b in day_bars if get_session(b.time) == "ASIAN"]
    london = [b for b in day_bars if get_session(b.time) == "LONDON"]
    ny = [b for b in day_bars if get_session(b.time) in ["NY", "NY_PM"]]
    
    asian_h = max(b.h for b in asian) if asian else day_bars[0].h
    asian_l = min(b.l for b in asian) if asian else day_bars[0].l
    london_h = max(b.h for b in london) if london else asian_h
    london_l = min(b.l for b in london) if london else asian_l
    ny_h = max(b.h for b in ny) if ny else london_h
    ny_l = min(b.l for b in ny) if ny else london_l
    
    # Detect manipulation (London sweep of Asian range)
    manipulation_up = london_h > asian_h * 1.001 if asian_h > 0 else False
    manipulation_down = london_l < asian_l * 0.999 if asian_l > 0 else False
    
    # Detect reversal after manipulation
    reversal_after_sweep_up = False
    reversal_after_sweep_down = False
    
    if manipulation_up:
        # After London swept Asian high, did NY reverse down?
        post_sweep = [b for b in day_bars if b.time > london[-1].time] if london else []
        if post_sweep and post_sweep[0].c < post_sweep[0].o:
            reversal_after_sweep_up = True
    
    if manipulation_down:
        post_sweep = [b for b in day_bars if b.time > london[-1].time] if london else []
        if post_sweep and post_sweep[0].c > post_sweep[0].o:
            reversal_after_sweep_down = True
    
    # ICT setups
    setups = []
    atr = _calc_atr(day_bars[-20:], period=14) if len(day_bars) >= 20 else (day_bars[-1].h - day_bars[-1].l)
    
    if manipulation_up:
        # SELL setup after sweep of Asian high
        ob = find_bearish_ob(day_bars, start=0, search=12)
        if ob:
            ob_l, ob_h = ob
            setups.append({
                "direction": "SELL",
                "entry": asian_h,  # The manipulation wick extreme
                "sl": london_h + atr * 0.3,
                "tp": asian_h - (london_h - asian_h) * 2,
                "pattern": "LONDON_SWEEP_SELL",
                "confidence": 75,
            })
    
    if manipulation_down:
        ob = find_bullish_ob(day_bars, start=0, search=12)
        if ob:
            ob_l, ob_h = ob
            setups.append({
                "direction": "BUY",
                "entry": asian_l,
                "sl": london_l - atr * 0.3,
                "tp": asian_l + (asian_l - london_l) * 2,
                "pattern": "LONDON_SWEEP_BUY",
                "confidence": 75,
            })
    
    return {
        "date": date,
        "symbol": symbol,
        "asian_high": asian_h,
        "asian_low": asian_l,
        "london_high": london_h,
        "london_low": london_l,
        "ny_high": ny_h,
        "ny_low": ny_l,
        "manipulation_up": manipulation_up,
        "manipulation_down": manipulation_down,
        "reversal_after_sweep_up": reversal_after_sweep_up,
        "reversal_after_sweep_down": reversal_after_sweep_down,
        "setups": setups,
        "bars": len(day_bars),
        "day_range": day_bars[-1].h - day_bars[0].l,
        "day_close": day_bars[-1].c,
        "day_open": day_bars[0].o,
    }

# ── Main analysis ───────────────────────────────────────────────────
def run_forensic_analysis():
    print("=" * 100)
    print(" FORENSIC DAY-BY-DAY ANALYZER — Real MT5 Data")
    print("=" * 100)
    print()
    
    symbols = ["XAUUSD", "XAGUSD"]
    tfs = ["D1", "H4", "H1"]
    
    # Parse all data
    all_bars = {}
    for sym in symbols:
        all_bars[sym] = {}
        for tf in tfs:
            bars = parse_bars(sym, tf)
            all_bars[sym][tf] = bars
            print(f"{sym} {tf}: {len(bars)} bars ({bars[0].time[:10] if bars else 'NONE'} → {bars[-1].time[:10] if bars else 'NONE'})")
    
    print()
    
    # For each symbol, analyze each day
    for sym in symbols:
        print(f"\n{'='*80}")
        print(f" FORENSIC ANALYSIS: {sym}")
        print(f"{'='*80}")
        
        d1 = all_bars[sym].get("D1", [])
        h1 = all_bars[sym].get("H1", [])
        
        if not d1 or not h1:
            print(f"  Skipping — insufficient data")
            continue
        
        # Day-by-day analysis
        daily_reports = []
        
        for i, day_bar in enumerate(d1):
            date = day_bar.time[:10]
            day_type = classify_day(d1, i)
            
            # H1 analysis for this day
            h1_analysis = analyze_h1_day(h1, date, sym)
            
            if h1_analysis:
                daily_reports.append({
                    "date": date,
                    "day_type": day_type,
                    "d1_open": day_bar.o,
                    "d1_high": day_bar.h,
                    "d1_low": day_bar.l,
                    "d1_close": day_bar.c,
                    "d1_range": day_bar.h - day_bar.l,
                    "asian_high": h1_analysis["asian_high"],
                    "asian_low": h1_analysis["asian_low"],
                    "london_high": h1_analysis["london_high"],
                    "london_low": h1_analysis["london_low"],
                    "manipulation_up": h1_analysis["manipulation_up"],
                    "manipulation_down": h1_analysis["manipulation_down"],
                    "reversal_after_sweep_up": h1_analysis["reversal_after_sweep_up"],
                    "reversal_after_sweep_down": h1_analysis["reversal_after_sweep_down"],
                    "setups_count": len(h1_analysis["setups"]),
                    "day_range": h1_analysis["day_range"],
                })
        
        # Pattern extraction
        print(f"\n  Analyzed {len(daily_reports)} trading days")
        
        # Find manipulation days
        manip_days = [d for d in daily_reports if d["manipulation_up"] or d["manipulation_down"]]
        reversal_days = [d for d in daily_reports if d["reversal_after_sweep_up"] or d["reversal_after_sweep_down"]]
        
        print(f"  Manipulation detected: {len(manip_days)} days")
        print(f"  Reversal after sweep: {len(reversal_days)} days")
        
        if manip_days:
            # Calculate accuracy of reversal after manipulation
            correct_reversals = sum(1 for d in manip_days 
                                   if (d["manipulation_up"] and d["reversal_after_sweep_up"]) or
                                      (d["manipulation_down"] and d["reversal_after_sweep_down"]))
            print(f"  Manipulation → Reversal accuracy: {correct_reversals}/{len(manip_days)} = {correct_reversals/len(manip_days)*100:.1f}%")
        
        # Day type distribution
        type_counts = defaultdict(int)
        for d in daily_reports:
            type_counts[d["day_type"]] += 1
        
        print(f"\n  Day type distribution:")
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"    {t}: {c} days")
        
        # Range analysis
        ranges = [d["day_range"] for d in daily_reports if d["day_range"] > 0]
        if ranges:
            avg_range = sum(ranges) / len(ranges)
            max_range = max(ranges)
            print(f"\n  Daily range stats:")
            print(f"    Average: {avg_range:.2f}")
            print(f"    Maximum: {max_range:.2f}")
            print(f"    Days > 250 pts: {sum(1 for r in ranges if r > 250)}")
            print(f"    Days > 500 pts: {sum(1 for r in ranges if r > 500)}")
        
        # Asian range vs. London breakout
        asian_ranges = []
        london_extensions = []
        for d in daily_reports:
            if d["asian_high"] > d["asian_low"]:
                ar = d["asian_high"] - d["asian_low"]
                asian_ranges.append(ar)
                if d["manipulation_up"]:
                    ext = d["london_high"] - d["asian_high"]
                    london_extensions.append(ext)
                elif d["manipulation_down"]:
                    ext = d["asian_low"] - d["london_low"]
                    london_extensions.append(ext)
        
        if asian_ranges and london_extensions:
            print(f"\n  Session analysis:")
            print(f"    Avg Asian range: {sum(asian_ranges)/len(asian_ranges):.2f}")
            print(f"    Avg London extension beyond Asian: {sum(london_extensions)/len(london_extensions):.2f}")
            print(f"    Extension / Asian range ratio: {(sum(london_extensions)/len(london_extensions)) / (sum(asian_ranges)/len(asian_ranges)):.2f}x")
        
        # 3-5 day cycle detection
        print(f"\n  3-5 day cycle detection:")
        for lookback in [3, 4, 5]:
            cycles_found = 0
            for i in range(lookback, len(daily_reports)):
                # Check if day types repeat in cycle
                recent = [daily_reports[j]["day_type"] for j in range(i-lookback, i)]
                # Look for trend → range → trend pattern
                has_trend = any(t in ["TREND_UP", "TREND_DOWN"] for t in recent)
                has_range = any(t == "RANGE" for t in recent)
                if has_trend and has_range:
                    cycles_found += 1
            print(f"    {lookback}-day cycles with trend+range: {cycles_found}")
    
    print(f"\n{'='*80}")
    print(" ANALYSIS COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run_forensic_analysis()
