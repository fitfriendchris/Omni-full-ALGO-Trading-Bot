#!/usr/bin/env python3
"""Deep audit — end-to-end pipeline test"""
import json, sys, traceback
sys.path.insert(0, '/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python')

# ── Load MT5 data ─────────────────────────────────────────────────────────
from mt5_connector import _load as mt5_load
data = mt5_load()
charts = data.get('charts', {})
print("=" * 60)
print("1. MT5 DATA LOAD")
print("=" * 60)
print("Timestamp:", data.get('timestamp'))
print("Balance:", data.get('account', {}).get('balance'))
print("Symbols:", list(charts.keys()))

has_data = False
for sym, tfs in charts.items():
    for tf in ['H4','H1','M15','M1']:
        bars = tfs.get(tf, [])
        if bars:
            print(f"  {sym} {tf}: {len(bars)} bars, last={bars[-1].get('t', '?')}")
            has_data = True

if not has_data:
    print("ERROR: No bar data in any timeframe")
    sys.exit(1)

# ── Multi-TF selector ─────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("2. MULTI-TF SELECTOR")
print("=" * 60)

from multi_tf_selector import select_trade_multi_tf
from forward_sim_multi_tf import generate_m15_from_h1, calc_atr
from manipulation_leg_detector import detect_manipulation_legs

xau = charts.get('XAUUSD', {})
h1 = xau.get('H1', [])
h4 = xau.get('H4', [])

if not h1 or not h4:
    print("ERROR: No H1 or H4 data for XAUUSD")
else:
    atr = calc_atr(h1, 14)
    m15 = generate_m15_from_h1(h1, seed=42)
    legs = detect_manipulation_legs(h1, atr=atr)
    sel = select_trade_multi_tf(h4=h4, h1=h1, m15=m15, current_price=h1[-1]['c'])
    print(f"XAUUSD H1 bars={len(h1)} H4 bars={len(h4)}")
    print(f"ATR={atr:.2f}  Legs={len(legs)}  M15 generated={len(m15)}")
    print(f"Direction={sel.direction}  Actionable={sel.actionable}")
    print(f"Score={sel.score:.3f}  Entry={sel.entry}  SL={sel.sl}  TP={sel.tp}")
    print(f"Confluences={sel.confluences}")
    if sel.reasons:
        print("Reasons:")
        for r in sel.reasons:
            print(f"  · {r}")

# ── Swarm state ───────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("3. SWARM STATE")
print("=" * 60)

import swarm
try:
    state = swarm.get_state()
    print("Swarm state loaded")
    print("Active agents:", len(state.get('agents', [])))
except Exception as e:
    print("Swarm state error:", e)

# ── Server routes ────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("4. SERVER ROUTES")
print("=" * 60)

import server
for route in server.app.routes:
    methods = getattr(route, 'methods', set())
    if methods:
        print(f"  {route.path}  {methods}")

# ── Rules ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("5. RULES")
print("=" * 60)
with open('/Users/yuhfriendchris/Omni-full-ALGO-Trading-Bot/python/rules.json') as f:
    rules = json.load(f)
print(f"Rules: {len(rules.get('rules', []))} rules loaded")

print("\n" + "=" * 60)
print("DEEP AUDIT COMPLETE")
print("=" * 60)
