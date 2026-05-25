#!/usr/bin/env python3
"""Debug the multi-TF selector on real data to find the blockage."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from forward_sim_multi_tf import fetch_h1_bars, resample_bars, generate_m15_from_h1, calc_atr
from multi_tf_selector import select_trade_multi_tf, compute_h4_bias
from smc_engine import Bar

h1 = fetch_h1_bars("XAUUSD", "1y")
h4 = resample_bars(h1, 4)
m15 = generate_m15_from_h1(h1, seed=12345)

print(f"Data: {len(h1)} H1, {len(h4)} H4, {len(m15)} M15")

# Pick a bar in the middle where there should be structure
h1_idx = 1000
h4_ctx = h4[max(0, (h1_idx // 4) - 20):(h1_idx // 4)]
h1_ctx = h1[max(0, h1_idx - 50):h1_idx]
m15_end = h1_idx * 4
m15_start = max(0, m15_end - 40)
m15_ctx = m15[m15_start:m15_end]

print(f"\nContext at H1 bar {h1_idx} (price={h1[h1_idx].close:.2f}):")
print(f"  H4 ctx: {len(h4_ctx)} bars")
print(f"  H1 ctx: {len(h1_ctx)} bars")
print(f"  M15 ctx: {len(m15_ctx)} bars")

# Test H4 bias alone
bias = compute_h4_bias(h4_ctx)
print(f"\nH4 Bias: {bias.direction} (score={bias.score:.2f})")
for r in bias.reasons:
    print(f"  · {r}")

# Test full selector
sel = select_trade_multi_tf(h4_ctx, h1_ctx, m15_ctx, current_price=h1[h1_idx].close,
                            amd_phase="DISTRIBUTION", pip_size=0.01)
print(f"\nSelector Result:")
print(f"  Direction: {sel.direction}")
print(f"  Entry: {sel.entry_price}")
print(f"  SL: {sel.sl}")
print(f"  TP: {sel.tp}")
print(f"  Confidence: {sel.confidence}")
print(f"  Confluences: {sel.confluence_count}/8")
print(f"  Actionable: {sel.is_actionable}")
print(f"\nConfluence Details:")
for d in sel.confluence_details:
    print(f"  · {d}")
print(f"\nReasons:")
for r in sel.reasons:
    print(f"  · {r}")

# Now scan multiple bars to see how many signals we get
# Now scan multiple bars to see how many signals we get
print(f"\n=== SCANNING 200 H1 BARS FOR SIGNALS ===")

# First: test raw manipulation leg detection on a few bars
print(f"\n--- RAW MANIPULATION LEG TEST ---")
from manipulation_leg_detector import detect_manipulation_legs, Bar as MLDBar

# Test on a larger chunk of H1 bars (250 bars)
test_h1 = h1[500:750]
mld_test = [MLDBar(time=b.time, o=b.open, h=b.high, l=b.low, c=b.close) for b in test_h1]
legs = detect_manipulation_legs(mld_test, pip_size=0.01, min_recent_bars=100)
print(f"Found {len(legs)} manipulation legs in 250-bar H1 chunk")
for leg in legs[:10]:
    print(f"  {leg.leg_type} → {leg.direction} | wick/body={leg.wick_body_ratio:.2f} | excess={leg.excess_pips:.2f} | kz={leg.kill_zone}")

# Also test on the FULL dataset
print(f"\n--- FULL DATASET MANIPULATION LEG TEST ---")
mld_full = [MLDBar(time=b.time, o=b.open, h=b.high, l=b.low, c=b.close) for b in h1]
legs_full = detect_manipulation_legs(mld_full, pip_size=0.01, min_recent_bars=200)
print(f"Found {len(legs_full)} manipulation legs across entire {len(h1)}-bar dataset")
for leg in legs_full[:20]:
    print(f"  {leg.leg_type} → {leg.direction} | wick/body={leg.wick_body_ratio:.2f} | excess={leg.excess_pips:.2f} | kz={leg.kill_zone} | idx={leg.start_idx}")

signals = 0
actionable = 0
for h1_idx in range(100, 1200, 4):
    h4_ctx = h4[max(0, (h1_idx // 4) - 20):(h1_idx // 4)]
    h1_ctx = h1[max(0, h1_idx - 50):h1_idx]
    m15_end = h1_idx * 4
    m15_start = max(0, m15_end - 40)
    m15_ctx = m15[m15_start:m15_end]
    
    if len(h4_ctx) < 6 or len(h1_ctx) < 20:
        continue
    
    try:
        sel = select_trade_multi_tf(h4_ctx, h1_ctx, m15_ctx, current_price=h1[h1_idx].close,
                                    amd_phase="DISTRIBUTION", pip_size=0.01)
    except Exception as e:
        continue
    
    if sel.direction != "NEUTRAL":
        signals += 1
        if sel.is_actionable:
            actionable += 1

print(f"\nDirectional signals: {signals}/300 bars")
print(f"Actionable signals: {actionable}/300 bars")
print(f"Signal rate: {signals/300*100:.1f}% | Actionable rate: {actionable/300*100:.1f}%")
