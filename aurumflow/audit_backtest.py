#!/usr/bin/env python3
"""
AurumFlow — honest audit & backtest (single file).

Runs the EXACT AurumCompounderStrategy logic TWO ways on real Dukascopy XAUUSD:
  1) AS-CODED   — close-only stops, no costs, same-bar  (reproduces the vendor's optimistic +77%)
  2) HONEST     — next-bar-open fills, INTRABAR stop fills (low pierces stop),
                  spread + slippage + commission                       (what you'd actually get)
…vs BUY & HOLD gold on the same window. Reports return, CAGR, maxDD, Sharpe,
win-rate, profit factor, expectancy, trades — full window + per year + the
2020→ slice (to compare apples-to-apples with the vendor's claim).

Data: ~/Omni-full-ALGO-Trading-Bot/python/data/hist_XAUUSD_h1.csv (11yr, OHLCV).
No look-ahead. No curve-fit — strategy params are the vendor's defaults, untouched.
"""
import os, sys
import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "python", "data", "hist_XAUUSD_h1.csv")

# ── realistic retail-gold costs (price terms, per side) ──────────────
HALF_SPREAD = 0.15     # ~$0.30 round-trip spread on XAUUSD
SLIPPAGE    = 0.05     # market-order slippage per side
ENTRY_COST  = HALF_SPREAD + SLIPPAGE   # added to long entry fill
EXIT_COST   = HALF_SPREAD + SLIPPAGE   # subtracted from long exit fill

# strategy params = vendor defaults (NOT tuned here)
P = dict(risk=0.01, atr=14, ema_fast=20, ema_slow=50, rsi=14,
         pyr_max=4, pyr_step=0.7, trail_atr=1.8, init_stop_atr=1.5)


def load(tf="D"):
    df = pd.read_csv(DATA)
    df.columns = [c.strip().lower() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S")
    df = df.set_index("time").rename(columns=str.capitalize)  # Open/High/Low/Close/Volume
    if tf.upper() in ("D", "1D", "DAILY"):
        df = df.resample("1D").agg({"Open":"first","High":"max","Low":"min",
                                    "Close":"last","Volume":"sum"}).dropna()
    return df


def indicators(df):
    df = df.copy()
    df["EMA_f"] = df["Close"].ewm(span=P["ema_fast"], adjust=False).mean()
    df["EMA_s"] = df["Close"].ewm(span=P["ema_slow"], adjust=False).mean()
    d = df["Close"].diff()
    gain = d.where(d > 0, 0).rolling(P["rsi"]).mean()
    loss = (-d.where(d < 0, 0)).rolling(P["rsi"]).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss)
    tr = pd.concat([df["High"]-df["Low"],
                    (df["High"]-df["Close"].shift()).abs(),
                    (df["Low"]-df["Close"].shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(P["atr"]).mean()
    df["VolSMA"] = df["Volume"].rolling(20).mean()
    return df.dropna()


# ════════════════════════════════════════════════════════════════════
# 1) AS-CODED — faithful re-implementation of the vendor strategy
#    (close-only stop checks, NO costs, same-bar entry/exit)
# ════════════════════════════════════════════════════════════════════
def run_as_coded(df, bal=10000.0):
    df = indicators(df)
    pos, eq, trades = [], [], []
    C = df["Close"].values; A = df["ATR"].values; EF = df["EMA_f"].values
    ES = df["EMA_s"].values; R = df["RSI"].values; V = df["Volume"].values; VS = df["VolSMA"].values
    for i in range(len(df)):
        c, atr, ef, es, rsi, vol, vs = C[i], A[i], EF[i], ES[i], R[i], V[i], VS[i]
        if pos:
            trail = c - P["trail_atr"]*atr
            for p in pos:
                if trail > p["sl"]: p["sl"] = trail
        rev = ef < es or rsi > 80
        keep = []
        for p in pos:
            if c <= p["sl"] or rev:
                bal += (c - p["entry"]) * p["units"]; trades.append((c-p["entry"])*p["units"])
            else: keep.append(p)
        pos = keep
        if ef > es and rsi > 50 and vol > vs:
            if not pos:
                sl = c - P["init_stop_atr"]*atr; rpu = c - sl
                if rpu > 0: pos.append(dict(entry=c, units=bal*P["risk"]/rpu, sl=sl))
            elif len(pos) < P["pyr_max"]:
                lp = pos[-1]
                if c >= lp["entry"] + P["pyr_step"]*atr:
                    sl = c - P["trail_atr"]*atr; rpu = c - sl
                    if rpu > 0:
                        pos.append(dict(entry=c, units=bal*P["risk"]/rpu, sl=sl))
                        for p in pos: p["sl"] = max(p["sl"], sl)
        mtm = sum((c - p["entry"])*p["units"] for p in pos)
        eq.append(bal + mtm)
    return pd.Series(eq, index=df.index), trades


# ════════════════════════════════════════════════════════════════════
# 2) HONEST — next-bar-open fills, intrabar stop fills, costs
# ════════════════════════════════════════════════════════════════════
def run_honest(df, bal=10000.0):
    df = indicators(df)
    O=df["Open"].values; H=df["High"].values; L=df["Low"].values; C=df["Close"].values
    A=df["ATR"].values; EF=df["EMA_f"].values; ES=df["EMA_s"].values
    R=df["RSI"].values; V=df["Volume"].values; VS=df["VolSMA"].values
    pos, eq, trades = [], [], []
    pending = None  # 'enter' signal from previous bar's close

    def close_pos(p, fill):
        nonlocal bal
        pnl = (fill - p["entry"]) * p["units"]
        bal += pnl; trades.append(pnl)

    for i in range(len(df)):
        o, h, l, c, atr = O[i], H[i], L[i], C[i], A[i]
        # (1) execute pending entry at THIS open + entry cost
        if pending is not None:
            fill = o + ENTRY_COST
            if pending == "init":
                sl = fill - P["init_stop_atr"]*atr; rpu = fill - sl
                if rpu > 0: pos.append(dict(entry=fill, units=bal*P["risk"]/rpu, sl=sl))
            elif pending == "pyr" and len(pos) < P["pyr_max"]:
                sl = fill - P["trail_atr"]*atr; rpu = fill - sl
                if rpu > 0:
                    pos.append(dict(entry=fill, units=bal*P["risk"]/rpu, sl=sl))
                    for p in pos: p["sl"] = max(p["sl"], sl)
            pending = None
        # (2) intrabar stop fills against THIS bar's low (gap-aware)
        keep = []
        for p in pos:
            if l <= p["sl"]:
                fill = min(o, p["sl"]) - EXIT_COST   # gap-through fills at open
                close_pos(p, fill)
            else:
                keep.append(p)
        pos = keep
        # (3) trailing update on close + reversal exit (decided at close, fill at close - cost)
        if pos:
            trail = c - P["trail_atr"]*atr
            for p in pos:
                if trail > p["sl"]: p["sl"] = trail
        if pos and (EF[i] < ES[i] or R[i] > 80):
            for p in pos: close_pos(p, c - EXIT_COST)
            pos = []
        # (4) entry / pyramid signal at close → fills next open
        if EF[i] > ES[i] and R[i] > 50 and V[i] > VS[i]:
            if not pos and pending is None:
                pending = "init"
            elif pos and len(pos) < P["pyr_max"]:
                if c >= pos[-1]["entry"] + P["pyr_step"]*atr:
                    pending = "pyr"
        eq.append(bal + sum((c - p["entry"])*p["units"] for p in pos))
    return pd.Series(eq, index=df.index), trades


def buy_hold(df, bal=10000.0):
    c = df["Close"]
    return bal * c / c.iloc[0], []


# ── metrics ──────────────────────────────────────────────────────────
def metrics(eq, trades, bars_per_year):
    eq = eq.dropna()
    if len(eq) < 2: return {}
    ret = eq.iloc[-1]/eq.iloc[0] - 1
    yrs = len(eq)/bars_per_year
    cagr = (eq.iloc[-1]/eq.iloc[0])**(1/max(yrs,1e-9)) - 1
    dd = (eq/eq.cummax() - 1).min()
    r = eq.pct_change().dropna()
    sharpe = (r.mean()/r.std()*np.sqrt(bars_per_year)) if r.std() > 0 else 0.0
    wins = [t for t in trades if t > 0]; losses = [t for t in trades if t <= 0]
    wr = len(wins)/len(trades) if trades else 0
    pf = (sum(wins)/abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    exp = (sum(trades)/len(trades)) if trades else 0
    return dict(ret=ret, cagr=cagr, dd=dd, sharpe=sharpe, wr=wr, pf=pf,
                exp=exp, n=len(trades), final=eq.iloc[-1])


def fmt(m):
    if not m: return "  (no trades)"
    pf = "inf" if m["pf"]==float("inf") else f"{m['pf']:.2f}"
    return (f"  return {m['ret']*100:+8.1f}%  CAGR {m['cagr']*100:+6.1f}%  maxDD {m['dd']*100:6.1f}%  "
            f"Sharpe {m['sharpe']:5.2f}  WR {m['wr']*100:4.1f}%  PF {pf:>4}  "
            f"exp ${m['exp']:+.0f}  trades {m['n']}  final ${m['final']:,.0f}")


def bars_per_year(df):
    span = (df.index[-1]-df.index[0]).total_seconds()/ (365.25*24*3600)
    return len(df)/max(span,1e-9)


def report(df, label):
    bpy = bars_per_year(df)
    ac_eq, ac_tr = run_as_coded(df)
    hn_eq, hn_tr = run_honest(df)
    bh_eq, _     = buy_hold(df)
    print(f"\n══════ {label}  ({df.index[0].date()} → {df.index[-1].date()}, {len(df)} bars) ══════")
    print("AS-CODED (vendor-style: close-only stops, no costs)")
    print(fmt(metrics(ac_eq, ac_tr, bpy)))
    print("HONEST   (next-open fills, intrabar stops, spread+slippage)")
    print(fmt(metrics(hn_eq, hn_tr, bpy)))
    print("BUY & HOLD gold")
    print(fmt(metrics(bh_eq, [], bpy)))


def main():
    print("Loading real Dukascopy XAUUSD…")
    d1 = load("D")
    h1 = load("H1")
    report(d1, "DAILY (strategy's design timeframe) — FULL 11yr")
    report(h1, "H1 (intraday reality) — FULL 11yr")
    # vendor-comparable slice
    d20 = d1[d1.index >= "2020-01-01"]
    report(d20, "DAILY — 2020→ (compare to vendor's +77% claim)")
    # per-year on daily, honest only
    print("\n══════ DAILY per-year — HONEST vs BUY&HOLD ══════")
    print(f"{'year':6} {'honest ret':>12} {'buyhold ret':>12} {'honest trades':>14}")
    for y, g in d1.groupby(d1.index.year):
        if len(g) < 60: continue
        he, ht = run_honest(g); bh, _ = buy_hold(g)
        hr = he.iloc[-1]/he.iloc[0]-1; br = bh.iloc[-1]/bh.iloc[0]-1
        print(f"{y:6} {hr*100:+11.1f}% {br*100:+11.1f}% {len(ht):14}")


if __name__ == "__main__":
    main()
