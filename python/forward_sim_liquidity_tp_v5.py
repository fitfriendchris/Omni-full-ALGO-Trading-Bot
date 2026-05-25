#!/usr/bin/env python3
"""
forward_sim_liquidity_tp_v5.py — REALISTIC TP DISTANCES

Fixes from v4:
  1. TP minimum = 3x ATR (hard floor for R:R)
  2. Liquidity = H4 structure pools only (not 50-bar H1 noise)
  3. For BULL: TP = next H4 pool / EQH from full dataset history (not just 50 bars)
     For BEAR: TP = next H4 pool / EQL from full dataset history
  4. SL floor = 1.5x ATR (already there)
  5. Trade frequency: check every 48 H1 bars (2x/week)
"""
import csv, json, math, random, sys, os, time
from dataclasses import dataclass, field
from typing import List, Dict

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


# ── Pre-compute H4 structure levels once ──────────────────────────────────────

def precompute_h4_pools(h4_bars: List[Bar], atr: float):
    """Pre-compute all H4 swing highs/lows and EQH/EQL for fast lookup."""
    # Swing highs/lows
    swing_highs = []  # (idx, price)
    swing_lows = []
    for i in range(2, len(h4_bars) - 2):
        b = h4_bars[i]
        if b.high > h4_bars[i-1].high and b.high > h4_bars[i-2].high and b.high > h4_bars[i+1].high and b.high > h4_bars[i+2].high:
            swing_highs.append((i, b.high))
        if b.low < h4_bars[i-1].low and b.low < h4_bars[i-2].low and b.low < h4_bars[i+1].low and b.low < h4_bars[i+2].low:
            swing_lows.append((i, b.low))
    
    # EQH clusters from all H4 highs
    eqhs = {}  # price_key -> list of bar_idx
    for i, b in enumerate(h4_bars):
        key = round(b.high / (atr * 0.3))
        eqhs.setdefault(key, []).append((i, b.high))
    
    eqls = {}
    for i, b in enumerate(h4_bars):
        key = round(b.low / (atr * 0.3))
        eqls.setdefault(key, []).append((i, b.low))
    
    # Find actual EQH/EQL (clusters of 2+ touches)
    eqh_levels = []
    for key, touches in eqhs.items():
        if len(touches) >= 2:
            eqh_levels.append((touches[-1][0], max(t[1] for t in touches)))
    eqh_levels.sort(key=lambda x: x[0])
    
    eql_levels = []
    for key, touches in eqls.items():
        if len(touches) >= 2:
            eql_levels.append((touches[-1][0], min(t[1] for t in touches)))
    eql_levels.sort(key=lambda x: x[0])
    
    return {"swing_highs": swing_highs, "swing_lows": swing_lows,
            "eqh": eqh_levels, "eql": eql_levels}


def find_tp_from_pools(entry_idx_h1: int, h1_bars, h4_pools, direction: str, atr: float,
                       min_rr: float = 3.0) -> tuple:
    """Find realistic TP from pre-computed H4 pools."""
    entry = h1_bars[entry_idx_h1].close
    sl_dist = atr * 1.5  # minimum SL distance proxy
    
    if direction == "BULL":
        # Nearest EQH that is above entry + min R:R
        min_tp = entry + sl_dist * min_rr
        for idx_h4, price in h4_pools["eqh"]:
            # map h4 index to h1 index for ordering
            h1_idx_equiv = idx_h4 * 4
            if price >= min_tp and h1_idx_equiv >= entry_idx_h1:
                return price, "EQH_H4"
        # Nearest swing high above min TP
        for idx_h4, price in h4_pools["swing_highs"]:
            h1_idx_equiv = idx_h4 * 4
            if price >= min_tp and h1_idx_equiv >= entry_idx_h1:
                return price, "SWING_HIGH_H4"
        # Fallback: realistic fixed target (3R minimum)
        return entry + sl_dist * min_rr, f"MIN_{min_rr}R"
    else:
        min_tp = entry - sl_dist * min_rr
        for idx_h4, price in h4_pools["eql"]:
            h1_idx_equiv = idx_h4 * 4
            if price <= min_tp and h1_idx_equiv >= entry_idx_h1:
                return price, "EQL_H4"
        for idx_h4, price in h4_pools["swing_lows"]:
            h1_idx_equiv = idx_h4 * 4
            if price <= min_tp and h1_idx_equiv >= entry_idx_h1:
                return price, "SWING_LOW_H4"
        return entry - sl_dist * min_rr, f"MIN_{min_rr}R"


# ── Trade Execution ──────────────────────────────────────────────────────────

def execute_trade(sel, h1_bars, entry_idx, h4_pools, atr, risk_per_trade=50, max_hold_bars=10, comm=0.01):
    t = SimTrade()
    t.entry_time = str(h1_bars[entry_idx].time)
    t.direction = sel.direction
    t.entry_price = sel.entry_price
    t.sl = sel.sl
    t.confidence = sel.confidence
    t.confluence_count = sel.confluence_count
    t.manipulation_type = sel.manipulation_leg.leg_type if sel.manipulation_leg else ""
    t.h4_bias = sel.h4_bias.direction if sel.h4_bias else "NEUTRAL"

    # Realistic TP from H4 structure pools
    tp, label = find_tp_from_pools(entry_idx, h1_bars, h4_pools, sel.direction, atr, min_rr=3.0)
    t.tp = tp
    t.tp_label = label

    # Position sizing
    sl_dist = max(abs(sel.sl - sel.entry_price), atr * 1.5)
    # Reject if TP too close to entry
    tp_dist = abs(tp - sel.entry_price)
    if tp_dist < sl_dist * 1.5:
        return None  # Skip — not enough reward
    
    units = risk_per_trade / sl_dist
    units = min(units, 5000)

    entry = sel.entry_price; sl = sel.sl
    for i in range(entry_idx + 1, min(entry_idx + max_hold_bars + 1, len(h1_bars))):
        b = h1_bars[i]
        if sel.direction == "BULL":
            if b.low <= sl:
                t.exit_price = sl; t.pnl_gross = -(sl - entry) * units; t.exit_reason = f"SL ({label})"; t.bars_held = i - entry_idx; t.pnl_net = t.pnl_gross - comm; return t
            if b.high >= tp:
                t.exit_price = tp; t.pnl_gross = (tp - entry) * units; t.exit_reason = f"TP ({label})"; t.bars_held = i - entry_idx; t.pnl_net = t.pnl_gross - comm; return t
        else:
            if b.high >= sl:
                t.exit_price = sl; t.pnl_gross = -(entry - sl) * units; t.exit_reason = f"SL ({label})"; t.bars_held = i - entry_idx; t.pnl_net = t.pnl_gross - comm; return t
            if b.low <= tp:
                t.exit_price = tp; t.pnl_gross = (entry - tp) * units; t.exit_reason = f"TP ({label})"; t.bars_held = i - entry_idx; t.pnl_net = t.pnl_gross - comm; return t

    last = h1_bars[min(entry_idx + max_hold_bars, len(h1_bars) - 1)]
    t.exit_price = last.close
    t.pnl_gross = (last.close - entry) * units if sel.direction == "BULL" else (entry - last.close) * units
    t.exit_reason = f"TIMEOUT ({label})"; t.bars_held = max_hold_bars; t.pnl_net = t.pnl_gross - comm
    return t


# ── Single Run ────────────────────────────────────────────────────────────────

def run_single(seed, h1, h4, h4_pools, atr_norm, risk_per_trade=50, max_hold_bars=10):
    random.seed(seed)
    m15 = generate_m15_from_h1(h1, seed=seed)
    step = random.choice([36, 40, 44, 48, 52, 56])  # coarser

    equity = 1000.0; peak = equity
    trades = []; cooldown = 0; in_trade_end = 0

    for i in range(50, len(h1) - max_hold_bars, step):
        if i < in_trade_end or cooldown > 0:
            if cooldown > 0: cooldown -= 1
            continue

        h4_ctx = h4[max(0, (i // 4) - 20):(i // 4)]
        h1_ctx = h1[max(0, i - 50):i]
        m15_ctx = m15[max(0, i * 4 - 40):i * 4]

        if len(h4_ctx) < 6 or len(h1_ctx) < 20:
            continue

        try:
            sel = select_trade_multi_tf(h4_ctx, h1_ctx, m15_ctx, current_price=h1[i].close, pip_size=0.01)
        except Exception:
            continue

        if not sel.is_actionable or sel.direction == "NEUTRAL":
            continue

        t = execute_trade(sel, h1, i, h4_pools, atr_norm, risk_per_trade, max_hold_bars)
        if t is None:
            continue

        equity += t.pnl_net
        trades.append(t)
        in_trade_end = i + max_hold_bars + 1
        cooldown = 3
        if equity > peak: peak = equity

    wins = sum(1 for t in trades if t.pnl_net > 0)
    dd = 0.0
    eq_curve = [1000.0]
    for t in trades: eq_curve.append(eq_curve[-1] + t.pnl_net)
    peak = 1000.0
    for e in eq_curve:
        if e > peak: peak = e
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
    print("[LIQ-TP v5] Loading data...")
    h1 = fetch_h1_bars("XAUUSD", "1y")
    h4 = resample_bars(h1, 4)
    atr_norm = max(calc_atr(h1[:50]), 0.5)
    h4_pools = precompute_h4_pools(h4, atr_norm)
    print(f"Loaded: {len(h1)} H1 | {len(h4)} H4 | EQH={len(h4_pools['eqh'])} | EQL={len(h4_pools['eql'])}")
    print(f"[LIQ-TP v5] {n_runs} runs | TP=H4 structure | min R:R=3.0 | risk=${risk_per_trade}/trade")

    results = []; t0 = time.time()
    for s in range(n_runs):
        r = run_single(s, h1, h4, h4_pools, atr_norm, risk_per_trade, max_hold_bars)
        results.append(r)
        if (s + 1) % 20 == 0:
            print(f"  {s+1}/{n_runs} done ({time.time()-t0:.1f}s)")
    elapsed = time.time() - t0

    fname = "liq_tp_v5_runs.csv"
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

    with open("liq_tp_v5_summary.json", "w") as f:
        json.dump(stats, f, indent=2)

    # Audit top/bottom performers for R:R
    for label, arr in [("TOP", sorted(results, key=lambda r: -r.total_return_pct)[:5]),
                       ("BOTTOM", sorted(results, key=lambda r: r.total_return_pct)[:5])]:
        print(f"\n--- {label} 5 performers ---")
        for r in arr:
            reasons = {}
            rr_total = 0; rr_count = 0
            for t in r.trades:
                reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
                sl_d = abs(t.entry_price - t.sl)
                tp_d = abs(t.tp - t.entry_price)
                if sl_d > 0:
                    rr_total += tp_d/sl_d; rr_count += 1
            avg_rr = rr_total / rr_count if rr_count > 0 else 0
            print(f"  seed {r.seed}: ${r.total_return_pct:+.1f} | {r.total_trades} trades | WR {r.win_rate:.0f}% | DD {r.max_drawdown_pct:.1f}% | avg R:R {avg_rr:.2f}:1 | Avg TP {tp_d:.1f}pt")
            for reas, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
                print(f"    {cnt:3d}x {reas}")

    print(f"\n{'='*50}")
    print(f"LIQUIDITY-TP v5 — H4 STRUCTURE TP — MIN R:R 3:1")
    print(f"{'='*50}")
    print(f"Profitable: {stats['profitable']}/{n_runs} ({stats['profitable_pct']:.1f}%)")
    print(f"Return:     mean={stats['return']['mean']:+.1f}%  med={stats['return']['med']:+.1f}%  min={stats['return']['min']:+.1f}%  max={stats['return']['max']:+.1f}%")
    print(f"Win Rate:   {stats['win_rate']['mean']:.1f}% (med {stats['win_rate']['med']:.1f}%)")
    print(f"Drawdown:   mean={stats['drawdown']['mean']:.1f}%  max={stats['drawdown']['max']:.1f}%")
    print(f"PF:         {stats['pf']['mean']:.1f} (med {stats['pf']['med']:.1f})")
    print(f"Trades/run: {stats['trades_per_run']['mean']:.1f} (med {stats['trades_per_run']['med']:.1f})")
    print(f"Time:       {elapsed:.1f}s")
    print(f"CSV:        {fname}")
    return results, stats


if __name__ == "__main__":
    run_monte(n_runs=100, max_hold_bars=10, risk_per_trade=50)
