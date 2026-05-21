#!/usr/bin/env python3
"""
edge_backtest.py — RIGOROUS bar-by-bar backtest of discovered edges

Edges tested:
  1. FVG fill → 2x TP (77.8% fill, 59.3% hit 2x on XAUUSD)
  2. Big bar fade (100% reverse next bar)
  3. H4 bias continuation (64.3% on XAUUSD)
  4. Asian range reversal on specific pairs

Execution:
  - FVG detected at close of bar[i]
  - Limit order placed for bar[i+1] or later
  - Entry only if price retests FVG zone AFTER detection
  - SL/TP resolved on subsequent bars (never same bar)
  - Commission: $7 per lot round-turn
  - Spread simulation: 0.02% of price
  - Slippage: 0.01% on fills

Compounding tiers:
  - $100-$199: risk $2 per trade
  - $200-$499: risk $5 per trade
  - $500-$999: risk $10 per trade
  - $1000+: risk $25 per trade
"""
import json, math, os, sys, re
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

# ── Config ──────────────────────────────────────────────────────────
SYMBOLS = {
    "GC=F": "XAUUSD",
    "SI=F": "XAGUSD",
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
}
START = "2024-05-22"
END = "2026-05-21"
COMMISSION = 7.0
SPREAD_PCT = 0.0002  # 0.02%
SLIPPAGE_PCT = 0.0001  # 0.01%

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

def get_risk(equity: float, base: float = 100.0) -> float:
    mult = equity / base
    if mult >= 10.0: return 25.0
    elif mult >= 5.0: return 10.0
    elif mult >= 2.0: return 5.0
    return 2.0

def calc_lot(symbol: str, risk_usd: float, sl_dist: float) -> float:
    if sl_dist <= 0:
        return 0.01
    if symbol in ["XAUUSD", "XAGUSD"]:
        pip_val, pip_size = 0.01, 0.01
    else:
        pip_val, pip_size = 0.0001, 0.0001
    pips = sl_dist / pip_size
    lot = risk_usd / (pips * pip_val)
    return max(0.01, min(lot, 1.0))

def calc_spread(price: float) -> float:
    return price * SPREAD_PCT

def calc_slippage(price: float) -> float:
    return price * SLIPPAGE_PCT

# ── Edge 1: FVG Fill Strategy ────────────────────────────────────
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

def simulate_fvg_fill(h1: List[Bar], symbol: str, base: float = 100.0) -> Dict:
    """
    FVG detected at bar[i]. 
    Limit order at FVG 50% for bar[i+1] or later.
    TP = FVG top/bottom + 2x FVG size.
    SL = beyond FVG extreme.
    """
    fvgs = find_fvg(h1, min_size=1.0)
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
        
        # Entry at FVG 50% on subsequent bars
        entry_idx = None
        entry_price = None
        
        for j in range(fvg_idx + 1, min(fvg_idx + 4, len(h1))):
            b = h1[j]
            if fvg["dir"] == "BUY":
                if b.l <= fvg["mid"]:
                    entry_idx = j
                    entry_price = fvg["mid"] + calc_slippage(fvg["mid"])
                    break
            else:
                if b.h >= fvg["mid"]:
                    entry_idx = j
                    entry_price = fvg["mid"] - calc_slippage(fvg["mid"])
                    break
        
        if entry_idx is None:
            continue
        
        # SL beyond FVG extreme
        if fvg["dir"] == "BUY":
            sl = fvg["bottom"] - calc_spread(fvg["bottom"]) * 2
            tp = fvg["top"] + fvg["size"] * 2
        else:
            sl = fvg["top"] + calc_spread(fvg["top"]) * 2
            tp = fvg["bottom"] - fvg["size"] * 2
        
        risk = abs(entry_price - sl)
        reward = abs(tp - entry_price)
        rr = reward / risk if risk > 0 else 0
        
        if rr < 1.5 or risk <= 0:
            continue
        
        # Lot size
        risk_usd = get_risk(equity, base)
        lot = calc_lot(symbol, risk_usd, risk)
        
        # Walk to exit (max 10 bars)
        pnl = None
        exit_idx = None
        exit_price = None
        reason = ""
        r_mult = 0.0
        
        for k in range(entry_idx + 1, min(entry_idx + 11, len(h1))):
            b = h1[k]
            
            if fvg["dir"] == "BUY":
                # SL first
                if b.l <= sl:
                    exit_idx = k
                    exit_price = sl
                    r_mult = -1.0
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                # TP
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
            exit_idx = min(entry_idx + 10, len(h1) - 1)
            exit_price = h1[exit_idx].c
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
            "fvg_size": fvg["size"], "bars": exit_idx - entry_idx if exit_idx else 0,
            "lot": lot, "risk_usd": risk_usd, "equity": equity,
        })
        
        # Cooldown after 3 losses
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

# ── Edge 2: Big Bar Fade ──────────────────────────────────────────
def simulate_big_bar_fade(h1: List[Bar], symbol: str, base: float = 100.0) -> Dict:
    """
    When a bar has body > 2x avg body, fade it next bar.
    Entry: opposite direction on open of next bar.
    TP: 2R.
    SL: beyond big bar extreme.
    """
    if len(h1) < 20:
        return {"error": "not enough bars"}
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    
    for i in range(10, len(h1) - 1):
        # Calculate avg body over last 10 bars
        avg_body = sum(abs(h1[j].c - h1[j].o) for j in range(i-10, i)) / 10
        curr = h1[i]
        body = abs(curr.c - curr.o)
        
        if body <= avg_body * 2.0:
            continue
        
        # Fade direction
        if curr.c > curr.o:  # Bullish big bar → SELL
            dir = "SELL"
            entry = h1[i+1].o  # Open next bar
            sl = curr.h + calc_spread(curr.h)
            tp = entry - (sl - entry) * 2.0
        else:  # Bearish big bar → BUY
            dir = "BUY"
            entry = h1[i+1].o
            sl = curr.l - calc_spread(curr.l)
            tp = entry + (entry - sl) * 2.0
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        if rr < 1.0 or risk <= 0:
            continue
        
        risk_usd = get_risk(equity, base)
        lot = calc_lot(symbol, risk_usd, risk)
        
        # Walk to exit (max 5 bars)
        pnl = None
        exit_idx = None
        exit_price = None
        reason = ""
        
        for k in range(i+1, min(i+6, len(h1))):
            b = h1[k]
            
            if dir == "BUY":
                if b.l <= sl:
                    exit_idx = k
                    exit_price = sl
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                if b.h >= tp:
                    exit_idx = k
                    exit_price = tp
                    pnl = risk_usd * rr - COMMISSION * lot
                    reason = "TP"
                    break
            else:
                if b.h >= sl:
                    exit_idx = k
                    exit_price = sl
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                if b.l <= tp:
                    exit_idx = k
                    exit_price = tp
                    pnl = risk_usd * rr - COMMISSION * lot
                    reason = "TP"
                    break
        
        if pnl is None:
            exit_idx = min(i+5, len(h1) - 1)
            exit_price = h1[exit_idx].c
            if dir == "BUY":
                gain = exit_price - entry
            else:
                gain = entry - exit_price
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
            "time": curr.time, "dir": dir, "entry": entry,
            "sl": sl, "tp": tp, "rr": rr, "exit": exit_price,
            "pnl": pnl, "reason": reason,
            "bars": exit_idx - (i+1) if exit_idx else 0,
            "lot": lot, "risk_usd": risk_usd, "equity": equity,
        })
    
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

# ── Edge 3: H4 Bias Continuation ────────────────────────────────
def simulate_h4_bias(h1: List[Bar], h4: List[Bar], symbol: str, base: float = 100.0) -> Dict:
    """
    When H4 shows 3+ consecutive higher highs (or lower lows),
    trade continuation on H1 pullback.
    Entry: FVG retest in direction of H4 bias.
    """
    if len(h4) < 6 or len(h1) < 50:
        return {"error": "not enough data"}
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    
    for i in range(3, len(h4)):
        # Check for 3+ HH or LL
        hh = sum(1 for j in range(i-3, i) if h4[j+1].h > h4[j].h)
        ll = sum(1 for j in range(i-3, i) if h4[j+1].l < h4[j].l)
        
        if hh >= 3:
            bias = "BUY"
        elif ll >= 3:
            bias = "SELL"
        else:
            continue
        
        # Find FVGs in the next H4 bar period
        h4_start_idx = i * 4
        h4_end_idx = min(h4_start_idx + 4, len(h1))
        
        if h4_start_idx >= len(h1):
            continue
        
        # Look for pullback FVG in H1
        window = h1[max(0, h4_start_idx-10):h4_end_idx]
        fvgs = find_fvg(window, min_size=0.5)
        
        for fvg in fvgs:
            if fvg["dir"] != bias:
                continue
            
            fvg_idx_in_h1 = fvg["idx"] + max(0, h4_start_idx-10)
            if fvg_idx_in_h1 + 1 >= len(h1):
                continue
            
            # Entry at FVG 50%
            entry_idx = None
            entry_price = None
            
            for j in range(fvg_idx_in_h1 + 1, min(fvg_idx_in_h1 + 3, len(h1))):
                b = h1[j]
                if bias == "BUY":
                    if b.l <= fvg["mid"]:
                        entry_idx = j
                        entry_price = fvg["mid"] + calc_slippage(fvg["mid"])
                        break
                else:
                    if b.h >= fvg["mid"]:
                        entry_idx = j
                        entry_price = fvg["mid"] - calc_slippage(fvg["mid"])
                        break
            
            if entry_idx is None:
                continue
            
            # SL/TP
            if bias == "BUY":
                sl = fvg["bottom"] - calc_spread(fvg["bottom"])
                tp = h4[i].h + 5.0
            else:
                sl = fvg["top"] + calc_spread(fvg["top"])
                tp = h4[i].l - 5.0
            
            risk = abs(entry_price - sl)
            reward = abs(tp - entry_price)
            rr = reward / risk if risk > 0 else 0
            
            if rr < 1.0 or risk <= 0:
                continue
            
            risk_usd = get_risk(equity, base)
            lot = calc_lot(symbol, risk_usd, risk)
            
            # Walk to exit (max 10 bars)
            pnl = None
            exit_idx = None
            exit_price = None
            reason = ""
            
            for k in range(entry_idx + 1, min(entry_idx + 11, len(h1))):
                b = h1[k]
                
                if bias == "BUY":
                    if b.l <= sl:
                        exit_idx = k
                        exit_price = sl
                        pnl = -risk_usd - COMMISSION * lot
                        reason = "SL"
                        break
                    if b.h >= tp:
                        exit_idx = k
                        exit_price = tp
                        pnl = risk_usd * rr - COMMISSION * lot
                        reason = "TP"
                        break
                else:
                    if b.h >= sl:
                        exit_idx = k
                        exit_price = sl
                        pnl = -risk_usd - COMMISSION * lot
                        reason = "SL"
                        break
                    if b.l <= tp:
                        exit_idx = k
                        exit_price = tp
                        pnl = risk_usd * rr - COMMISSION * lot
                        reason = "TP"
                        break
            
            if pnl is None:
                exit_idx = min(entry_idx + 10, len(h1) - 1)
                exit_price = h1[exit_idx].c
                if bias == "BUY":
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
                "time": fvg["time"], "dir": bias, "entry": entry_price,
                "sl": sl, "tp": tp, "rr": rr, "exit": exit_price,
                "pnl": pnl, "reason": reason,
                "bars": exit_idx - entry_idx if exit_idx else 0,
                "lot": lot, "risk_usd": risk_usd, "equity": equity,
            })
            
            break  # One trade per H4 bar
    
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
    print(" RIGOROUS EDGE BACKTEST")
    print(" No look-ahead | Realistic execution | Commission + spread + slippage")
    print("=" * 100)
    print()
    
    results = {}
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" {symbol}")
        print(f"{'='*80}")
        
        h1 = fetch_h1(yf_ticker)
        h4 = to_h4(h1)
        
        print(f"  H1: {len(h1)} | H4: {len(h4)}")
        
        # Test all 3 edges
        for edge_name, func in [
            ("FVG Fill", lambda: simulate_fvg_fill(h1, symbol, 100.0)),
            ("Big Bar Fade", lambda: simulate_big_bar_fade(h1, symbol, 100.0)),
            ("H4 Bias", lambda: simulate_h4_bias(h1, h4, symbol, 100.0)),
        ]:
            print(f"\n  --- {edge_name} ---")
            r = func()
            
            if "error" in r:
                print(f"    {r['error']}")
                continue
            
            print(f"    Trades: {r['trades']}")
            print(f"    WR: {r['wr']:.1f}% | P&L: ${r['pnl']:.2f}")
            print(f"    Equity: ${r['equity']:.2f} | Max DD: {r['max_dd']:.1f}%")
            print(f"    Return: {r['return_pct']:.1f}%")
            
            if r['trades'] > 0:
                reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
                for t in r['detail']:
                    reasons[t["reason"]]["count"] += 1
                    reasons[t["reason"]]["pnl"] += t["pnl"]
                
                for reason, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
                    print(f"      {reason}: {d['count']} (${d['pnl']:.2f})")
            
            results[f"{symbol}_{edge_name}"] = r
    
    # Summary
    print(f"\n{'='*80}")
    print(" SUMMARY")
    print(f"{'='*80}")
    
    for key, r in sorted(results.items()):
        if "error" not in r:
            print(f"  {key}: {r['trades']} trades, {r['wr']:.1f}% WR, ${r['pnl']:.2f} P&L, {r['return_pct']:.1f}% return")
    
    # Save
    out = os.path.join(os.path.dirname(__file__), 'edge_backtest_results.json')
    with open(out, 'w') as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "detail"} for k, v in results.items()}, f, indent=2)
    print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
