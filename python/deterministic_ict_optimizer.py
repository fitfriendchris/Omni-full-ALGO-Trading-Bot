#!/usr/bin/env python3
"""
deterministic_ict_optimizer.py — Systematic parameter sweep on real H1+D1 data
Tests combinations of execution/SL/session fixes and reports best reality-adjusted config.

Grid parameters:
  execution_mode  : LIMIT_THEN_MARKET, MARKET_ONLY
  sl_cap_pips     : 50, 100, 200, None
  fill_window     : 25, 48, 96
  session         : LONDON, NY, SILVER_BULLET, ALL (LONDON+NY+SILVER)
  min_rr          : 1.5, 2.0
"""
from __future__ import annotations

import itertools, json, math, os, random, sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import yfinance as yf

from deterministic_ict_engine import Bar, DeterministicICTEngine, Signal
from deterministic_honest_backtest import (
    BacktestResult, Trade, _commission, _pip_value,
)

HERE: Path = Path(__file__).parent
RESULTS_PATH: Path = HERE / "optimizer_results.json"

# ── Parameter grid ──────────────────────────────────────────────────────────────
# Focused grid targeting the biggest reality-impact levers
GRID = {
    "execution_mode": ["LIMIT_THEN_MARKET", "MARKET_ONLY"],
    "sl_cap_pips": [100.0, 200.0, None],   # 100 and 200 caps vs no cap
    "fill_window": [48, 96],                  # 48h and 96h windows (improve fill rate)
    "session": ["LONDON", "ALL"],             # London only vs multi-session
    "min_rr": [1.5, 2.0],
}

# Cap combos to avoid combinatorial explosion
MAX_COMBOS = 48  # 2*3*2*2*2 = 24 → well within limit


@dataclass
class ExecutionConfig:
    execution_mode: str       # LIMIT_ONLY, LIMIT_THEN_MARKET, MARKET_ONLY
    sl_cap_pips: Optional[float]
    fill_window: int
    session: str
    min_rr: float

    def key(self) -> str:
        return f"{self.execution_mode}_SL{self.sl_cap_pips}_FW{self.fill_window}_S{self.session}_RR{self.min_rr}"


# ── Data fetch (once) ───────────────────────────────────────────────────────────
def fetch_once(ticker: str, interval: str, period: str) -> list[Bar]:
    print(f"[FETCH] {ticker} {interval} {period} ...")
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    bars: list[Bar] = []
    for i, (ts, row) in enumerate(df.iterrows()):
        ts_val = pd.Timestamp(ts).timestamp()
        bars.append(Bar(
            idx=i, time=str(ts)[:19],
            o=float(row["Open"]), h=float(row["High"]),
            l=float(row["Low"]), c=float(row["Close"]),
            v=int(row.get("Volume", 0)), broker_ts=float(ts_val),
        ))
    print(f"[FETCH] {len(bars)} bars")
    return bars


def align_htf_to_ltf(ltf: list[Bar], htf: list[Bar]) -> list[int]:
    htf_ts = [b.broker_ts for b in htf]
    mapping: list[int] = []
    h = 0
    for lb in ltf:
        while h < len(htf_ts) and htf_ts[h] <= lb.broker_ts:
            h += 1
        mapping.append(h - 1)
    return mapping


# ── Session gate (expanded) ────────────────────────────────────────────────────
def _in_session(broker_ts: float, session: str) -> bool:
    """True if broker_ts falls in the requested session(s)."""
    if broker_ts <= 0:
        return True
    try:
        from datetime import timezone as _tz
        dt = datetime.fromtimestamp(broker_ts, tz=_tz.utc)
        h = dt.hour
        if session == "LONDON":
            return 7 <= h < 10
        if session == "NY":
            return 12 <= h < 15
        if session == "SILVER_BULLET":
            return 13 <= h < 17
        if session == "ALL":
            return (7 <= h < 10) or (12 <= h < 15) or (13 <= h < 17)
        return True
    except Exception:
        return True


# ── Simulate fills with market fallback ───────────────────────────────────────
def simulate_fill(
    sig: Signal, bars: list[Bar], start_idx: int, cfg: ExecutionConfig, symbol: str
) -> tuple[Optional[float], Optional[int]]:
    """
    Returns (filled_price, filled_bar_idx) or (None, None).
    LIMIT_THEN_MARKET: try limit for fill_window bars, then market on next bar.
    MARKET_ONLY: market on next bar open.
    """
    entry = sig.entry_price
    if entry is None:
        return None, None

    if cfg.execution_mode == "MARKET_ONLY":
        # Market fill at next bar open, with slippage
        if start_idx < len(bars):
            return bars[start_idx].o, start_idx
        return None, None

    # LIMIT_THEN_MARKET
    for i in range(start_idx, min(start_idx + cfg.fill_window, len(bars))):
        b = bars[i]
        if b.l <= entry <= b.h:
            return entry, i
    # Gap-through market fallback: if price blew past the level, market at next bar
    if start_idx + cfg.fill_window < len(bars):
        next_bar = bars[start_idx + cfg.fill_window]
        # Directional gap-through check
        if sig.direction == "BULL" and next_bar.o > entry:
            return next_bar.o, start_idx + cfg.fill_window
        if sig.direction == "BEAR" and next_bar.o < entry:
            return next_bar.o, start_idx + cfg.fill_window
    return None, None


# ── Simulate single trade with SL cap ──────────────────────────────────────────
def run_single_trade(
    sig: Signal, bars: list[Bar], fill_idx: int, cfg: ExecutionConfig, reality: bool
) -> Trade:
    sym = sig.symbol
    direction = sig.direction
    entry_raw = sig.entry_price
    sl_raw = sig.sl
    tp1 = sig.tp
    tp2 = sig.tp2
    pip = _pip_value(sym)

    # --- SL cap ---
    max_sl_dist = 99999.0  # pips
    if cfg.sl_cap_pips is not None:
        max_sl_dist = cfg.sl_cap_pips
    sl_dist_pips = abs(entry_raw - sl_raw) / pip
    if sl_dist_pips > max_sl_dist:
        # Tighten SL toward entry
        if direction == "BULL":
            sl_raw = entry_raw - (max_sl_dist * pip)
        else:
            sl_raw = entry_raw + (max_sl_dist * pip)

    slip = 0.0
    if reality:
        slip_pips = random.uniform(0.5, 2.0)  # market execution has tighter spread but slippage
        slip = slip_pips * pip
        # Market mode: tighter entry slippage (0.5-2 pips vs 0-2 for limit)
        # But wider on exit (1-3 pips)

    if direction == "BULL":
        entry_filled = entry_raw + slip
        sl_filled = sl_raw - slip
    else:
        entry_filled = entry_raw - slip
        sl_filled = sl_raw + slip

    comm = _commission(sym) * 0.01 if reality else 0.0

    exit_price: Optional[float] = None
    exit_bar: Optional[int] = None
    exit_reason = ""
    max_hold = 200

    for j in range(fill_idx + 1, min(fill_idx + max_hold, len(bars))):
        b = bars[j]
        if direction == "BULL":
            if b.l <= sl_raw:
                exit_slip = 0.0
                if reality:
                    exit_slip = random.uniform(1.0, 3.0) * pip
                exit_price = sl_raw - exit_slip
                exit_bar = j
                exit_reason = "SL"
                break
            if tp1 is not None and b.h >= tp1:
                exit_slip = 0.0
                if reality:
                    exit_slip = random.uniform(1.0, 3.0) * pip
                exit_price = tp1 - exit_slip
                exit_bar = j
                exit_reason = "TP1"
                break
        else:
            if b.h >= sl_raw:
                exit_slip = 0.0
                if reality:
                    exit_slip = random.uniform(1.0, 3.0) * pip
                exit_price = sl_raw + exit_slip
                exit_bar = j
                exit_reason = "SL"
                break
            if tp1 is not None and b.l <= tp1:
                exit_slip = 0.0
                if reality:
                    exit_slip = random.uniform(1.0, 3.0) * pip
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


# ── Run backtest for a single config ───────────────────────────────────────────
def run_config(cfg: ExecutionConfig, ltf: list[Bar], htf: list[Bar], htf_map: list[int], reality: bool) -> BacktestResult:
    engine = DeterministicICTEngine(
        session_window=cfg.session,
        max_spread_pips=50.0,
        min_confidence=0.55,
        min_rr=cfg.min_rr,
        require_htf_alignment=True,
    )
    result = BacktestResult()
    if len(ltf) < 55:
        return result

    def _to_raw(bb: list[Bar]):
        return [{"time": b.time, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "broker_ts": b.broker_ts} for b in bb]

    MAX_LTF = 500
    MAX_HTF = 500

    for i in range(50, len(ltf) - 1):
        l_start = max(0, i + 1 - MAX_LTF)
        ltf_window = ltf[l_start:i + 1]
        h_end = htf_map[i]
        if h_end < 0:
            continue
        h_start = max(0, h_end + 1 - MAX_HTF)
        htf_window = htf[h_start:h_end + 1]

        try:
            sigs = engine.generate_signals("XAUUSD", _to_raw(htf_window), _to_raw(ltf_window), broker_ts=ltf[i].broker_ts)
        except Exception:
            continue

        for sig in sigs:
            # Session override for combined session
            if cfg.session == "ALL" and not _in_session(ltf[i].broker_ts, cfg.session):
                continue

            result.total_signals += 1
            fill_price, fill_idx = simulate_fill(sig, ltf, i + 1, cfg, sig.symbol)
            if fill_price is None:
                continue

            trade = run_single_trade(sig, ltf, fill_idx, cfg, reality)
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
            result.trades.append({
                "symbol": trade.symbol, "direction": trade.direction,
                "entry": trade.entry_price_raw, "sl": trade.sl_raw,
                "tp1": trade.tp1_raw, "fill_bar": trade.fill_bar,
                "exit_bar": trade.exit_bar, "exit_reason": trade.exit_reason,
                "profit_raw_pips": trade.profit_raw,
                "profit_net_pips": trade.profit_net,
                "commission": trade.commission_usd,
                "slippage_pips": trade.slippage_pips,
                "reality_mode": reality,
                "session": cfg.session,
            })

    if result.filled_trades > 0:
        result.win_rate = result.wins / result.filled_trades
        wins = [t["profit_net_pips"] for t in result.trades if t["profit_net_pips"] > 0]
        losses = [t["profit_net_pips"] for t in result.trades if t["profit_net_pips"] < 0]
        result.avg_win = sum(wins) / len(wins) if wins else 0
        result.avg_loss = sum(losses) / len(losses) if losses else 0
        result.avg_rr = abs(result.avg_win / result.avg_loss) if result.avg_loss != 0 else 0
        result.expectancy = (result.win_rate * result.avg_win) + ((1 - result.win_rate) * result.avg_loss)
    return result


def score(result: BacktestResult) -> float:
    """Scoring: must have >=20 filled trades and positive expectancy to be viable."""
    if result.filled_trades < 20:
        return -9999.0
    return result.expectancy


def main():
    symbol = "XAUUSD"
    print("="*72)
    print("  DETERMINISTIC ICT OPTIMIZER — SYSTEMATIC PARAMETER SWEEP")
    print("="*72)

    # Fetch H1 + D1 data once
    h1 = fetch_once("GC=F", "1h", "2y")
    d1 = fetch_once("GC=F", "1d", "5y")
    if not h1 or not d1:
        print("[FATAL] No data")
        return

    htf_map = align_htf_to_ltf(h1, d1)

    # Build all combos
    keys = list(GRID.keys())
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    if len(combos) > MAX_COMBOS:
        random.shuffle(combos)
        combos = combos[:MAX_COMBOS]
    print(f"\n[INFO] Testing {len(combos)} configurations...")

    best_raw: Optional[tuple] = None
    best_reality: Optional[tuple] = None
    all_results: list[dict] = []

    for idx, values in enumerate(combos):
        params = dict(zip(keys, values))
        cfg = ExecutionConfig(
            execution_mode=params["execution_mode"],
            sl_cap_pips=params["sl_cap_pips"],
            fill_window=params["fill_window"],
            session=params["session"],
            min_rr=params["min_rr"],
        )
        label = cfg.key()
        print(f"\n[{idx+1}/{len(combos)}] {label}")

        raw_res = run_config(cfg, h1, d1, htf_map, reality=False)
        reality_res = run_config(cfg, h1, d1, htf_map, reality=True)

        score_raw = score(raw_res)
        score_reality = score(reality_res)

        if best_raw is None or score_raw > best_raw[0]:
            best_raw = (score_raw, cfg, raw_res)
        if best_reality is None or score_reality > best_reality[0]:
            best_reality = (score_reality, cfg, reality_res)

        entry = {
            "config": {
                "execution_mode": cfg.execution_mode,
                "sl_cap_pips": cfg.sl_cap_pips,
                "fill_window": cfg.fill_window,
                "session": cfg.session,
                "min_rr": cfg.min_rr,
            },
            "raw": {
                "signals": raw_res.total_signals, "filled": raw_res.filled_trades,
                "wins": raw_res.wins, "losses": raw_res.losses, "be": raw_res.breakevens,
                "win_rate_pct": round(raw_res.win_rate*100, 2),
                "avg_win": raw_res.avg_win, "avg_loss": raw_res.avg_loss,
                "avg_rr": raw_res.avg_rr, "expectancy": raw_res.expectancy,
                "total_pnl": raw_res.total_profit_net or raw_res.total_profit_raw,
            },
            "reality": {
                "signals": reality_res.total_signals, "filled": reality_res.filled_trades,
                "wins": reality_res.wins, "losses": reality_res.losses, "be": reality_res.breakevens,
                "win_rate_pct": round(reality_res.win_rate*100, 2),
                "avg_win": reality_res.avg_win, "avg_loss": reality_res.avg_loss,
                "avg_rr": reality_res.avg_rr, "expectancy": reality_res.expectancy,
                "total_pnl": reality_res.total_profit_net,
            },
        }
        all_results.append(entry)
        print(f"  RAW  : filled={raw_res.filled_trades}  WR={raw_res.win_rate*100:.1f}%  Exp={raw_res.expectancy:.1f}")
        print(f"  REAL : filled={reality_res.filled_trades}  WR={reality_res.win_rate*100:.1f}%  Exp={reality_res.expectancy:.1f}")

    # ── Summary ──
    print("\n" + "="*72)
    print("  OPTIMIZATION COMPLETE")
    print("="*72)

    if best_raw:
        _, cfg, res = best_raw
        print(f"\n  BEST RAW CONFIG: {cfg.key()}")
        print(f"    Filled: {res.filled_trades} | WR: {res.win_rate*100:.1f}% | Expectancy: {res.expectancy:.1f} pips")

    if best_reality:
        _, cfg, res = best_reality
        print(f"\n  BEST REALITY CONFIG: {cfg.key()}")
        print(f"    Filled: {res.filled_trades} | WR: {res.win_rate*100:.1f}% | Expectancy: {res.expectancy:.1f} pips")
        print(f"    Total PnL: {res.total_profit_net:.0f} pips over {res.filled_trades} trades")

    # Sort all by reality expectancy
    all_results.sort(key=lambda x: x["reality"]["expectancy"], reverse=True)
    top10 = all_results[:10]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "data": {"h1_bars": len(h1), "d1_bars": len(d1)},
        "best_raw_config": best_raw[1].key() if best_raw else None,
        "best_reality_config": best_reality[1].key() if best_reality else None,
        "top10": top10,
        "all_results": all_results,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n[OK] Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
