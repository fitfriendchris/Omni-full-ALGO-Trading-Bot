#!/usr/bin/env python3
"""
orb_ny_refine.py — focused walk-forward refinement of the one honest thread:
NY-open (13:00 UTC) H1 opening-range breakout on gold.

Protocol (no peeking): tune the whole grid on IN-SAMPLE (2015–2021), SELECT the best
config by IS profit factor, then evaluate that selected config ONCE on OUT-OF-SAMPLE
(2022–2026). Also show the OOS of the top-5 IS picks (does the IS-best generalize, or
was it luck?). Per-year OOS + buy&hold benchmark.
"""
import itertools, numpy as np
from daytrade_lab import (load, strat_orb, execute, metrics, yrs, buyhold,
                          START_EQ, IS_END, row, NAN)

def run_cfg(df, p, risk, be):
    sig = strat_orb(df, open_hour=13, range_bars=p["rb"], rr=p["rr"],
                    body_atr=p["body"], stop_cap_atr=p["scap"], long_only=False)
    eq, tr = execute(df, sig, risk_pct=risk, breakeven_r=be)
    return metrics(eq, tr, yrs(df))

def main():
    df = load("h1").dropna(subset=["Open","High","Low","Close"])
    IS = df[df.index <= IS_END]; OOS = df[df.index > IS_END]

    grid = []
    for rb in (2, 4):
        for rr in (2.0, 3.0, 4.0):
            for body in (0.0, 0.5, 0.8):
                for scap in (0.0, 1.5, 2.5):
                    for be in (None, 1.0):
                        for risk in (0.02, 0.03):
                            grid.append(dict(rb=rb, rr=rr, body=body, scap=scap, be=be, risk=risk))

    scored = []
    for g in grid:
        p = dict(rb=g["rb"], rr=g["rr"], body=g["body"], scap=g["scap"])
        m_is = run_cfg(IS, p, g["risk"], g["be"])
        if not m_is or m_is["n"] < 80:        # need enough IS trades to trust the tune
            continue
        scored.append((g, m_is))
    # rank by IS profit factor, tie-break IS return
    scored.sort(key=lambda x: ((0 if x[1]["pf"] == float("inf") else x[1]["pf"]), x[1]["ret"]),
                reverse=True)

    print("="*118)
    print("ORB NY-OPEN (13:00 UTC) H1 — walk-forward refinement")
    print(f"Grid: {len(grid)} configs · {len(scored)} with enough IS trades · "
          f"IS ≤ {IS_END} · OOS 2022→ · costs ~$0.40RT+$7/lot")
    print("="*118)

    print("\n── Top 5 by IN-SAMPLE, with their OUT-OF-SAMPLE result (does it generalize?) ──")
    label = lambda g: f"rb{g['rb']} rr{g['rr']:.0f} body{g['body']} scap{g['scap']} be{g['be']} r{int(g['risk']*100)}%"
    for g, m_is in scored[:5]:
        p = dict(rb=g["rb"], rr=g["rr"], body=g["body"], scap=g["scap"])
        m_oo = run_cfg(OOS, p, g["risk"], g["be"])
        print(f"  IS : {row(label(g), m_is).strip()}")
        print(f"  OOS: {row(label(g), m_oo).strip()}\n")

    # the honest pick = IS#1, evaluated on OOS
    g, m_is = scored[0]
    p = dict(rb=g["rb"], rr=g["rr"], body=g["body"], scap=g["scap"])
    m_oo = run_cfg(OOS, p, g["risk"], g["be"])
    print("── THE WALK-FORWARD PICK (selected on IS only) → its honest OOS ──")
    print(f"  config: {label(g)}")
    print(f"  OOS: {row('pick', m_oo).strip()}")

    print("\n── that pick, OUT-OF-SAMPLE per year ──")
    print(f"  {'year':6}{'ret':>10}{'trades':>9}")
    for y, gg in OOS.groupby(OOS.index.year):
        if len(gg) < 200: continue
        m = run_cfg(gg, p, g["risk"], g["be"])
        if m: print(f"  {y:<6}{m['ret']*100:+9.1f}%{m['n']:9}")

    bh = buyhold(OOS); bm = metrics(bh, [], yrs(OOS))
    print("\n── benchmark ──")
    print("  " + row("BUY&HOLD (OOS)", bm).strip())

    ok = (m_oo and not m_oo["ruin"] and m_oo["pf"] >= 1.3 and m_oo["n"] >= 150
          and m_oo["dd"] > -0.35 and m_oo["ret"] > 0)
    print("\n" + ("✅ PICK CLEARS the OOS bar." if ok
                  else "❌ Even the refined NY-ORB does NOT clear the OOS bar (PF≥1.3, n≥150, DD>-35%)."))

if __name__ == "__main__":
    main()
