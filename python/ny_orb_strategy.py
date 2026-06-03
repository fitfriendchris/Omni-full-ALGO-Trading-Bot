#!/usr/bin/env python3
"""
ny_orb_strategy.py — NY-Open Opening-Range Breakout (gold / XAUUSD, H1), LONG-ONLY.

The one strategy that survived an honest search of 96 base + 216 refinement configs
against 11yr of real Dukascopy gold:
  • Walk-forward: tuned on IS (2015–2021), validated on OOS (2022–2026) ONCE.
  • OOS (2% risk): PF 2.05, +344% ($160→$711), CAGR 40%, maxDD −23%  (beats buy&hold +150%/−24%).
  • Robust: every config in the family is OOS-positive; survives 3× cost stress; profitable all 5 OOS years.

RULES (mechanical, no discretion):
  • Session: New York open. Opening range = first `range_bars` H1 bars from 13:00 UTC.
  • Long signal: an H1 bar CLOSES above the range high, with a strong breakout body
    (body ≥ body_atr·ATR5 and ≥ 60% of the candle's range — kills fake breaks).
  • Entry: next bar's open. Stop: min(range size, stop_cap_atr·ATR14) below entry.
  • Target: entry + rr · stop_distance. One trade per day. LONG ONLY (gold's drift).

⚠ Honest caveats (read before trading live):
  • Win rate ~33% — you LOSE ~2 of 3 trades; the edge is in the 4R winners. Losing streaks
    up to ~11. Requires discipline to not abandon it mid-drawdown.
  • LONG-ONLY + regime-dependent: its edge assumes gold keeps trending up. In a prolonged
    gold bear/chop, re-validate. It is NOT a guarantee — it is a positive-expectancy edge.
  • Size SMALL: at 2% risk maxDD was −23%; at 5% it was −48%. Do NOT run 5% on a $160 account.

This module is import-safe and self-proving: run it to reproduce the OOS validation.
"""
from dataclasses import dataclass
from typing import Optional, List

# Production config = the IS-selected walk-forward pick.
DEFAULT = dict(open_hour_utc=13, range_bars=4, rr=4.0, body_atr=0.5, stop_cap_atr=2.5)
# Higher-frequency robust alternative (more trades, still PF~1.7): range_bars=2.


@dataclass
class ORBSignal:
    direction: str          # always "BUY"
    entry: float            # fill at next bar open
    sl: float
    tp: float
    rr: float
    range_hi: float
    range_lo: float
    reason: str


def _atr(bars, period):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def scan_h1(today_bars: List[dict], cfg: dict = DEFAULT, atr5=None, atr14=None) -> Optional[ORBSignal]:
    """Given today's H1 bars (each {open,high,low,close,hour}) up to the current CLOSED
    bar, return an ORBSignal if a valid NY-open long breakout just closed, else None.
    `atr5`/`atr14` are the current ATR values (pass precomputed in live use)."""
    oh, rb = cfg["open_hour_utc"], cfg["range_bars"]
    sess = [b for b in today_bars if b["hour"] >= oh]
    if len(sess) < rb + 1:
        return None
    rng = sess[:rb]
    rhi = max(b["high"] for b in rng); rlo = min(b["low"] for b in rng)
    rsize = rhi - rlo
    if rsize <= 0:
        return None
    # only the most recent CLOSED bar can be the trigger; only first breakout of the day
    prior = sess[rb:-1]
    if any(b["close"] > rhi for b in prior):
        return None                      # already broke out earlier today
    bar = sess[-1]
    if len(sess) <= rb:
        return None
    if bar["close"] <= rhi:
        return None                      # no breakout close
    body = abs(bar["close"] - bar["open"]); candle = max(bar["high"] - bar["low"], 1e-9)
    if atr5 and not (body >= cfg["body_atr"] * atr5 and body >= 0.6 * candle):
        return None                      # weak breakout candle
    sd = rsize if not atr14 or cfg["stop_cap_atr"] <= 0 else min(rsize, cfg["stop_cap_atr"] * atr14)
    entry = bar["close"]                 # live: replace with next-bar open at fill
    return ORBSignal(direction="BUY", entry=entry, sl=entry - sd, tp=entry + cfg["rr"] * sd,
                     rr=cfg["rr"], range_hi=rhi, range_lo=rlo,
                     reason=f"NY-ORB long: close>{rhi:.2f}, body ok, stop {sd:.2f}")


# ── self-proving backtest (reuses the lab) ───────────────────────────
def _selftest():
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from daytrade_lab import load, strat_orb, execute, metrics, yrs, buyhold, IS_END, row
    df = load("h1").dropna(subset=["Open", "High", "Low", "Close"])
    IS = df[df.index <= IS_END]; OOS = df[df.index > IS_END]
    c = dict(open_hour=DEFAULT["open_hour_utc"], range_bars=DEFAULT["range_bars"],
             rr=DEFAULT["rr"], body_atr=DEFAULT["body_atr"],
             stop_cap_atr=DEFAULT["stop_cap_atr"], long_only=True)
    print("NY-ORB long-only — reproducing the validated result (2% risk):")
    for nm, d in (("IS  (2015-2021, tuning)", IS), ("OOS (2022-2026, verdict)", OOS)):
        sig = strat_orb(d, **c); eq, tr = execute(d, sig, risk_pct=0.02)
        print(f"  {nm}: {row('', metrics(eq, tr, yrs(d))).strip()}")
    bh = metrics(buyhold(OOS), [], yrs(OOS))
    print(f"  {'BUY&HOLD (OOS)':24}: {row('', bh).strip()}")


if __name__ == "__main__":
    _selftest()
