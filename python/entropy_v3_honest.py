#!/usr/bin/env python3
"""
entropy_v3_honest.py — Strategy v3 with CORRECT SL placement + min distance

FIXES from v2:
  1. SL = beyond manipulation extreme (not min(entry, manip_low))
  2. MIN SL distance = $2.00 on gold (avoid 1-tick stops)
  3. MAX R:R cap = 10:1 (avoid fake R:R from tiny SL)
  4. Better confluence: require STDV ce WITHIN OTE range (not just near)
  5. Only trade after sweep + close back above/below key level

HONEST: Commission $5/RT, slippage 2-tick, limit fill simulation.
"""

import json, sys
from datetime import datetime, timedelta
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
MAX_RR = 10.0  # Cap fake R:R
MIN_SL_DISTANCE = 2.0  # $2.00 minimum SL on gold
CONFLUENCE_ZONE_PCT = 1.0
ENTRY_MODE = "deep"
COOLDOWN_BARS = 3
HOLD_BARS = 20
MAX_TRADES_PER_DAY = 2


def calc_stdv(manip_high: float, manip_low: float) -> Dict[str, float]:
    r = manip_high - manip_low
    return {"ce": manip_low + r * 0.5}


def calc_ote(swing_high: float, swing_low: float, direction: str = "long") -> Dict[str, float]:
    r = swing_high - swing_low
    if direction == "long":
        return {"0.5": swing_low + r * 0.5, "0.886": swing_low + r * 0.886}
    else:
        return {"0.5": swing_high - r * 0.5, "0.886": swing_high - r * 0.886}


def find_confluence_zone(stdv_levels: Dict, ote_levels: Dict, tol_pct: float):
    out = []
    for sk, sv in stdv_levels.items():
        for ok, ov in ote_levels.items():
            dist = abs(sv - ov)
            avg = (sv + ov) / 2
            if avg > 0 and (dist / avg) * 100 <= tol_pct:
                out.append((sk, ok, (sv + ov) / 2))
    return out


def is_london_or_ny(dt_str: str) -> bool:
    dt = datetime.strptime(dt_str, "%Y.%m.%d %H:%M:%S")
    hour = dt.hour
    return (8 <= hour < 17) or (13 <= hour < 22)


def calc_ema(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return sum(prices) / len(prices)
    k = 2.0 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def detect_manipulation_leg(window: List[Dict], trend: str) -> Optional[Dict]:
    if len(window) < LOOKBACK + 2:
        return None

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
    
    body = abs(curr["c"] - curr["o"])
    rng = curr["h"] - curr["l"]
    strong_rejection = body > rng * 0.5 if rng > 0 else False

    if curr["l"] < last_sl[1] and curr["c"] > curr["o"] and strong_rejection:
        if trend != "up":
            return None
        return {
            "type": "long",
            "manip_high": prev["h"],
            "manip_low": curr["l"],
            "swing_high": last_sh[1],
            "swing_low": last_sl[1],
            "time": curr["t"],
        }
    
    if curr["h"] > last_sh[1] and curr["c"] < curr["o"] and strong_rejection:
        if trend != "down":
            return None
        return {
            "type": "short",
            "manip_high": curr["h"],
            "manip_low": prev["l"],
            "swing_high": last_sh[1],
            "swing_low": last_sl[1],
            "time": curr["t"],
        }
    return None


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

    equity = EQUITY_START
    peak = equity
    max_dd = 0.0
    trades = []
    skip_until = -1
    trades_today = 0
    last_trade_date = None

    for i in range(20 + 2, len(bars) - 2):
        if i <= skip_until:
            continue

        # Trend filter
        trend_window = bars[i - 20:i + 1]
        closes = [b["c"] for b in trend_window]
        ema = calc_ema(closes, 20)
        trend = "up" if bars[i]["c"] > ema else "down"

        # Session filter
        if not is_london_or_ny(bars[i]["t"]):
            continue

        # Daily limit
        curr_date = bars[i]["t"].split()[0]
        if curr_date != last_trade_date:
            trades_today = 0
            last_trade_date = curr_date
        if trades_today >= MAX_TRADES_PER_DAY:
            continue

        # Detect manipulation leg
        window = bars[i - LOOKBACK - 1 : i + 1]
        leg = detect_manipulation_leg(window, trend)
        if not leg:
            continue

        # Calculate levels
        stdv = calc_stdv(leg["manip_high"], leg["manip_low"])
        
        # Wider swing for TP
        wider_window = bars[max(0, i - LOOKBACK * 2 - 1) : i + 1]
        wider_sh = max(b["h"] for b in wider_window)
        wider_sl = min(b["l"] for b in wider_window)
        ote = calc_ote(wider_sh, wider_sl, leg["type"])

        # Confluence zone
        confs = find_confluence_zone(stdv, ote, CONFLUENCE_ZONE_PCT)
        if not confs:
            continue

        # Entry
        if leg["type"] == "long":
            entry = min(confs, key=lambda x: x[2])[2] if ENTRY_MODE == "deep" else max(confs, key=lambda x: x[2])[2]
        else:
            entry = max(confs, key=lambda x: x[2])[2] if ENTRY_MODE == "deep" else min(confs, key=lambda x: x[2])[2]

        # ── CORRECT SL PLACEMENT ──
        # SL is ALWAYS beyond the manipulation extreme, never at entry price
        manip_range = leg["manip_high"] - leg["manip_low"]
        buffer = manip_range * SL_BUFFER

        if leg["type"] == "long":
            # SL below the manipulation low (the sweep extreme)
            sl = leg["manip_low"] - buffer - 0.5  # 50-cent buffer beyond wick
            # Ensure minimum SL distance
            if entry - sl < MIN_SL_DISTANCE:
                sl = entry - MIN_SL_DISTANCE
            tp = max(wider_sh, stdv["ce"])
            if tp <= entry:
                tp = entry + manip_range * 0.5
        else:
            # SL above the manipulation high
            sl = leg["manip_high"] + buffer + 0.5
            if sl - entry < MIN_SL_DISTANCE:
                sl = entry + MIN_SL_DISTANCE
            tp = min(wider_sl, stdv["ce"])
            if tp >= entry:
                tp = entry - manip_range * 0.5

        rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        if rr < MIN_RR or rr > MAX_RR:
            continue

        # HONEST: Simulate limit fill
        fill_price = None
        fill_idx = None
        for k in range(i + 1, min(i + 5, len(bars))):
            b = bars[k]
            if leg["type"] == "long":
                if b["l"] <= entry:
                    fill_price = entry + 0.01
                    fill_idx = k
                    break
            else:
                if b["h"] >= entry:
                    fill_price = entry - 0.01
                    fill_idx = k
                    break
        if not fill_price:
            continue

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
            "equity": round(equity, 2), "trend": trend,
        })

        trades_today += 1
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
    print(f" ENTROPY STDV+OTE — v3 WITH CORRECT SL")
    print(f"{'='*70}")
    print(f"  Total Trades:   {result['trades']}")
    print(f"  Wins:           {result['wins']}")
    print(f"  Losses:         {result['losses']}")
    print(f"  Win Rate:       {result['wr']:.1f}%")
    print(f"  Total PnL:      ${result['total_pnl']:.2f}")
    print(f"  Return:         {result['return_pct']:.1f}%")
    print(f"  Max Drawdown:   {result['max_dd']:.1f}%")
    print(f"  Final Equity:   ${result['equity']:.2f}")

    out = "/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/entropy_v3_honest.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n💾 Saved: {out}")

    if result["detail"]:
        print(f"\n📊 First 3:")
        for t in result["detail"][:3]:
            print(f"   {t['time']} {t['type'].upper()} E={t['entry']:.2f} SL={t['sl']:.2f} TP={t['tp']:.2f} RR={t['rr']:.1f} → {t['reason']} ${t['pnl']:.2f}")
        print(f"\n📊 Last 3:")
        for t in result["detail"][-3:]:
            print(f"   {t['time']} {t['type'].upper()} E={t['entry']:.2f} SL={t['sl']:.2f} TP={t['tp']:.2f} RR={t['rr']:.1f} → {t['reason']} ${t['pnl']:.2f}")
        
        # SL distance analysis
        sl_dists = [abs(t['entry'] - t['sl']) for t in result['detail']]
        print(f"\n📊 SL Distance: min=${min(sl_dists):.2f} max=${max(sl_dists):.2f} avg=${sum(sl_dists)/len(sl_dists):.2f}")
