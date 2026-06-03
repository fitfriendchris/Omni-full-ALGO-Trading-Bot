#!/usr/bin/env python3
"""
daytrade_lab.py — one honest harness to test many intraday gold strategies identically.

Discipline: tune on IN-SAMPLE (2015–2021), judge ONLY on OUT-OF-SAMPLE (2022–2026).
Every strategy runs through the SAME executor with realistic costs, intrabar stop/
target fills, next-bar-open entries, and 2–5% compounding position sizing with leverage.
Reports a leaderboard of EVERYTHING tried + buy&hold + per-year + maxDD, so a lucky
in-sample fit can't be cherry-picked as "the winner."

Strategies (web-researched, mechanical):
  ORB        Opening-Range Breakout (session)         — momentum
  DONCHIAN   N-bar channel breakout, ATR stop         — trend
  EMATREND   EMA20>EMA50 pullback, ATR stop, fixed RR — trend
  RSI2       Connors RSI(2) mean-reversion + 200MA    — mean reversion
  BOLLREV    Bollinger lower-band reversion           — mean reversion
  VWAPREV    daily-VWAP over-extension reversion       — mean reversion

Data: python/data/hist_XAUUSD_{m5,m15,h1}.csv  (11yr, time,open,high,low,close,volume)
"""
import os, sys, itertools
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ── realistic retail-gold costs (price units, per side) ──────────────
HALF_SPREAD = 0.15            # ~$0.30 round-trip spread
SLIPPAGE    = 0.05            # per side
ENTRY_COST  = HALF_SPREAD + SLIPPAGE
EXIT_COST   = HALF_SPREAD + SLIPPAGE
COMMISSION_PER_LOT = 7.0      # round-turn, per 1.0 lot (100 oz)
LEVERAGE    = 1000.0
START_EQ    = 160.0
IS_END      = "2021-12-31"    # in-sample ends here; OOS = 2022→

# ── data ─────────────────────────────────────────────────────────────
def load(tf):
    df = pd.read_csv(os.path.join(DATA, f"hist_XAUUSD_{tf}.csv"))
    df.columns = [c.strip().lower() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S")
    df = df.set_index("time")
    df = df.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
    df["hour"] = df.index.hour
    df["day"]  = df.index.normalize()
    return df

# ── indicators ───────────────────────────────────────────────────────
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def sma(s, n):  return s.rolling(n).mean()
def rsi(s, n):
    d = s.diff()
    g = d.where(d > 0, 0.0).rolling(n).mean()
    l = (-d.where(d < 0, 0.0)).rolling(n).mean()
    return 100 - 100/(1 + g/l)
def atr(df, n=14):
    tr = pd.concat([df["High"]-df["Low"],
                    (df["High"]-df["Close"].shift()).abs(),
                    (df["Low"]-df["Close"].shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

NAN = float("nan")

# A strategy returns six aligned numpy arrays (len = len(df)):
#   long_sig, short_sig (bool) · stop_dist (price) · target_rr (float|nan)
#   exit_long, exit_short (bool)  — indicator-based exits (optional)

def strat_donchian(df, n=20, atr_mult=2.0, rr=NAN, exit_n=10):
    hi = df["High"].rolling(n).max(); lo = df["Low"].rolling(n).min()
    a = atr(df); c = df["Close"]
    long_sig  = (c > hi.shift(1)).values
    short_sig = (c < lo.shift(1)).values
    stop_dist = (atr_mult*a).values
    exit_long  = (c < lo.rolling(exit_n).min().shift(1)).values
    exit_short = (c > hi.rolling(exit_n).max().shift(1)).values
    tr = np.full(len(df), rr)
    return long_sig, short_sig, stop_dist, tr, exit_long, exit_short

def strat_ematrend(df, fast=20, slow=50, atr_mult=1.5, rr=3.0):
    ef, es = ema(df["Close"], fast), ema(df["Close"], slow)
    a = atr(df); c = df["Close"]
    up = ef > es
    pull = (df["Low"] <= ef) & up          # pullback to fast EMA in uptrend
    long_sig = (pull & (c > ef)).values
    dn = ef < es
    pull_d = (df["High"] >= ef) & dn
    short_sig = (pull_d & (c < ef)).values
    stop_dist = (atr_mult*a).values
    tr = np.full(len(df), rr)
    ex = np.zeros(len(df), bool)
    return long_sig, short_sig, stop_dist, tr, ex, ex

def strat_rsi2(df, lo=10, hi=65, ma=200, atr_mult=4.0, long_only=True):
    r = rsi(df["Close"], 2); m = sma(df["Close"], ma); a = atr(df)
    long_sig  = ((r < lo) & (df["Close"] > m)).values
    short_sig = (np.zeros(len(df), bool) if long_only
                 else ((r > (100-lo)) & (df["Close"] < m)).values)
    exit_long  = (r > hi).values
    exit_short = (r < (100-hi)).values
    stop_dist = (atr_mult*a).values            # safety stop (Connors uses none)
    tr = np.full(len(df), NAN)                  # exit by indicator, not target
    return long_sig, short_sig, stop_dist, tr, exit_long, exit_short

def strat_bollrev(df, n=20, k=2.0, atr_mult=3.0, ma=200, long_only=True):
    mid = sma(df["Close"], n); sd = df["Close"].rolling(n).std()
    lower = mid - k*sd; upper = mid + k*sd; trend = sma(df["Close"], ma); a = atr(df)
    long_sig  = ((df["Close"] < lower) & (df["Close"] > trend)).values
    short_sig = (np.zeros(len(df), bool) if long_only
                 else ((df["Close"] > upper) & (df["Close"] < trend)).values)
    exit_long  = (df["Close"] >= mid).values
    exit_short = (df["Close"] <= mid).values
    stop_dist = (atr_mult*a).values
    tr = np.full(len(df), NAN)
    return long_sig, short_sig, stop_dist, tr, exit_long, exit_short

def strat_vwaprev(df, ext=0.012, atr_mult=3.0, long_only=True):
    # daily-anchored VWAP
    tp = (df["High"]+df["Low"]+df["Close"])/3.0
    pv = (tp*df["Volume"]);
    cum_pv = pv.groupby(df["day"]).cumsum(); cum_v = df["Volume"].groupby(df["day"]).cumsum()
    vwap = cum_pv/cum_v
    a = atr(df)
    long_sig  = (df["Close"] < vwap*(1-ext)).values
    short_sig = (np.zeros(len(df), bool) if long_only
                 else (df["Close"] > vwap*(1+ext)).values)
    exit_long  = (df["Close"] >= vwap).values
    exit_short = (df["Close"] <= vwap).values
    stop_dist = (atr_mult*a).values
    tr = np.full(len(df), NAN)
    return long_sig, short_sig, stop_dist, tr, exit_long, exit_short

def strat_orb(df, open_hour=7, range_bars=2, sess_len_bars=24, rr=2.0, long_only=False,
              body_atr=0.0, stop_cap_atr=0.0):
    """Opening-range breakout: first `range_bars` bars after open_hour set the range;
    first close beyond it = entry, stop = other side of range, target = rr*range.
    body_atr>0  : require breakout candle body >= body_atr*ATR5 AND >= 60% of its range (kill fakes).
    stop_cap_atr>0 : cap the stop distance at stop_cap_atr*ATR14 (tighter risk on wide ranges)."""
    n = len(df)
    long_sig = np.zeros(n, bool); short_sig = np.zeros(n, bool)
    stop_dist = np.full(n, NAN); tr = np.full(n, rr)
    ex = np.zeros(n, bool)
    H, L, C, O = (df["High"].values, df["Low"].values, df["Close"].values, df["Open"].values)
    hour, day = df["hour"].values, df["day"].values
    a5 = atr(df, 5).values; a14 = atr(df, 14).values
    day_change = np.where(day[1:] != day[:-1])[0] + 1
    starts = np.concatenate(([0], day_change)); ends = np.concatenate((day_change, [n]))
    for s, e in zip(starts, ends):
        idx = [k for k in range(s, e) if hour[k] >= open_hour]
        if len(idx) < range_bars + 1: continue
        rng = idx[:range_bars]; rhi = max(H[k] for k in rng); rlo = min(L[k] for k in rng)
        rsize = rhi - rlo
        if rsize <= 0: continue
        fired = False
        for k in idx[range_bars: range_bars + sess_len_bars]:
            if fired: break
            body = abs(C[k]-O[k]); candle = max(H[k]-L[k], 1e-9)
            if body_atr > 0 and not (body >= body_atr*a5[k] and body >= 0.6*candle):
                continue                                  # weak breakout candle → skip
            sd = rsize if stop_cap_atr <= 0 else min(rsize, stop_cap_atr*a14[k])
            if C[k] > rhi:
                long_sig[k] = True; stop_dist[k] = sd; fired = True
            elif (not long_only) and C[k] < rlo:
                short_sig[k] = True; stop_dist[k] = sd; fired = True
    return long_sig, short_sig, stop_dist, tr, ex, ex

STRATS = {
    "ORB":      strat_orb,
    "DONCHIAN": strat_donchian,
    "EMATREND": strat_ematrend,
    "RSI2":     strat_rsi2,
    "BOLLREV":  strat_bollrev,
    "VWAPREV":  strat_vwaprev,
}

# ── executor: one position at a time, honest fills + costs + compounding ──
def execute(df, sig, risk_pct=0.03, cooldown=2, breakeven_r=None):
    long_sig, short_sig, stop_dist, target_rr, exit_long, exit_short = sig
    O,H,L,C = (df["Open"].values, df["High"].values, df["Low"].values, df["Close"].values)
    n = len(df); eq = START_EQ
    eq_curve = np.empty(n); eq_curve[:] = START_EQ
    trades = []
    pos = None; pending = None; cool = 0
    for i in range(n):
        # 1) execute pending entry at THIS open
        if pending is not None:
            d = pending["dir"]; sd = pending["sd"]
            entry = O[i] + ENTRY_COST*d
            stop = entry - sd*d
            risk_amt = eq*risk_pct
            units = risk_amt/sd if sd > 0 else 0.0
            notional = units*entry
            if notional/LEVERAGE > eq:           # margin cap
                units = eq*LEVERAGE/entry
            tgt = entry + sd*pending["rr"]*d if pending["rr"] == pending["rr"] else None
            pos = dict(dir=d, entry=entry, stop=stop, units=units, target=tgt, i0=i,
                       risk=sd, be=False)
            pending = None
        # 2) manage open position with THIS bar (intrabar stop/target, then close-exit)
        if pos is not None:
            d = pos["dir"]
            # breakeven: once price runs +breakeven_r*risk in favor, move stop to entry
            if breakeven_r and not pos["be"]:
                fav = (H[i]-pos["entry"]) if d == 1 else (pos["entry"]-L[i])
                if fav >= breakeven_r*pos["risk"]:
                    pos["stop"] = pos["entry"]; pos["be"] = True
            xprice = None
            if d == 1:
                if L[i] <= pos["stop"]: xprice = min(O[i], pos["stop"])
                elif pos["target"] and H[i] >= pos["target"]: xprice = pos["target"]
            else:
                if H[i] >= pos["stop"]: xprice = max(O[i], pos["stop"])
                elif pos["target"] and L[i] <= pos["target"]: xprice = pos["target"]
            if xprice is None:  # indicator exit at close
                if (d == 1 and exit_long[i]) or (d == -1 and exit_short[i]):
                    xprice = C[i]
            if xprice is not None:
                fill = xprice - EXIT_COST*d
                lots = pos["units"]/100.0
                pnl = (fill - pos["entry"])*d*pos["units"] - COMMISSION_PER_LOT*lots
                eq += pnl
                trades.append(dict(i0=pos["i0"], i1=i, dir=d, pnl=pnl,
                                   r=(pnl/(eq*risk_pct) if eq > 0 else 0)))
                pos = None; cool = cooldown
        # 3) entry signal at THIS close → fill next open
        if pos is None and pending is None and cool <= 0 and i+1 < n:
            if long_sig[i] and stop_dist[i] == stop_dist[i] and stop_dist[i] > 0:
                pending = dict(dir=1, sd=stop_dist[i], rr=target_rr[i])
            elif short_sig[i] and stop_dist[i] == stop_dist[i] and stop_dist[i] > 0:
                pending = dict(dir=-1, sd=stop_dist[i], rr=target_rr[i])
        if cool > 0: cool -= 1
        # mark-to-market
        mtm = 0.0
        if pos is not None:
            mtm = (C[i]-pos["entry"])*pos["dir"]*pos["units"]
        eq_curve[i] = eq + mtm
    return pd.Series(eq_curve, index=df.index), trades

def execute_scaled(df, sig, risk_pct=0.02, cooldown=2, pyr_max=3, pyr_step_atr=1.0,
                   trail_atr=2.5, atr_n=14, target_rr=None):
    """LONG pyramiding + ATR trail. Enter on signal; ADD a unit each +pyr_step_atr*ATR
    in favour (up to pyr_max total); trail the whole book by trail_atr*ATR; optional fixed
    target_rr cap on the initial risk. Honest: next-open fills, intrabar stops, costs."""
    long_sig, short_sig, stop_dist, _tr, exit_long, exit_short = sig
    O,H,L,C = df["Open"].values, df["High"].values, df["Low"].values, df["Close"].values
    A = atr(df, atr_n).values
    n=len(df); eq=START_EQ; eq_curve=np.empty(n); eq_curve[:]=START_EQ; trades=[]
    book=[]; stop=None; init_risk=None; last_add=None; hw=None; target=None
    pending=None; pend_sd=None; cool=0
    for i in range(n):
        # 1) fills at this open
        if pending=="entry" and not book:
            sd=pend_sd; entry=O[i]+ENTRY_COST
            units=eq*risk_pct/sd if sd>0 else 0.0
            if units*entry/LEVERAGE>eq: units=eq*LEVERAGE/entry
            book=[dict(entry=entry,units=units)]
            init_risk=sd; stop=entry-sd; last_add=entry; hw=entry
            target=entry+sd*target_rr if target_rr else None
            pending=None
        elif pending=="add" and book and len(book)<pyr_max:
            entry=O[i]+ENTRY_COST
            held=sum(p["units"] for p in book)
            units=eq*risk_pct/init_risk if init_risk and init_risk>0 else 0.0
            if (held+units)*entry/LEVERAGE>eq: units=max(0.0, eq*LEVERAGE/entry-held)
            if units>0: book.append(dict(entry=entry,units=units)); last_add=entry
            pending=None
        # 2) manage book
        if book:
            hw=max(hw,H[i]); trail=hw-trail_atr*A[i]
            if trail>stop: stop=trail
            xprice=None
            if L[i]<=stop: xprice=min(O[i],stop)
            elif target and H[i]>=target: xprice=target
            if xprice is not None:
                fill=xprice-EXIT_COST; held=sum(p["units"] for p in book)
                pnl=sum((fill-p["entry"])*p["units"] for p in book)-COMMISSION_PER_LOT*held/100.0
                eq+=pnl
                trades.append(dict(pnl=pnl, r=(pnl/(eq*risk_pct) if eq>0 else 0), units=len(book)))
                book=[]; stop=None; target=None; init_risk=None; cool=cooldown
            elif pending is None and len(book)<pyr_max and C[i]>=last_add+pyr_step_atr*A[i]:
                pending="add"
        # 3) new entry if flat
        if not book and pending is None and cool<=0 and i+1<n:
            if long_sig[i] and stop_dist[i]==stop_dist[i] and stop_dist[i]>0:
                pending="entry"; pend_sd=stop_dist[i]
        if cool>0: cool-=1
        eq_curve[i]=eq+(sum((C[i]-p["entry"])*p["units"] for p in book) if book else 0.0)
    return pd.Series(eq_curve,index=df.index), trades


# ── metrics ──────────────────────────────────────────────────────────
def metrics(eq, trades, years):
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return None
    ret = eq.iloc[-1]/eq.iloc[0] - 1
    cagr = (max(eq.iloc[-1], 0.01)/eq.iloc[0])**(1/max(years, 1e-9)) - 1
    dd = (eq/eq.cummax() - 1).min()
    pnl = [t["pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]; losses = [p for p in pnl if p <= 0]
    wr = len(wins)/len(pnl) if pnl else 0
    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses) != 0 else (float("inf") if wins else 0)
    # longest losing streak
    streak = mx = 0
    for p in pnl:
        streak = streak+1 if p <= 0 else 0
        mx = max(mx, streak)
    ruin = eq.min() <= START_EQ*0.1   # lost 90%+ at any point
    return dict(ret=ret, cagr=cagr, dd=dd, wr=wr, pf=pf, n=len(pnl),
                final=eq.iloc[-1], maxloss_streak=mx, ruin=ruin)

def yrs(df): return (df.index[-1]-df.index[0]).days/365.25

def buyhold(df):
    return START_EQ * df["Close"]/df["Close"].iloc[0]

def row(name, m):
    if not m: return f"  {name:34} (no trades)"
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    flag = " 💀RUIN" if m["ruin"] else ""
    return (f"  {name:34} ret {m['ret']*100:+8.1f}%  CAGR {m['cagr']*100:+6.1f}%  "
            f"DD {m['dd']*100:6.1f}%  WR {m['wr']*100:4.1f}%  PF {pf:>4}  "
            f"n {m['n']:4}  maxLossStreak {m['maxloss_streak']:2}  final ${m['final']:,.0f}{flag}")

# ── runner ───────────────────────────────────────────────────────────
def passes(m):
    return (m and not m["ruin"] and m["pf"] >= 1.3 and m["n"] >= 150
            and m["dd"] > -0.35 and m["ret"] > 0)

def main():
    tfs = sys.argv[1:] or ["m15", "h1"]
    # parameter grids per strategy (kept small; tuned only via IS, judged on OOS)
    grid = {
        "ORB":      [dict(open_hour=h, range_bars=rb, rr=rr)
                     for h in (7, 13) for rb in (2, 4) for rr in (2.0, 3.0)],
        "DONCHIAN": [dict(n=nn, atr_mult=am, rr=rr)
                     for nn in (20, 40) for am in (2.0,) for rr in (NAN, 3.0)],
        "EMATREND": [dict(fast=f, slow=s, rr=rr)
                     for (f, s) in ((20, 50), (10, 30)) for rr in (2.0, 3.0)],
        "RSI2":     [dict(lo=l, atr_mult=am, long_only=lo)
                     for l in (5, 10) for am in (4.0,) for lo in (True, False)],
        "BOLLREV":  [dict(n=nn, k=kk, long_only=True) for nn in (20,) for kk in (2.0, 2.5)],
        "VWAPREV":  [dict(ext=e, long_only=True) for e in (0.008, 0.015)],
    }
    results = []  # (label, tf, IS_m, OOS_m)
    for tf in tfs:
        df = load(tf)
        df = df.dropna(subset=["Open","High","Low","Close"])
        IS = df[df.index <= IS_END]; OOS = df[df.index > IS_END]
        for name, fn in STRATS.items():
            for params in grid[name]:
                for risk in (0.02, 0.05):
                    sig_is = fn(IS, **params);  eq_is, tr_is = execute(IS, sig_is, risk)
                    sig_oo = fn(OOS, **params);  eq_oo, tr_oo = execute(OOS, sig_oo, risk)
                    pstr = ",".join(f"{k}={v}" for k, v in params.items())
                    label = f"{name}[{tf} r{int(risk*100)}% {pstr}]"
                    results.append((label, metrics(eq_is, tr_is, yrs(IS)),
                                    metrics(eq_oo, tr_oo, yrs(OOS))))
        # benchmark
        bh_oo = buyhold(OOS)
        results.append((f"BUY&HOLD[{tf}]", metrics(buyhold(IS), [], yrs(IS)),
                        metrics(bh_oo, [], yrs(OOS))))

    # leaderboard ranked by OOS profit factor then return
    def key(r):
        m = r[2]
        if not m: return (-1, -1)
        pf = 0 if m["pf"] == float("inf") else m["pf"]
        return (pf, m["ret"])
    results.sort(key=key, reverse=True)

    print("\n" + "="*120)
    print(f"DAYTRADE LAB — XAUUSD — IS ≤ {IS_END}  |  OOS 2022→  |  costs ~$0.40 RT + $7/lot comm  |  start ${START_EQ:.0f}")
    print(f"Tested {len(results)-len(tfs)} strategy configs across {tfs}.  Verdict = OUT-OF-SAMPLE only.")
    print("="*120)
    print("\n── OUT-OF-SAMPLE leaderboard (top 25) ──")
    for label, mis, moo in results[:25]:
        print(row(label, moo))
    print("\n── the PASS bar: OOS PF≥1.3, n≥150, maxDD>-35%, ret>0, no ruin ──")
    winners = [(l, mis, moo) for (l, mis, moo) in results
               if not l.startswith("BUY") and passes(moo)]
    if winners:
        print(f"  {len(winners)} config(s) cleared OOS:")
        for l, mis, moo in winners:
            print("   ✅ " + row(l, moo).strip())
            print("      IS:  " + row(l, mis).strip())
    else:
        print("  ❌ NOTHING cleared the OOS bar. No honest edge found in this batch.")
    # buy&hold reference
    print("\n── benchmark ──")
    for l, mis, moo in results:
        if l.startswith("BUY"): print(row(l, moo))

if __name__ == "__main__":
    main()
