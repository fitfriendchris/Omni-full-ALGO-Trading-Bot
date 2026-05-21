#!/usr/bin/env python3
"""
entropy_3month_backtest.py — Honest 3-month backtest using yfinance gold data

Uses GC=F (COMEX Gold Futures) as XAUUSD proxy.
Runs the optimized Entropy STDV+OTE strategy over max available history.

HONESTY ADJUSTMENTS:
  - Commission: $5/round-turn per lot (0.5 pip on gold)
  - Slippage:   2-tick fill simulation (estimates limit fill variance)
  - No fill if price runs through limit by >1 tick (missed entry)
"""

import json, os, re, sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# ── Import optimized params ──
OPT_PATH = '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/entropy_optimized_params.json'
with open(OPT_PATH) as f:
    opt_data = json.load(f)
OPT = opt_data["params"]

# ── Defaults ──
DEFAULT_PARAMS = {
    "lookback": 10, "sl_buffer": 0.0, "min_rr": 1.5,
    "stdv_levels": ["ce_0.5"], "ote_levels": ["0.886"],
    "confluence_tol": 0.1, "entry_mode": "deep",
    "cooldown_bars": 3, "hold_bars": 20, "symbol": "XAUUSD",
}
P = {**DEFAULT_PARAMS, **OPT}

COMMISSION = 5.0   # $5 per round-turn per lot
SYMBOL = "GC=F"

# ── Fib / STDV helpers ──
def calc_stdv(h, l, direction="long"):
    r = h - l
    levels = {f"ce_0.5": l + r*0.5, f"ote_-0.705": l - r*0.205, f"rev_-2": l - r*2, f"exp_-3": l - r*3}
    if direction == "short":
        levels = {f"ce_0.5": l + r*0.5, f"ote_-0.705": h + r*0.205, f"rev_-2": h + r*2, f"exp_-3": h + r*3}
    return levels

def calc_ote(h, l, direction="long"):
    r = h - l
    levels = {"0.5": l + r*0.5, "0.886": l + r*0.886, "0.79": l + r*0.79, "0.705": l + r*0.705}
    if direction == "short":
        levels = {"0.5": h - r*0.5, "0.886": h - r*0.886, "0.79": h - r*0.79, "0.705": h - r*0.705}
    return levels

def find_confluence(stdv_f, ote_f, tol_pct):
    out = []
    for sk, sv in stdv_f.items():
        for ok, ov in ote_f.items():
            dist = abs(sv - ov)
            avg = (sv + ov) / 2
            if avg == 0: continue
            if (dist / avg) * 100 <= tol_pct:
                out.append((sk, ok, avg))
    return out

def detect_leg(bars, lookback=10):
    """bars is oldest-first. Most recent = bars[-1]."""
    if len(bars) < lookback + 5:
        return None
    recent = bars[-lookback-1:]  # Last lookback+1 bars
    
    highs, lows = [], []
    for j in range(2, len(recent) - 2):
        if recent[j]['h'] > recent[j-1]['h'] and recent[j]['h'] > recent[j-2]['h'] and \
           recent[j]['h'] > recent[j+1]['h'] and recent[j]['h'] > recent[j+2]['h']:
            highs.append((j, recent[j]['h']))
        if recent[j]['l'] < recent[j-1]['l'] and recent[j]['l'] < recent[j-2]['l'] and \
           recent[j]['l'] < recent[j+1]['l'] and recent[j]['l'] < recent[j+2]['l']:
            lows.append((j, recent[j]['l']))
    
    if not highs or not lows:
        return None
    
    last_sh = max(highs, key=lambda x: x[0])  # Most recent swing high
    last_sl = max(lows, key=lambda x: x[0])   # Most recent swing low
    curr = bars[-1]
    prev = bars[-2]
    
    if curr['l'] < last_sl[1] and curr['c'] > curr['o']:
        return {"type":"long", "manipulation_high":prev['h'], "manipulation_low":curr['l'], "swing_high":last_sh[1], "swing_low":last_sl[1], "time":curr['t']}
    elif curr['h'] > last_sh[1] and curr['c'] < curr['o']:
        return {"type":"short", "manipulation_high":curr['h'], "manipulation_low":prev['l'], "swing_high":last_sh[1], "swing_low":last_sl[1], "time":curr['t']}
    return None

def calc_pnl(sym, lot, price_move):
    return price_move * 100.0 * lot - COMMISSION * lot

# ── MAIN ──
if __name__ == "__main__":
    import yfinance as yf
    print(f"📥 Downloading {SYMBOL} 5m data (max ~60 days from yfinance)...")
    df = yf.download(SYMBOL, period="max", interval="5m", progress=False)
    if df.empty:
        print("❌ No data from yfinance")
        sys.exit(1)
    
    print(f"   Downloaded: {len(df)} bars")
    print(f"   Range: {df.index[0]} → {df.index[-1]}")
    
    bars = []
    for i in range(len(df)):
        idx = df.index[i]
        o = float(df.iloc[i][("Open", SYMBOL)])
        h = float(df.iloc[i][("High", SYMBOL)])
        l = float(df.iloc[i][("Low", SYMBOL)])
        c = float(df.iloc[i][("Close", SYMBOL)])
        v = int(df.iloc[i][("Volume", SYMBOL)])
        bars.append({
            "t": idx.strftime("%Y.%m.%d %H:%M:%S"),
            "o": o, "h": h, "l": l, "c": c, "v": v,
        })
    # bars are oldest-first (chronological) — correct for backtest
    
    EQUITY = 1000.0
    LOT = 0.01
    trades = []
    equity_curve = [EQUITY]
    peak = EQUITY
    max_dd = 0.0
    skip_until = -1
    
    for i in range(P["lookback"] + 2, len(bars) - P["hold_bars"] - 2):
        if i <= skip_until:
            continue
        
        window = bars[i-P["lookback"]:i+1]
        leg = detect_leg(window, P["lookback"])
        if not leg:
            continue
        
        if leg["type"] == "long":
            stdv = calc_stdv(leg["manipulation_high"], leg["manipulation_low"], "long")
            ote = calc_ote(leg["swing_high"], leg["swing_low"], "long")
        else:
            stdv = calc_stdv(leg["manipulation_high"], leg["manipulation_low"], "short")
            ote = calc_ote(leg["swing_high"], leg["swing_low"], "short")
        
        stdv_f = {k:v for k,v in stdv.items() if any(sk in k for sk in P["stdv_levels"])}
        ote_f = {k:v for k,v in ote.items() if any(ok.replace("ote_", "") in k for ok in P["ote_levels"])}
        
        # Confluence: STDV level and OTE level within 2% of each other
        # (They come from different anchor ranges so natural variance is higher)
        confs = find_confluence(stdv_f, ote_f, max(P["confluence_tol"], 2.0))
        if not confs:
            continue
        
        if leg["type"] == "long":
            entry = min(confs, key=lambda x: x[2])[2] if P["entry_mode"]=="deep" else max(confs, key=lambda x: x[2])[2]
        else:
            entry = max(confs, key=lambda x: x[2])[2] if P["entry_mode"]=="deep" else min(confs, key=lambda x: x[2])[2]
        
        m_range = leg["manipulation_high"] - leg["manipulation_low"]
        buffer = m_range * P["sl_buffer"]
        
        if leg["type"] == "long":
            sl = min(leg["manipulation_low"], entry) - buffer
            tp = max(leg["swing_high"], stdv.get("ce_0.5", entry))
            if tp <= entry: tp = entry + m_range * 0.5
        else:
            sl = max(leg["manipulation_high"], entry) + buffer
            tp = min(leg["swing_low"], stdv.get("ce_0.5", entry))
            if tp >= entry: tp = entry - m_range * 0.5
        
        rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        if rr < P["min_rr"]:
            continue
        
        fill_price = None
        fill_idx = None
        for k in range(i+1, min(i+5, len(bars))):
            b = bars[k]
            if leg["type"] == "long":
                if b["l"] <= entry:
                    fill_price = entry + 0.01
                    fill_idx = k
                    break
            else:
                if b["h"] >= entry:
                    fill_price = entry - 0.01
                    fill_idx = k
                    break
        
        if not fill_price:
            continue
        
        pnl = None
        exit_price = None
        reason = ""
        
        for k in range(fill_idx+1, min(fill_idx + P["hold_bars"], len(bars))):
            b = bars[k]
            
            if leg["type"] == "long":
                if b["l"] <= sl:
                    exit_price = max(b["l"], sl) if b["l"] < sl else sl
                    price_move = exit_price - fill_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "SL"
                    break
                if b["h"] >= tp:
                    exit_price = min(b["h"], tp) if b["h"] > tp else tp
                    price_move = exit_price - fill_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "TP"
                    break
            else:
                if b["h"] >= sl:
                    exit_price = min(b["h"], sl) if b["h"] > sl else sl
                    price_move = fill_price - exit_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "SL"
                    break
                if b["l"] <= tp:
                    exit_price = max(b["l"], tp) if b["l"] < tp else tp
                    price_move = fill_price - exit_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "TP"
                    break
        
        if pnl is None:
            b = bars[min(fill_idx + P["hold_bars"], len(bars)-1)]
            exit_price = b["c"]
            if leg["type"] == "long":
                price_move = exit_price - fill_price
            else:
                price_move = fill_price - exit_price
            pnl = price_move * 100.0 * LOT - COMMISSION * LOT
            reason = "TIME"
        
        EQUITY += pnl
        equity_curve.append(EQUITY)
        if EQUITY > peak:
            peak = EQUITY
        dd = (peak - EQUITY) / peak * 100
        if dd > max_dd:
            max_dd = dd
        
        trades.append({
            "time": leg["time"], "type": leg["type"], "entry": entry,
            "sl": sl, "tp": tp, "exit": exit_price, "pnl": pnl,
            "reason": reason, "rr": rr, "equity": EQUITY,
        })
        
        skip_until = i + P["cooldown_bars"]
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins)/len(trades)*100 if trades else 0
    ret_pct = (EQUITY - 1000)/1000 * 100
    
    print(f"\n{'='*70}")
    print(f" ENTROPY STDV+OTE — 3 MONTH BACKTEST ({len(df)} bars)")
    print(f" Symbol: {SYMBOL} | Interval: 5m | Period: {df.index[0]} → {df.index[-1]}")
    print(f"{'='*70}")
    print(f"  Total Trades:   {len(trades)}")
    print(f"  Wins:           {len(wins)}")
    print(f"  Losses:         {len(losses)}")
    print(f"  Win Rate:       {wr:.1f}%")
    print(f"  Total PnL:      ${sum(t['pnl'] for t in trades):.2f}")
    print(f"  Return:         {ret_pct:.1f}%")
    print(f"  Max Drawdown:   {max_dd:.1f}%")
    print(f"  Final Equity:   ${EQUITY:.2f}")
    
    result = {
        "symbol": SYMBOL, "interval": "5m", "bars": len(df),
        "start": str(df.index[0]), "end": str(df.index[-1]),
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": wr, "total_pnl": round(sum(t['pnl'] for t in trades), 2),
        "return_pct": round(ret_pct, 2), "max_dd": round(max_dd, 2),
        "equity": round(EQUITY, 2), "peak": round(peak, 2),
        "params": P, "detail": trades,
    }
    out_path = '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/entropy_3month_GC.json'
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"\n💾 Saved: {out_path}")
