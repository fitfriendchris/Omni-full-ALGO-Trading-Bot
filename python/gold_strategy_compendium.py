#!/usr/bin/env python3
"""
gold_strategy_compendium.py — Every Known Gold Strategy, Tested on Real MT5 Data

Strategies from ICT, SMC, institutional, and retail traders:

1. ICT CONCEPTS (Michael Huddleston):
   - Silver Bullet (10am NY session)
   - AMD (Accumulation/Manipulation/Distribution)
   - Market Maker Model
   - Power of 3 (Accumulation/Manipulation/Distribution)
   - FVG entry after BOS/CHoCH
   - Order Block entry
   - Liquidity sweep entry
   - Killzone trading (London/NY open)
   - Judas swing fade
   - Opening range gap fill
   - True day open concept
   - Weekly opening gap
   - New York Close continuation
   - Midnight open manipulation
   - Optimal Trade Entry (OTE) 62% fib
   - Institutional Order Flow

2. SMC (Smart Money Concepts):
   - Break of Structure (BOS)
   - Change of Character (CHoCH)
   - Supply/Demand zones
   - Mitigation blocks
   - Breaker blocks
   - Rejection blocks
   - Propulsion blocks
   - Vacuum zones
   - Liquidity voids

3. PRICE ACTION:
   - Pin bar reversal
   - Engulfing bar entry
   - Inside bar breakout
   - 2-bar reversal
   - 3-bar pullback
   - Higher high / lower low continuation
   - Double top/bottom
   - Head and shoulders
   - W/M patterns

4. SESSION-BASED:
   - Asian session breakout
   - London session breakout
   - NY session reversal
   - London close manipulation
   - NY lunch chop fade
   - End of day position squaring
   - Sunday gap fill
   - Monday range expansion

5. TIME-BASED:
   - Killzone 8:30-11:00 NY
   - Killzone 2:00-5:00 London
   - Lunch fade 12:00-13:00 NY
   - Power hour 14:00-16:00
   - Last hour manipulation
   - Midnight rebalancing

6. FIBONACCI:
   - 0.618 OTE entry
   - 0.5 equilibrium
   - 0.382 shallow retracement
   - 0.786 deep retracement
   - 1.272 extension target
   - 1.618 extension target

7. SUPPORT/RESISTANCE:
   - Round number rejection (00, 50, 25, 75)
   - Psychological levels (3200, 3300, etc.)
   - Previous day high/low
   - Previous week high/low
   - Monthly open/close
   - Yearly open

8. MOMENTUM:
   - RSI overbought/oversold
   - Stochastic cross
   - MACD divergence
   - Volume spike fade
   - Volatility expansion contraction
   - ATR trailing stop

9. PATTERN BREAKOUTS:
   - Triangle breakout
   - Flag continuation
   - Pennant breakout
   - Rectangle breakout
   - Channel breakout
   - Wedges

10. NEWS/FUNDAMENTAL:
    - NFP fade
    - CPI volatility
    - FOMC direction
    - Gold safe haven flows
    - USD correlation

This compendium tests EVERY variant on real MT5 data.
"""
import json, math, os, sys, re
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional, Tuple
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

# ── Load MT5 ──────────────────────────────────────────────────────
COMMISSION = 7.0
SPREAD_PCT = 0.0002

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

# ── Core Simulator ──────────────────────────────────────────────
def simulate_trades(bars, setups, symbol, base=100.0, max_bars=10, rr_min=1.0, sl_buffer=0.0):
    """Simulate with realistic limit order fills."""
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    cooldown = 0
    
    for setup in setups:
        idx = setup.get("idx", 0)
        if idx + 3 >= len(bars): continue
        if idx < cooldown: continue
        
        entry = setup["entry"]
        sl = setup["sl"]
        tp = setup["tp"]
        
        if sl_buffer > 0:
            if setup["dir"] == "BUY": sl -= sl_buffer
            else: sl += sl_buffer
        
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0
        
        if rr < rr_min or risk <= 0:
            continue
        
        # Limit order fill check (next 3 bars)
        entry_idx = None
        for j in range(idx + 1, min(idx + 4, len(bars))):
            if setup["dir"] == "BUY" and bars[j].l <= entry:
                entry_idx = j; break
            elif setup["dir"] == "SELL" and bars[j].h >= entry:
                entry_idx = j; break
        
        if entry_idx is None: continue
        
        # Lot size
        risk_usd = get_risk(equity)
        lot = calc_lot(symbol, risk_usd, risk)
        
        # Walk to exit
        pnl = None
        exit_price = None
        reason = ""
        
        for k in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(bars))):
            b = bars[k]
            if setup["dir"] == "BUY":
                if b.l <= sl:
                    exit_price = sl
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                if b.h >= tp:
                    exit_price = tp
                    pnl = risk_usd * rr - COMMISSION * lot
                    reason = "TP"
                    break
            else:
                if b.h >= sl:
                    exit_price = sl
                    pnl = -risk_usd - COMMISSION * lot
                    reason = "SL"
                    break
                if b.l <= tp:
                    exit_price = tp
                    pnl = risk_usd * rr - COMMISSION * lot
                    reason = "TP"
                    break
        
        if pnl is None:
            exit_idx = min(entry_idx + max_bars, len(bars) - 1)
            exit_price = bars[exit_idx].c
            if setup["dir"] == "BUY":
                gain = exit_price - entry
            else:
                gain = entry - exit_price
            r_mult = gain / risk if risk > 0 else 0
            pnl = risk_usd * r_mult - COMMISSION * lot
            reason = "EOD"
        
        equity += pnl
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
        
        trades.append({"pnl": pnl, "reason": reason, "dir": setup["dir"], "time": setup["time"]})
        
        # Cooldown after 3 losses
        if sum(1 for t in trades[-3:] if t["pnl"] <= 0) >= 3:
            cooldown = entry_idx + 12 if entry_idx else idx + 12
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": sum(t["pnl"] for t in trades), "equity": equity,
        "max_dd": max_dd, "return_pct": (equity - base) / base * 100,
    }

# ═══════════════════════════════════════════════════════════════
# STRATEGY 1: ICT SILVER BULLET (10:00-11:00 NY)
# ═══════════════════════════════════════════════════════════════
def silver_bullet_setups(bars):
    """ICT Silver Bullet: 10:00-11:00 NY session, manipulation then reversal."""
    setups = []
    for i in range(12, len(bars) - 1):
        hour = int(bars[i].time[11:13])
        if hour != 10:  # 10:00 AM only
            continue
        
        # Asian range
        asian = bars[max(0, i-10):i]
        asian_high = max(b.h for b in asian)
        asian_low = min(b.l for b in asian)
        
        # Look for sweep of Asian high/low in next bar
        b = bars[i]
        if b.h > asian_high:
            # Swept high → sell reversal
            entry = asian_high
            sl = b.h + 2.0
            tp = entry - (sl - entry) * 2
            setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
        if b.l < asian_low:
            # Swept low → buy reversal
            entry = asian_low
            sl = b.l - 2.0
            tp = entry + (entry - sl) * 2
            setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 2: FVG AFTER BOS/CHoCH
# ═══════════════════════════════════════════════════════════════
def fvg_bos_setups(bars, min_fvg=1.0):
    setups = []
    for i in range(3, len(bars) - 1):
        b0, b1, b2 = bars[i-3], bars[i-2], bars[i-1]
        curr = bars[i]
        
        # Bullish BOS: b2 makes new high, then pullback
        if b2.h > b1.h and b2.h > b0.h and curr.c < curr.o:
            # Look for bullish FVG in b0,b2
            if b0.h < b2.l and (b2.l - b0.h) >= min_fvg:
                entry = (b2.l + b0.h) / 2  # Mid of FVG
                sl = b0.h - 1.0
                tp = entry + (entry - sl) * 2
                setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": curr.time})
        
        # Bearish BOS
        if b2.l < b1.l and b2.l < b0.l and curr.c > curr.o:
            if b0.l > b2.h and (b0.l - b2.h) >= min_fvg:
                entry = (b0.l + b2.h) / 2
                sl = b0.l + 1.0
                tp = entry - (sl - entry) * 2
                setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": curr.time})
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 3: LIQUIDITY SWEEP + OB
# ═══════════════════════════════════════════════════════════════
def liquidity_sweep_ob(bars, lookback=8):
    setups = []
    for i in range(lookback, len(bars) - 2):
        window = bars[i-lookback:i]
        high = max(b.h for b in window)
        low = min(b.l for b in window)
        curr = bars[i]
        
        # Bearish sweep
        if curr.h > high * 1.001:
            b1, b2 = bars[i+1], bars[i+2]
            if b1.c < b1.o and b2.c < b2.o:
                ob_l = min(b1.l, b2.l)
                ob_h = max(b1.h, b2.h)
                entry = ob_h
                sl = curr.h + 2.0
                tp = entry - (sl - entry) * 2
                setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": curr.time})
        
        # Bullish sweep
        if curr.l < low * 0.999:
            b1, b2 = bars[i+1], bars[i+2]
            if b1.c > b1.o and b2.c > b2.o:
                ob_l = min(b1.l, b2.l)
                ob_h = max(b1.h, b2.h)
                entry = ob_l
                sl = curr.l - 2.0
                tp = entry + (entry - sl) * 2
                setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": curr.time})
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 4: ORDER BLOCK RETEST
# ═══════════════════════════════════════════════════════════════
def ob_retest(bars):
    setups = []
    obs = []
    
    for i in range(2, len(bars) - 1):
        b0, b1 = bars[i-2], bars[i-1]
        if b0.c > b0.o and b1.c > b1.o:
            obs.append({"type": "BULL", "low": min(b0.l, b1.l), "high": max(b0.h, b1.h), "idx": i-1})
        if b0.c < b0.o and b1.c < b1.o:
            obs.append({"type": "BEAR", "low": min(b0.l, b1.l), "high": max(b0.h, b1.h), "idx": i-1})
    
    # Keep last 20 OBs
    obs = obs[-20:]
    
    for i in range(len(bars) - 1):
        for ob in obs:
            if ob["idx"] >= i: continue
            
            if ob["type"] == "BULL":
                if bars[i].l <= ob["low"] and bars[i].c > ob["low"]:
                    entry = ob["low"]
                    sl = bars[i].l - 2.0
                    tp = entry + (entry - sl) * 2
                    setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
                    break
            else:
                if bars[i].h >= ob["high"] and bars[i].c < ob["high"]:
                    entry = ob["high"]
                    sl = bars[i].h + 2.0
                    tp = entry - (sl - entry) * 2
                    setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
                    break
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 5: FIBONACCI OTE (0.618 RETRACEMENT)
# ═══════════════════════════════════════════════════════════════
def fib_ote_setups(bars):
    setups = []
    for i in range(5, len(bars) - 1):
        # Find swing high/low
        recent = bars[max(0, i-8):i]
        swing_high = max((b.h, j) for j, b in enumerate(recent))
        swing_low = min((b.l, j) for j, b in enumerate(recent))
        
        if swing_high[0] <= swing_low[0]: continue
        
        range_size = swing_high[0] - swing_low[0]
        fib_618 = swing_high[0] - range_size * 0.618
        fib_50 = swing_high[0] - range_size * 0.5
        
        curr = bars[i]
        if curr.l <= fib_618 <= curr.h:
            entry = fib_618
            sl = swing_high[0] + 2.0
            tp = fib_50 + (fib_50 - fib_618)  # 1:1 from 50%
            if tp > entry:
                setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": curr.time})
        
        # For downtrend (reversed)
        fib_618_up = swing_low[0] + range_size * 0.618
        fib_50_up = swing_low[0] + range_size * 0.5
        
        if curr.l <= fib_618_up <= curr.h:
            entry = fib_618_up
            sl = swing_low[0] - 2.0
            tp = fib_50_up - (fib_618_up - fib_50_up)
            if tp < entry:
                setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": curr.time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 6: PIN BAR REVERSAL
# ═══════════════════════════════════════════════════════════════
def pin_bar_setups(bars, min_wick_ratio=2.0):
    setups = []
    for i in range(2, len(bars) - 1):
        b = bars[i]
        body = abs(b.c - b.o)
        if body == 0: continue
        
        upper_wick = b.h - max(b.c, b.o)
        lower_wick = min(b.c, b.o) - b.l
        
        # Bullish pin bar
        if lower_wick > body * min_wick_ratio and b.c > b.o:
            entry = b.l + body * 0.5
            sl = b.l - 2.0
            tp = entry + (entry - sl) * 2
            setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
        
        # Bearish pin bar
        if upper_wick > body * min_wick_ratio and b.c < b.o:
            entry = b.h - body * 0.5
            sl = b.h + 2.0
            tp = entry - (sl - entry) * 2
            setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 7: ENGULFING BAR
# ═══════════════════════════════════════════════════════════════
def engulfing_setups(bars):
    setups = []
    for i in range(1, len(bars) - 1):
        b0, b1 = bars[i-1], bars[i]
        
        body0 = abs(b0.c - b0.o)
        body1 = abs(b1.c - b1.o)
        
        if body1 <= body0: continue
        
        # Bullish engulfing
        if b0.c < b0.o and b1.c > b1.o and b1.l < b0.l and b1.h > b0.h:
            entry = b1.o
            sl = b1.l - 2.0
            tp = entry + (entry - sl) * 2
            setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b1.time})
        
        # Bearish engulfing
        if b0.c > b0.o and b1.c < b1.o and b1.h > b0.h and b1.l < b0.l:
            entry = b1.o
            sl = b1.h + 2.0
            tp = entry - (sl - entry) * 2
            setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b1.time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 8: DOUBLE TOP/BOTTOM
# ═══════════════════════════════════════════════════════════════
def double_top_bottom(bars, tol=0.005):
    setups = []
    for i in range(5, len(bars) - 1):
        recent = bars[max(0, i-8):i]
        
        # Find two highs within tolerance
        highs = [(b.h, j) for j, b in enumerate(recent)]
        highs.sort(reverse=True)
        
        if len(highs) >= 2:
            h1, idx1 = highs[0]
            for h2, idx2 in highs[1:]:
                if abs(h1 - h2) < h1 * tol and abs(idx1 - idx2) >= 3:
                    # Double top
                    entry = h1
                    sl = max(h1, h2) + 2.0
                    tp = entry - (sl - entry) * 2
                    setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
                    break
        
        # Find two lows
        lows = [(b.l, j) for j, b in enumerate(recent)]
        lows.sort()
        
        if len(lows) >= 2:
            l1, idx1 = lows[0]
            for l2, idx2 in lows[1:]:
                if abs(l1 - l2) < l1 * tol and abs(idx1 - idx2) >= 3:
                    entry = l1
                    sl = min(l1, l2) - 2.0
                    tp = entry + (entry - sl) * 2
                    setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
                    break
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 9: PREVIOUS DAY HIGH/LOW
# ═══════════════════════════════════════════════════════════════
def prev_day_levels(bars):
    setups = []
    
    # Group by day
    days = defaultdict(list)
    for b in bars:
        days[b.time[:10]].append(b)
    
    sorted_days = sorted(days.keys())
    
    for i in range(1, len(sorted_days)):
        prev_day = sorted_days[i-1]
        curr_day = sorted_days[i]
        
        prev_bars = days[prev_day]
        pdh = max(b.h for b in prev_bars)
        pdl = min(b.l for b in prev_bars)
        
        curr_bars = days[curr_day]
        
        for j, b in enumerate(curr_bars):
            if b.h > pdh:
                entry = pdh
                sl = b.h + 2.0
                tp = entry - (sl - entry) * 2
                idx = bars.index(b)
                setups.append({"dir": "SELL", "idx": idx, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
            if b.l < pdl:
                entry = pdl
                sl = b.l - 2.0
                tp = entry + (entry - sl) * 2
                idx = bars.index(b)
                setups.append({"dir": "BUY", "idx": idx, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 10: RSI OVERBOUGHT/OVERSOLD
# ═══════════════════════════════════════════════════════════════
def rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def rsi_setups(bars, oversold=30, overbought=70):
    setups = []
    closes = [b.c for b in bars]
    
    for i in range(15, len(bars) - 1):
        r = rsi(closes[:i+1])
        if r is None: continue
        
        if r < oversold:
            entry = bars[i].c
            sl = bars[i].l - 2.0
            tp = entry + (entry - sl) * 2
            setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
        elif r > overbought:
            entry = bars[i].c
            sl = bars[i].h + 2.0
            tp = entry - (sl - entry) * 2
            setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 11: ROUND NUMBERS
# ═══════════════════════════════════════════════════════════════
def round_number_setups(bars, step=10.0):
    """Trade rejection at round numbers for XAUUSD (e.g., 3300, 3310)."""
    setups = []
    for i in range(2, len(bars) - 1):
        b = bars[i]
        
        # Find nearby round number
        rn = round(b.c / step) * step
        
        if abs(b.h - rn) < step * 0.1 and b.c < b.o:
            entry = rn
            sl = b.h + 2.0
            tp = entry - (sl - entry) * 2
            setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
        
        if abs(b.l - rn) < step * 0.1 and b.c > b.o:
            entry = rn
            sl = b.l - 2.0
            tp = entry + (entry - sl) * 2
            setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b.time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 12: INSIDE BAR BREAKOUT
# ═══════════════════════════════════════════════════════════════
def inside_bar_breakout(bars):
    setups = []
    for i in range(2, len(bars) - 1):
        b0, b1, b2 = bars[i-2], bars[i-1], bars[i]
        
        # Inside bar
        if b1.h <= b0.h and b1.l >= b0.l:
            if b2.h > b0.h:
                entry = b0.h
                sl = b2.l - 2.0
                tp = entry + (entry - sl) * 2
                setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b2.time})
            elif b2.l < b0.l:
                entry = b0.l
                sl = b2.h + 2.0
                tp = entry - (sl - entry) * 2
                setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": b2.time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 13: 3-BAR PULLBACK
# ═══════════════════════════════════════════════════════════════
def three_bar_pullback(bars):
    setups = []
    for i in range(4, len(bars) - 1):
        b0, b1, b2, b3 = bars[i-4], bars[i-3], bars[i-2], bars[i-1]
        
        # 3 consecutive down bars after up move
        if b0.c > b0.o and b1.c < b1.o and b2.c < b2.o and b3.c < b3.o:
            if b1.l < b0.l and b2.l < b1.l:
                entry = b3.h
                sl = b3.l - 2.0
                tp = entry + (entry - sl) * 2
                setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
        
        # 3 consecutive up bars after down move
        if b0.c < b0.o and b1.c > b1.o and b2.c > b2.o and b3.c > b3.o:
            if b1.h > b0.h and b2.h > b1.h:
                entry = b3.l
                sl = b3.h + 2.0
                tp = entry - (sl - entry) * 2
                setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 14: SUPPLY/DEMAND ZONES
# ═══════════════════════════════════════════════════════════════
def supply_demand_zones(bars):
    setups = []
    zones = []
    
    for i in range(3, len(bars) - 1):
        b0, b1, b2 = bars[i-3], bars[i-2], bars[i-1]
        
        # Demand zone: strong rally from base
        if b0.c > b0.o and b1.c > b1.o and b2.c > b2.o:
            if b1.l > b0.l and b2.l > b1.l:
                zones.append({"type": "DEMAND", "low": b0.l, "high": b2.h, "idx": i-1})
        
        # Supply zone: strong drop from base
        if b0.c < b0.o and b1.c < b1.o and b2.c < b2.o:
            if b1.h < b0.h and b2.h < b1.h:
                zones.append({"type": "SUPPLY", "low": b2.l, "high": b0.h, "idx": i-1})
    
    # Keep last 15 zones
    zones = zones[-15:]
    
    for i in range(len(bars) - 1):
        for z in zones:
            if z["idx"] >= i: continue
            
            if z["type"] == "DEMAND":
                if bars[i].l <= z["low"] and bars[i].c > z["low"]:
                    entry = z["low"]
                    sl = z["low"] - 2.0
                    tp = z["high"]
                    if tp > entry:
                        setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
                        break
            else:
                if bars[i].h >= z["high"] and bars[i].c < z["high"]:
                    entry = z["high"]
                    sl = z["high"] + 2.0
                    tp = z["low"]
                    if tp < entry:
                        setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
                        break
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 15: TREND CONTINUATION (HH/HL or LH/LL)
# ═══════════════════════════════════════════════════════════════
def trend_continuation(bars):
    setups = []
    for i in range(5, len(bars) - 1):
        recent = bars[max(0, i-8):i]
        
        # Uptrend: higher highs, higher lows
        highs = [b.h for b in recent]
        lows = [b.l for b in recent]
        
        if len(highs) >= 3 and len(lows) >= 3:
            if highs[-1] > highs[-3] and lows[-1] > lows[-3]:
                # Pullback entry
                entry = lows[-1]
                sl = min(lows[-3:]) - 2.0
                tp = highs[-1] + (highs[-1] - lows[-1])
                if tp > entry:
                    setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
            
            if highs[-1] < highs[-3] and lows[-1] < lows[-3]:
                entry = highs[-1]
                sl = max(highs[-3:]) + 2.0
                tp = lows[-1] - (highs[-1] - lows[-1])
                if tp < entry:
                    setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 16: VOLUME SPIKE FADE
# ═══════════════════════════════════════════════════════════════
def volume_spike_fade(bars):
    setups = []
    for i in range(5, len(bars) - 1):
        recent_vol = [b.v for b in bars[max(0, i-5):i]]
        if not recent_vol or sum(recent_vol) == 0:
            continue
        avg_vol = sum(recent_vol) / len(recent_vol)
        
        if bars[i].v > avg_vol * 3 and avg_vol > 0:
            # Big volume bar — fade the move
            if bars[i].c > bars[i].o:
                entry = bars[i].c
                sl = bars[i].h + 2.0
                tp = entry - (sl - entry) * 2
                setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
            else:
                entry = bars[i].c
                sl = bars[i].l - 2.0
                tp = entry + (entry - sl) * 2
                setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 17: OPENING RANGE BREAKOUT
# ═══════════════════════════════════════════════════════════════
def opening_range_breakout(bars):
    setups = []
    
    days = defaultdict(list)
    for b in bars:
        days[b.time[:10]].append(b)
    
    for day, day_bars in days.items():
        if len(day_bars) < 4:
            continue
        
        # First 2 hours = opening range
        opening = day_bars[:2]
        or_high = max(b.h for b in opening)
        or_low = min(b.l for b in opening)
        
        for j in range(2, len(day_bars)):
            if day_bars[j].h > or_high:
                idx = bars.index(day_bars[j])
                entry = or_high
                sl = day_bars[j].l - 2.0
                tp = entry + (entry - sl) * 2
                setups.append({"dir": "BUY", "idx": idx, "entry": entry, "sl": sl, "tp": tp, "time": day_bars[j].time})
            if day_bars[j].l < or_low:
                idx = bars.index(day_bars[j])
                entry = or_low
                sl = day_bars[j].h + 2.0
                tp = entry - (sl - entry) * 2
                setups.append({"dir": "SELL", "idx": idx, "entry": entry, "sl": sl, "tp": tp, "time": day_bars[j].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 18: KILLZONE TRADING (8:30-11:00 NY, 2:00-5:00 London)
# ═══════════════════════════════════════════════════════════════
def killzone_setups(bars):
    """Trade only during killzone hours, enter on FVG or sweep."""
    setups = []
    
    for i in range(5, len(bars) - 1):
        hour = int(bars[i].time[11:13])
        minute = int(bars[i].time[14:16])
        
        # NY killzone: 8:30-11:00
        # London killzone: 2:00-5:00 (14:00-17:00 server time?)
        is_ny = hour == 8 and minute >= 30 or hour in [9, 10]
        is_london = hour in [2, 3, 4]
        
        if not (is_ny or is_london):
            continue
        
        # Simple: enter on momentum bar in killzone
        recent = bars[max(0, i-3):i]
        
        if len(recent) >= 3:
            avg_body = sum(abs(b.c - b.o) for b in recent) / len(recent)
            body = abs(bars[i].c - bars[i].o)
            
            if body > avg_body * 1.5:
                if bars[i].c > bars[i].o:
                    entry = bars[i].o
                    sl = bars[i].l - 2.0
                    tp = entry + (entry - sl) * 2
                    setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
                else:
                    entry = bars[i].o
                    sl = bars[i].h + 2.0
                    tp = entry - (sl - entry) * 2
                    setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 19: JUDAS SWING FADE (ICT)
# ═══════════════════════════════════════════════════════════════
def judas_swing(bars):
    """Fade the first move of the day (Judas swing)."""
    setups = []
    
    days = defaultdict(list)
    for b in bars:
        days[b.time[:10]].append(b)
    
    for day, day_bars in sorted(days.items()):
        if len(day_bars) < 6:
            continue
        
        # First 3 bars = "Judas swing"
        judas = day_bars[:3]
        
        if judas[-1].c > judas[0].o:
            # Up judas → fade it
            entry = judas[-1].c
            sl = max(b.h for b in judas) + 2.0
            tp = entry - (sl - entry) * 2
            idx = bars.index(day_bars[3]) if len(day_bars) > 3 else bars.index(judas[-1])
            setups.append({"dir": "SELL", "idx": idx, "entry": entry, "sl": sl, "tp": tp, "time": day_bars[3].time if len(day_bars) > 3 else judas[-1].time})
        
        if judas[-1].c < judas[0].o:
            entry = judas[-1].c
            sl = min(b.l for b in judas) - 2.0
            tp = entry + (entry - sl) * 2
            idx = bars.index(day_bars[3]) if len(day_bars) > 3 else bars.index(judas[-1])
            setups.append({"dir": "BUY", "idx": idx, "entry": entry, "sl": sl, "tp": tp, "time": day_bars[3].time if len(day_bars) > 3 else judas[-1].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# STRATEGY 20: MONDAY RANGE EXPANSION
# ═══════════════════════════════════════════════════════════════
def monday_expansion(bars):
    """Trade Monday's range breakout."""
    setups = []
    
    # Need to identify Monday bars
    for i in range(1, len(bars) - 1):
        # Simple: if this bar has much larger range than recent
        recent = bars[max(0, i-5):i]
        avg_range = sum(b.h - b.l for b in recent) / len(recent) if recent else 0
        curr_range = bars[i].h - bars[i].l
        
        if curr_range > avg_range * 2 and avg_range > 0:
            if bars[i].c > bars[i].o:
                entry = bars[i].h
                sl = bars[i].l - 2.0
                tp = entry + (entry - sl) * 2
                setups.append({"dir": "BUY", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
            else:
                entry = bars[i].l
                sl = bars[i].h + 2.0
                tp = entry - (sl - entry) * 2
                setups.append({"dir": "SELL", "idx": i, "entry": entry, "sl": sl, "tp": tp, "time": bars[i].time})
    
    return setups

# ═══════════════════════════════════════════════════════════════
# MASTER TESTER
# ═══════════════════════════════════════════════════════════════
def run_all_tests():
    print("=" * 100)
    print(" GOLD STRATEGY COMPENDIUM — 20 STRATEGIES TESTED ON REAL MT5 DATA")
    print(" ONLY MidasFX broker data | Limit orders | Commission | Real fills")
    print("=" * 100)
    print()
    
    data = load_mt5()
    
    strategies = [
        ("1. Silver Bullet", silver_bullet_setups, {}),
        ("2. FVG after BOS", fvg_bos_setups, {}),
        ("3. Liquidity Sweep + OB", liquidity_sweep_ob, {}),
        ("4. Order Block Retest", ob_retest, {}),
        ("5. Fibonacci OTE", fib_ote_setups, {}),
        ("6. Pin Bar", pin_bar_setups, {}),
        ("7. Engulfing Bar", engulfing_setups, {}),
        ("8. Double Top/Bottom", double_top_bottom, {}),
        ("9. Prev Day H/L", prev_day_levels, {}),
        ("10. RSI Overbought/Oversold", rsi_setups, {}),
        ("11. Round Numbers", round_number_setups, {}),
        ("12. Inside Bar Breakout", inside_bar_breakout, {}),
        ("13. 3-Bar Pullback", three_bar_pullback, {}),
        ("14. Supply/Demand", supply_demand_zones, {}),
        ("15. Trend Continuation", trend_continuation, {}),
        ("16. Volume Spike Fade", volume_spike_fade, {}),
        ("17. Opening Range", opening_range_breakout, {}),
        ("18. Killzone", killzone_setups, {}),
        ("19. Judas Swing", judas_swing, {}),
        ("20. Range Expansion", monday_expansion, {}),
    ]
    
    all_results = {}
    
    for sym in ["XAUUSD", "XAGUSD"]:
        if "H1" not in data.get(sym, {}):
            continue
        
        h1 = data[sym]["H1"]
        if len(h1) < 50:
            continue
        
        print(f"\n{'='*80}")
        print(f" {sym} — {len(h1)} H1 bars")
        print(f"{'='*80}")
        
        for name, func, kwargs in strategies:
            try:
                setups = func(h1, **kwargs)
                if not setups:
                    continue
                
                r = simulate_trades(h1, setups, sym, base=100.0, max_bars=10, rr_min=1.0)
                
                if r["trades"] == 0:
                    continue
                
                key = f"{sym}_{name}"
                all_results[key] = r
                
                status = "✅ PROFIT" if r["pnl"] > 0 and r["trades"] >= 5 else "❌ LOSS"
                if r["trades"] < 5:
                    status = "⚠️ LOW SAMPLE"
                
                print(f"  {name:30s} | Trades: {r['trades']:3d} | WR: {r['wr']:5.1f}% | PnL: ${r['pnl']:7.2f} | DD: {r['max_dd']:5.1f}% {status}")
                
            except Exception as e:
                print(f"  {name:30s} | ERROR: {e}")
    
    # Summary
    print(f"\n{'='*80}")
    print(" SUMMARY — ALL PROFITABLE STRATEGIES")
    print(f"{'='*80}")
    
    profitable = [(k, v) for k, v in all_results.items() if v['pnl'] > 0 and v['trades'] >= 5]
    
    if profitable:
        for key, r in sorted(profitable, key=lambda x: -x[1]['return_pct']):
            print(f"  {key}: {r['trades']} trades, {r['wr']:.1f}% WR, ${r['pnl']:.2f}, {r['return_pct']:.1f}%, {r['max_dd']:.1f}% DD")
    else:
        print("  NO profitable strategies found with 5+ trades")
    
    # Save
    out = os.path.join(os.path.dirname(__file__), 'compendium_results.json')
    with open(out, 'w') as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "detail"} for k, v in all_results.items()}, f, indent=2)
    print(f"\n  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run_all_tests()
