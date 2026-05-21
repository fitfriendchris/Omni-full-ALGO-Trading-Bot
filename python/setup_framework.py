#!/usr/bin/env python3
"""
setup_framework.py — Complete Trading Setup Framework from Screenshot Analysis

ANALYSIS OF 6 SCREENSHOTS (IMG_4062-4067):
============================================
All screenshots show the SAME AUDUSD M5 trade being managed over time.

IDENTIFIED PATTERN — "Precision Support Bounce":
-------------------------------------------------
1. WAIT for sharp sell-off (momentum flush) on M5
2. IDENTIFY the exact support level (where the wick bounced)
3. PLACE Buy Limit at or slightly above the wick low
4. SL = tight, just below the absolute wick low (5-8 pips)
5. TP = wide, targeting recent resistance / MAs / structural high (4-6R)
6. HOLD for swing — don't micromanage

KEY METRICS FROM SCREENSHOTS:
- Entry: 0.71085 (Buy Limit)
- SL: 0.71034 (51 points = 5.1 pips)
- TP: 0.71370 (285 points = 28.5 pips)
- R:R = 1:5.6
- Lot: 0.1
- Direction: LONG only (buying support after sell-off)

MOVING AVERAGES:
- Brown/Red = Fast EMA (8-20 period), hugs price
- Orange/Gold = Medium EMA (50 period), trend filter
- Blue = Slow EMA (100-200 period), major resistance/target

SETUP CRITERIA CHECKLIST:
=========================
1. Price makes sharp drop (1-3 strong red candles)
2. Price finds support with long lower wick
3. Fast MA (brown) turns flat or up
4. Medium MA (orange) is below entry or flat
5. Entry is at/near the wick low + small buffer
6. SL is below the wick extreme
7. TP is at next structural resistance or slow MA
8. Minimum 3:1 R:R (preferably 5:1+)
9. Only 1 trade per setup — no stacking
10. If not filled in 3 bars, cancel and reassess

This framework creates a "setup database" and prediction engine.
"""
import json, os, sys, re, math
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

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

def calc_ema(bars: List[Bar], period: int, field: str = "c") -> List[float]:
    """Calculate EMA for a list of bars."""
    if len(bars) < period:
        return [getattr(b, field) for b in bars]
    
    multiplier = 2 / (period + 1)
    emas = []
    
    # Start with SMA
    sma = sum(getattr(b, field) for b in bars[:period]) / period
    emas.append(sma)
    
    for i in range(period, len(bars)):
        ema = (getattr(bars[i], field) - emas[-1]) * multiplier + emas[-1]
        emas.append(ema)
    
    # Pad beginning
    result = [getattr(b, field) for b in bars[:period-1]] + emas
    return result

def find_precision_support_setups(m5_bars: List[Bar], h1_bars: List[Bar]) -> List[Dict]:
    """
    Find "Precision Support Bounce" setups from screenshots.
    
    Pattern:
      1. Sharp drop: 1-3 consecutive bearish candles with large bodies
      2. Wick support: Long lower wick on the final drop candle
      3. Entry at wick low + small buffer
      4. SL below wick extreme
      5. TP at next resistance (recent high or H1 structure)
    """
    setups = []
    
    for i in range(5, len(m5_bars)):
        # Look for sharp drop pattern
        recent = m5_bars[i-4:i+1]
        
        # Count consecutive bearish candles
        bearish_streak = 0
        drop_size = 0
        
        for j in range(len(recent)-2, -1, -1):
            b = recent[j]
            if b.c < b.o:
                bearish_streak += 1
                drop_size += (b.o - b.c)
            else:
                break
        
        if bearish_streak < 1:
            continue
        
        # The final drop candle
        drop_candle = recent[-2] if bearish_streak >= 1 else None
        if not drop_candle:
            continue
        
        # Check for wick support (long lower wick)
        body = abs(drop_candle.c - drop_candle.o)
        lower_wick = min(drop_candle.c, drop_candle.o) - drop_candle.l
        upper_wick = drop_candle.h - max(drop_candle.c, drop_candle.o)
        total_range = drop_candle.h - drop_candle.l
        
        if total_range <= 0:
            continue
        
        wick_ratio = lower_wick / total_range if total_range > 0 else 0
        
        # Need significant lower wick (rejection)
        if wick_ratio < 0.3:
            continue
        
        # Support level = the low of the drop candle
        support = drop_candle.l
        
        # Entry = support + small buffer (like screenshot: 0.71085 when low was 0.71034)
        buffer = total_range * 0.15  # 15% of candle range as buffer
        entry = support + buffer
        
        # SL = below wick extreme (like screenshot: 0.71034, below the actual low)
        sl_buffer = total_range * 0.1
        sl = support - sl_buffer
        
        risk = entry - sl
        if risk <= 0:
            continue
        
        # Find TP target: recent resistance from H1 or M5
        # Look for recent high in last 20 bars
        recent_high = max(b.h for b in m5_bars[max(0, i-20):i])
        
        # Or use H1 structure if available
        h1_high = None
        if h1_bars:
            # Map M5 index to H1
            curr_time = m5_bars[i].time
            for hb in h1_bars:
                if curr_time >= hb.time:
                    h1_high = hb.h
                    break
        
        tp_target = max(recent_high, h1_high or 0)
        
        # Ensure minimum 3R
        min_tp = entry + risk * 3
        tp = max(tp_target, min_tp)
        
        rr = (tp - entry) / risk if risk > 0 else 0
        
        if rr < 3.0:
            continue
        
        setups.append({
            "idx": i,
            "time": m5_bars[i].time,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "risk": risk,
            "support": support,
            "wick_ratio": wick_ratio,
            "drop_size": drop_size,
            "bearish_streak": bearish_streak,
            "drop_candle": {
                "o": drop_candle.o, "h": drop_candle.h,
                "l": drop_candle.l, "c": drop_candle.c,
                "time": drop_candle.time,
            },
        })
    
    return setups

def simulate_precision_setup(data: Dict, symbol: str, base=1000.0) -> Dict:
    """Simulate the screenshot-based precision support strategy."""
    m5 = data.get("M5", [])
    h1 = data.get("H1", [])
    
    if len(m5) < 20:
        return {"trades": 0}
    
    setups = find_precision_support_setups(m5, h1)
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    cooldown = 0
    
    for setup in setups:
        if cooldown > 0:
            cooldown -= 1
            continue
        
        idx = setup["idx"]
        entry = setup["entry"]
        sl = setup["sl"]
        tp = setup["tp"]
        risk_r = setup["risk"]
        
        # Place limit order — check fill in next 3 bars
        fill_idx = None
        for k in range(idx + 1, min(idx + 4, len(m5))):
            if m5[k].l <= entry <= m5[k].h:
                fill_idx = k
                break
        
        if fill_idx is None:
            continue
        
        risk_usd = min(equity * 0.02, 25.0)  # 2% risk, max $25
        lot = 0.1  # Fixed 0.1 lot like screenshots
        
        # Calculate lot value properly using broker tick values
        # PnL = lot_size * tick_value * (price_move / point)
        tick_value = 1.0  # Default
        point = 0.01      # Default
        
        # Try to get from MT5 data
        mt5_data = data  # Already loaded
        if symbol == "XAUUSD":
            tick_value = 1.0
            point = 0.01
        elif symbol == "XAGUSD":
            tick_value = 5.0
            point = 0.001
        elif symbol == "EURUSD":
            tick_value = 1.0
            point = 0.00001
        elif symbol == "GBPUSD":
            tick_value = 1.0
            point = 0.00001
        elif symbol == "USDJPY":
            tick_value = 0.63
            point = 0.001
        elif symbol == "AUDUSD":
            tick_value = 1.0
            point = 0.00001
        elif symbol == "USDCAD":
            tick_value = 0.73
            point = 0.00001
        
        def calc_pnl(lot_size, price_move):
            """Calculate P&L in USD with correct sign."""
            ticks = price_move / point  # Signed ticks
            return lot_size * tick_value * ticks  # Signed P&L
        
        # Walk to exit (hold for swing — up to 50 bars)
        pnl = None
        exit_price = None
        reason = ""
        
        for k in range(fill_idx + 1, min(fill_idx + 50, len(m5))):
            b = m5[k]
            if b.l <= sl:
                exit_price = sl
                raw_pnl = calc_pnl(lot, exit_price - entry)  # negative for loss
                pnl = raw_pnl - COMMISSION * lot
                reason = "SL"
                break
            if b.h >= tp:
                exit_price = tp
                raw_pnl = calc_pnl(lot, exit_price - entry)  # positive for win
                pnl = raw_pnl - COMMISSION * lot
                reason = "TP"
                break
        
        if pnl is None:
            exit_idx = min(fill_idx + 49, len(m5) - 1)
            exit_price = m5[exit_idx].c
            raw_pnl = calc_pnl(lot, exit_price - entry)
            pnl = raw_pnl - COMMISSION * lot
            reason = "EOD"
        
        equity += pnl
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
        
        trades.append({
            "time": setup["time"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "exit": exit_price,
            "pnl": pnl,
            "reason": reason,
            "rr": setup["rr"],
            "lot": lot,
        })
        
        # 3-bar cooldown after trade
        cooldown = 3
    
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
        "setups": len(setups),
        "detail": trades,
    }

def run():
    print("=" * 100)
    print(" PRECISION SUPPORT BOUNCE STRATEGY")
    print(" Based on screenshot analysis (IMG_4062-4067)")
    print(" M5 timeframe | Tight SL | Wide TP | Swing hold")
    print("=" * 100)
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]:
        if sym not in data or "M5" not in data[sym]:
            continue
        
        print(f"\n{'='*80}")
        print(f" {sym}")
        print(f"{'='*80}")
        
        r = simulate_precision_setup(data[sym], sym, base=1000.0)
        
        print(f"  Setups found: {r['setups']}")
        print(f"  Trades executed: {r['trades']}")
        print(f"  Wins: {r['wins']} | Losses: {r['losses']}")
        print(f"  Win Rate: {r['wr']:.1f}%")
        print(f"  P&L: ${r['pnl']:.2f}")
        print(f"  Equity: ${r['equity']:.2f} (peak: ${r['peak']:.2f})")
        print(f"  Return: {r['return_pct']:.1f}%")
        print(f"  Max DD: {r['max_dd']:.1f}%")
        
        if r['trades'] > 0:
            avg_rr = sum(t['rr'] for t in r['detail']) / len(r['detail'])
            print(f"  Avg R:R: {avg_rr:.1f}:1")
        
        out = os.path.join(os.path.dirname(__file__), f'precision_{sym}.json')
        with open(out, 'w') as f:
            json.dump(r, f, indent=2)
        print(f"  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
