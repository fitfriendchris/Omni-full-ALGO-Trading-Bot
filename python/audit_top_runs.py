#!/usr/bin/env python3
"""
audit_top_runs.py — Re-run top seeds with full per-trade journals.
"""
import csv, json, math, random, statistics, sys, os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from smc_engine import Bar
from dual_tf_selector import select_trade
from stdv_ote_engine import is_price_in_ote_zone

from forward_simulator_1000 import (
    SimConfig, SimTrade, SimResult, fetch_h1_bars, resample_bars,
    calc_atr, simulate_ticks, limit_crossed, best_fill, next_amd,
    calc_lots, AMD_TRANSITION,
    INITIAL_EQUITY, MAX_RISK_PER_TRADE, COMMISSION_PER_LOT,
    PIPSIZE, LIMIT_FILL_BASE, LIMIT_FILL_OTE, SLIPPAGE_BASE_PIPS,
    SLIPPAGE_VOL_MULT, ATR_WINDOW, SKIP_BARS, TICKS_PER_BAR,
)

# ── Full-journal single run (non-parallel) ───────────────────────────────────

def run_single_with_journal(seed: int, all_bars: List[Bar], h4_bars: List[Bar],
                            cfg: SimConfig, atr_norm: float) -> SimResult:
    rng = random.Random(seed)
    result = SimResult(seed=seed)
    equity = cfg.initial_equity
    peak = equity
    max_dd = 0.0
    open_trades: List[SimTrade] = []
    amd = "ACCUMULATION"
    ltf_w = 20
    htf_w = 50

    for idx in range(htf_w + 10, len(all_bars), SKIP_BARS):
        bar = all_bars[idx]
        result.bars_processed += 1
        if (idx // SKIP_BARS) % 4 == 0:
            amd = next_amd(amd, rng)

        htf = all_bars[max(0, idx - htf_w):idx]
        ltf = all_bars[max(0, idx - ltf_w):idx]
        macro = h4_bars[max(0, (idx // 4) - 12):(idx // 4)]

        try:
            sel = select_trade(htf_bars=htf, ltf_bars=ltf, rules=None,
                               macro_bars=macro if macro else None,
                               amd_phase=amd, pip_size=cfg.pip_size)
        except Exception:
            continue

        result.setups_seen += 1
        if sel.is_actionable and sel.entry_price and sel.sl and sel.tp:
            if len(open_trades) >= 3:
                continue

            lots, risk_usd = calc_lots(equity, cfg.max_risk_per_trade,
                                       sel.entry_price, sel.sl, cfg.pip_size)

            fp = LIMIT_FILL_BASE
            if sel.stdv_profile and is_price_in_ote_zone(sel.stdv_profile, bar.close):
                fp += LIMIT_FILL_OTE
            dist = abs(sel.entry_price - bar.close)
            atr_cur = calc_atr(htf, 14)
            if atr_cur > 0 and dist > atr_cur * 2:
                fp -= 0.20
            fp = max(0.10, min(0.95, fp))

            ticks = simulate_ticks(bar, n=TICKS_PER_BAR, seed=seed + idx)
            crossed = limit_crossed(ticks, sel.entry_price, sel.direction)
            filled = crossed and rng.random() < fp

            atr_now = calc_atr(htf, 14)
            vm = 1.0 + (atr_now / atr_norm - 1.0) * (SLIPPAGE_VOL_MULT - 1.0)
            vm = max(0.5, min(3.0, vm))
            slip_pips = rng.gauss(SLIPPAGE_BASE_PIPS, SLIPPAGE_BASE_PIPS * 0.5) * vm
            slip = slip_pips * cfg.pip_size

            if filled and crossed:
                actual = best_fill(ticks, sel.entry_price, sel.direction)
                actual += slip if sel.direction == "BULL" else -slip
            else:
                actual = sel.entry_price

            comm = cfg.commission_per_lot * lots

            trade = SimTrade(
                entry_time=bar.time, direction=sel.direction,
                entry_price=actual, sl=sel.sl, tp=sel.tp,
                lots=lots, risk_usd=risk_usd, commission=comm,
                slippage_entry=slip, fill_prob=fp, filled=filled,
                confluence_count=sel.confluence_count, confidence=sel.confidence,
                manipulation_type=sel.manipulation_leg.leg_type if sel.manipulation_leg else "",
            )
            if filled:
                open_trades.append(trade)
                equity -= comm
                result.total_commission += comm

        # Exits
        still_open: List[SimTrade] = []
        for t in open_trades:
            eticks = simulate_ticks(bar, n=50, seed=seed + idx + 5000)
            exited = False
            eprice = 0.0
            reason = ""
            if t.direction == "BULL":
                slh = any(x <= t.sl for x in eticks)
                tph = any(x >= t.tp for x in eticks)
                if slh and tph:
                    si = next((i for i, x in enumerate(eticks) if x <= t.sl), 9999)
                    ti = next((i for i, x in enumerate(eticks) if x >= t.tp), 9999)
                    exited = True; eprice = t.sl if si < ti else t.tp; reason = "SL" if si < ti else "TP"
                elif slh:
                    exited = True; eprice = t.sl; reason = "SL"
                elif tph:
                    exited = True; eprice = t.tp; reason = "TP"
            else:
                slh = any(x >= t.sl for x in eticks)
                tph = any(x <= t.tp for x in eticks)
                if slh and tph:
                    si = next((i for i, x in enumerate(eticks) if x >= t.sl), 9999)
                    ti = next((i for i, x in enumerate(eticks) if x <= t.tp), 9999)
                    exited = True; eprice = t.sl if si < ti else t.tp; reason = "SL" if si < ti else "TP"
                elif slh:
                    exited = True; eprice = t.sl; reason = "SL"
                elif tph:
                    exited = True; eprice = t.tp; reason = "TP"

            if exited:
                t.exit_time = bar.time; t.exit_price = eprice; t.exit_reason = reason
                if t.direction == "BULL":
                    t.pnl_gross = (eprice - t.entry_price) * t.lots * 100
                else:
                    t.pnl_gross = (t.entry_price - eprice) * t.lots * 100
                t.pnl_net = t.pnl_gross - t.commission
                equity += t.pnl_net
                result.trades.append(t)
                result.filled_trades += 1
                if t.pnl_net > 0:
                    result.winning_trades += 1; result.gross_profit += t.pnl_net
                else:
                    result.losing_trades += 1; result.gross_loss += abs(t.pnl_net)
            else:
                still_open.append(t)
        open_trades = still_open

        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    last = all_bars[-1].close if all_bars else 0.0
    for t in open_trades:
        t.exit_time = all_bars[-1].time if all_bars else 0.0
        t.exit_price = last
        if t.direction == "BULL":
            t.pnl_gross = (last - t.entry_price) * t.lots * 100
        else:
            t.pnl_gross = (t.entry_price - last) * t.lots * 100
        t.pnl_net = t.pnl_gross - t.commission
        t.exit_reason = "CLOSE_EOD"
        result.trades.append(t)
        result.filled_trades += 1
        equity += t.pnl_net
        if t.pnl_net > 0:
            result.winning_trades += 1; result.gross_profit += t.pnl_net
        else:
            result.losing_trades += 1; result.gross_loss += abs(t.pnl_net)

    result.final_equity = equity
    result.total_return_pct = ((equity / cfg.initial_equity) - 1.0) * 100
    result.max_drawdown_usd = max_dd
    result.max_drawdown_pct = (max_dd / peak * 100) if peak > 0 else 0.0
    result.total_trades = result.filled_trades
    wins = [t.pnl_net for t in result.trades if t.pnl_net > 0]
    losses = [t.pnl_net for t in result.trades if t.pnl_net < 0]
    result.avg_win = statistics.mean(wins) if wins else 0.0
    result.avg_loss = statistics.mean(losses) if losses else 0.0
    result.win_rate = (result.winning_trades / result.total_trades * 100) if result.total_trades > 0 else 0.0
    result.profit_factor = (result.gross_profit / result.gross_loss) if result.gross_loss > 0 else 999.0
    result.expectancy = (result.avg_win * (result.win_rate / 100) + result.avg_loss * (1 - result.win_rate / 100)) if result.total_trades > 0 else 0.0
    result.avg_trade_net = (sum(t.pnl_net for t in result.trades) / len(result.trades)) if result.trades else 0.0
    return result


# ── Audit driver ────────────────────────────────────────────────────────────

def audit_seeds(seeds: List[int], symbol: str = "XAUUSD", period: str = "1y"):
    print(f"[AUDIT] Loading data for {symbol} {period}...")
    h1 = fetch_h1_bars(symbol, period)
    if not h1:
        print("[AUDIT] No data!")
        return
    h4 = resample_bars(h1, 4)
    atr_norm = calc_atr(h1[:ATR_WINDOW]) if len(h1) > ATR_WINDOW else 5.0
    if atr_norm < 0.5:
        atr_norm = 0.5
    cfg = SimConfig(symbol=symbol, pip_size=PIPSIZE.get(symbol, 0.01))

    out_dir = Path(__file__).parent / "audit_trades"
    out_dir.mkdir(exist_ok=True)

    all_summaries = []
    for seed in seeds:
        print(f"[AUDIT] Seed {seed} running...")
        res = run_single_with_journal(seed, h1, h4, cfg, atr_norm)
        all_summaries.append({
            "seed": seed,
            "return_pct": round(res.total_return_pct, 2),
            "win_rate": round(res.win_rate, 2),
            "max_dd_pct": round(res.max_drawdown_pct, 2),
            "pf": round(res.profit_factor, 2) if res.profit_factor < 900 else 999,
            "trades": res.total_trades,
            "final_equity": round(res.final_equity, 2),
        })

        # Write per-trade CSV
        csv_path = out_dir / f"seed_{seed}_trades.csv"
        with open(csv_path, "w", newline="") as f:
            if res.trades:
                keys = [k for k in asdict(res.trades[0]).keys()]
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for t in res.trades:
                    w.writerow(asdict(t))
        print(f"[AUDIT] Seed {seed} → {csv_path} ({res.total_trades} trades)")

    summary_path = out_dir / "audit_summary.json"
    with open(summary_path, "w") as f:
        json.dump(all_summaries, f, indent=2)
    print(f"[AUDIT] Summary → {summary_path}")
    return all_summaries


if __name__ == "__main__":
    # Top 5 seeds from the 1000-run (by return_pct)
    # run 150 -> seed 193 (+3549%), run 129 -> seed 171 (+3282%)
    # run 835 -> seed 877 (+2495%), run 653 -> seed 695 (+2419%)
    # run 647 -> seed 689 (+2175%)
    top_seeds = [193, 171, 877, 695, 689]
    audit_seeds(top_seeds, "XAUUSD", "1y")
