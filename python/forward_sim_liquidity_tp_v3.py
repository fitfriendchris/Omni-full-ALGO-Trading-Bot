#!/usr/bin/env python3
"""
forward_sim_liquidity_tp_v3.py — Multi-TF Simulator with Liquidity-Based Exits v3
Fixed lot sizing: $50 risk per trade (5% of $1000 base)
Exits: SL (leg extreme), TP (opposing liquidity), TIMEOUT (max_hold_bars)
Checks: Once per day (24 H1 bar step) with in-trade blocking
"""
import csv, json, math, random, sys, os, time
from dataclasses import dataclass, field
from typing import List, Tuple

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


# ── Liquidity Scanner ───────────────────────────────────────────────────────

def find_next_liquidity(h1_bars, h4_bars, start_idx, direction, atr):
    sc = min(start_idx + 50, len(h1_bars))
    if direction == "BULL":
        for i in range(start_idx, min(start_idx+20, sc)):
            hi = h1_bars[i].high
            cluster = [h1_bars[j].high for j in range(i+1, min(i+5, sc)) if abs(h1_bars[j].high - hi) <= atr * 0.5]
            if len(cluster) >= 1:
                return max(hi, max(cluster)), "EQH"
        for i in range(start_idx+2, sc-2):
            b = h1_bars[i]
            if b.high > h1_bars[i-1].high and b.high > h1_bars[i-2].high and b.high > h1_bars[i+1].high and b.high > h1_bars[i+2].high:
                return b.high, "SWING_HIGH"
        hh = [b.high for b in h4_bars if b.time >= h1_bars[start_idx].time]
        if hh:
            return hh[0], "H4_HIGH"
        return h1_bars[start_idx].close + atr * 3, "FIXED_3R"
    else:
        for i in range(start_idx, min(start_idx+20, sc)):
            lo = h1_bars[i].low
            cluster = [h1_bars[j].low for j in range(i+1, min(i+5, sc)) if abs(h1_bars[j].low - lo) <= atr * 0.5]
            if len(cluster) >= 1:
                return min(lo, min(cluster)), "EQL"
        for i in range(start_idx+2, sc-2):
            b = h1_bars[i]
            if b.low < h1_bars[i-1].low and b.low < h1_bars[i-2].low and b.low < h1_bars[i+1].low and b.low < h1_bars[i+2].low:
                return b.low, "SWING_LOW"
        ll = [b.low for b in h4_bars if b.time >= h1_bars[start_idx].time]
        if ll:
            return ll[0], "H4_LOW"
        return h1_bars[start_idx].close - atr * 3, "FIXED_3R"


# ── Trade Execution ───────────────────────────────────────────────────────────

def execute_trade(sel, h1_bars, h4_bars, entry_idx, cfg, atr, risk_per_trade=50, max_hold_bars=10):
    t = SimTrade()
    t.entry_time = str(h1_bars[entry_idx].time)
    t.direction = sel.direction
    t.entry_price = sel.entry_price
    t.sl = sel.sl
    t.confidence = sel.confidence
    t.confluence_count = sel.confluence_count
    t.manipulation_type = sel.manipulation_leg.leg_type if sel.manipulation_leg else ""
    t.h4_bias = sel.h4_bias.direction if sel.h4_bias else "NEUTRAL"

    tp, label = find_next_liquidity(h1_bars, h4_bars, entry_idx, sel.direction, atr)
    t.tp = tp
    t.tp_label = label

    # Fixed-lot sizing: $50 risk per trade, SL floor = 1.5x ATR
    sl_dist = max(abs(sel.sl - sel.entry_price), atr * 1.5)
    units = risk_per_trade / sl_dist
    units = min(units, 5000)  # max 5000 units ($50k notional, 5:1 leverage on $10k)

    entry = sel.entry_price; sl = sel.sl; tp_price = tp
    comm = cfg.commission_per_lot * 0.01

    for i in range(entry_idx + 1, min(entry_idx + max_hold_bars + 1, len(h1_bars))):
        b = h1_bars[i]
        if sel.direction == "BULL":
            if b.low <= sl:
                t.exit_price = sl
                t.pnl_gross = -(sl - entry) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"SL ({label})"; t.bars_held = i - entry_idx; return t
            if b.high >= tp_price:
                t.exit_price = tp_price
                t.pnl_gross = (tp_price - entry) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"TP ({label})"; t.bars_held = i - entry_idx; return t
        else:
            if b.high >= sl:
                t.exit_price = sl
                t.pnl_gross = -(entry - sl) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"SL ({label})"; t.bars_held = i - entry_idx; return t
            if b.low <= tp_price:
                t.exit_price = tp_price
                t.pnl_gross = (entry - tp_price) * units
                t.pnl_net = t.pnl_gross - comm
                t.exit_reason = f"TP ({label})"; t.bars_held = i - entry_idx; return t

    # Timeout
    last = h1_bars[min(entry_idx + max_hold_bars, len(h1_bars) - 1)]
    t.exit_price = last.close
    t.pnl_gross = (last.close - entry) * units if sel.direction == "BULL" else (entry - last.close) * units
    t.exit_reason = f"TIMEOUT ({label})"; t.bars_held = max_hold_bars; t.pnl_net = t.pnl_gross - comm
    return t


# ── Single Run ────────────────────────────────────────────────────────────────

def run_single(seed, h1, h4, atr_norm, risk_per_trade=50, max_hold_bars=10):
    random.seed(seed)
    
    # Vary M15 per seed for real Monte Carlo
    m15 = generate_m15_from_h1(h1, seed=seed)
    step = random.choice([12, 16, 20, 24, 28, 32])
    
    equity = 1000.0
    peak = equity
    trades = []
    cooldown = 0
    in_trade_end = 0

    for i in range(50, len(h1) - max_hold_bars, step):
        if i < in_trade_end:
            continue
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

        t = execute_trade(sel, h1, h4, i, SimConfig(symbol="XAUUSD", pip_size=0.01), atr_norm, risk_per_trade, max_hold_bars)
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
    print(f"[LIQ-TP v3] Loading data...")
    h1 = fetch_h1_bars("XAUUSD", "1y")
    h4 = resample_bars(h1, 4)
    atr_norm = max(calc_atr(h1[:50]), 0.5)
    print(f"Loaded: {len(h1)} H1 | {len(h4)} H4")
    print(f"[LIQ-TP v3] {n_runs} runs | max_hold={max_hold_bars}H1 | risk=${risk_per_trade}/trade")

    results = []
    t0 = time.time()
    for s in range(n_runs):
        r = run_single(s, h1, h4, atr_norm, risk_per_trade, max_hold_bars)
        results.append(r)
        if (s + 1) % 20 == 0:
            print(f"  {s+1}/{n_runs} done ({time.time()-t0:.1f}s)")
    elapsed = time.time() - t0

    # CSV
    fname = f"liq_tp_v3_runs.csv"
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run","seed","return_pct","win_rate","max_dd_pct","trades","pf"])
        for i, r in enumerate(results):
            w.writerow([i+1, r.seed, f"{r.total_return_pct:.2f}", f"{r.win_rate:.1f}",
                       f"{r.max_drawdown_pct:.2f}", r.total_trades, f"{r.profit_factor:.2f}"])

    rets = [r.total_return_pct for r in results]
    wrs = [r.win_rate for r in results]
    dds = [r.max_drawdown_pct for r in results]
    pfs = [r.profit_factor for r in results]
    tcs = [r.total_trades for r in results]

    def _stat(arr):
        s = sorted(arr)
        n = len(s)
        return {"mean": sum(s)/n, "med": s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2,
                "min": min(s), "max": max(s), "p5": s[max(0,int(n*0.05)-1)],
                "p95": s[min(n-1,int(n*0.95)-1)]}

    stats = {
        "n_runs": n_runs, "max_hold": max_hold_bars, "risk_per_trade": risk_per_trade,
        "profitable": sum(1 for r in results if r.total_return_pct > 0),
        "profitable_pct": sum(1 for r in results if r.total_return_pct > 0) / n_runs * 100,
        "return": _stat(rets), "win_rate": _stat(wrs),
        "drawdown": _stat(dds), "pf": _stat(pfs),
        "trades_per_run": _stat(tcs), "time_sec": elapsed,
    }

    with open("liq_tp_v3_summary.json", "w") as f:
        json.dump(stats, f, indent=2)

    print(f"\n{'='*50}")
    print(f"LIQUIDITY-TP v3 — FIXED LOT ($50/trade)")
    print(f"{'='*50}")
    print(f"Profitable: {stats['profitable']}/{n_runs} ({stats['profitable_pct']:.1f}%)")
    print(f"Return:     mean={stats['return']['mean']:+.1f}%  med={stats['return']['med']:+.1f}%  min={stats['return']['min']:+.1f}%  max={stats['return']['max']:+.1f}%")
    print(f"Win Rate:   mean={stats['win_rate']['mean']:.1f}%  med={stats['win_rate']['med']:.1f}%")
    print(f"Drawdown:   mean={stats['drawdown']['mean']:.1f}%  med={stats['drawdown']['med']:.1f}%  max={stats['drawdown']['max']:.1f}%")
    print(f"PF:         mean={stats['pf']['mean']:.1f}  med={stats['pf']['med']:.1f}")
    print(f"Trades/run: mean={stats['trades_per_run']['mean']:.1f}  med={stats['trades_per_run']['med']:.1f}")
    print(f"Time:       {elapsed:.1f}s ({n_runs/elapsed:.1f} runs/s)")
    print(f"CSV:        {fname}")
    return results, stats


if __name__ == "__main__":
    run_monte(n_runs=100, max_hold_bars=10, risk_per_trade=50)
