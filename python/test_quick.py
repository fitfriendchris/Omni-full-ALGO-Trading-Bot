#!/usr/bin/env python3
import sys, time
sys.path.insert(0, '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python')

from forward_sim_liquidity_tp_v2 import run_single, fetch_h1_bars, resample_bars
from forward_sim_liquidity_tp_v2 import generate_m15_from_h1, calc_atr, SimConfig

h1 = fetch_h1_bars('XAUUSD', '1y')
h4 = resample_bars(h1, 4)
m15 = generate_m15_from_h1(h1, seed=12345)
atr = max(calc_atr(h1[:50]), 0.5)
cfg = SimConfig(symbol='XAUUSD', pip_size=0.01)

print(f'Loaded: {len(h1)} H1 | {len(h4)} H4 | {len(m15)} M15')
for s in range(3):
    t0 = time.time()
    r = run_single(s, h1, h4, m15, cfg, atr, 10)
    print(f'seed {s}: {time.time()-t0:.1f}s | {r.total_trades} trades | ${r.total_return_pct:+.1f} ({r.win_rate:.0f}% WR) | DD {r.max_drawdown_pct:.1f}%')
