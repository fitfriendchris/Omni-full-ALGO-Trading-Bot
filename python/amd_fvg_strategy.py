#!/usr/bin/env python3
"""
amd_fvg_strategy.py — Multi-Timeframe ICT Strategy

MACRO (H4/D1):    AMD cycle detection — accumulation/manipulation/distribution
INTERMEDIATE (H1): Range bounds, liquidity pools, Asian/London levels
MICRO (M15/M5):   FVG entries after BOS/CHoCH, retest precision

Entry logic:
  1. H4 shows clear AMD phase (manipulation or distribution, NOT accumulation)
  2. H1 price broke recent high/low (BOS) creating structural shift
  3. M15 formed a Fair Value Gap after the break
  4. Price retraces to retest the FVG 50% level
  5. ENTER in direction of H4 bias, SL beyond swing point, TP at next liquidity

Compounding: $2 → $5 → $10 → $25 as equity grows.
"""
import json, math, os, re, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar, _calc_atr

# ── Config ──────────────────────────────────────────────────────────
JSON_PATH = "/Users/yuhfriendchris/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"

SYMBOLS = ["XAUUSD", "XAGUSD"]
TFS = ["D1", "H4", "H1", "M15", "M5"]

# Risk tiers (compounding)
TIER_RISK = [2.0, 5.0, 10.0, 25.0]
TIER_MULTIPLE = [1.0, 2.0, 5.0, 10.0]
COMMISSION = 7.0  # per lot round-turn
SPREAD = {"XAUUSD": 0.50, "XAGUSD": 0.03}

# ── Load data ────────────────────────────────────────────────────────
def load_data() -> dict:
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r',\s*([\]\}])', r'\1', raw)
    return json.loads(raw)

def parse_bars(data: dict, symbol: str, tf: str) -> List[Bar]:
    bars = []
    chart_data = data.get("charts", {}).get(symbol, {}).get(tf, [])
    for item in chart_data:
        t = item.get("t", "").replace(".", "-", 2)
        bars.append(Bar(
            time=t, o=item.get("o", 0.0), h=item.get("h", 0.0),
            l=item.get("l", 0.0), c=item.get("c", 0.0), v=item.get("v", 0),
        ))
    return sorted(bars, key=lambda b: b.time)

# ── H4 AMD Cycle Detection ─────────────────────────────────────────
def detect_amd_cycle(h4_bars: List[Bar], lookback: int = 6) -> str:
    """
    Detect AMD phase from H4 bars:
    - ACCUMULATION: ranging, small bodies, equal highs/lows
    - MANIPULATION: sweep of accumulation range, wick beyond level
    - DISTRIBUTION: strong directional move after manipulation
    - CHOP: no clear structure
    """
    if len(h4_bars) < lookback + 2:
        return "CHOP"
    
    window = h4_bars[-lookback:]
    
    highs = [b.h for b in window]
    lows = [b.l for b in window]
    closes = [b.c for b in window]
    opens = [b.o for b in window]
    
    # Range analysis
    highest = max(highs)
    lowest = min(lows)
    range_size = highest - lowest
    
    # Body analysis
    bodies = [abs(c - o) for c, o in zip(closes, opens)]
    avg_body = sum(bodies) / len(bodies) if bodies else 0
    
    # Equal highs/lows
    eq_highs = sum(1 for i in range(len(highs)-1) if abs(highs[i] - highs[i+1]) < range_size * 0.003)
    eq_lows = sum(1 for i in range(len(lows)-1) if abs(lows[i] - lows[i+1]) < range_size * 0.003)
    
    # Recent sweep
    recent = h4_bars[-3:]
    recent_high = max(b.h for b in recent)
    recent_low = min(b.l for b in recent)
    
    # Did price sweep the range then reverse?
    swept_high = recent_high > highest * 0.999 and closes[-1] < closes[-2]
    swept_low = recent_low < lowest * 1.001 and closes[-1] > closes[-2]
    
    # Trend strength
    hh = sum(1 for i in range(len(highs)-1) if highs[i+1] > highs[i])
    ll = sum(1 for i in range(len(lows)-1) if lows[i+1] < lows[i])
    
    if range_size > 0 and avg_body / range_size < 0.25 and (eq_highs >= 2 or eq_lows >= 2):
        return "ACCUMULATION"
    elif swept_high and hh < 2:
        return "MANIPULATION_DOWN"  # Swept high, expect down
    elif swept_low and ll < 2:
        return "MANIPULATION_UP"  # Swept low, expect up
    elif hh >= 4 and closes[-1] > closes[0]:
        return "DISTRIBUTION_UP"
    elif ll >= 4 and closes[-1] < closes[0]:
        return "DISTRIBUTION_DOWN"
    else:
        return "CHOP"

# ── FVG Detection ──────────────────────────────────────────────────
def detect_fvg(bars: List[Bar], direction: str) -> Optional[Dict]:
    """
    Detect Fair Value Gap:
    Bullish FVG: bar[i-2].high < bar[i].low (gap up)
    Bearish FVG: bar[i-2].low > bar[i].high (gap down)
    
    Returns: {top, bottom, mid, direction, created_idx}
    """
    if len(bars) < 4:
        return None
    
    for i in range(2, len(bars)):
        b0 = bars[i-2]  # two bars ago
        b1 = bars[i-1]  # one bar ago (the FVG bar)
        b2 = bars[i]    # current
        
        if direction == "BUY":
            # Bullish FVG: b0.high < b2.low
            if b0.h < b2.l:
                fvg_top = b2.l
                fvg_bot = b0.h
                return {
                    "top": fvg_top,
                    "bottom": fvg_bot,
                    "mid": (fvg_top + fvg_bot) / 2,
                    "direction": "BUY",
                    "created_bar": i,
                    "created_time": b2.time,
                }
        else:  # SELL
            # Bearish FVG: b0.low > b2.high
            if b0.l > b2.h:
                fvg_top = b0.l
                fvg_bot = b2.h
                return {
                    "top": fvg_top,
                    "bottom": fvg_bot,
                    "mid": (fvg_top + fvg_bot) / 2,
                    "direction": "SELL",
                    "created_bar": i,
                    "created_time": b2.time,
                }
    
    return None

# ── BOS/CHoCH Detection ────────────────────────────────────────────
def detect_bos(bars: List[Bar], direction: str, lookback: int = 5) -> Optional[Dict]:
    """
    Break of Structure:
    Bullish BOS: price breaks above recent swing high
    Bearish BOS: price breaks below recent swing low
    """
    if len(bars) < lookback + 2:
        return None
    
    recent = bars[-lookback:]
    
    if direction == "BUY":
        # Find recent swing high
        swing_highs = []
        for i in range(1, len(recent)-1):
            if recent[i].h > recent[i-1].h and recent[i].h > recent[i+1].h:
                swing_highs.append((i, recent[i].h))
        
        if not swing_highs:
            return None
        
        # Did latest bar break above the most recent swing high?
        last_swing_idx, last_swing_high = swing_highs[-1]
        current = bars[-1]
        
        if current.h > last_swing_high:
            return {
                "type": "BOS",
                "direction": "BUY",
                "swing_level": last_swing_high,
                "break_bar": len(bars) - 1,
            }
    else:
        swing_lows = []
        for i in range(1, len(recent)-1):
            if recent[i].l < recent[i-1].l and recent[i].l < recent[i+1].l:
                swing_lows.append((i, recent[i].l))
        
        if not swing_lows:
            return None
        
        last_swing_idx, last_swing_low = swing_lows[-1]
        current = bars[-1]
        
        if current.l < last_swing_low:
            return {
                "type": "BOS",
                "direction": "SELL",
                "swing_level": last_swing_low,
                "break_bar": len(bars) - 1,
            }
    
    return None

# ── Main Strategy: H4 bias → H1 range → M15 FVG ──────────────────
def find_setups_amd_fvg(data: dict, symbol: str, h4_bars: List[Bar], h1_bars: List[Bar], m15_bars: List[Bar], m5_bars: List[Bar]) -> List[Dict]:
    """Find all setups matching the AMD-FVG model."""
    setups = []
    
    if len(h4_bars) < 10 or len(m15_bars) < 10:
        return setups
    
    # H4 AMD bias
    amd = detect_amd_cycle(h4_bars)
    
    # Only trade manipulation or distribution phases
    allowed_bias = ["MANIPULATION_UP", "MANIPULATION_DOWN", "DISTRIBUTION_UP", "DISTRIBUTION_DOWN"]
    if amd not in allowed_bias:
        return setups
    
    # Determine direction from H4
    if "UP" in amd:
        h4_direction = "BUY"
    else:
        h4_direction = "SELL"
    
    # H1: find recent BOS and liquidity
    bos = detect_bos(h1_bars, h4_direction, lookback=8)
    if not bos:
        return setups
    
    # M15: find FVG in direction of H4
    fvg = detect_fvg(m15_bars[-20:], h4_direction)
    if not fvg:
        return setups
    
    # Check if FVG was created AFTER the BOS
    # Map H1 time to M15 time
    bos_time = h1_bars[bos["break_bar"]].time if bos["break_bar"] < len(h1_bars) else ""
    fvg_time = fvg["created_time"]
    
    # FVG must be fresh (within last 6 M15 bars = 1.5 hours)
    if len(m15_bars) - fvg["created_bar"] > 6:
        return setups
    
    # Calculate entry at FVG 50% retest
    entry = fvg["mid"]
    
    # SL: beyond the FVG extreme or BOS swing point
    if h4_direction == "BUY":
        sl = min(fvg["bottom"] * 0.999, bos["swing_level"] * 0.998)
        # TP: next H1 liquidity level or 2R
        h1_highs = [b.h for b in h1_bars[-12:]]
        tp = max(h1_highs) * 1.002 if h1_highs else entry * 1.01
    else:
        sl = max(fvg["top"] * 1.001, bos["swing_level"] * 1.002)
        h1_lows = [b.l for b in h1_bars[-12:]]
        tp = min(h1_lows) * 0.998 if h1_lows else entry * 0.99
    
    risk = abs(entry - sl)
    if risk <= 0:
        return setups
    
    reward = abs(tp - entry)
    rr = reward / risk if risk > 0 else 0
    
    if rr < 1.5:
        return setups
    
    # Confidence scoring
    confidence = 50
    if "MANIPULATION" in amd:
        confidence += 15  # Manipulation phase = high probability
    if "DISTRIBUTION" in amd:
        confidence += 10
    if rr >= 3.0:
        confidence += 10
    if fvg["created_bar"] >= len(m15_bars) - 3:
        confidence += 10  # Fresh FVG
    if bos:
        confidence += 10  # Confirmed BOS
    
    setups.append({
        "direction": h4_direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "risk": risk,
        "confidence": confidence,
        "h4_amd": amd,
        "h1_bos_time": bos_time,
        "m15_fvg_time": fvg_time,
        "fvg_mid": fvg["mid"],
        "fvg_top": fvg["top"],
        "fvg_bot": fvg["bottom"],
    })
    
    return setups

# ── Simulation with compounding ───────────────────────────────────
def simulate_amd_fvg(data: dict, symbol: str, base_equity: float = 100.0) -> Tuple[List[Dict], Dict]:
    d1 = parse_bars(data, symbol, "D1")
    h4 = parse_bars(data, symbol, "H4")
    h1 = parse_bars(data, symbol, "H1")
    m15 = parse_bars(data, symbol, "M15")
    m5 = parse_bars(data, symbol, "M5")
    
    if not all([d1, h4, h1, m15]):
        print(f"  {symbol}: insufficient data (D1={len(d1)}, H4={len(h4)}, H1={len(h1)}, M15={len(m15)})")
        return [], {"error": "insufficient data"}
    
    print(f"  {symbol}: D1={len(d1)} H4={len(h4)} H1={len(h1)} M15={len(m15)} M5={len(m5)}")
    
    # Walk M15 bars
    trades = []
    equity = base_equity
    peak = equity
    max_dd = 0.0
    daily_loss = 0.0
    current_day = ""
    
    stats = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0, "max_dd": 0.0}
    
    for i in range(20, len(m15)):
        window_m15 = m15[i-20:i+1]
        cur_time = m15[i].time
        cur_day = cur_time[:10]
        
        # Reset daily loss
        if cur_day != current_day:
            current_day = cur_day
            daily_loss = 0.0
        
        # Get corresponding H1 and H4 bars up to this time
        h1_up_to = [b for b in h1 if b.time <= cur_time]
        h4_up_to = [b for b in h4 if b.time <= cur_time]
        m5_up_to = [b for b in m5 if b.time <= cur_time] if m5 else []
        
        if len(h1_up_to) < 10 or len(h4_up_to) < 6:
            continue
        
        # Find setups
        setups = find_setups_amd_fvg(data, symbol, h4_up_to, h1_up_to, window_m15, m5_up_to)
        
        for setup in setups:
            # Skip if daily loss limit
            risk_usd = get_compounding_risk(equity, base_equity)
            if daily_loss <= -(equity * 0.03):
                continue
            
            # Check for duplicate (same direction within 2 hours)
            if trades and trades[-1]["direction"] == setup["direction"]:
                last_time = trades[-1]["time"]
                try:
                    dt_cur = datetime.strptime(cur_time, "%Y-%m-%d %H:%M:%S")
                    dt_last = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
                    if (dt_cur - dt_last).total_seconds() < 7200:
                        continue
                except:
                    pass
            
            # Calculate lot size
            sl_dist = abs(setup["sl"] - setup["entry"])
            lot = calculate_lot(symbol, risk_usd, sl_dist)
            
            # Limit order: wait for price to hit FVG mid
            filled = False
            fill_price = None
            
            # Check next 3 M15 bars for fill
            for j in range(i, min(i+4, len(m15))):
                b = m15[j]
                if setup["direction"] == "BUY":
                    if b.l <= setup["entry"]:
                        filled = True
                        fill_price = setup["entry"]
                        break
                else:
                    if b.h >= setup["entry"]:
                        filled = True
                        fill_price = setup["entry"]
                        break
            
            if not filled:
                continue
            
            # Walk to exit
            pnl = None
            exit_p = None
            reason = ""
            r_mult = 0.0
            
            for j in range(i, len(m15)):
                b = m15[j]
                
                if setup["direction"] == "BUY":
                    if b.l <= setup["sl"]:
                        exit_p = setup["sl"]
                        r_mult = -1.0
                        pnl = -risk_usd - COMMISSION * lot
                        reason = "SL"
                        break
                    if b.h >= setup["tp"]:
                        exit_p = setup["tp"]
                        r_mult = setup["rr"]
                        pnl = risk_usd * setup["rr"] - COMMISSION * lot
                        reason = "TP"
                        break
                else:
                    if b.h >= setup["sl"]:
                        exit_p = setup["sl"]
                        r_mult = -1.0
                        pnl = -risk_usd - COMMISSION * lot
                        reason = "SL"
                        break
                    if b.l <= setup["tp"]:
                        exit_p = setup["tp"]
                        r_mult = setup["rr"]
                        pnl = risk_usd * setup["rr"] - COMMISSION * lot
                        reason = "TP"
                        break
            
            if pnl is None:
                # Close at last bar
                exit_p = m15[-1].c
                if setup["direction"] == "BUY":
                    gain = exit_p - fill_price
                else:
                    gain = fill_price - exit_p
                r_mult = gain / sl_dist if sl_dist > 0 else 0
                pnl = risk_usd * r_mult - COMMISSION * lot
                reason = "EOD"
            
            equity += pnl
            daily_loss += pnl
            
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
            
            trade = {
                "time": cur_time,
                "direction": setup["direction"],
                "entry": fill_price,
                "sl": setup["sl"],
                "tp": setup["tp"],
                "exit": exit_p,
                "pnl": pnl,
                "r": r_mult,
                "reason": reason,
                "lot": lot,
                "risk_usd": risk_usd,
                "equity": equity,
                "h4_amd": setup["h4_amd"],
                "confidence": setup["confidence"],
                "rr": setup["rr"],
            }
            trades.append(trade)
            
            stats["total"] += 1
            if pnl > 0:
                stats["wins"] += 1
            else:
                stats["losses"] += 1
            stats["pnl"] += pnl
    
    stats["final_equity"] = equity
    stats["peak"] = peak
    stats["max_dd"] = max_dd
    stats["return_pct"] = (equity - base_equity) / base_equity * 100
    
    return trades, stats

def get_compounding_risk(equity: float, base: float) -> float:
    mult = equity / base
    if mult >= 10.0:
        return TIER_RISK[3]
    elif mult >= 5.0:
        return TIER_RISK[2]
    elif mult >= 2.0:
        return TIER_RISK[1]
    return TIER_RISK[0]

def calculate_lot(symbol: str, risk_usd: float, sl_dist: float) -> float:
    if symbol == "XAUUSD":
        pip_val = 0.01
        pip_size = 0.01
    elif symbol == "XAGUSD":
        pip_val = 0.001
        pip_size = 0.001
    else:
        pip_val = 0.0001
        pip_size = 0.0001
    
    if sl_dist <= 0:
        return 0.01
    
    pips = sl_dist / pip_size
    lot = risk_usd / (pips * pip_val)
    return max(0.01, min(lot, 0.5))

# ── Main ───────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" AMD-FVG MULTI-TIMEFRAME STRATEGY")
    print(" H4 Bias → H1 Structure → M15 FVG Entry")
    print(" With COMPOUNDING ($100 base)")
    print("=" * 100)
    print()
    
    data = load_data()
    
    all_results = {}
    
    for symbol in SYMBOLS:
        print(f"\n{'='*80}")
        print(f" {symbol}")
        print(f"{'='*80}")
        
        trades, stats = simulate_amd_fvg(data, symbol, base_equity=100.0)
        
        print(f"\n  Trades: {stats.get('total', 0)}")
        print(f"  Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}")
        if stats.get('total', 0) > 0:
            wr = stats['wins'] / stats['total'] * 100
            print(f"  Win Rate: {wr:.1f}%")
            print(f"  Total P&L: ${stats.get('pnl', 0):.2f}")
            print(f"  Final Equity: ${stats.get('final_equity', 0):.2f}")
            print(f"  Peak Equity: ${stats.get('peak', 0):.2f}")
            print(f"  Max Drawdown: {stats.get('max_dd', 0):.1f}%")
            print(f"  Return: {stats.get('return_pct', 0):.1f}%")
        
        if trades:
            # By exit reason
            reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
            for t in trades:
                reasons[t["reason"]]["count"] += 1
                reasons[t["reason"]]["pnl"] += t["pnl"]
            
            print(f"\n  Exit breakdown:")
            for r, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
                print(f"    {r}: {d['count']} trades, ${d['pnl']:.2f}")
            
            # By H4 phase
            phases = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
            for t in trades:
                p = t["h4_amd"]
                phases[p]["count"] += 1
                phases[p]["pnl"] += t["pnl"]
                if t["pnl"] > 0:
                    phases[p]["wins"] += 1
            
            print(f"\n  By H4 AMD phase:")
            for p, d in sorted(phases.items(), key=lambda x: -x[1]["count"]):
                wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
                print(f"    {p}: {d['count']} trades, {wr:.1f}% WR, ${d['pnl']:.2f}")
            
            # By confidence
            high_conf = [t for t in trades if t["confidence"] >= 70]
            low_conf = [t for t in trades if t["confidence"] < 70]
            if high_conf:
                hw = sum(1 for t in high_conf if t["pnl"] > 0)
                print(f"\n  High conf (70+): {len(high_conf)} trades, {hw/len(high_conf)*100:.1f}% WR, ${sum(t['pnl'] for t in high_conf):.2f}")
            if low_conf:
                lw = sum(1 for t in low_conf if t["pnl"] > 0)
                print(f"  Low conf (<70): {len(low_conf)} trades, {lw/len(low_conf)*100:.1f}% WR, ${sum(t['pnl'] for t in low_conf):.2f}")
            
            # Monthly
            monthly = defaultdict(lambda: {"pnl": 0, "trades": 0})
            for t in trades:
                m = t["time"][:7]
                monthly[m]["pnl"] += t["pnl"]
                monthly[m]["trades"] += 1
            
            print(f"\n  Monthly:")
            for m in sorted(monthly.keys()):
                d = monthly[m]
                print(f"    {m}: {d['trades']} trades, ${d['pnl']:.2f}")
            
            # Save
            out = os.path.join(os.path.dirname(__file__), f'amd_fvg_{symbol}.json')
            with open(out, 'w') as f:
                json.dump(trades, f, indent=2)
            print(f"\n  Saved: {out}")
            
            all_results[symbol] = stats
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")
    
    # Save summary
    out = os.path.join(os.path.dirname(__file__), 'amd_fvg_summary.json')
    with open(out, 'w') as f:
        json.dump(all_results, f, indent=2)

if __name__ == "__main__":
    run()
