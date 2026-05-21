#!/usr/bin/env python3
"""
level_quality_test.py — Do H4/D1 levels actually work?

Instead of testing a strategy, test the CONCEPT:
  1. Mark all significant H4/D1 levels (OBs, FVGs, PDH/PDL, PMH/PML)
  2. When price hits a level, observe what happens next
  3. Does it reverse? Break through? Chop?

This tests whether the FOUNDATION of swing trading (levels hold)
exists in the data, without execution assumptions.
"""
import json, os, sys, re
from collections import defaultdict
from typing import List, Dict, Tuple

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

# ── Find all significant levels ──────────────────────────────────
def find_all_levels(d1_bars: List[Bar], h4_bars: List[Bar]) -> Dict[str, List[Dict]]:
    levels = {"D1_OB": [], "D1_FVG": [], "H4_OB": [], "H4_FVG": [], "PDH": [], "PDL": [], "PMH": [], "PML": []}
    
    # D1 OBs
    for i in range(2, len(d1_bars)):
        b0, b1 = d1_bars[i-2], d1_bars[i-1]
        body0 = abs(b0.c - b0.o)
        body1 = abs(b1.c - b1.o)
        range1 = b1.h - b1.l
        
        if body1 >= range1 * 0.5:
            if b0.c < b0.o and b1.c > b1.o:
                levels["D1_OB"].append({"type": "BULL", "top": b1.h, "bottom": b1.l, "time": b1.time})
            if b0.c > b0.o and b1.c < b1.o:
                levels["D1_OB"].append({"type": "BEAR", "top": b1.h, "bottom": b1.l, "time": b1.time})
    
    # D1 FVGs
    for i in range(3, len(d1_bars)):
        b0, b1, b2 = d1_bars[i-3], d1_bars[i-2], d1_bars[i-1]
        if b0.h < b2.l:
            levels["D1_FVG"].append({"type": "BULL", "top": b2.l, "bottom": b0.h, "time": b2.time})
        if b0.l > b2.h:
            levels["D1_FVG"].append({"type": "BEAR", "top": b0.l, "bottom": b2.h, "time": b2.time})
    
    # H4 OBs
    for i in range(2, len(h4_bars)):
        b0, b1 = h4_bars[i-2], h4_bars[i-1]
        body1 = abs(b1.c - b1.o)
        range1 = b1.h - b1.l
        
        if body1 >= range1 * 0.5:
            if b0.c < b0.o and b1.c > b1.o:
                levels["H4_OB"].append({"type": "BULL", "top": b1.h, "bottom": b1.l, "time": b1.time})
            if b0.c > b0.o and b1.c < b1.o:
                levels["H4_OB"].append({"type": "BEAR", "top": b1.h, "bottom": b1.l, "time": b1.time})
    
    # H4 FVGs
    for i in range(3, len(h4_bars)):
        b0, b1, b2 = h4_bars[i-3], h4_bars[i-2], h4_bars[i-1]
        if b0.h < b2.l:
            levels["H4_FVG"].append({"type": "BULL", "top": b2.l, "bottom": b0.h, "time": b2.time})
        if b0.l > b2.h:
            levels["H4_FVG"].append({"type": "BEAR", "top": b0.l, "bottom": b2.h, "time": b2.time})
    
    # Previous day/week high/low
    for i in range(1, len(d1_bars)):
        prev = d1_bars[i-1]
        levels["PDH"].append({"type": "RESISTANCE", "price": prev.h, "time": prev.time})
        levels["PDL"].append({"type": "SUPPORT", "price": prev.l, "time": prev.time})
    
    # Previous month high/low (approx every 20 bars)
    for i in range(20, len(d1_bars)):
        month_bars = d1_bars[i-20:i]
        pmh = max(b.h for b in month_bars)
        pml = min(b.l for b in month_bars)
        levels["PMH"].append({"type": "RESISTANCE", "price": pmh, "time": d1_bars[i].time})
        levels["PML"].append({"type": "SUPPORT", "price": pml, "time": d1_bars[i].time})
    
    return levels

def test_level_quality(h1_bars: List[Bar], levels: List[Dict], level_type: str, lookforward: int = 10):
    """Test: when H1 hits a level, what happens in next N bars?"""
    results = []
    
    for level in levels:
        price = level.get("top") or level.get("price") or level.get("bottom")
        if not price: continue
        
        # Find when H1 hits this level
        for i in range(len(h1_bars)):
            b = h1_bars[i]
            
            # Hit detection
            hit = False
            if level.get("type") in ["BULL", "SUPPORT"]:
                # Price touches or goes below level then reverses up
                if b.l <= price <= b.h:
                    hit = True
            else:
                if b.l <= price <= b.h:
                    hit = True
            
            if not hit:
                continue
            
            # Measure what happens next
            future = h1_bars[i+1:min(i+1+lookforward, len(h1_bars))]
            if not future:
                continue
            
            max_future_high = max(b.h for b in future)
            min_future_low = min(b.l for b in future)
            
            # Calculate moves from level
            up_move = max_future_high - price
            down_move = price - min_future_low
            
            # Determine outcome
            if level.get("type") in ["BULL", "SUPPORT"]:
                # Expected: price goes UP from level
                if up_move > down_move * 1.5:
                    outcome = "REVERSAL_UP"
                elif down_move > up_move * 1.5:
                    outcome = "BREAKDOWN"
                else:
                    outcome = "CHOP"
            else:
                # Expected: price goes DOWN from level
                if down_move > up_move * 1.5:
                    outcome = "REVERSAL_DOWN"
                elif up_move > down_move * 1.5:
                    outcome = "BREAKOUT"
                else:
                    outcome = "CHOP"
            
            results.append({
                "level_type": level_type,
                "price": price,
                "time": b.time,
                "outcome": outcome,
                "up_move": up_move,
                "down_move": down_move,
                "bars": len(future),
            })
            
            # Only count first hit
            break
    
    return results

def run():
    print("=" * 100)
    print(" LEVEL QUALITY TEST — Do H4/D1 levels actually hold?")
    print("=" * 100)
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD"]:
        if sym not in data: continue
        
        d1 = data[sym].get("D1", [])
        h4 = data[sym].get("H4", [])
        h1 = data[sym].get("H1", [])
        
        if len(d1) < 10 or len(h1) < 50:
            continue
        
        levels = find_all_levels(d1, h4)
        
        print(f"\n{'='*80}")
        print(f" {sym}")
        print(f"{'='*80}")
        
        for level_type, level_list in levels.items():
            if not level_list:
                continue
            
            results = test_level_quality(h1, level_list, level_type, lookforward=10)
            
            if not results:
                continue
            
            outcomes = defaultdict(int)
            for r in results:
                outcomes[r["outcome"]] += 1
            
            total = len(results)
            
            print(f"\n  {level_type}: {len(level_list)} levels, {total} hits")
            
            for outcome, count in sorted(outcomes.items(), key=lambda x: -x[1]):
                pct = count / total * 100
                print(f"    {outcome:20s}: {count:3d}/{total} ({pct:5.1f}%)")
            
            # Calculate average move
            avg_up = sum(r["up_move"] for r in results) / len(results)
            avg_down = sum(r["down_move"] for r in results) / len(results)
            print(f"    Avg up move: ${avg_up:.2f} | Avg down move: ${avg_down:.2f}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
