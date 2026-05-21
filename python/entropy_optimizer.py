#!/usr/bin/env python3
"""
entropy_optimizer.py — Brute-force parameter optimization for Entropy STDV+OTE

Tests thousands of parameter combinations to find the optimal setup.
Parameters to optimize:
  - lookback period for manipulation leg detection
  - buffer size for SL placement
  - minimum R:R requirement
  - which STDV levels to use
  - which OTE levels to use
  - confluence tolerance
  - cooldown period
  - hold time (bars)
  - entry selection (deepest vs shallowest OTE)
"""
import json, re, math, sys, os
from datetime import datetime
from typing import List, Dict, Tuple
from itertools import product

sys.path.insert(0, os.path.dirname(__file__))
from entropy_ote_engine import load_mt5, find_manipulation_legs, calc_stdv_levels, calc_ote_levels, find_confluence, COMMISSION
from ict_precision import Bar

def simulate_with_params(data: Dict, symbol: str, params: Dict, base=1000.0) -> Dict:
    """Run strategy with specific parameter set."""
    m5 = data.get("M5", [])
    if len(m5) < 30:
        return {"score": -9999}
    
    # Unpack params
    lookback = params["lookback"]
    sl_buffer = params["sl_buffer"]
    min_rr = params["min_rr"]
    stdv_levels = params["stdv_levels"]  # Which STDV keys to consider
    ote_levels = params["ote_levels"]    # Which OTE keys to consider
    confluence_tol = params["confluence_tol"]
    cooldown_bars = params["cooldown"]
    hold_bars = params["hold_bars"]
    entry_mode = params["entry_mode"]  # "deep" or "shallow"
    
    # Symbol config
    tick_value = 1.0
    point = 0.01
    if symbol == "XAUUSD":
        tick_value, point = 1.0, 0.01
    elif symbol == "XAGUSD":
        tick_value, point = 5.0, 0.001
    elif symbol in ["EURUSD", "GBPUSD", "AUDUSD", "USDCAD"]:
        tick_value, point = 1.0, 0.00001
    elif symbol == "USDJPY":
        tick_value, point = 0.63, 0.001
    
    # Find manipulation legs with custom lookback
    legs = []
    for i in range(lookback, len(m5) - 5):
        recent = m5[i-lookback:i+1]
        swing_highs = []
        swing_lows = []
        for j in range(2, len(recent) - 2):
            if recent[j].h > recent[j-1].h and recent[j].h > recent[j-2].h and \
               recent[j].h > recent[j+1].h and recent[j].h > recent[j+2].h:
                swing_highs.append((j, recent[j].h))
            if recent[j].l < recent[j-1].l and recent[j].l < recent[j-2].l and \
               recent[j].l < recent[j+1].l and recent[j].l < recent[j+2].l:
                swing_lows.append((j, recent[j].l))
        
        if not swing_highs or not swing_lows:
            continue
        
        last_sh = max(swing_highs, key=lambda x: x[0])
        last_sl = max(swing_lows, key=lambda x: x[0])
        curr = m5[i]
        prev = m5[i-1]
        
        if curr.l < last_sl[1] and curr.c > curr.o:
            body = abs(curr.c - curr.o)
            total_range = curr.h - curr.l
            if total_range > 0:
                legs.append({"idx": i, "time": curr.time, "type": "long",
                    "manipulation_high": prev.h, "manipulation_low": curr.l,
                    "swing_high": last_sh[1], "swing_low": last_sl[1]})
        elif curr.h > last_sh[1] and curr.c < curr.o:
            body = abs(curr.c - curr.o)
            total_range = curr.h - curr.l
            if total_range > 0:
                legs.append({"idx": i, "time": curr.time, "type": "short",
                    "manipulation_high": curr.h, "manipulation_low": prev.l,
                    "swing_high": last_sh[1], "swing_low": last_sl[1]})
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    cooldown = 0
    
    for leg in legs:
        if cooldown > 0:
            cooldown -= 1
            continue
        
        idx = leg["idx"]
        
        # Calculate STDV and OTE
        if leg["type"] == "long":
            stdv = calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "long")
            ote = calc_ote_levels(leg["swing_high"], leg["swing_low"], "long")
        else:
            stdv = calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "short")
            ote = calc_ote_levels(leg["swing_high"], leg["swing_low"], "short")
        
        # Filter to requested levels
        stdv_filtered = {k: v for k, v in stdv.items() if any(sk in k for sk in stdv_levels)}
        ote_filtered = {k: v for k, v in ote.items() if any(ok in k for ok in ote_levels)}
        
        # Find confluences
        confluences = []
        for sk, sv in stdv_filtered.items():
            for ok, ov in ote_filtered.items():
                avg = (sv + ov) / 2
                if avg == 0:
                    continue
                diff_pct = abs(sv - ov) / avg * 100
                if diff_pct < confluence_tol:
                    confluences.append((sk, ok, (sv + ov) / 2))
        
        if not confluences:
            continue
        
        # Select entry
        if leg["type"] == "long":
            if entry_mode == "deep":
                entry_level = min(confluences, key=lambda x: x[2])
            else:
                entry_level = max(confluences, key=lambda x: x[2])
        else:
            if entry_mode == "deep":
                entry_level = max(confluences, key=lambda x: x[2])
            else:
                entry_level = min(confluences, key=lambda x: x[2])
        
        entry = entry_level[2]
        manipulation_range = leg["manipulation_high"] - leg["manipulation_low"]
        buffer = manipulation_range * sl_buffer
        
        if leg["type"] == "long":
            sl = min(leg["manipulation_low"], entry) - buffer
            tp = max(leg["swing_high"], stdv.get("ce_0.5", entry))
            if tp <= entry:
                tp = entry + manipulation_range * 0.5
        else:
            sl = max(leg["manipulation_high"], entry) + buffer
            tp = min(leg["swing_low"], stdv.get("ce_0.5", entry))
            if tp >= entry:
                tp = entry - manipulation_range * 0.5
        
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        
        reward = abs(tp - entry)
        if reward / risk < min_rr:
            continue
        
        # Check fill in next 5 bars
        fill_idx = None
        for k in range(idx + 1, min(idx + 6, len(m5))):
            if m5[k].l <= entry <= m5[k].h:
                fill_idx = k
                break
        
        if fill_idx is None:
            continue
        
        lot = 0.1
        
        # Walk to exit
        pnl = None
        exit_price = None
        reason = ""
        
        for k in range(fill_idx + 1, min(fill_idx + hold_bars, len(m5))):
            b = m5[k]
            if leg["type"] == "long":
                if b.l <= sl:
                    exit_price = sl
                    pnl = lot * tick_value * ((exit_price - entry) / point) - COMMISSION * lot
                    reason = "SL"
                    break
                if b.h >= tp:
                    exit_price = tp
                    pnl = lot * tick_value * ((exit_price - entry) / point) - COMMISSION * lot
                    reason = "TP"
                    break
            else:
                if b.h >= sl:
                    exit_price = sl
                    pnl = lot * tick_value * ((entry - exit_price) / point) - COMMISSION * lot
                    reason = "SL"
                    break
                if b.l <= tp:
                    exit_price = tp
                    pnl = lot * tick_value * ((entry - exit_price) / point) - COMMISSION * lot
                    reason = "TP"
                    break
        
        if pnl is None:
            exit_idx = min(fill_idx + hold_bars - 1, len(m5) - 1)
            exit_price = m5[exit_idx].c
            if leg["type"] == "long":
                pnl = lot * tick_value * ((exit_price - entry) / point) - COMMISSION * lot
            else:
                pnl = lot * tick_value * ((entry - exit_price) / point) - COMMISSION * lot
            reason = "TIME"
        
        equity += pnl
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
        
        trades.append({"pnl": pnl, "reason": reason})
        cooldown = cooldown_bars
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    
    total_trades = len(trades)
    if total_trades == 0:
        return {"score": -9999}
    
    wr = len(wins) / total_trades
    total_pnl = sum(t["pnl"] for t in trades)
    
    # Score = PnL - penalize drawdown and low trade count
    # Need at least 20 trades for significance
    score = total_pnl
    if total_trades < 20:
        score *= 0.5  # Penalize low sample
    if max_dd > 50:
        score *= 0.5  # Penalize high drawdown
    
    return {
        "score": score,
        "trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "wr": wr * 100,
        "pnl": total_pnl,
        "return_pct": (equity - base) / base * 100,
        "max_dd": max_dd,
        "params": params,
    }

def optimize():
    print("=" * 100)
    print(" ENTROPY STDV+OTE PARAMETER OPTIMIZER")
    print(" Testing thousands of combinations on real MT5 data")
    print("=" * 100)
    
    data = load_mt5()
    symbol = "XAUUSD"
    
    if symbol not in data or "M5" not in data[symbol]:
        print(f"No M5 data for {symbol}")
        return
    
    # Parameter grid
    param_grid = {
        "lookback": [10, 15, 20, 30],
        "sl_buffer": [0.0, 0.02, 0.05, 0.10],
        "min_rr": [1.5, 2.0, 3.0, 5.0],
        "stdv_levels": [
            ["ce_0.5"],
            ["ote_-0.705"],
            ["reaccum_-1"],
            ["reversal_-2"],
            ["ce_0.5", "ote_-0.705"],
            ["ote_-0.705", "reaccum_-1"],
            ["ce_0.5", "ote_-0.705", "reaccum_-1"],
            ["ote_-0.705", "reaccum_-1", "reversal_-2"],
        ],
        "ote_levels": [
            ["ce_0.5"],
            ["ote_0.886"],
            ["ote_0.79"],
            ["ote_0.705"],
            ["ote_0.886", "ote_0.79"],
            ["ote_0.79", "ote_0.705"],
            ["ote_0.886", "ote_0.79", "ote_0.705"],
            ["ote_0.886", "ote_0.79", "ote_0.705", "ote_0.65"],
        ],
        "confluence_tol": [0.1, 0.2, 0.5, 1.0],
        "cooldown": [0, 3, 5, 10],
        "hold_bars": [20, 30, 50, 100],
        "entry_mode": ["deep", "shallow"],
    }
    
    # Generate all combinations (limited for speed)
    keys = list(param_grid.keys())
    total_combos = 1
    for k in keys:
        total_combos *= len(param_grid[k])
    
    print(f"\nTotal parameter combinations: {total_combos:,}")
    print(f"Testing on {symbol} with {len(data[symbol]['M5'])} M5 bars...")
    print()
    
    # Limit to first 500 combinations for speed
    max_test = 500
    tested = 0
    results = []
    
    for values in product(*[param_grid[k] for k in keys]):
        params = dict(zip(keys, values))
        
        r = simulate_with_params(data[symbol], symbol, params)
        if r["score"] > -1000:
            results.append(r)
        
        tested += 1
        if tested >= max_test:
            break
        
        if tested % 100 == 0:
            print(f"  Tested {tested}/{max_test}...")
    
    # Sort by score
    results.sort(key=lambda x: -x["score"])
    
    print(f"\n{'='*80}")
    print(" TOP 10 PARAMETER SETS")
    print(f"{'='*80}")
    
    for i, r in enumerate(results[:10]):
        p = r["params"]
        print(f"\n#{i+1} | Score: ${r['score']:.2f}")
        print(f"  Trades: {r['trades']} | WR: {r['wr']:.1f}% | PnL: ${r['pnl']:.2f} | DD: {r['max_dd']:.1f}%")
        print(f"  lookback={p['lookback']} | buffer={p['sl_buffer']} | min_RR={p['min_rr']}")
        print(f"  STDV: {p['stdv_levels']} | OTE: {p['ote_levels']}")
        print(f"  tol={p['confluence_tol']}% | cooldown={p['cooldown']} | hold={p['hold_bars']} | mode={p['entry_mode']}")
    
    # Save best
    if results:
        best = results[0]
        out = os.path.join(os.path.dirname(__file__), 'entropy_optimized_params.json')
        with open(out, 'w') as f:
            json.dump(best, f, indent=2)
        print(f"\n  Best params saved: {out}")
    
    print(f"\n{'='*80}")
    print(" OPTIMIZATION COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    optimize()
