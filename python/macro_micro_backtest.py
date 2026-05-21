#!/usr/bin/env python3
"""
macro_micro_backtest.py — 3-Year Bar-by-Bar Strategy Discovery
Pulls D1/H4/H1 data from yfinance, detects patterns across timeframes,
correlates macro structure with micro setups, and reports real stats.
"""
import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple

import pandas as pd
import yfinance as yf

# ── Load ICT precision functions ──────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import (
    Bar, detect_sweep_high, detect_sweep_low,
    find_bearish_ob, find_bullish_ob,
    _calc_atr, _snap_to_interval, _dist_intervals,
)

# ── Config ────────────────────────────────────────────────────────
SYMBOLS = {"GC=F": "XAUUSD", "SI=F": "XAGUSD"}
START_DATE = "2023-01-01"
END_DATE   = "2026-05-21"
H1_START   = "2024-05-22"  # 730 days max for yfinance H1
H1_END     = "2026-05-21"

# ── Dollar-risk sizing (leverage-aware) ───────────────────────────
# At 1:1000 leverage on XAUUSD:
#   0.01 lot = 1 oz notional (~$4,500 at current prices)
#   Margin required = ~$4.50
#   1 pip = $0.01 (0.01 lot × $1/pip per lot)
#   Wait: MT5 lot sizing for metals is different
#   Standard: 1 lot = 100 oz, pip value depends on contract spec
#   At 1:1000, 0.01 lot XAUUSD margin ≈ $4-5, 1 pip ≈ $0.01-0.10
#
# For a $100-500 account:
#   Max risk per trade = $1-2 (0.2-0.4% of account is insane)
#   Actually with 1:1000, the problem is REVERSE:
#   You have massive buying power but need tiny positions
#   0.01 lot with 100 pip SL = $1-10 risk (depending on pip value)
#
# REALITY CHECK: Let's model based on actual MT5 contract specs
# XAUUSD: 1 pip = $0.01 per 0.01 micro lot (standard MT5)
# XAGUSD: 1 pip = $0.001 per 0.01 micro lot
# Forex: 1 pip = $0.01 per 0.01 micro lot
#
# So $2 risk at 100 pip SL = 0.02 lots for XAUUSD
# $2 risk at 50 pip SL = 0.04 lots for XAUUSD

PIP_VALUE_USD = {
    'XAUUSD': 0.01,   # $0.01 per pip per 0.01 lot
    'XAGUSD': 0.001,  # $0.001 per pip per 0.01 lot
    'EURUSD': 0.01,
    'GBPUSD': 0.01,
    'AUDUSD': 0.01,
    'USDCAD': 0.01,
}

MAX_DAILY_LOSS_USD = 5.0    # $5 max loss per day
MAX_TRADE_RISK_USD = 2.0    # $2 max risk per trade
MIN_ATR_FOR_TRADE  = 5.0    # Minimum ATR in price units

# ── Pattern scoring ───────────────────────────────────────────────
@dataclass
class MacroPattern:
    week_start: str
    week_end: str
    bias: str          # BULLISH / BEARISH / RANGING
    structure: str     # HH_HL / LH_LL / EQUAL_HIGHS / EQUAL_LOWS / BREAKOUT_UP / BREAKDOWN
    range: float       # Weekly high - low
    body_pct: float    # |close-open| / range
    prev_week_bias: str
    streak: int        # Consecutive weeks in same bias

@dataclass
class MicroSetup:
    time: str
    direction: str     # BUY / SELL
    entry: float
    sl: float
    tp: float
    risk_usd: float
    macro_bias: str
    session: str       # ASIAN / LONDON / NY / OVERLAP
    pattern: str       # SWEEP_OB / SWEEP_FVG / AMD_REVERSAL / BREAKOUT_PULLBACK
    confluence: int    # 0-100 score

@dataclass
class TradeResult:
    time: str
    direction: str
    entry: float
    sl: float
    tp: float
    exit_price: float
    exit_time: str
    pnl_usd: float
    r_multiple: float
    macro_bias: str
    pattern: str
    session: str

# ── Data fetching ─────────────────────────────────────────────────
def fetch_timeframe(ticker: str, interval: str, start: str, end: str) -> pd.DataFrame:
    """Fetch OHLCV from yfinance."""
    try:
        df = yf.download(ticker, start=start, end=end, interval=interval,
                        progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna(subset=["Open", "High", "Low", "Close"])
    except Exception as e:
        print(f"  ERROR fetching {ticker} {interval}: {e}")
        return pd.DataFrame()

def df_to_bars(df: pd.DataFrame) -> List[Bar]:
    """Convert DataFrame to Bar dataclass list."""
    bars = []
    for ts, row in df.iterrows():
        bars.append(Bar(
            time=str(ts)[:19],
            o=float(row["Open"]),
            h=float(row["High"]),
            l=float(row["Low"]),
            c=float(row["Close"]),
            v=int(row.get("Volume", 0)),
        ))
    return bars

# ── Macro pattern detection (D1 bars) ─────────────────────────────
def detect_macro_patterns(d1_bars: List[Bar]) -> List[MacroPattern]:
    """Analyze weekly structure on D1 bars."""
    if len(d1_bars) < 20:
        return []
    
    # Group by week
    weeks = defaultdict(list)
    for b in d1_bars:
        # Parse week from date
        dt = datetime.strptime(b.time[:10], "%Y-%m-%d")
        week_key = dt.strftime("%Y-W%U")
        weeks[week_key].append(b)
    
    patterns = []
    prev_bias = "NEUTRAL"
    streak = 0
    
    for week_key in sorted(weeks.keys()):
        wbars = weeks[week_key]
        if len(wbars) < 3:
            continue
        
        w_open = wbars[0].o
        w_close = wbars[-1].c
        w_high = max(b.h for b in wbars)
        w_low = min(b.l for b in wbars)
        w_range = w_high - w_low
        w_body = abs(w_close - w_open)
        body_pct = w_body / w_range if w_range > 0 else 0
        
        # Detect weekly structure
        # Compare to previous 2 weeks for HH/HL/LH/LL
        prev_weeks = [w for w in patterns[-2:] if w.bias != "RANGING"] if patterns else []
        
        if len(prev_weeks) >= 2:
            p1 = prev_weeks[-1]
            p2 = prev_weeks[-2]
            
            hh = w_high > p1.range  # simplified — need prev week high
            hl = w_low > p1.range    # simplified
            lh = w_high < p1.range
            ll = w_low < p1.range
            
            if w_close > w_open and body_pct > 0.4:
                bias = "BULLISH"
                if w_high > p1.range * 0.95 and w_low > p1.range * 0.9:
                    structure = "HH_HL"
                else:
                    structure = "BREAKOUT_UP"
            elif w_close < w_open and body_pct > 0.4:
                bias = "BEARISH"
                if w_high < p1.range * 1.05 and w_low < p1.range * 1.1:
                    structure = "LH_LL"
                else:
                    structure = "BREAKDOWN"
            else:
                bias = "RANGING"
                structure = "ACCUMULATION" if body_pct < 0.25 else "CHOP"
        else:
            bias = "BULLISH" if w_close > w_open else "BEARISH"
            structure = "INITIAL"
        
        # Streak counting
        if bias == prev_bias and bias != "RANGING":
            streak += 1
        else:
            streak = 1 if bias != "RANGING" else 0
        
        prev_bias = bias
        
        patterns.append(MacroPattern(
            week_start=wbars[0].time[:10],
            week_end=wbars[-1].time[:10],
            bias=bias,
            structure=structure,
            range=w_range,
            body_pct=body_pct,
            prev_week_bias=prev_bias,
            streak=streak,
        ))
    
    return patterns

# ── Micro setup detection (H1 bars with macro context) ──────────────
def detect_micro_setups(h1_bars: List[Bar], macro_bias: str, symbol: str) -> List[MicroSetup]:
    """Detect H1 setups that align with macro bias."""
    if len(h1_bars) < 60:
        return []
    
    atr = _calc_atr(h1_bars[-20:], period=14)
    if atr < MIN_ATR_FOR_TRADE:
        return []
    
    setups = []
    
    # Process in rolling windows of 24 bars (~1 day)
    for i in range(24, len(h1_bars), 6):  # Step by 6 hours for overlap
        window = h1_bars[max(0, i-48):i]
        current = h1_bars[i]
        
        # Determine session
        hour = datetime.strptime(current.time, "%Y-%m-%d %H:%M:%S").hour
        if 0 <= hour < 8:
            session = "ASIAN"
        elif 8 <= hour < 13:
            session = "LONDON"
        elif 13 <= hour < 21:
            session = "NY"
        else:
            session = "OVERLAP"
        
        # Get Asian range for this "day" (previous 8 bars)
        asian_bars = h1_bars[max(0, i-16):max(0, i-8)]
        if not asian_bars:
            continue
        
        asian_high = max(b.h for b in asian_bars)
        asian_low = min(b.l for b in asian_bars)
        
        # London bars (previous 5 bars before current)
        london_bars = h1_bars[max(0, i-8):i]
        london_high = max(b.h for b in london_bars) if london_bars else asian_high
        london_low = min(b.l for b in london_bars) if london_bars else asian_low
        
        # ── SELL Setup ──
        if macro_bias in ["BEARISH", "RANGING"]:
            # London sweep of Asian high
            if london_high > asian_high:
                # Check for OB near the sweep
                ob = find_bearish_ob(window, start=max(0, len(window)-12), search=12)
                if ob:
                    ob_low, ob_high = ob
                    # Entry at manipulation wick extreme (Asian high)
                    entry = asian_high
                    # SL beyond London sweep + buffer
                    sl = london_high + atr * 0.5
                    risk_pips = sl - entry
                    
                    if risk_pips > 0 and risk_pips < atr * 5:
                        # Dollar risk sizing based on actual pip value
                        pip_val = PIP_VALUE_USD.get(symbol, 0.01)
                        risk_pips_count = risk_pips * 100  # Convert price diff to pips (for XAUUSD 0.01 = 1 pip)
                        lot_size = MAX_TRADE_RISK_USD / (risk_pips_count * pip_val)
                        lot_size = max(0.01, min(lot_size, 1.0))  # Cap at 1 lot
                        risk_usd = risk_pips_count * pip_val * lot_size
                        risk_usd = min(risk_usd, MAX_TRADE_RISK_USD)
                        
                        tp = entry - risk_pips * 2
                        
                        setups.append(MicroSetup(
                            time=current.time,
                            direction="SELL",
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            risk_usd=risk_usd,
                            macro_bias=macro_bias,
                            session=session,
                            pattern="SWEEP_OB",
                            confluence=50 + (20 if session == "NY" else 0) + (10 if "BEARISH" in macro_bias else 0),
                        ))
        
        # ── BUY Setup ──
        if macro_bias in ["BULLISH", "RANGING"]:
            if london_low < asian_low:
                ob = find_bullish_ob(window, start=max(0, len(window)-12), search=12)
                if ob:
                    ob_low, ob_high = ob
                    entry = asian_low
                    sl = london_low - atr * 0.5
                    risk_pips = entry - sl
                    
                    if risk_pips > 0 and risk_pips < atr * 5:
                        # Dollar risk sizing
                        pip_val = PIP_VALUE_USD.get(symbol, 0.01)
                        risk_pips_count = risk_pips * 100
                        lot_size = MAX_TRADE_RISK_USD / (risk_pips_count * pip_val)
                        lot_size = max(0.01, min(lot_size, 1.0))
                        risk_usd = risk_pips_count * pip_val * lot_size
                        risk_usd = min(risk_usd, MAX_TRADE_RISK_USD)
                        
                        tp = entry + risk_pips * 2
                        
                        setups.append(MicroSetup(
                            time=current.time,
                            direction="BUY",
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            risk_usd=risk_usd,
                            macro_bias=macro_bias,
                            session=session,
                            pattern="SWEEP_OB",
                            confluence=50 + (20 if session == "NY" else 0) + (10 if "BULLISH" in macro_bias else 0),
                        ))
    
    return setups

# ── Simulate trades with realistic fills ──────────────────────────
def simulate_trades(h1_bars: List[Bar], setups: List[MicroSetup], symbol: str) -> List[TradeResult]:
    """Walk forward: place limit order, check if filled, track to exit."""
    results = []
    
    for setup in setups:
        # Find when setup triggered
        setup_time = datetime.strptime(setup.time, "%Y-%m-%d %H:%M:%S")
        
        # Look for fill within next 6 bars (6 hours)
        filled = False
        fill_price = None
        fill_idx = None
        
        for i, bar in enumerate(h1_bars):
            bar_time = datetime.strptime(bar.time, "%Y-%m-%d %H:%M:%S")
            if bar_time < setup_time:
                continue
            if bar_time > setup_time + timedelta(hours=6):
                break
            
            # Limit fill check
            if setup.direction == "SELL":
                if bar.h >= setup.entry:  # Price reached our limit
                    filled = True
                    fill_price = setup.entry  # Limit fill at our price
                    fill_idx = i
                    break
            else:
                if bar.l <= setup.entry:
                    filled = True
                    fill_price = setup.entry
                    fill_idx = i
                    break
        
        if not filled:
            continue
        
        # Track to exit: SL, TP, or 48 hours max hold
        exit_price = None
        exit_time = None
        pnl = 0.0
        r = 0.0
        
        for i in range(fill_idx, min(fill_idx + 48, len(h1_bars))):
            bar = h1_bars[i]
            
            if setup.direction == "SELL":
                # Check SL (price went up to our stop)
                if bar.h >= setup.sl:
                    exit_price = setup.sl
                    exit_time = bar.time
                    risk = setup.sl - setup.entry
                    pnl = -(setup.risk_usd)
                    r = -1.0
                    break
                # Check TP (price went down to target)
                if bar.l <= setup.tp:
                    exit_price = setup.tp
                    exit_time = bar.time
                    risk = setup.sl - setup.entry
                    gain = setup.entry - setup.tp
                    r = gain / risk if risk > 0 else 0
                    pnl = setup.risk_usd * r
                    break
            else:
                if bar.l <= setup.sl:
                    exit_price = setup.sl
                    exit_time = bar.time
                    risk = setup.entry - setup.sl
                    pnl = -(setup.risk_usd)
                    r = -1.0
                    break
                if bar.h >= setup.tp:
                    exit_price = setup.tp
                    exit_time = bar.time
                    risk = setup.entry - setup.sl
                    gain = setup.tp - setup.entry
                    r = gain / risk if risk > 0 else 0
                    pnl = setup.risk_usd * r
                    break
        
        # If no exit, close at last bar
        if exit_price is None:
            last_bar = h1_bars[min(fill_idx + 48 - 1, len(h1_bars) - 1)]
            exit_price = last_bar.c
            exit_time = last_bar.time
            
            if setup.direction == "SELL":
                risk = setup.sl - setup.entry
                gain = setup.entry - exit_price
            else:
                risk = setup.entry - setup.sl
                gain = exit_price - setup.entry
            
            r = gain / risk if risk > 0 else 0
            pnl = setup.risk_usd * r
        
        results.append(TradeResult(
            time=setup.time,
            direction=setup.direction,
            entry=fill_price,
            sl=setup.sl,
            tp=setup.tp,
            exit_price=exit_price,
            exit_time=exit_time,
            pnl_usd=pnl,
            r_multiple=r,
            macro_bias=setup.macro_bias,
            pattern=setup.pattern,
            session=setup.session,
        ))
    
    return results

# ── Main analysis loop ────────────────────────────────────────────
def run_full_analysis():
    print("=" * 100)
    print(" 3-YEAR MACRO + MICRO STRATEGY DISCOVERY")
    print("=" * 100)
    print()
    print(f"Period: {START_DATE} → {END_DATE}")
    print(f"Symbols: {list(SYMBOLS.values())}")
    print(f"Timeframes: D1 (macro) + H1 (micro)")
    print(f"Leverage model: 1:1000, Dollar-risk sizing ($2/trade max)")
    print()
    
    all_results = {}
    
    for yf_ticker, symbol in SYMBOLS.items():
        print(f"\n{'='*80}")
        print(f" Processing {symbol}")
        print(f"{'='*80}")
        
        # Fetch D1 for macro
        print(f"  Fetching D1 data...")
        d1_df = fetch_timeframe(yf_ticker, "1d", START_DATE, END_DATE)
        if d1_df.empty:
            print(f"  ERROR: No D1 data for {symbol}")
            continue
        d1_bars = df_to_bars(d1_df)
        print(f"  D1: {len(d1_bars)} days ({d1_bars[0].time[:10]} → {d1_bars[-1].time[:10]})")
        
        # Fetch H1 for micro
        print(f"  Fetching H1 data...")
        h1_df = fetch_timeframe(yf_ticker, "1h", H1_START, H1_END)
        if h1_df.empty:
            print(f"  ERROR: No H1 data for {symbol}")
            continue
        h1_bars = df_to_bars(h1_df)
        print(f"  H1: {len(h1_bars)} hours ({h1_bars[0].time[:10]} → {h1_bars[-1].time[:10]})")
        
        # Macro patterns
        print(f"  Detecting macro patterns...")
        macro = detect_macro_patterns(d1_bars)
        print(f"  Found {len(macro)} weekly patterns")
        
        # Print macro summary
        bull_weeks = sum(1 for p in macro if p.bias == "BULLISH")
        bear_weeks = sum(1 for p in macro if p.bias == "BEARISH")
        range_weeks = sum(1 for p in macro if p.bias == "RANGING")
        print(f"    Bullish: {bull_weeks} | Bearish: {bear_weeks} | Ranging: {range_weeks}")
        
        # Micro setups
        print(f"  Detecting micro setups...")
        all_setups = []
        # Process H1 in chunks aligned with D1 macro bias
        for i in range(0, len(h1_bars), 24):
            chunk = h1_bars[i:i+24]
            if not chunk:
                continue
            
            # Find matching macro bias for this chunk
            chunk_day = chunk[0].time[:10]
            matching_macro = None
            for p in macro:
                if p.week_start <= chunk_day <= p.week_end:
                    matching_macro = p
                    break
            
            if matching_macro:
                chunk_setups = detect_micro_setups(chunk, matching_macro.bias, symbol)
                all_setups.extend(chunk_setups)
        
        print(f"  Found {len(all_setups)} micro setups")
        
        # Simulate trades
        print(f"  Simulating trades...")
        trades = simulate_trades(h1_bars, all_setups, symbol)
        print(f"  Executed: {len(trades)} trades")
        
        # Stats
        if trades:
            wins = [t for t in trades if t.pnl_usd > 0]
            losses = [t for t in trades if t.pnl_usd <= 0]
            total_pnl = sum(t.pnl_usd for t in trades)
            avg_r = sum(t.r_multiple for t in trades) / len(trades)
            
            print(f"\n  Results:")
            print(f"    Wins: {len(wins)} | Losses: {len(losses)}")
            print(f"    Win Rate: {len(wins)/len(trades)*100:.1f}%")
            print(f"    Total P&L: ${total_pnl:.2f}")
            print(f"    Average R: {avg_r:.2f}")
            print(f"    Best trade: ${max(t.pnl_usd for t in trades):.2f} R{max(t.r_multiple for t in trades):.2f}")
            print(f"    Worst trade: ${min(t.pnl_usd for t in trades):.2f} R{min(t.r_multiple for t in trades):.2f}")
            
            # By session
            session_pnl = defaultdict(float)
            session_count = defaultdict(int)
            for t in trades:
                session_pnl[t.session] += t.pnl_usd
                session_count[t.session] += 1
            
            print(f"\n    By session:")
            for sess in sorted(session_count.keys()):
                print(f"      {sess}: {session_count[sess]} trades, ${session_pnl[sess]:.2f}")
            
            # By pattern
            pattern_pnl = defaultdict(float)
            pattern_count = defaultdict(int)
            for t in trades:
                pattern_pnl[t.pattern] += t.pnl_usd
                pattern_count[t.pattern] += 1
            
            print(f"\n    By pattern:")
            for pat in sorted(pattern_count.keys()):
                print(f"      {pat}: {pattern_count[pat]} trades, ${pattern_pnl[pat]:.2f}")
            
            # By macro alignment
            aligned = [t for t in trades if 
                      (t.direction == "SELL" and t.macro_bias == "BEARISH") or
                      (t.direction == "BUY" and t.macro_bias == "BULLISH")]
            counter = [t for t in trades if t not in aligned]
            
            print(f"\n    Macro alignment:")
            print(f"      With trend: {len(aligned)} trades, ${sum(t.pnl_usd for t in aligned):.2f}")
            print(f"      Counter-trend: {len(counter)} trades, ${sum(t.pnl_usd for t in counter):.2f}")
            
            all_results[symbol] = {
                'macro_patterns': [asdict(p) for p in macro],
                'trades': len(trades),
                'wins': len(wins),
                'losses': len(losses),
                'win_rate': len(wins)/len(trades)*100,
                'total_pnl': total_pnl,
                'avg_r': avg_r,
                'session_stats': dict(session_pnl),
                'pattern_stats': dict(pattern_pnl),
                'macro_aligned_pnl': sum(t.pnl_usd for t in aligned),
                'counter_trend_pnl': sum(t.pnl_usd for t in counter),
            }
        else:
            print(f"\n  No trades executed — setups didn't reach limit prices")
    
    # Save results
    output_file = os.path.join(os.path.dirname(__file__), 'macro_micro_results.json')
    with open(output_file, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    
    print(f"\n{'='*80}")
    print(f" Results saved to: {output_file}")
    print(f"{'='*80}")
    
    return all_results

if __name__ == "__main__":
    results = run_full_analysis()
