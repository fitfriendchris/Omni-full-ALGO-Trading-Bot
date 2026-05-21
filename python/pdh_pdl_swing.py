#!/usr/bin/env python3
"""
pdh_pdl_swing.py — Previous Day High/Low Rejection Strategy

Discovered edge from level quality test:
  XAUUSD PDH: 60.6% reversal down when hit
  XAUUSD PDL: 53.8% reversal up when hit
  XAGUSD PDL: 72.7% reversal up when hit

Strategy:
  1. Mark yesterday's high and low as key levels
  2. When H1 approaches within 1% of PDH/PDL, place limit order AT the level
  3. Stop beyond yesterday's extreme + ATR buffer
  4. Target 2-3R
  5. Hold for swing (10+ bars)

Uses real MT5 data with proper limit order execution.
"""
import json, os, sys, re
from collections import defaultdict
from typing import List, Dict

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

COMMISSION = 7.0

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

def get_risk(e):
    if e >= 1000: return 25.0
    elif e >= 500: return 10.0
    elif e >= 200: return 5.0
    elif e >= 100: return 2.0
    return 1.0

def calc_lot(sym, risk_usd, sl_dist):
    if sl_dist <= 0: return 0.01
    pip_val = 0.01 if sym in ["XAUUSD","XAGUSD"] else 0.0001
    pip_size = 0.01 if sym in ["XAUUSD","XAGUSD"] else 0.0001
    pips = sl_dist / pip_size
    lot = risk_usd / (pips * pip_val)
    return max(0.01, min(lot, 1.0))

def simulate_pdh_pdl(d1_bars: List[Bar], h1_bars: List[Bar], symbol: str, base=1000.0):
    """
    PDH/PDL rejection strategy.
    At start of each day, mark yesterday's high/low.
    When price approaches within 0.5%, place limit at level.
    """
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    
    # Group H1 by day
    h1_days = defaultdict(list)
    for b in h1_bars:
        h1_days[b.time[:10]].append(b)
    
    sorted_days = sorted(h1_days.keys())
    
    for i in range(1, len(sorted_days)):
        prev_day = sorted_days[i-1]
        curr_day = sorted_days[i]
        
        prev_bars = h1_days[prev_day]
        curr_bars = h1_days[curr_day]
        
        if not prev_bars or not curr_bars:
            continue
        
        pdh = max(b.h for b in prev_bars)
        pdl = min(b.l for b in prev_bars)
        
        # Skip if range too small
        day_range = pdh - pdl
        if day_range <= 0:
            continue
        
        # Track if we already entered today
        entered_pdh = False
        entered_pdl = False
        
        for j, b in enumerate(curr_bars):
            if entered_pdh and entered_pdl:
                break
            
            idx = h1_bars.index(b)
            
            # ── PDH rejection (sell) ──────────────────────────────
            if not entered_pdh:
                # Price approaches PDH within 0.5%
                approach_dist = abs(b.h - pdh) / pdh
                if approach_dist <= 0.005 and b.c < b.h:
                    entry = pdh
                    sl = pdh + day_range * 0.3  # Stop beyond PDH
                    tp_rr = 2.5
                    risk_r = sl - entry
                    if risk_r <= 0: continue
                    tp = entry - risk_r * tp_rr
                    
                    # Check fill in next 3 bars
                    fill_idx = None
                    for k in range(idx + 1, min(idx + 4, len(h1_bars))):
                        if h1_bars[k].h >= entry:
                            fill_idx = k
                            break
                    
                    if fill_idx is None: continue
                    
                    risk_usd = get_risk(equity)
                    lot = calc_lot(symbol, risk_usd, risk_r)
                    
                    # Walk to exit (swing hold: 20+ bars)
                    pnl = None
                    exit_price = None
                    reason = ""
                    
                    for k in range(fill_idx + 1, min(fill_idx + 25, len(h1_bars))):
                        hb = h1_bars[k]
                        if hb.h >= sl:
                            exit_price = sl
                            pnl = -risk_usd - COMMISSION * lot
                            reason = "SL"
                            break
                        if hb.l <= tp:
                            exit_price = tp
                            pnl = risk_usd * tp_rr - COMMISSION * lot
                            reason = "TP"
                            break
                    
                    if pnl is None:
                        exit_idx = min(fill_idx + 24, len(h1_bars) - 1)
                        exit_price = h1_bars[exit_idx].c
                        gain = entry - exit_price
                        r_mult = gain / risk_r if risk_r > 0 else 0
                        pnl = risk_usd * r_mult - COMMISSION * lot
                        reason = "EOD"
                    
                    equity += pnl
                    if equity > peak: peak = equity
                    dd = (peak - equity) / peak * 100 if peak > 0 else 0
                    if dd > max_dd: max_dd = dd
                    
                    trades.append({
                        "dir": "SELL", "entry": entry, "sl": sl, "tp": tp,
                        "exit": exit_price, "pnl": pnl, "reason": reason,
                        "day": curr_day, "level": "PDH", "lot": lot,
                    })
                    entered_pdh = True
            
            # ── PDL rejection (buy) ───────────────────────────────
            if not entered_pdl:
                approach_dist = abs(b.l - pdl) / pdl
                if approach_dist <= 0.005 and b.c > b.l:
                    entry = pdl
                    sl = pdl - day_range * 0.3
                    tp_rr = 2.5
                    risk_r = entry - sl
                    if risk_r <= 0: continue
                    tp = entry + risk_r * tp_rr
                    
                    fill_idx = None
                    for k in range(idx + 1, min(idx + 4, len(h1_bars))):
                        if h1_bars[k].l <= entry:
                            fill_idx = k
                            break
                    
                    if fill_idx is None: continue
                    
                    risk_usd = get_risk(equity)
                    lot = calc_lot(symbol, risk_usd, risk_r)
                    
                    pnl = None
                    exit_price = None
                    reason = ""
                    
                    for k in range(fill_idx + 1, min(fill_idx + 25, len(h1_bars))):
                        hb = h1_bars[k]
                        if hb.l <= sl:
                            exit_price = sl
                            pnl = -risk_usd - COMMISSION * lot
                            reason = "SL"
                            break
                        if hb.h >= tp:
                            exit_price = tp
                            pnl = risk_usd * tp_rr - COMMISSION * lot
                            reason = "TP"
                            break
                    
                    if pnl is None:
                        exit_idx = min(fill_idx + 24, len(h1_bars) - 1)
                        exit_price = h1_bars[exit_idx].c
                        gain = exit_price - entry
                        r_mult = gain / risk_r if risk_r > 0 else 0
                        pnl = risk_usd * r_mult - COMMISSION * lot
                        reason = "EOD"
                    
                    equity += pnl
                    if equity > peak: peak = equity
                    dd = (peak - equity) / peak * 100 if peak > 0 else 0
                    if dd > max_dd: max_dd = dd
                    
                    trades.append({
                        "dir": "BUY", "entry": entry, "sl": sl, "tp": tp,
                        "exit": exit_price, "pnl": pnl, "reason": reason,
                        "day": curr_day, "level": "PDL", "lot": lot,
                    })
                    entered_pdl = True
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": sum(t["pnl"] for t in trades), "equity": equity,
        "peak": peak, "max_dd": max_dd,
        "return_pct": (equity - base) / base * 100,
        "detail": trades,
    }

def run():
    print("=" * 100)
    print(" PDH/PDL SWING STRATEGY — Previous Day Level Rejection")
    print(" Real MT5 data | Limit orders | Swing holds | Commission")
    print("=" * 100)
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]:
        if sym not in data or "D1" not in data[sym]:
            continue
        
        d1 = data[sym].get("D1", [])
        h1 = data[sym].get("H1", [])
        
        if len(d1) < 5 or len(h1) < 50:
            continue
        
        print(f"\n{'='*80}")
        print(f" {sym}")
        print(f"{'='*80}")
        
        r = simulate_pdh_pdl(d1, h1, sym, base=1000.0)
        
        if r["trades"] == 0:
            print("  No trades")
            continue
        
        print(f"  Trades: {r['trades']}")
        print(f"  Wins: {r['wins']} | Losses: {r['losses']}")
        print(f"  Win Rate: {r['wr']:.1f}%")
        print(f"  P&L: ${r['pnl']:.2f}")
        print(f"  Equity: ${r['equity']:.2f} (peak: ${r['peak']:.2f})")
        print(f"  Return: {r['return_pct']:.1f}%")
        print(f"  Max DD: {r['max_dd']:.1f}%")
        
        # By level
        pdh_trades = [t for t in r["detail"] if t["level"] == "PDH"]
        pdl_trades = [t for t in r["detail"] if t["level"] == "PDL"]
        
        if pdh_trades:
            pdh_wins = [t for t in pdh_trades if t["pnl"] > 0]
            print(f"\n  PDH Sells: {len(pdh_trades)} trades, {len(pdh_wins)/len(pdh_trades)*100:.1f}% WR, ${sum(t['pnl'] for t in pdh_trades):.2f}")
        
        if pdl_trades:
            pdl_wins = [t for t in pdl_trades if t["pnl"] > 0]
            print(f"  PDL Buys: {len(pdl_trades)} trades, {len(pdl_wins)/len(pdl_trades)*100:.1f}% WR, ${sum(t['pnl'] for t in pdl_trades):.2f}")
        
        # By exit reason
        reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
        for t in r["detail"]:
            reasons[t["reason"]]["count"] += 1
            reasons[t["reason"]]["pnl"] += t["pnl"]
        
        print(f"\n  Exits:")
        for reason, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
            print(f"    {reason}: {d['count']} (${d['pnl']:.2f})")
        
        out = os.path.join(os.path.dirname(__file__), f'pdh_pdl_{sym}.json')
        with open(out, 'w') as f:
            json.dump(r["detail"], f, indent=2)
        print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
