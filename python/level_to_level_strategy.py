#!/usr/bin/env python3
"""
level_to_level_strategy.py — What Chris Actually Does

MACRO (H4):       AMD cycle direction + identify 250-500pt move target
INTERMEDIATE (H1): Liquidity levels (equal highs/lows, session extremes)
MICRO (M15):      FVG entry after BOS/CHoCH

Execution:
  1. H4 shows clear direction (distribution phase, NOT accumulation)
  2. H1 has identifiable liquidity level within 250-500 pts
  3. M15 BOS breaks structure creating FVG
  4. Price retests FVG 50% level
  5. ENTER in H4 direction
  6. SL: beyond FVG extreme (tight)
  7. TP: next H1 liquidity level (level-to-level)
  8. TIME STOP: exit after 3 bars if not 50% toward target
  9. COMPOUND: $2 → $5 → $10 → $25 as equity grows
"""
import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

# ── Config ──────────────────────────────────────────────────────────
SYMBOLS = {"GC=F": "XAUUSD"}  # XAGUSD is poison, skip it
START = "2024-05-22"
END = "2026-05-21"
COMMISSION = 7.0

# ── Risk tiers ──────────────────────────────────────────────────────
def get_risk(equity: float, base: float = 100.0) -> float:
    mult = equity / base
    if mult >= 10.0: return 25.0
    elif mult >= 5.0: return 10.0
    elif mult >= 2.0: return 5.0
    return 2.0

# ── Fetch ─────────────────────────────────────────────────────────
def fetch_h1(ticker: str) -> List[Bar]:
    df = yf.download(ticker, start=START, end=END, interval="1h",
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return [Bar(time=str(ts)[:19], o=float(row["Open"]), h=float(row["High"]),
            l=float(row["Low"]), c=float(row["Close"]), v=int(row.get("Volume", 0)))
            for ts, row in df.iterrows()]

def to_h4(h1: List[Bar]) -> List[Bar]:
    if not h1: return []
    buckets = defaultdict(list)
    for b in h1:
        dt = datetime.strptime(b.time, "%Y-%m-%d %H:%M:%S")
        bucket = dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0)
        buckets[str(bucket)].append(b)
    return [Bar(time=k, o=v[0].o, h=max(b.h for b in v), l=min(b.l for b in v),
                c=v[-1].c, v=sum(b.v for b in v)) for k, v in sorted(buckets.items())]

# ── Liquidity Levels ───────────────────────────────────────────────
def find_liquidity_levels(bars: List[Bar], min_touches: int = 2, tolerance: float = 0.003) -> List[Tuple[float, str]]:
    """Find equal highs and lows (liquidity pools). Returns (level, type)."""
    if len(bars) < 20:
        return []
    
    levels = []
    
    # Equal highs
    highs = [(i, b.h) for i, b in enumerate(bars)]
    for i in range(len(highs)):
        for j in range(i+1, min(i+30, len(highs))):
            if abs(highs[i][1] - highs[j][1]) / highs[i][1] < tolerance:
                levels.append((highs[i][1], "EQ_HIGH"))
                break
    
    # Equal lows
    lows = [(i, b.l) for i, b in enumerate(bars)]
    for i in range(len(lows)):
        for j in range(i+1, min(i+30, len(lows))):
            if abs(lows[i][1] - lows[j][1]) / lows[i][1] < tolerance:
                levels.append((lows[i][1], "EQ_LOW"))
                break
    
    return levels

# ── Session extremes ──────────────────────────────────────────────
def session_extremes(bars: List[Bar]) -> Dict:
    """Find Asian, London, NY session highs/lows."""
    asian = [b for b in bars if 0 <= datetime.strptime(b.time, "%Y-%m-%d %H:%M:%S").hour < 8]
    london = [b for b in bars if 8 <= datetime.strptime(b.time, "%Y-%m-%d %H:%M:%S").hour < 13]
    ny = [b for b in bars if 13 <= datetime.strptime(b.time, "%Y-%m-%d %H:%M:%S").hour < 21]
    
    return {
        "asian_high": max((b.h for b in asian), default=0),
        "asian_low": min((b.l for b in asian), default=999999),
        "london_high": max((b.h for b in london), default=0),
        "london_low": min((b.l for b in london), default=999999),
        "ny_high": max((b.h for b in ny), default=0),
        "ny_low": min((b.l for b in ny), default=999999),
    }

# ── H4 AMD Direction ──────────────────────────────────────────────
def h4_direction(h4_bars: List[Bar], up_to_idx: int) -> Tuple[str, float, float]:
    """
    Returns (direction, target_distance, confidence).
    Only trade clear distribution phases.
    """
    if up_to_idx < 8:
        return ("NONE", 0, 0)
    
    w = h4_bars[max(0, up_to_idx-8):up_to_idx+1]
    highs = [b.h for b in w]
    lows = [b.l for b in w]
    closes = [b.c for b in w]
    
    range_size = max(highs) - min(lows)
    avg_body = sum(abs(b.c - b.o) for b in w) / len(w)
    
    # HH/LL count
    hh = sum(1 for i in range(len(highs)-1) if highs[i+1] > highs[i])
    ll = sum(1 for i in range(len(lows)-1) if lows[i+1] < lows[i])
    
    # In accumulation, skip
    if range_size > 0 and avg_body / range_size < 0.20:
        return ("ACCUMULATION", range_size, 20)
    
    # Distribution up
    if hh >= 5 and closes[-1] > closes[0] * 1.01:
        target = max(highs) + range_size * 0.5
        return ("BUY", target, 70 + hh)
    
    # Distribution down
    if ll >= 5 and closes[-1] < closes[0] * 0.99:
        target = min(lows) - range_size * 0.5
        return ("SELL", target, 70 + ll)
    
    # Manipulation up (expecting reversal down)
    if hh >= 3 and closes[-1] < closes[-3]:
        return ("SELL", min(lows), 60)
    
    # Manipulation down (expecting reversal up)
    if ll >= 3 and closes[-1] > closes[-3]:
        return ("BUY", max(highs), 60)
    
    return ("CHOP", range_size, 30)

# ── FVG detection ────────────────────────────────────────────────
def find_fvg(bars: List[Bar], min_size: float = 0.5) -> List[Dict]:
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

# ── Main simulation ──────────────────────────────────────────────
def simulate_level_to_level(h1: List[Bar], h4: List[Bar], symbol: str, base: float = 100.0) -> Dict:
    fvgs = find_fvg(h1, min_size=1.0)  # Only trade FVGs >= 1 pt
    
    if not fvgs:
        return {"error": "no FVGs"}
    
    equity = base
    peak = base
    max_dd = 0.0
    daily_loss = 0.0
    current_day = ""
    consecutive_losses = 0
    cooldown_until = 0
    
    trades = []
    
    for fvg in fvgs:
        fvg_idx = fvg["idx"]
        
        if fvg_idx + 1 >= len(h1):
            continue
        
        # Cooldown check
        if fvg_idx < cooldown_until:
            continue
        
        # Daily loss limit
        day = fvg["time"][:10]
        if day != current_day:
            current_day = day
            daily_loss = 0.0
        
        risk_usd = get_risk(equity, base)
        if daily_loss <= -(equity * 0.03):
            continue
        
        # ── H4 Direction ──
        h4_idx = min(fvg_idx // 4, len(h4) - 1)
        direction, target, confidence = h4_direction(h4, h4_idx)
        
        # Skip if no clear direction or chop
        if direction in ["NONE", "ACCUMULATION", "CHOP"]:
            continue
        
        # Skip if FVG direction doesn't match H4
        if fvg["dir"] != direction:
            continue
        
        # ── H1 Liquidity Levels ──
        h1_window = h1[max(0, fvg_idx-50):fvg_idx]
        liquidity = find_liquidity_levels(h1_window)
        sessions = session_extremes(h1_window)
        
        # Find TP: nearest liquidity level in direction of trade
        if direction == "BUY":
            # Target: nearest equal high or previous session high
            tp_candidates = [l[0] for l in liquidity if l[1] == "EQ_HIGH" and l[0] > fvg["mid"]]
            tp_candidates.append(sessions["london_high"])
            tp_candidates.append(sessions["ny_high"])
            tp_candidates.append(sessions["asian_high"])
            tp_candidates = [p for p in tp_candidates if p > fvg["mid"]]
            if not tp_candidates:
                continue
            tp = min(tp_candidates)  # Nearest level
        else:
            tp_candidates = [l[0] for l in liquidity if l[1] == "EQ_LOW" and l[0] < fvg["mid"]]
            tp_candidates.append(sessions["london_low"])
            tp_candidates.append(sessions["ny_low"])
            tp_candidates.append(sessions["asian_low"])
            tp_candidates = [p for p in tp_candidates if p < fvg["mid"]]
            if not tp_candidates:
                continue
            tp = max(tp_candidates)
        
        # Entry at FVG 50% (not mid, use ICT precision)
        entry = fvg["mid"]
        
        # SL: beyond FVG extreme
        if direction == "BUY":
            sl = fvg["bottom"] * 0.998
        else:
            sl = fvg["top"] * 1.002
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        # Skip if RR too small or risk too big
        if rr < 1.5 or risk > 20:  # Max 20 pt SL
            continue
        
        # ── Entry execution (limit order on next bars) ──
        entry_idx = None
        entry_price = None
        
        for j in range(fvg_idx + 1, min(fvg_idx + 4, len(h1))):
            b = h1[j]
            if direction == "BUY":
                if b.l <= entry:
                    entry_idx = j
                    entry_price = entry
                    break
            else:
                if b.h >= entry:
                    entry_idx = j
                    entry_price = entry
                    break
        
        if entry_idx is None:
            continue
        
        # ── Lot sizing ──
        if symbol == "XAUUSD":
            pip_val = 0.01
            pip_size = 0.01
        else:
            pip_val = 0.001
            pip_size = 0.001
        
        pips = risk / pip_size
        lot = risk_usd / (pips * pip_val)
        lot = max(0.01, min(lot, 0.5))
        
        # ── Walk to exit (with time stop) ──
        pnl = None
        exit_idx = None
        exit_price = None
        reason = ""
        r_mult = 0.0
        
        # Max 5 bars to resolve
        for k in range(entry_idx + 1, min(entry_idx + 6, len(h1))):
            b = h1[k]
            
            if direction == "BUY":
                if b.l <= sl:
                    exit_idx = k
                    exit_price = sl
                    r_mult = -1.0
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                if b.h >= tp:
                    exit_idx = k
                    exit_price = tp
                    r_mult = rr
                    pnl = risk_usd * rr - COMMISSION * lot
                    reason = "TP"
                    break
                
                # Time stop: after 3 bars, if not 50% toward target, exit breakeven
                bars_held = k - entry_idx
                if bars_held >= 3:
                    progress = (b.c - entry_price) / (tp - entry_price) if (tp - entry_price) != 0 else 0
                    if progress < 0.3:  # Less than 30% toward target
                        exit_idx = k
                        exit_price = b.c
                        r_mult = (b.c - entry_price) / risk if risk > 0 else 0
                        pnl = risk_usd * r_mult - COMMISSION * lot
                        reason = "TIME"
                        break
            else:
                if b.h >= sl:
                    exit_idx = k
                    exit_price = sl
                    r_mult = -1.0
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                if b.l <= tp:
                    exit_idx = k
                    exit_price = tp
                    r_mult = rr
                    pnl = risk_usd * rr - COMMISSION * lot
                    reason = "TP"
                    break
                
                bars_held = k - entry_idx
                if bars_held >= 3:
                    progress = (entry_price - b.c) / (entry_price - tp) if (entry_price - tp) != 0 else 0
                    if progress < 0.3:
                        exit_idx = k
                        exit_price = b.c
                        r_mult = (entry_price - b.c) / risk if risk > 0 else 0
                        pnl = risk_usd * r_mult - COMMISSION * lot
                        reason = "TIME"
                        break
        
        if pnl is None:
            exit_idx = min(entry_idx + 5, len(h1) - 1)
            exit_price = h1[exit_idx].c
            if direction == "BUY":
                gain = exit_price - entry_price
            else:
                gain = entry_price - exit_price
            r_mult = gain / risk if risk > 0 else 0
            pnl = risk_usd * r_mult - COMMISSION * lot
            reason = "EOD"
        
        equity += pnl
        daily_loss += pnl
        
        if equity > peak:
            peak = equity
            consecutive_losses = 0
        else:
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
        
        if pnl > 0:
            consecutive_losses = 0
        else:
            consecutive_losses += 1
            if consecutive_losses >= 3:
                cooldown_until = exit_idx + 12  # 12-bar cooldown (~12 hours)
        
        trades.append({
            "time": fvg["time"], "dir": direction, "entry": entry_price,
            "sl": sl, "tp": tp, "rr": rr, "exit": exit_price,
            "pnl": pnl, "r": r_mult, "reason": reason,
            "fvg_size": fvg["size"], "confidence": confidence,
            "bars_held": exit_idx - entry_idx if exit_idx else 0,
            "lot": lot, "risk_usd": risk_usd,
            "equity": equity,
        })
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": sum(t["pnl"] for t in trades),
        "equity": equity, "peak": peak, "max_dd": max_dd,
        "return_pct": (equity - base) / base * 100,
        "trades_detail": trades,
    }

# ── Main ───────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" LEVEL-TO-LEVEL STRATEGY")
    print(" H4 Direction → H1 Liquidity → M15 FVG Entry")
    print(" Time stops | 3-loss cooldown | Compounding")
    print("=" * 100)
    print()
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" {symbol}")
        print(f"{'='*80}")
        
        h1 = fetch_h1(yf_ticker)
        h4 = to_h4(h1)
        
        print(f"  H1: {len(h1)} | H4: {len(h4)}")
        
        result = simulate_level_to_level(h1, h4, symbol, base=100.0)
        
        if "error" in result:
            print(f"  {result['error']}")
            continue
        
        print(f"\n  Trades: {result['trades']}")
        print(f"  Wins: {result['wins']} | Losses: {result['losses']}")
        print(f"  Win Rate: {result['wr']:.1f}%")
        print(f"  P&L: ${result['pnl']:.2f}")
        print(f"  Equity: ${result['equity']:.2f} (peak: ${result['peak']:.2f})")
        print(f"  Return: {result['return_pct']:.1f}%")
        print(f"  Max DD: {result['max_dd']:.1f}%")
        
        if result['trades'] > 0:
            trades = result['trades_detail']
            
            reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
            for t in trades:
                reasons[t["reason"]]["count"] += 1
                reasons[t["reason"]]["pnl"] += t["pnl"]
            
            print(f"\n  Exits:")
            for r, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
                print(f"    {r}: {d['count']} (${d['pnl']:.2f})")
            
            # Monthly
            monthly = defaultdict(lambda: {"pnl": 0, "trades": 0})
            for t in trades:
                m = t["time"][:7]
                monthly[m]["pnl"] += t["pnl"]
                monthly[m]["trades"] += 1
            
            print(f"\n  Monthly:")
            for m in sorted(monthly.keys()):
                d = monthly[m]
                print(f"    {m}: {d['trades']} trades, ${d['pnl']:.2f}")
            
            # Save
            out = os.path.join(os.path.dirname(__file__), f'level_to_level_{symbol}.json')
            with open(out, 'w') as f:
                json.dump(trades, f, indent=2)
            print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
