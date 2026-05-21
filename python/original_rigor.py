#!/usr/bin/env python3
"""
original_rigor.py — Original backtester logic with TRUE realistic execution

The original backtester has a critical execution flaw:
  - It detects sweep on bar[i-1]
  - Enters at wick extreme of bar[i-1]
  - Assumes trade is immediately active on bar[i]
  - But if bar[i] never retests the wick extreme, the limit order NEVER FILLS

This script replicates the EXACT original logic but with proper limit order
execution: only fills if bar[i] (or later) actually hits the entry price.

Uses ONLY real MT5 data.
"""
import json, math, os, sys, re
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

# ── Config ──────────────────────────────────────────────────────────
COMMISSION = 7.0
SPREAD_PCT = 0.0002
SLIPPAGE_PCT = 0.0001

# ── Load MT5 ──────────────────────────────────────────────────────
def load_mt5() -> Dict[str, Dict[str, List[Bar]]]:
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

# ── Helpers ──────────────────────────────────────────────────────
def get_risk(equity: float) -> float:
    if equity >= 1000: return 25.0
    elif equity >= 500: return 10.0
    elif equity >= 200: return 5.0
    elif equity >= 100: return 2.0
    return 1.0

def calc_lot(symbol: str, risk_usd: float, sl_dist: float) -> float:
    if sl_dist <= 0: return 0.01
    if symbol in ["XAUUSD", "XAGUSD"]:
        pip_val, pip_size = 0.01, 0.01
    else:
        pip_val, pip_size = 0.0001, 0.0001
    pips = sl_dist / pip_size
    lot = risk_usd / (pips * pip_val)
    return max(0.01, min(lot, 1.0))

def calc_spread(p: float) -> float: return p * SPREAD_PCT
def calc_slippage(p: float) -> float: return p * SLIPPAGE_PCT

# ── Find equal highs/lows ────────────────────────────────────────
def find_eq_levels(bars: List[Bar], tol: float = 0.002) -> Tuple[List[float], List[float]]:
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    
    eq_h = []
    for i in range(len(highs)-1):
        for j in range(i+1, len(highs)):
            if abs(highs[i] - highs[j]) < highs[i] * tol:
                eq_h.append(highs[i])
                break
    
    eq_l = []
    for i in range(len(lows)-1):
        for j in range(i+1, len(lows)):
            if abs(lows[i] - lows[j]) < lows[i] * tol:
                eq_l.append(lows[i])
                break
    
    return eq_h, eq_l

# ── Find OB ─────────────────────────────────────────────────────
def find_ob(bars: List[Bar], direction: str) -> Optional[Tuple[float, float]]:
    for i in range(1, len(bars)):
        b0, b1 = bars[i-1], bars[i]
        if direction == "BULL":
            if b0.c > b0.o and b1.c > b1.o and b1.l > b0.l:
                return (b0.l, b1.h)
        else:
            if b0.c < b0.o and b1.c < b1.o and b1.h < b0.h:
                return (b1.l, b0.h)
    return None

# ── ATR ─────────────────────────────────────────────────────────
def atr(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period:
        return sum(b.h - b.l for b in bars) / len(bars) if bars else 0.0
    trs = []
    for i in range(1, min(period, len(bars))):
        b0, b1 = bars[i-1], bars[i]
        tr = max(b1.h - b1.l, abs(b1.h - b0.c), abs(b1.l - b0.c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0

# ── ORIGINAL backtester logic — TRUE execution ──────────────────
def simulate_original(bars: List[Bar], symbol: str, base: float = 10000.0) -> Dict:
    """
    EXACT logic from backtester.py but with realistic limit order fills.
    
    Key difference from original:
      - Original: entry_price set, trade immediately active (assumes fill)
      - This: limit order placed, only fills if subsequent bar hits entry
    """
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    
    # Key levels from first few bars (simulating D1 data)
    all_highs = [b.h for b in bars[:60]]
    all_lows = [b.l for b in bars[:60]]
    pdh = max(all_highs) if all_highs else 0
    pdl = min(all_lows) if all_lows else 999999
    pwh = max(all_highs[:5]) if len(all_highs) >= 5 else pdh
    pwl = min(all_lows[:5]) if len(all_lows) >= 5 else pdl
    
    for idx in range(20, len(bars)):
        bar = bars[idx]
        prev_bars = bars[max(0, idx-20):idx]  # recent bars, newest first
        
        if len(prev_bars) < 8:
            continue
        
        # Session
        hour = int(bar.time[11:13])
        if 0 <= hour < 8: session = "ASIAN"
        elif 8 <= hour < 13: session = "LONDON"
        elif 13 <= hour < 17: session = "NY_AM"
        elif 17 <= hour < 21: session = "NY_PM"
        else: session = "OVERLAP"
        
        cur = bar.c
        
        # Equal highs/lows
        eq_h, eq_l = find_eq_levels(prev_bars[:15])
        liq_highs = [l for l in eq_h + [pdh, pwh] if l > cur * 0.999 and l > 0]
        liq_lows = [l for l in eq_l + [pdl, pwl] if l < cur * 1.001 and l > 0]
        
        # ATR
        atr_bt = atr(prev_bars, 14)
        
        # ── SELL setup: sweep of high ───────────────────────────
        for level in sorted(liq_highs)[:4]:
            b1 = prev_bars[0] if prev_bars else None
            if b1 and b1.h > level * 1.001 and b1.c < level:
                ob = find_ob(prev_bars[:12], "BEAR")
                
                if ob:
                    ob_l, ob_h = ob
                    sweep_extreme = b1.h
                    spread = calc_spread(sweep_extreme)
                    entry = sweep_extreme + spread
                    
                    sl_buffer = max(atr_bt * 0.5, (sweep_extreme - level) * 0.2)
                    sl = sweep_extreme + sl_buffer
                    if atr_bt > 0:
                        sl = min(sl, entry + atr_bt * 1.5)
                    
                    risk_r = sl - entry
                    if risk_r <= 0:
                        continue
                    
                    tp1 = entry - risk_r * 1.5
                    tp2 = entry - risk_r * 2.5
                    tp3 = entry - risk_r * 4.0
                    
                    # REALISTIC EXECUTION: place limit order for subsequent bars
                    entry_idx = None
                    for j in range(idx + 1, min(idx + 4, len(bars))):
                        if bars[j].h >= entry:
                            entry_idx = j
                            break
                    
                    if entry_idx is None:
                        continue  # Never filled
                    
                    # Walk to exit
                    risk_usd = get_risk(equity)
                    lot = calc_lot(symbol, risk_usd, risk_r)
                    
                    pnl = None
                    exit_price = None
                    reason = ""
                    
                    for k in range(entry_idx + 1, min(entry_idx + 16, len(bars))):
                        b = bars[k]
                        
                        if b.l <= sl:
                            exit_price = sl - calc_slippage(sl)
                            pnl = -risk_usd - COMMISSION * lot
                            reason = "SL"
                            break
                        elif b.h >= tp3:
                            exit_price = tp3
                            pnl = risk_usd * 4.0 - COMMISSION * lot
                            reason = "TP3"
                            break
                        elif b.h >= tp2:
                            exit_price = tp2
                            pnl = risk_usd * 2.5 - COMMISSION * lot
                            reason = "TP2"
                            break
                        elif b.h >= tp1:
                            exit_price = tp1
                            pnl = risk_usd * 1.5 - COMMISSION * lot
                            reason = "TP1"
                            break
                    
                    if pnl is None:
                        exit_price = bars[min(entry_idx + 15, len(bars) - 1)].c
                        gain = entry - exit_price
                        r_mult = gain / risk_r if risk_r > 0 else 0
                        pnl = risk_usd * r_mult - COMMISSION * lot
                        reason = "EOD"
                    
                    equity += pnl
                    if equity > peak: peak = equity
                    dd = (peak - equity) / peak * 100 if peak > 0 else 0
                    if dd > max_dd: max_dd = dd
                    
                    trades.append({
                        "time": bar.time, "dir": "SELL", "entry": entry,
                        "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "exit": exit_price, "pnl": pnl, "reason": reason,
                        "lot": lot, "risk_usd": risk_usd, "equity": equity,
                    })
                    break  # One setup per bar
        
        # ── BUY setup: sweep of low ──────────────────────────────
        if not any(t["time"] == bar.time for t in trades):
            for level in sorted(liq_lows, reverse=True)[:4]:
                b1 = prev_bars[0] if prev_bars else None
                if b1 and b1.l < level * 0.999 and b1.c > level:
                    ob = find_ob(prev_bars[:12], "BULL")
                    
                    if ob:
                        ob_l, ob_h = ob
                        sweep_extreme = b1.l
                        spread = calc_spread(sweep_extreme)
                        entry = sweep_extreme - spread
                        
                        sl_buffer = max(atr_bt * 0.5, (level - sweep_extreme) * 0.2)
                        sl = sweep_extreme - sl_buffer
                        if atr_bt > 0:
                            sl = max(sl, entry - atr_bt * 1.5)
                        
                        risk_r = entry - sl
                        if risk_r <= 0:
                            continue
                        
                        tp1 = entry + risk_r * 1.5
                        tp2 = entry + risk_r * 2.5
                        tp3 = entry + risk_r * 4.0
                        
                        # REALISTIC execution
                        entry_idx = None
                        for j in range(idx + 1, min(idx + 4, len(bars))):
                            if bars[j].l <= entry:
                                entry_idx = j
                                break
                        
                        if entry_idx is None:
                            continue
                        
                        risk_usd = get_risk(equity)
                        lot = calc_lot(symbol, risk_usd, risk_r)
                        
                        pnl = None
                        exit_price = None
                        reason = ""
                        
                        for k in range(entry_idx + 1, min(entry_idx + 16, len(bars))):
                            b = bars[k]
                            
                            if b.h >= sl:
                                exit_price = sl + calc_slippage(sl)
                                pnl = -risk_usd - COMMISSION * lot
                                reason = "SL"
                                break
                            elif b.l <= tp3:
                                exit_price = tp3
                                pnl = risk_usd * 4.0 - COMMISSION * lot
                                reason = "TP3"
                                break
                            elif b.l <= tp2:
                                exit_price = tp2
                                pnl = risk_usd * 2.5 - COMMISSION * lot
                                reason = "TP2"
                                break
                            elif b.l <= tp1:
                                exit_price = tp1
                                pnl = risk_usd * 1.5 - COMMISSION * lot
                                reason = "TP1"
                                break
                        
                        if pnl is None:
                            exit_price = bars[min(entry_idx + 15, len(bars) - 1)].c
                            gain = exit_price - entry
                            r_mult = gain / risk_r if risk_r > 0 else 0
                            pnl = risk_usd * r_mult - COMMISSION * lot
                            reason = "EOD"
                        
                        equity += pnl
                        if equity > peak: peak = equity
                        dd = (peak - equity) / peak * 100 if peak > 0 else 0
                        if dd > max_dd: max_dd = dd
                        
                        trades.append({
                            "time": bar.time, "dir": "BUY", "entry": entry,
                            "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                            "exit": exit_price, "pnl": pnl, "reason": reason,
                            "lot": lot, "risk_usd": risk_usd, "equity": equity,
                        })
                        break
    
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

# ── Main ─────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" ORIGINAL BACKTESTER LOGIC — TRUE REALISTIC EXECUTION")
    print(" Same sweep+OB detection | Same SL/TP/RR | But limit orders must actually fill")
    print("=" * 100)
    print()
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD"]:
        if "H1" not in data.get(sym, {}):
            continue
        
        h1 = data[sym]["H1"]
        if len(h1) < 50:
            continue
        
        print(f"\n{'='*80}")
        print(f" {sym} — {len(h1)} H1 bars | {h1[0].time} → {h1[-1].time}")
        print(f"{'='*80}")
        
        # Original uses $10,000 base
        r = simulate_original(h1, sym, base=10000.0)
        
        if r["trades"] == 0:
            print("  No trades")
            continue
        
        print(f"\n  Trades: {r['trades']}")
        print(f"  Wins: {r['wins']} | Losses: {r['losses']}")
        print(f"  Win Rate: {r['wr']:.1f}%")
        print(f"  P&L: ${r['pnl']:.2f}")
        print(f"  Equity: ${r['equity']:.2f} (peak: ${r['peak']:.2f})")
        print(f"  Return: {r['return_pct']:.1f}%")
        print(f"  Max DD: {r['max_dd']:.1f}%")
        
        reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
        for t in r['detail']:
            reasons[t["reason"]]["count"] += 1
            reasons[t["reason"]]["pnl"] += t["pnl"]
        
        print(f"\n  Exits:")
        for reason, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
            print(f"    {reason}: {d['count']} (${d['pnl']:.2f})")
        
        out = os.path.join(os.path.dirname(__file__), f'original_rigor_{sym}.json')
        with open(out, 'w') as f:
            json.dump(r['detail'], f, indent=2)
        print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
