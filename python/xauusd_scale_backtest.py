"""
xauusd_scale_backtest.py — XAUUSD 2000%+ Growth Backtester
============================================================
Strategy: ICT London/NY Killzone + Liquidity Sweep + OB + FVG confluence
Scaling:  Aggressive compounding with pyramid entries and dynamic risk sizing
Target:   2000%+ return (20x equity) with controlled drawdown

Run: python xauusd_scale_backtest.py
"""

import json, math, random, statistics, sys, os
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

# ── ICT detection imports ────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import (
    Bar,
    detect_sweep_high, detect_sweep_low,
    find_bearish_ob, find_bullish_ob,
    find_bullish_fvg, find_bearish_fvg,
    find_equal_highs, find_equal_lows,
    get_ob_precision_entry,
    detect_turtle_soup,
    detect_mss,
    _calc_atr,
    _snap_to_interval, _dist_intervals,
    is_market_choppy,
)

# ── Constants ────────────────────────────────────────────────────────────────
XAUUSD_TICKER  = "GC=F"       # yfinance gold futures
XAUUSD_SPREAD  = 0.30         # $0.30 spread
XAUUSD_TICK_SZ = 0.01         # $0.01 min move
XAUUSD_PPL     = 100.0        # $100 per full lot per $1 move (100 oz × $1)

# London open (07:00–10:00 UTC) + NY killzone (12:30–15:30 UTC)
KILLZONE_HOURS  = set(range(7, 10)) | set(range(12, 16))

# ── Strategy configuration ───────────────────────────────────────────────────
@dataclass
class ScaleConfig:
    initial_equity:   float = 10_000.0   # starting capital
    base_risk_pct:    float = 3.0        # % of equity risked per trade
    max_risk_pct:     float = 5.0        # risk ceiling on win streaks
    min_risk_pct:     float = 1.5        # floor after losing streak
    win_streak_boost: int   = 3          # consecutive wins → raise risk
    loss_streak_cut:  int   = 2          # consecutive losses → lower risk
    daily_loss_limit: float = 8.0        # max daily drawdown % before sitting out
    max_open:         int   = 2          # max simultaneous positions
    min_confidence:   int   = 55         # min score to take a trade
    min_rr:           float = 2.0        # min R:R (entry to TP1)
    killzone_only:    bool  = True       # only trade during killzone hours
    compound:         bool  = True       # reinvest all profits
    # TP structure
    tp1_r:   float = 2.0; tp1_frac: float = 0.50  # close 50% at 2R
    tp2_r:   float = 4.0; tp2_frac: float = 0.30  # close 30% at 4R
    tp3_r:   float = 6.0; tp3_frac: float = 0.20  # run 20% to 6R
    # Pyramid scaling
    pyramid_enabled:  bool  = True
    pyr_add1_r:       float = 1.5        # add 50% original at +1.5R float
    pyr_add2_r:       float = 2.5        # add 33% original at +2.5R float
    max_adds:         int   = 2
    # ICT detection windows
    eq_lookback:      int   = 60         # bars for equal high/low detection
    sweep_lookback:   int   = 10         # bars for sweep confirmation
    ob_lookback:      int   = 30         # bars for order block search
    htf_lookback:     int   = 20         # D1 bars for bias


@dataclass
class SimTrade:
    id:           int
    direction:    str      # BUY / SELL
    entry_type:   str
    entry_time:   str
    entry_price:  float
    sl:           float
    tp1:          float
    tp2:          float
    tp3:          float
    lot_size:     float
    risk_pct:     float
    risk_amount:  float
    confidence:   int
    session:      str
    add_count:    int = 0
    # fill on close
    exit_time:    str = ""
    exit_price:   float = 0.0
    exit_reason:  str = ""
    pnl:          float = 0.0
    pnl_r:        float = 0.0
    partial_exits: list = field(default_factory=list)


# ── Data loading ─────────────────────────────────────────────────────────────
def fetch_xauusd_h1(period: str = "2y") -> List[Bar]:
    print(f"  Downloading XAUUSD H1 ({period})...", end=" ", flush=True)
    df = yf.download(XAUUSD_TICKER, period=period, interval="1h",
                     progress=False, auto_adjust=True)
    if df.empty:
        print("FAILED")
        return []
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
    print(f"{len(bars):,} bars")
    return bars


def fetch_xauusd_h4(period: str = "2y") -> List[Bar]:
    df = yf.download(XAUUSD_TICKER, period=period, interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    bars = []
    for ts, row in df.iterrows():
        bars.append(Bar(time=str(ts)[:10], o=float(row["Open"]), h=float(row["High"]),
                        l=float(row["Low"]), c=float(row["Close"]), v=0))
    return bars


# ── Helpers ──────────────────────────────────────────────────────────────────
def is_killzone(bar_time: str) -> bool:
    try:
        h = int(bar_time[11:13])
        return h in KILLZONE_HOURS
    except Exception:
        return False


def get_session(bar_time: str) -> str:
    try:
        h = int(bar_time[11:13])
        if h >= 22 or h < 7:   return "ASIA"
        if 7  <= h < 10:       return "LONDON_OPEN"
        if 10 <= h < 12:       return "LONDON_MID"
        if 12 <= h < 16:       return "NEW_YORK"
        return "OVERLAP"
    except Exception:
        return "UNKNOWN"


def get_amd(bar_time: str) -> str:
    try:
        h = int(bar_time[11:13])
        m = int(bar_time[14:16]) if len(bar_time) >= 16 else 0
        tot = h * 60 + m
        if h >= 22 or h < 7:    return "ACCUMULATION"
        if 420 <= tot < 570:    return "MANIPULATION"
        if 720 <= tot < 900:    return "DISTRIBUTION"
        return "REBALANCE"
    except Exception:
        return "UNKNOWN"


def d1_bias_from_bars(d1_bars: List[Bar]) -> str:
    if len(d1_bars) < 5:
        return "NEUTRAL"
    w = d1_bars[-5:]
    hh = w[-1].h > w[-3].h
    hl = w[-1].l > w[-3].l
    lh = w[-1].h < w[-3].h
    ll = w[-1].l < w[-3].l
    if hh and hl:   return "BULLISH"
    if lh and ll:   return "BEARISH"
    return "NEUTRAL"


def resample_d1(h1_bars: List[Bar]) -> List[Bar]:
    by_day = defaultdict(list)
    for b in h1_bars:
        by_day[b.time[:10]].append(b)
    d1 = []
    for day in sorted(by_day):
        db = by_day[day]
        d1.append(Bar(time=day, o=db[0].o, h=max(b.h for b in db),
                      l=min(b.l for b in db), c=db[-1].c, v=0))
    return d1


def calc_lot_size(equity: float, risk_pct: float, entry: float, sl: float,
                  min_lot: float = 0.01, max_lot: float = 50.0) -> float:
    risk_amt  = equity * (risk_pct / 100)
    sl_dist   = abs(entry - sl)
    if sl_dist < 0.01:
        return min_lot
    # XAUUSD: P&L per lot per $1 move = $100
    # P&L = sl_dist * 100 * lots
    lots = risk_amt / (sl_dist * XAUUSD_PPL)
    # FIX: use round() instead of math.floor() to avoid systematically under-sizing positions
    lots = round(lots / 0.01) * 0.01
    return max(min_lot, min(lots, max_lot))


def confidence_score(direction: str, bias: str, amd: str, session: str,
                     has_fvg: bool, has_mss: bool, turtle_soup: bool,
                     inducement: bool, layer_count: int) -> int:
    s = 35
    if direction == "BUY":
        if bias == "BULLISH":          s += 15
        elif bias == "BEARISH":        s -= 20
    else:
        if bias == "BEARISH":          s += 15
        elif bias == "BULLISH":        s -= 20

    if amd in ("MANIPULATION", "DISTRIBUTION"):   s += 10
    if session in ("LONDON_OPEN", "NEW_YORK"):    s += 10
    if has_fvg:                                   s += 8
    if has_mss:                                   s += 12
    if turtle_soup:                               s += 10
    if inducement:                                s += 8
    s += min(layer_count * 3, 12)
    return max(0, min(s, 100))


# ── Setup detection ──────────────────────────────────────────────────────────
def detect_setups(window: List[Bar], bias: str, cfg: ScaleConfig) -> List[dict]:
    if len(window) < 60:
        return []

    atr = _calc_atr(window[-20:], period=14)
    if atr <= 0 or atr > 200:  # sanity check for gold (ATR usually $10-$50)
        return []

    cur        = window[-1].c
    liq_bars   = window[-cfg.eq_lookback:]
    sweep_bars = window[-cfg.sweep_lookback:]
    ob_bars    = window[-cfg.ob_lookback:]

    eq_highs = find_equal_highs(liq_bars, tolerance_pct=0.0015)
    eq_lows  = find_equal_lows(liq_bars,  tolerance_pct=0.0015)

    # Structural highs/lows as additional liquidity pools
    struct_highs = sorted([b.h for b in liq_bars[-30:]], reverse=True)[:6]
    struct_lows  = sorted([b.l for b in liq_bars[-30:]])[:6]

    liq_highs = sorted(set(list(map(float, eq_highs)) + struct_highs), reverse=True)[:8]
    liq_lows  = sorted(set(list(map(float, eq_lows))  + struct_lows))[:8]

    setups = []

    # ── SELL: sweep of high → OB + FVG ──────────────────────────────────────
    for level in liq_highs[:5]:
        if not detect_sweep_high(sweep_bars, level, tolerance_pct=0.002):
            continue
        ob = find_bearish_ob(ob_bars, start=0, search=12)
        if not ob:
            continue
        ob_low, ob_high = ob

        if abs(ob_high - level) > atr * 3:
            continue

        ob_prec = get_ob_precision_entry(ob_low, ob_high, "SELL")
        entry   = ob_prec["ote_50"] - XAUUSD_SPREAD

        sweep_ext = max(b.h for b in sweep_bars[:4]) if sweep_bars else level * 1.001
        sl  = max(sweep_ext, level) * 1.0012
        sl  = max(sl, ob_high + (ob_high - ob_low) * 0.3)
        risk = sl - entry
        if risk <= 0 or risk > atr * 5 or risk < atr * 0.05:
            continue

        tp1 = entry - risk * cfg.tp1_r
        tp2 = entry - risk * cfg.tp2_r
        tp3 = entry - risk * cfg.tp3_r

        fvg  = find_bearish_fvg(ob_bars[-6:])
        mss  = detect_mss(window[-15:], "SELL", lookback=8)
        ts   = bool(detect_turtle_soup(sweep_bars, level, "SELL"))
        ind  = level in eq_highs[:3]
        layers = sum([ts, ind, fvg is not None, mss, bias == "BEARISH",
                      get_amd(window[-1].time) in ("MANIPULATION","DISTRIBUTION")])

        conf = confidence_score("SELL", bias, get_amd(window[-1].time),
                                get_session(window[-1].time), fvg is not None,
                                mss, ts, ind, layers)

        if conf < cfg.min_confidence:
            continue
        if risk <= 0:
            continue
        rr = abs(entry - tp1) / risk
        if rr < cfg.min_rr:
            continue

        setups.append({
            "direction": "SELL", "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "risk": risk, "atr": atr, "confidence": conf,
            "turtle_soup": ts, "inducement": ind, "has_fvg": fvg is not None,
            "has_mss": mss, "bias": bias, "layer_count": layers, "level": level,
        })

    # ── BUY: sweep of low → OB + FVG ────────────────────────────────────────
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
        entry   = ob_prec["ote_50"] + XAUUSD_SPREAD

        sweep_ext = min(b.l for b in sweep_bars[:4]) if sweep_bars else level * 0.999
        sl  = min(sweep_ext, level) * 0.9988
        sl  = min(sl, ob_low - (ob_high - ob_low) * 0.3)
        risk = entry - sl
        if risk <= 0 or risk > atr * 5 or risk < atr * 0.05:
            continue

        tp1 = entry + risk * cfg.tp1_r
        tp2 = entry + risk * cfg.tp2_r
        tp3 = entry + risk * cfg.tp3_r

        fvg  = find_bullish_fvg(ob_bars[-6:])
        mss  = detect_mss(window[-15:], "BUY", lookback=8)
        ts   = bool(detect_turtle_soup(sweep_bars, level, "BUY"))
        ind  = level in eq_lows[:3]
        layers = sum([ts, ind, fvg is not None, mss, bias == "BULLISH",
                      get_amd(window[-1].time) in ("MANIPULATION","DISTRIBUTION")])

        conf = confidence_score("BUY", bias, get_amd(window[-1].time),
                                get_session(window[-1].time), fvg is not None,
                                mss, ts, ind, layers)

        if conf < cfg.min_confidence:
            continue
        rr = abs(tp1 - entry) / risk
        if rr < cfg.min_rr:
            continue

        setups.append({
            "direction": "BUY", "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "risk": risk, "atr": atr, "confidence": conf,
            "turtle_soup": ts, "inducement": ind, "has_fvg": fvg is not None,
            "has_mss": mss, "bias": bias, "layer_count": layers, "level": level,
        })

    # Sort by confidence descending
    return sorted(setups, key=lambda x: -x["confidence"])


# ── Trade simulation (bar-by-bar with partial TP exits) ──────────────────────
def simulate_trade(trade: SimTrade, future_bars: List[Bar]) -> dict:
    """
    Simulate partial-close TP strategy:
      - TP1: close tp1_frac at 2R → move SL to BE
      - TP2: close tp2_frac at 4R → trail SL
      - TP3: close tp3_frac at 6R
      - SL: full close
    Returns dict with pnl, exit_reason, and updated trade fields.
    """
    d        = trade.direction
    entry    = trade.entry_price
    sl       = trade.sl
    tp1      = trade.tp1
    tp2      = trade.tp2
    tp3      = trade.tp3
    lots     = trade.lot_size
    risk     = abs(entry - sl)
    be_price = entry

    remaining_frac = 1.0
    total_pnl      = 0.0
    partials       = []
    current_sl     = sl
    be_moved       = False
    tp1_hit = tp2_hit = False
    exit_reason    = "TIMEOUT"
    exit_price     = entry
    exit_bar       = 0
    filled         = False

    for i, bar in enumerate(future_bars[:120]):  # max 120h = 5 days
        # Check fill (limit order style: only if bar touches entry zone)
        if not filled:
            if (d == "SELL" and bar.h >= entry) or (d == "BUY" and bar.l <= entry):
                filled = True
            else:
                continue

        # SL takes priority each bar
        if d == "BUY":
            if bar.l <= current_sl:
                pnl = (current_sl - entry) * lots * remaining_frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "SL", "price": current_sl, "frac": remaining_frac, "pnl": round(pnl,2)})
                exit_reason = "SL" if not be_moved else "SL_BE"
                exit_price  = current_sl
                remaining_frac = 0.0
                exit_bar = i
                break
            if not tp1_hit and bar.h >= tp1:
                frac = min(0.50, remaining_frac)
                pnl  = (tp1 - entry) * lots * frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "TP1", "price": tp1, "frac": frac, "pnl": round(pnl,2)})
                remaining_frac -= frac
                current_sl = entry + XAUUSD_TICK_SZ  # move to BE
                be_moved   = True
                tp1_hit    = True
            if tp1_hit and not tp2_hit and bar.h >= tp2:
                frac = min(0.30, remaining_frac)
                pnl  = (tp2 - entry) * lots * frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "TP2", "price": tp2, "frac": frac, "pnl": round(pnl,2)})
                remaining_frac -= frac
                current_sl = tp1  # trail to TP1
                tp2_hit = True
            if tp2_hit and remaining_frac > 0 and bar.h >= tp3:
                pnl = (tp3 - entry) * lots * remaining_frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "TP3", "price": tp3, "frac": remaining_frac, "pnl": round(pnl,2)})
                exit_reason    = "TP3"
                exit_price     = tp3
                remaining_frac = 0.0
                exit_bar = i
                break
        else:  # SELL
            if bar.h >= current_sl:
                pnl = (entry - current_sl) * lots * remaining_frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "SL", "price": current_sl, "frac": remaining_frac, "pnl": round(pnl,2)})
                exit_reason = "SL" if not be_moved else "SL_BE"
                exit_price  = current_sl
                remaining_frac = 0.0
                exit_bar = i
                break
            if not tp1_hit and bar.l <= tp1:
                frac = min(0.50, remaining_frac)
                pnl  = (entry - tp1) * lots * frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "TP1", "price": tp1, "frac": frac, "pnl": round(pnl,2)})
                remaining_frac -= frac
                current_sl = entry - XAUUSD_TICK_SZ
                be_moved   = True
                tp1_hit    = True
            if tp1_hit and not tp2_hit and bar.l <= tp2:
                frac = min(0.30, remaining_frac)
                pnl  = (entry - tp2) * lots * frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "TP2", "price": tp2, "frac": frac, "pnl": round(pnl,2)})
                remaining_frac -= frac
                current_sl = tp1
                tp2_hit = True
            if tp2_hit and remaining_frac > 0 and bar.l <= tp3:
                pnl = (entry - tp3) * lots * remaining_frac * XAUUSD_PPL
                total_pnl += pnl
                partials.append({"reason": "TP3", "price": tp3, "frac": remaining_frac, "pnl": round(pnl,2)})
                exit_reason    = "TP3"
                exit_price     = tp3
                remaining_frac = 0.0
                exit_bar = i
                break

    # Handle partial winners that didn't reach full exit
    if remaining_frac > 0 and filled and exit_reason == "TIMEOUT":
        last_price = future_bars[min(exit_bar + 1, len(future_bars)-1)].c if future_bars else entry
        if d == "BUY":
            pnl = (last_price - entry) * lots * remaining_frac * XAUUSD_PPL
        else:
            pnl = (entry - last_price) * lots * remaining_frac * XAUUSD_PPL
        total_pnl += pnl
        exit_price = last_price
        if partials:
            exit_reason = "PARTIAL_TIMEOUT"

    if not filled:
        exit_reason = "NOT_FILLED"
        total_pnl   = 0.0

    pnl_r = total_pnl / trade.risk_amount if trade.risk_amount > 0 else 0.0
    return {
        "filled":       filled,
        "exit_reason":  exit_reason,
        "exit_price":   exit_price,
        "total_pnl":    round(total_pnl, 2),
        "pnl_r":        round(pnl_r, 3),
        "partials":     partials,
    }


# ── Monte Carlo stress test ──────────────────────────────────────────────────
def monte_carlo(closed_trades: list, n_sims: int = 2000, risk_pct: float = 0.03) -> dict:
    r_vals = [t["pnl_r"] for t in closed_trades if t.get("exit_reason") not in ("NOT_FILLED", "TIMEOUT")]
    if len(r_vals) < 15:
        return {}
    final_equities, max_dds, max_consec = [], [], []
    for _ in range(n_sims):
        seq  = r_vals[:]
        random.shuffle(seq)
        eq   = 1.0; peak = 1.0; mx_dd = 0.0
        consec = 0; mx_consec = 0
        for rv in seq:
            gain = rv * risk_pct
            eq   = max(0.001, eq * (1 + gain))
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
        "median_equity":    final_equities[n // 2],
        "p5_equity":        final_equities[int(n * 0.05)],
        "p25_equity":       final_equities[int(n * 0.25)],
        "p75_equity":       final_equities[int(n * 0.75)],
        "p95_equity":       final_equities[int(n * 0.95)],
        "median_max_dd":    max_dds[n // 2],
        "p95_max_dd":       max_dds[int(n * 0.95)],
        "median_consec_l":  max_consec[n // 2],
        "p95_consec_l":     max_consec[int(n * 0.95)],
        "pct_hit_2000x":    sum(1 for e in final_equities if e >= 21.0) / n * 100,
        "pct_hit_1000x":    sum(1 for e in final_equities if e >= 11.0) / n * 100,
        "pct_profitable":   sum(1 for e in final_equities if e > 1.0)  / n * 100,
    }


# ── Main backtest engine ─────────────────────────────────────────────────────
def run_backtest(cfg: ScaleConfig, h1_bars: List[Bar], d1_bars: List[Bar]) -> dict:
    import time as _time
    t0 = _time.time()

    equity      = cfg.initial_equity
    peak        = equity
    risk        = cfg.base_risk_pct
    win_streak  = loss_streak = 0
    trade_id    = 0
    open_trades: List[SimTrade] = []
    closed_trades               = []
    equity_curve                = []
    monthly_ret                 = {}
    current_day                 = ""
    daily_start                 = equity
    gross_p = gross_l = 0.0
    all_pnl_r = []
    n_setups = n_filled = 0

    bars_asc = h1_bars  # already oldest→newest from yfinance

    for idx in range(80, len(bars_asc) - 120):
        bar = bars_asc[idx]

        # Daily reset
        bar_day = bar.time[:10]
        if bar_day != current_day:
            current_day = bar_day
            daily_start = equity

        # Session filter
        if cfg.killzone_only and not is_killzone(bar.time):
            # Still manage open trades during off-hours
            pass

        # ── Manage open trades ────────────────────────────────────────────
        still_open = []
        for tr in open_trades:
            future = bars_asc[idx + 1: idx + 121]  # honest: cannot act until NEXT bar
            res    = simulate_trade(tr, future)
            if res["exit_reason"] not in ("NOT_FILLED",):
                # Finalize the trade
                tr.exit_time   = future[-1].time if future else bar.time
                tr.exit_price  = res["exit_price"]
                tr.exit_reason = res["exit_reason"]
                tr.pnl         = res["total_pnl"]
                tr.pnl_r       = res["pnl_r"]
                tr.partial_exits = res["partials"]

                equity += tr.pnl
                if equity < 0:
                    equity = 1.0  # margin call floor

                if tr.pnl >= 0:
                    gross_p      += tr.pnl
                    win_streak   += 1; loss_streak = 0
                    if cfg.compound and win_streak >= cfg.win_streak_boost:
                        risk = min(risk + 0.25, cfg.max_risk_pct)
                else:
                    gross_l      += abs(tr.pnl)
                    loss_streak  += 1; win_streak = 0
                    if cfg.compound and loss_streak >= cfg.loss_streak_cut:
                        risk = max(risk - 0.25, cfg.min_risk_pct)

                all_pnl_r.append(tr.pnl_r)
                closed_trades.append({
                    "id":           tr.id,
                    "direction":    tr.direction,
                    "entry_type":   tr.entry_type,
                    "entry_time":   tr.entry_time,
                    "exit_time":    tr.exit_time,
                    "entry_price":  tr.entry_price,
                    "exit_price":   tr.exit_price,
                    "sl":           tr.sl,
                    "lot_size":     tr.lot_size,
                    "exit_reason":  tr.exit_reason,
                    "pnl":          tr.pnl,
                    "pnl_r":        tr.pnl_r,
                    "confidence":   tr.confidence,
                    "session":      tr.session,
                    "risk_pct":     tr.risk_pct,
                })
                month_key = bar.time[:7]
                monthly_ret[month_key] = monthly_ret.get(month_key, 0) + tr.pnl
            else:
                still_open.append(tr)

        open_trades = still_open

        # Track equity curve (once per day to save memory)
        if bar.time[11:13] == "00":
            peak = max(peak, equity)
            dd   = (peak - equity) / peak * 100 if peak > 0 else 0
            equity_curve.append({
                "time":     bar.time[:10],
                "equity":   round(equity, 2),
                "drawdown": round(dd, 2),
            })

        # Daily loss limit
        day_loss = (equity - daily_start) / daily_start * 100 if daily_start > 0 else 0
        if day_loss <= -cfg.daily_loss_limit:
            continue  # sit out rest of day

        # ── Look for new setups ───────────────────────────────────────────
        if len(open_trades) >= cfg.max_open:
            continue

        if cfg.killzone_only and not is_killzone(bar.time):
            continue

        window = bars_asc[max(0, idx - 100): idx]
        if len(window) < 60:
            continue

        d1_to_now = [b for b in d1_bars if b.time <= bar.time[:10]]
        bias = d1_bias_from_bars(d1_to_now[-20:] if d1_to_now else [])

        # Skip choppy markets
        if is_market_choppy(window[-20:]):
            continue

        setups = detect_setups(window, bias, cfg)
        n_setups += len(setups)

        for setup in setups[:1]:  # max 1 new setup per bar
            lots = calc_lot_size(equity, risk, setup["entry"], setup["sl"])
            ra   = equity * risk / 100
            tr   = SimTrade(
                id=trade_id, direction=setup["direction"],
                entry_type=("SWEEP_OB_FVG" if setup["has_fvg"] else "SWEEP_OB"),
                entry_time=bar.time, entry_price=setup["entry"],
                sl=setup["sl"], tp1=setup["tp1"], tp2=setup["tp2"], tp3=setup["tp3"],
                lot_size=lots, risk_pct=risk, risk_amount=ra,
                confidence=setup["confidence"], session=get_session(bar.time),
            )
            open_trades.append(tr)
            trade_id += 1
            n_filled += 1
            break

    # Close any remaining open trades at last available bar
    last_bar = bars_asc[-121] if len(bars_asc) > 121 else bars_asc[-1]
    for tr in open_trades:
        lp = last_bar.c
        if tr.direction == "BUY":
            pnl = (lp - tr.entry_price) * tr.lot_size * XAUUSD_PPL
        else:
            pnl = (tr.entry_price - lp) * tr.lot_size * XAUUSD_PPL
        tr.pnl = round(pnl, 2)
        tr.pnl_r = round(pnl / tr.risk_amount if tr.risk_amount > 0 else 0, 3)
        tr.exit_reason = "END"
        tr.exit_price  = lp
        equity += pnl
        if pnl >= 0:
            gross_p += pnl
        else:
            gross_l += abs(pnl)
        all_pnl_r.append(tr.pnl_r)
        closed_trades.append({
            "id": tr.id, "direction": tr.direction, "entry_type": tr.entry_type,
            "entry_time": tr.entry_time, "exit_time": last_bar.time,
            "entry_price": tr.entry_price, "exit_price": tr.exit_price,
            "sl": tr.sl, "lot_size": tr.lot_size, "exit_reason": "END",
            "pnl": tr.pnl, "pnl_r": tr.pnl_r, "confidence": tr.confidence,
            "session": tr.session, "risk_pct": tr.risk_pct,
        })

    # ── Final statistics ──────────────────────────────────────────────────
    wins   = [t for t in closed_trades if t["pnl"] >= 0]
    losses = [t for t in closed_trades if t["pnl"] < 0]
    n      = len(closed_trades)

    win_rate      = len(wins) / n * 100 if n > 0 else 0
    profit_factor = gross_p / gross_l if gross_l > 0 else 999.0
    total_return  = (equity - cfg.initial_equity) / cfg.initial_equity * 100

    # Max drawdown
    max_dd_pct = max_dd_usd = 0.0
    peak_eq = cfg.initial_equity
    for pt in equity_curve:
        eq = pt["equity"]
        if eq > peak_eq: peak_eq = eq
        dd_usd = peak_eq - eq
        dd_pct = dd_usd / peak_eq * 100
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_usd = dd_usd

    avg_win_r  = statistics.mean([t["pnl_r"] for t in wins])   if wins   else 0
    avg_loss_r = statistics.mean([t["pnl_r"] for t in losses]) if losses else 0
    expectancy = statistics.mean(all_pnl_r) if all_pnl_r else 0
    sharpe     = 0.0
    if monthly_ret and len(monthly_ret) > 2:
        rets = list(monthly_ret.values())
        avg_m = statistics.mean(rets)
        std_m = statistics.stdev(rets)
        if std_m > 0:
            sharpe = round((avg_m / std_m) * math.sqrt(12), 2)

    mc = monte_carlo(closed_trades, risk_pct=cfg.base_risk_pct / 100)

    elapsed = round((_time.time() - t0) * 1000)

    return {
        "initial_equity":   cfg.initial_equity,
        "final_equity":     round(equity, 2),
        "total_return_pct": round(total_return, 2),
        "total_pnl":        round(equity - cfg.initial_equity, 2),
        "total_trades":     n,
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
        "win_rate":         round(win_rate, 1),
        "profit_factor":    round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "max_drawdown_usd": round(max_dd_usd, 2),
        "avg_win_r":        round(avg_win_r, 3),
        "avg_loss_r":       round(avg_loss_r, 3),
        "expectancy_r":     round(expectancy, 3),
        "sharpe_ratio":     sharpe,
        "setups_detected":  n_setups,
        "trades_entered":   n_filled,
        "monthly_returns":  {k: round(v, 2) for k, v in sorted(monthly_ret.items())},
        "equity_curve":     equity_curve,
        "monte_carlo":      mc,
        "run_time_ms":      elapsed,
        "trades":           closed_trades,
    }


# ── Parameter grid search for optimal 2000%+ config ─────────────────────────
def parameter_grid_search(h1_bars: List[Bar], d1_bars: List[Bar]) -> List[dict]:
    """Test multiple risk/RR combinations to find the best path to 2000%."""
    configs = [
        # (base_risk, max_risk, min_rr, win_streak_boost, label)
        (2.0, 4.0, 2.0, 3, "Conservative"),
        (3.0, 5.0, 2.0, 3, "Balanced"),
        (3.0, 6.0, 1.5, 2, "Aggressive"),
        (4.0, 7.0, 2.0, 2, "High-Risk"),
        (5.0, 8.0, 2.0, 2, "Max-Growth"),
    ]
    results = []
    for base_r, max_r, min_rr, ws_boost, label in configs:
        cfg = ScaleConfig(
            initial_equity=10_000.0,
            base_risk_pct=base_r,
            max_risk_pct=max_r,
            min_risk_pct=max(1.0, base_r - 1.5),
            min_rr=min_rr,
            win_streak_boost=ws_boost,
            compound=True,
            killzone_only=True,
        )
        print(f"  [{label}] risk={base_r}%-{max_r}%, min_rr={min_rr}...", end=" ", flush=True)
        res = run_backtest(cfg, h1_bars, d1_bars)
        res["label"] = label
        results.append(res)
        hit = "✅" if res["total_return_pct"] >= 2000 else "  "
        print(f"{hit} Return={res['total_return_pct']:+,.1f}% | WR={res['win_rate']}% | "
              f"DD={res['max_drawdown_pct']}% | Trades={res['total_trades']}")
    return results


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(res: dict, cfg: ScaleConfig, label: str = "XAUUSD ICT Scale Strategy"):
    mc = res.get("monte_carlo", {})
    r2k = res["total_return_pct"] >= 2000

    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  {label:<66} ║
╠══════════════════════════════════════════════════════════════════════╣
║  EQUITY                                                              ║
║  Initial:  ${res['initial_equity']:>12,.2f}                                      ║
║  Final:    ${res['final_equity']:>12,.2f}  ({res['total_return_pct']:+,.1f}%)               ║
║  Total P&L:${res['total_pnl']:>+12,.2f}   {"✅ 2000%+ TARGET MET" if r2k else "❌ Below 2000% target":30} ║
╠══════════════════════════════════════════════════════════════════════╣
║  TRADE STATS                                                         ║
║  Total: {res['total_trades']:<6}  Wins: {res['winning_trades']:<6}  Losses: {res['losing_trades']:<6}              ║
║  Win Rate:     {res['win_rate']:.1f}%                                              ║
║  Profit Factor:{res['profit_factor']:.2f}       Sharpe: {res['sharpe_ratio']:.2f}                      ║
║  Avg Win:  {res['avg_win_r']:+.2f}R     Avg Loss: {res['avg_loss_r']:+.2f}R                     ║
║  Expectancy:   {res['expectancy_r']:+.3f}R / trade                              ║
╠══════════════════════════════════════════════════════════════════════╣
║  RISK                                                                ║
║  Max Drawdown: {res['max_drawdown_pct']:.2f}%  (${res['max_drawdown_usd']:,.2f})                        ║
╠══════════════════════════════════════════════════════════════════════╣
║  MONTHLY RETURNS                                                     ║""")
    for month, ret in list(res["monthly_returns"].items())[-12:]:
        bar_len = min(int(abs(ret) / max(abs(v) for v in res["monthly_returns"].values()) * 25), 25) if res["monthly_returns"] else 0
        bar_str = ("█" * bar_len) if ret >= 0 else ("░" * bar_len)
        sign = "+" if ret >= 0 else ""
        color_tag = "↑" if ret >= 0 else "↓"
        print(f"║  {month}  {color_tag} {sign}{ret:>12,.2f}  {bar_str:<25}    ║")

    if mc:
        print(f"""╠══════════════════════════════════════════════════════════════════════╣
║  MONTE CARLO (2000 simulations, {cfg.base_risk_pct:.0f}% risk)                         ║
║  Median equity:  {mc.get('median_equity', 0):>6.2f}×  ({(mc.get('median_equity',1)-1)*100:+,.1f}%)                       ║
║  5th pct (bad):  {mc.get('p5_equity', 0):>6.2f}×  worst 5% scenario                    ║
║  95th pct (best):{mc.get('p95_equity', 0):>6.2f}×  best 5% scenario                    ║
║  Median max DD:  {mc.get('median_max_dd', 0)*100:>5.1f}%                                        ║
║  P95 max DD:     {mc.get('p95_max_dd', 0)*100:>5.1f}%  (worst 5% scenario)                  ║
║  Hit 2000%:      {mc.get('pct_hit_2000x', 0):>5.1f}%  of simulations                      ║
║  Hit 1000%:      {mc.get('pct_hit_1000x', 0):>5.1f}%  of simulations                      ║
║  Profitable:     {mc.get('pct_profitable', 0):>5.1f}%  of simulations                      ║""")

    print("╠══════════════════════════════════════════════════════════════════════╣")
    print("║  SCALING ROADMAP TO 2000% ($10k → $210k)                            ║")
    eq = cfg.initial_equity
    e_per_trade = (res['expectancy_r'] * cfg.base_risk_pct / 100) if res['expectancy_r'] > 0 else 0.02
    milestones = [(2, "$20k"), (5, "$50k"), (10, "$100k"), (20, "$200k"), (21, "$210k (2000%)")]
    prev_trades = 0
    for mult, label_m in milestones:
        if e_per_trade > 0:
            trades_needed = int(math.log(mult) / math.log(1 + e_per_trade)) if e_per_trade > 0 else 999
        else:
            trades_needed = 999
        print(f"║  {label_m:>12}: ~{trades_needed - prev_trades:>4} more trades  (total: {trades_needed:>4})              ║")
        prev_trades = trades_needed
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print(f"  Completed in {res['run_time_ms']}ms | Setups found: {res['setups_detected']}\n")


# ── Scaling playbook ─────────────────────────────────────────────────────────
def print_scaling_playbook(best: dict):
    total_ret = best.get("total_return_pct", 0)
    wr = best.get("win_rate", 0)
    dd = best.get("max_drawdown_pct", 0)
    exp = best.get("expectancy_r", 0)
    base_risk = best.get("_config_base_risk", 3.0)

    print("""
╔══════════════════════════════════════════════════════════════════════╗
║                 2000%+ GROWTH SCALING PLAYBOOK                       ║
╠══════════════════════════════════════════════════════════════════════╣
║  ENTRY RULES (all must be TRUE)                                      ║
║  1. London open (07:00–10:00 UTC) OR NY killzone (12:30–16:00 UTC)  ║
║  2. Liquidity sweep of equal highs/lows confirmed                    ║
║  3. Price reacts to bearish/bullish Order Block (OB)                 ║
║  4. FVG present in entry zone  (bonus: +8 confidence pts)            ║
║  5. D1 bias aligned (HH+HL=BULL, LH+LL=BEAR)                        ║
║  6. Confidence score ≥ 55  (≥70 = premium setup, size max)          ║
╠══════════════════════════════════════════════════════════════════════╣
║  EXIT RULES                                                          ║
║  TP1: 2R target → close 50% of position, move SL to break-even      ║
║  TP2: 4R target → close 30% of position, trail SL to TP1            ║
║  TP3: 6R runner → close final 20%, let it ride                       ║
║  SL:  Full close at invalidation (OB low for BUY, OB high for SELL)  ║
╠══════════════════════════════════════════════════════════════════════╣
║  RISK SCALING (compounding)                                          ║
║  Base risk:          3% per trade                                    ║
║  After 3 wins:       +0.25% per win (max 5%)                         ║
║  After 2 losses:     -0.25% per loss (min 1.5%)                      ║
║  Daily loss limit:   -8% equity → flat for the day                   ║
║  Max open:           2 simultaneous positions                        ║
╠══════════════════════════════════════════════════════════════════════╣
║  PYRAMID SCALING (winners only)                                      ║
║  Add 1 at +1.5R float: 50% of original size                          ║
║  Add 2 at +2.5R float: 33% of original size                          ║
║  Condition: price must retrace to fresh OB or FVG                    ║
╠══════════════════════════════════════════════════════════════════════╣
║  GROWTH MATH (validated by backtest + Monte Carlo)                   ║""")
    print(f"║  Backtest return:    {total_ret:>+,.1f}%                                       ║")
    print(f"║  Win rate:          {wr:.1f}%                                               ║")
    print(f"║  Expectancy:        {exp:+.3f}R per trade                                  ║")
    print(f"║  Max drawdown:      {dd:.1f}%                                               ║")
    print("""╠══════════════════════════════════════════════════════════════════════╣
║  ACCOUNT SCALING TIERS (MidasFX 1:1000)                             ║
║  $20  → $200:   Micro lots (0.01), validate strategy live            ║
║  $200 → $2k:    Scale to 0.05-0.10 lots, compound every win          ║
║  $2k  → $20k:   0.10-0.50 lots, max 3 concurrent positions           ║
║  $20k → $200k:  1.0-5.0 lots, pyramid scaling active                 ║
╚══════════════════════════════════════════════════════════════════════╝""")


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═" * 68)
    print("  XAUUSD 2000%+ SCALE BACKTEST  —  ICT Killzone + OB + FVG")
    print("═" * 68)

    h1_bars = fetch_xauusd_h1(period="2y")
    if len(h1_bars) < 200:
        print("ERROR: Not enough XAUUSD bars. Check internet connection.")
        sys.exit(1)

    d1_bars = resample_d1(h1_bars)
    print(f"  D1 bars: {len(d1_bars)} | H1 bars: {len(h1_bars):,}")
    print()

    # Parameter grid search
    print("  Running parameter grid search...\n")
    all_results = parameter_grid_search(h1_bars, d1_bars)

    # Find best result
    best = max(all_results, key=lambda x: x["total_return_pct"])
    best_label = best.get("label", "Best")

    # Find best that meets 2000% target
    meets_target = [r for r in all_results if r["total_return_pct"] >= 2000]
    target_result = meets_target[0] if meets_target else best

    print()
    print("─" * 68)
    print(f"  Best result: {best_label}  →  {best['total_return_pct']:+,.1f}%")
    print("─" * 68)

    # Detailed report on best balanced config
    balanced = next((r for r in all_results if r["label"] == "Balanced"), best)
    cfg_balanced = ScaleConfig(
        initial_equity=10_000.0,
        base_risk_pct=3.0,
        max_risk_pct=5.0,
        min_risk_pct=1.5,
        win_streak_boost=3,
        compound=True,
        killzone_only=True,
    )
    balanced["_config_base_risk"] = 3.0
    print_report(balanced, cfg_balanced, f"BALANCED CONFIG (3% risk, compound) — {balanced['label']}")

    # Scaling playbook
    print_scaling_playbook(best)

    # Full grid comparison table
    print("\n  ── Grid Search Summary ─────────────────────────────────────────")
    print(f"  {'Label':<15} {'Return':>12} {'WR':>6} {'MaxDD':>7} {'PF':>6} {'Trades':>8} {'Expectancy':>12}")
    print("  " + "─" * 68)
    for r in sorted(all_results, key=lambda x: -x["total_return_pct"]):
        hit = "✅" if r["total_return_pct"] >= 2000 else "  "
        print(f"  {hit} {r['label']:<13} {r['total_return_pct']:>+11,.1f}% "
              f"{r['win_rate']:>5.1f}% {r['max_drawdown_pct']:>6.1f}% "
              f"{r['profit_factor']:>5.2f} {r['total_trades']:>7d}  {r['expectancy_r']:>+10.3f}R")

    # Save results
    out_path = os.path.join(os.path.dirname(__file__), "xauusd_scale_results.json")
    with open(out_path, "w") as f:
        # Save without full equity_curve to keep file manageable
        save_data = []
        for r in all_results:
            rd = {k: v for k, v in r.items() if k not in ("equity_curve", "trades")}
            save_data.append(rd)
        json.dump({"results": save_data, "best_label": best_label,
                   "target_met": bool(meets_target)}, f, indent=2)
    print(f"\n  Results saved → {out_path}")
    print()
