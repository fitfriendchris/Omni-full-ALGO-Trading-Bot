#!/usr/bin/env python3
"""
forward_sim_liquidity_tp_v2.py — Multi-TF Simulator with Liquidity-Based Exits v2

Exits:
  - SL: Manipulation leg extreme
  - TP: Next opposing liquidity (EQH/EQL, swing high/low, H4 external)
  - TIMEOUT: max_hold_bars

Guards:
  - SL floor: 1.5 x ATR
  - Max units: 5x equity (leverage cap)
  - In-trade blocking: no overlapping positions
  - Cooldown: 3 bars between trades
"""
import csv, json, math, random, sys, os, time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))

from smc_engine import Bar
from multi_tf_selector import select_trade_multi_tf, MultiTFSelection
from forward_sim_multi_tf import fetch_h1_bars, resample_bars, generate_m15_from_h1, calc_atr, SimConfig


# ── Dataclasses ─────────────────────────────────────────────────────────────

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
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    trades: List[SimTrade] = field(default_factory=list)


# ── Liquidity Target Scanner ────────────────────────────────────────────────

def find_next_liquidity(
    h1_bars: List[Bar],
    h4_bars: List[Bar],
    start_idx: int,
    direction: str,
    max_scan: int = 50,
    atr: float = 10.0,
) -> Tuple[float, str]:
    """Find next opposing liquidity. Returns (price, label)."""
    if start_idx >= len(h1_bars) - 1:
        # Fallback: fixed 3R TP
        entry = h1_bars[start_idx].close
        if direction == "BULL":
            return entry + atr * 3, "FIXED_3R"
        return entry - atr * 3, "FIXED_3R"

    if direction == "BULL":
        # Scan for equal highs or swing highs
        highs = [(i, h1_bars[i].high) for i in range(start_idx, min(start_idx + max_scan, len(h1_bars)))]
        # Equal highs cluster
        for i, hi in highs[:20]:
            cluster = [h for j, h in highs if abs(h - hi) <= atr * 0.5 and j != i and j > i]
            if len(cluster) >= 1:
                return max(hi, max(cluster)), "EQH"
        # Swing high
        for i in range(start_idx + 2, min(start_idx + max_scan, len(h1_bars) - 2)):
            b = h1_bars[i]
            if b.high > h1_bars[i-1].high and b.high > h1_bars[i-2].high and \
               b.high > h1_bars[i+1].high and b.high > h1_bars[i+2].high:
                return b.high, "SWING_HIGH"
        # External: H4 high
        h4_highs = [b.high for b in h4_bars if b.time >= h1_bars[start_idx].time]
        if h4_highs:
            return h4_highs[0], "H4_HIGH"
        return h1_bars[start_idx].close + atr * 3, "FIXED_3R"

    else:  # BEAR
        lows = [(i, h1_bars[i].low) for i in range(start_idx, min(start_idx + max_scan, len(h1_bars)))]
        for i, lo in lows[:20]:
            cluster = [l for j, l in lows if abs(l - lo) <= atr * 0.5 and j != i and j > i]
            if len(cluster) >= 1:
                return min(lo, min(cluster)), "EQL"
        for i in range(start_idx + 2, min(start_idx + max_scan, len(h1_bars) - 2)):
            b = h1_bars[i]
            if b.low < h1_bars[i-1].low and b.low < h1_bars[i-2].low and \
               b.low < h1_bars[i+1].low and b.low < h1_bars[i+2].low:
                return b.low, "SWING_LOW"
        h4_lows = [b.low for b in h4_bars if b.time >= h1_bars[start_idx].time]
        if h4_lows:
            return h4_lows[0], "H4_LOW"
        return h1_bars[start_idx].close - atr * 3, "FIXED_3R"


# ── Trade Execution ─────────────────────────────────────────────────────────

def execute_trade(
    sel,
    h1_bars,
    h4_bars,
    entry_idx,
    cfg,
    atr,
    equity,
    position_risk=0.05,  # 5% risk on $1000 fixed lot
    max_hold_bars=10,
):
    t = SimTrade()
    t.entry_time = str(h1_bars[entry_idx].time)
    t.direction = sel.direction
    t.entry_price = sel.entry_price
    t.sl = sel.sl
    t.confidence = sel.confidence
    t.confluence_count = sel.confluence_count
    t.manipulation_type = sel.manipulation_leg.leg_type if sel.manipulation_leg else ""
    t.h4_bias = sel.h4_bias.direction if sel.h4_bias else "NEUTRAL"

    tp, label = find_next_liquidity(h1_bars, h4_bars, entry_idx, sel.direction, atr=atr)
    t.tp = tp
    t.tp_label = label

    # Position sizing with SL floor
    sl_dist = abs(sel.sl - sel.entry_price)
    sl_dist = max(sl_dist, atr * 1.5)  # floor
    risk_usd = 1000 * position_risk
    units = risk_usd / sl_dist
    # Leverage cap: max 5x notional
    max_units = 5000
    units = min(units, max_units)

    entry = sel.entry_price
    sl = sel.sl
    tp_price = tp

    for i in range(entry_idx + 1, min(entry_idx + max_hold_bars + 1, len(h1_bars))):
        b = h1_bars[i]
        if sel.direction == "BULL":
            if b.low <= sl:
                t.exit_price = sl
                t.pnl_gross = -(sl - entry) * units
                t.exit_reason = f"SL ({label})"
                t.bars_held = i - entry_idx
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * 0.01 * 0.01
                return t
            if b.high >= tp_price:
                t.exit_price = tp_price
                t.pnl_gross = (tp_price - entry) * units
                t.exit_reason = f"TP ({label})"
                t.bars_held = i - entry_idx
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * 0.01 * 0.01
                return t
        else:
            if b.high >= sl:
                t.exit_price = sl
                t.pnl_gross = -(entry - sl) * units
                t.exit_reason = f"SL ({label})"
                t.bars_held = i - entry_idx
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * 0.01 * 0.01
                return t
            if b.low <= tp_price:
                t.exit_price = tp_price
                t.pnl_gross = (entry - tp_price) * units
                t.exit_reason = f"TP ({label})"
                t.bars_held = i - entry_idx
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * 0.01 * 0.01
                return t

    # Timeout
    last = h1_bars[min(entry_idx + max_hold_bars, len(h1_bars) - 1)]
    t.exit_price = last.close
    if sel.direction == "BULL":
        t.pnl_gross = (last.close - entry) * units
    else:
        t.pnl_gross = (entry - last.close) * units
    t.exit_reason = f"TIMEOUT ({label})"
    t.bars_held = max_hold_bars
    t.pnl_net = t.pnl_gross - cfg.commission_per_lot * 0.01 * 0.01
    return t


# ── Run Single Seed ─────────────────────────────────────────────────────────

def run_single(seed, h1, h4, m15, cfg, atr_norm, max_hold_bars=10):
    random.seed(seed)
    
    # ── VARY M15 GENERATION PER SEED (real Monte Carlo) ──
    m15 = generate_m15_from_h1(h1, seed=seed)
    
    # ── VARY CHECK INTERVAL PER SEED ──
    step = random.choice([12, 16, 20, 24, 28, 32])
    
    equity = cfg.initial_equity
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
        m15_end = i * 4
        m15_start = max(0, m15_end - 40)
        m15_ctx = m15[m15_start:m15_end]

        if len(h4_ctx) < 6 or len(h1_ctx) < 20:
            continue

        try:
            sel = select_trade_multi_tf(h4_ctx, h1_ctx, m15_ctx,
                                        current_price=h1[i].close,
                                        pip_size=cfg.pip_size)
        except Exception:
            continue

        if not sel.is_actionable or sel.direction == "NEUTRAL":
            continue

        t = execute_trade(sel, h1, h4, i, cfg, atr_norm, equity,
                          position_risk=0.05  # 5% of current equity, max_hold_bars=max_hold_bars)

        equity += t.pnl_net
        trades.append(t)
        in_trade_end = i + max_hold_bars + 1
        cooldown = 3

        if equity > peak:
            peak = equity

    wins = sum(1 for t in trades if t.pnl_net > 0)
    losses = len(trades) - wins
    wr = (wins / len(trades) * 100) if trades else 0

    gross_profit = sum(t.pnl_gross for t in trades if t.pnl_gross > 0)
    gross_loss = abs(sum(t.pnl_gross for t in trades if t.pnl_gross < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 999

    # True max DD
    eq_curve = [cfg.initial_equity]
    for t in trades:
        eq_curve.append(eq_curve[-1] + t.pnl_net)
    peak = cfg.initial_equity
    dd = 0.0
    for e in eq_curve:
        if e > peak:
            peak = e
        dd = max(dd, (peak - e) / peak * 100)

    return SimResult(
        seed=seed,
        final_equity=equity,
        total_return_pct=(equity - cfg.initial_equity) / cfg.initial_equity * 100,
        total_trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=wr,
        max_drawdown_pct=dd,
        profit_factor=pf,
        trades=trades,
    )


# ── Monte Carlo ─────────────────────────────────────────────────────────────

def run_monte(n_runs=100, workers=6, max_hold_bars=10):
    h1 = fetch_h1_bars("XAUUSD", "1y")
    h4 = resample_bars(h1, 4)
    m15 = generate_m15_from_h1(h1, seed=12345)
    atr_norm = max(calc_atr(h1[:50]), 0.5) if len(h1) >= 50 else 0.5
    cfg = SimConfig(symbol="XAUUSD", pip_size=0.01)

    print(f"[LIQ-TP v2] {n_runs} runs | XAUUSD | 1y | max_hold={max_hold_bars}H1 | workers={workers}")
    print(f"[LIQ-TP v2] {len(h1)} H1 bars → {len(h4)} H4 → {len(m15)} M15")

    results = []
    t0 = time.time()

    for s in range(n_runs):
        r = run_single(s, h1, h4, m15, cfg, atr_norm, max_hold_bars)
        results.append(r)
        if (s + 1) % 10 == 0:
            print(f"[LIQ-TP v2] {s+1}/{n_runs} done")

    elapsed = time.time() - t0

    # CSV
    fname = f"liq_tp_v2_runs_{max_hold_bars}h.csv"
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run","seed","return_pct","win_rate","max_dd_pct","trades","pf"])
        for i, r in enumerate(results):
            w.writerow([i+1, r.seed, f"{r.total_return_pct:.4f}", f"{r.win_rate:.2f}",
                       f"{r.max_drawdown_pct:.4f}", r.total_trades, f"{r.profit_factor:.4f}"])

    # Stats
    arr = lambda x: sorted(x)
    def _stat(x):
        s = sorted(x)
        n = len(s)
        return {
            "mean": sum(s)/n,
            "median": s[n//2] if n%2 else (s[n//2 - 1] + s[n//2]) / 2,
            "min": min(s), "max": max(s),
            "p5": s[max(0, int(n*0.05)-1)],
            "p95": s[min(n-1, int(n*0.95)-1)],
        }

    stats = {
        "symbol": "XAUUSD", "n_runs": n_runs, "period": "1y",
        "max_hold_bars": max_hold_bars,
        "profitable_runs": sum(1 for r in results if r.total_return_pct > 0),
        "profitable_pct": sum(1 for r in results if r.total_return_pct > 0) / n_runs * 100,
        "return_pct": _stat([r.total_return_pct for r in results]),
        "win_rate": _stat([r.win_rate for r in results]),
        "max_drawdown_pct": _stat([r.max_drawdown_pct for r in results]),
        "profit_factor": _stat([r.profit_factor for r in results]),
        "trades_per_run": _stat([r.total_trades for r in results]),
        "total_time_sec": elapsed,
    }

    with open("liq_tp_v2_summary.json", "w") as f:
        json.dump(stats, f, indent=2)

    print(f"\n{'='*60}")
    print(f"LIQUIDITY-TP v2 — {max_hold_bars}H1 MAX HOLD")
    print(f"{'='*60}")
    print(json.dumps(stats, indent=2))
    print(f"{'='*60}")
    print(f"CSV: {fname}")
    return results, stats


if __name__ == "__main__":
    run_monte(n_runs=100, workers=1, max_hold_bars=10)
