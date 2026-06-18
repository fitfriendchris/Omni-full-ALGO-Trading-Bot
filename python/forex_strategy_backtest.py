#!/usr/bin/env python3
"""
Forex Strategy Backtest Arena — 3 Strategies, 3 Symbols, 2023-2025 (H1)
=========================================================================

Strategies:
  1. NY-ORB (Opening Range Breakout) — momentum, session-based
  2. EMA-RSI Trend Pullback — trend following with mean-reversion entry
  3. ICT-SMC Structural Reversal — liquidity sweep + CHoCH (simplified)

Symbols: EURUSD, GBPUSD, XAUUSD
Timeframe: H1 (1-hour)

Metrics: Total Return, Sharpe Ratio, Max Drawdown, Win Rate, Profit Factor
Output: JSON results + PNG equity curves

Data:
  - XAUUSD: local hist_XAUUSD_h1.csv (real broker data, full history)
  - EURUSD, GBPUSD: yfinance 1h (last ~730 days)
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
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

INITIAL_EQUITY = 10000.0
RISK_PER_TRADE = 0.02          # 2% risk per trade
COMMISSION_PER_LOT = 7.0       # $7 round-turn per lot (forex)
COMMISSION_XAU = 10.0          # $10 round-turn for gold
SPREAD_PIPS_FX = 0.00015       # ~1.5 pips for forex majors
SPREAD_PIPS_XAU = 0.15         # ~$0.15 for XAUUSD
SLIPPAGE_PIPS = 0.00005        # 0.5 pip slippage forex
SLIPPAGE_XAU = 0.05            # $0.05 slippage gold

PIP_SIZE = {
    "EURUSD=X": 0.0001,
    "GBPUSD=X": 0.0001,
    "GC=F": 0.01,
}

SYMBOL_NAMES = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "GC=F": "XAUUSD",
}

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"

# ──────────────────────────────────────────────────────────────────────────────
# Data Loading
# ──────────────────────────────────────────────────────────────────────────────

def load_local_csv(symbol: str) -> pd.DataFrame:
    """Load local H1 CSV (from MT5/Dukascopy export)."""
    csv_path = DATA_DIR / f"hist_{symbol}_h1.csv"
    if not csv_path.exists():
        csv_path = DATA_DIR / f"hist_{symbol.lower()}_h1.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Local CSV not found: {csv_path}")

    print(f"[LOCAL] Loading {csv_path} ...")
    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]

    # Detect time column
    time_col = None
    for c in df.columns:
        if "time" in c:
            time_col = c
            break
    if time_col is None:
        raise ValueError("No time column found in CSV")

    df[time_col] = pd.to_datetime(df[time_col], format="%Y.%m.%d %H:%M:%S")
    df = df.set_index(time_col).sort_index()
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    print(f"[LOCAL] {len(df)} rows ({df.index[0]} → {df.index[-1]})")
    return df


def fetch_yf_hourly(symbol: str, period: str = "730d") -> pd.DataFrame:
    """Fetch last ~730 days of 1h data from yfinance."""
    print(f"[YF] Fetching {symbol} 1h ...")
    df = yf.download(symbol, period=period, interval="1h",
                     auto_adjust=True, progress=False, threads=False)
    if df is None or len(df) == 0:
        raise SystemExit(f"No hourly data for {symbol}")
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
    print(f"[YF] {len(df)} rows ({df.index[0]} → {df.index[-1]})")
    return df


def get_data(symbol: str, name: str) -> pd.DataFrame:
    if name == "XAUUSD":
        return load_local_csv("XAUUSD")
    return fetch_yf_hourly(symbol)


# ──────────────────────────────────────────────────────────────────────────────
# Indicators
# ──────────────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def atr_calc(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def rsi_calc(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ──────────────────────────────────────────────────────────────────────────────
# Strategy 1: NY Open Range Breakout (ORB)
# ──────────────────────────────────────────────────────────────────────────────

def strategy_orb(df: pd.DataFrame,
                 open_hour_utc: int = 13,
                 range_bars: int = 4,
                 rr: float = 4.0,
                 body_atr: float = 0.5,
                 stop_cap_atr: float = 2.5,
                 long_only: bool = True) -> pd.DataFrame:
    df = df.copy()
    df["hour"] = df.index.hour
    df["day"] = df.index.normalize()

    n = len(df)
    signals = pd.DataFrame(index=df.index)
    signals["long"] = False
    signals["short"] = False
    signals["entry_price"] = np.nan
    signals["sl"] = np.nan
    signals["tp"] = np.nan
    signals["stop_dist"] = np.nan

    a5 = atr_calc(df, 5)
    a14 = atr_calc(df, 14)

    days = df["day"].unique()
    for day in days:
        day_mask = df["day"] == day
        day_df = df[day_mask]
        if len(day_df) == 0:
            continue

        sess = day_df[day_df["hour"] >= open_hour_utc]
        if len(sess) < range_bars + 1:
            continue

        rng = sess.iloc[:range_bars]
        rhi = rng["High"].max()
        rlo = rng["Low"].min()
        rsize = rhi - rlo
        if rsize <= 0:
            continue

        trigger = sess.iloc[range_bars:]
        if len(trigger) == 0:
            continue

        fired = False
        for ts, row in trigger.iterrows():
            if fired:
                break
            if row["Close"] <= rhi:
                continue

            body = abs(row["Close"] - row["Open"])
            candle = max(row["High"] - row["Low"], 1e-9)
            if body < body_atr * a5.loc[ts] or body < 0.6 * candle:
                continue

            sd = min(rsize, stop_cap_atr * a14.loc[ts]) if stop_cap_atr > 0 else rsize
            entry = row["Close"]
            signals.loc[ts, "long"] = True
            signals.loc[ts, "entry_price"] = entry
            signals.loc[ts, "sl"] = entry - sd
            signals.loc[ts, "tp"] = entry + rr * sd
            signals.loc[ts, "stop_dist"] = sd
            fired = True

    return signals


# ──────────────────────────────────────────────────────────────────────────────
# Strategy 2: EMA + RSI Trend Pullback
# ──────────────────────────────────────────────────────────────────────────────

def strategy_ema_rsi(df: pd.DataFrame,
                     fast: int = 20,
                     slow: int = 50,
                     rsi_period: int = 14,
                     rsi_low: float = 35.0,
                     rsi_high: float = 65.0,
                     atr_mult: float = 1.5,
                     rr: float = 2.5) -> pd.DataFrame:
    df = df.copy()
    c = df["Close"]
    efast = ema(c, fast)
    eslow = ema(c, slow)
    r = rsi_calc(c, rsi_period)
    a = atr_calc(df, 14)

    n = len(df)
    signals = pd.DataFrame(index=df.index)
    signals["long"] = False
    signals["short"] = False
    signals["entry_price"] = np.nan
    signals["sl"] = np.nan
    signals["tp"] = np.nan
    signals["stop_dist"] = np.nan

    for i in range(slow + rsi_period + 5, n - 1):
        ts = df.index[i]
        ts_next = df.index[i + 1]

        trend_up = (c.iloc[i] > efast.iloc[i]) and (efast.iloc[i] > eslow.iloc[i])
        trend_down = (c.iloc[i] < efast.iloc[i]) and (efast.iloc[i] < eslow.iloc[i])

        # Require RSI pull back into zone and then reverse
        rsi_long = (r.iloc[i - 1] < rsi_low) and (r.iloc[i] > rsi_low) and trend_up
        rsi_short = (r.iloc[i - 1] > rsi_high) and (r.iloc[i] < rsi_high) and trend_down

        sd = a.iloc[i] * atr_mult
        if sd <= 0:
            continue

        if rsi_long:
            entry = c.iloc[i]
            signals.loc[ts_next, "long"] = True
            signals.loc[ts_next, "entry_price"] = entry
            signals.loc[ts_next, "sl"] = entry - sd
            signals.loc[ts_next, "tp"] = entry + rr * sd
            signals.loc[ts_next, "stop_dist"] = sd
        elif rsi_short:
            entry = c.iloc[i]
            signals.loc[ts_next, "short"] = True
            signals.loc[ts_next, "entry_price"] = entry
            signals.loc[ts_next, "sl"] = entry + sd
            signals.loc[ts_next, "tp"] = entry - rr * sd
            signals.loc[ts_next, "stop_dist"] = sd

    return signals


# ──────────────────────────────────────────────────────────────────────────────
# Strategy 3: ICT-SMC Simplified Structural Reversal
# ──────────────────────────────────────────────────────────────────────────────

def strategy_ict_smc(df: pd.DataFrame,
                     sweep_lookback: int = 8,
                     choch_lookback: int = 4,
                     sl_atr_mult: float = 1.0,
                     min_rr: float = 2.0,
                     kill_zones: Tuple[Tuple[int, int], ...] = ((7, 10), (12, 16))) -> pd.DataFrame:
    df = df.copy()
    df["hour"] = df.index.hour
    a = atr_calc(df, 14)

    n = len(df)
    signals = pd.DataFrame(index=df.index)
    signals["long"] = False
    signals["short"] = False
    signals["entry_price"] = np.nan
    signals["sl"] = np.nan
    signals["tp"] = np.nan
    signals["stop_dist"] = np.nan

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    hours = df["hour"].values

    def in_kill_zone(h):
        for start, end in kill_zones:
            if start <= h <= end:
                return True
        return False

    for i in range(sweep_lookback + choch_lookback + 5, n - 1):
        ts = df.index[i]
        ts_next = df.index[i + 1]

        if not in_kill_zone(hours[i]):
            continue

        recent_high = max(highs[i - sweep_lookback:i])
        recent_low = min(lows[i - sweep_lookback:i])

        sweep_high = (highs[i] > recent_high) and (closes[i] < opens[i]) and (closes[i] < recent_high)
        sweep_low = (lows[i] < recent_low) and (closes[i] > opens[i]) and (closes[i] > recent_low)

        choch_buy = closes[i] > max(highs[i - choch_lookback:i])
        choch_sell = closes[i] < min(lows[i - choch_lookback:i])

        sd = a.iloc[i] * sl_atr_mult
        if sd <= 0:
            continue

        if sweep_low and choch_buy:
            entry = closes[i]
            sl = recent_low - sd
            risk = entry - sl
            if risk <= 0:
                continue
            rr_actual = (entry + min_rr * risk - entry) / risk
            if rr_actual < min_rr:
                continue
            signals.loc[ts_next, "long"] = True
            signals.loc[ts_next, "entry_price"] = entry
            signals.loc[ts_next, "sl"] = sl
            signals.loc[ts_next, "tp"] = entry + min_rr * risk
            signals.loc[ts_next, "stop_dist"] = risk

        elif sweep_high and choch_sell:
            entry = closes[i]
            sl = recent_high + sd
            risk = sl - entry
            if risk <= 0:
                continue
            rr_actual = (entry - entry + min_rr * risk) / risk
            if rr_actual < min_rr:
                continue
            signals.loc[ts_next, "short"] = True
            signals.loc[ts_next, "entry_price"] = entry
            signals.loc[ts_next, "sl"] = sl
            signals.loc[ts_next, "tp"] = entry - min_rr * risk
            signals.loc[ts_next, "stop_dist"] = risk

    return signals


# ──────────────────────────────────────────────────────────────────────────────
# Backtest Executor
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_bars_held: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)


def run_backtest(df: pd.DataFrame,
                 signals: pd.DataFrame,
                 symbol: str,
                 strategy_name: str,
                 long_only: bool = False) -> BacktestResult:
    result = BacktestResult(strategy=strategy_name, symbol=symbol)
    equity = INITIAL_EQUITY
    equity_curve = [equity]
    trades = []

    pip = PIP_SIZE.get(symbol, 0.0001)
    comm = COMMISSION_XAU if "GC=F" in symbol else COMMISSION_PER_LOT
    spread = SPREAD_PIPS_XAU if "GC=F" in symbol else SPREAD_PIPS_FX
    slip = SLIPPAGE_XAU if "GC=F" in symbol else SLIPPAGE_PIPS

    pos = None

    for i in range(len(df) - 1):
        row = df.iloc[i + 1]
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]

        if pos is None:
            sig = signals.iloc[i]
            if sig["long"]:
                entry_raw = o
                entry_filled = entry_raw + spread + slip
                sd = sig["stop_dist"] if not pd.isna(sig["stop_dist"]) else 20 * pip
                sd = max(sd, pip)
                sl = sig["sl"] - slip if not pd.isna(sig["sl"]) else entry_filled - sd
                tp = sig["tp"] - spread if not pd.isna(sig["tp"]) else entry_filled + 40 * pip

                risk_amt = equity * RISK_PER_TRADE
                units = risk_amt / sd if sd > 0 else 0
                pos = {
                    "direction": "BUY",
                    "entry": entry_filled,
                    "sl": sl,
                    "tp": tp,
                    "units": units,
                    "entry_i": i + 1,
                    "rr": (tp - entry_filled) / max(entry_filled - sl, pip) if (entry_filled - sl) > 0 else 2.0,
                }
            elif sig["short"] and not long_only:
                entry_raw = o
                entry_filled = entry_raw - spread - slip
                sd = sig["stop_dist"] if not pd.isna(sig["stop_dist"]) else 20 * pip
                sd = max(sd, pip)
                sl = sig["sl"] + slip if not pd.isna(sig["sl"]) else entry_filled + sd
                tp = sig["tp"] + spread if not pd.isna(sig["tp"]) else entry_filled - 40 * pip

                risk_amt = equity * RISK_PER_TRADE
                units = risk_amt / sd if sd > 0 else 0
                pos = {
                    "direction": "SELL",
                    "entry": entry_filled,
                    "sl": sl,
                    "tp": tp,
                    "units": units,
                    "entry_i": i + 1,
                    "rr": (entry_filled - tp) / max(sl - entry_filled, pip) if (sl - entry_filled) > 0 else 2.0,
                }

        if pos is not None:
            exit_price = None
            exit_reason = ""

            if pos["direction"] == "BUY":
                if l <= pos["sl"]:
                    exit_price = pos["sl"] - slip
                    exit_reason = "SL"
                elif h >= pos["tp"]:
                    exit_price = pos["tp"] - slip
                    exit_reason = "TP"
            else:
                if h >= pos["sl"]:
                    exit_price = pos["sl"] + slip
                    exit_reason = "SL"
                elif l <= pos["tp"]:
                    exit_price = pos["tp"] + slip
                    exit_reason = "TP"

            # Time exit for ORB (max 24 bars ~ 1 day)
            if exit_reason == "" and strategy_name == "ORB":
                if (i + 1) - pos["entry_i"] >= 24:
                    exit_price = c - spread if pos["direction"] == "BUY" else c + spread
                    exit_reason = "TIME"

            if exit_price is not None:
                if pos["direction"] == "BUY":
                    pnl_raw = (exit_price - pos["entry"]) * pos["units"]
                else:
                    pnl_raw = (pos["entry"] - exit_price) * pos["units"]

                pnl_net = pnl_raw - max(comm * 0.01, abs(pnl_raw) * 0.001)
                equity += pnl_net
                equity_curve.append(equity)

                trades.append({
                    "direction": pos["direction"],
                    "entry": pos["entry"],
                    "exit": exit_price,
                    "sl": pos["sl"],
                    "tp": pos["tp"],
                    "pnl_raw": pnl_raw,
                    "pnl_net": pnl_net,
                    "reason": exit_reason,
                    "bars": (i + 1) - pos["entry_i"],
                    "rr_target": pos["rr"],
                })
                pos = None
            else:
                equity_curve.append(equity)

    if pos is not None:
        exit_price = df.iloc[-1]["Close"] - spread if pos["direction"] == "BUY" else df.iloc[-1]["Close"] + spread
        if pos["direction"] == "BUY":
            pnl_raw = (exit_price - pos["entry"]) * pos["units"]
        else:
            pnl_raw = (pos["entry"] - exit_price) * pos["units"]
        pnl_net = pnl_raw - max(comm * 0.01, abs(pnl_raw) * 0.001)
        equity += pnl_net
        equity_curve.append(equity)
        trades.append({
            "direction": pos["direction"],
            "entry": pos["entry"],
            "exit": exit_price,
            "sl": pos["sl"],
            "tp": pos["tp"],
            "pnl_raw": pnl_raw,
            "pnl_net": pnl_net,
            "reason": "CLOSE",
            "bars": len(df) - pos["entry_i"],
            "rr_target": pos["rr"],
        })

    result.total_trades = len(trades)
    wins = [t for t in trades if t["pnl_net"] > 0]
    losses = [t for t in trades if t["pnl_net"] <= 0]
    result.wins = len(wins)
    result.losses = len(losses)
    result.win_rate = len(wins) / len(trades) * 100 if trades else 0
    result.avg_win = float(np.mean([t["pnl_net"] for t in wins])) if wins else 0.0
    result.avg_loss = float(np.mean([t["pnl_net"] for t in losses])) if losses else 0.0
    result.avg_bars_held = float(np.mean([t["bars"] for t in trades])) if trades else 0.0
    result.total_return_pct = (equity - INITIAL_EQUITY) / INITIAL_EQUITY * 100

    total_profit = sum(t["pnl_net"] for t in wins)
    total_loss = abs(sum(t["pnl_net"] for t in losses))
    result.profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")

    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown_pct = max_dd

    returns = np.diff(equity_curve) / np.array(equity_curve[:-1])
    if len(returns) > 1 and np.std(returns) > 0:
        result.sharpe_ratio = float(np.mean(returns) / np.std(returns) * np.sqrt(252 * 6.5))
    else:
        result.sharpe_ratio = 0.0

    result.equity_curve = equity_curve
    result.trades = trades
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    symbols = {
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "XAUUSD": "GC=F",
    }

    all_results = []

    for name, ticker in symbols.items():
        print(f"\n{'='*60}")
        print(f"Symbol: {name} ({ticker})")
        print(f"{'='*60}")

        try:
            df = get_data(ticker, name)
        except Exception as e:
            print(f"[ERROR] Could not load data for {name}: {e}")
            continue

        # Strategy 1: ORB
        print(f"\n--- Running ORB ---")
        orb_signals = strategy_orb(df, long_only=True)
        orb_result = run_backtest(df, orb_signals, ticker, "ORB", long_only=True)
        print(f"  Trades: {orb_result.total_trades}, Wins: {orb_result.wins}, Losses: {orb_result.losses}")
        print(f"  Win Rate: {orb_result.win_rate:.1f}%, Return: {orb_result.total_return_pct:.1f}%")
        print(f"  Sharpe: {orb_result.sharpe_ratio:.2f}, Max DD: {orb_result.max_drawdown_pct:.1f}%")
        print(f"  Profit Factor: {orb_result.profit_factor:.2f}")
        all_results.append(orb_result)

        # Strategy 2: EMA_RSI
        print(f"\n--- Running EMA_RSI ---")
        ema_signals = strategy_ema_rsi(df)
        ema_result = run_backtest(df, ema_signals, ticker, "EMA_RSI", long_only=False)
        print(f"  Trades: {ema_result.total_trades}, Wins: {ema_result.wins}, Losses: {ema_result.losses}")
        print(f"  Win Rate: {ema_result.win_rate:.1f}%, Return: {ema_result.total_return_pct:.1f}%")
        print(f"  Sharpe: {ema_result.sharpe_ratio:.2f}, Max DD: {ema_result.max_drawdown_pct:.1f}%")
        print(f"  Profit Factor: {ema_result.profit_factor:.2f}")
        all_results.append(ema_result)

        # Strategy 3: ICT_SMC
        print(f"\n--- Running ICT_SMC ---")
        ict_signals = strategy_ict_smc(df)
        ict_result = run_backtest(df, ict_signals, ticker, "ICT_SMC", long_only=False)
        print(f"  Trades: {ict_result.total_trades}, Wins: {ict_result.wins}, Losses: {ict_result.losses}")
        print(f"  Win Rate: {ict_result.win_rate:.1f}%, Return: {ict_result.total_return_pct:.1f}%")
        print(f"  Sharpe: {ict_result.sharpe_ratio:.2f}, Max DD: {ict_result.max_drawdown_pct:.1f}%")
        print(f"  Profit Factor: {ict_result.profit_factor:.2f}")
        all_results.append(ict_result)

    # Pick winner: highest Sharpe with lowest Max DD
    print(f"\n{'='*60}")
    print("LEADERBOARD")
    print(f"{'='*60}")
    scored = []
    for r in all_results:
        if r.total_trades == 0:
            continue
        if r.max_drawdown_pct > 0:
            score = r.sharpe_ratio / (1 + r.max_drawdown_pct / 100)
        else:
            score = r.sharpe_ratio
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    for rank, (score, r) in enumerate(scored[:5], 1):
        sym = SYMBOL_NAMES.get(r.symbol, r.symbol)
        print(f"{rank}. {r.strategy} ({sym}) | Return: {r.total_return_pct:.1f}% | "
              f"Sharpe: {r.sharpe_ratio:.2f} | MaxDD: {r.max_drawdown_pct:.1f}% | "
              f"PF: {r.profit_factor:.2f} | Score: {score:.3f}")

    if not scored:
        print("No valid results.")
        return

    winner = scored[0][1]
    print(f"\n🏆 WINNER: {winner.strategy} on {SYMBOL_NAMES.get(winner.symbol, winner.symbol)}")

    # Save JSON results
    results_json = []
    for score, r in scored:
        results_json.append({
            "strategy": r.strategy,
            "symbol": SYMBOL_NAMES.get(r.symbol, r.symbol),
            "total_trades": r.total_trades,
            "win_rate_pct": round(r.win_rate, 2),
            "total_return_pct": round(r.total_return_pct, 2),
            "sharpe_ratio": round(r.sharpe_ratio, 3),
            "max_drawdown_pct": round(r.max_drawdown_pct, 2),
            "profit_factor": round(r.profit_factor, 3),
            "avg_win": round(r.avg_win, 2),
            "avg_loss": round(r.avg_loss, 2),
            "avg_bars_held": round(r.avg_bars_held, 1),
            "score": round(score, 3),
        })

    out_path = HERE / "forex_backtest_results.json"
    with open(out_path, "w") as f:
        json.dump(results_json, f, indent=2)
    print(f"\nResults saved to: {out_path}")

    # Generate equity curve plot
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        valid = [r for r in all_results if r.total_trades > 0]
        n_strats = 3
        n_syms = len({r.symbol for r in valid})

        fig, axes = plt.subplots(n_strats, n_syms, figsize=(16, 12))
        fig.suptitle("Forex Strategy Backtest Results 2023-2025", fontsize=14, fontweight="bold")

        strat_map = {"ORB": 0, "EMA_RSI": 1, "ICT_SMC": 2}
        sym_map = {s: i for i, s in enumerate(symbols.values())}

        for r in valid:
            ax = axes[strat_map[r.strategy], sym_map[r.symbol]]
            ax.plot(r.equity_curve, linewidth=1.2)
            ax.set_title(f"{r.strategy} | {SYMBOL_NAMES.get(r.symbol, r.symbol)}")
            ax.set_ylabel("Equity ($)")
            ax.grid(True, alpha=0.3)

        plt.tight_layout()
        png_path = HERE / "forex_backtest_equity_curves.png"
        plt.savefig(png_path, dpi=150)
        print(f"Equity curves saved to: {png_path}")

        # Winner plot
        fig2, ax2 = plt.subplots(figsize=(12, 6))
        ax2.plot(winner.equity_curve, linewidth=1.5, color="green")
        ax2.fill_between(range(len(winner.equity_curve)), winner.equity_curve, INITIAL_EQUITY,
                         alpha=0.3, color="green")
        ax2.axhline(INITIAL_EQUITY, color="gray", linestyle="--", alpha=0.5)
        ax2.set_title(f"WINNER: {winner.strategy} on {SYMBOL_NAMES.get(winner.symbol, winner.symbol)}\n"
                     f"Return: {winner.total_return_pct:.1f}% | Sharpe: {winner.sharpe_ratio:.2f} | "
                     f"MaxDD: {winner.max_drawdown_pct:.1f}%", fontweight="bold")
        ax2.set_ylabel("Equity ($)")
        ax2.set_xlabel("Bar")
        ax2.grid(True, alpha=0.3)
        winner_png = HERE / "forex_winner_equity_curve.png"
        plt.savefig(winner_png, dpi=150)
        print(f"Winner equity curve saved to: {winner_png}")
    except Exception as e:
        print(f"Plotting error: {e}")


if __name__ == "__main__":
    main()
