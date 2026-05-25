#!/usr/bin/env python3
"""
deterministic_5yr_backtest.py — Full 5-year walk-forward backtest for XAUUSD
Fetches data from yfinance, runs deterministic ICT engine, reports
RAW vs REALITY-ADJUSTED metrics honestly.

DATA:
  - Daily:  GC=F gold futures via yfinance (up to ~20 years available, we use 5yr)
  - H1:     GC=F last ~730 days (yfinance API limit for intraday)

SESSION HANDLING:
  - Daily bars close at UTC midnight (hour=0). Only ASIA session (22-07) passes.
  - So daily backtest uses session="ASIA" to get meaningful coverage.
  - H1 backtest uses session="LONDON" for proper killzone fidelity.

WALK-FORWARD:
  - No future leakage. At each bar i, engine sees bars[0:i+1] only.
  - Limit fill checked on subsequent bars within 25-bar window.
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
    _simulate_slippage, run_backtest, print_summary, COMMISSION,
)

HERE: Path = Path(__file__).parent
RESULTS_PATH: Path = HERE / "deterministic_5yr_backtest_results.json"


def fetch_yf_bars(ticker: str, interval: str, period: str) -> list[Bar]:
    """Download yfinance bars → deterministic Bar list (oldest first)."""
    print(f"[FETCH] {ticker} {interval} {period} ...")
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        print(f"[WARN] No data returned for {ticker} {interval}")
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    bars: list[Bar] = []
    for i, (ts, row) in enumerate(df.iterrows()):
        ts_val = pd.Timestamp(ts).timestamp() if hasattr(ts, "timestamp") else float(ts)
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
    print(f"[FETCH] Got {len(bars)} bars ({bars[0].time} → {bars[-1].time})")
    return bars


def run_deterministic_backtest(
    symbol: str,
    bars: list[Bar],
    session_window: str,
    htf_bars: Optional[list[Bar]] = None,
    reality: bool = True,
) -> BacktestResult:
    """Walk-forward deterministic backtest with explicit session_window."""
    engine = DeterministicICTEngine(
        session_window=session_window,
        max_spread_pips=50.0,
        min_confidence=0.55,
        min_rr=2.0,
        require_htf_alignment=True,
    )
    result = BacktestResult()
    if len(bars) < 55:
        return result

    htf = htf_bars if htf_bars else bars
    MAX_LOOKBACK = 500  # cap history for O(N) total runtime
    for i in range(50, len(bars) - 1):
        start = max(0, i + 1 - MAX_LOOKBACK)
        ltf_window = bars[start : i + 1]
        h_end = min(i + 1, len(htf))
        h_start = max(0, h_end - MAX_LOOKBACK)
        htf_window = htf[h_start : h_end]

        def _to_raw(bb: list[Bar]):
            return [
                {
                    "time": b.time,
                    "o": b.o,
                    "h": b.h,
                    "l": b.l,
                    "c": b.c,
                    "v": b.v,
                    "broker_ts": b.broker_ts,
                }
                for b in bb
            ]

        try:
            sigs = engine.generate_signals(
                symbol, _to_raw(htf_window), _to_raw(ltf_window), broker_ts=bars[i].broker_ts
            )
        except Exception as e:
            print(f"[ERROR] engine failed at bar {i}: {e}")
            continue

        for sig in sigs:
            result.total_signals += 1
            fill_price, fill_idx = _check_limit_fill(sig, bars, i + 1, max_forward=25, reality=reality)
            if fill_price is None:
                continue

            # Build trade via deterministic_honest_backtest helper
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

            comm = _commission(sym) * 0.01 if reality else 0.0
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
            slip_pips = (
                abs(entry_filled - entry_raw) + abs(exit_price - (tp1 if exit_reason == "TP1" else sl_raw))
            ) / pip
            profit_pips_net = profit_pips_raw - slip_pips - (comm / pip)

            trade = Trade(
                symbol=sym,
                direction=direction,
                entry_price_raw=round(entry_raw, 5),
                entry_price_filled=round(entry_filled, 5),
                sl_raw=round(sl_raw, 5),
                sl_filled=round(sl_filled, 5),
                tp1_raw=round(tp1, 5) if tp1 else None,
                tp2_raw=round(tp2, 5) if tp2 else None,
                fill_bar=fill_idx,
                exit_price=round(exit_price, 5),
                exit_bar=exit_bar,
                exit_reason=exit_reason,
                commission_usd=round(comm, 2),
                slippage_pips=round(slip_pips, 2),
                profit_raw=round(profit_pips_raw, 2),
                profit_net=round(profit_pips_net, 2),
            )

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
            result.trades.append(
                {
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                    "entry": trade.entry_price_raw,
                    "sl": trade.sl_raw,
                    "tp1": trade.tp1_raw,
                    "fill_bar": trade.fill_bar,
                    "exit_bar": trade.exit_bar,
                    "exit_reason": trade.exit_reason,
                    "profit_raw_pips": trade.profit_raw,
                    "profit_net_pips": trade.profit_net,
                    "commission": trade.commission_usd,
                    "slippage_pips": trade.slippage_pips,
                    "reality_mode": reality,
                    "session": session_window,
                }
            )

    if result.filled_trades > 0:
        result.win_rate = result.wins / result.filled_trades
        wins = [t["profit_net_pips"] for t in result.trades if t["profit_net_pips"] > 0]
        losses = [t["profit_net_pips"] for t in result.trades if t["profit_net_pips"] < 0]
        result.avg_win = sum(wins) / len(wins) if wins else 0
        result.avg_loss = sum(losses) / len(losses) if losses else 0
        result.avg_rr = abs(result.avg_win / result.avg_loss) if result.avg_loss != 0 else 0
        result.expectancy = (
            result.win_rate * result.avg_win
        ) + ((1 - result.win_rate) * result.avg_loss)

    return result


def print_section(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def save_results(payload: dict):
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n[OK] Results written to {RESULTS_PATH}")


def main():
    symbol = "XAUUSD"
    all_results: dict = {}

    # ──────────────────────────────────────────────────────────────
    # RUN 1: DAILY BARS (5 years, session=ASIA since midnight UTC)
    # ──────────────────────────────────────────────────────────────
    print_section(f"RUN 1: {symbol} DAILY — 5 YEARS — ASIA SESSION GATE")
    daily_bars = fetch_yf_bars("GC=F", "1d", "5y")
    if daily_bars:
        print(f"[INFO] Running RAW mode on {len(daily_bars)} daily bars...")
        daily_raw = run_deterministic_backtest(symbol, daily_bars, session_window="ASIA", reality=False)
        print(f"[INFO] Running REALITY mode on {len(daily_bars)} daily bars...")
        daily_reality = run_deterministic_backtest(symbol, daily_bars, session_window="ASIA", reality=True)
        print_summary(daily_raw, daily_reality)
        all_results["daily_5yr"] = {
            "bars": len(daily_bars),
            "session": "ASIA",
            "raw": {
                "signals": daily_raw.total_signals,
                "filled": daily_raw.filled_trades,
                "wins": daily_raw.wins,
                "losses": daily_raw.losses,
                "breakevens": daily_raw.breakevens,
                "win_rate_pct": round(daily_raw.win_rate * 100, 2),
                "avg_win_pips": daily_raw.avg_win,
                "avg_loss_pips": daily_raw.avg_loss,
                "avg_rr": daily_raw.avg_rr,
                "expectancy_pips": daily_raw.expectancy,
                "total_profit_pips": daily_raw.total_profit_raw,
            },
            "reality": {
                "signals": daily_reality.total_signals,
                "filled": daily_reality.filled_trades,
                "wins": daily_reality.wins,
                "losses": daily_reality.losses,
                "breakevens": daily_reality.breakevens,
                "win_rate_pct": round(daily_reality.win_rate * 100, 2),
                "avg_win_pips": daily_reality.avg_win,
                "avg_loss_pips": daily_reality.avg_loss,
                "avg_rr": daily_reality.avg_rr,
                "expectancy_pips": daily_reality.expectancy,
                "total_profit_pips": daily_reality.total_profit_net,
            },
            "trades": daily_reality.trades[:500],  # cap for JSON size
        }
    else:
        print("[SKIP] No daily data available.")

    # ──────────────────────────────────────────────────────────────
    # RUN 2: H1 BARS (max ~2 years, yfinance limit)
    # ──────────────────────────────────────────────────────────────
    print_section(f"RUN 2: {symbol} H1 — ~2 YEARS — LONDON SESSION GATE")
    h1_bars = fetch_yf_bars("GC=F", "1h", "2y")
    if h1_bars:
        print(f"[INFO] Running RAW mode on {len(h1_bars)} H1 bars...")
        h1_raw = run_deterministic_backtest(symbol, h1_bars, session_window="LONDON", reality=False)
        print(f"[INFO] Running REALITY mode on {len(h1_bars)} H1 bars...")
        h1_reality = run_deterministic_backtest(symbol, h1_bars, session_window="LONDON", reality=True)
        print_summary(h1_raw, h1_reality)
        all_results["h1_2yr"] = {
            "bars": len(h1_bars),
            "session": "LONDON",
            "raw": {
                "signals": h1_raw.total_signals,
                "filled": h1_raw.filled_trades,
                "wins": h1_raw.wins,
                "losses": h1_raw.losses,
                "breakevens": h1_raw.breakevens,
                "win_rate_pct": round(h1_raw.win_rate * 100, 2),
                "avg_win_pips": h1_raw.avg_win,
                "avg_loss_pips": h1_raw.avg_loss,
                "avg_rr": h1_raw.avg_rr,
                "expectancy_pips": h1_raw.expectancy,
                "total_profit_pips": h1_raw.total_profit_raw,
            },
            "reality": {
                "signals": h1_reality.total_signals,
                "filled": h1_reality.filled_trades,
                "wins": h1_reality.wins,
                "losses": h1_reality.losses,
                "breakevens": h1_reality.breakevens,
                "win_rate_pct": round(h1_reality.win_rate * 100, 2),
                "avg_win_pips": h1_reality.avg_win,
                "avg_loss_pips": h1_reality.avg_loss,
                "avg_rr": h1_reality.avg_rr,
                "expectancy_pips": h1_reality.expectancy,
                "total_profit_pips": h1_reality.total_profit_net,
            },
            "trades": h1_reality.trades[:500],
        }
    else:
        print("[SKIP] No H1 data available.")

    # ──────────────────────────────────────────────────────────────
    # EXPORT
    # ──────────────────────────────────────────────────────────────
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "runs": all_results,
    }
    save_results(payload)


if __name__ == "__main__":
    main()
