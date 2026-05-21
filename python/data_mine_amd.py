#!/usr/bin/env python3
"""
data_mine_amd.py — Brute force pattern discovery on 2-year H1 data.

Finds ALL break-of-structure events, ALL FVGs, and tests what happens
when price retests each one. Lets the data reveal the profitable edge.
"""
import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar, _calc_atr

# ── Config ──────────────────────────────────────────────────────────
SYMBOLS = {"GC=F": "XAUUSD", "SI=F": "XAGUSD"}
START = "2024-05-22"
END = "2026-05-21"

COMMISSION = 7.0

# ── Fetch H1 ─────────────────────────────────────────────────────────
def fetch_h1(ticker: str) -> List[Bar]:
    df = yf.download(ticker, start=START, end=END, interval="1h",
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

# ── Resample to H4 ───────────────────────────────────────────────────
def to_h4(h1_bars: List[Bar]) -> List[Bar]:
    """Convert H1 to H4 bars for AMD cycle detection."""
    if not h1_bars:
        return []
    by_4h = defaultdict(list)
    for b in h1_bars:
        dt = datetime.strptime(b.time, "%Y-%m-%d %H:%M:%S")
        # 4-hour buckets: 0,4,8,12,16,20
        bucket = dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0)
        by_4h[str(bucket)].append(b)
    
    h4 = []
    for bucket in sorted(by_4h.keys()):
        bb = by_4h[bucket]
        h4.append(Bar(
            time=bucket, o=bb[0].o, h=max(b.h for b in bb),
            l=min(b.l for b in bb), c=bb[-1].c, v=sum(b.v for b in bb),
        ))
    return h4

# ── Detect swing points ──────────────────────────────────────────────
def find_swing_highs(bars: List[Bar], window: int = 3) -> List[Tuple[int, float]]:
    """Find swing highs: bar higher than window bars on both sides."""
    swings = []
    for i in range(window, len(bars) - window):
        mid = bars[i].h
        left = [bars[j].h for j in range(i-window, i)]
        right = [bars[j].h for j in range(i+1, i+window+1)]
        if mid > max(left) and mid > max(right):
            swings.append((i, mid))
    return swings

def find_swing_lows(bars: List[Bar], window: int = 3) -> List[Tuple[int, float]]:
    swings = []
    for i in range(window, len(bars) - window):
        mid = bars[i].l
        left = [bars[j].l for j in range(i-window, i)]
        right = [bars[j].l for j in range(i+1, i+window+1)]
        if mid < min(left) and mid < min(right):
            swings.append((i, mid))
    return swings

# ── Detect BOS ─────────────────────────────────────────────────────
def find_all_bos(bars: List[Bar]) -> List[Dict]:
    """Find all break-of-structure events."""
    highs = find_swing_highs(bars)
    lows = find_swing_lows(bars)
    
    events = []
    
    # Bullish BOS: price breaks above recent swing high
    for i, level in highs:
        if i + 1 >= len(bars):
            continue
        # Check next 5 bars for break
        for j in range(i+1, min(i+6, len(bars))):
            if bars[j].c > level:
                events.append({
                    "type": "BOS_BULL",
                    "swing_idx": i,
                    "break_idx": j,
                    "level": level,
                    "time": bars[j].time,
                })
                break
    
    # Bearish BOS: price breaks below recent swing low
    for i, level in lows:
        if i + 1 >= len(bars):
            continue
        for j in range(i+1, min(i+6, len(bars))):
            if bars[j].c < level:
                events.append({
                    "type": "BOS_BEAR",
                    "swing_idx": i,
                    "break_idx": j,
                    "level": level,
                    "time": bars[j].time,
                })
                break
    
    return sorted(events, key=lambda x: x["break_idx"])

# ── Detect FVG ─────────────────────────────────────────────────────
def find_all_fvg(bars: List[Bar]) -> List[Dict]:
    """Find all Fair Value Gaps."""
    fvgs = []
    for i in range(2, len(bars)):
        b0 = bars[i-2]
        b1 = bars[i-1]
        b2 = bars[i]
        
        # Bullish FVG
        if b0.h < b2.l:
            fvgs.append({
                "direction": "BUY",
                "top": b2.l,
                "bottom": b0.h,
                "mid": (b2.l + b0.h) / 2,
                "created_idx": i,
                "time": b2.time,
            })
        
        # Bearish FVG
        if b0.l > b2.h:
            fvgs.append({
                "direction": "SELL",
                "top": b0.l,
                "bottom": b2.h,
                "mid": (b0.l + b2.h) / 2,
                "created_idx": i,
                "time": b2.time,
            })
    
    return fvgs

# ── Test retest outcomes ────────────────────────────────────────────
def test_retest(bars: List[Bar], fvg: Dict, bos: Dict) -> Optional[Dict]:
    """
    After BOS + FVG created, test if price retests FVG and what happens.
    Returns outcome if retest occurred within 10 bars.
    """
    start_idx = max(fvg["created_idx"], bos["break_idx"])
    if start_idx >= len(bars):
        return None
    
    fvg_top = fvg["top"]
    fvg_bot = fvg["bottom"]
    fvg_mid = fvg["mid"]
    direction = fvg["direction"]
    
    # Look for retest within 10 bars
    retest_idx = None
    for i in range(start_idx, min(start_idx + 11, len(bars))):
        if direction == "BUY":
            # Price needs to come down into FVG zone
            if bars[i].l <= fvg_top and bars[i].h >= fvg_bot:
                retest_idx = i
                break
        else:
            if bars[i].h >= fvg_bot and bars[i].l <= fvg_top:
                retest_idx = i
                break
    
    if retest_idx is None:
        return None
    
    # Simulate entry at FVG mid, SL at opposite extreme
    entry = fvg_mid
    if direction == "BUY":
        sl = fvg_bot * 0.999
        # TP: use recent swing projection or 2R
        # Find a reasonable TP from BOS projection
        tp = bos["level"] + (bos["level"] - fvg_bot) * 1.5
    else:
        sl = fvg_top * 1.001
        tp = bos["level"] - (fvg_top - bos["level"]) * 1.5
    
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    rr = reward / risk if risk > 0 else 0
    
    # Walk forward to see what happens
    for i in range(retest_idx, min(retest_idx + 20, len(bars))):
        if direction == "BUY":
            if bars[i].l <= sl:
                return {
                    "retest_idx": retest_idx,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "rr": rr,
                    "result": "SL",
                    "exit": sl,
                    "bars_held": i - retest_idx,
                    "direction": direction,
                }
            if bars[i].h >= tp:
                return {
                    "retest_idx": retest_idx,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "rr": rr,
                    "result": "TP",
                    "exit": tp,
                    "bars_held": i - retest_idx,
                    "direction": direction,
                }
        else:
            if bars[i].h >= sl:
                return {
                    "retest_idx": retest_idx,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "rr": rr,
                    "result": "SL",
                    "exit": sl,
                    "bars_held": i - retest_idx,
                    "direction": direction,
                }
            if bars[i].l <= tp:
                return {
                    "retest_idx": retest_idx,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "rr": rr,
                    "result": "TP",
                    "exit": tp,
                    "bars_held": i - retest_idx,
                    "direction": direction,
                }
    
    # No resolution within 20 bars
    return None

# ── AMD cycle on H4 ────────────────────────────────────────────────
def h4_amd_phase(h4_bars: List[Bar], up_to_idx: int) -> str:
    """Simple AMD: trending or ranging."""
    if up_to_idx < 6:
        return "UNKNOWN"
    
    window = h4_bars[max(0, up_to_idx-6):up_to_idx+1]
    highs = [b.h for b in window]
    lows = [b.l for b in window]
    closes = [b.c for b in window]
    
    range_size = max(highs) - min(lows)
    avg_body = sum(abs(b.c - b.o) for b in window) / len(window)
    
    hh = sum(1 for i in range(len(highs)-1) if highs[i+1] > highs[i])
    ll = sum(1 for i in range(len(lows)-1) if lows[i+1] < lows[i])
    
    if range_size > 0 and avg_body / range_size < 0.25:
        return "ACCUMULATION"
    elif hh >= 4:
        return "DISTRIBUTION_UP"
    elif ll >= 4:
        return "DISTRIBUTION_DOWN"
    elif hh >= 2 and closes[-1] > closes[0]:
        return "MANIPULATION_UP"
    elif ll >= 2 and closes[-1] < closes[0]:
        return "MANIPULATION_DOWN"
    else:
        return "CHOP"

# ── Main data mining ────────────────────────────────────────────────
def mine_symbol(ticker: str, symbol: str):
    print(f"\n{'='*80}")
    print(f" DATA MINING: {symbol}")
    print(f"{'='*80}")
    
    h1 = fetch_h1(ticker)
    h4 = to_h4(h1)
    
    print(f"  H1 bars: {len(h1)} | H4 bars: {len(h4)}")
    
    # Find all BOS on H1
    all_bos = find_all_bos(h1)
    print(f"  BOS events found: {len(all_bos)}")
    
    # Find all FVG on H1
    all_fvg = find_all_fvg(h1)
    print(f"  FVG events found: {len(all_fvg)}")
    
    # Match BOS + FVG pairs
    matched = []
    for bos in all_bos:
        for fvg in all_fvg:
            # FVG must be created after BOS
            if fvg["created_idx"] <= bos["break_idx"]:
                continue
            # FVG must be within 5 bars after BOS
            if fvg["created_idx"] > bos["break_idx"] + 5:
                continue
            # Direction must match
            if bos["type"] == "BOS_BULL" and fvg["direction"] != "BUY":
                continue
            if bos["type"] == "BOS_BEAR" and fvg["direction"] != "SELL":
                continue
            
            # Test retest
            outcome = test_retest(h1, fvg, bos)
            if outcome:
                # Add H4 context
                h4_idx = min(fvg["created_idx"] // 4, len(h4)-1)
                amd = h4_amd_phase(h4, h4_idx)
                outcome["h4_amd"] = amd
                outcome["time"] = fvg["time"]
                outcome["fvg_size"] = fvg["top"] - fvg["bottom"]
                matched.append(outcome)
    
    print(f"  BOS+FVG+retest matches: {len(matched)}")
    
    if not matched:
        return
    
    # Analyze outcomes
    wins = [m for m in matched if m["result"] == "TP"]
    losses = [m for m in matched if m["result"] == "SL"]
    
    print(f"\n  Results:")
    print(f"    TP: {len(wins)} | SL: {len(losses)}")
    if matched:
        print(f"    Win Rate: {len(wins)/len(matched)*100:.1f}%")
    
    # By H4 AMD phase
    by_amd = defaultdict(lambda: {"tp": 0, "sl": 0})
    for m in matched:
        by_amd[m["h4_amd"]]["tp" if m["result"] == "TP" else "sl"] += 1
    
    print(f"\n  By H4 AMD phase:")
    for phase, counts in sorted(by_amd.items(), key=lambda x: -(x[1]["tp"]+x[1]["sl"])):
        total = counts["tp"] + counts["sl"]
        wr = counts["tp"] / total * 100 if total > 0 else 0
        print(f"    {phase}: {total} trades, {wr:.1f}% WR (TP:{counts['tp']} SL:{counts['sl']})")
    
    # By R:R
    by_rr = defaultdict(lambda: {"tp": 0, "sl": 0})
    for m in matched:
        rr_bucket = f"{m['rr']:.1f}R" if m['rr'] < 10 else "10R+"
        by_rr[rr_bucket]["tp" if m["result"] == "TP" else "sl"] += 1
    
    print(f"\n  By R:R:")
    for rr, counts in sorted(by_rr.items(), key=lambda x: float(x[0].replace('R','').replace('+',''))):
        total = counts["tp"] + counts["sl"]
        wr = counts["tp"] / total * 100 if total > 0 else 0
        print(f"    {rr}: {total} trades, {wr:.1f}% WR")
    
    # By FVG size
    small_fvg = [m for m in matched if m["fvg_size"] < 2.0]
    large_fvg = [m for m in matched if m["fvg_size"] >= 2.0]
    
    if small_fvg:
        sw = sum(1 for m in small_fvg if m["result"] == "TP")
        print(f"\n  Small FVG (<2 pts): {len(small_fvg)} trades, {sw/len(small_fvg)*100:.1f}% WR")
    if large_fvg:
        lw = sum(1 for m in large_fvg if m["result"] == "TP")
        print(f"  Large FVG (≥2 pts): {len(large_fvg)} trades, {lw/len(large_fvg)*100:.1f}% WR")
    
    # By direction
    buys = [m for m in matched if m["direction"] == "BUY"]
    sells = [m for m in matched if m["direction"] == "SELL"]
    if buys:
        bw = sum(1 for m in buys if m["result"] == "TP")
        print(f"\n  BUYs: {len(buys)} trades, {bw/len(buys)*100:.1f}% WR")
    if sells:
        sw = sum(1 for m in sells if m["result"] == "TP")
        print(f"  SELLs: {len(sells)} trades, {sw/len(sells)*100:.1f}% WR")
    
    # Bars held
    tp_bars = [m["bars_held"] for m in wins]
    sl_bars = [m["bars_held"] for m in losses]
    if tp_bars:
        print(f"\n  Avg bars to TP: {sum(tp_bars)/len(tp_bars):.1f}")
    if sl_bars:
        print(f"  Avg bars to SL: {sum(sl_bars)/len(sl_bars):.1f}")
    
    # Save top patterns
    # Filter for high-probability setups
    good_setups = [m for m in matched
                   if m["result"] == "TP"
                   and m["rr"] >= 1.5
                   and m["h4_amd"] in ["MANIPULATION_UP", "MANIPULATION_DOWN", "DISTRIBUTION_UP", "DISTRIBUTION_DOWN"]]
    
    print(f"\n  High-quality TP setups (with trend + 1.5R+): {len(good_setups)}")
    
    # Save
    out = os.path.join(os.path.dirname(__file__), f'data_mined_{symbol}.json')
    with open(out, 'w') as f:
        json.dump(matched, f, indent=2)
    print(f"\n  Saved: {out}")

# ── Main ───────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" DATA MINING — Finding ALL BOS + FVG Patterns")
    print(" 2 Years H1 Data, H4 AMD Context")
    print("=" * 100)
    
    for yf_ticker, symbol in SYMBOLS.items():
        mine_symbol(yf_ticker, symbol)
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
