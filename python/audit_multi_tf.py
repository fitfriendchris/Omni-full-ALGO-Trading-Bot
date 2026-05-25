#!/usr/bin/env python3
"""Audit top multi-TF runs with full trade journals."""
import csv, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))

from forward_sim_multi_tf import (
    fetch_h1_bars, resample_bars, generate_m15_from_h1, calc_atr,
    run_single, SimConfig,
)

# Read top seeds from multi_tf_runs.csv
seeds = []
with open('multi_tf_runs.csv') as f:
    r = csv.DictReader(f)
    rows = sorted(r, key=lambda x: float(x['return_pct']), reverse=True)
    for row in rows[:5]:
        seeds.append(int(row['seed']))

print("=== AUDITING TOP 5 MULTI-TF SEEDS ===")
print(f"Seeds: {seeds}\n")

h1 = fetch_h1_bars("XAUUSD", "1y")
h4 = resample_bars(h1, 4)
m15 = generate_m15_from_h1(h1, seed=12345)
atr_norm = calc_atr(h1[:50]) if len(h1) >= 50 else 5.0
if atr_norm < 0.5: atr_norm = 0.5
cfg = SimConfig(symbol="XAUUSD", pip_size=0.01)

for seed in seeds:
    print(f"\n{'='*60}")
    print(f"SEED {seed}")
    print(f"{'='*60}")
    result = run_single(seed, h1, h4, m15, cfg, atr_norm)
    print(f"Return: {result.total_return_pct:+.1f}% | WR: {result.win_rate:.1f}% | DD: {result.max_drawdown_pct:.1f}% | PF: {result.profit_factor:.2f}")
    print(f"Trades: {result.total_trades} | Final Equity: ${result.final_equity:.2f}")
    
    if result.trades:
        print(f"\n--- TRADE JOURNAL ---")
        for i, t in enumerate(result.trades, 1):
            status = "WIN" if t.pnl_net > 0 else "LOSS"
            print(f"  {i}. {status} {t.direction:4s} | Entry {t.entry_price:.2f} → Exit {t.exit_price:.2f} | "
                  f"P/L ${t.pnl_net:+.2f} | Reason: {t.exit_reason} | "
                  f"Conf: {t.confidence:.2f} | Confl: {t.confluence_count} | Manip: {t.manipulation_type} | H4: {t.h4_bias}")
        
        # Confluence analysis
        print(f"\n--- CONFLUENCE BREAKDOWN ---")
        confl_counts = {}
        for t in result.trades:
            c = t.confluence_count
            confl_counts[c] = confl_counts.get(c, 0) + 1
        for c in sorted(confl_counts.keys()):
            wins = sum(1 for t in result.trades if t.confluence_count == c and t.pnl_net > 0)
            total = confl_counts[c]
            print(f"  {c} confluences: {wins}/{total} wins ({wins/total*100:.0f}% WR)")
        
        # Manipulation type analysis
        print(f"\n--- MANIPULATION TYPE BREAKDOWN ---")
        manip_counts = {}
        for t in result.trades:
            mt = t.manipulation_type or "UNKNOWN"
            if mt not in manip_counts:
                manip_counts[mt] = {"wins": 0, "total": 0}
            manip_counts[mt]["total"] += 1
            if t.pnl_net > 0:
                manip_counts[mt]["wins"] += 1
        for mt in sorted(manip_counts.keys()):
            d = manip_counts[mt]
            print(f"  {mt}: {d['wins']}/{d['total']} wins ({d['wins']/d['total']*100:.0f}% WR)")
        
        # H4 bias analysis
        print(f"\n--- H4 BIAS BREAKDOWN ---")
        bias_counts = {}
        for t in result.trades:
            b = t.h4_bias or "NEUTRAL"
            if b not in bias_counts:
                bias_counts[b] = {"wins": 0, "total": 0}
            bias_counts[b]["total"] += 1
            if t.pnl_net > 0:
                bias_counts[b]["wins"] += 1
        for b in sorted(bias_counts.keys()):
            d = bias_counts[b]
            print(f"  H4 {b}: {d['wins']}/{d['total']} wins ({d['wins']/d['total']*100:.0f}% WR)")

print(f"\n{'='*60}")
print("AUDIT COMPLETE")
print(f"{'='*60}")
