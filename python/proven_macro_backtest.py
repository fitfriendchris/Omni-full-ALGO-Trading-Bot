#!/usr/bin/env python3
"""
proven_macro_backtest.py — Uses backtester's proven logic WITH macro filters.
2 years H1 data from yfinance. Dollar-risk sizing for 1:1000 leverage.
"""
import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import (
    Bar, detect_sweep_high, detect_sweep_low,
    find_bearish_ob, find_bullish_ob,
    find_equal_highs, find_equal_lows,
    get_ob_precision_entry, detect_turtle_soup,
    _calc_atr, _snap_to_interval, _dist_intervals,
)

# ── Config ──────────────────────────────────────────────────────────
SYMBOLS_YF = {"GC=F": "XAUUSD", "SI=F": "XAGUSD"}
H1_START = "2024-05-22"
H1_END = "2026-05-21"

# Dollar-risk for 1:1000 leverage, $100-500 account
MAX_TRADE_RISK_USD = 2.0    # $2 max loss per trade
COMMISSION_PER_LOT = 7.0    # Round-turn
SPREAD = {"XAUUSD": 0.50, "XAGUSD": 0.03}

# ── Fetch data ────────────────────────────────────────────────────
def fetch_h1(ticker: str) -> List[Bar]:
    df = yf.download(ticker, start=H1_START, end=H1_END, interval="1h", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    bars = []
    for ts, row in df.iterrows():
        bars.append(Bar(
            time=str(ts)[:19], o=float(row["Open"]), h=float(row["High"]),
            l=float(row["Low"]), c=float(row["Close"]), v=int(row.get("Volume", 0)),
        ))
    return bars

def resample_d1(h1_bars: List[Bar]) -> List[Bar]:
    by_day = defaultdict(list)
    for b in h1_bars:
        by_day[b.time[:10]].append(b)
    d1 = []
    for day in sorted(by_day):
        db = by_day[day]
        d1.append(Bar(time=day, o=db[0].o, h=max(b.h for b in db),
                      l=min(b.l for b in db), c=db[-1].c, v=sum(b.v for b in db)))
    return d1

# ── Macro bias (weekly D1 EMA20) ─────────────────────────────────
def weekly_bias(d1_bars: List[Bar], week_lookback: int = 3) -> List[tuple]:
    """Returns [(week_key, bias, ema20, price_vs_ema)] for each week."""
    if len(d1_bars) < 20:
        return []
    
    # Compute EMA20
    ema = d1_bars[0].c
    k = 2.0 / 21.0
    emas = []
    for b in d1_bars:
        ema = b.c * k + ema * (1 - k)
        emas.append(ema)
    
    # Group by week
    weeks = defaultdict(list)
    for i, b in enumerate(d1_bars):
        dt = datetime.strptime(b.time, "%Y-%m-%d")
        wk = dt.strftime("%Y-W%U")
        weeks[wk].append((b, emas[i]))
    
    result = []
    for wk in sorted(weeks.keys()):
        wbars = weeks[wk]
        prices = [b.c for b, e in wbars]
        ema_vals = [e for b, e in wbars]
        
        above = sum(1 for p, e in zip(prices, ema_vals) if p > e)
        below = len(wbars) - above
        
        # Week open vs week close
        w_open = wbars[0][0].o
        w_close = wbars[-1][0].c
        
        if above > below and w_close > w_open:
            bias = "BULLISH"
        elif below > above and w_close < w_open:
            bias = "BEARISH"
        else:
            bias = "RANGING"
        
        result.append((wk, bias, ema_vals[-1], w_close))
    
    return result

# ── Setup detection (proven backtester logic) ─────────────────────
def detect_setups_proven(h1_bars: List[Bar], bias: str, symbol: str, week_ema: float) -> List[dict]:
    if len(h1_bars) < 60:
        return []
    
    atr = _calc_atr(h1_bars[-20:], period=14)
    if atr <= 0:
        return []
    
    spread = SPREAD.get(symbol, atr * 0.05)
    cur = h1_bars[-1].c
    
    # Only trade if price aligns with macro bias
    if bias == "BULLISH" and cur < week_ema * 0.995:
        return []  # Price too far below EMA, don't buy
    if bias == "BEARISH" and cur > week_ema * 1.005:
        return []  # Price too far above EMA, don't sell
    
    liq_bars = h1_bars[-60:]
    sweep_bars = h1_bars[-10:]
    ob_bars = h1_bars[-30:]
    
    eq_highs = find_equal_highs(liq_bars, tolerance_pct=0.0015)
    eq_lows = find_equal_lows(liq_bars, tolerance_pct=0.0015)
    
    struct_highs = sorted([b.h for b in liq_bars[-30:]], reverse=True)[:6]
    struct_lows = sorted([b.l for b in liq_bars[-30:]])[:6]
    
    liq_highs = sorted(set(eq_highs + struct_highs), reverse=True)[:8]
    liq_lows = sorted(set(eq_lows + struct_lows))[:8]
    
    dist = _dist_intervals.get(symbol, _dist_intervals.get("default", 0.0))
    setups = []
    
    # ── SELL ── (only if bias allows)
    if bias in ["BEARISH", "RANGING"]:
        for level in liq_highs[:5]:
            if not detect_sweep_high(sweep_bars, level, tolerance_pct=0.002):
                continue
            ob = find_bearish_ob(ob_bars, start=0, search=12)
            if not ob:
                continue
            ob_low, ob_high = ob
            if abs(ob_high - level) > atr * 3:
                continue
            
            # AMD Market Maker entry: manipulation wick extreme
            # For bearish OB, the wick extreme is ob_high (the high of the OB)
            entry = ob_high - spread
            sweep_ext = max(b.h for b in sweep_bars[:4]) if sweep_bars else level * 1.001
            sl = max(sweep_ext, level) + atr * 0.5  # Beyond sweep + buffer
            risk = sl - entry
            if risk <= 0 or risk > atr * 5 or risk < atr * 0.1:
                continue
            
            tp1 = entry - risk * 1.5
            tp2 = entry - risk * 2.5
            tp3 = entry - risk * 4.0
            if dist > 0:
                tp1 = _snap_to_interval(tp1, dist, "SELL")
                tp2 = _snap_to_interval(tp2, dist, "SELL")
            
            confidence = 48
            if detect_turtle_soup(sweep_bars, level, "SELL"):
                confidence += 12
            if level in eq_highs[:3]:
                confidence += 10
            if bias == "BEARISH":
                confidence += 12
            elif bias == "BULLISH":
                confidence -= 15
            if cur < week_ema:  # Price below weekly EMA = stronger sell
                confidence += 8
            
            if confidence < 35:
                continue
            
            setups.append({
                "direction": "SELL", "entry": entry, "sl": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "risk": risk, "atr": atr, "confidence": confidence,
                "level": level, "bias": bias, "ema": week_ema,
            })
    
    # ── BUY ── (only if bias allows)
    if bias in ["BULLISH", "RANGING"]:
        for level in liq_lows[:5]:
            if not detect_sweep_low(sweep_bars, level, tolerance_pct=0.002):
                continue
            ob = find_bullish_ob(ob_bars, start=0, search=12)
            if not ob:
                continue
            ob_low, ob_high = ob
            if abs(ob_low - level) > atr * 3:
                continue
            
            # AMD Market Maker entry: manipulation wick extreme
            # For bullish OB, the wick extreme is ob_low (the low of the OB)
            entry = ob_low + spread
            sweep_ext = min(b.l for b in sweep_bars[:4]) if sweep_bars else level * 0.999
            sl = min(sweep_ext, level) - atr * 0.5
            risk = entry - sl
            if risk <= 0 or risk > atr * 5 or risk < atr * 0.1:
                continue
            
            tp1 = entry + risk * 1.5
            tp2 = entry + risk * 2.5
            tp3 = entry + risk * 4.0
            if dist > 0:
                tp1 = _snap_to_interval(tp1, dist, "BUY")
                tp2 = _snap_to_interval(tp2, dist, "BUY")
            
            confidence = 48
            if detect_turtle_soup(sweep_bars, level, "BUY"):
                confidence += 12
            if level in eq_lows[:3]:
                confidence += 10
            if bias == "BULLISH":
                confidence += 12
            elif bias == "BEARISH":
                confidence -= 15
            if cur > week_ema:
                confidence += 8
            
            if confidence < 35:
                continue
            
            setups.append({
                "direction": "BUY", "entry": entry, "sl": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "risk": risk, "atr": atr, "confidence": confidence,
                "level": level, "bias": bias, "ema": week_ema,
            })
    
    return setups

# ── Simulate with dollar-risk sizing ──────────────────────────────
def simulate_dollar_risk(h1_bars: List[Bar], setups: List[dict], symbol: str) -> List[dict]:
    """Simulate using $2 max risk per trade, proper lot sizing for 1:1000."""
    results = []
    equity = 500.0  # Start with $500 account
    daily_loss = 0.0
    current_day = ""
    
    for setup in setups:
        day = setup['time'][:10] if 'time' in setup else ""
        if day != current_day:
            current_day = day
            daily_loss = 0.0
        
        # Daily loss limit
        if daily_loss <= -MAX_TRADE_RISK_USD * 2:
            continue
        
        # Calculate lot size for $2 risk
        risk_pips = setup['risk']  # In price units
        # Convert to actual pips based on symbol
        if symbol == "XAUUSD":
            pip_size = 0.01  # 1 pip = $0.01
            pip_value = 0.01  # per 0.01 lot
        elif symbol == "XAGUSD":
            pip_size = 0.001
            pip_value = 0.001
        else:
            pip_size = 0.0001
            pip_value = 0.01
        
        pips = risk_pips / pip_size
        lot_size = MAX_TRADE_RISK_USD / (pips * pip_value)
        lot_size = max(0.01, min(lot_size, 1.0))  # Cap at 1 lot
        
        actual_risk_usd = pips * pip_value * lot_size
        
        # Limit order simulation: check if price retraces to entry within 3 bars
        setup_time = setup['time']
        filled = False
        fill_price = None
        
        for b in h1_bars:
            if b.time < setup_time:
                continue
            if setup['direction'] == 'SELL':
                if b.h >= setup['entry']:
                    filled = True
                    fill_price = setup['entry']
                    break
            else:
                if b.l <= setup['entry']:
                    filled = True
                    fill_price = setup['entry']
                    break
            # Max 3 bars wait
            if sum(1 for bb in h1_bars if bb.time >= setup_time) > 3:
                break
        
        if not filled:
            continue
        
        # Walk to exit
        pnl_usd = None
        exit_price = None
        r_multiple = 0
        hit_tp = None
        
        for b in h1_bars:
            if b.time <= setup_time:
                continue
            
            if setup['direction'] == 'SELL':
                # SL hit
                if b.h >= setup['sl']:
                    exit_price = setup['sl']
                    pnl_usd = -(actual_risk_usd + COMMISSION_PER_LOT * lot_size)
                    r_multiple = -1.0
                    hit_tp = "SL"
                    break
                # TP3
                if b.l <= setup['tp3']:
                    exit_price = setup['tp3']
                    r_multiple = 4.0
                    pnl_usd = actual_risk_usd * 4.0 - COMMISSION_PER_LOT * lot_size
                    hit_tp = "TP3"
                    break
                # TP2
                if b.l <= setup['tp2']:
                    exit_price = setup['tp2']
                    r_multiple = 2.5
                    pnl_usd = actual_risk_usd * 2.5 - COMMISSION_PER_LOT * lot_size
                    hit_tp = "TP2"
                    break
                # TP1
                if b.l <= setup['tp1']:
                    exit_price = setup['tp1']
                    r_multiple = 1.5
                    pnl_usd = actual_risk_usd * 1.5 - COMMISSION_PER_LOT * lot_size
                    hit_tp = "TP1"
                    break
            else:
                if b.l <= setup['sl']:
                    exit_price = setup['sl']
                    pnl_usd = -(actual_risk_usd + COMMISSION_PER_LOT * lot_size)
                    r_multiple = -1.0
                    hit_tp = "SL"
                    break
                if b.h >= setup['tp3']:
                    exit_price = setup['tp3']
                    r_multiple = 4.0
                    pnl_usd = actual_risk_usd * 4.0 - COMMISSION_PER_LOT * lot_size
                    hit_tp = "TP3"
                    break
                if b.h >= setup['tp2']:
                    exit_price = setup['tp2']
                    r_multiple = 2.5
                    pnl_usd = actual_risk_usd * 2.5 - COMMISSION_PER_LOT * lot_size
                    hit_tp = "TP2"
                    break
                if b.h >= setup['tp1']:
                    exit_price = setup['tp1']
                    r_multiple = 1.5
                    pnl_usd = actual_risk_usd * 1.5 - COMMISSION_PER_LOT * lot_size
                    hit_tp = "TP1"
                    break
        
        if pnl_usd is None:
            # Close at last bar
            last = h1_bars[-1].c
            exit_price = last
            if setup['direction'] == 'SELL':
                gain = fill_price - last
            else:
                gain = last - fill_price
            r_multiple = gain / setup['risk'] if setup['risk'] > 0 else 0
            pnl_usd = actual_risk_usd * r_multiple - COMMISSION_PER_LOT * lot_size
            hit_tp = "CLOSE"
        
        equity += pnl_usd
        daily_loss += pnl_usd
        
        results.append({
            'time': setup_time, 'direction': setup['direction'],
            'entry': fill_price, 'sl': setup['sl'], 'tp': setup.get('tp2'),
            'exit': exit_price, 'pnl_usd': pnl_usd, 'r': r_multiple,
            'hit': hit_tp, 'bias': setup['bias'], 'confidence': setup['confidence'],
            'lot': lot_size, 'equity': equity,
        })
    
    return results

# ── Main ───────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" PROVEN LOGIC + MACRO FILTER — 2 Years H1 Data")
    print("=" * 100)
    print()
    print(f"Period: {H1_START} → {H1_END}")
    print(f"Leverage: 1:1000 | Max risk: ${MAX_TRADE_RISK_USD}/trade")
    print()
    
    all_results = {}
    
    for yf_ticker, symbol in SYMBOLS_YF.items():
        print(f"\n{'='*80}")
        print(f" {symbol}")
        print(f"{'='*80}")
        
        h1 = fetch_h1(yf_ticker)
        if not h1:
            print("  No H1 data")
            continue
        print(f"  H1 bars: {len(h1)} ({h1[0].time[:10]} → {h1[-1].time[:10]})")
        
        d1 = resample_d1(h1)
        print(f"  D1 bars: {len(d1)}")
        
        macro = weekly_bias(d1)
        print(f"  Weekly patterns: {len(macro)}")
        bull = sum(1 for _, b, _, _ in macro if b == "BULLISH")
        bear = sum(1 for _, b, _, _ in macro if b == "BEARISH")
        rng = sum(1 for _, b, _, _ in macro if b == "RANGING")
        print(f"    Bull: {bull} | Bear: {bear} | Range: {rng}")
        
        # Walk forward: detect setups on each H1 bar
        all_setups = []
        for i in range(60, len(h1)):
            window = h1[i-60:i]
            cur_time = h1[i].time
            cur_day = cur_time[:10]
            
            # Find macro bias for this week
            dt = datetime.strptime(cur_day, "%Y-%m-%d")
            wk = dt.strftime("%Y-W%U")
            bias = "NEUTRAL"
            ema = 0
            for week_key, b, e, p in macro:
                if week_key == wk:
                    bias = b
                    ema = e
                    break
            
            setups = detect_setups_proven(window, bias, symbol, ema)
            for s in setups:
                s['time'] = cur_time
                all_setups.append(s)
        
        print(f"  Setups detected: {len(all_setups)}")
        
        # Simulate
        trades = simulate_dollar_risk(h1, all_setups, symbol)
        print(f"  Trades executed: {len(trades)}")
        
        if trades:
            wins = [t for t in trades if t['pnl_usd'] > 0]
            losses = [t for t in trades if t['pnl_usd'] <= 0]
            total_pnl = sum(t['pnl_usd'] for t in trades)
            
            print(f"\n  RESULTS:")
            print(f"    Wins: {len(wins)} | Losses: {len(losses)}")
            print(f"    Win Rate: {len(wins)/len(trades)*100:.1f}%")
            print(f"    Total P&L: ${total_pnl:.2f}")
            print(f"    Avg Trade: ${total_pnl/len(trades):.2f}")
            if wins:
                print(f"    Avg Win: ${sum(t['pnl_usd'] for t in wins)/len(wins):.2f}")
            if losses:
                print(f"    Avg Loss: ${sum(t['pnl_usd'] for t in losses)/len(losses):.2f}")
            
            # By bias
            for bias_type in ["BULLISH", "BEARISH", "RANGING", "NEUTRAL"]:
                bt = [t for t in trades if t['bias'] == bias_type]
                if bt:
                    b_wins = sum(1 for t in bt if t['pnl_usd'] > 0)
                    print(f"    {bias_type}: {len(bt)} trades, {b_wins/len(bt)*100:.1f}% WR, ${sum(t['pnl_usd'] for t in bt):.2f}")
            
            # By confidence
            high_conf = [t for t in trades if t['confidence'] >= 70]
            low_conf = [t for t in trades if t['confidence'] < 70]
            if high_conf:
                print(f"    High conf (70+): {len(high_conf)} trades, {sum(1 for t in high_conf if t['pnl_usd']>0)/len(high_conf)*100:.1f}% WR")
            if low_conf:
                print(f"    Low conf (<70): {len(low_conf)} trades, {sum(1 for t in low_conf if t['pnl_usd']>0)/len(low_conf)*100:.1f}% WR")
            
            # Equity curve
            print(f"\n  Final equity: ${trades[-1]['equity']:.2f}")
            print(f"  Peak equity: ${max(t['equity'] for t in trades):.2f}")
            print(f"  Min equity: ${min(t['equity'] for t in trades):.2f}")
            
            all_results[symbol] = {
                'trades': len(trades),
                'wins': len(wins),
                'losses': len(losses),
                'win_rate': len(wins)/len(trades)*100 if trades else 0,
                'total_pnl': total_pnl,
                'final_equity': trades[-1]['equity'],
            }
    
    # Save
    out = os.path.join(os.path.dirname(__file__), 'proven_macro_results.json')
    with open(out, 'w') as f:
        json.dump(all_results, f, indent=2)
    print(f"\n{'='*80}")
    print(f" Saved: {out}")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
