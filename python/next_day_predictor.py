#!/usr/bin/env python3
"""
next_day_predictor.py — End-of-day analysis → next day trade

Discovered from 14 days of MT5 data:
- DOWN-DOWN-DOWN: 3 occurrences (trend continuation)
- UP-UP-UP: 2 occurrences (trend continuation)
- UP-DOWN-UP: 2-3 occurrences (range/volatile)

Hypothesis: After 2 consecutive same-direction days, trade continuation.
After manipulation, trade reversal next day.
After range day, trade breakout next day.

Uses ONLY end-of-day analysis, no intraday timing.
"""
import json, os, sys, re
from datetime import datetime
from collections import defaultdict

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

def analyze_day(bars):
    if not bars:
        return None
    asian = bars[:8]
    asian_high = max(b.h for b in asian)
    asian_low = min(b.l for b in asian)
    
    day_open = bars[0].o
    day_close = bars[-1].c
    day_high = max(b.h for b in bars)
    day_low = min(b.l for b in bars)
    
    trend = "UP" if day_close > day_open else "DOWN" if day_close < day_open else "FLAT"
    
    # Did London sweep Asian range?
    london = bars[8:13] if len(bars) >= 13 else bars[8:]
    if london:
        london_high = max(b.h for b in london)
        london_low = min(b.l for b in london)
        manipulation = "YES" if (london_high > asian_high or london_low < asian_low) else "NO"
    else:
        manipulation = "NO"
    
    return {
        "open": day_open, "high": day_high, "low": day_low, "close": day_close,
        "trend": trend, "manipulation": manipulation, "asian_range": asian_high - asian_low,
        "bars": bars,
    }

def simulate_next_day(days_data, strategy="continuation"):
    """
    Strategies:
    - continuation: After 2 same-direction days, trade continuation next day
    - reversal: After manipulation day, trade reversal next day
    - breakout: After range day, trade breakout next day
    """
    equity = 100.0
    trades = []
    
    for i in range(2, len(days_data)):
        d0, d1, d2 = days_data[i-2], days_data[i-1], days_data[i]
        
        if strategy == "continuation":
            # After 2 UP days, buy next day open
            if d0["trend"] == "UP" and d1["trend"] == "UP":
                entry = d2["open"]
                sl = d1["low"] - 2.0
                tp = entry + (entry - sl) * 2
                
                if tp > entry:
                    # Check if next day hit TP or SL
                    for b in d2["bars"]:
                        if b.l <= sl:
                            pnl = -2.0 - COMMISSION * 0.01
                            trades.append({"pnl": pnl, "dir": "BUY", "reason": "SL", "date": d2["bars"][0].time[:10]})
                            equity += pnl
                            break
                        if b.h >= tp:
                            pnl = 4.0 - COMMISSION * 0.01
                            trades.append({"pnl": pnl, "dir": "BUY", "reason": "TP", "date": d2["bars"][0].time[:10]})
                            equity += pnl
                            break
                    else:
                        pnl = (d2["close"] - entry) - COMMISSION * 0.01
                        trades.append({"pnl": pnl, "dir": "BUY", "reason": "EOD", "date": d2["bars"][0].time[:10]})
                        equity += pnl
            
            # After 2 DOWN days, sell next day open
            if d0["trend"] == "DOWN" and d1["trend"] == "DOWN":
                entry = d2["open"]
                sl = d1["high"] + 2.0
                tp = entry - (sl - entry) * 2
                
                if tp < entry:
                    for b in d2["bars"]:
                        if b.h >= sl:
                            pnl = -2.0 - COMMISSION * 0.01
                            trades.append({"pnl": pnl, "dir": "SELL", "reason": "SL", "date": d2["bars"][0].time[:10]})
                            equity += pnl
                            break
                        if b.l <= tp:
                            pnl = 4.0 - COMMISSION * 0.01
                            trades.append({"pnl": pnl, "dir": "SELL", "reason": "TP", "date": d2["bars"][0].time[:10]})
                            equity += pnl
                            break
                    else:
                        pnl = (entry - d2["close"]) - COMMISSION * 0.01
                        trades.append({"pnl": pnl, "dir": "SELL", "reason": "EOD", "date": d2["bars"][0].time[:10]})
                        equity += pnl
        
        elif strategy == "reversal":
            # After manipulation day, trade reversal next day
            if d1["manipulation"] == "YES":
                # Determine direction based on last day's close relative to manipulation
                if d1["close"] > d1["open"]:
                    # Up manipulation → sell next day
                    entry = d2["open"]
                    sl = d1["high"] + 2.0
                    tp = entry - (sl - entry) * 2
                    
                    if tp < entry:
                        for b in d2["bars"]:
                            if b.h >= sl:
                                pnl = -2.0 - COMMISSION * 0.01
                                trades.append({"pnl": pnl, "dir": "SELL", "reason": "SL"})
                                equity += pnl
                                break
                            if b.l <= tp:
                                pnl = 4.0 - COMMISSION * 0.01
                                trades.append({"pnl": pnl, "dir": "SELL", "reason": "TP"})
                                equity += pnl
                                break
                        else:
                            pnl = (entry - d2["close"]) - COMMISSION * 0.01
                            trades.append({"pnl": pnl, "dir": "SELL", "reason": "EOD"})
                            equity += pnl
                else:
                    # Down manipulation → buy next day
                    entry = d2["open"]
                    sl = d1["low"] - 2.0
                    tp = entry + (entry - sl) * 2
                    
                    if tp > entry:
                        for b in d2["bars"]:
                            if b.l <= sl:
                                pnl = -2.0 - COMMISSION * 0.01
                                trades.append({"pnl": pnl, "dir": "BUY", "reason": "SL"})
                                equity += pnl
                                break
                            if b.h >= tp:
                                pnl = 4.0 - COMMISSION * 0.01
                                trades.append({"pnl": pnl, "dir": "BUY", "reason": "TP"})
                                equity += pnl
                                break
                        else:
                            pnl = (d2["close"] - entry) - COMMISSION * 0.01
                            trades.append({"pnl": pnl, "dir": "BUY", "reason": "EOD"})
                            equity += pnl
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": sum(t["pnl"] for t in trades), "equity": equity,
    }

def run():
    print("=" * 80)
    print(" NEXT-DAY PREDICTOR")
    print(" End-of-day pattern → next day trade")
    print("=" * 80)
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD"]:
        if "H1" not in data.get(sym, {}):
            continue
        
        h1 = data[sym]["H1"]
        
        # Group by day
        days_dict = defaultdict(list)
        for b in h1:
            days_dict[b.time[:10]].append(b)
        
        days = [analyze_day(days_dict[d]) for d in sorted(days_dict.keys())]
        days = [d for d in days if d]
        
        print(f"\n{sym}: {len(days)} trading days")
        
        for strategy in ["continuation", "reversal"]:
            r = simulate_next_day(days, strategy)
            print(f"\n  {strategy.upper()}:")
            print(f"    Trades: {r['trades']} | Wins: {r['wins']} | Losses: {r['losses']}")
            print(f"    Win Rate: {r['wr']:.1f}%")
            print(f"    P&L: ${r['pnl']:.2f} | Equity: ${r['equity']:.2f}")
    
    print(f"\n{'='*80}")

if __name__ == "__main__":
    run()
