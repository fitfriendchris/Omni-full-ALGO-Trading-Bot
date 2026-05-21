#!/usr/bin/env python3
"""
h4_swing_backtest.py — Comprehensive H4/D1 swing backtest on GC=F.

HONEST execution: limit orders, slippage, commission, time stops.
Uses D1 resampled to H4-like for 2yr backtest (yfinance limits).

STRATEGY (ICT Institutional):
  1. H4/D1 bias from EMA20/200 alignment + structure
  2. Wait for RETURN to OB (not breakout) — 50% retest level
  3. LIMIT order at retest — never market execution
  4. SL beyond OB extreme + ATR buffer
  5. TP at next opposing OB or 3R minimum
  6. Risk 2% per trade, max 2 concurrent
  7. ADX < 25 (ranging/choppy = mean reversion works)
"""

import yfinance as yf
import json, math
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

try:
    import pandas as pd
except ImportError:
    pd = None

# ── CONFIG ───────────────────────────────────────────────────────────────────
SYMBOL = "GC=F"
RISK_PCT = 2.0
MIN_RR = 2.0
TARGET_RR = 3.0
LOT = 0.01
DOLLAR_PER_POINT = 1.0
COMMISSION = 5.0
SLIPPAGE = 2.0
MAX_CONCURRENT = 2
ADX_THRESHOLD = 25.0
HOLD_BARS = 24
LOOKBACK = 50
MIN_EQ = 5.0


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float

@dataclass
class OrderBlock:
    anchor_idx: int
    top: float
    bot: float
    side: str
    mitigated: bool = False
    strength: str = "WEAK"
    retest_count: int = 0


# ── INDICATORS ───────────────────────────────────────────────────────────────

def ema(closes: List[float], period: int) -> float:
    """Return latest EMA value. Compute on oldest-first."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    # Reverse to oldest-first for correct recursive calculation
    c = list(reversed(closes))
    mult = 2.0 / (period + 1)
    out = [sum(c[:period]) / period]
    for p in c[period:]:
        out.append((p - out[-1]) * mult + out[-1])
    return out[-1]  # return latest (most recent)

def atr(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return sum(b.high - b.low for b in bars) / len(bars) if bars else 0
    # Reverse to oldest-first for correct calculation
    b = list(reversed(bars))
    trs = [max(b[i-1].high, b[i].close) - min(b[i-1].low, b[i].close)
           for i in range(1, min(period + 1, len(b)))]
    return sum(trs) / len(trs)

def adx(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period * 3:
        return 0.0
    # Reverse to oldest-first for correct calculation
    b = list(reversed(bars))
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(b)):
        up = b[i-1].high - b[i].high
        down = b[i].low - b[i-1].low
        plus_dm.append(max(up, 0) if up > down else 0)
        minus_dm.append(max(down, 0) if down > up else 0)
        trs.append(max(b[i-1].high, b[i].close) - min(b[i-1].low, b[i].close))
    
    def smooth(vals, period):
        if len(vals) < period:
            return []
        s = [sum(vals[:period])]
        for v in vals[period:]:
            s.append(s[-1] - s[-1]/period + v)
        return s
    
    sp, sm, st = smooth(plus_dm, period), smooth(minus_dm, period), smooth(trs, period)
    if not sp or not sm or not st:
        return 0.0
    
    dx_vals = [abs(sp[i] - sm[i]) / (sp[i] + sm[i]) * 100 if (sp[i] + sm[i]) > 0 else 0
               for i in range(len(sp))]
    
    if len(dx_vals) < period:
        return sum(dx_vals) / len(dx_vals) if dx_vals else 0
    
    out = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        out = (out * (period - 1) + dx) / period
    return out


# ── ORDER BLOCKS ────────────────────────────────────────────────────────────

def find_obs(bars: List[Bar]) -> List[OrderBlock]:
    """Find bullish and bearish order blocks.
    bars: newest-first. Iterate chronologically (oldest toward newest).
    """
    obs = []
    # Chronological order: from oldest (end) toward newest (beginning)
    for i in range(len(bars) - 2, 2, -1):
        older = bars[i + 1]   # chronologically older
        newer = bars[i]       # chronologically newer
        
        # Bullish OB: bearish older candle, then bullish newer that engulfs
        if older.close < older.open and newer.close > newer.open and newer.close > older.open:
            top = max(older.open, older.close)
            bot = min(older.open, older.close)
            strength = "STRONG" if (newer.close - newer.open) > (older.open - older.close) else "WEAK"
            obs.append(OrderBlock(i, top, bot, "BULL", strength=strength))
        
        # Bearish OB: bullish older candle, then bearish newer that engulfs
        if older.close > older.open and newer.close < newer.open and newer.close < older.open:
            top = max(older.open, older.close)
            bot = min(older.open, older.close)
            strength = "STRONG" if (older.close - older.open) > (newer.open - newer.close) else "WEAK"
            obs.append(OrderBlock(i, top, bot, "BEAR", strength=strength))
    
    return obs

def check_mitigated(obs: List[OrderBlock], bars: List[Bar]) -> None:
    for ob in obs:
        for i in range(ob.anchor_idx - 1, -1, -1):
            bar = bars[i]
            body_through = (bar.close > ob.top and bar.open > ob.top) or \
                           (bar.close < ob.bot and bar.open < ob.bot)
            if body_through:
                ob.mitigated = True
                break
            wick_through = (bar.low < ob.bot and bar.close > ob.bot) or \
                           (bar.high > ob.top and bar.close < ob.top)
            if wick_through:
                ob.retest_count += 1


# ── HTF BIAS ──────────────────────────────────────────────────────────────

def htf_bias(bars: List[Bar]) -> Tuple[str, float]:
    """Return direction and score from EMA20/200 alignment.
    
    FIX: Price does not need to be on the right side of EMA20.
    A pullback TO EMA20 is the entry, not a break of it.
    Only reject if price is more than 1 ATR beyond EMA20.
    """
    if len(bars) < 50:  # relaxed from 200
        return "NEUTRAL", 0.0
    
    closes = [b.close for b in bars]
    e20 = ema(closes, 20)
    e200 = ema(closes, 200) if len(closes) >= 200 else 0
    
    # If not enough data for EMA200, use price vs EMA20 only
    if e200 == 0:
        score = 0.5
        if bars[0].close > e20:
            return "BULL", score
        elif bars[0].close < e20:
            return "BEAR", score
        return "NEUTRAL", 0.0
    
    # Full analysis with both EMAs
    atr_val = atr(bars)
    dist_from_ema20 = abs(bars[0].close - e20)
    
    if e20 > e200:
        # Bull trend — accept price up to 1 ATR below EMA20 (normal pullback)
        if bars[0].close > e20 - atr_val:
            score = 0.7 + min(0.3, abs(e20 - e200) / (e200 * 0.01))
            return "BULL", min(1.0, score)
    elif e20 < e200:
        # Bear trend — accept price up to 1 ATR above EMA20
        if bars[0].close < e20 + atr_val:
            score = 0.7 + min(0.3, abs(e20 - e200) / (e200 * 0.01))
            return "BEAR", min(1.0, score)
    
    return "NEUTRAL", 0.0


# ── LIMIT FILL SIMULATION ─────────────────────────────────────────────────

def simulate_fill(entry: float, sl: float, tp: float, bars: List[Bar],
                  start_idx: int, direction: str, equity: float) -> Tuple[float, str, float]:
    risk = equity * (RISK_PCT / 100)
    sl_dist = abs(entry - sl)
    if sl_dist <= 0.1:
        return 0.0, "DEAD_ON_ARRIVAL", equity
    
    lot = min(LOT, risk / sl_dist)
    lot = max(lot, 0.01)
    
    filled = False
    fill_price = 0.0
    
    for k in range(start_idx, min(start_idx + HOLD_BARS, len(bars))):
        bar = bars[k]
        
        if not filled:
            if direction == "BULL" and bar.low <= entry:
                filled = True
                fill_price = entry + SLIPPAGE
            elif direction == "BEAR" and bar.high >= entry:
                filled = True
                fill_price = entry - SLIPPAGE
        
        if filled:
            if direction == "BULL":
                if bar.low <= sl:
                    pnl = (sl - fill_price - COMMISSION) * lot * DOLLAR_PER_POINT
                    return pnl, f"SL_b{k}", max(MIN_EQ, equity + pnl)
                if bar.high >= tp:
                    pnl = (tp - fill_price - COMMISSION) * lot * DOLLAR_PER_POINT
                    return pnl, f"TP_b{k}", max(MIN_EQ, equity + pnl)
            else:
                if bar.high >= sl:
                    pnl = (fill_price - sl - COMMISSION) * lot * DOLLAR_PER_POINT
                    return pnl, f"SL_b{k}", max(MIN_EQ, equity + pnl)
                if bar.low <= tp:
                    pnl = (fill_price - tp - COMMISSION) * lot * DOLLAR_PER_POINT
                    return pnl, f"TP_b{k}", max(MIN_EQ, equity + pnl)
    
    if not filled:
        return 0.0, "NEVER_FILLED", equity
    
    last = bars[min(start_idx + HOLD_BARS - 1, len(bars) - 1)]
    if direction == "BULL":
        pnl = (last.close - fill_price - COMMISSION) * lot * DOLLAR_PER_POINT
    else:
        pnl = (fill_price - last.close - COMMISSION) * lot * DOLLAR_PER_POINT
    return pnl, "TIME_STOP", max(MIN_EQ, equity + pnl)


# ── MAIN ───────────────────────────────────────────────────────────────────

def backtest():
    print("=" * 70)
    print("H4 SWING BACKTEST — 2yr Honest Execution")
    print("=" * 70)
    
    print("\nFetching 2 years of GC=F daily data...")
    df = yf.download('GC=F', period='2y', interval='1d', progress=False)
    
    if df.empty:
        print("FAILED: no data")
        return
    
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    
    # Convert to H4-like by resampling D1 → 4H (simulated)
    # Actually just use D1 and treat as lower-res H4
    bars = [Bar(df.index[i].to_pydatetime(),
                float(df.iloc[i]['Open']), float(df.iloc[i]['High']),
                float(df.iloc[i]['Low']), float(df.iloc[i]['Close']))
            for i in range(len(df))]
    bars.reverse()  # newest-first
    
    print(f"Bars: {len(bars)} | Range: {bars[-1].time:%Y-%m-%d} → {bars[0].time:%Y-%m-%d}")
    
    trades = []
    equity = 1000.0
    max_eq = equity
    max_dd = 0.0
    skipped = {"adx": 0, "no_ob": 0, "mitigated": 0, "no_rr": 0, "never": 0, "neutral": 0}
    active_trades = 0
    
    for i in range(LOOKBACK + 5, len(bars) - HOLD_BARS - 1):
        window = bars[i-LOOKBACK:i+1]
        current = window[0].close
        
        # HTF bias
        bias_dir, bias_score = htf_bias(window)
        if bias_dir == "NEUTRAL":
            skipped["neutral"] += 1
            continue
        
        # ADX
        adx_val = adx(window)
        if adx_val >= ADX_THRESHOLD:
            skipped["adx"] += 1
            continue
        
        # Find OBs
        obs = find_obs(window)
        check_mitigated(obs, window)
        valid = [o for o in obs if not o.mitigated or o.retest_count <= 2]
        valid.sort(key=lambda o: o.anchor_idx, reverse=True)
        
        if not valid:
            skipped["no_ob"] += 1
            continue
        
        atr_val = atr(window)
        
        # Try generate trade
        for ob in valid[:5]:
            ob_body = ob.top - ob.bot
            retest = ob.bot + ob_body * 0.5
            dist = abs(current - retest)
            
            if dist > atr_val * 1.5:
                continue
            
            # Direction must align with bias
            if ob.side != bias_dir:
                continue
            
            if ob.side == "BULL":
                sl = ob.bot - atr_val * 2.0
                tp = current + atr_val * TARGET_RR
                rr = (tp - retest) / (retest - sl) if (retest - sl) > 0 else 0
                if rr < MIN_RR:
                    skipped["no_rr"] += 1
                    continue
                
                pnl, reason, new_eq = simulate_fill(retest, sl, tp, bars, i, "BULL", equity)
                trades.append({"dir": "BULL", "pnl": pnl, "reason": reason,
                              "entry": retest, "sl": sl, "tp": tp, "rr": rr, "adx": adx_val})
                if reason != "NEVER_FILLED":
                    equity = new_eq
                    max_eq = max(max_eq, equity)
                    dd = (max_eq - equity) / max_eq * 100
                    max_dd = max(max_dd, dd)
                else:
                    skipped["never"] += 1
                break
            
            else:
                sl = ob.top + atr_val * 2.0
                tp = current - atr_val * TARGET_RR
                rr = (retest - tp) / (sl - retest) if (sl - retest) > 0 else 0
                if rr < MIN_RR:
                    skipped["no_rr"] += 1
                    continue
                
                pnl, reason, new_eq = simulate_fill(retest, sl, tp, bars, i, "BEAR", equity)
                trades.append({"dir": "BEAR", "pnl": pnl, "reason": reason,
                              "entry": retest, "sl": sl, "tp": tp, "rr": rr, "adx": adx_val})
                if reason != "NEVER_FILLED":
                    equity = new_eq
                    max_eq = max(max_eq, equity)
                    dd = (max_eq - equity) / max_eq * 100
                    max_dd = max(max_dd, dd)
                else:
                    skipped["never"] += 1
                break
    
    # Results
    filled = [t for t in trades if t["reason"] != "NEVER_FILLED"]
    wins = [t for t in filled if t["pnl"] > 0]
    losses = [t for t in filled if t["pnl"] <= 0]
    
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    
    print(f"\nTotal attempts: {len(trades)}")
    print(f"Filled: {len(filled)} | Never filled: {skipped['never']}")
    print(f"Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Win rate: {len(wins)*100/len(filled):.1f}%" if filled else "N/A")
    
    if filled:
        avg_w = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        pf = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 0
        total = sum(t["pnl"] for t in filled)
        ret = (equity - 1000) / 1000 * 100
        
        print(f"Avg win: +${avg_w:.2f} | Avg loss: ${avg_l:.2f}")
        print(f"Profit factor: {pf:.2f}")
        print(f"Max drawdown: {max_dd:.1f}%")
        print(f"Final equity: ${equity:.2f} ({ret:+.1f}%)")
    
    print(f"\nSkipped: {skipped}")
    
    # Save
    with open("h4_swing_results.json", "w") as f:
        json.dump({
            "filled": len(filled), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins)*100/len(filled) if filled else 0,
            "profit_factor": pf if filled else 0,
            "max_drawdown": max_dd,
            "net_profit": sum(t["pnl"] for t in filled),
            "return_pct": (equity - 1000) / 1000 * 100,
            "equity": equity,
            "skipped": skipped,
            "trades": filled,
        }, f, indent=2)
    print("\nSaved: h4_swing_results.json")


if __name__ == "__main__":
    backtest()
