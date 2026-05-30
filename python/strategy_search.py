"""
strategy_search.py — honest multi-strategy search over the 3yr gold history.

GOAL: find a configuration with a REAL edge, not a curve-fit. The guard against
self-deception is a strict train/test split:
  - IN-SAMPLE  (IS): first ~2/3 of the data — used to pick each family's best params.
  - OUT-OF-SAMPLE (OOS): last ~1/3 — the winner is judged ONLY here, on data it was
    never tuned on. A strategy that shines IS and dies OOS is overfit → rejected.

Benchmark: BUY & HOLD. Gold roughly doubled over 2023-2026, so any long-biased
trend system has a tailwind. We only credit a strategy if it beats buy-and-hold on
a RISK-ADJUSTED basis (Sharpe) OOS — otherwise it's just "long gold in a bull run".

Strategy families (each long/short/flat, vectorized, walk-forward safe via shift):
  MAX   EMA crossover (trend)
  DON   Donchian channel breakout (trend)
  MOM   time-series momentum (trend)
  RSI   RSI mean-reversion (counter-trend)
  BB    Bollinger mean-reversion (counter-trend)
  MA200 long-only above slow MA (pure bull-capture filter)
  ATR   ATR/Keltner channel breakout (trend)

Costs: spread+slippage charged on every position change (turnover), in price-fraction
terms. Default $0.40 round-trip on gold.

Usage:
  ../.venv-kronos/bin/python strategy_search.py --tf h1
  ../.venv-kronos/bin/python strategy_search.py --tf m15 --cost 0.40
"""
from __future__ import annotations

import argparse
import itertools
import os
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# bars per year by timeframe (24x5 market, ~252 trading days; gold trades ~24/5)
BARS_PER_YEAR = {"h1": 6000, "m15": 24000, "m5": 72000, "h4": 1500, "d1": 252}


def load(tf: str, symbol: str = "XAUUSD") -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"hist_{symbol}_{tf}.csv")
    df = pd.read_csv(path, parse_dates=["time"], date_format="%Y.%m.%d %H:%M:%S")
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close"]].astype(float)


# ──────────────────────────────────────────────────────────────────────────────
# Indicators (all causal)
# ──────────────────────────────────────────────────────────────────────────────

def ema(s, n):   return s.ewm(span=n, adjust=False).mean()
def sma(s, n):   return s.rolling(n).mean()

def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


# ──────────────────────────────────────────────────────────────────────────────
# Strategy signal generators -> position series in {-1,0,+1} (pre-shift)
# ──────────────────────────────────────────────────────────────────────────────

def sig_max(df, fast, slow):
    f, s = ema(df.close, fast), ema(df.close, slow)
    return np.sign(f - s)

def sig_don(df, n):
    hi = df.high.rolling(n).max().shift(1)
    lo = df.low.rolling(n).min().shift(1)
    pos = pd.Series(np.nan, index=df.index)
    pos[df.close > hi] = 1
    pos[df.close < lo] = -1
    return pos.ffill().fillna(0)

def sig_mom(df, n):
    return np.sign(df.close - df.close.shift(n))

def sig_rsi(df, n, lo, hi):
    r = rsi(df.close, n)
    pos = pd.Series(np.nan, index=df.index)
    pos[r < lo] = 1          # oversold -> long
    pos[r > hi] = -1         # overbought -> short
    pos[(r > 50 - 0) & (r < 50 + 0)] = 0
    return pos.ffill().fillna(0)

def sig_bb(df, n, k):
    m = sma(df.close, n); sd = df.close.rolling(n).std()
    upper, lower = m + k * sd, m - k * sd
    pos = pd.Series(np.nan, index=df.index)
    pos[df.close < lower] = 1
    pos[df.close > upper] = -1
    pos[(df.close >= m) & (pos.shift(1) == 1)] = 0   # exit longs at mean
    pos[(df.close <= m) & (pos.shift(1) == -1)] = 0
    return pos.ffill().fillna(0)

def sig_ma200(df, n):
    return (df.close > sma(df.close, n)).astype(float)   # long-only

def sig_atr(df, n, mult):
    m = ema(df.close, n); a = atr(df, n)
    upper, lower = m + mult * a, m - mult * a
    pos = pd.Series(np.nan, index=df.index)
    pos[df.close > upper] = 1
    pos[df.close < lower] = -1
    return pos.ffill().fillna(0)


FAMILIES = {
    "MAX":   (sig_max,   [dict(fast=f, slow=s) for f, s in itertools.product([10, 20, 50], [50, 100, 200]) if f < s]),
    "DON":   (sig_don,   [dict(n=n) for n in [20, 40, 55, 100]]),
    "MOM":   (sig_mom,   [dict(n=n) for n in [24, 48, 120, 240]]),
    "RSI":   (sig_rsi,   [dict(n=14, lo=lo, hi=hi) for lo, hi in [(30, 70), (25, 75), (20, 80), (35, 65)]]),
    "BB":    (sig_bb,    [dict(n=n, k=k) for n, k in itertools.product([20, 50], [2.0, 2.5])]),
    "MA200": (sig_ma200, [dict(n=n) for n in [100, 150, 200]]),
    "ATR":   (sig_atr,   [dict(n=n, mult=m) for n, m in itertools.product([20, 50], [1.5, 2.5])]),
}


# ──────────────────────────────────────────────────────────────────────────────
# Backtest a position series -> metrics
# ──────────────────────────────────────────────────────────────────────────────

def evaluate(df: pd.DataFrame, pos: pd.Series, cost_dollars: float, bpy: int) -> dict:
    pos = pos.shift(1).fillna(0)                      # trade on next bar (no lookahead)
    ret = df.close.pct_change().fillna(0)
    cost_frac = cost_dollars / df.close              # per full flip, in fraction terms
    turn = pos.diff().abs().fillna(pos.abs())
    strat = pos * ret - turn * cost_frac
    eq = (1 + strat).cumprod()
    n = len(strat)
    if n < 50 or eq.iloc[-1] <= 0:
        return dict(ret=-1.0, sharpe=-9, maxdd=-1, pf=0, trades=int(turn[turn > 0].count()), expo=float(pos.abs().mean()))
    total = eq.iloc[-1] - 1
    sharpe = (strat.mean() / strat.std() * np.sqrt(bpy)) if strat.std() > 0 else 0.0
    dd = (eq / eq.cummax() - 1).min()
    gains = strat[strat > 0].sum(); losses = -strat[strat < 0].sum()
    pf = gains / losses if losses > 0 else np.inf
    return dict(ret=float(total), sharpe=float(sharpe), maxdd=float(dd),
                pf=float(pf), trades=int((turn > 1e-9).sum()), expo=float(pos.abs().mean()))


def buy_hold(df, bpy):
    ret = df.close.pct_change().fillna(0)
    eq = (1 + ret).cumprod()
    sharpe = ret.mean() / ret.std() * np.sqrt(bpy) if ret.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    return dict(ret=float(eq.iloc[-1] - 1), sharpe=float(sharpe), maxdd=float(dd))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="h1", choices=["h4", "h1", "m15", "m5", "d1"])
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--cost", type=float, default=0.40, help="round-trip cost $ (spread+slippage)")
    ap.add_argument("--split", type=float, default=0.667, help="in-sample fraction")
    args = ap.parse_args()

    df = load(args.tf, args.symbol)
    bpy = BARS_PER_YEAR[args.tf]
    cut = int(len(df) * args.split)
    is_df, oos_df = df.iloc[:cut], df.iloc[cut:]
    print(f"{args.symbol} {args.tf}: {len(df):,} bars  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
    print(f"  IS  {is_df.index[0]:%Y-%m-%d}->{is_df.index[-1]:%Y-%m-%d} ({len(is_df):,})"
          f"   OOS {oos_df.index[0]:%Y-%m-%d}->{oos_df.index[-1]:%Y-%m-%d} ({len(oos_df):,})")
    bh_is, bh_oos = buy_hold(is_df, bpy), buy_hold(oos_df, bpy)
    print(f"  BUY&HOLD   IS ret {bh_is['ret']*100:+.0f}% Sharpe {bh_is['sharpe']:.2f}"
          f" | OOS ret {bh_oos['ret']*100:+.0f}% Sharpe {bh_oos['sharpe']:.2f} maxDD {bh_oos['maxdd']*100:.0f}%")

    # For each family: pick best params by IS Sharpe, then report OOS.
    rows = []
    for fam, (fn, grid) in FAMILIES.items():
        best = None
        for params in grid:
            pos_is = fn(is_df, **params)
            m_is = evaluate(is_df, pos_is, args.cost, bpy)
            if best is None or m_is["sharpe"] > best[1]["sharpe"]:
                best = (params, m_is)
        params, m_is = best
        pos_oos = fn(oos_df, **params)
        m_oos = evaluate(oos_df, pos_oos, args.cost, bpy)
        rows.append((fam, params, m_is, m_oos))

    rows.sort(key=lambda r: r[3]["sharpe"], reverse=True)
    print("\n" + "=" * 100)
    print(f"{'FAM':5} {'best IS params':28} {'IS_Shrp':>7} {'IS_ret':>7} | "
          f"{'OOS_Shrp':>8} {'OOS_ret':>7} {'OOS_PF':>6} {'OOS_DD':>7} {'OOS_tr':>6} {'expo':>5}")
    print("-" * 100)
    for fam, params, m_is, m_oos in rows:
        ps = ",".join(f"{k}={v}" for k, v in params.items())
        print(f"{fam:5} {ps:28} {m_is['sharpe']:7.2f} {m_is['ret']*100:6.0f}% | "
              f"{m_oos['sharpe']:8.2f} {m_oos['ret']*100:6.0f}% {m_oos['pf']:6.2f} "
              f"{m_oos['maxdd']*100:6.0f}% {m_oos['trades']:6d} {m_oos['expo']:5.2f}")
    print("=" * 100)
    print("VERDICT: a strategy is credible only if OOS Sharpe > buy&hold OOS Sharpe "
          f"({bh_oos['sharpe']:.2f}) AND OOS PF > 1.1 with a sane trade count.")


if __name__ == "__main__":
    main()
