#!/usr/bin/env python3
"""
real_amd_fvg.py — REALISTIC BOS + FVG Strategy

Proper execution timing:
  - FVG detected at close of bar[i]
  - Limit order placed for bar[i+1] onward
  - Entry only if price retests FVG zone AFTER detection
  - SL/TP resolved on subsequent bars (never same bar)

Compounding with 1:1000 leverage.
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

# Risk compounding tiers
RISK_TIERS = [(1.0, 2.0), (2.0, 5.0), (5.0, 10.0), (10.0, 25.0)]

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
    if not h1:
        return []
    buckets = defaultdict(list)
    for b in h1:
        dt = datetime.strptime(b.time, "%Y-%m-%d %H:%M:%S")
        bucket = dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0)
        buckets[str(bucket)].append(b)
    return [Bar(time=k, o=v[0].o, h=max(b.h for b in v), l=min(b.l for b in v),
                c=v[-1].c, v=sum(b.v for b in v)) for k, v in sorted(buckets.items())]

# ── FVG detection ─────────────────────────────────────────────────
def find_fvg(bars: List[Bar], min_size: float = 0.3) -> List[Dict]:
    """Find all Fair Value Gaps. Returns list with confirmed bar index."""
    fvgs = []
    for i in range(2, len(bars)):
        b0, b2 = bars[i-2], bars[i]
        
        # Bullish: b0.high < b2.low
        if b0.h < b2.l:
            size = b2.l - b0.h
            if size >= min_size:
                fvgs.append({
                    "dir": "BUY", "top": b2.l, "bottom": b0.h,
                    "mid": (b2.l + b0.h) / 2, "idx": i, "time": b2.time,
                    "size": size,
                })
        
        # Bearish: b0.low > b2.high
        if b0.l > b2.h:
            size = b0.l - b2.h
            if size >= min_size:
                fvgs.append({
                    "dir": "SELL", "top": b0.l, "bottom": b2.h,
                    "mid": (b0.l + b2.h) / 2, "idx": i, "time": b2.time,
                    "size": size,
                })
    return fvgs

# ── Swing points ────────────────────────────────────────────────────
def find_swings(bars: List[Bar], window: int = 3) -> Dict:
    highs = []
    lows = []
    for i in range(window, len(bars) - window):
        if bars[i].h > max(bars[j].h for j in range(i-window, i)) and \
           bars[i].h > max(bars[j].h for j in range(i+1, i+window+1)):
            highs.append((i, bars[i].h))
        if bars[i].l < min(bars[j].l for j in range(i-window, i)) and \
           bars[i].l < min(bars[j].l for j in range(i+1, i+window+1)):
            lows.append((i, bars[i].l))
    return {"highs": highs, "lows": lows}

# ── H4 AMD phase ──────────────────────────────────────────────────
def h4_phase(h4_bars: List[Bar], up_to_idx: int) -> str:
    if up_to_idx < 6:
        return "UNKNOWN"
    w = h4_bars[max(0, up_to_idx-6):up_to_idx+1]
    highs = [b.h for b in w]
    lows = [b.l for b in w]
    closes = [b.c for b in w]
    rng = max(highs) - min(lows)
    avg_body = sum(abs(b.c - b.o) for b in w) / len(w)
    
    hh = sum(1 for i in range(len(highs)-1) if highs[i+1] > highs[i])
    ll = sum(1 for i in range(len(lows)-1) if lows[i+1] < lows[i])
    
    if rng > 0 and avg_body / rng < 0.25:
        return "ACCUMULATION"
    elif hh >= 4:
        return "DIST_UP"
    elif ll >= 4:
        return "DIST_DOWN"
    elif hh >= 2 and closes[-1] > closes[0]:
        return "MANIP_UP"
    elif ll >= 2 and closes[-1] < closes[0]:
        return "MANIP_DOWN"
    return "CHOP"

# ── Realistic simulation ──────────────────────────────────────────
def simulate_realistic(h1: List[Bar], h4: List[Bar], symbol: str, base: float = 100.0) -> Dict:
    """
    Realistic execution:
    1. FVG detected at close of bar[i]
    2. Limit order placed at FVG mid for bar[i+1] or later
    3. Entry fills if price hits zone within 3 bars
    4. SL/TP checked from bar AFTER entry (never same bar)
    """
    fvgs = find_fvg(h1, min_size=0.5)
    swings = find_swings(h1)
    
    if not fvgs:
        return {"error": "no FVGs"}
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    
    for fvg in fvgs:
        fvg_idx = fvg["idx"]
        if fvg_idx + 1 >= len(h1):
            continue
        
        # Skip if already in a trade
        if trades and trades[-1]["exit_idx"] > fvg_idx:
            continue
        
        # ── Look for entry on subsequent bars (NOT same bar) ──
        entry_idx = None
        entry_price = None
        
        for j in range(fvg_idx + 1, min(fvg_idx + 5, len(h1))):
            b = h1[j]
            if fvg["dir"] == "BUY":
                # Price must come down into FVG zone
                if b.l <= fvg["top"] and b.h >= fvg["bottom"]:
                    # Limit order at mid — fills if price reaches it
                    if b.l <= fvg["mid"]:
                        entry_idx = j
                        entry_price = fvg["mid"]
                        break
            else:  # SELL
                if b.h >= fvg["bottom"] and b.l <= fvg["top"]:
                    if b.h >= fvg["mid"]:
                        entry_idx = j
                        entry_price = fvg["mid"]
                        break
        
        if entry_idx is None:
            continue
        
        # ── Calculate SL and TP ──
        if fvg["dir"] == "BUY":
            sl = fvg["bottom"] * 0.999
            # TP: use recent swing high or 2R minimum
            recent_highs = [s[1] for s in swings["highs"] if s[0] < entry_idx and s[0] > entry_idx - 20]
            if recent_highs:
                tp = max(recent_highs) * 1.001
            else:
                tp = entry_price + (entry_price - sl) * 2.0
        else:
            sl = fvg["top"] * 1.001
            recent_lows = [s[1] for s in swings["lows"] if s[0] < entry_idx and s[0] > entry_idx - 20]
            if recent_lows:
                tp = min(recent_lows) * 0.999
            else:
                tp = entry_price - (sl - entry_price) * 2.0
        
        risk = abs(entry_price - sl)
        reward = abs(tp - entry_price)
        rr = reward / risk if risk > 0 else 0
        
        if rr < 1.0 or risk <= 0:
            continue
        
        # ── H4 context ──
        h4_idx = min(entry_idx // 4, len(h4) - 1)
        phase = h4_phase(h4, h4_idx)
        
        # ── Lot sizing (compounding) ──
        mult = equity / base
        if mult >= 10:
            risk_usd = 25.0
        elif mult >= 5:
            risk_usd = 10.0
        elif mult >= 2:
            risk_usd = 5.0
        else:
            risk_usd = 2.0
        
        if symbol == "XAUUSD":
            pip_val = 0.01
            pip_size = 0.01
        elif symbol == "XAGUSD":
            pip_val = 0.001
            pip_size = 0.001
        else:
            pip_val = 0.0001
            pip_size = 0.0001
        
        pips = risk / pip_size
        lot = risk_usd / (pips * pip_val)
        lot = max(0.01, min(lot, 0.5))
        
        # ── Walk to exit (from bar AFTER entry) ──
        pnl = None
        exit_idx = None
        exit_price = None
        reason = ""
        r_mult = 0.0
        
        for k in range(entry_idx + 1, min(entry_idx + 25, len(h1))):
            b = h1[k]
            
            if fvg["dir"] == "BUY":
                # Check SL first (worst case)
                if b.l <= sl:
                    exit_idx = k
                    exit_price = sl
                    r_mult = -1.0
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                # Then TP
                if b.h >= tp:
                    exit_idx = k
                    exit_price = tp
                    r_mult = rr
                    pnl = risk_usd * rr - COMMISSION * lot
                    reason = "TP"
                    break
            else:  # SELL
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
            # EOD close
            exit_idx = len(h1) - 1
            exit_price = h1[-1].c
            if fvg["dir"] == "BUY":
                gain = exit_price - entry_price
            else:
                gain = entry_price - exit_price
            r_mult = gain / risk if risk > 0 else 0
            pnl = risk_usd * r_mult - COMMISSION * lot
            reason = "EOD"
        
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
            "fvg_size": fvg["size"], "phase": phase,
            "lot": lot, "risk_usd": risk_usd,
            "entry_idx": entry_idx, "exit_idx": exit_idx,
            "equity": equity,
        })
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": sum(t["pnl"] for t in trades),
        "equity": equity,
        "peak": peak,
        "max_dd": max_dd,
        "return_pct": (equity - base) / base * 100,
        "trades_detail": trades,
    }

# ── Main ───────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" REALISTIC AMD-FVG STRATEGY")
    print(" Proper execution timing (no same-bar resolution)")
    print("=" * 100)
    print()
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" {symbol}")
        print(f"{'='*80}")
        
        h1 = fetch_h1(yf_ticker)
        h4 = to_h4(h1)
        
        print(f"  H1: {len(h1)} bars | H4: {len(h4)} bars")
        
        result = simulate_realistic(h1, h4, symbol, base=100.0)
        
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
            
            # By exit reason
            reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
            for t in trades:
                reasons[t["reason"]]["count"] += 1
                reasons[t["reason"]]["pnl"] += t["pnl"]
            
            print(f"\n  Exits:")
            for r, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
                print(f"    {r}: {d['count']} (${d['pnl']:.2f})")
            
            # By H4 phase
            phases = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
            for t in trades:
                p = t["phase"]
                phases[p]["count"] += 1
                phases[p]["pnl"] += t["pnl"]
                if t["pnl"] > 0:
                    phases[p]["wins"] += 1
            
            print(f"\n  By H4 phase:")
            for p, d in sorted(phases.items(), key=lambda x: -x[1]["count"]):
                wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
                print(f"    {p}: {d['count']} trades, {wr:.1f}% WR, ${d['pnl']:.2f}")
            
            # By FVG size
            small = [t for t in trades if t["fvg_size"] < 2.0]
            large = [t for t in trades if t["fvg_size"] >= 2.0]
            if small:
                sw = sum(1 for t in small if t["pnl"] > 0)
                print(f"\n  Small FVG (<2): {len(small)} trades, {sw/len(small)*100:.1f}% WR")
            if large:
                lw = sum(1 for t in large if t["pnl"] > 0)
                print(f"  Large FVG (≥2): {len(large)} trades, {lw/len(large)*100:.1f}% WR")
            
            # Bars held
            tp_bars = [t["exit_idx"] - t["entry_idx"] for t in trades if t["reason"] == "TP"]
            sl_bars = [t["exit_idx"] - t["entry_idx"] for t in trades if t["reason"] == "SL"]
            if tp_bars:
                print(f"\n  Avg bars to TP: {sum(tp_bars)/len(tp_bars):.1f}")
            if sl_bars:
                print(f"  Avg bars to SL: {sum(sl_bars)/len(sl_bars):.1f}")
            
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
            out = os.path.join(os.path.dirname(__file__), f'real_amd_fvg_{symbol}.json')
            with open(out, 'w') as f:
                json.dump(trades, f, indent=2)
            print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
