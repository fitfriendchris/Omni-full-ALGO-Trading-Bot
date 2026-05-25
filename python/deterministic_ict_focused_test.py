#!/usr/bin/env python3
"""
deterministic_ict_focused_test.py — 6 targeted configs to find reality-profitability
Pre-fetches data once, runs 6 configs sequentially, reports honest results.
"""
from __future__ import annotations

import itertools, json, math, os, random, sys, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import yfinance as yf

from deterministic_ict_engine import Bar, DeterministicICTEngine, Signal
from deterministic_honest_backtest import _commission, _pip_value

HERE: Path = Path(__file__).parent
RESULTS_PATH: Path = HERE / "focused_test_results.json"


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


@dataclass
class Config:
    name: str
    execution_mode: str   # LIMIT_ONLY, LIMIT_THEN_MARKET, MARKET_ONLY
    sl_cap_pips: Optional[float]
    fill_window: int
    session: str
    min_rr: float


# ── 6 configs: baseline + market execution + SL cap + wider sessions ────────────
CONFIGS = [
    Config("BASELINE (original)", "LIMIT_ONLY", None, 25, "LONDON", 2.0),
    Config("MARKET + 200SL + 48fw + LONDON", "MARKET_ONLY", 200.0, 48, "LONDON", 1.5),
    Config("MARKET + 200SL + 96fw + ALL", "MARKET_ONLY", 200.0, 96, "ALL", 1.5),
    Config("MARKET + 100SL + 48fw + ALL", "MARKET_ONLY", 100.0, 48, "ALL", 1.5),
    Config("LIMIT_THEN_MARKET + 200SL + 96fw + ALL", "LIMIT_THEN_MARKET", 200.0, 96, "ALL", 1.5),
    Config("MARKET + 200SL + 96fw + LONDON", "MARKET_ONLY", 200.0, 96, "LONDON", 2.0),
]


def _in_session(broker_ts: float, session: str) -> bool:
    if broker_ts <= 0:
        return True
    try:
        dt = datetime.fromtimestamp(broker_ts, tz=timezone.utc)
        h = dt.hour
        if session == "LONDON":
            return 7 <= h < 10
        if session == "NY":
            return 12 <= h < 15
        if session == "SILVER_BULLET":
            return 13 <= h < 17
        if session == "ALL":
            return True  # optimizer uses _in_session override in loop
        return True
    except Exception:
        return True


def simulate_fill(sig: Signal, bars: list[Bar], start_idx: int, cfg: Config, symbol: str):
    entry = sig.entry_price
    if entry is None:
        return None, None
    if cfg.execution_mode == "MARKET_ONLY":
        if start_idx < len(bars):
            return bars[start_idx].o, start_idx
        return None, None
    # LIMIT: try for fill_window bars
    for i in range(start_idx, min(start_idx + cfg.fill_window, len(bars))):
        b = bars[i]
        if b.l <= entry <= b.h:
            return entry, i
    # LIMIT_THEN_MARKET: fallback
    if cfg.execution_mode == "LIMIT_THEN_MARKET" and start_idx + cfg.fill_window < len(bars):
        nb = bars[start_idx + cfg.fill_window]
        if sig.direction == "BULL" and nb.o > entry:
            return nb.o, start_idx + cfg.fill_window
        if sig.direction == "BEAR" and nb.o < entry:
            return nb.o, start_idx + cfg.fill_window
    return None, None


def run_trade(sig: Signal, bars: list[Bar], fill_idx: int, cfg: Config, reality: bool):
    sym = sig.symbol
    direction = sig.direction
    entry_raw = sig.entry_price
    sl_raw = sig.sl
    tp1 = sig.tp
    pip = _pip_value(sym)

    # SL cap — must also rescale TP1 to maintain intended RR
    if cfg.sl_cap_pips is not None:
        max_dist = cfg.sl_cap_pips
        dist = abs(entry_raw - sl_raw) / pip
        if dist > max_dist:
            ratio = max_dist / dist
            # Scale SL toward entry
            if direction == "BULL":
                sl_raw = entry_raw - (max_dist * pip)
            else:
                sl_raw = entry_raw + (max_dist * pip)
            # Scale TP1 proportionally (maintaining engine's intended RR ratio)
            if tp1 is not None:
                tp_dist = abs(tp1 - entry_raw)
                if direction == "BULL":
                    tp1 = entry_raw + (tp_dist * ratio)
                else:
                    tp1 = entry_raw - (tp_dist * ratio)

    slip = 0.0
    if reality:
        slip_pips = random.uniform(0.5, 2.0)
        slip = slip_pips * pip

    if direction == "BULL":
        entry_filled = entry_raw + slip
        sl_filled = sl_raw - slip
    else:
        entry_filled = entry_raw - slip
        sl_filled = sl_raw + slip

    comm = _commission(sym) * 0.01 if reality else 0.0

    exit_price = None
    exit_bar = None
    exit_reason = ""
    for j in range(fill_idx + 1, min(fill_idx + 200, len(bars))):
        b = bars[j]
        if direction == "BULL":
            if b.l <= sl_raw:
                exit_slip = random.uniform(1.0, 3.0) * pip if reality else 0.0
                exit_price = sl_raw - exit_slip
                exit_bar = j
                exit_reason = "SL"
                break
            if tp1 is not None and b.h >= tp1:
                exit_slip = random.uniform(1.0, 3.0) * pip if reality else 0.0
                exit_price = tp1 - exit_slip
                exit_bar = j
                exit_reason = "TP1"
                break
        else:
            if b.h >= sl_raw:
                exit_slip = random.uniform(1.0, 3.0) * pip if reality else 0.0
                exit_price = sl_raw + exit_slip
                exit_bar = j
                exit_reason = "SL"
                break
            if tp1 is not None and b.l <= tp1:
                exit_slip = random.uniform(1.0, 3.0) * pip if reality else 0.0
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

    return {
        "symbol": sym, "direction": direction,
        "entry": round(entry_raw, 5), "sl": round(sl_raw, 5),
        "tp1": round(tp1, 5) if tp1 else None,
        "fill_bar": fill_idx, "exit_bar": exit_bar,
        "exit_reason": exit_reason,
        "profit_raw_pips": round(profit_pips_raw, 2),
        "profit_net_pips": round(profit_pips_net, 2),
        "commission": round(comm, 2),
        "slippage_pips": round(slip_pips, 2),
    }


def run_config(cfg: Config, ltf: list[Bar], htf: list[Bar], htf_map: list[int], reality: bool):
    engine = DeterministicICTEngine(
        session_window=cfg.session,
        max_spread_pips=50.0,
        min_confidence=0.55,
        min_rr=cfg.min_rr,
        require_htf_alignment=True,
    )
    result = {"signals": 0, "filled": 0, "wins": 0, "losses": 0, "be": 0,
              "total_raw_pnl": 0.0, "total_net_pnl": 0.0, "trades": []}
    if len(ltf) < 55:
        return result

    def _to_raw(bb):
        return [{"time": b.time, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "broker_ts": b.broker_ts} for b in bb]

    for i in range(50, len(ltf) - 1):
        l_start = max(0, i + 1 - 500)
        ltf_window = ltf[l_start:i + 1]
        h_end = htf_map[i]
        if h_end < 0:
            continue
        h_start = max(0, h_end + 1 - 500)
        htf_window = htf[h_start:h_end + 1]

        try:
            sigs = engine.generate_signals("XAUUSD", _to_raw(htf_window), _to_raw(ltf_window), broker_ts=ltf[i].broker_ts)
        except Exception:
            continue

        for sig in sigs:
            # Session override for ALL
            if cfg.session == "ALL" and not _in_session(ltf[i].broker_ts, "ALL"):
                continue

            result["signals"] += 1
            fill_price, fill_idx = simulate_fill(sig, ltf, i + 1, cfg, sig.symbol)
            if fill_price is None:
                continue

            trade = run_trade(sig, ltf, fill_idx, cfg, reality)
            result["filled"] += 1
            if trade["exit_reason"] == "TP1":
                result["wins"] += 1
            elif trade["exit_reason"] == "SL":
                result["losses"] += 1
            else:
                if trade["profit_net_pips"] > 0:
                    result["wins"] += 1
                elif trade["profit_net_pips"] < 0:
                    result["losses"] += 1
                else:
                    result["be"] += 1
            result["total_raw_pnl"] += trade["profit_raw_pips"]
            result["total_net_pnl"] += trade["profit_net_pips"]
            result["trades"].append(trade)

    ft = result["filled"]
    if ft > 0:
        wr = result["wins"] / ft
        wins = [t["profit_net_pips"] for t in result["trades"] if t["profit_net_pips"] > 0]
        losses = [t["profit_net_pips"] for t in result["trades"] if t["profit_net_pips"] < 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        avg_rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        exp = (wr * avg_win) + ((1 - wr) * avg_loss)
        result["win_rate_pct"] = round(wr * 100, 1)
        result["avg_win"] = round(avg_win, 2)
        result["avg_loss"] = round(avg_loss, 2)
        result["avg_rr"] = round(avg_rr, 2)
        result["expectancy"] = round(exp, 2)
    else:
        result["win_rate_pct"] = 0.0
        result["avg_win"] = 0.0
        result["avg_loss"] = 0.0
        result["avg_rr"] = 0.0
        result["expectancy"] = 0.0

    return result


def main():
    print("=" * 72)
    print("  FOCUSED 6-CONFIG COMPARISON — REAL H1+D1 DATA")
    print("=" * 72)

    h1 = fetch_once("GC=F", "1h", "2y")
    d1 = fetch_once("GC=F", "1d", "5y")
    htf_map = align_htf_to_ltf(h1, d1)

    all_results = []
    for idx, cfg in enumerate(CONFIGS):
        t0 = time.time()
        print(f"\n[{idx+1}/{len(CONFIGS)}] {cfg.name}")
        raw = run_config(cfg, h1, d1, htf_map, reality=False)
        reality = run_config(cfg, h1, d1, htf_map, reality=True)
        elapsed = time.time() - t0
        print(f"  RAW   : filled={raw['filled']}  WR={raw['win_rate_pct']}%  Exp={raw['expectancy']}  PnL={raw['total_raw_pnl']:.0f}")
        print(f"  REAL  : filled={reality['filled']}  WR={reality['win_rate_pct']}%  Exp={reality['expectancy']}  PnL={reality['total_net_pnl']:.0f}")
        print(f"  TIME  : {elapsed:.1f}s")
        all_results.append({
            "name": cfg.name,
            "config": {
                "execution_mode": cfg.execution_mode,
                "sl_cap_pips": cfg.sl_cap_pips,
                "fill_window": cfg.fill_window,
                "session": cfg.session,
                "min_rr": cfg.min_rr,
            },
            "raw": raw,
            "reality": reality,
        })

    # Sort by reality expectancy
    all_results.sort(key=lambda x: x["reality"]["expectancy"], reverse=True)

    print("\n" + "=" * 72)
    print("  RANKED BY REALITY EXPECTANCY")
    print("=" * 72)
    for i, r in enumerate(all_results):
        re = r["reality"]
        print(f"\n  #{i+1}: {r['name']}")
        print(f"       Filled: {re['filled']} | WR: {re['win_rate_pct']}% | Exp: {re['expectancy']} pips | PnL: {re['total_net_pnl']:.0f}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": "XAUUSD",
        "ranked": all_results,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n[OK] Results saved to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
