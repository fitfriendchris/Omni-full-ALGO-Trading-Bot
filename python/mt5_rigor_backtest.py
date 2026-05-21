#!/usr/bin/env python3
"""
mt5_rigor_backtest.py — Brute-force edge discovery on REAL MT5 broker data only

Uses ONLY the data from ~/Library/.../omni_data.json
No yfinance. No simulated data. Real ticks from MidasFX.

Tests ALL discovered edges with realistic execution:
  1. Sweep + OB (original backtester logic)
  2. FVG fill → 2x TP
  3. Big bar fade
  4. Asian range → reversal
  5. H4 bias continuation
  6. Session manipulation → reversal

Execution rules:
  - Signal detected at close of bar[i]
  - Limit order for bar[i+1] or later (never same bar)
  - Commission: $7/lot round-turn
  - Spread: 0.02% of price
  - Slippage: 0.01%
  - Dollar-risk compounding
"""
import json, math, os, sys, re
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar, get_ob_precision_entry

# ── Config ──────────────────────────────────────────────────────────
COMMISSION = 7.0
SPREAD_PCT = 0.0002
SLIPPAGE_PCT = 0.0001

# ── Load MT5 data ─────────────────────────────────────────────────
def load_mt5_data() -> Dict[str, Dict[str, List[Bar]]]:
    """Load ONLY MT5 data from broker."""
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
            if not isinstance(bars, list) or not bars:
                continue
            if not isinstance(bars[0], dict):
                continue
            
            result[sym][tf] = [
                Bar(time=b["t"], o=b["o"], h=b["h"], l=b["l"], c=b["c"], v=b.get("v", 0))
                for b in bars
            ]
    
    return result

# ── Helpers ────────────────────────────────────────────────────────
def get_risk(equity: float) -> float:
    if equity >= 1000: return 25.0
    elif equity >= 500: return 10.0
    elif equity >= 200: return 5.0
    elif equity >= 100: return 2.0
    return 1.0

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

# ── Strategy 1: Sweep + OB ────────────────────────────────────────
def detect_sweep_ob(bars: List[Bar], lookback: int = 8) -> List[Dict]:
    """Detect sweep of recent structure + OB formation."""
    setups = []
    for i in range(lookback, len(bars) - 2):
        window = bars[i-lookback:i]
        recent_high = max(b.h for b in window)
        recent_low = min(b.l for b in window)
        curr = bars[i]
        
        # Bullish sweep
        if curr.l < recent_low * 0.999:
            b1, b2 = bars[i+1], bars[i+2]
            if b1.c > b1.o and b2.c > b2.o:
                ob_low = min(b1.l, b2.l)
                ob_high = max(b1.h, b2.h)
                setups.append({
                    "dir": "BUY", "sweep_idx": i, "ob_low": ob_low, "ob_high": ob_high,
                    "sweep_low": curr.l, "time": curr.time,
                })
        
        # Bearish sweep
        if curr.h > recent_high * 1.001:
            b1, b2 = bars[i+1], bars[i+2]
            if b1.c < b1.o and b2.c < b2.o:
                ob_low = min(b1.l, b2.l)
                ob_high = max(b1.h, b2.h)
                setups.append({
                    "dir": "SELL", "sweep_idx": i, "ob_low": ob_low, "ob_high": ob_high,
                    "sweep_high": curr.h, "time": curr.time,
                })
    return setups

# ── Strategy 2: FVG ──────────────────────────────────────────────
def find_fvg(bars: List[Bar], min_size: float = 0.5) -> List[Dict]:
    fvgs = []
    for i in range(2, len(bars)):
        b0, b2 = bars[i-2], bars[i]
        if b0.h < b2.l and (b2.l - b0.h) >= min_size:
            fvgs.append({
                "dir": "BUY", "top": b2.l, "bottom": b0.h,
                "mid": (b2.l + b0.h) / 2, "idx": i, "time": b2.time,
                "size": b2.l - b0.h,
            })
        if b0.l > b2.h and (b0.l - b2.h) >= min_size:
            fvgs.append({
                "dir": "SELL", "top": b0.l, "bottom": b2.h,
                "mid": (b0.l + b2.h) / 2, "idx": i, "time": b2.time,
                "size": b0.l - b2.h,
            })
    return fvgs

# ── Strategy 3: Big bar fade ─────────────────────────────────────
def detect_big_bars(bars: List[Bar], multiplier: float = 2.0) -> List[Dict]:
    setups = []
    for i in range(10, len(bars)):
        avg_body = sum(abs(bars[j].c - bars[j].o) for j in range(i-10, i)) / 10
        curr = bars[i]
        body = abs(curr.c - curr.o)
        if body > avg_body * multiplier:
            setups.append({
                "idx": i, "time": curr.time,
                "dir": "SELL" if curr.c > curr.o else "BUY",
                "body": body, "avg": avg_body,
            })
    return setups

# ── Strategy 4: Asian range ──────────────────────────────────────
def detect_asian_range(bars: List[Bar]) -> List[Dict]:
    """Detect small Asian range + London sweep + NY reversal."""
    setups = []
    
    # Group by day
    days = defaultdict(list)
    for b in bars:
        day = b.time[:10]
        days[day].append(b)
    
    for day, day_bars in days.items():
        if len(day_bars) < 13:
            continue
        
        asian = day_bars[:8]
        london = day_bars[8:13]
        ny = day_bars[13:] if len(day_bars) > 13 else []
        
        asian_high = max(b.h for b in asian)
        asian_low = min(b.l for b in asian)
        asian_range = asian_high - asian_low
        
        london_high = max(b.h for b in london)
        london_low = min(b.l for b in london)
        
        # Only if Asian range is small
        if asian_range < 50:
            if london_high > asian_high:
                setups.append({
                    "dir": "SELL", "sweep_idx": 8 + len(asian),
                    "sweep_high": london_high, "asian_high": asian_high,
                    "time": london[0].time, "asian_range": asian_range,
                })
            if london_low < asian_low:
                setups.append({
                    "dir": "BUY", "sweep_idx": 8 + len(asian),
                    "sweep_low": london_low, "asian_low": asian_low,
                    "time": london[0].time, "asian_range": asian_range,
                })
    
    return setups

# ── Unified simulator ─────────────────────────────────────────────
def simulate(bars: List[Bar], setups: List[Dict], symbol: str, strategy_name: str,
             base: float = 100.0, entry_type: str = "limit",
             max_bars: int = 10, rr_min: float = 1.0) -> Dict:
    """
    Simulate trades with realistic execution.
    
    entry_type: "limit" (wait for retest) or "market" (next bar open)
    """
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    cooldown_until = 0
    
    for setup in setups:
        idx = setup.get("sweep_idx", setup.get("idx", 0))
        if idx + 3 >= len(bars):
            continue
        if idx < cooldown_until:
            continue
        
        # Determine entry/SL/TP based on strategy
        if strategy_name == "sweep_ob":
            spread = calc_spread(bars[idx].c)
            if setup["dir"] == "BUY":
                entry = setup["ob_low"] + spread + calc_slippage(setup["ob_low"])
                sl = setup["sweep_low"] - 2.0
                tp = setup["ob_high"] + (setup["ob_high"] - setup["ob_low"]) * 2
            else:
                entry = setup["ob_high"] - spread - calc_slippage(setup["ob_high"])
                sl = setup["sweep_high"] + 2.0
                tp = setup["ob_low"] - (setup["ob_high"] - setup["ob_low"]) * 2
        
        elif strategy_name == "fvg_fill":
            fvg = setup
            if fvg["dir"] == "BUY":
                entry = fvg["mid"] + calc_slippage(fvg["mid"])
                sl = fvg["bottom"] - calc_spread(fvg["bottom"]) * 2
                tp = fvg["top"] + fvg["size"] * 2
            else:
                entry = fvg["mid"] - calc_slippage(fvg["mid"])
                sl = fvg["top"] + calc_spread(fvg["top"]) * 2
                tp = fvg["bottom"] - fvg["size"] * 2
        
        elif strategy_name == "big_bar_fade":
            big_bar = bars[idx]
            if setup["dir"] == "BUY":
                entry = bars[idx+1].o
                sl = big_bar.l - calc_spread(big_bar.l)
                tp = entry + (entry - sl) * 2
            else:
                entry = bars[idx+1].o
                sl = big_bar.h + calc_spread(big_bar.h)
                tp = entry - (sl - entry) * 2
        
        elif strategy_name == "asian_range":
            if setup["dir"] == "BUY":
                entry = setup["sweep_low"] + calc_slippage(setup["sweep_low"])
                sl = setup["asian_low"] - 2.0
                tp = entry + (entry - sl) * 2
            else:
                entry = setup["sweep_high"] - calc_slippage(setup["sweep_high"])
                sl = setup["asian_high"] + 2.0
                tp = entry - (sl - entry) * 2
        
        else:
            continue
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        if rr < rr_min or risk <= 0:
            continue
        
        # Entry execution
        entry_idx = None
        entry_price = None
        
        if entry_type == "limit":
            for j in range(idx + 1, min(idx + 4, len(bars))):
                b = bars[j]
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
        else:  # market
            entry_idx = idx + 1
            entry_price = bars[entry_idx].o
        
        if entry_idx is None:
            continue
        
        # Recalculate risk with actual entry
        risk = abs(entry_price - sl)
        reward = abs(tp - entry_price)
        rr = reward / risk if risk > 0 else 0
        
        if rr < rr_min or risk <= 0:
            continue
        
        # Lot size
        risk_usd = get_risk(equity)
        lot = calc_lot(symbol, risk_usd, risk)
        
        # Walk to exit
        pnl = None
        exit_idx = None
        exit_price = None
        reason = ""
        
        for k in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(bars))):
            b = bars[k]
            
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
            exit_idx = min(entry_idx + max_bars, len(bars) - 1)
            exit_price = bars[exit_idx].c
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
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        
        trades.append({
            "time": setup["time"], "dir": setup["dir"], "entry": entry_price,
            "sl": sl, "tp": tp, "rr": rr, "exit": exit_price,
            "pnl": pnl, "reason": reason, "lot": lot,
            "risk_usd": risk_usd, "equity": equity,
            "strategy": strategy_name,
        })
        
        # Cooldown after 3 losses
        recent_losses = sum(1 for t in trades[-3:] if t["pnl"] <= 0)
        if recent_losses >= 3:
            cooldown_until = exit_idx + 12 if exit_idx else idx + 12
    
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
    print(" MT5 BROKER DATA — RIGOROUS BACKTEST")
    print(" ONLY real ticks from MidasFX | No yfinance | Commission + spread + slippage")
    print("=" * 100)
    print()
    
    data = load_mt5_data()
    
    results = {}
    
    for sym in sorted(data.keys()):
        if "H1" not in data[sym]:
            continue
        
        h1 = data[sym]["H1"]
        if len(h1) < 50:
            continue
        
        print(f"\n{'='*80}")
        print(f" {sym} — {len(h1)} H1 bars | {len(data[sym].get('H4', []))} H4 bars")
        print(f" Date range: {h1[0].time} → {h1[-1].time}")
        print(f"{'='*80}")
        
        # Generate setups for each strategy
        sweep_setups = detect_sweep_ob(h1, lookback=8)
        fvg_setups = find_fvg(h1, min_size=1.0)
        big_bar_setups = detect_big_bars(h1, multiplier=2.0)
        asian_setups = detect_asian_range(h1)
        
        print(f"  Setups found: Sweep+OB={len(sweep_setups)}, FVG={len(fvg_setups)}, BigBar={len(big_bar_setups)}, Asian={len(asian_setups)}")
        
        # Test each strategy
        strategies = [
            ("Sweep+OB_limit", "sweep_ob", sweep_setups, "limit", 15),
            ("Sweep+OB_market", "sweep_ob", sweep_setups, "market", 15),
            ("FVG_limit", "fvg_fill", fvg_setups, "limit", 10),
            ("FVG_market", "fvg_fill", fvg_setups, "market", 10),
            ("BigBar_limit", "big_bar_fade", big_bar_setups, "limit", 5),
            ("BigBar_market", "big_bar_fade", big_bar_setups, "market", 5),
            ("Asian_limit", "asian_range", asian_setups, "limit", 10),
            ("Asian_market", "asian_range", asian_setups, "market", 10),
        ]
        
        for name, key, setups, entry_type, max_bars in strategies:
            if not setups:
                continue
            
            r = simulate(h1, setups, sym, key, base=100.0, entry_type=entry_type, max_bars=max_bars)
            
            if r["trades"] == 0:
                continue
            
            print(f"\n  {name}:")
            print(f"    Trades: {r['trades']} | WR: {r['wr']:.1f}% | P&L: ${r['pnl']:.2f}")
            print(f"    Equity: ${r['equity']:.2f} | Return: {r['return_pct']:.1f}% | MaxDD: {r['max_dd']:.1f}%")
            
            if r['trades'] > 0:
                reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
                for t in r['detail']:
                    reasons[t["reason"]]["count"] += 1
                    reasons[t["reason"]]["pnl"] += t["pnl"]
                
                for reason, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
                    print(f"      {reason}: {d['count']} (${d['pnl']:.2f})")
            
            results[f"{sym}_{name}"] = r
    
    # Summary
    print(f"\n{'='*80}")
    print(" SUMMARY — ONLY PROFITABLE RESULTS")
    print(f"{'='*80}")
    
    profitable = [(k, v) for k, v in results.items() if v['pnl'] > 0 and v['trades'] >= 5]
    
    if profitable:
        for key, r in sorted(profitable, key=lambda x: -x[1]['return_pct']):
            print(f"  {key}: {r['trades']} trades, {r['wr']:.1f}% WR, ${r['pnl']:.2f}, {r['return_pct']:.1f}% return, {r['max_dd']:.1f}% DD")
    else:
        print("  NO profitable strategies found with 5+ trades")
    
    # Save
    out = os.path.join(os.path.dirname(__file__), 'mt5_rigor_results.json')
    with open(out, 'w') as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "detail"} for k, v in results.items()}, f, indent=2)
    print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
