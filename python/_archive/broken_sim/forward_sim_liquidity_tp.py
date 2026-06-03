#!/usr/bin/env python3
"""
forward_sim_liquidity_tp.py — Multi-TF Simulator with Liquidity-Based Exits

Exits:
  - SL: Manipulation leg extreme (fixed)
  - TP: Next opposing liquidity pool in direction of trade
    BULL → next swing high / equal highs / external liquidity
    BEAR → next swing low / equal lows / external liquidity
  - TIMEOUT: max_hold_bars (default 10 H1 bars = ~1 trading day)
  - If price retraces 50% of move from entry, trail to breakeven
"""
import csv, json, math, random, sys, os, time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from smc_engine import Bar, analyze as smc_analyze, Swing, LiquidityPool
from multi_tf_selector import select_trade_multi_tf, MultiTFSelection
from forward_sim_multi_tf import fetch_h1_bars, resample_bars, generate_m15_from_h1, calc_atr, SimConfig

@dataclass
class SimTrade:
    entry_time: str
    direction: str
    entry_price: float
    exit_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    pnl_net: float = 0.0
    pnl_gross: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    confidence: float = 0.0
    confluence_count: int = 0
    manipulation_type: str = ""
    h4_bias: str = ""
    liquidity_distance: float = 0.0  # points from entry to liquidity target
    max_runup: float = 0.0
    max_drawdown: float = 0.0

@dataclass
class SimResult:
    seed: int
    final_equity: float
    total_return_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown_pct: float
    profit_factor: float
    avg_r_per_trade: float
    total_commission: float
    total_slippage: float
    trades: List[SimTrade] = field(default_factory=list)

# ── Liquidity Target Scanner ────────────────────────────────────────────────

def find_next_liquidity(
    h1_bars: List[Bar],
    h4_bars: List[Bar],
    start_idx: int,
    direction: str,
    max_scan: int = 50,
    atr: float = 10.0,
) -> Tuple[Optional[float], str]:
    """
    Scan forward from start_idx for the next opposing liquidity level.
    Returns (price, label) or (None, 'NO_LIQUIDITY').
    
    Priority:
      1. Equal highs/lows cluster (2+ touches within 0.5 ATR)
      2. Swing high/low (pivot with 2-bar confirmation each side)
      3. Nearest H4 high/low as external liquidity
    """
    if start_idx >= len(h1_bars) - 1:
        return None, "NO_LIQUIDITY"
    
    # Collect H1 highs/lows from start_idx forward
    highs = [(i, h1_bars[i].high) for i in range(start_idx, min(start_idx + max_scan, len(h1_bars)))]
    lows = [(i, h1_bars[i].low) for i in range(start_idx, min(start_idx + max_scan, len(h1_bars)))]
    
    if direction == "BULL":
        # Look for swing highs and equal highs
        swing_highs = []
        for i in range(start_idx + 2, min(start_idx + max_scan, len(h1_bars) - 2)):
            b = h1_bars[i]
            if b.high > h1_bars[i-1].high and b.high > h1_bars[i-2].high and \
               b.high > h1_bars[i+1].high and b.high > h1_bars[i+2].high:
                swing_highs.append((i, b.high))
        
        # Equal highs cluster
        for i, hi in highs:
            cluster = [h for j, h in highs if abs(h - hi) <= atr * 0.5 and j != i and j > i]
            if len(cluster) >= 1:
                # Return the highest point in cluster
                return max(hi, max(cluster)), "EQH"
        
        # Nearest swing high
        if swing_highs:
            return swing_highs[0][1], "SWING_HIGH"
        
        # External liquidity: nearest H4 high
        h4_highs = [b.high for b in h4_bars if b.time >= h1_bars[start_idx].time]
        if h4_highs:
            return min(h4_highs), "H4_HIGH"
            
        return None, "NO_LIQUIDITY"
    
    else:  # BEAR
        swing_lows = []
        for i in range(start_idx + 2, min(start_idx + max_scan, len(h1_bars) - 2)):
            b = h1_bars[i]
            if b.low < h1_bars[i-1].low and b.low < h1_bars[i-2].low and \
               b.low < h1_bars[i+1].low and b.low < h1_bars[i+2].low:
                swing_lows.append((i, b.low))
        
        # Equal lows cluster
        for i, lo in lows:
            cluster = [l for j, l in lows if abs(l - lo) <= atr * 0.5 and j != i and j > i]
            if len(cluster) >= 1:
                return min(lo, min(cluster)), "EQL"
        
        if swing_lows:
            return swing_lows[0][1], "SWING_LOW"
        
        h4_lows = [b.low for b in h4_bars if b.time >= h1_bars[start_idx].time]
        if h4_lows:
            return max(h4_lows), "H4_LOW"
            
        return None, "NO_LIQUIDITY"


# ── Trade Execution with Liquidity TP ──────────────────────────────────────

def execute_trade(
    sel: MultiTFSelection,
    h1_bars: List[Bar],
    h4_bars: List[Bar],
    entry_idx: int,
    cfg: SimConfig,
    atr: float,
    equity: float,
    position_risk: float = 0.05,
    max_hold_bars: int = 10,
) -> SimTrade:
    """Execute a single trade with liquidity-based TP."""
    t = SimTrade(
        entry_time=h1_bars[entry_idx].time.strftime("%Y-%m-%d %H:%M") if hasattr(h1_bars[entry_idx].time, 'strftime') else str(h1_bars[entry_idx].time),
        direction=sel.direction,
        entry_price=sel.entry_price,
        sl=sel.sl,
        confidence=sel.confidence,
        confluence_count=sel.confluence_count,
        manipulation_type=sel.manipulation_leg.leg_type if sel.manipulation_leg else "",
        h4_bias=sel.h4_bias.direction if sel.h4_bias else "NEUTRAL",
    )
    
    # Find TP: next opposing liquidity
    tp_price, tp_label = find_next_liquidity(h1_bars, h4_bars, entry_idx, sel.direction, max_scan=50, atr=atr)
    
    if tp_price is None:
        # No liquidity found — use fixed R:R or skip
        if sel.direction == "BULL":
            tp_price = sel.entry_price + (sel.entry_price - sel.sl) * 3.0
            tp_label = "FIXED_3R"
        else:
            tp_price = sel.entry_price - (sel.sl - sel.entry_price) * 3.0
            tp_label = "FIXED_3R"
    
    t.tp = tp_price
    t.liquidity_distance = abs(tp_price - sel.entry_price)
    
    # Position sizing: risk position_risk% of equity on SL distance
    sl_dist = abs(sel.sl - sel.entry_price)
    # ── SL floor: never less than 1.5 x ATR ──
    sl_dist = max(sl_dist, atr * 1.5)
    risk_usd = equity * position_risk
    units = risk_usd / sl_dist
    # Cap unit size: max 5:1 effective leverage on capital
    max_units = equity * 5
    units = min(abs(units), max_units) * (1 if units >= 0 else -1)
    units = abs(units)  # always positive — direction is separate

    # ── Simulate forward ──
    entry = sel.entry_price
    sl = sel.sl
    tp = tp_price
    
    for i in range(entry_idx + 1, min(entry_idx + max_hold_bars + 1, len(h1_bars))):
        b = h1_bars[i]
        
        if sel.direction == "BULL":
            # SL hit
            if b.low <= sl:
                t.exit_price = sl
                t.pnl_gross = -(sl - entry) * units
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * abs(units) * 0.01
                t.exit_reason = f"SL ({tp_label})"
                t.bars_held = i - entry_idx
                t.max_runup = max(0, (max(b.high for b in h1_bars[entry_idx:i+1]) - entry) * units)
                t.max_drawdown = max(0, -(min(b.low for b in h1_bars[entry_idx:i+1]) - entry) * units)
                return t
            
            # TP hit
            if b.high >= tp:
                t.exit_price = tp
                t.pnl_gross = (tp - entry) * units
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * abs(units) * 0.01
                t.exit_reason = f"TP ({tp_label})"
                t.bars_held = i - entry_idx
                t.max_runup = max(0, (max(b.high for b in h1_bars[entry_idx:i+1]) - entry) * units)
                return t
        
        else:  # BEAR
            if b.high >= sl:
                t.exit_price = sl
                t.pnl_gross = -(entry - sl) * units
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * abs(units) * 0.01
                t.exit_reason = f"SL ({tp_label})"
                t.bars_held = i - entry_idx
                t.max_runup = max(0, (entry - min(b.low for b in h1_bars[entry_idx:i+1])) * units)
                t.max_drawdown = max(0, -(entry - max(b.high for b in h1_bars[entry_idx:i+1])) * units)
                return t
            
            if b.low <= tp:
                t.exit_price = tp
                t.pnl_gross = (entry - tp) * units
                t.pnl_net = t.pnl_gross - cfg.commission_per_lot * abs(units) * 0.01
                t.exit_reason = f"TP ({tp_label})"
                t.bars_held = i - entry_idx
                t.max_runup = max(0, (entry - min(b.low for b in h1_bars[entry_idx:i+1])) * units)
                return t
    
    # Timeout — exit at last price
    last_bar = h1_bars[min(entry_idx + max_hold_bars, len(h1_bars) - 1)]
    if sel.direction == "BULL":
        t.exit_price = last_bar.close
        t.pnl_gross = (last_bar.close - entry) * units
    else:
        t.exit_price = last_bar.close
        t.pnl_gross = (entry - last_bar.close) * units
    
    t.pnl_net = t.pnl_gross - cfg.commission_per_lot * abs(units) * 0.01
    t.exit_reason = f"TIMEOUT ({tp_label})"
    t.bars_held = max_hold_bars
    return t


# ── Main Simulation ─────────────────────────────────────────────────────────

def run_single(seed: int, h1: List[Bar], h4: List[Bar], m15: List[Bar], 
               cfg: SimConfig, atr_norm: float, max_hold_bars: int = 10) -> SimResult:
    """Run one simulation with liquidity-based exits."""
    random.seed(seed)
    equity = cfg.initial_equity
    peak = equity
    trades = []
    cooldown_bars = 0
    in_trade = False
    entry_end = 0

   for i in range(50, len(h1) - max_hold_bars):
        if in_trade and i < entry_end:
            continue
        in_trade = False
        
        if cooldown_bars > 0:
            cooldown_bars -= 1
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

        # ── Enter trade ──
        in_trade = True
        entry_end = i + max_hold_bars + 1

        # Execute with liquidity TP
        t = execute_trade(sel, h1, h4, i, cfg, atr_norm, equity, 
                         position_risk=0.05, max_hold_bars=max_hold_bars)
        
        equity += t.pnl_net
        trades.append(t)
        cooldown_bars = 3
        
        if equity > peak:
            peak = equity
    
    wins = sum(1 for t in trades if t.pnl_net > 0)
    losses = len(trades) - wins
    wr = wins / len(trades) * 100 if trades else 0
    
    gross_profit = sum(t.pnl_gross for t in trades if t.pnl_gross > 0)
    gross_loss = abs(sum(t.pnl_gross for t in trades if t.pnl_gross < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 999
    
    total_comm = sum(cfg.commission_per_lot * 0.01 for t in trades)
    total_slip = sum(abs(t.pnl_net - t.pnl_gross) for t in trades)
    
    # True max DD from equity curve
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
        avg_r_per_trade=(equity - cfg.initial_equity) / len(trades) / cfg.initial_equity * 100 if trades else 0,
        total_commission=total_comm,
        total_slippage=total_slip,
        trades=trades,
    )


def run_monte(n_runs=100, workers=12, max_hold_bars=10):
    h1 = fetch_h1_bars("XAUUSD", "1y")
    h4 = resample_bars(h1, 4)
    m15 = generate_m15_from_h1(h1, seed=12345)
    atr_norm = calc_atr(h1[:50]) if len(h1) >= 50 else 5.0
    if atr_norm < 0.5: atr_norm = 0.5
    cfg = SimConfig(symbol="XAUUSD", pip_size=0.01)
    
    seeds = list(range(n_runs))
    
    print(f"[LIQ-TP SIM] {n_runs} runs | XAUUSD | 1y | max_hold={max_hold_bars}H1 | workers={workers}")
    print(f"[LIQ-TP SIM] {len(h1)} H1 → {len(h4)} H4 → {len(m15)} M15")
    
    results = []
    t0 = time.time()
    
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(run_single, s, h1, h4, m15, cfg, atr_norm, max_hold_bars): s for s in seeds}
            for fut in futures:
                try:
                    results.append(fut.result())
                except Exception as e:
                    print(f"[ERR] seed {futures[fut]}: {e}")
    else:
        for s in seeds:
            results.append(run_single(s, h1, h4, m15, cfg, atr_norm, max_hold_bars))
    
    elapsed = time.time() - t0
    
    # Write CSV
    fname = f"liq_tp_runs_{max_hold_bars}h.csv"
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run","seed","return_pct","win_rate","max_dd_pct","trades","pf","commission","slippage","avg_r"])
        for i, r in enumerate(results):
            w.writerow([i+1, r.seed, f"{r.total_return_pct:.4f}", f"{r.win_rate:.2f}",
                       f"{r.max_drawdown_pct:.4f}", r.total_trades, f"{r.profit_factor:.4f}",
                       f"{r.total_commission:.4f}", f"{r.total_slippage:.4f}",
                       f"{r.avg_r_per_trade:.4f}"])
    
    # Stats
    rets = [r.total_return_pct for r in results]
    wrs = [r.win_rate for r in results]
    dds = [r.max_drawdown_pct for r in results]
    pfs = [r.profit_factor for r in results]
    trades_counts = [r.total_trades for r in results]
    
    def _stat(arr):
        arr_s = sorted(arr)
        n = len(arr_s)
        return {
            "mean": sum(arr_s) / n,
            "median": arr_s[n // 2] if n % 2 else (arr_s[n // 2 - 1] + arr_s[n // 2]) / 2,
            "min": min(arr_s), "max": max(arr_s),
            "std": (sum((x - sum(arr_s)/n)**2 for x in arr_s) / n) ** 0.5,
            "p5": arr_s[max(0, int(n * 0.05) - 1)],
            "p95": arr_s[min(n - 1, int(n * 0.95) - 1)],
        }
    
    stats = {
        "symbol": "XAUUSD", "n_runs": n_runs, "period": "1y",
        "max_hold_bars": max_hold_bars,
        "profitable_runs": sum(1 for r in results if r.total_return_pct > 0),
        "profitable_pct": sum(1 for r in results if r.total_return_pct > 0) / n_runs * 100,
        "return_pct": _stat(rets), "win_rate": _stat(wrs),
        "max_drawdown_pct": _stat(dds), "profit_factor": _stat(pfs),
        "trades_per_run": _stat(trades_counts),
        "commission_per_run": _stat([r.total_commission for r in results]),
        "total_time_sec": elapsed,
    }
    
    with open("liq_tp_summary.json", "w") as f:
        json.dump(stats, f, indent=2, default=lambda o: float(o) if isinstance(o, (int, float)) else str(o))
    
    print(f"\n{'='*60}")
    print(f"LIQUIDITY-TP SIMULATION — {max_hold_bars}H1 MAX HOLD")
    print(f"{'='*60}")
    print(json.dumps(stats, indent=2, default=lambda o: float(o) if isinstance(o, (int, float)) else str(o)))
    print(f"{'='*60}")
    print(f"CSV: {fname}")
    
    return results, stats


if __name__ == "__main__":
    # Run with 10H1 max hold (~1 trading day)
    run_monte(n_runs=100, workers=12, max_hold_bars=10)
    
    # Also run with 20H1 max hold (~2-3 trading days) for comparison
    # run_monte(n_runs=100, workers=12, max_hold_bars=20)
