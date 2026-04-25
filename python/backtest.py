#!/usr/bin/env python3
"""
backtest.py — ICT walk-forward backtester
Downloads 2yr H1 OHLCV via yfinance, runs the sweep-gate+OB detection
functions from ict_precision.py, simulates realistic limit-entry trades,
and reports per-1000-trade statistics.
"""
import json, math, statistics, sys, os
from dataclasses import dataclass
from typing import List, Optional
from collections import defaultdict

import pandas as pd
import yfinance as yf

# ── ICT detection imports ────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import (
    Bar,
    detect_sweep_high, detect_sweep_low,
    find_bearish_ob, find_bullish_ob,
    find_equal_highs, find_equal_lows,
    get_ob_precision_entry,
    detect_turtle_soup,
    _calc_atr,
    _snap_to_interval, _dist_intervals,
)

# ── Instrument config ────────────────────────────────────────────
SYMBOLS = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "USDCAD=X": "USDCAD",
    "GBPJPY=X": "GBPJPY",
    "GC=F":     "XAUUSD",
    "SI=F":     "XAGUSD",
    "NQ=F":     "NAS100",
    "YM=F":     "US30",
}

SPREAD = {
    "EURUSD": 0.00015, "GBPUSD": 0.00020, "USDJPY": 0.030,
    "AUDUSD": 0.00020, "USDCAD": 0.00025, "GBPJPY": 0.050,
    "XAUUSD": 0.50,    "XAGUSD": 0.030,
    "NAS100": 2.0,     "US30":   3.0,
}


# ── Data fetching ────────────────────────────────────────────────
def fetch_bars(ticker: str) -> List[Bar]:
    """Download 2yr H1 OHLCV from yfinance → Bar list (oldest first)."""
    df = yf.download(ticker, period="2y", interval="1h",
                     progress=False, auto_adjust=True)
    if df.empty:
        return []
    # Flatten MultiIndex columns that newer yfinance may produce
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
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


def resample_d1(h1_bars: List[Bar]) -> List[Bar]:
    """H1 → D1 resampling for bias detection."""
    by_day: dict = defaultdict(list)
    for b in h1_bars:
        by_day[b.time[:10]].append(b)
    d1 = []
    for day in sorted(by_day):
        day_bars = by_day[day]
        d1.append(Bar(
            time=day,
            o=day_bars[0].o,
            h=max(b.h for b in day_bars),
            l=min(b.l for b in day_bars),
            c=day_bars[-1].c,
            v=sum(b.v for b in day_bars),
        ))
    return d1


def d1_bias(d1_bars: List[Bar]) -> str:
    """HH+HL = BULLISH, LH+LL = BEARISH, else NEUTRAL."""
    if len(d1_bars) < 5:
        return "NEUTRAL"
    w = d1_bars[-5:]
    hh = w[-1].h > w[-3].h
    hl = w[-1].l > w[-3].l
    lh = w[-1].h < w[-3].h
    ll = w[-1].l < w[-3].l
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "NEUTRAL"


# ── Setup detection ──────────────────────────────────────────────
def detect_setups(h1_bars: List[Bar], bias: str, symbol: str) -> List[dict]:
    """
    Run sweep-gate detection on a H1 window.
    Returns list of setup dicts ready for simulation.
    """
    if len(h1_bars) < 60:
        return []

    atr = _calc_atr(h1_bars[-20:], period=14)
    if atr <= 0:
        return []

    spread   = SPREAD.get(symbol, atr * 0.05)
    cur      = h1_bars[-1].c
    liq_bars  = h1_bars[-60:]   # equal-high/low detection context
    sweep_bars = h1_bars[-10:]  # recent bars for sweep check
    ob_bars    = h1_bars[-30:]  # OB search window

    eq_highs = find_equal_highs(liq_bars, tolerance_pct=0.0015)
    eq_lows  = find_equal_lows(liq_bars,  tolerance_pct=0.0015)

    struct_highs = sorted([b.h for b in liq_bars[-30:]], reverse=True)[:6]
    struct_lows  = sorted([b.l for b in liq_bars[-30:]])[:6]

    liq_highs = sorted(set(eq_highs + struct_highs), reverse=True)[:8]
    liq_lows  = sorted(set(eq_lows  + struct_lows))[:8]

    dist = _dist_intervals.get(symbol, _dist_intervals.get("default", 0.0))
    setups = []

    # ── SELL: sweep of high → OB sell ────────────────────────────
    for level in liq_highs[:5]:
        if not detect_sweep_high(sweep_bars, level, tolerance_pct=0.002):
            continue
        ob = find_bearish_ob(ob_bars, start=0, search=12)
        if not ob:
            continue
        ob_low, ob_high = ob

        # OB must be near the swept level, not a stale OB far below
        if abs(ob_high - level) > atr * 3:
            continue

        ob_prec = get_ob_precision_entry(ob_low, ob_high, "SELL")
        entry = ob_prec["ote_50"] - spread

        sweep_ext = max(b.h for b in sweep_bars[:4]) if sweep_bars else level * 1.001
        sl = max(sweep_ext, level) * 1.0012
        sl = max(sl, ob_high + (ob_high - ob_low) * 0.3)
        risk = sl - entry
        if risk <= 0 or risk > atr * 4 or risk < atr * 0.1:
            continue

        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.5
        tp3 = entry - risk * 4.0
        if dist > 0:
            tp1 = _snap_to_interval(tp1, dist, "SELL")
            tp2 = _snap_to_interval(tp2, dist, "SELL")

        confidence = 48
        _turtle = bool(detect_turtle_soup(sweep_bars, level, "SELL"))
        if _turtle:
            confidence += 12
        _inducement = level in eq_highs[:3]
        if _inducement:
            confidence += 10
        if bias == "BEARISH":
            confidence += 12
        elif bias == "BULLISH":
            confidence -= 15

        if confidence < 35:
            continue

        setups.append({
            "direction": "SELL", "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "risk": risk, "atr": atr, "confidence": confidence,
            "turtle_soup": _turtle, "inducement": _inducement,
            "level": level,
        })

    # ── BUY: sweep of low → OB buy ───────────────────────────────
    for level in liq_lows[:5]:
        if not detect_sweep_low(sweep_bars, level, tolerance_pct=0.002):
            continue
        ob = find_bullish_ob(ob_bars, start=0, search=12)
        if not ob:
            continue
        ob_low, ob_high = ob

        if abs(ob_low - level) > atr * 3:
            continue

        ob_prec = get_ob_precision_entry(ob_low, ob_high, "BUY")
        entry = ob_prec["ote_50"] + spread

        sweep_ext = min(b.l for b in sweep_bars[:4]) if sweep_bars else level * 0.999
        sl = min(sweep_ext, level) * 0.9988
        sl = min(sl, ob_low - (ob_high - ob_low) * 0.3)
        risk = entry - sl
        if risk <= 0 or risk > atr * 4 or risk < atr * 0.1:
            continue

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.5
        tp3 = entry + risk * 4.0
        if dist > 0:
            tp1 = _snap_to_interval(tp1, dist, "BUY")
            tp2 = _snap_to_interval(tp2, dist, "BUY")

        confidence = 48
        _turtle = bool(detect_turtle_soup(sweep_bars, level, "BUY"))
        if _turtle:
            confidence += 12
        _inducement = level in eq_lows[:3]
        if _inducement:
            confidence += 10
        if bias == "BULLISH":
            confidence += 12
        elif bias == "BEARISH":
            confidence -= 15

        if confidence < 35:
            continue

        setups.append({
            "direction": "BUY", "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "risk": risk, "atr": atr, "confidence": confidence,
            "turtle_soup": _turtle, "inducement": _inducement,
            "level": level,
        })

    return setups


# ── Trade simulation ─────────────────────────────────────────────
def simulate(setup: dict, future_bars: List[Bar]) -> dict:
    """
    Simulate a limit-entry trade against future price bars.
    SL takes priority within any bar (conservative).
    Checks TP2 before TP1 — if TP2 reached, TP1 was hit first.
    """
    d   = setup["direction"]
    entry = setup["entry"]
    sl    = setup["sl"]
    tp1   = setup["tp1"]
    tp2   = setup["tp2"]

    filled = False
    btf = btc = 0
    outcome = "EXPIRED"
    r = 0.0

    for i, bar in enumerate(future_bars[:72]):
        if not filled:
            hit = (d == "SELL" and bar.h >= entry) or \
                  (d == "BUY"  and bar.l <= entry)
            if hit:
                filled = True
                btf = i + 1
        else:
            if d == "SELL":
                if bar.h >= sl:          outcome, r, btc = "LOSS", -1.0, i+1; break
                elif bar.l <= tp2:       outcome, r, btc = "WIN",  2.5,  i+1; break
                elif bar.l <= tp1:       outcome, r, btc = "WIN",  1.5,  i+1; break
            else:
                if bar.l <= sl:          outcome, r, btc = "LOSS", -1.0, i+1; break
                elif bar.h >= tp2:       outcome, r, btc = "WIN",  2.5,  i+1; break
                elif bar.h >= tp1:       outcome, r, btc = "WIN",  1.5,  i+1; break

    if filled and outcome == "EXPIRED":
        outcome = "TIMEOUT"

    return {"filled": filled, "outcome": outcome, "r": r, "btf": btf, "btc": btc}


# ── Monte Carlo stress test ──────────────────────────────────────
def monte_carlo(results: list, n_sims: int = 1000, risk_pct: float = 0.01) -> dict:
    """
    Shuffle the trade sequence n_sims times to test path-dependence.
    Returns distribution of final equity, max-DD, and consecutive-loss stats.
    """
    import random
    r_vals = [t["r_multiple"] for t in results if t["outcome"] in ("WIN", "LOSS")]
    if len(r_vals) < 10:
        return {}

    final_equities, max_dds, max_consec = [], [], []

    for _ in range(n_sims):
        seq = r_vals[:]
        random.shuffle(seq)
        eq = 1.0; peak = 1.0; mx_dd = 0.0
        consec = 0; mx_consec = 0
        for rv in seq:
            eq *= (1 + rv * risk_pct)
            peak = max(peak, eq)
            mx_dd = max(mx_dd, (peak - eq) / peak)
            if rv < 0:
                consec += 1; mx_consec = max(mx_consec, consec)
            else:
                consec = 0
        final_equities.append(eq)
        max_dds.append(mx_dd)
        max_consec.append(mx_consec)

    final_equities.sort(); max_dds.sort(); max_consec.sort()
    n = len(final_equities)
    return {
        "median_equity":  final_equities[n // 2],
        "p5_equity":      final_equities[int(n * 0.05)],
        "p95_equity":     final_equities[int(n * 0.95)],
        "median_max_dd":  max_dds[n // 2],
        "p95_max_dd":     max_dds[int(n * 0.95)],
        "median_consec_l": max_consec[n // 2],
        "p95_consec_l":   max_consec[int(n * 0.95)],
    }


# ── Walk-forward engine ──────────────────────────────────────────
def run_backtest(lookback: int = 150, step: int = 5) -> list:
    all_trades = []

    for ticker, symbol in SYMBOLS.items():
        print(f"  {symbol:8s}  downloading...", end=" ", flush=True)
        bars = fetch_bars(ticker)
        if len(bars) < lookback + 80:
            print(f"SKIP ({len(bars)} bars)")
            continue
        print(f"{len(bars):,} bars → scanning", end=" ", flush=True)

        d1_bars = resample_d1(bars)
        close_bar_idx = -1   # last trade close bar index; one trade at a time per symbol
        n_setups = n_filled = 0

        i = lookback
        while i < len(bars) - 72:
            # Respect active trade lock
            if i <= close_bar_idx:
                i += step
                continue

            window = bars[i - lookback: i]
            future = bars[i: i + 72]

            # D1 bias from history up to this scan point
            d1_window = [b for b in d1_bars if b.time <= bars[i].time[:10]]
            bias = d1_bias(d1_window[-12:] if d1_window else [])

            setups = detect_setups(window, bias, symbol)
            n_setups += len(setups)

            for setup in setups[:1]:
                res = simulate(setup, future)
                if not res["filled"]:
                    continue
                n_filled += 1
                all_trades.append({
                    "symbol":     symbol,
                    "direction":  setup["direction"],
                    "outcome":    res["outcome"],
                    "r_multiple": res["r"],
                    "confidence": setup["confidence"],
                    "turtle_soup": setup["turtle_soup"],
                    "inducement": setup["inducement"],
                    "bias":       bias,
                    "btf":        res["btf"],
                    "btc":        res["btc"],
                })
                if res["outcome"] in ("WIN", "LOSS"):
                    close_bar_idx = i + res["btf"] + res["btc"]

            i += step

        print(f"| setups={n_setups}  filled={n_filled}")

    return all_trades


# ── Reporting ────────────────────────────────────────────────────
def report(trades: list) -> None:
    closed  = [t for t in trades if t["outcome"] in ("WIN", "LOSS")]
    timeout = [t for t in trades if t["outcome"] == "TIMEOUT"]
    wins    = [t for t in closed if t["outcome"] == "WIN"]
    losses  = [t for t in closed if t["outcome"] == "LOSS"]

    n = len(closed)
    if n == 0:
        print("No closed trades recorded."); return

    wr  = len(wins) / n * 100
    avg = statistics.mean(t["r_multiple"] for t in closed)
    exp = avg   # expectancy already in R units

    # Live equity curve (1% risk per trade)
    eq = 1.0; peak = 1.0; max_dd = 0.0
    consec = 0; mx_consec = 0
    for t in closed:
        eq  *= (1 + t["r_multiple"] * 0.01)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
        if t["outcome"] == "LOSS":
            consec += 1; mx_consec = max(mx_consec, consec)
        else:
            consec = 0

    # Scale to 1000 trades
    scale = 1000 / n

    # Confidence tiers
    def tier_stats(label, tlist):
        tw = [t for t in tlist if t["outcome"] == "WIN"]
        if not tlist: return
        ar = statistics.mean(t["r_multiple"] for t in tlist)
        print(f"  {label:18s}: {len(tlist):4d} trades ({len(tlist)*scale:5.0f}/1k) "
              f"| WR {len(tw)/len(tlist)*100:5.1f}% | Avg R {ar:+.3f}")

    # Per-symbol
    by_sym: dict = defaultdict(lambda: {"w": 0, "n": 0, "r": []})
    for t in closed:
        by_sym[t["symbol"]]["n"] += 1
        by_sym[t["symbol"]]["r"].append(t["r_multiple"])
        if t["outcome"] == "WIN":
            by_sym[t["symbol"]]["w"] += 1

    # Monte Carlo
    mc = monte_carlo(trades)

    print()
    print("═" * 64)
    print("  ICT SWEEP-GATE STRATEGY — WALK-FORWARD BACKTEST RESULTS")
    print("═" * 64)
    print(f"  Data              : 10 instruments × 2yr H1 (yfinance)")
    print(f"  Filled trades     : {n:,}   (timeout/expired: {len(timeout)})")
    print(f"  Scaled to 1 000   : {round(len(wins)*scale)} wins | {round(len(losses)*scale)} losses")
    print()
    print("  ── Core Metrics ─────────────────────────────────────────")
    print(f"  Win rate          : {wr:.1f}%")
    print(f"  Avg R per trade   : {avg:+.4f}R")
    print(f"  Avg winning R     : +{statistics.mean(t['r_multiple'] for t in wins):.2f}R")
    print(f"  Avg losing R      : {statistics.mean(t['r_multiple'] for t in losses):.2f}R")
    print(f"  Expectancy        : {exp:+.4f}R / trade")
    print(f"  Expected R / 1000 : {exp*1000:+.1f}R")
    print(f"  Max consec losses : {mx_consec}")
    print(f"  Max drawdown (1%) : {max_dd*100:.1f}%")
    print(f"  Final equity (1%) : {eq:.4f}× ({(eq-1)*100:+.1f}%)")
    print()
    print("  ── Confirmation Breakdown ───────────────────────────────")
    ts_cl  = [t for t in closed if t["turtle_soup"]]
    ts_win = [t for t in ts_cl if t["outcome"] == "WIN"]
    in_cl  = [t for t in closed if t["inducement"]]
    in_win = [t for t in in_cl if t["outcome"] == "WIN"]
    if ts_cl:
        print(f"  Turtle Soup       : {len(ts_cl):4d} trades | WR {len(ts_win)/len(ts_cl)*100:.1f}%  | "
              f"Avg R {statistics.mean(t['r_multiple'] for t in ts_cl):+.3f}")
    if in_cl:
        print(f"  Inducement        : {len(in_cl):4d} trades | WR {len(in_win)/len(in_cl)*100:.1f}%  | "
              f"Avg R {statistics.mean(t['r_multiple'] for t in in_cl):+.3f}")
    # Bias-aligned trades
    ba = [t for t in closed if
          (t["direction"]=="SELL" and t["bias"]=="BEARISH") or
          (t["direction"]=="BUY"  and t["bias"]=="BULLISH")]
    ba_win = [t for t in ba if t["outcome"]=="WIN"]
    if ba:
        print(f"  Bias-aligned      : {len(ba):4d} trades | WR {len(ba_win)/len(ba)*100:.1f}%  | "
              f"Avg R {statistics.mean(t['r_multiple'] for t in ba):+.3f}")
    print()
    print("  ── Confidence Tiers ─────────────────────────────────────")
    tier_stats("High  (≥70)", [t for t in closed if t["confidence"] >= 70])
    tier_stats("Mid   (55–69)", [t for t in closed if 55 <= t["confidence"] < 70])
    tier_stats("Low   (<55)", [t for t in closed if t["confidence"] < 55])
    print()
    print("  ── Per-Symbol Performance ───────────────────────────────")
    for sym, s in sorted(by_sym.items(), key=lambda x: -x[1]["n"]):
        swr = s["w"] / s["n"] * 100
        sar = statistics.mean(s["r"])
        print(f"  {sym:8s}: {s['n']:4d} trades | WR {swr:5.1f}% | "
              f"Avg R {sar:+.4f} | "
              f"Per-1k: {s['n']*scale:5.0f} trades")
    if mc:
        print()
        print("  ── Monte Carlo (1000 shuffles, 1% risk) ─────────────────")
        print(f"  Median final eq   : {mc['median_equity']:.4f}× ({(mc['median_equity']-1)*100:+.1f}%)")
        print(f"  5th pct equity    : {mc['p5_equity']:.4f}×  (worst 5%)")
        print(f"  95th pct equity   : {mc['p95_equity']:.4f}×  (best 5%)")
        print(f"  Median max DD     : {mc['median_max_dd']*100:.1f}%")
        print(f"  95th pct max DD   : {mc['p95_max_dd']*100:.1f}%  (worst 5% scenario)")
        print(f"  Median consec L   : {mc['median_consec_l']}")
        print(f"  95th consec L     : {mc['p95_consec_l']}  (bad-luck run)")
    print("═" * 64)


if __name__ == "__main__":
    print("ICT Walk-Forward Backtester  —  10 instruments, 2yr H1")
    print("─" * 50)
    trades = run_backtest()
    report(trades)
    # Save raw results for future Monte Carlo / analysis
    with open("backtest_results.json", "w") as f:
        json.dump(trades, f, indent=2)
    print(f"\nSaved {len(trades)} trade records → backtest_results.json")
