#!/usr/bin/env python3
"""
entropy_ote_engine.py — STDV + OTE Strategy Engine

Based on user's ICT/SMC playbook:
  1. Identify manipulation leg (clear breakout that sweeps liquidity)
  2. Anchor STDV from wick to wick of the manipulation leg
  3. Calculate OTE levels (.63, .65, .705, .79, .886, .5)
  4. Place limit orders at STDV/OTE confluences
  5. SL below manipulation leg wick extreme
  6. TP at next structural level

Manipulation Leg Criteria:
  - Clear breakout beyond a swing high/low
  - Sweeps liquidity (takes out stops from previous structure)
  - Strong momentum (large candle body)
  - Immediate rejection / reversal after sweep

STDV Levels (from manipulation leg wick to wick):
  [0.5] = C.E. / Equilibrium
  [-0.705] = OTE
  [-1] = Re Accumulation/Distribution
  [-2] = Reversal Level
  [-3..-5] = Max Expansion

OTE Levels (from swing wick to wick):
  [.5] = C.E.
  [.63, .65, .705, .79, .886] = OTE zone
"""
import json, os, sys, re, math
from datetime import datetime
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

# ─── STDV + OTE CALCULATIONS ───

def calc_stdv_levels(anchor_high: float, anchor_low: float, mode="long") -> Dict[str, float]:
    """
    Calculate STDV levels from manipulation leg wick to wick.
    Range = high - low
    """
    rng = anchor_high - anchor_low
    
    if mode == "long":
        # For longs: anchor from high (manipulation top) down
        return {
            "ce_0.5": anchor_high - rng * 0.5,
            "ote_-0.705": anchor_high - rng * 0.705,
            "reaccum_-1": anchor_high - rng * 1.0,
            "reversal_-2": anchor_high - rng * 2.0,
            "maxexp_-3": anchor_high - rng * 3.0,
            "maxexp_-4": anchor_high - rng * 4.0,
            "maxexp_-5": anchor_high - rng * 5.0,
        }
    else:
        # For shorts: anchor from low (manipulation bottom) up
        return {
            "ce_0.5": anchor_low + rng * 0.5,
            "ote_-0.705": anchor_low + rng * 0.705,
            "reaccum_-1": anchor_low + rng * 1.0,
            "reversal_-2": anchor_low + rng * 2.0,
            "maxexp_-3": anchor_low + rng * 3.0,
            "maxexp_-4": anchor_low + rng * 4.0,
            "maxexp_-5": anchor_low + rng * 5.0,
        }

def calc_ote_levels(swing_high: float, swing_low: float, mode="long") -> Dict[str, float]:
    """
    Calculate OTE Fibonacci levels from wick to wick.
    For longs: retrace from high down to low, entry at .705-.886
    For shorts: retrace from low up to high, entry at .705-.886
    """
    rng = swing_high - swing_low
    
    if mode == "long":
        return {
            "ce_0.5": swing_high - rng * 0.5,
            "ote_0.886": swing_high - rng * 0.886,
            "ote_0.79": swing_high - rng * 0.79,
            "ote_0.705": swing_high - rng * 0.705,
            "ote_0.65": swing_high - rng * 0.65,
            "ote_0.63": swing_high - rng * 0.63,
        }
    else:
        return {
            "ce_0.5": swing_low + rng * 0.5,
            "ote_0.886": swing_low + rng * 0.886,
            "ote_0.79": swing_low + rng * 0.79,
            "ote_0.705": swing_low + rng * 0.705,
            "ote_0.65": swing_low + rng * 0.65,
            "ote_0.63": swing_low + rng * 0.63,
        }

def find_confluence(stdv: Dict, ote: Dict, mode="long") -> List[Tuple[str, str, float]]:
    """Find where STDV and OTE levels align (within 0.1% of each other)."""
    confluences = []
    
    for sk, sv in stdv.items():
        for ok, ov in ote.items():
            # Check if levels are close (within 0.2% of range)
            avg = (sv + ov) / 2
            if avg == 0:
                continue
            diff_pct = abs(sv - ov) / avg * 100
            if diff_pct < 0.2:  # 0.2% alignment
                confluences.append((sk, ok, (sv + ov) / 2))
    
    return confluences

# ─── MANIPULATION LEG DETECTION ───

def find_manipulation_legs(bars: List[Bar], lookback: int = 20) -> List[Dict]:
    """
    Find manipulation legs:
      1. Price builds a swing structure (HH/HL or LH/LL)
      2. Clear breakout beyond the swing extreme
      3. Large momentum candle
      4. Immediate rejection / reversal after breakout
      
    Returns list of manipulation legs with anchor points.
    """
    legs = []
    
    for i in range(lookback, len(bars) - 5):
        # Look at recent structure
        recent = bars[i-lookback:i+1]
        
        # Find swing highs and lows
        swing_highs = []
        swing_lows = []
        
        for j in range(2, len(recent) - 2):
            # Swing high: higher than 2 before and 2 after
            if recent[j].h > recent[j-1].h and recent[j].h > recent[j-2].h and \
               recent[j].h > recent[j+1].h and recent[j].h > recent[j+2].h:
                swing_highs.append((j, recent[j].h))
            
            # Swing low: lower than 2 before and 2 after
            if recent[j].l < recent[j-1].l and recent[j].l < recent[j-2].l and \
               recent[j].l < recent[j+1].l and recent[j].l < recent[j+2].l:
                swing_lows.append((j, recent[j].l))
        
        if not swing_highs or not swing_lows:
            continue
        
        # Find most recent swing high and low
        last_sh = max(swing_highs, key=lambda x: x[0])
        last_sl = max(swing_lows, key=lambda x: x[0])
        
        # Check if current bar is a manipulation leg
        curr = bars[i]
        prev = bars[i-1]
        
        # LONG manipulation: breakout below last swing low (sweep low), then reject
        if curr.l < last_sl[1] and curr.c > curr.o:
            # Swept below swing low but closed bullish = rejection
            body = abs(curr.c - curr.o)
            wick = curr.h - max(curr.c, curr.o)
            lower_wick = min(curr.c, curr.o) - curr.l
            total_range = curr.h - curr.l
            
            if total_range > 0 and (lower_wick / total_range > 0.3 or body > 0):
                legs.append({
                    "idx": i,
                    "time": curr.time,
                    "type": "long",
                    "manipulation_high": prev.h,  # Before breakout
                    "manipulation_low": curr.l,   # Wick low of sweep
                    "swing_high": last_sh[1],
                    "swing_low": last_sl[1],
                    "candle": {"o": curr.o, "h": curr.h, "l": curr.l, "c": curr.c},
                    "reason": "sweep_low_rejection",
                })
        
        # SHORT manipulation: breakout above last swing high (sweep high), then reject
        elif curr.h > last_sh[1] and curr.c < curr.o:
            # Swept above swing high but closed bearish = rejection
            body = abs(curr.c - curr.o)
            wick = max(curr.c, curr.o) - curr.l
            upper_wick = curr.h - max(curr.c, curr.o)
            total_range = curr.h - curr.l
            
            if total_range > 0 and (upper_wick / total_range > 0.3 or body > 0):
                legs.append({
                    "idx": i,
                    "time": curr.time,
                    "type": "short",
                    "manipulation_high": curr.h,  # Wick high of sweep
                    "manipulation_low": prev.l,   # Before breakout
                    "swing_high": last_sh[1],
                    "swing_low": last_sl[1],
                    "candle": {"o": curr.o, "h": curr.h, "l": curr.l, "c": curr.c},
                    "reason": "sweep_high_rejection",
                })
    
    return legs

# ─── STRATEGY SIMULATION ───

def simulate_entropy_ote(data: Dict, symbol: str, base=1000.0) -> Dict:
    """Simulate the STDV+OTE strategy on real MT5 data."""
    m5 = data.get("M5", [])
    
    if len(m5) < 30:
        return {"trades": 0}
    
    # Find manipulation legs
    legs = find_manipulation_legs(m5, lookback=20)
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    cooldown = 0
    
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
    
    for leg in legs:
        if cooldown > 0:
            cooldown -= 1
            continue
        
        idx = leg["idx"]
        
        # Calculate STDV from manipulation leg
        if leg["type"] == "long":
            stdv = calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "long")
            ote = calc_ote_levels(leg["swing_high"], leg["swing_low"], "long")
        else:
            stdv = calc_stdv_levels(leg["manipulation_high"], leg["manipulation_low"], "short")
            ote = calc_ote_levels(leg["swing_high"], leg["swing_low"], "short")
        
        # Find confluences
        confluences = find_confluence(stdv, ote, leg["type"])
        
        if not confluences:
            continue
        
        # Use the deepest OTE confluence as entry
        # For longs: lowest confluence (best discount)
        # For shorts: highest confluence (best premium)
        if leg["type"] == "long":
            entry_level = min(confluences, key=lambda x: x[2])
        else:
            entry_level = max(confluences, key=lambda x: x[2])
        
        entry = entry_level[2]
        
        # SL = beyond manipulation wick extreme (with buffer)
        # TP = at structural level or CE
        manipulation_range = leg["manipulation_high"] - leg["manipulation_low"]
        buffer = manipulation_range * 0.05
        
        if leg["type"] == "long":
            # For longs: SL below manipulation wick low, TP at recent high / CE
            sl = min(leg["manipulation_low"], entry) - buffer
            # TP: recent swing high or STDV CE, whichever is higher
            tp = max(leg["swing_high"], stdv.get("ce_0.5", entry))
            # Ensure TP is above entry
            if tp <= entry:
                tp = entry + manipulation_range * 0.5
        else:
            # For shorts: SL above manipulation wick high, TP at recent low / CE
            sl = max(leg["manipulation_high"], entry) + buffer
            # TP: recent swing low or STDV CE, whichever is lower
            tp = min(leg["swing_low"], stdv.get("ce_0.5", entry))
            # Ensure TP is below entry
            if tp >= entry:
                tp = entry - manipulation_range * 0.5
        
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        
        # Minimum 2:1 R:R
        reward = abs(tp - entry)
        if reward / risk < 2.0:
            continue
        
        # Check fill in next 5 bars
        fill_idx = None
        for k in range(idx + 1, min(idx + 6, len(m5))):
            if m5[k].l <= entry <= m5[k].h:
                fill_idx = k
                break
        
        if fill_idx is None:
            continue
        
        # Fixed lot
        lot = 0.1
        
        # Walk to exit (hold up to 50 bars)
        pnl = None
        exit_price = None
        reason = ""
        
        for k in range(fill_idx + 1, min(fill_idx + 50, len(m5))):
            b = m5[k]
            if leg["type"] == "long":
                # Long: price goes up = profit
                if b.l <= sl:
                    exit_price = sl
                    raw_pnl = lot * tick_value * ((exit_price - entry) / point)
                    pnl = raw_pnl - COMMISSION * lot
                    reason = "SL"
                    break
                if b.h >= tp:
                    exit_price = tp
                    raw_pnl = lot * tick_value * ((exit_price - entry) / point)
                    pnl = raw_pnl - COMMISSION * lot
                    reason = "TP"
                    break
            else:
                # Short: price goes down = profit
                if b.h >= sl:
                    # Stopped out — price went up against us
                    exit_price = sl
                    raw_pnl = lot * tick_value * ((entry - exit_price) / point)
                    pnl = raw_pnl - COMMISSION * lot
                    reason = "SL"
                    break
                if b.l <= tp:
                    # Hit TP — price went down (profit for short)
                    exit_price = tp
                    raw_pnl = lot * tick_value * ((entry - exit_price) / point)
                    pnl = raw_pnl - COMMISSION * lot
                    reason = "TP"
                    break
        
        if pnl is None:
            exit_idx = min(fill_idx + 49, len(m5) - 1)
            exit_price = m5[exit_idx].c
            if leg["type"] == "long":
                raw_pnl = lot * tick_value * ((exit_price - entry) / point)
            else:
                raw_pnl = lot * tick_value * ((entry - exit_price) / point)
            pnl = raw_pnl - COMMISSION * lot
            reason = "EOD"
        
        equity += pnl
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
        
        trades.append({
            "time": leg["time"],
            "type": leg["type"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "exit": exit_price,
            "pnl": pnl,
            "reason": reason,
            "rr": reward / risk,
            "confluence": f"{entry_level[0]} + {entry_level[1]}",
            "lot": lot,
        })
        
        cooldown = 5  # 5-bar cooldown
    
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
    print(" ENTROPY STDV + OTE STRATEGY")
    print(" Manipulation Leg Detection + Confluence Entry")
    print("=" * 100)
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]:
        if sym not in data or "M5" not in data[sym]:
            continue
        
        print(f"\n{'='*80}")
        print(f" {sym}")
        print(f"{'='*80}")
        
        r = simulate_entropy_ote(data[sym], sym, base=1000.0)
        
        print(f"  Manipulation legs found: {r['legs']}")
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
            
            # Show first 3 trades
            print(f"\n  First 3 trades:")
            for t in r['detail'][:3]:
                status = "✅" if t['pnl'] > 0 else "❌"
                print(f"    {status} {t['time']} | {t['type'].upper()} | Entry: {t['entry']:.2f} | Exit: {t['exit']:.2f} ({t['reason']}) | PnL: ${t['pnl']:.2f} | {t['confluence']}")
        
        out = os.path.join(os.path.dirname(__file__), f'entropy_{sym}.json')
        with open(out, 'w') as f:
            json.dump(r, f, indent=2)
        print(f"  Saved: {out}")
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
