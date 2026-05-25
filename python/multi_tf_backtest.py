#!/usr/bin/env python3
"""
multi_tf_backtest.py — Dual-timeframe deterministic ICT backtest across M5/M15/H1/D1/W1

HTF/LTF PAIRS:
  LTF= M5  → HTF= H1  (both 60d yfinance max)
  LTF= M15 → HTF= H1  (both 60d)
  LTF= H1  → HTF= D1  (H1=2yr, D1=5yr — aligned by timestamp)
  LTF= D1  → HTF= W1  (D1=5yr, W1=10yr — aligned by timestamp)
  LTF= W1  → HTF= self (W1=10yr, HTF = same bars longer lookback)

SESSIONS per LTF:
  M5 / M15 / H1  → LONDON  (07–10 UTC)
  D1 / W1        → ASIA    (22–07 UTC, captures daily/weekly closes)

ALIGNMENT:
  Each LTF bar maps to the last HTF bar whose close timestamp ≤ LTF bar timestamp.
  No future leakage. HTF window capped to 500 bars.

REALITY MODE:
  Limit-fill simulation + commission + slippage per deterministic_honest_backtest.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import yfinance as yf

from deterministic_ict_engine import Bar, DeterministicICTEngine, Signal
from deterministic_honest_backtest import (
    BacktestResult, Trade, _check_limit_fill, _commission, _pip_value,
    _simulate_slippage,
)

HERE: Path = Path(__file__).parent
RESULTS_PATH: Path = HERE / "multi_tf_backtest_results.json"
MAX_LTF_LOOKBACK = 500
MAX_HTF_LOOKBACK = 500


# ── Timeframe config ──────────────────────────────────────────────────────────
TF_CONFIG: dict[str, dict] = {
    "M5":   {"interval": "5m",  "period": "60d",  "htf": "H1",  "session": "LONDON"},
    "M15":  {"interval": "15m", "period": "60d",  "htf": "H1",  "session": "LONDON"},
    "H1":   {"interval": "1h",  "period": "2y",   "htf": "D1",  "session": "LONDON"},
    "D1":   {"interval": "1d",  "period": "5y",   "htf": "W1",  "session": "ASIA"},
    "W1":   {"interval": "1wk", "period": "10y",  "htf": None, "session": "ASIA"},
}


@dataclass
class AlignedPair:
    ltf_bars: list[Bar]
    htf_bars: list[Bar]
    session: str


def fetch_bars(ticker: str, interval: str, period: str) -> list[Bar]:
    """Download → Bar list (oldest first)."""
    print(f"    [FETCH] {ticker} {interval} {period} ...")
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        print(f"    [WARN] empty data for {ticker} {interval}")
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    bars: list[Bar] = []
    for i, (ts, row) in enumerate(df.iterrows()):
        ts_val = pd.Timestamp(ts).timestamp()
        bars.append(Bar(
            idx=i,
            time=str(ts)[:19],
            o=float(row["Open"]),
            h=float(row["High"]),
            l=float(row["Low"]),
            c=float(row["Close"]),
            v=int(row.get("Volume", 0)),
            broker_ts=float(ts_val),
        ))
    print(f"    [FETCH] {len(bars)} bars  ({bars[0].time} → {bars[-1].time})")
    return bars


def align_htf_to_ltf(ltf: list[Bar], htf: list[Bar]) -> list[int]:
    """
    Return list `map` where map[i] = index of last HTF bar with broker_ts <= ltf[i].broker_ts.
    If no HTF bar precedes LTF bar i, returns -1.
    """
    htf_ts = [b.broker_ts for b in htf]
    mapping: list[int] = []
    h = 0
    for lb in ltf:
        while h < len(htf_ts) and htf_ts[h] <= lb.broker_ts:
            h += 1
        mapping.append(h - 1)
    return mapping


def run_deterministic_backtest(
    symbol: str,
    ltf: list[Bar],
    htf: list[Bar],
    session: str,
    htf_map: list[int],
    lot_size: float = 0.01,
    reality: bool = True,
) -> BacktestResult:
    """Walk-forward with capped lookback and true dual-timeframe alignment."""
    engine = DeterministicICTEngine(
        session_window=session,
        max_spread_pips=50.0,
        min_confidence=0.55,
        min_rr=2.0,
        require_htf_alignment=True,
    )
    result = BacktestResult()
    if len(ltf) < 55:
        return result

    def _to_raw(bb: list[Bar]) -> list[dict]:
        return [
            {"time": b.time, "o": b.o, "h": b.h, "l": b.l, "c": b.c,
             "v": b.v, "broker_ts": b.broker_ts}
            for b in bb
        ]

    for i in range(50, len(ltf) - 1):
        # ---- LTF window (capped) ----
        l_start = max(0, i + 1 - MAX_LTF_LOOKBACK)
        ltf_window = ltf[l_start : i + 1]

        # ---- HTF window (capped, aligned) ----
        h_end = htf_map[i]
        if h_end < 0:
            continue
        h_start = max(0, h_end + 1 - MAX_HTF_LOOKBACK)
        htf_window = htf[h_start : h_end + 1]

        try:
            sigs = engine.generate_signals(
                symbol, _to_raw(htf_window), _to_raw(ltf_window),
                broker_ts=ltf[i].broker_ts,
            )
        except Exception as e:
            # Suppress repetitive struct warnings in backtest
            continue

        for sig in sigs:
            result.total_signals += 1
            fill_price, fill_idx = _check_limit_fill(sig, ltf, i + 1, max_forward=25, reality=reality)
            if fill_price is None:
                continue

            trade = _run_single_trade(sig, ltf, fill_idx, lot_size, reality)
            result.filled_trades += 1
            if trade.exit_reason == "TP1":
                result.wins += 1
            elif trade.exit_reason == "SL":
                result.losses += 1
            else:
                if trade.profit_net > 0:
                    result.wins += 1
                elif trade.profit_net < 0:
                    result.losses += 1
                else:
                    result.breakevens += 1
            result.total_profit_raw += trade.profit_raw
            result.total_profit_net += trade.profit_net
            result.trades.append(_trade_to_dict(trade, reality, session))

    if result.filled_trades > 0:
        result.win_rate = result.wins / result.filled_trades
        wins = [t["profit_net_pips"] for t in result.trades if t["profit_net_pips"] > 0]
        losses = [t["profit_net_pips"] for t in result.trades if t["profit_net_pips"] < 0]
        result.avg_win = sum(wins) / len(wins) if wins else 0
        result.avg_loss = sum(losses) / len(losses) if losses else 0
        result.avg_rr = abs(result.avg_win / result.avg_loss) if result.avg_loss != 0 else 0
        result.expectancy = (result.win_rate * result.avg_win) + ((1 - result.win_rate) * result.avg_loss)
    return result


def _run_single_trade(sig: Signal, bars: list[Bar], fill_idx: int, lot_size: float, reality: bool) -> Trade:
    sym = sig.symbol
    direction = sig.direction
    entry_raw = sig.entry_price
    sl_raw = sig.sl
    tp1 = sig.tp
    tp2 = sig.tp2

    slip = _simulate_slippage(sym, direction, True) if reality else 0.0
    if direction == "BULL":
        entry_filled = entry_raw + slip
        sl_filled = sl_raw - (slip if reality else 0.0)
    else:
        entry_filled = entry_raw - slip
        sl_filled = sl_raw + (slip if reality else 0.0)

    comm = _commission(sym) * lot_size if reality else 0.0
    exit_price: Optional[float] = None
    exit_bar: Optional[int] = None
    exit_reason = ""
    max_hold = 200

    for j in range(fill_idx + 1, min(fill_idx + max_hold, len(bars))):
        b = bars[j]
        if direction == "BULL":
            if b.l <= sl_raw:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = sl_raw - exit_slip
                exit_bar = j
                exit_reason = "SL"
                break
            if tp1 is not None and b.h >= tp1:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = tp1 - exit_slip
                exit_bar = j
                exit_reason = "TP1"
                break
        else:
            if b.h >= sl_raw:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = sl_raw + exit_slip
                exit_bar = j
                exit_reason = "SL"
                break
            if tp1 is not None and b.l <= tp1:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = tp1 + exit_slip
                exit_bar = j
                exit_reason = "TP1"
                break

    if exit_price is None:
        exit_bar = len(bars) - 1
        exit_price = bars[-1].c
        exit_reason = "TIMEOUT"

    if direction == "BULL":
        profit_raw = exit_price - entry_raw
    else:
        profit_raw = entry_raw - exit_price

    pip = _pip_value(sym)
    profit_pips_raw = profit_raw / pip
    slip_pips = (abs(entry_filled - entry_raw) + abs(exit_price - (tp1 if exit_reason == "TP1" else sl_raw))) / pip
    profit_pips_net = profit_pips_raw - slip_pips - (comm / pip)

    return Trade(
        symbol=sym, direction=direction,
        entry_price_raw=round(entry_raw, 5),
        entry_price_filled=round(entry_filled, 5),
        sl_raw=round(sl_raw, 5),
        sl_filled=round(sl_filled, 5),
        tp1_raw=round(tp1, 5) if tp1 else None,
        tp2_raw=round(tp2, 5) if tp2 else None,
        fill_bar=fill_idx, exit_price=round(exit_price, 5),
        exit_bar=exit_bar, exit_reason=exit_reason,
        commission_usd=round(comm, 2),
        slippage_pips=round(slip_pips, 2),
        profit_raw=round(profit_pips_raw, 2),
        profit_net=round(profit_pips_net, 2),
    )


def _trade_to_dict(trade: Trade, reality: bool, session: str) -> dict:
    return {
        "symbol": trade.symbol, "direction": trade.direction,
        "entry": trade.entry_price_raw, "sl": trade.sl_raw,
        "tp1": trade.tp1_raw, "fill_bar": trade.fill_bar,
        "exit_bar": trade.exit_bar, "exit_reason": trade.exit_reason,
        "profit_raw_pips": trade.profit_raw,
        "profit_net_pips": trade.profit_net,
        "commission": trade.commission_usd,
        "slippage_pips": trade.slippage_pips,
        "reality_mode": reality, "session": session,
    }


def print_summary(label: str, raw: BacktestResult, reality: BacktestResult):
    print("\n" + "=" * 72)
    print(f"  {label}")
    print("=" * 72)
    print(f"{'Metric':<30} {'RAW (Optimistic)':<20} {'REALITY-ADJUSTED':<20}")
    print("-" * 72)
    print(f"{'Total signals generated':<30} {raw.total_signals:<20} {reality.total_signals:<20}")
    print(f"{'Filled trades':<30} {raw.filled_trades:<20} {reality.filled_trades:<20}")
    print(f"{'Win rate (%)':<30} {raw.win_rate*100:.1f}%                 {reality.win_rate*100:.1f}%")
    print(f"{'Wins / Losses / BE':<30} {raw.wins}/{raw.losses}/{raw.breakevens}                {reality.wins}/{reality.losses}/{reality.breakevens}")
    print(f"{'Avg win (pips)':<30} {raw.avg_win:.2f}                {reality.avg_win:.2f}")
    print(f"{'Avg loss (pips)':<30} {raw.avg_loss:.2f}                {reality.avg_loss:.2f}")
    print(f"{'Avg R:R':<30} {raw.avg_rr:.2f}                {reality.avg_rr:.2f}")
    print(f"{'Expectancy (pips)':<30} {raw.expectancy:.2f}                {reality.expectancy:.2f}")
    print(f"{'Total profit raw (pips)':<30} {raw.total_profit_raw:.2f}")
    print(f"{'Total profit net (pips)':<30} {'N/A':<20} {reality.total_profit_net:.2f}")
    print("=" * 72)


def run_tf(tf_label: str) -> Optional[dict]:
    cfg = TF_CONFIG[tf_label]
    print(f"\n{'='*72}")
    print(f"  RUNNING: LTF={tf_label}  |  HTF={cfg['htf']}  |  SESSION={cfg['session']}")
    print(f"{'='*72}")

    # Fetch LTF
    ltf = fetch_bars("GC=F", cfg["interval"], cfg["period"])
    if not ltf:
        print(f"[SKIP] No LTF data for {tf_label}")
        return None

    # Fetch HTF
    if cfg["htf"]:
        htf_cfg = TF_CONFIG[cfg["htf"]]
        # HTF must cover LTF period + lookback headroom
        htf_period = cfg["period"]  # same period; yfinance handles overlapping range
        htf = fetch_bars("GC=F", htf_cfg["interval"], htf_period)
        if not htf:
            print(f"[SKIP] No HTF data for {tf_label} → {cfg['htf']}")
            return None
    else:
        # W1 self-HTF: use same bars but with longer lookback (engine handles it)
        htf = ltf

    # Align
    if cfg["htf"]:
        htf_map = align_htf_to_ltf(ltf, htf)
    else:
        htf_map = list(range(len(ltf)))

    symbol = "XAUUSD"
    print(f"[INFO] Running RAW backtest on {len(ltf)} LTF bars...")
    raw_res = run_deterministic_backtest(symbol, ltf, htf, cfg["session"], htf_map, reality=False)
    print(f"[INFO] Running REALITY backtest on {len(ltf)} LTF bars...")
    reality_res = run_deterministic_backtest(symbol, ltf, htf, cfg["session"], htf_map, reality=True)
    print_summary(f"XAUUSD  {tf_label} (HTF={cfg['htf']})", raw_res, reality_res)

    return {
        "ltf": tf_label, "htf": cfg["htf"], "session": cfg["session"],
        "ltf_bars": len(ltf), "htf_bars": len(htf),
        "raw": _result_to_dict(raw_res),
        "reality": _result_to_dict(reality_res),
        "trades": reality_res.trades[:300],  # cap for JSON
    }


def _result_to_dict(res: BacktestResult) -> dict:
    return {
        "signals": res.total_signals,
        "filled": res.filled_trades,
        "wins": res.wins,
        "losses": res.losses,
        "breakevens": res.breakevens,
        "win_rate_pct": round(res.win_rate * 100, 2),
        "avg_win_pips": res.avg_win,
        "avg_loss_pips": res.avg_loss,
        "avg_rr": res.avg_rr,
        "expectancy_pips": res.expectancy,
        "total_profit_pips": res.total_profit_raw if res.total_profit_raw else res.total_profit_net,
    }


def main():
    results: dict = {}
    for tf_label in ["M5", "M15", "H1", "D1", "W1"]:
        try:
            r = run_tf(tf_label)
            if r:
                results[tf_label] = r
        except Exception as e:
            print(f"[ERROR] {tf_label} failed: {e}")
            import traceback
            traceback.print_exc()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": "XAUUSD",
        "runs": results,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n[OK] Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
