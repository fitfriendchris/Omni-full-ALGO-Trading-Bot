#!/usr/bin/env python3
"""
enhance_orb.py — can we raise the NY-ORB's profitability without breaking it?
Tests three honest enhancement paths, all walk-forward (tune IS 2015-2021, judge OOS 2022-2026):
  A) SCALING/PYRAMIDING the winner (add as trend extends + ATR trail) vs the fixed-4R base.
  B) MORE SETUPS within the trend (extra long sessions / lower TF) — does frequency help or just add cost?
  C) The 40-SCALPS/DAY hypothesis on M5 — show the cost wall.
"""
import numpy as np
from daytrade_lab import (load, strat_orb, execute, execute_scaled, metrics, yrs,
                          buyhold, IS_END, row)

BASE = dict(open_hour=13, range_bars=4, rr=4.0, body_atr=0.5, stop_cap_atr=2.5, long_only=True)

def main():
    h1 = load("h1").dropna(subset=["Open","High","Low","Close"])
    IS = h1[h1.index <= IS_END]; OOS = h1[h1.index > IS_END]
    def sig(d, **kw): return strat_orb(d, **{**BASE, **kw})

    print("="*112)
    print("ENHANCE NY-ORB — walk-forward (IS≤2021 tune / OOS 2022+ judge), real costs, $160 start, 2% risk")
    print("="*112)

    # reference: validated base (fixed 4R)
    eb,tb=execute(OOS, sig(OOS), risk_pct=0.02)
    mb=metrics(eb,tb,yrs(OOS))
    print("\n[REFERENCE] base NY-ORB fixed-4R, OOS:")
    print("  "+row("base",mb).strip())

    # ── A) scaling / pyramiding ──────────────────────────────────────
    print("\n── A) SCALING (pyramid + trail) — tune on IS, then OOS ──")
    grid=[dict(pyr_max=pm, pyr_step_atr=ps, trail_atr=tr, target_rr=tg)
          for pm in (2,3,4) for ps in (0.7,1.0,1.5) for tr in (2.0,3.0) for tg in (None,6.0)]
    scored=[]
    for g in grid:
        e,t=execute_scaled(IS, sig(IS), risk_pct=0.02, **g)
        m=metrics(e,t,yrs(IS))
        if m and m["n"]>=60: scored.append((g,m))
    scored.sort(key=lambda x:(0 if x[1]["pf"]==float("inf") else x[1]["pf"], x[1]["ret"]), reverse=True)
    lab=lambda g:f"pyr{g['pyr_max']} step{g['pyr_step_atr']} trail{g['trail_atr']} tgt{g['target_rr']}"
    print("  top-3 by IS, with OOS:")
    for g,mis in scored[:3]:
        eo,to=execute_scaled(OOS, sig(OOS), risk_pct=0.02, **g); mo=metrics(eo,to,yrs(OOS))
        print(f"   IS  {lab(g):34} {row('',mis).strip()}")
        print(f"   OOS {lab(g):34} {row('',mo).strip()}\n")
    # the IS-pick on OOS = honest scaling verdict
    gbest=scored[0][0]
    eo,to=execute_scaled(OOS, sig(OOS), risk_pct=0.02, **gbest); ms=metrics(eo,to,yrs(OOS))
    print(f"  → SCALING pick (IS-selected) OOS: {row(lab(gbest),ms).strip()}")
    print(f"    base OOS for comparison:        {row('base',mb).strip()}")

    # ── B) more setups: extra sessions + lower TF frequency ──────────
    print("\n── B) MORE SETUPS — add London session + M15, long-only (does frequency help?) ──")
    m15=load("m15").dropna(subset=["Open","High","Low","Close"]); OOS15=m15[m15.index>IS_END]
    variants={
        "NY only (base)":            (OOS, dict()),
        "London only (07h)":         (OOS, dict(open_hour=7)),
        "NY rb2 (more triggers)":    (OOS, dict(range_bars=2)),
        "NY on M15":                 (OOS15, dict()),
        "London on M15":             (OOS15, dict(open_hour=7)),
    }
    for name,(d,kw) in variants.items():
        e,t=execute(d, sig(d,**kw), risk_pct=0.02); m=metrics(e,t,yrs(d))
        print("  "+row(name,m).strip())

    # ── C) 40-scalps/day hypothesis on M5 ────────────────────────────
    print("\n── C) HIGH-FREQUENCY SCALP test on M5 (the '40 setups/day' idea) ──")
    m5=load("m5").dropna(subset=["Open","High","Low","Close"]); OOS5=m5[m5.index>IS_END]
    # many small breakouts, all sessions, tight 1-1.5R targets
    for rr in (1.0,1.5,2.0):
        e,t=execute(OOS5, strat_orb(OOS5, open_hour=0, range_bars=2, rr=rr, body_atr=0.5,
                                    stop_cap_atr=1.0, long_only=True), risk_pct=0.02)
        m=metrics(e,t,yrs(OOS5))
        print(f"  M5 all-session rr{rr}: "+row("",m).strip())

    print("\n── benchmark ──")
    print("  "+row("BUY&HOLD OOS", metrics(buyhold(OOS),[],yrs(OOS))).strip())

if __name__=="__main__":
    main()
