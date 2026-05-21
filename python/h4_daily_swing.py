#!/usr/bin/env python3
"""
h4_daily_swing.py — H4/D1 Order Block Swing Trading with Scaling

Strategy:
  1. Find order blocks on H4 and D1 candles
  2. Place limit orders at OB retest (50-70% of OB range)
  3. Wide stops beyond OB extreme (swing holds, not scalps)
  4. Scale in on H1/M15 confirmation after initial entry
  5. Trail stop on H1 structure breaks
  6. Target 3-5R minimum (swing moves)

Uses real MT5 multi-timeframe data:
  - D1: 60 bars (3 months) for major OBs
  - H4: 100 bars (25 days) for intermediate OBs
  - H1: 300 bars for entry timing and scaling
  - M15: 600 bars for micro confirmation
"""
import json, math, os, sys, re
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from ict_precision import Bar

COMMISSION = 7.0

def load_mt5():
    path = '/Users/yuhfriendchris/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json'
    with open(path, 'r') as f:
        raw = f.read()
    raw = re.sub(r',\s*([\]\}])', r'\1', raw)
    data = json.loads(raw)
    charts = data.get("charts", {})
    result = {}
    for sym in charts:
        result[sym] = {}
        for tf in charts[sym]:
            bars = charts[sym][tf]
            if not isinstance(bars, list) or not bars or not isinstance(bars[0], dict):
                continue
            result[sym][tf] = [Bar(time=b["t"], o=b["o"], h=b["h"], l=b["l"], c=b["c"], v=b.get("v", 0)) for b in bars]
    return result

def get_risk(e):
    if e >= 1000: return 25.0
    elif e >= 500: return 10.0
    elif e >= 200: return 5.0
    elif e >= 100: return 2.0
    return 1.0

def calc_lot(sym, risk_usd, sl_dist):
    if sl_dist <= 0: return 0.01
    pip_val = 0.01 if sym in ["XAUUSD","XAGUSD"] else 0.0001
    pip_size = 0.01 if sym in ["XAUUSD","XAGUSD"] else 0.0001
    pips = sl_dist / pip_size
    lot = risk_usd / (pips * pip_val)
    return max(0.01, min(lot, 1.0))

# ── Find Order Blocks ────────────────────────────────────────────
def find_order_blocks(bars: List[Bar], min_body_pct: float = 0.3) -> List[Dict]:
    """Find bullish and bearish order blocks."""
    obs = []
    
    for i in range(2, len(bars)):
        b0, b1, b2 = bars[i-2], bars[i-1], bars[i]
        
        # Bullish OB: b0 down, b1 strong up (imbalance), b2 confirms
        b1_body = abs(b1.c - b1.o)
        b1_range = b1.h - b1.l
        
        if b0.c < b0.o and b1.c > b1.o and b1_body >= b1_range * min_body_pct:
            # Strong bullish candle after bearish
            if b2.c > b2.o or b2.c > b1.c * 0.5:
                obs.append({
                    "type": "BULL",
                    "top": b1.h,
                    "bottom": b1.l,
                    "idx": i,
                    "time": b1.time,
                    "strength": b1_body / b1_range if b1_range > 0 else 0,
                })
        
        # Bearish OB
        if b0.c > b0.o and b1.c < b1.o and b1_body >= b1_range * min_body_pct:
            if b2.c < b2.o or b2.c < b1.c * 1.5:
                obs.append({
                    "type": "BEAR",
                    "top": b1.h,
                    "bottom": b1.l,
                    "idx": i,
                    "time": b1.time,
                    "strength": b1_body / b1_range if b1_range > 0 else 0,
                })
    
    return obs

def find_d1_obs(bars: List[Bar]) -> List[Dict]:
    """Find D1 order blocks — major swing points."""
    obs = []
    
    for i in range(3, len(bars)):
        # Look for strong momentum candle after consolidation
        recent = bars[max(0, i-5):i]
        avg_range = sum(b.h - b.l for b in recent) / len(recent)
        
        b = bars[i-1]  # The OB candle
        b_range = b.h - b.l
        b_body = abs(b.c - b.o)
        
        if b_range <= 0: continue
        
        body_pct = b_body / b_range
        
        # Strong bullish candle
        if b.c > b.o and body_pct >= 0.6 and b_range > avg_range * 1.2:
            # Previous candle was bearish or neutral
            prev = bars[i-2]
            if prev.c <= prev.o or prev.c < prev.o * 1.001:
                obs.append({
                    "type": "BULL",
                    "top": b.h,
                    "bottom": b.l,
                    "idx": i-1,
                    "time": b.time,
                    "strength": body_pct,
                    "size": b_range,
                })
        
        # Strong bearish candle
        if b.c < b.o and body_pct >= 0.6 and b_range > avg_range * 1.2:
            prev = bars[i-2]
            if prev.c >= prev.o or prev.c > prev.o * 0.999:
                obs.append({
                    "type": "BEAR",
                    "top": b.h,
                    "bottom": b.l,
                    "idx": i-1,
                    "time": b.time,
                    "strength": body_pct,
                    "size": b_range,
                })
    
    return obs

# ── Find H1 scaling opportunities ────────────────────────────────
def find_scale_entries(h1_bars: List[Bar], direction: str, entry_zone: Tuple[float, float],
                       start_idx: int, max_bars: int = 20) -> List[Dict]:
    """Find lower timeframe entries to scale into winning position."""
    scales = []
    
    for i in range(start_idx, min(start_idx + max_bars, len(h1_bars))):
        b = h1_bars[i]
        
        if direction == "BUY":
            # Price pulls back into entry zone
            if entry_zone[0] <= b.l <= b.h <= entry_zone[1]:
                # Look for bullish confirmation
                if b.c > b.o:
                    scales.append({
                        "idx": i,
                        "time": b.time,
                        "entry": (b.l + b.o) / 2,
                        "dir": "BUY",
                    })
        else:
            if entry_zone[0] <= b.l <= b.h <= entry_zone[1]:
                if b.c < b.o:
                    scales.append({
                        "idx": i,
                        "time": b.time,
                        "entry": (b.h + b.o) / 2,
                        "dir": "SELL",
                    })
    
    return scales

# ── Main Swing Simulator ────────────────────────────────────────
def simulate_swing(data: Dict, symbol: str, base: float = 1000.0) -> Dict:
    """
    H4/D1 swing trading simulation.
    
    Entry: Limit order at OB 50-70% retest
    Stop: Beyond OB extreme
    Target: 3-5R
    Scale: Add on H1 confirmation within entry zone
    """
    d1 = data.get("D1", [])
    h4 = data.get("H4", [])
    h1 = data.get("H1", [])
    
    if len(d1) < 10 or len(h4) < 10:
        return {"trades": 0}
    
    equity = base
    peak = base
    max_dd = 0.0
    trades = []
    active_positions = []  # Can hold multiple scaled positions
    
    # Find D1 OBs
    d1_obs = find_d1_obs(d1)
    # Find H4 OBs
    h4_obs = find_order_blocks(h4)
    
    print(f"  {symbol}: Found {len(d1_obs)} D1 OBs, {len(h4_obs)} H4 OBs")
    
    # Map H1 bars to H4 and D1 indices
    def h4_idx_for_h1(h1_bar):
        """Find which H4 bar contains this H1 bar."""
        for j, h4b in enumerate(h4):
            if h1_bar.time >= h4b.time:
                # Check if within next H4 bar
                if j + 1 < len(h4):
                    if h1_bar.time < h4[j+1].time:
                        return j
                else:
                    return j
        return None
    
    def d1_idx_for_h1(h1_bar):
        """Find which D1 bar contains this H1 bar."""
        for j, d1b in enumerate(d1):
            if h1_bar.time >= d1b.time:
                if j + 1 < len(d1):
                    if h1_bar.time < d1[j+1].time:
                        return j
                else:
                    return j
        return None
    
    # Process H1 bar by bar
    for i in range(1, len(h1)):
        curr_h1 = h1[i]
        
        # Map to higher timeframes
        h4_i = h4_idx_for_h1(curr_h1)
        d1_i = d1_idx_for_h1(curr_h1)
        
        # ── Manage existing positions ────────────────────────────
        still_open = []
        for pos in active_positions:
            if pos.get("closed"):
                continue
            
            # Check SL
            if pos["dir"] == "BUY":
                if curr_h1.l <= pos["sl"]:
                    pos["closed"] = True
                    pos["exit"] = pos["sl"]
                    pos["exit_time"] = curr_h1.time
                    pos["pnl"] = -pos["risk_usd"] - COMMISSION * pos["lot"]
                    equity += pos["pnl"]
                    trades.append(pos)
                    continue
                # Check TP
                if curr_h1.h >= pos["tp"]:
                    pos["closed"] = True
                    pos["exit"] = pos["tp"]
                    pos["exit_time"] = curr_h1.time
                    rr = pos["tp_rr"]
                    pos["pnl"] = pos["risk_usd"] * rr - COMMISSION * pos["lot"]
                    equity += pos["pnl"]
                    trades.append(pos)
                    continue
            else:
                if curr_h1.h >= pos["sl"]:
                    pos["closed"] = True
                    pos["exit"] = pos["sl"]
                    pos["exit_time"] = curr_h1.time
                    pos["pnl"] = -pos["risk_usd"] - COMMISSION * pos["lot"]
                    equity += pos["pnl"]
                    trades.append(pos)
                    continue
                if curr_h1.l <= pos["tp"]:
                    pos["closed"] = True
                    pos["exit"] = pos["tp"]
                    pos["exit_time"] = curr_h1.time
                    rr = pos["tp_rr"]
                    pos["pnl"] = pos["risk_usd"] * rr - COMMISSION * pos["lot"]
                    equity += pos["pnl"]
                    trades.append(pos)
                    continue
            
            # Trailing stop: if up 2R, trail at breakeven
            if pos.get("trailing"):
                if pos["dir"] == "BUY":
                    # Trail below recent H4 low
                    if h4_i is not None and h4_i >= 2:
                        trail_level = min(h4[h4_i-1].l, h4[h4_i-2].l)
                        if trail_level > pos["entry"]:
                            pos["sl"] = max(pos["sl"], trail_level)
                else:
                    if h4_i is not None and h4_i >= 2:
                        trail_level = max(h4[h4_i-1].h, h4[h4_i-2].h)
                        if trail_level < pos["entry"]:
                            pos["sl"] = min(pos["sl"], trail_level)
            
            still_open.append(pos)
        
        active_positions = still_open
        
        # Update peak/DD
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
        
        # ── Look for new entries ──────────────────────────────────
        # Only enter if no position in same direction on this OB
        existing_dirs = [p["dir"] for p in active_positions if not p.get("closed")]
        
        # Check D1 OB retests
        if d1_i is not None:
            for ob in d1_obs:
                if ob["idx"] >= d1_i: continue  # OB hasn't formed yet
                if ob["idx"] < d1_i - 5: continue  # OB too old (5+ days)
                
                ob_age = d1_i - ob["idx"]
                
                if ob["type"] == "BULL" and "BUY" not in existing_dirs:
                    # Retest zone: bottom to 70% of OB
                    retest_top = ob["bottom"] + (ob["top"] - ob["bottom"]) * 0.7
                    
                    if curr_h1.l <= retest_top and curr_h1.h >= ob["bottom"]:
                        entry = (ob["bottom"] + retest_top) / 2
                        sl = ob["bottom"] - ob["size"] * 0.5  # Wide stop below OB
                        tp_rr = 4.0  # Swing target
                        risk_r = entry - sl
                        if risk_r <= 0: continue
                        tp = entry + risk_r * tp_rr
                        
                        risk_usd = get_risk(equity)
                        lot = calc_lot(symbol, risk_usd, risk_r)
                        
                        pos = {
                            "dir": "BUY",
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "tp_rr": tp_rr,
                            "risk_usd": risk_usd,
                            "lot": lot,
                            "entry_time": curr_h1.time,
                            "ob_type": "D1",
                            "ob_age": ob_age,
                            "trailing": False,
                        }
                        active_positions.append(pos)
                        break
                
                if ob["type"] == "BEAR" and "SELL" not in existing_dirs:
                    retest_bottom = ob["top"] - (ob["top"] - ob["bottom"]) * 0.7
                    
                    if curr_h1.h >= retest_bottom and curr_h1.l <= ob["top"]:
                        entry = (ob["top"] + retest_bottom) / 2
                        sl = ob["top"] + ob["size"] * 0.5
                        tp_rr = 4.0
                        risk_r = sl - entry
                        if risk_r <= 0: continue
                        tp = entry - risk_r * tp_rr
                        
                        risk_usd = get_risk(equity)
                        lot = calc_lot(symbol, risk_usd, risk_r)
                        
                        pos = {
                            "dir": "SELL",
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "tp_rr": tp_rr,
                            "risk_usd": risk_usd,
                            "lot": lot,
                            "entry_time": curr_h1.time,
                            "ob_type": "D1",
                            "ob_age": ob_age,
                            "trailing": False,
                        }
                        active_positions.append(pos)
                        break
        
        # Check H4 OB retests (secondary, only if no D1 position)
        if h4_i is not None and len(active_positions) < 2:
            for ob in h4_obs:
                if ob["idx"] >= h4_i: continue
                if ob["idx"] < h4_i - 8: continue  # OB too old (2+ days)
                
                ob_age = h4_i - ob["idx"]
                
                if ob["type"] == "BULL" and "BUY" not in existing_dirs:
                    retest_top = ob["bottom"] + (ob["top"] - ob["bottom"]) * 0.7
                    
                    if curr_h1.l <= retest_top and curr_h1.h >= ob["bottom"]:
                        entry = (ob["bottom"] + retest_top) / 2
                        sl = ob["bottom"] - (ob["top"] - ob["bottom"]) * 0.3
                        tp_rr = 3.0
                        risk_r = entry - sl
                        if risk_r <= 0: continue
                        tp = entry + risk_r * tp_rr
                        
                        risk_usd = get_risk(equity) * 0.7  # Smaller on H4
                        lot = calc_lot(symbol, risk_usd, risk_r)
                        
                        pos = {
                            "dir": "BUY",
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "tp_rr": tp_rr,
                            "risk_usd": risk_usd,
                            "lot": lot,
                            "entry_time": curr_h1.time,
                            "ob_type": "H4",
                            "ob_age": ob_age,
                            "trailing": False,
                        }
                        active_positions.append(pos)
                        break
                
                if ob["type"] == "BEAR" and "SELL" not in existing_dirs:
                    retest_bottom = ob["top"] - (ob["top"] - ob["bottom"]) * 0.7
                    
                    if curr_h1.h >= retest_bottom and curr_h1.l <= ob["top"]:
                        entry = (ob["top"] + retest_bottom) / 2
                        sl = ob["top"] + (ob["top"] - ob["bottom"]) * 0.3
                        tp_rr = 3.0
                        risk_r = sl - entry
                        if risk_r <= 0: continue
                        tp = entry - risk_r * tp_rr
                        
                        risk_usd = get_risk(equity) * 0.7
                        lot = calc_lot(symbol, risk_usd, risk_r)
                        
                        pos = {
                            "dir": "SELL",
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "tp_rr": tp_rr,
                            "risk_usd": risk_usd,
                            "lot": lot,
                            "entry_time": curr_h1.time,
                            "ob_type": "H4",
                            "ob_age": ob_age,
                            "trailing": False,
                        }
                        active_positions.append(pos)
                        break
        
        # ── Scaling ────────────────────────────────────────────────
        # If position is in profit 1R+, look to scale on H1 pullback
        for pos in active_positions:
            if pos.get("closed"): continue
            if pos.get("scaled"): continue
            
            if pos["dir"] == "BUY":
                unrealized = curr_h1.c - pos["entry"]
                risk_r = pos["entry"] - pos["sl"]
                if risk_r > 0 and unrealized >= risk_r * 1.0:
                    # In profit 1R+, look for pullback into 50% zone
                    mid = pos["entry"] - risk_r * 0.5
                    if curr_h1.l <= mid <= curr_h1.h:
                        # Scale in
                        scale_risk = pos["risk_usd"] * 0.5
                        scale_lot = calc_lot(symbol, scale_risk, risk_r)
                        
                        scale_pos = {
                            "dir": "BUY",
                            "entry": mid,
                            "sl": pos["sl"],
                            "tp": pos["tp"],
                            "tp_rr": pos["tp_rr"],
                            "risk_usd": scale_risk,
                            "lot": scale_lot,
                            "entry_time": curr_h1.time,
                            "ob_type": pos["ob_type"] + "_SCALE",
                            "scaled": False,
                            "trailing": True,
                        }
                        active_positions.append(scale_pos)
                        pos["scaled"] = True
            else:
                unrealized = pos["entry"] - curr_h1.c
                risk_r = pos["sl"] - pos["entry"]
                if risk_r > 0 and unrealized >= risk_r * 1.0:
                    mid = pos["entry"] + risk_r * 0.5
                    if curr_h1.l <= mid <= curr_h1.h:
                        scale_risk = pos["risk_usd"] * 0.5
                        scale_lot = calc_lot(symbol, scale_risk, risk_r)
                        
                        scale_pos = {
                            "dir": "SELL",
                            "entry": mid,
                            "sl": pos["sl"],
                            "tp": pos["tp"],
                            "tp_rr": pos["tp_rr"],
                            "risk_usd": scale_risk,
                            "lot": scale_lot,
                            "entry_time": curr_h1.time,
                            "ob_type": pos["ob_type"] + "_SCALE",
                            "scaled": False,
                            "trailing": True,
                        }
                        active_positions.append(scale_pos)
                        pos["scaled"] = True
    
    # Close any remaining positions at last price
    for pos in active_positions:
        if pos.get("closed"):
            continue
        pos["closed"] = True
        pos["exit_time"] = h1[-1].time
        pos["exit"] = h1[-1].c
        
        if pos["dir"] == "BUY":
            gain = pos["exit"] - pos["entry"]
        else:
            gain = pos["entry"] - pos["exit"]
        
        risk_r = abs(pos["entry"] - pos["sl"])
        r_mult = gain / risk_r if risk_r > 0 else 0
        pos["pnl"] = pos["risk_usd"] * r_mult - COMMISSION * pos["lot"]
        equity += pos["pnl"]
        trades.append(pos)
    
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": sum(t["pnl"] for t in trades),
        "equity": equity,
        "peak": peak,
        "max_dd": max_dd,
        "return_pct": (equity - base) / base * 100,
        "d1_obs": len(d1_obs),
        "h4_obs": len(h4_obs),
        "detail": trades,
    }

# ── Main ──────────────────────────────────────────────────────────
def run():
    print("=" * 100)
    print(" H4/D1 SWING TRADING — Order Block Retest with Scaling")
    print(" Multi-timeframe: D1 OBs → H4 OBs → H1 entries → H1 scaling")
    print(" Real MT5 data only")
    print("=" * 100)
    print()
    
    data = load_mt5()
    
    for sym in ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]:
        if sym not in data or "D1" not in data[sym]:
            continue
        
        sym_data = data[sym]
        
        print(f"\n{'='*80}")
        print(f" {sym}")
        print(f"  D1: {len(sym_data.get('D1', []))} bars | H4: {len(sym_data.get('H4', []))} | H1: {len(sym_data.get('H1', []))}")
        print(f"{'='*80}")
        
        r = simulate_swing(sym_data, sym, base=1000.0)
        
        if r["trades"] == 0:
            print("  No trades")
            continue
        
        print(f"\n  D1 OBs: {r['d1_obs']} | H4 OBs: {r['h4_obs']}")
        print(f"  Trades: {r['trades']} (includes scaled entries)")
        print(f"  Wins: {r['wins']} | Losses: {r['losses']}")
        print(f"  Win Rate: {r['wr']:.1f}%")
        print(f"  P&L: ${r['pnl']:.2f}")
        print(f"  Equity: ${r['equity']:.2f} (peak: ${r['peak']:.2f})")
        print(f"  Return: {r['return_pct']:.1f}%")
        print(f"  Max DD: {r['max_dd']:.1f}%")
        
        # By OB type
        d1_trades = [t for t in r["detail"] if "D1" in t.get("ob_type", "") and "SCALE" not in t.get("ob_type", "")]
        h4_trades = [t for t in r["detail"] if "H4" in t.get("ob_type", "") and "SCALE" not in t.get("ob_type", "")]
        scale_trades = [t for t in r["detail"] if "SCALE" in t.get("ob_type", "")]
        
        if d1_trades:
            d1_wins = [t for t in d1_trades if t["pnl"] > 0]
            print(f"\n  D1 Entries: {len(d1_trades)} trades, {len(d1_wins)/len(d1_trades)*100:.1f}% WR, ${sum(t['pnl'] for t in d1_trades):.2f}")
        
        if h4_trades:
            h4_wins = [t for t in h4_trades if t["pnl"] > 0]
            print(f"  H4 Entries: {len(h4_trades)} trades, {len(h4_wins)/len(h4_trades)*100:.1f}% WR, ${sum(t['pnl'] for t in h4_trades):.2f}")
        
        if scale_trades:
            scale_wins = [t for t in scale_trades if t["pnl"] > 0]
            print(f"  Scaled Entries: {len(scale_trades)} trades, {len(scale_wins)/len(scale_trades)*100:.1f}% WR, ${sum(t['pnl'] for t in scale_trades):.2f}")
        
        out = os.path.join(os.path.dirname(__file__), f'swing_{sym}.json')
        with open(out, 'w') as f:
            json.dump(r, f, indent=2)
    
    print(f"\n{'='*80}")
    print(" COMPLETE")
    print(f"{'='*80}")

if __name__ == "__main__":
    run()
