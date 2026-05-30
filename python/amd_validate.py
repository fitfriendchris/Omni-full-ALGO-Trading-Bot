"""
amd_validate.py — validate the strict AMD setup on the FULL gold history with
enough trades to matter. Runs the two credible configs (V2, V3) walk-forward,
in-sample (first 2/3) vs out-of-sample (last 1/3), with full metrics.

Usage: ../.venv-kronos/bin/python amd_validate.py [--stride 4]
"""
from __future__ import annotations
import argparse
from ict_sequential_backtest import load_mt5_csv, run
from ict_sequential import SequentialConfig
from risk_sizing import RiskConfig
from ict_amd import evaluate as amd_eval, AMDConfig

ap = argparse.ArgumentParser()
ap.add_argument("--stride", type=int, default=4)
args = ap.parse_args()

htf = load_mt5_csv("XAUUSD", "h1")
ltf = load_mt5_csv("XAUUSD", "m15")
from datetime import datetime, timezone
def span(b): return f"{datetime.fromtimestamp(b[0].time,timezone.utc):%Y-%m}→{datetime.fromtimestamp(b[-1].time,timezone.utc):%Y-%m}"
cut_t = ltf[int(len(ltf) * 0.667)].time
htf_is = [b for b in htf if b.time <= cut_t]; htf_oos = [b for b in htf if b.time > cut_t]
ltf_is = [b for b in ltf if b.time <= cut_t]; ltf_oos = [b for b in ltf if b.time > cut_t]
print(f"HTF {len(htf)} LTF {len(ltf)} span {span(ltf)} | IS {span(ltf_is)} ({len(ltf_is)})  OOS {span(ltf_oos)} ({len(ltf_oos)})")

VARIANTS = {
    "V2 OB+tightAccum":          AMDConfig(require_htf_ob_anchor=True, accum_max_width_atr=3.0),
    "V3 +realSweep":             AMDConfig(require_htf_ob_anchor=True, accum_max_width_atr=3.0, sweep_min_atr=0.25),
}
risk = RiskConfig()
def bt(h, l, cfg):
    ev = lambda hh, ll, now_ts: amd_eval(hh, ll, cfg=cfg, now_ts=now_ts)
    return run(h, l, "XAUUSD", SequentialConfig(), risk, 133.42, 0.30, 0.10, 7.0, 6, 4,
               evaluator=ev, ltf_window=800, htf_window=300, stride=args.stride, partials=True)
def pf(r): return r['profit_factor'] if isinstance(r['profit_factor'], (int, float)) else 99.9

hdr = f"{'config':20} {'set':4} {'T':>4} {'WR%':>6} {'PF':>6} {'avgR':>6} {'maxLs':>6} {'ret%':>8}"
print("\n" + hdr); print("-"*len(hdr))
for name, cfg in VARIANTS.items():
    for tag, h, l in (("IS", htf_is, ltf_is), ("OOS", htf_oos, ltf_oos)):
        r = bt(h, l, cfg)
        print(f"{name:20} {tag:4} {r['trades']:>4} {r['win_rate']:>6.1f} {pf(r):>6.2f} "
              f"{r['avg_R']:>6.2f} {r['max_consec_losses']:>6} {r['return_pct']:>8.1f}", flush=True)
print("\nCredible if OOS PF>1.1 on a meaningful trade count, consistent with IS.")
