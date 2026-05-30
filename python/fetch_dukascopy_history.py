"""
fetch_dukascopy_history.py — pull multi-year XAUUSD/XAGUSD history from Dukascopy
and write it in the CSV format `ict_sequential_backtest.py --source mt5` reads.

WHY: the live MT5 JSON bridge keeps only a shallow window (M15≈600 bars / 6 days,
H1≈300 / 12 days) — far too little to validate an edge (the 60-day yfinance screen
yielded just 3 trades). Dukascopy serves clean, deep spot-metal history (bid OHLCV)
for free, fully scriptable. Broker (MidasFX) spread differences are absorbed by the
backtest's own spread/slippage params, so spot gold is a sound validation proxy.

Output: `<MT5 Common/Files>/hist_<SYMBOL>_<tf>.csv` with columns
    time,open,high,low,close,volume   ; time = YYYY.MM.DD HH:MM:SS (UTC)
plus a mirror copy under `python/data/` for safety.

Usage:
    ../.venv-kronos/bin/python fetch_dukascopy_history.py \
        --symbol XAUUSD --tfs h1,m15,m5 --years 3
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import dukascopy_python as duka
from dukascopy_python.instruments import (
    INSTRUMENT_FX_METALS_XAU_USD, INSTRUMENT_FX_METALS_XAG_USD,
)

MT5_COMMON = ("/Users/yuhfriendchris/Library/Application Support/"
              "net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/"
              "MetaQuotes/Terminal/Common/Files")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

INSTRUMENT = {
    "XAUUSD": INSTRUMENT_FX_METALS_XAU_USD,
    "XAGUSD": INSTRUMENT_FX_METALS_XAG_USD,
}
INTERVAL = {
    "h4":  duka.INTERVAL_HOUR_4,
    "h1":  duka.INTERVAL_HOUR_1,
    "m30": duka.INTERVAL_MIN_30,
    "m15": duka.INTERVAL_MIN_15,
    "m5":  duka.INTERVAL_MIN_5,
    "m1":  duka.INTERVAL_MIN_1,
}


def fetch_tf(symbol: str, tf: str, start: datetime, end: datetime):
    """Fetch one timeframe in monthly chunks (progress + resilience), dedup, sort."""
    import pandas as pd
    inst, interval = INSTRUMENT[symbol], INTERVAL[tf]
    frames = []
    cur = start
    chunks = 0
    while cur < end:
        nxt = min(cur + timedelta(days=31), end)
        try:
            df = duka.fetch(inst, interval, duka.OFFER_SIDE_BID, cur, nxt)
            if df is not None and len(df):
                frames.append(df)
                chunks += 1
        except Exception as e:
            print(f"    ! {tf} {cur:%Y-%m} chunk failed ({type(e).__name__}: {e}) — skipping")
        print(f"    {tf} {cur:%Y-%m} … {sum(len(f) for f in frames):>7} bars so far", flush=True)
        cur = nxt
    if not frames:
        return None
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    return out


def write_csv(df, symbol: str, tf: str) -> str:
    """Write to the MT5 Common/Files dir (backtest reads here) + a data/ mirror."""
    os.makedirs(DATA_DIR, exist_ok=True)
    lines = ["time,open,high,low,close,volume\n"]
    for ts, r in df.iterrows():
        t = ts.tz_convert("UTC") if ts.tzinfo else ts
        lines.append(f"{t:%Y.%m.%d %H:%M:%S},{r.open:.3f},{r.high:.3f},"
                     f"{r.low:.3f},{r.close:.3f},{r.volume:.2f}\n")
    body = "".join(lines)
    paths = [os.path.join(MT5_COMMON, f"hist_{symbol}_{tf}.csv"),
             os.path.join(DATA_DIR, f"hist_{symbol}_{tf}.csv")]
    for p in paths:
        try:
            with open(p, "w") as f:
                f.write(body)
        except Exception as e:
            print(f"    ! could not write {p}: {e}")
    return paths[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD", choices=list(INSTRUMENT))
    ap.add_argument("--tfs", default="h1,m15,m5", help="comma list: h4,h1,m30,m15,m5,m1")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: today UTC)")
    args = ap.parse_args()

    end = (datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    start = end - timedelta(days=int(args.years * 365.25))
    tfs = [t.strip().lower() for t in args.tfs.split(",") if t.strip()]

    print(f"Dukascopy {args.symbol}  {start:%Y-%m-%d} -> {end:%Y-%m-%d}  TFs={tfs}")
    for tf in tfs:
        print(f"\n[{tf}] fetching…")
        df = fetch_tf(args.symbol, tf, start, end)
        if df is None or not len(df):
            print(f"[{tf}] NO DATA")
            continue
        path = write_csv(df, args.symbol, tf)
        span = f"{df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}"
        print(f"[{tf}] DONE: {len(df):,} bars  span {span}\n   -> {path}")


if __name__ == "__main__":
    main()
