#!/usr/bin/env python3
"""
gold_setup_database.py — Historical Setup Pattern Database

Builds a searchable database of every trading setup/pattern found in the data,
links it to outcomes, and creates a prediction engine:

"Given today's conditions, what setups have historically occurred tomorrow?"
"What was the win rate of similar setups in the past?"

Uses real MT5 data only.
"""
import json, os, sys, re
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

def load_mt5():
    path = '/Users/yuhfriendchris/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json'
    with open(path, 'r') as f:
        raw = f.read()
    raw = re.sub(r',\s*([\]\}])', r'\1', raw)
    data = json.loads(raw)
    charts = data.get("charts", {})
    result = {}
    for sym in charts:
        result[sym] = {}
        for tf in charts[sym]:
            bars = charts[sym][tf]
            if not isinstance(bars, list) or not bars or not isinstance(bars[0], dict):
                continue
            result[sym][tf] = [Bar(time=b["t"], o=b["o"], h=b["h"], l=b["l"], c=b["c"], v=b.get("v", 0)) for b in bars]
    return result

def get_session(hour):
    if 0 <= hour < 8: return "ASIAN"
    elif 8 <= hour < 13: return "LONDON"
    elif 13 <= hour < 17: return "NY_AM"
    elif 17 <= hour < 21: return "NY_PM"
    else: return "OVERLAP"

def analyze_day(bars):
    """Extract all features from a single day's bars."""
    if len(bars) < 8:
        return None
    
    asian = bars[:8]
    london = bars[8:13] if len(bars) >= 13 else bars[8:]
    ny = bars[13:] if len(bars) > 13 else []
    
    asian_high = max(b.h for b in asian)
    asian_low = min(b.l for b in asian)
    asian_range = asian_high - asian_low
    asian_body = sum(abs(b.c - b.o) for b in asian) / len(asian)
    
    london_high = max(b.h for b in london) if london else asian_high
    london_low = min(b.l for b in london) if london else asian_low
    london_range = london_high - london_low if london else 0
    
    day_open = bars[0].o
    day_high = max(b.h for b in bars)
    day_low = min(b.l for b in bars)
    day_close = bars[-1].c
    day_range = day_high - day_low
    
    # Trend direction
    trend = "UP" if day_close > day_open else "DOWN" if day_close < day_open else "FLAT"
    
    # Volatility
    volatility = "HIGH" if day_range > asian_range * 5 else "MED" if day_range > asian_range * 3 else "LOW"
    
    # Manipulation (did London sweep Asian range?)
    manipulation = "YES" if (london_high > asian_high or london_low < asian_low) else "NO"
    
    # Reversal (did NY reverse London's move?)
    reversal = "NO"
    if ny and manipulation == "YES":
        if london_high > asian_high and ny[-1].c < london_high:
            reversal = "YES"
        if london_low < asian_low and ny[-1].c > london_low:
            reversal = "YES"
    
    # Equal highs/lows
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    eq_high = any(abs(highs[i] - highs[j]) < highs[i] * 0.002 for i in range(len(highs)) for j in range(i+1, len(highs)))
    eq_low = any(abs(lows[i] - lows[j]) < lows[i] * 0.002 for i in range(len(lows)) for j in range(i+1, len(lows)))
    
    # BOS/CHoCH
    hh = any(bars[i].h > bars[i-1].h for i in range(1, len(bars)))
    ll = any(bars[i].l < bars[i-1].l for i in range(1, len(bars)))
    
    return {
        "date": bars[0].time[:10],
        "asian_range": round(asian_range, 2),
        "asian_body": round(asian_body, 2),
        "london_range": round(london_range, 2),
        "day_range": round(day_range, 2),
        "day_open": day_open,
        "day_high": day_high,
        "day_low": day_low,
        "day_close": day_close,
        "trend": trend,
        "volatility": volatility,
        "manipulation": manipulation,
        "reversal": reversal,
        "eq_high": eq_high,
        "eq_low": eq_low,
        "hh": hh,
        "ll": ll,
        "bars": len(bars),
    }

def build_database():
    print("=" * 80)
    print(" GOLD SETUP DATABASE — Pattern Recognition Engine")
    print("=" * 80)
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD"]:
        if "H1" not in data.get(sym, {}):
            continue
        
        h1 = data[sym]["H1"]
        
        # Group by day
        days = defaultdict(list)
        for b in h1:
            days[b.time[:10]].append(b)
        
        sorted_days = sorted(days.keys())
        
        database = []
        
        print(f"\n{sym}: {len(sorted_days)} days analyzed")
        
        for day in sorted_days:
            analysis = analyze_day(days[day])
            if analysis:
                database.append(analysis)
        
        # Pattern analysis
        print(f"\n  Day Type Distribution:")
        types = defaultdict(int)
        for d in database:
            key = f"{d['trend']}_{d['volatility']}_{d['manipulation']}_{d['reversal']}"
            types[key] += 1
        
        for k, v in sorted(types.items(), key=lambda x: -x[1]):
            print(f"    {k}: {v} days")
        
        # Sequence analysis (what follows what?)
        print(f"\n  Sequence Patterns (3-day cycles):")
        if len(database) >= 3:
            sequences = defaultdict(int)
            for i in range(len(database) - 2):
                seq = f"{database[i]['trend']}-{database[i+1]['trend']}-{database[i+2]['trend']}"
                sequences[seq] += 1
            
            for k, v in sorted(sequences.items(), key=lambda x: -x[1]):
                print(f"    {k}: {v} occurrences")
        
        # Manipulation → Reversal correlation
        if database:
            manip_days = [d for d in database if d['manipulation'] == "YES"]
            rev_days = [d for d in manip_days if d['reversal'] == "YES"]
            print(f"\n  Manipulation → Reversal: {len(rev_days)}/{len(manip_days)} ({len(rev_days)/len(manip_days)*100:.1f}%)")
        
        # Save
        out = os.path.join(os.path.dirname(__file__), f'setup_database_{sym}.json')
        with open(out, 'w') as f:
            json.dump(database, f, indent=2)
        print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    build_database()
