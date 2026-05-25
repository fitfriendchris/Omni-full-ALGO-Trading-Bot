import sys
sys.path.insert(0, '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python')
from forward_sim_liquidity_tp_v4 import run_single, fetch_h1_bars, resample_bars, calc_atr

h1 = fetch_h1_bars('XAUUSD', '1y')
h4 = resample_bars(h1, 4)
atr_norm = max(calc_atr(h1[:50]), 0.5)

r = run_single(0, h1, h4, atr_norm, risk_per_trade=50, max_hold_bars=10)

# Analyze TP distances
tp_dists = []
for t in r.trades:
    d = abs(t.tp - t.entry_price)
    tp_dists.append((t.direction, d, t.tp_label, t.exit_reason, t.pnl_net))

print(f'seed 0: {r.total_trades} trades')
print(f'\nTP distances (points for XAUUSD):')
for direction, d, label, reason, pnl in tp_dists:
    print(f'  {direction:4s} | TP={d:.2f}pts | {label:20s} | {reason:25s} | ${pnl:+.2f}')

avg_tp = sum(d for _, d, _, _, _ in tp_dists) / len(tp_dists)
avg_sl = sum(abs(t.sl - t.entry_price) for t in r.trades) / len(r.trades)
print(f'\nAvg TP dist: {avg_tp:.2f} pts | Avg SL dist: {avg_sl:.2f} pts | R:R = {avg_tp/avg_sl:.2f}:1')

# Count tiny TPs (< 2 ATR)
small_tps = sum(1 for _, d, _, _, _ in tp_dists if d < atr_norm * 2)
print(f'TPs < 2 ATR ({atr_norm*2:.1f} pts): {small_tps}/{len(tp_dists)}')
