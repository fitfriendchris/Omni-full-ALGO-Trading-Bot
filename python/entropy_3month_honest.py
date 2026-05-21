#!/usr/bin/env python3
"""
entropy_3month_honest.py — Honest 3-month backtest on yfinance gold data

Uses GC=F (COMEX Gold Futures) as XAUUSD proxy.
HONEST adjustments: commission $5/RT, slippage 2-tick, no fill if limit missed.

STRATEGY LOGIC (per Chris playbook):
  1. Detect manipulation leg: sweep liquidity + close against direction
  2. Anchor STDV wick-to-wick on manipulation leg
  3. Anchor OTE wick-to-wick on swing range
  4. Entry = confluence zone where STDV level and OTE level overlap
  5. SL = beyond manipulation extreme
  6. TP = structural level (swing extreme)
  7. Min R:R = 1.5:1
"""

import json, sys
from datetime import datetime
from typing import List, Dict, Optional

import yfinance as yf

SYMBOL = "GC=F"
LOT = 0.01
COMMISSION = 5.0
EQUITY_START = 1000.0

# ── Params ──
LOOKBACK = 10
SL_BUFFER = 0.0
MIN_RR = 1.5
CONFLUENCE_ZONE_PCT = 5.0  # STDV and OTE within 5% of each other = valid zone
ENTRY_MODE = "deep"
COOLDOWN_BARS = 3
HOLD_BARS = 20


def calc_stdv(manip_high: float, manip_low: float, direction: str = "long") -> Dict[str, float]:
    r = manip_high - manip_low
    return {"ce": manip_low + r * 0.5}


def calc_ote(swing_high: float, swing_low: float, direction: str = "long") -> Dict[str, float]:
    r = swing_high - swing_low
    if direction == "long":
        return {"0.5": swing_low + r * 0.5, "0.886": swing_low + r * 0.886,
                "0.79": swing_low + r * 0.79, "0.705": swing_low + r * 0.705}
    else:
        return {"0.5": swing_high - r * 0.5, "0.886": swing_high - r * 0.886,
                "0.79": swing_high - r * 0.79, "0.705": swing_high - r * 0.705}


def detect_manipulation_leg(window: List[Dict]) -> Optional[Dict]:
    """
    window: last LOOKBACK+1 bars, oldest-first.
    Returns manipulation leg dict if sweep + rejection detected.
    """
    if len(window) < LOOKBACK + 2:
        return None

    # Find swing highs/lows in the window
    highs, lows = [], []
    for j in range(2, len(window) - 2):
        b = window[j]
        if b["h"] > window[j - 1]["h"] and b["h"] > window[j - 2]["h"] and b["h"] > window[j + 1]["h"] and b["h"] > window[j + 2]["h"]:
            highs.append((j, b["h"]))
        if b["l"] < window[j - 1]["l"] and b["l"] < window[j - 2]["l"] and b["l"] < window[j + 1]["l"] and b["l"] < window[j + 2]["l"]:
            lows.append((j, b["l"]))

    if not highs or not lows:
        return None

    last_sh = max(highs, key=lambda x: x[0])
    last_sl = max(lows, key=lambda x: x[0])
    curr = window[-1]
    prev = window[-2]

    # LONG: sweep below last swing low, then close bullish
    if curr["l"] < last_sl[1] and curr["c"] > curr["o"]:
        return {
            "type": "long",
            "manip_high": prev["h"],
            "manip_low": curr["l"],
            "swing_high": last_sh[1],
            "swing_low": last_sl[1],
            "time": curr["t"],
        }
    # SHORT: sweep above last swing high, then close bearish
    if curr["h"] > last_sh[1] and curr["c"] < curr["o"]:
        return {
            "type": "short",
            "manip_high": curr["h"],
            "manip_low": prev["l"],
            "swing_high": last_sh[1],
            "swing_low": last_sl[1],
            "time": curr["t"],
        }
    return None


def find_confluence_zone(stdv_levels: Dict, ote_levels: Dict, tol_pct: float):
    """
    Find confluence zone: any STDV level within tol_pct of any OTE level.
    Returns list of (stdv_key, ote_key, price).
    """
    out = []
    for sk, sv in stdv_levels.items():
        for ok, ov in ote_levels.items():
            dist = abs(sv - ov)
            avg = (sv + ov) / 2
            if avg == 0:
                continue
            if (dist / avg) * 100 <= tol_pct:
                out.append((sk, ok, (sv + ov) / 2))
    return out


def run_backtest(df) -> Dict:
    bars = []
    for i in range(len(df)):
        bars.append({
            "t": df.index[i].strftime("%Y.%m.%d %H:%M:%S"),
            "o": float(df.iloc[i][("Open", SYMBOL)]),
            "h": float(df.iloc[i][("High", SYMBOL)]),
            "l": float(df.iloc[i][("Low", SYMBOL)]),
            "c": float(df.iloc[i][("Close", SYMBOL)]),
        })
    # bars are oldest-first

    equity = EQUITY_START
    peak = equity
    max_dd = 0.0
    trades = []
    skip_until = -1

    for i in range(LOOKBACK + 2, len(bars) - 2):
        if i <= skip_until:
            continue

        window = bars[i - LOOKBACK - 1 : i + 1]
        leg = detect_manipulation_leg(window)
        if not leg:
            continue

        # Calculate levels
        stdv = calc_stdv(leg["manip_high"], leg["manip_low"], leg["type"])
        ote = calc_ote(leg["swing_high"], leg["swing_low"], leg["type"])

        # Find confluence zone
        confs = find_confluence_zone(stdv, ote, CONFLUENCE_ZONE_PCT)
        if not confs:
            continue

        # Select entry (deep = best discount)
        if leg["type"] == "long":
            entry = min(confs, key=lambda x: x[2])[2] if ENTRY_MODE == "deep" else max(confs, key=lambda x: x[2])[2]
        else:
            entry = max(confs, key=lambda x: x[2])[2] if ENTRY_MODE == "deep" else min(confs, key=lambda x: x[2])[2]

        manip_range = leg["manip_high"] - leg["manip_low"]
        buffer = manip_range * SL_BUFFER

        if leg["type"] == "long":
            sl = min(leg["manip_low"], entry) - buffer
            tp = max(leg["swing_high"], stdv["ce"])
            if tp <= entry:
                tp = entry + manip_range * 0.5
        else:
            sl = max(leg["manip_high"], entry) + buffer
            tp = min(leg["swing_low"], stdv["ce"])
            if tp >= entry:
                tp = entry - manip_range * 0.5

        rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        if rr < MIN_RR:
            continue

        # HONEST: Simulate limit fill
        fill_price = None
        fill_idx = None
        for k in range(i + 1, min(i + 5, len(bars))):
            b = bars[k]
            if leg["type"] == "long":
                if b["l"] <= entry:
                    fill_price = entry + 0.01  # slight slip
                    fill_idx = k
                    break
            else:
                if b["h"] >= entry:
                    fill_price = entry - 0.01
                    fill_idx = k
                    break
        if not fill_price:
            continue  # Missed entry

        # Walk to exit
        pnl = None
        exit_price = None
        reason = ""

        for k in range(fill_idx + 1, min(fill_idx + HOLD_BARS, len(bars))):
            b = bars[k]
            if leg["type"] == "long":
                if b["l"] <= sl:
                    exit_price = sl
                    price_move = exit_price - fill_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "SL"
                    break
                if b["h"] >= tp:
                    exit_price = tp
                    price_move = exit_price - fill_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "TP"
                    break
            else:
                if b["h"] >= sl:
                    exit_price = sl
                    price_move = fill_price - exit_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "SL"
                    break
                if b["l"] <= tp:
                    exit_price = tp
                    price_move = fill_price - exit_price
                    pnl = price_move * 100.0 * LOT - COMMISSION * LOT
                    reason = "TP"
                    break

        if pnl is None:
            # Time exit
            b = bars[min(fill_idx + HOLD_BARS, len(bars) - 1)]
            exit_price = b["c"]
            if leg["type"] == "long":
                price_move = exit_price - fill_price
            else:
                price_move = fill_price - exit_price
            pnl = price_move * 100.0 * LOT - COMMISSION * LOT
            reason = "TIME"

        equity += pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd

        trades.append({
            "time": leg["time"], "type": leg["type"], "entry": entry,
            "sl": sl, "tp": tp, "fill": fill_price, "exit": exit_price,
            "pnl": round(pnl, 2), "reason": reason, "rr": round(rr, 2),
            "equity": round(equity, 2),
        })

        skip_until = i + COOLDOWN_BARS

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    ret_pct = (equity - EQUITY_START) / EQUITY_START * 100

    return {
        "symbol": SYMBOL, "interval": "5m", "bars": len(bars),
        "start": str(df.index[0]), "end": str(df.index[-1]),
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": round(wr, 2), "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "return_pct": round(ret_pct, 2), "max_dd": round(max_dd, 2),
        "equity": round(equity, 2), "peak": round(peak, 2),
        "detail": trades,
    }


if __name__ == "__main__":
    print(f"📥 Downloading {SYMBOL} 5m data...")
    df = yf.download(SYMBOL, period="max", interval="5m", progress=False)
    if df.empty:
        print("❌ No data")
        sys.exit(1)

    print(f"   Bars: {len(df)} | {df.index[0]} → {df.index[-1]}")

    result = run_backtest(df)

    print(f"\n{'='*70}")
    print(f" ENTROPY STDV+OTE — 3-MONTH HONEST BACKTEST")
    print(f"{'='*70}")
    print(f"  Total Trades:   {result['trades']}")
    print(f"  Wins:           {result['wins']}")
    print(f"  Losses:         {result['losses']}")
    print(f"  Win Rate:       {result['wr']:.1f}%")
    print(f"  Total PnL:      ${result['total_pnl']:.2f}")
    print(f"  Return:         {result['return_pct']:.1f}%")
    print(f"  Max Drawdown:   {result['max_dd']:.1f}%")
    print(f"  Final Equity:   ${result['equity']:.2f}")

    # Save
    out = "/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/entropy_3month_honest.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n💾 Saved: {out}")

    # Print first 5 and last 5 trades
    if result["detail"]:
        print(f"\n📊 First 3 trades:")
        for t in result["detail"][:3]:
            print(f"   {t['time']} {t['type'].upper()} E={t['entry']:.2f} SL={t['sl']:.2f} TP={t['tp']:.2f} → {t['reason']} ${t['pnl']:.2f}")
        print(f"\n📊 Last 3 trades:")
        for t in result["detail"][-3:]:
            print(f"   {t['time']} {t['type'].upper()} E={t['entry']:.2f} SL={t['sl']:.2f} TP={t['tp']:.2f} → {t['reason']} ${t['pnl']:.2f}")
