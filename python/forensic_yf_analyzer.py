#!/usr/bin/env python3
"""
forensic_yf_analyzer.py — Day-by-day pattern extraction from 2 years yfinance H1 data.

Analyzes every single day, every session, finds manipulation → reversal patterns,
extracts what works, and outputs the repeating rules.
"""
import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import (
    Bar, detect_sweep_high, detect_sweep_low,
    find_bearish_ob, find_bullish_ob,
    _calc_atr,
)

# ── Config ──────────────────────────────────────────────────────────
SYMBOLS = {"GC=F": "XAUUSD", "SI=F": "XAGUSD"}
H1_START = "2024-05-22"
H1_END = "2026-05-21"

# ── Fetch H1 bars ──────────────────────────────────────────────────
def fetch_h1(ticker: str) -> List[Bar]:
    df = yf.download(ticker, start=H1_START, end=H1_END, interval="1h",
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
    return bars

# ── Session detection ───────────────────────────────────────────────
def get_session(bar_time: str) -> str:
    try:
        dt = datetime.strptime(bar_time, "%Y-%m-%d %H:%M:%S")
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
    except:
        return "UNKNOWN"

# ── Analyze a single day ────────────────────────────────────────────
def analyze_day(h1_bars: List[Bar], date: str, symbol: str) -> Dict:
    day_bars = [b for b in h1_bars if b.time[:10] == date]
    if len(day_bars) < 6:
        return None
    
    # Session bars
    asian = [b for b in day_bars if get_session(b.time) == "ASIAN"]
    london = [b for b in day_bars if get_session(b.time) == "LONDON"]
    ny = [b for b in day_bars if get_session(b.time) in ["NY_AM", "NY_PM"]]
    
    if not asian or not london:
        return None
    
    # Session ranges
    asian_h = max(b.h for b in asian)
    asian_l = min(b.l for b in asian)
    asian_range = asian_h - asian_l
    
    london_h = max(b.h for b in london) if london else asian_h
    london_l = min(b.l for b in london) if london else asian_l
    london_range = london_h - london_l
    
    ny_h = max(b.h for b in ny) if ny else london_h
    ny_l = min(b.l for b in ny) if ny else london_l
    ny_range = ny_h - ny_l if ny else 0
    
    # Manipulation = London extends beyond Asian range
    manip_up = london_h > asian_h
    manip_down = london_l < asian_l
    
    # After manipulation, what did NY do?
    post_london = [b for b in day_bars if b.time > london[-1].time] if london else []
    
    reversal_up = False
    reversal_down = False
    continuation_up = False
    continuation_down = False
    
    if post_london:
        # Check first 3 NY bars
        first_ny = post_london[:3]
        if first_ny:
            first_ny_close = first_ny[-1].c
            first_ny_open = first_ny[0].o
            
            if manip_up:
                # If NY reversed down after London swept high
                if first_ny_close < asian_h:
                    reversal_up = True
                elif first_ny_close > london_h:
                    continuation_up = True
            
            if manip_down:
                # If NY reversed up after London swept low
                if first_ny_close > asian_l:
                    reversal_down = True
                elif first_ny_close < london_l:
                    continuation_down = True
    
    # Day close vs open
    day_open = day_bars[0].o
    day_close = day_bars[-1].c
    day_high = max(b.h for b in day_bars)
    day_low = min(b.l for b in day_bars)
    day_range = day_high - day_low
    
    # Day classification
    body = abs(day_close - day_open)
    body_pct = body / day_range if day_range > 0 else 0
    
    if day_close > day_open and body_pct > 0.4 and day_range > 30:
        day_type = "TREND_UP"
    elif day_close < day_open and body_pct > 0.4 and day_range > 30:
        day_type = "TREND_DOWN"
    elif body_pct < 0.25:
        day_type = "RANGE"
    elif manip_up and reversal_up:
        day_type = "REVERSAL_DOWN"
    elif manip_down and reversal_down:
        day_type = "REVERSAL_UP"
    else:
        day_type = "MIXED"
    
    # Setup quality
    setups = []
    atr = _calc_atr(day_bars, period=14) if len(day_bars) >= 14 else day_range / 4
    
    if manip_up:
        setups.append({
            "direction": "SELL",
            "entry": asian_h,
            "sl": london_h + atr * 0.3,
            "tp": asian_h - (london_h - asian_h) * 2,
            "pattern": "LONDON_SWEEP_SELL",
            "manip_distance": london_h - asian_h,
        })
    
    if manip_down:
        setups.append({
            "direction": "BUY",
            "entry": asian_l,
            "sl": london_l - atr * 0.3,
            "tp": asian_l + (asian_l - london_l) * 2,
            "pattern": "LONDON_SWEEP_BUY",
            "manip_distance": asian_l - london_l,
        })
    
    return {
        "date": date,
        "symbol": symbol,
        "day_type": day_type,
        "day_open": day_open,
        "day_close": day_close,
        "day_high": day_high,
        "day_low": day_low,
        "day_range": day_range,
        "asian_high": asian_h,
        "asian_low": asian_l,
        "asian_range": asian_range,
        "london_high": london_h,
        "london_low": london_l,
        "london_range": london_range,
        "ny_high": ny_h,
        "ny_low": ny_l,
        "ny_range": ny_range,
        "manip_up": manip_up,
        "manip_down": manip_down,
        "reversal_after_manip_up": reversal_up,
        "reversal_after_manip_down": reversal_down,
        "continuation_up": continuation_up,
        "continuation_down": continuation_down,
        "setups": setups,
        "body_pct": body_pct,
    }

# ── Main ────────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" FORENSIC DAY-BY-DAY — 2 Years Real Data")
    print("=" * 100)
    print()
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" {symbol} — Processing 2 years of H1 data...")
        print(f"{'='*80}")
        
        h1 = fetch_h1(yf_ticker)
        if not h1:
            print("  No data")
            continue
        
        print(f"  Loaded {len(h1)} H1 bars ({h1[0].time[:10]} → {h1[-1].time[:10]})")
        
        # Group by day
        days = defaultdict(list)
        for b in h1:
            days[b.time[:10]].append(b)
        
        print(f"  Trading days: {len(days)}")
        
        # Analyze each day
        daily = []
        for date in sorted(days.keys()):
            result = analyze_day(h1, date, symbol)
            if result:
                daily.append(result)
        
        print(f"  Analyzed {len(daily)} complete days")
        
        # ── Pattern extraction ──
        
        # 1. Manipulation frequency
        manip_days = [d for d in daily if d["manip_up"] or d["manip_down"]]
        print(f"\n  Manipulation detected: {len(manip_days)} days ({len(manip_days)/len(daily)*100:.1f}%)")
        
        # 2. Reversal accuracy after manipulation
        rev_after_manip = [d for d in manip_days 
                          if (d["manip_up"] and d["reversal_after_manip_up"]) or
                             (d["manip_down"] and d["reversal_after_manip_down"])]
        cont_after_manip = [d for d in manip_days
                           if (d["manip_up"] and d["continuation_up"]) or
                              (d["manip_down"] and d["continuation_down"])]
        
        print(f"  Reversal after manipulation: {len(rev_after_manip)} ({len(rev_after_manip)/len(manip_days)*100:.1f}%)")
        print(f"  Continuation after manipulation: {len(cont_after_manip)} ({len(cont_after_manip)/len(manip_days)*100:.1f}%)")
        
        # 3. When does reversal work best?
        rev_by_type = defaultdict(lambda: {"count": 0, "avg_range": 0, "avg_manip_dist": 0})
        for d in rev_after_manip:
            dt = d["day_type"]
            rev_by_type[dt]["count"] += 1
            rev_by_type[dt]["avg_range"] += d["day_range"]
            if d["setups"]:
                rev_by_type[dt]["avg_manip_dist"] += d["setups"][0].get("manip_distance", 0)
        
        print(f"\n  Reversal accuracy by day type:")
        for dt, stats in sorted(rev_by_type.items(), key=lambda x: -x[1]["count"]):
            c = stats["count"]
            print(f"    {dt}: {c} days, avg range {stats['avg_range']/c:.1f}, avg manip {stats['avg_manip_dist']/c:.1f}")
        
        # 4. Day type distribution
        type_counts = defaultdict(int)
        type_pnl = defaultdict(float)
        for d in daily:
            type_counts[d["day_type"]] += 1
            # Estimate P&L: if reversal trade worked = +2R, if not = -1R
            if d in rev_after_manip:
                type_pnl[d["day_type"]] += 2.0
            elif d in cont_after_manip:
                type_pnl[d["day_type"]] -= 1.0
        
        print(f"\n  Day type distribution & estimated edge:")
        for dt in sorted(type_counts.keys(), key=lambda x: -type_counts[x]):
            c = type_counts[dt]
            pnl = type_pnl[dt]
            print(f"    {dt}: {c} days, est edge {pnl:.0f}R")
        
        # 5. Range analysis
        ranges = [d["day_range"] for d in daily if d["day_range"] > 0]
        if ranges:
            avg_r = sum(ranges) / len(ranges)
            print(f"\n  Daily range stats:")
            print(f"    Average: {avg_r:.2f}")
            print(f"    Max: {max(ranges):.2f}")
            print(f"    Min: {min(ranges):.2f}")
            print(f"    Days > 250 pts: {sum(1 for r in ranges if r > 250)}")
            print(f"    Days > 500 pts: {sum(1 for r in ranges if r > 500)}")
            
            # XAUUSD: points are in price units (0.01 = 1 pip ≈ $1)
            # 250 points = 250 pips ≈ $250 per lot
            if symbol == "XAUUSD":
                print(f"    (250 pts ≈ $250 per 1.0 lot, $25 per 0.1 lot, $2.50 per 0.01 lot)")
        
        # 6. Session range ratios
        asian_ranges = [d["asian_range"] for d in daily if d["asian_range"] > 0]
        london_ranges = [d["london_range"] for d in daily if d["london_range"] > 0]
        ny_ranges = [d["ny_range"] for d in daily if d["ny_range"] > 0]
        
        if asian_ranges and london_ranges:
            print(f"\n  Session range comparison:")
            print(f"    Asian avg: {sum(asian_ranges)/len(asian_ranges):.2f}")
            print(f"    London avg: {sum(london_ranges)/len(london_ranges):.2f}")
            print(f"    NY avg: {sum(ny_ranges)/len(ny_ranges):.2f}" if ny_ranges else "    NY: insufficient data")
        
        # 7. 3-5 day cycle detection
        print(f"\n  3-5 day cycle detection:")
        day_types_list = [d["day_type"] for d in daily]
        
        for cycle_len in [3, 4, 5]:
            cycles = 0
            for i in range(cycle_len, len(day_types_list)):
                window = day_types_list[i-cycle_len:i]
                # Look for RANGE → TREND pattern
                has_range = any(t == "RANGE" for t in window)
                has_trend = any(t in ["TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKDOWN"] for t in window)
                if has_range and has_trend:
                    cycles += 1
            print(f"    {cycle_len}-day cycles (range+trend): {cycles}")
        
        # 8. Specific pattern: Accumulation → Manipulation → Distribution
        print(f"\n  AMD cycle detection:")
        amd_cycles = 0
        for i in range(3, len(daily)):
            w = daily[i-3:i]
            # Look for: range day → manip day → reversal day
            range_day = w[0]["day_type"] == "RANGE" or w[0]["body_pct"] < 0.3
            manip_day = w[1]["manip_up"] or w[1]["manip_down"]
            reversal_day = w[2]["day_type"] in ["REVERSAL_UP", "REVERSAL_DOWN"]
            if range_day and manip_day and reversal_day:
                amd_cycles += 1
        print(f"    Accumulation → Manipulation → Distribution cycles: {amd_cycles}")
        
        # 9. When does manipulation lead to reversal vs. continuation?
        print(f"\n  Manipulation outcome by day range:")
        for threshold in [20, 30, 50, 100]:
            small = [d for d in manip_days if d["day_range"] < threshold]
            large = [d for d in manip_days if d["day_range"] >= threshold]
            
            small_rev = sum(1 for d in small if d in rev_after_manip)
            large_rev = sum(1 for d in large if d in rev_after_manip)
            
            print(f"    Days < {threshold}: {len(small)} manip, {small_rev} rev ({small_rev/len(small)*100:.1f}% if >0)")
            print(f"    Days >= {threshold}: {len(large)} manip, {large_rev} rev ({large_rev/len(large)*100:.1f}% if >0)")
    
    print(f"\n{'='*80}")
    print(" ANALYSIS COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
