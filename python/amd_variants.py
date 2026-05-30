"""
amd_variants.py — does ENFORCING Chris's selectivity rules turn the AMD engine
positive? Each variant adds a rule HE stated (not a fitted param), and every
variant is scored in-sample (first 2/3) AND out-of-sample (last 1/3). A variant is
only credible if it is positive OOS, not just IS.

Loads the 3yr data once; reuses the validated backtest run() + partials manager.
"""
from __future__ import annotations
import sys
from ict_sequential_backtest import load_mt5_csv, run
from ict_sequential import SequentialConfig
from risk_sizing import RiskConfig
from ict_amd import evaluate as amd_eval, AMDConfig

htf = load_mt5_csv("XAUUSD", "h1")
ltf = load_mt5_csv("XAUUSD", "m15")
cut_t = ltf[int(len(ltf) * 0.667)].time
htf_is = [b for b in htf if b.time <= cut_t]; htf_oos = [b for b in htf if b.time > cut_t]
ltf_is = [b for b in ltf if b.time <= cut_t]; ltf_oos = [b for b in ltf if b.time > cut_t]
print(f"data: HTF {len(htf)} LTF {len(ltf)} | IS LTF {len(ltf_is)} OOS LTF {len(ltf_oos)}")

VARIANTS = {
    "V0 loose (all matches)":          AMDConfig(),
    "V1 +major-OB anchor":             AMDConfig(require_htf_ob_anchor=True),
    "V2 +tight accumulation (≤3ATR)":  AMDConfig(require_htf_ob_anchor=True, accum_max_width_atr=3.0),
    "V3 +real sweep (≥0.25ATR)":       AMDConfig(require_htf_ob_anchor=True, accum_max_width_atr=3.0, sweep_min_atr=0.25),
    "V4 +killzone only":               AMDConfig(require_htf_ob_anchor=True, accum_max_width_atr=3.0, sweep_min_atr=0.25, use_killzone=True),
}

risk = RiskConfig()
def bt(htf_, ltf_, cfg):
    ev = lambda h, l, now_ts: amd_eval(h, l, cfg=cfg, now_ts=now_ts)
    return run(htf_, ltf_, "XAUUSD", SequentialConfig(), risk, 133.42, 0.30, 0.10, 7.0, 6, 4,
               evaluator=ev, ltf_window=800, htf_window=300, stride=3, partials=True)

print(f"\n{'variant':32} {'IN-SAMPLE':>28} | {'OUT-OF-SAMPLE':>28}")
print(f"{'':32} {'T':>5}{'WR%':>7}{'PF':>7}{'ret%':>8} | {'T':>5}{'WR%':>7}{'PF':>7}{'ret%':>8}")
print("-" * 96)
for name, cfg in VARIANTS.items():
    ri = bt(htf_is, ltf_is, cfg)
    ro = bt(htf_oos, ltf_oos, cfg)
    def pf(r): return r['profit_factor'] if isinstance(r['profit_factor'], (int, float)) else 99.9
    print(f"{name:32} {ri['trades']:>5}{ri['win_rate']:>7.1f}{pf(ri):>7.2f}{ri['return_pct']:>8.1f} | "
          f"{ro['trades']:>5}{ro['win_rate']:>7.1f}{pf(ro):>7.2f}{ro['return_pct']:>8.1f}", flush=True)
print("-" * 96)
print("Credible only if OOS PF > 1.0 with a sane OOS trade count (overfit dies here).")
