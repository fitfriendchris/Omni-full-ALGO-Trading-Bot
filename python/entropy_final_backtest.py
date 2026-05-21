#!/usr/bin/env python3
"""
entropy_final_backtest.py — Optimized Entropy STDV+OTE on Full Available Data

Uses best parameters from optimizer:
  - lookback=10 | sl_buffer=0.0 | min_rr=1.5
  - STDV: ce_0.5 only | OTE: ote_0.886 only
  - confluence_tol=0.1% | cooldown=3 | hold=20 bars
"""
import json, re, sys, os
from datetime import datetime
from typing import List, Dict, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from entropy_ote_engine import load_mt5, calc_stdv_levels, calc_ote_levels, COMMISSION
from ict_precision import Bar

OPTIMAL_PARAMS = {
    "lookback": 10,
    "sl_buffer": 0.0,
    "min_rr": 1.5,
    "stdv_levels": ["ce_0.5"],
    "ote_levels": ["ote_0.886"],
    "confluence_tol": 0.1,
    "cooldown": 3,
    "hold_bars": 20,
    "entry_mode": "deep",
}

def simulate_optimized(data: Dict, symbol: str, params: Dict, base=1000.0) -> Dict:
    m5 = data.get("M5", [])
    if len(m5) < 30:
        return {"trades": 0}
    
    # Symbol config
    tick_value, point = 1.0, 0.01
    if symbol == "XAUUSD":
        tick_value, point = 1.0, 0.01
    elif symbol == "XAGUSD":
        tick_value, point = 5.0, 0.001
    elif symbol in ["EURUSD", "GBPUSD", "AUDUSD", "USDCAD"]:
        tick_value, point = 1.0, 0.00001
    elif symbol == "USDJPY":
        tick_value, point = 0.63, 0.001
    
    lookback = params["lookback"]
    
    # Find manipulation legs
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
            legs.append({"idx": i, "time": curr.time, "type": "long",
                "manipulation_high": prev.h, "manipulation_low": curr.l,
                "swing_high": last_sh[1], "swing_low": last_sl[1]})
        elif curr.h > last_sh[1] and curr.c < curr.o:
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
        
        # Calculate STDV + OTE
        if leg["type"] == "long":
            stdv = calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "long")
            ote = calc_ote_levels(leg["swing_high"], leg["swing_low"], "long")
        else:
            stdv = calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "short")
            ote = calc_ote_levels(leg["swing_high"], leg["swing_low"], "short")
        
        # Filter to optimal levels only
        stdv_f = {k: v for k, v in stdv.items() if any(sk in k for sk in params["stdv_levels"])}
        ote_f = {k: v for k, v in ote.items() if any(ok in k for ok in params["ote_levels"])}
        
        # Find confluences
        confluences = []
        for sk, sv in stdv_f.items():
            for ok, ov in ote_f.items():
                avg = (sv + ov) / 2
                if avg == 0:
                    continue
                diff_pct = abs(sv - ov) / avg * 100
                if diff_pct < params["confluence_tol"]:
                    confluences.append((sk, ok, avg))
        
        if not confluences:
            continue
        
        # Select entry
        if leg["type"] == "long":
            if params["entry_mode"] == "deep":
                entry_level = min(confluences, key=lambda x: x[2])
            else:
                entry_level = max(confluences, key=lambda x: x[2])
        else:
            if params["entry_mode"] == "deep":
                entry_level = max(confluences, key=lambda x: x[2])
            else:
                entry_level = min(confluences, key=lambda x: x[2])
        
        entry = entry_level[2]
        manipulation_range = leg["manipulation_high"] - leg["manipulation_low"]
        buffer = manipulation_range * params["sl_buffer"]
        
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
        if reward / risk < params["min_rr"]:
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
        
        for k in range(fill_idx + 1, min(fill_idx + params["hold_bars"], len(m5))):
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
            exit_idx = min(fill_idx + params["hold_bars"] - 1, len(m5) - 1)
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
        
        trades.append({
            "time": leg["time"], "type": leg["type"],
            "entry": entry, "sl": sl, "tp": tp, "exit": exit_price,
            "pnl": pnl, "reason": reason,
            "rr": reward / risk,
            "confluence": f"{entry_level[0]} + {entry_level[1]}",
        })
        
        cooldown = params["cooldown"]
    
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
        "legs": len(legs),
        "detail": trades,
    }

def run():
    print("=" * 100)
    print(" ENTROPY STDV+OTE — OPTIMIZED PARAMETERS")
    print("=" * 100)
    print("\nOptimal Parameters:")
    for k, v in OPTIMAL_PARAMS.items():
        print(f"  {k}: {v}")
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]:
        if sym not in data or "M5" not in data[sym]:
            continue
        
        print(f"\n{'='*80}")
        print(f" {sym}")
        print(f"{'='*80}")
        
        r = simulate_optimized(data[sym], sym, OPTIMAL_PARAMS)
        
        print(f"  Manipulation legs: {r['legs']}")
        print(f"  Trades: {r['trades']} | Wins: {r['wins']} | Losses: {r['losses']}")
        print(f"  Win Rate: {r['wr']:.1f}%")
        print(f"  P&L: ${r['pnl']:.2f}")
        print(f"  Return: {r['return_pct']:.1f}%")
        print(f"  Max DD: {r['max_dd']:.1f}%")
        
        if r['trades'] > 0:
            avg_rr = sum(t['rr'] for t in r['detail']) / len(r['detail'])
            print(f"  Avg R:R: {avg_rr:.1f}:1")
            
            # Show last 5 trades
            print(f"\n  Last 5 trades:")
            for t in r['detail'][-5:]:
                status = "✅" if t['pnl'] > 0 else "❌"
                print(f"    {status} {t['time']} | {t['type'].upper()} | Entry: {t['entry']:.2f} | Exit: {t['exit']:.2f} ({t['reason']}) | PnL: ${t['pnl']:.2f}")
        
        out = os.path.join(os.path.dirname(__file__), f'entropy_final_{sym}.json')
        with open(out, 'w') as f:
            json.dump(r, f, indent=2)
        print(f"  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" BACKTEST COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
