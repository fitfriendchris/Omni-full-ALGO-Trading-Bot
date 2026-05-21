#!/usr/bin/env python3
"""
proven_rigor.py — Original backtester logic with FULL reality checks

Uses the EXACT sweep + OB detection from backtester.py,
but with:
  - Limit order execution (no same-bar fills)
  - Commission + spread + slippage
  - Dollar-risk compounding
  - No look-ahead bias
  - 2 years yfinance H1 data
"""
import json, math, os, sys
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
COMMISSION = 7.0
SPREAD = 0.0002
SLIPPAGE = 0.0001

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

# ── Sweep + OB detection (from backtester.py) ─────────────────────
def detect_sweep_ob(bars: List[Bar], lookback: int = 12) -> List[Dict]:
    """Find sweep of recent high/low followed by OB formation."""
    if len(bars) < lookback + 3:
        return []
    
    setups = []
    
    for i in range(lookback, len(bars) - 2):
        window = bars[i-lookback:i]
        recent_high = max(b.h for b in window)
        recent_low = min(b.l for b in window)
        
        curr = bars[i]
        
        # Bullish sweep: price takes out recent low, then creates bullish OB
        if curr.l < recent_low * 0.999:
            # Check if next 2 bars form bullish OB
            b1, b2 = bars[i+1], bars[i+2]
            if b1.c > b1.o and b2.c > b2.o and b1.l < b2.l:
                # OB formed
                ob_low = min(b1.l, b2.l)
                ob_high = max(b1.h, b2.h)
                setups.append({
                    "dir": "BUY", "sweep_idx": i, "ob_low": ob_low, "ob_high": ob_high,
                    "sweep_low": curr.l, "time": curr.time,
                })
        
        # Bearish sweep
        if curr.h > recent_high * 1.001:
            b1, b2 = bars[i+1], bars[i+2]
            if b1.c < b1.o and b2.c < b2.o and b1.h > b2.h:
                ob_low = min(b1.l, b2.l)
                ob_high = max(b1.h, b2.h)
                setups.append({
                    "dir": "SELL", "sweep_idx": i, "ob_low": ob_low, "ob_high": ob_high,
                    "sweep_high": curr.h, "time": curr.time,
                })
    
    return setups

# ── Realistic simulation ──────────────────────────────────────────
def simulate_proven(h1: List[Bar], symbol: str, base: float = 100.0) -> Dict:
    setups = detect_sweep_ob(h1, lookback=12)
    
    if not setups:
        return {"error": "no setups"}
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    cooldown_until = 0
    
    for setup in setups:
        sweep_idx = setup["sweep_idx"]
        if sweep_idx + 3 >= len(h1):
            continue
        if sweep_idx < cooldown_until:
            continue
        
        # AMD wick extreme entry (from ict_precision.py)
        spread = abs(h1[sweep_idx].c - h1[sweep_idx].o) * SPREAD
        
        if setup["dir"] == "BUY":
            entry = setup["ob_low"] + spread + SLIPPAGE
            sl = setup["sweep_low"] - 2.0
            tp = setup["ob_high"] + (setup["ob_high"] - setup["ob_low"]) * 2
        else:
            entry = setup["ob_high"] - spread - SLIPPAGE
            sl = setup["sweep_high"] + 2.0
            tp = setup["ob_low"] - (setup["ob_high"] - setup["ob_low"]) * 2
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        if rr < 1.0 or risk <= 0:
            continue
        
        # Entry execution (limit order on subsequent bars)
        entry_idx = None
        entry_price = None
        
        for j in range(sweep_idx + 3, min(sweep_idx + 6, len(h1))):
            b = h1[j]
            if setup["dir"] == "BUY":
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
        
        # Walk to exit (max 15 bars)
        pnl = None
        exit_idx = None
        exit_price = None
        reason = ""
        
        for k in range(entry_idx + 1, min(entry_idx + 16, len(h1))):
            b = h1[k]
            
            if setup["dir"] == "BUY":
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
            exit_idx = min(entry_idx + 15, len(h1) - 1)
            exit_price = h1[exit_idx].c
            if setup["dir"] == "BUY":
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
            "time": setup["time"], "dir": setup["dir"], "entry": entry_price,
            "sl": sl, "tp": tp, "rr": rr, "exit": exit_price,
            "pnl": pnl, "reason": reason,
            "lot": lot, "risk_usd": risk_usd, "equity": equity,
        })
        
        # Cooldown after 3 losses
        recent_losses = sum(1 for t in trades[-3:] if t["pnl"] <= 0)
        if recent_losses >= 3:
            cooldown_until = exit_idx + 12 if exit_idx else sweep_idx + 12
    
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
    print(" PROVEN LOGIC — RIGOROUS BACKTEST")
    print(" Sweep + OB | Limit orders | Commission + spread + slippage | Compounding")
    print("=" * 100)
    print()
    
    results = {}
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" {symbol}")
        print(f"{'='*80}")
        
        h1 = fetch_h1(yf_ticker)
        print(f"  H1: {len(h1)} bars")
        
        r = simulate_proven(h1, symbol, 100.0)
        
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
            reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
            for t in r['detail']:
                reasons[t["reason"]]["count"] += 1
                reasons[t["reason"]]["pnl"] += t["pnl"]
            
            print(f"\n  Exits:")
            for reason, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
                print(f"    {reason}: {d['count']} (${d['pnl']:.2f})")
            
            # Monthly
            monthly = defaultdict(lambda: {"pnl": 0, "trades": 0})
            for t in r['detail']:
                m = t["time"][:7]
                monthly[m]["pnl"] += t["pnl"]
                monthly[m]["trades"] += 1
            
            print(f"\n  Monthly:")
            for m in sorted(monthly.keys()):
                d = monthly[m]
                print(f"    {m}: {d['trades']} trades, ${d['pnl']:.2f}")
            
            out = os.path.join(os.path.dirname(__file__), f'proven_rigor_{symbol}.json')
            with open(out, 'w') as f:
                json.dump(r['detail'], f, indent=2)
            print(f"\n  Saved: {out}")
        
        results[symbol] = r
    
    # Save summary
    out = os.path.join(os.path.dirname(__file__), 'proven_rigor_results.json')
    with open(out, 'w') as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "detail"} for k, v in results.items()}, f, indent=2)
    print(f"\n  Summary saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
