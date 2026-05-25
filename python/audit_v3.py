import sys
sys.path.insert(0, '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python')

from forward_sim_liquidity_tp_v3 import run_single, fetch_h1_bars, resample_bars, calc_atr

h1 = fetch_h1_bars('XAUUSD', '1y')
h4 = resample_bars(h1, 4)
atr_norm = max(calc_atr(h1[:50]), 0.5)

for seed in [0, 25, 50, 75, 99]:
    r = run_single(seed, h1, h4, atr_norm, risk_per_trade=50, max_hold_bars=10)
    reasons = {}
    types = {}
    bias = {}
    for t in r.trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        types[t.manipulation_type] = types.get(t.manipulation_type, 0) + 1
        bias[t.h4_bias] = bias.get(t.h4_bias, 0) + 1
    print(f'seed {seed}: {r.total_trades} trades | ${r.total_return_pct:+.1f} | WR {r.win_rate:.0f}% | DD {r.max_drawdown_pct:.1f}%')
    for reas, cnt in sorted(reasons.items(), key=lambda xx: -xx[1]):
        print(f'  {reas}: {cnt}')
    print(f'  By type: {types}')
    print(f'  By H4 bias: {bias}')
    print()
