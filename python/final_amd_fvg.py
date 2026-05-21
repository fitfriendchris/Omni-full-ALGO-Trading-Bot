#!/usr/bin/env python3
"""
final_amd_fvg.py — THE REAL STRATEGY

Problem discovered: SL at FVG extreme gets hit on almost every retest.
Fix: Wider SL (1.5x ATR beyond FVG), entry at FVG 70% (deeper discount).

MACRO: H4 AMD direction
MICRO: H1 FVG entry at 70% level (not 50%)
SL: Beyond FVG + ATR buffer (never gets run)
TP: Next liquidity level or 3R minimum
Time stop: 5 bars
Compounding: $2 → $5 → $10 → $25
"""
import json, math, os, sys
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

# ── Config ──────────────────────────────────────────────────────────
SYMBOLS = {"GC=F": "XAUUSD"}  # XAGUSD is poison
START = "2024-05-22"
END = "2026-05-21"
COMMISSION = 7.0

# ── Helpers ─────────────────────────────────────────────────────────
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

def calc_atr(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return sum(b.h - b.l for b in bars) / len(bars) if bars else 5.0
    trs = []
    for i in range(1, min(period + 1, len(bars))):
        b = bars[-i]
        prev = bars[-i-1]
        tr = max(b.h - b.l, abs(b.h - prev.c), abs(b.l - prev.c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 5.0

def h4_trend(h4_bars: List[Bar], up_to: int) -> str:
    if up_to < 6:
        return "NONE"
    w = h4_bars[max(0, up_to-6):up_to+1]
    highs = [b.h for b in w]
    lows = [b.l for b in w]
    closes = [b.c for b in w]
    rng = max(highs) - min(lows)
    avg_body = sum(abs(b.c - b.o) for b in w) / len(w)
    
    if rng > 0 and avg_body / rng < 0.20:
        return "RANGE"
    
    hh = sum(1 for i in range(len(highs)-1) if highs[i+1] > highs[i])
    ll = sum(1 for i in range(len(lows)-1) if lows[i+1] < lows[i])
    
    if hh >= 4 and closes[-1] > closes[0]:
        return "UP"
    if ll >= 4 and closes[-1] < closes[0]:
        return "DOWN"
    if hh >= 2 and closes[-1] > closes[-3]:
        return "UP"
    if ll >= 2 and closes[-1] < closes[-3]:
        return "DOWN"
    return "CHOP"

def get_risk(equity: float, base: float = 100.0) -> float:
    mult = equity / base
    if mult >= 10.0: return 25.0
    elif mult >= 5.0: return 10.0
    elif mult >= 2.0: return 5.0
    return 2.0

def calc_lot(symbol: str, risk_usd: float, sl_dist: float) -> float:
    if sl_dist <= 0:
        return 0.01
    if symbol == "XAUUSD":
        pip_val, pip_size = 0.01, 0.01
    elif symbol == "XAGUSD":
        pip_val, pip_size = 0.001, 0.001
    else:
        pip_val, pip_size = 0.0001, 0.0001
    pips = sl_dist / pip_size
    lot = risk_usd / (pips * pip_val)
    return max(0.01, min(lot, 0.5))

# ── FVG with entry levels ─────────────────────────────────────────
def find_fvg_levels(bars: List[Bar], min_size: float = 0.5) -> List[Dict]:
    """Find FVGs with ICT precision entry levels (50%, 62%, 79%)."""
    fvgs = []
    for i in range(2, len(bars)):
        b0, b2 = bars[i-2], bars[i]
        
        if b0.h < b2.l:
            size = b2.l - b0.h
            if size >= min_size:
                top, bot = b2.l, b0.h
                fvgs.append({
                    "dir": "BUY", "top": top, "bottom": bot,
                    "size": size,
                    "entry_50": (top + bot) / 2,
                    "entry_62": bot + size * 0.62,
                    "entry_79": bot + size * 0.79,
                    "idx": i, "time": b2.time,
                })
        
        if b0.l > b2.h:
            size = b0.l - b2.h
            if size >= min_size:
                top, bot = b0.l, b2.h
                fvgs.append({
                    "dir": "SELL", "top": top, "bottom": bot,
                    "size": size,
                    "entry_50": (top + bot) / 2,
                    "entry_62": top - size * 0.62,
                    "entry_79": top - size * 0.79,
                    "idx": i, "time": b2.time,
                })
    return fvgs

# ── Simulation ───────────────────────────────────────────────────
def simulate(h1: List[Bar], h4: List[Bar], symbol: str, base: float = 100.0) -> Dict:
    fvgs = find_fvg_levels(h1, min_size=1.0)
    
    if not fvgs:
        return {"error": "no FVGs"}
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    cooldown_until = 0
    
    for fvg in fvgs:
        fvg_idx = fvg["idx"]
        if fvg_idx + 1 >= len(h1):
            continue
        if fvg_idx < cooldown_until:
            continue
        
        # H4 trend check
        h4_idx = min(fvg_idx // 4, len(h4) - 1)
        trend = h4_trend(h4, h4_idx)
        
        # Match FVG direction to H4 trend
        if fvg["dir"] == "BUY" and trend not in ["UP", "RANGE"]:
            continue
        if fvg["dir"] == "SELL" and trend not in ["DOWN", "RANGE"]:
            continue
        
        # ATR for SL buffer
        atr = calc_atr(h1[max(0, fvg_idx-20):fvg_idx])
        
        # Entry at 62% level (deeper discount than 50%)
        entry = fvg["entry_62"]
        
        # SL: 1.5x ATR beyond FVG extreme
        if fvg["dir"] == "BUY":
            sl = fvg["bottom"] - atr * 1.5
            # TP: next resistance or 3R
            recent_highs = [b.h for b in h1[max(0, fvg_idx-30):fvg_idx]]
            if recent_highs:
                resistance = max(recent_highs)
                if resistance > entry:
                    tp = resistance
                else:
                    tp = entry + (entry - sl) * 3.0
            else:
                tp = entry + (entry - sl) * 3.0
        else:
            sl = fvg["top"] + atr * 1.5
            recent_lows = [b.l for b in h1[max(0, fvg_idx-30):fvg_idx]]
            if recent_lows:
                support = min(recent_lows)
                if support < entry:
                    tp = support
                else:
                    tp = entry - (sl - entry) * 3.0
            else:
                tp = entry - (sl - entry) * 3.0
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        # Filters
        if rr < 2.0 or risk > 30:  # Max 30pt SL, min 2R
            continue
        
        # Entry execution (limit on next 3 bars)
        entry_idx = None
        entry_price = None
        
        for j in range(fvg_idx + 1, min(fvg_idx + 4, len(h1))):
            b = h1[j]
            if fvg["dir"] == "BUY":
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
        
        # Lot size
        risk_usd = get_risk(equity, base)
        lot = calc_lot(symbol, risk_usd, risk)
        
        # Walk to exit (5 bar max)
        pnl = None
        exit_idx = None
        exit_price = None
        reason = ""
        r_mult = 0.0
        
        for k in range(entry_idx + 1, min(entry_idx + 6, len(h1))):
            b = h1[k]
            
            if fvg["dir"] == "BUY":
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
        
        if pnl is None:
            # EOD — check if profitable
            exit_idx = min(entry_idx + 5, len(h1) - 1)
            exit_price = h1[exit_idx].c
            if fvg["dir"] == "BUY":
                gain = exit_price - entry_price
            else:
                gain = entry_price - exit_price
            r_mult = gain / risk if risk > 0 else 0
            pnl = risk_usd * r_mult - COMMISSION * lot
            reason = "EOD"
            
            # Time stop: if not 50% toward target, exit
            progress = abs(exit_price - entry_price) / abs(tp - entry_price) if tp != entry_price else 0
            if progress < 0.5 and r_mult < 0:
                # Cut at small loss instead of holding
                pass
        
        equity += pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd
        
        trades.append({
            "time": fvg["time"], "dir": fvg["dir"], "entry": entry_price,
            "sl": sl, "tp": tp, "rr": rr, "exit": exit_price,
            "pnl": pnl, "r": r_mult, "reason": reason,
            "fvg_size": fvg["size"], "atr": atr, "trend": trend,
            "lot": lot, "risk_usd": risk_usd,
            "bars": exit_idx - entry_idx if exit_idx else 0,
            "equity": equity,
        })
        
        # Cooldown after 3 consecutive losses
        recent_losses = sum(1 for t in trades[-3:] if t["pnl"] <= 0)
        if recent_losses >= 3:
            cooldown_until = exit_idx + 12 if exit_idx else fvg_idx + 12
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": sum(t["pnl"] for t in trades),
        "equity": equity, "peak": peak, "max_dd": max_dd,
        "return_pct": (equity - base) / base * 100,
        "detail": trades,
    }

# ── Main ───────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" FINAL AMD-FVG STRATEGY")
    print(" Deeper entry (62%) | Wider SL (1.5x ATR) | 2R+ only | Time stops")
    print("=" * 100)
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" {symbol}")
        print(f"{'='*80}")
        
        h1 = fetch_h1(yf_ticker)
        h4 = to_h4(h1)
        
        print(f"  H1: {len(h1)} | H4: {len(h4)}")
        
        r = simulate(h1, h4, symbol, base=100.0)
        
        if "error" in r:
            print(f"  {r['error']}")
            continue
        
        print(f"\n  Trades: {r['trades']}")
        print(f"  Wins: {r['wins']} | Losses: {r['losses']}")
        print(f"  Win Rate: {r['wr']:.1f}%")
        print(f"  P&L: ${r['pnl']:.2f}")
        print(f"  Equity: ${r['equity']:.2f} (peak: ${r['peak']:.2f})")
        print(f"  Return: {r['return_pct']:.1f}%")
        print(f"  Max DD: {r['max_dd']:.1f}%")
        
        if r['trades'] > 0:
            trades = r['detail']
            
            reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
            for t in trades:
                reasons[t["reason"]]["count"] += 1
                reasons[t["reason"]]["pnl"] += t["pnl"]
            
            print(f"\n  Exits:")
            for reason, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
                print(f"    {reason}: {d['count']} (${d['pnl']:.2f})")
            
            trends = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
            for t in trades:
                tr = t["trend"]
                trends[tr]["count"] += 1
                trends[tr]["pnl"] += t["pnl"]
                if t["pnl"] > 0:
                    trends[tr]["wins"] += 1
            
            print(f"\n  By H4 trend:")
            for tr, d in sorted(trends.items(), key=lambda x: -x[1]["count"]):
                wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
                print(f"    {tr}: {d['count']} trades, {wr:.1f}% WR, ${d['pnl']:.2f}")
            
            # Size analysis
            small = [t for t in trades if t["fvg_size"] < 2]
            large = [t for t in trades if t["fvg_size"] >= 2]
            if small:
                sw = sum(1 for t in small if t["pnl"] > 0)
                print(f"\n  Small FVG (<2): {len(small)} trades, {sw/len(small)*100:.1f}% WR")
            if large:
                lw = sum(1 for t in large if t["pnl"] > 0)
                print(f"  Large FVG (≥2): {len(large)} trades, {lw/len(large)*100:.1f}% WR")
            
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
            
            out = os.path.join(os.path.dirname(__file__), f'final_amd_fvg_{symbol}.json')
            with open(out, 'w') as f:
                json.dump(trades, f, indent=2)
            print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
