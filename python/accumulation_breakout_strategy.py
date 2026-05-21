#!/usr/bin/env python3
"""
accumulation_breakout_strategy.py — Asian Range < 30 → London Sweep → NY Reversal
With COMPOUNDING dollar-risk sizing for 1:1000 leverage.

Discovery from 2-year forensic:
- Asian range < 30 pts: 63.5-81.8% reversal accuracy
- Asian range > 100 pts: 41.1% accuracy (avoid)
- Range days edge: +167R
"""
import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import (
    Bar, _calc_atr,
)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
SYMBOLS = {"GC=F": "XAUUSD", "SI=F": "XAGUSD"}
START = "2024-05-22"
END = "2026-05-21"

# Compounding risk tiers
TIER_1_RISK = 2.0    # Base risk per trade ($)
TIER_2_RISK = 5.0    # After equity doubles
TIER_3_RISK = 10.0   # After equity 5x
TIER_4_RISK = 25.0   # After equity 10x

COMMISSION_PER_LOT = 7.0
MAX_DAILY_LOSS_PCT = 3.0  # Max 3% of equity per day
MAX_OPEN_TRADES = 3

# Asian range thresholds
ACCUMULATION_MAX = 30.0   # Asian high - low must be < 30 pts
AVOID_MIN = 100.0         # Asian range > 100 = avoid

# Level-to-level targets
TP1_R = 2.0
TP2_R = 3.0
TP3_R = 5.0

# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCH
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_h1(ticker: str) -> List[Bar]:
    df = yf.download(ticker, start=START, end=END, interval="1h",
                     progress=False, auto_adjust=True)
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

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION TOOLS
# ═══════════════════════════════════════════════════════════════════════════════
def get_session(bar_time: str) -> str:
    try:
        dt = datetime.strptime(bar_time, "%Y-%m-%d %H:%M:%S")
        hour = dt.hour
        if 0 <= hour < 8:
            return "ASIAN"
        elif 8 <= hour < 13:
            return "LONDON"
        elif 13 <= hour < 17:
            return "NY_AM"
        elif 17 <= hour < 21:
            return "NY_PM"
        else:
            return "OVERLAP"
    except:
        return "UNKNOWN"

def get_weekday(bar_time: str) -> int:
    try:
        dt = datetime.strptime(bar_time, "%Y-%m-%d %H:%M:%S")
        return dt.weekday()
    except:
        return 0

# ═══════════════════════════════════════════════════════════════════════════════
# DAILY ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
class DayState:
    """Tracks the accumulation → manipulation → distribution cycle for one day."""
    date: str
    symbol: str
    
    asian_high: float = 0.0
    asian_low: float = 0.0
    asian_range: float = 0.0
    asian_mid: float = 0.0
    
    london_high: float = 0.0
    london_low: float = 0.0
    london_swept_high: bool = False
    london_swept_low: bool = False
    london_sweep_time: str = ""
    
    ny_high: float = 0.0
    ny_low: float = 0.0
    
    manipulation_distance: float = 0.0
    day_type: str = ""
    
    # Setup
    entry_triggered: bool = False
    direction: str = ""
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    confidence: int = 0
    
    # Result
    filled: bool = False
    fill_price: float = 0.0
    exit_price: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    lot_size: float = 0.0

# ═══════════════════════════════════════════════════════════════════════════════
# COMPOUNDING RISK CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════
def get_risk_per_trade(equity: float, base_equity: float = 100.0) -> float:
    """Compounding tiers: increase risk as equity grows."""
    multiple = equity / base_equity
    
    if multiple >= 10.0:
        return TIER_4_RISK
    elif multiple >= 5.0:
        return TIER_3_RISK
    elif multiple >= 2.0:
        return TIER_2_RISK
    else:
        return TIER_1_RISK

def calculate_lot_size(symbol: str, risk_usd: float, sl_distance: float) -> float:
    """Calculate lot size for dollar-risk on 1:1000 leverage."""
    if symbol == "XAUUSD":
        pip_value = 0.01  # $0.01 per pip per 0.01 lot
        pip_size = 0.01
    elif symbol == "XAGUSD":
        pip_value = 0.001
        pip_size = 0.001
    else:
        pip_value = 0.0001
        pip_size = 0.0001
    
    if sl_distance <= 0:
        return 0.01
    
    pips = sl_distance / pip_size
    lot_size = risk_usd / (pips * pip_value)
    
    # Cap based on equity margin (1:1000)
    # 0.01 lot XAUUSD = ~$4.50 notional, $0.0045 margin
    # Cap at 0.5 lots to stay safe
    lot_size = max(0.01, min(lot_size, 0.5))
    
    return round(lot_size, 2)

# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_day_cycle(h1_bars: List[Bar], date: str, symbol: str) -> DayState:
    """Analyze one day's accumulation → manipulation → distribution cycle."""
    day_bars = [b for b in h1_bars if b.time[:10] == date]
    if len(day_bars) < 6:
        return None
    
    state = DayState()
    state.date = date
    state.symbol = symbol
    
    # ── Asian Session (00:00-08:00 GMT) ──
    asian = [b for b in day_bars if get_session(b.time) == "ASIAN"]
    if not asian:
        return None
    
    state.asian_high = max(b.h for b in asian)
    state.asian_low = min(b.l for b in asian)
    state.asian_range = state.asian_high - state.asian_low
    state.asian_mid = (state.asian_high + state.asian_low) / 2
    
    # ── Accumulation Check ──
    if state.asian_range < ACCUMULATION_MAX:
        state.day_type = "ACCUMULATION"
        state.confidence = 75
    elif state.asian_range < AVOID_MIN:
        state.day_type = "MIXED"
        state.confidence = 50
    else:
        state.day_type = "EXPANSION"
        state.confidence = 25
    
    # ── London Session (08:00-13:00 GMT) ──
    london = [b for b in day_bars if get_session(b.time) == "LONDON"]
    if not london:
        return state
    
    state.london_high = max(b.h for b in london)
    state.london_low = min(b.l for b in london)
    
    # Did London sweep Asian high?
    if state.london_high > state.asian_high:
        state.london_swept_high = True
        state.manipulation_distance = state.london_high - state.asian_high
        state.london_sweep_time = london[0].time
        
        # If accumulation day, expect reversal down
        if state.day_type == "ACCUMULATION":
            state.direction = "SELL"
            state.entry_price = state.asian_high  # The manipulation wick extreme
            state.sl_price = state.london_high + state.asian_range * 0.15
            
            # Level-to-level: Asian mid as first target, Asian low as second
            risk = state.sl_price - state.entry_price
            state.tp1 = state.entry_price - risk * TP1_R
            state.tp2 = state.entry_price - risk * TP2_R
            state.tp3 = max(state.entry_price - risk * TP3_R, state.asian_low * 0.998)
            
            # Also target the Asian low (level-to-level)
            state.tp2 = min(state.tp2, state.asian_low)
    
    # Did London sweep Asian low?
    elif state.london_low < state.asian_low:
        state.london_swept_low = True
        state.manipulation_distance = state.asian_low - state.london_low
        state.london_sweep_time = london[0].time
        
        if state.day_type == "ACCUMULATION":
            state.direction = "BUY"
            state.entry_price = state.asian_low
            state.sl_price = state.london_low - state.asian_range * 0.15
            
            risk = state.entry_price - state.sl_price
            state.tp1 = state.entry_price + risk * TP1_R
            state.tp2 = state.entry_price + risk * TP2_R
            state.tp3 = min(state.entry_price + risk * TP3_R, state.asian_high * 1.002)
            
            state.tp2 = max(state.tp2, state.asian_high)
    
    # ── NY Session (13:00+ GMT) ──
    ny = [b for b in day_bars if get_session(b.time) in ["NY_AM", "NY_PM"]]
    if ny:
        state.ny_high = max(b.h for b in ny)
        state.ny_low = min(b.l for b in ny)
    
    return state

# ═══════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE (with compounding)
# ═══════════════════════════════════════════════════════════════════════════════
def simulate_compounding(h1_bars: List[Bar], symbol: str, base_equity: float = 100.0) -> Tuple[List[DayState], Dict]:
    """Simulate the strategy with compounding dollar-risk sizing."""
    
    # Group by day
    days = defaultdict(list)
    for b in h1_bars:
        days[b.time[:10]].append(b)
    
    sorted_dates = sorted(days.keys())
    
    equity = base_equity
    peak_equity = equity
    max_drawdown_pct = 0.0
    daily_loss_today = 0.0
    current_day = ""
    
    results: List[DayState] = []
    stats = {
        "total_days": 0,
        "accumulation_days": 0,
        "manipulated_days": 0,
        "trades_taken": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "max_drawdown": 0.0,
        "max_consecutive_losses": 0,
        "current_loss_streak": 0,
    }
    
    open_trades: List[DayState] = []
    
    for date in sorted_dates:
        day_bars = days[date]
        
        if date != current_day:
            current_day = date
            daily_loss_today = 0.0
        
        # Analyze day cycle
        state = analyze_day_cycle(h1_bars, date, symbol)
        if not state:
            continue
        
        stats["total_days"] += 1
        
        if state.day_type == "ACCUMULATION":
            stats["accumulation_days"] += 1
        
        # Skip if no manipulation
        if not state.london_swept_high and not state.london_swept_low:
            results.append(state)
            continue
        
        stats["manipulated_days"] += 1
        
        # Skip if not accumulation day
        if state.day_type != "ACCUMULATION":
            results.append(state)
            continue
        
        # Skip if already max trades open
        if len(open_trades) >= MAX_OPEN_TRADES:
            results.append(state)
            continue
        
        # Skip if daily loss limit hit
        daily_loss_limit = -(equity * MAX_DAILY_LOSS_PCT / 100)
        if daily_loss_today <= daily_loss_limit:
            results.append(state)
            continue
        
        # Calculate risk and lot size (compounding)
        risk_usd = get_risk_per_trade(equity, base_equity)
        sl_dist = abs(state.sl_price - state.entry_price)
        state.lot_size = calculate_lot_size(symbol, risk_usd, sl_dist)
        
        # Skip if SL too tight
        if sl_dist <= 0:
            results.append(state)
            continue
        
        # ── Limit order simulation ──
        # Order placed at Asian extreme, waits for NY to retrace
        filled = False
        fill_price = None
        
        for b in day_bars:
            if state.direction == "SELL":
                if b.h >= state.entry_price:
                    filled = True
                    fill_price = state.entry_price
                    break
            else:  # BUY
                if b.l <= state.entry_price:
                    filled = True
                    fill_price = state.entry_price
                    break
        
        if not filled:
            state.entry_triggered = True
            state.filled = False
            results.append(state)
            continue
        
        state.filled = True
        state.fill_price = fill_price
        stats["trades_taken"] += 1
        
        # ── Walk to exit ──
        pnl_usd = None
        exit_price = None
        exit_reason = ""
        r_multiple = 0.0
        
        # Calculate pip value for this lot size
        if symbol == "XAUUSD":
            pip_val = 0.01 * state.lot_size * 100  # per pip
        elif symbol == "XAGUSD":
            pip_val = 0.001 * state.lot_size * 100
        else:
            pip_val = 0.0001 * state.lot_size * 100
        
        for b in day_bars:
            if b.time <= state.london_sweep_time:
                continue  # Only check after London sweep
            
            if state.direction == "SELL":
                # SL hit
                if b.h >= state.sl_price:
                    exit_price = state.sl_price
                    pnl_usd = -risk_usd - (COMMISSION_PER_LOT * state.lot_size)
                    r_multiple = -1.0
                    exit_reason = "SL"
                    break
                # TP3
                if b.l <= state.tp3:
                    exit_price = state.tp3
                    r_multiple = TP3_R
                    pnl_usd = risk_usd * TP3_R - (COMMISSION_PER_LOT * state.lot_size)
                    exit_reason = "TP3"
                    break
                # TP2
                if b.l <= state.tp2:
                    exit_price = state.tp2
                    r_multiple = TP2_R
                    pnl_usd = risk_usd * TP2_R - (COMMISSION_PER_LOT * state.lot_size)
                    exit_reason = "TP2"
                    break
                # TP1
                if b.l <= state.tp1:
                    exit_price = state.tp1
                    r_multiple = TP1_R
                    pnl_usd = risk_usd * TP1_R - (COMMISSION_PER_LOT * state.lot_size)
                    exit_reason = "TP1"
                    break
            else:  # BUY
                if b.l <= state.sl_price:
                    exit_price = state.sl_price
                    pnl_usd = -risk_usd - (COMMISSION_PER_LOT * state.lot_size)
                    r_multiple = -1.0
                    exit_reason = "SL"
                    break
                if b.h >= state.tp3:
                    exit_price = state.tp3
                    r_multiple = TP3_R
                    pnl_usd = risk_usd * TP3_R - (COMMISSION_PER_LOT * state.lot_size)
                    exit_reason = "TP3"
                    break
                if b.h >= state.tp2:
                    exit_price = state.tp2
                    r_multiple = TP2_R
                    pnl_usd = risk_usd * TP2_R - (COMMISSION_PER_LOT * state.lot_size)
                    exit_reason = "TP2"
                    break
                if b.h >= state.tp1:
                    exit_price = state.tp1
                    r_multiple = TP1_R
                    pnl_usd = risk_usd * TP1_R - (COMMISSION_PER_LOT * state.lot_size)
                    exit_reason = "TP1"
                    break
        
        if pnl_usd is None:
            # Close at end of day
            exit_price = day_bars[-1].c
            if state.direction == "SELL":
                gain = fill_price - exit_price
            else:
                gain = exit_price - fill_price
            r_multiple = gain / sl_dist if sl_dist > 0 else 0
            pnl_usd = risk_usd * r_multiple - (COMMISSION_PER_LOT * state.lot_size)
            exit_reason = "EOD"
        
        state.exit_price = exit_price
        state.exit_reason = exit_reason
        state.pnl_usd = pnl_usd
        state.r_multiple = r_multiple
        
        equity += pnl_usd
        daily_loss_today += pnl_usd
        
        # Track drawdown
        if equity > peak_equity:
            peak_equity = equity
            stats["current_loss_streak"] = 0
        else:
            dd = (peak_equity - equity) / peak_equity * 100
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd
        
        # Track streaks
        if pnl_usd > 0:
            stats["wins"] += 1
            stats["current_loss_streak"] = 0
        else:
            stats["losses"] += 1
            stats["current_loss_streak"] += 1
            if stats["current_loss_streak"] > stats["max_consecutive_losses"]:
                stats["max_consecutive_losses"] = stats["current_loss_streak"]
        
        stats["total_pnl"] += pnl_usd
        results.append(state)
    
    stats["final_equity"] = equity
    stats["peak_equity"] = peak_equity
    stats["max_drawdown"] = max_drawdown_pct
    stats["return_pct"] = (equity - base_equity) / base_equity * 100
    
    return results, stats

# ═══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════════════════════
def print_results(symbol: str, results: List[DayState], stats: Dict, base_equity: float):
    print(f"\n{'='*80}")
    print(f" {symbol} — ACCUMULATION → MANIPULATION → REVERSAL")
    print(f" With COMPOUNDING ($100 base, 1:1000 leverage)")
    print(f"{'='*80}")
    
    print(f"\n  Period: 2024-05-22 → 2026-05-21 (498 trading days)")
    print(f"  Base equity: ${base_equity:.2f}")
    print(f"  Final equity: ${stats['final_equity']:.2f}")
    print(f"  Peak equity: ${stats['peak_equity']:.2f}")
    print(f"  Return: {stats['return_pct']:.1f}%")
    print(f"  Max drawdown: {stats['max_drawdown']:.1f}%")
    
    print(f"\n  Days analyzed: {stats['total_days']}")
    print(f"  Accumulation days (< {ACCUMULATION_MAX} pts): {stats['accumulation_days']}")
    print(f"  Manipulated days: {stats['manipulated_days']}")
    print(f"  Trades taken: {stats['trades_taken']}")
    
    if stats['trades_taken'] > 0:
        wr = stats['wins'] / stats['trades_taken'] * 100
        print(f"\n  Wins: {stats['wins']} | Losses: {stats['losses']}")
        print(f"  Win Rate: {wr:.1f}%")
        print(f"  Max consecutive losses: {stats['max_consecutive_losses']}")
        
        # By exit reason
        reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
        for r in results:
            if r.filled and r.exit_reason:
                reasons[r.exit_reason]["count"] += 1
                reasons[r.exit_reason]["pnl"] += r.pnl_usd
        
        print(f"\n  Exit breakdown:")
        for reason, data in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
            print(f"    {reason}: {data['count']} trades, ${data['pnl']:.2f}")
        
        # By direction
        sells = [r for r in results if r.filled and r.direction == "SELL"]
        buys = [r for r in results if r.filled and r.direction == "BUY"]
        if sells:
            s_wins = sum(1 for r in sells if r.pnl_usd > 0)
            print(f"\n  SELLs: {len(sells)} trades, {s_wins/len(sells)*100:.1f}% WR, ${sum(r.pnl_usd for r in sells):.2f}")
        if buys:
            b_wins = sum(1 for r in buys if r.pnl_usd > 0)
            print(f"  BUYs: {len(buys)} trades, {b_wins/len(buys)*100:.1f}% WR, ${sum(r.pnl_usd for r in buys):.2f}")
        
        # Monthly breakdown
        monthly = defaultdict(lambda: {"pnl": 0, "trades": 0})
        for r in results:
            if r.filled:
                month = r.date[:7]
                monthly[month]["pnl"] += r.pnl_usd
                monthly[month]["trades"] += 1
        
        print(f"\n  Monthly performance:")
        for month in sorted(monthly.keys()):
            m = monthly[month]
            print(f"    {month}: {m['trades']} trades, ${m['pnl']:.2f}")
    
    # Save detailed results
    out = os.path.join(os.path.dirname(__file__), f'accumulation_strategy_{symbol}.json')
    export = []
    for r in results:
        if r.filled:
            export.append({
                "date": r.date,
                "direction": r.direction,
                "asian_range": r.asian_range,
                "entry": r.entry_price,
                "sl": r.sl_price,
                "tp1": r.tp1,
                "tp2": r.tp2,
                "fill": r.fill_price,
                "exit": r.exit_price,
                "reason": r.exit_reason,
                "pnl": r.pnl_usd,
                "r": r.r_multiple,
                "lot": r.lot_size,
            })
    with open(out, 'w') as f:
        json.dump(export, f, indent=2)
    print(f"\n  Detailed trade log saved: {out}")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def run():
    print("=" * 100)
    print(" ACCUMULATION → MANIPULATION → REVERSAL STRATEGY")
    print(" With COMPOUNDING Dollar-Risk Sizing")
    print("=" * 100)
    print()
    print("Strategy rules:")
    print(f"  1. Asian session (00-08 GMT) range < {ACCUMULATION_MAX} pts = ACCUMULATION")
    print(f"  2. London (08-13 GMT) sweeps Asian high/low = MANIPULATION")
    print(f"  3. NY (13+ GMT) reversal trade at manipulation wick")
    print(f"  4. SL beyond London sweep, TP at 2R/3R/5R + level-to-level")
    print(f"  5. Risk compounds: $2 → $5 → $10 → $25 as equity grows")
    print()
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\nFetching {symbol} data...")
        h1 = fetch_h1(yf_ticker)
        if not h1:
            print("  No data")
            continue
        
        results, stats = simulate_compounding(h1, symbol, base_equity=100.0)
        print_results(symbol, results, stats, 100.0)
    
    print(f"\n{'='*80}")
    print(" BACKTEST COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
