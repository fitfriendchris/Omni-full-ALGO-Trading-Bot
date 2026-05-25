#!/usr/bin/env python3
"""
forward_sim_liquidity_tp_v4.py — Look-ahead BIAS FIXED

TP is set ONLY from historical data (bars before entry):
  1. Last formed EQH/EQL in the H1 context
  2. Nearest H4 swing high/low from known H4 bars
  3. Fallback: fixed 2.5R target

Exits: SL (leg extreme), TP (historical liquidity only), TIMEOUT
Spacing: trade once per H4 bar (every 3 bars minimum)
"""
import csv, json, math, random, sys, os, time
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

sys.path.insert(0, os.path.dirname(__file__))
from smc_engine import Bar
from multi_tf_selector import select_trade_multi_tf
from forward_sim_multi_tf import fetch_h1_bars, resample_bars, generate_m15_from_h1, calc_atr, SimConfig


@dataclass
class SimTrade:
    entry_time: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    tp_label: str = ""
    pnl_net: float = 0.0
    pnl_gross: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    confidence: float = 0.0
    confluence_count: int = 0
    manipulation_type: str = ""
    h4_bias: str = ""


@dataclass
class SimResult:
    seed: int = 0
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    total_trades: int = 0
    wins: int = 0; losses: int = 0
    win_rate: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    trades: List[SimTrade] = field(default_factory=list)


# ── Historical Liquidity Scanner (NO LOOK-AHEAD) ────────────────────────────

def find_historical_liquidity(h1_bars, entry_idx, direction, atr):
    """
    Find liquidity target using ONLY bars before or at entry_idx.
    NO look-ahead — this is the key fix.
    """
    # We can look at the full H1 array, but we only form clusters from bars <= entry_idx
    # This simulates knowing all history up to the entry (real-world valid for a backtest)
    known_bars = h1_bars[:entry_idx+1]
    if len(known_bars) < 20:
        # Fallback to fixed R:R
        entry = h1_bars[entry_idx].close
        sl_dist = atr * 2.5
        if direction == "BULL":
            return entry + sl_dist, "FIXED_2.5R_HIST"
        return entry - sl_dist, "FIXED_2.5R_HIST"

    if direction == "BULL":
        # Look at last 50 known highs for EQH cluster
        idx_start = max(0, len(known_bars) - 50)
        highs = [(j, known_bars[j].high) for j in range(idx_start, len(known_bars))]
        # Find most recent cluster of equal highs (within 0.5 ATR)
        for i, hi in reversed(highs):
            cluster = [h for j, h in highs if abs(h - hi) <= atr * 0.5 and j != i]
            if len(cluster) >= 1:
                return max(hi, max(cluster)), "EQH_HIST"
        # Last known swing high
        for i in reversed(range(2, len(known_bars)-2)):
            b = known_bars[i]
            if b.high > known_bars[i-2].high and b.high > known_bars[i-1].high and \
               b.high > known_bars[i+1].high and b.high > known_bars[i+2].high:
                return b.high, "SWING_HIGH_HIST"
        # Fallback
        return known_bars[-1].high + atr * 2, "FIXED_2R_ABOVE"
    else:
        lows = [(j, known_bars[j].low) for j in range(max(0, len(known_bars)-50), len(known_bars))]
        for i, lo in reversed(lows):
            cluster = [l for j, l in lows if abs(l - lo) <= atr * 0.5 and j != i]
            if len(cluster) >= 1:
                return min(lo, min(cluster)), "EQL_HIST"
        for i in reversed(range(2, len(known_bars)-2)):
            b = known_bars[i]
            if b.low < known_bars[i-2].low and b.low < known_bars[i-1].low and \
               b.low < known_bars[i+1].low and b.low < known_bars[i+2].low:
                return b.low, "SWING_LOW_HIST"
        return known_bars[-1].low - atr * 2, "FIXED_2R_BELOW"


# ── Trade Execution ──────────────────────────────────────────────────────────

def execute_trade(sel, h1_bars, entry_idx, atr, risk_per_trade=50, max_hold_bars=10, comm=0.01):
    t = SimTrade()
    t.entry_time = str(h1_bars[entry_idx].time)
    t.direction = sel.direction
    t.entry_price = sel.entry_price
    t.sl = sel.sl
    t.confidence = sel.confidence
    t.confluence_count = sel.confluence_count
    t.manipulation_type = sel.manipulation_leg.leg_type if sel.manipulation_leg else ""
    t.h4_bias = sel.h4_bias.direction if sel.h4_bias else "NEUTRAL"

    # HISTORICAL TP ONLY — no look-ahead
    tp, label = find_historical_liquidity(h1_bars, entry_idx, sel.direction, atr)
    t.tp = tp
    t.tp_label = label

    # Fixed lot: $50 risk per trade
    sl_dist = max(abs(sel.sl - sel.entry_price), atr * 1.5)
    units = risk_per_trade / sl_dist
    units = min(units, 5000)  # notional cap

    entry = sel.entry_price; sl = sel.sl;

    for i in range(entry_idx + 1, min(entry_idx + max_hold_bars + 1, len(h1_bars))):
        b = h1_bars[i]
        if sel.direction == "BULL":
            if b.low <= sl:
                t.exit_price = sl
                t.pnl_gross = -(sl - entry) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"SL ({label})"; t.bars_held = i - entry_idx; return t
            if b.high >= tp:
                t.exit_price = tp
                t.pnl_gross = (tp - entry) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"TP ({label})"; t.bars_held = i - entry_idx; return t
        else:
            if b.high >= sl:
                t.exit_price = sl
                t.pnl_gross = -(entry - sl) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"SL ({label})"; t.bars_held = i - entry_idx; return t
            if b.low <= tp:
                t.exit_price = tp
                t.pnl_gross = (entry - tp) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"TP ({label})"; t.bars_held = i - entry_idx; return t

    last = h1_bars[min(entry_idx + max_hold_bars, len(h1_bars) - 1)]
    t.exit_price = last.close
    t.pnl_gross = (last.close - entry) * units if sel.direction == "BULL" else (entry - last.close) * units
    t.exit_reason = f"TIMEOUT ({label})"; t.bars_held = max_hold_bars; t.pnl_net = t.pnl_gross - comm
    return t


# ── Single Run ───────────────────────────────────────────────────────────────

def run_single(seed, h1, h4, atr_norm, risk_per_trade=50, max_hold_bars=10):
    random.seed(seed)
    m15 = generate_m15_from_h1(h1, seed=seed)
    step = random.choice([24, 28, 32, 36, 40])  # coarser: every 1-2 days

    equity = 1000.0
    peak = equity
    trades = []
    cooldown = 0
    in_trade_end = 0

    for i in range(50, len(h1) - max_hold_bars, step):
        if i < in_trade_end or cooldown > 0:
            if cooldown > 0:
                cooldown -= 1
            continue

        h4_ctx = h4[max(0, (i // 4) - 20):(i // 4)]
        h1_ctx = h1[max(0, i - 50):i]
        m15_start = max(0, i * 4 - 40)
        m15_ctx = m15[m15_start:i*4]

        if len(h4_ctx) < 6 or len(h1_ctx) < 20:
            continue

        try:
            sel = select_trade_multi_tf(h4_ctx, h1_ctx, m15_ctx, current_price=h1[i].close, pip_size=0.01)
        except Exception:
            continue

        if not sel.is_actionable or sel.direction == "NEUTRAL":
            continue

        t = execute_trade(sel, h1, i, atr_norm, risk_per_trade, max_hold_bars)
        equity += t.pnl_net
        trades.append(t)
        in_trade_end = i + max_hold_bars + 1
        cooldown = 3
        if equity > peak:
            peak = equity

    wins = sum(1 for t in trades if t.pnl_net > 0)
    dd = 0.0
    eq_curve = [1000.0]
    for t in trades:
        eq_curve.append(eq_curve[-1] + t.pnl_net)
    peak = 1000.0
    for e in eq_curve:
        if e > peak:
            peak = e
        dd = max(dd, (peak - e) / peak * 100)

    gp = sum(t.pnl_gross for t in trades if t.pnl_gross > 0)
    gl = abs(sum(t.pnl_gross for t in trades if t.pnl_gross < 0))
    pf = gp / gl if gl > 0 else 999

    return SimResult(
        seed=seed, final_equity=equity,
        total_return_pct=(equity - 1000) / 1000 * 100,
        total_trades=len(trades), wins=wins, losses=len(trades)-wins,
        win_rate=(wins/len(trades)*100) if trades else 0,
        max_drawdown_pct=dd, profit_factor=pf, trades=trades,
    )


# ── Monte Carlo ──────────────────────────────────────────────────────────────

def run_monte(n_runs=100, max_hold_bars=10, risk_per_trade=50):
    print("[LIQ-TP v4] Loading data...")
    h1 = fetch_h1_bars("XAUUSD", "1y")
    h4 = resample_bars(h1, 4)
    atr_norm = max(calc_atr(h1[:50]), 0.5)
    print(f"Loaded: {len(h1)} H1 | {len(h4)} H4")
    print(f"[LIQ-TP v4] {n_runs} runs | max_hold={max_hold_bars}H1 | risk=${risk_per_trade}/trade")

    results = []; t0 = time.time()
    for s in range(n_runs):
        r = run_single(s, h1, h4, atr_norm, risk_per_trade, max_hold_bars)
        results.append(r)
        if (s + 1) % 20 == 0:
            print(f"  {s+1}/{n_runs} done ({time.time()-t0:.1f}s)")
    elapsed = time.time() - t0

    fname = "liq_tp_v4_runs.csv"
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run","seed","return_pct","win_rate","max_dd_pct","trades","pf"])
        for i, r in enumerate(results):
            w.writerow([i+1, r.seed, f"{r.total_return_pct:.2f}", f"{r.win_rate:.1f}",
                       f"{r.max_drawdown_pct:.2f}", r.total_trades, f"{r.profit_factor:.2f}"])

    def _stat(arr):
        s = sorted(arr); n = len(s)
        return {"mean": sum(s)/n, "med": s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2,
                "min": min(s), "max": max(s), "p5": s[max(0,int(n*0.05)-1)],
                "p95": s[min(n-1,int(n*0.95)-1)]}

    stats = {
        "n_runs": n_runs, "max_hold": max_hold_bars, "risk_per_trade": risk_per_trade,
        "profitable": sum(1 for r in results if r.total_return_pct > 0),
        "profitable_pct": sum(1 for r in results if r.total_return_pct > 0) / n_runs * 100,
        "return": _stat([r.total_return_pct for r in results]),
        "win_rate": _stat([r.win_rate for r in results]),
        "drawdown": _stat([r.max_drawdown_pct for r in results]),
        "pf": _stat([r.profit_factor for r in results]),
        "trades_per_run": _stat([r.total_trades for r in results]),
        "time_sec": elapsed,
    }

    with open("liq_tp_v4_summary.json", "w") as f:
        json.dump(stats, f, indent=2)

    print(f"\n{'='*50}")
    print(f"LIQUIDITY-TP v4 — HISTORICAL TP ONLY — FIXED LOT")
    print(f"{'='*50}")
    print(f"Profitable: {stats['profitable']}/{n_runs} ({stats['profitable_pct']:.1f}%)")
    print(f"Return:     mean={stats['return']['mean']:+.1f}%  med={stats['return']['med']:+.1f}%")
    print(f"Win Rate:   mean={stats['win_rate']['mean']:.1f}%  med={stats['win_rate']['med']:.1f}%")
    print(f"Drawdown:   mean={stats['drawdown']['mean']:.1f}%  max={stats['drawdown']['max']:.1f}%")
    print(f"PF:         mean={stats['pf']['mean']:.1f}  med={stats['pf']['med']:.1f}")
    print(f"Trades/run: mean={stats['trades_per_run']['mean']:.1f}")
    print(f"Time:       {elapsed:.1f}s")
    print(f"CSV:        {fname}")
    
    # audit exit reasons for top and bottom performers
    for label, arr in [("TOP", sorted(results, key=lambda r: -r.total_return_pct)[:5]),
                       ("BOTTOM", sorted(results, key=lambda r: r.total_return_pct)[:5])]:
        print(f"\n--- {label} 5 performers ---")
        for r in arr:
            reasons = {}
            for t in r.trades:
                reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
            print(f"  seed {r.seed}: ${r.total_return_pct:+.1f} | {r.total_trades} trades | {dict(reasons)}")
    return results, stats


if __name__ == "__main__":
    run_monte(n_runs=100, max_hold_bars=10, risk_per_trade=50)
