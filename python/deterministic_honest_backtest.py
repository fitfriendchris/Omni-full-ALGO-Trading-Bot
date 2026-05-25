"""
deterministic_honest_backtest.py — Honest walk-forward backtest for deterministic_ict_engine

Key rules (per user demand: NEVER show fantasy numbers without reality check):
  - RAW mode      : instant market fills, zero commission, zero slippage
  - REALITY mode  : limit-fill simulation + commission + slippage

Commission:
  - XAUUSD / XAGUSD : $10 round-turn per lot
  - Forex pairs     : $7 round-turn per lot

Slippage (applied AGAINST the trade):
  - Entry: random 0–2 pips
  - Exit : random 0–3 pips

Limit fill simulation:
  - Buy limit : price must trade AT or BELOW entry_price on a subsequent bar
  - Sell limit: price must trade AT or ABOVE entry_price on a subsequent bar
  - Gaps through the level without touching = NO FILL (skipped)
  - Fill window: signal valid for up to 25 bars (configurable)

NOTE: Walk-forward means we evaluate the engine on bars[0:i+1] at every index i,
then check subsequent bars for fill conditions. No future data leakage.
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

from deterministic_ict_engine import Bar, DeterministicICTEngine, Signal

HERE: Path = Path(__file__).parent
PROJECT: Path = HERE.parent
RESULTS_PATH: Path = HERE / "deterministic_backtest_results.json"

# ── Commission table (round-turn per lot, in account currency) ────────────────
COMMISSION: dict[str, float] = {"XAUUSD": 10.0, "XAGUSD": 10.0, "NAS100": 10.0, "US30": 10.0}
DEFAULT_FX_COMMISSION: float = 7.0

# ── Pip values for slippage ─────────────────────────────────────────────────────
PIP: dict[str, float] = {"XAUUSD": 0.01, "XAGUSD": 0.001, "NAS100": 0.10, "US30": 0.10}
DEFAULT_PIP: float = 0.0001


def _pip_value(symbol: str) -> float:
    sym = symbol.upper()
    for k, v in PIP.items():
        if k in sym:
            return v
    return DEFAULT_PIP


def _commission(sym: str) -> float:
    for k, v in COMMISSION.items():
        if k in sym.upper():
            return v
    return DEFAULT_FX_COMMISSION


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_price_raw: float
    entry_price_filled: float
    sl_raw: float
    sl_filled: float
    tp1_raw: float
    tp2_raw: float
    fill_bar: int
    exit_price: Optional[float] = None
    exit_bar: Optional[int] = None
    exit_reason: str = ""
    commission_usd: float = 0.0
    slippage_pips: float = 0.0
    profit_raw: float = 0.0
    profit_net: float = 0.0
    rr_target: float = 2.0


@dataclass
class BacktestResult:
    total_signals: int = 0
    filled_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    total_profit_net: float = 0.0
    total_profit_raw: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_rr: float = 0.0
    expectancy: float = 0.0
    trades: list[dict] = field(default_factory=list)


def _simulate_slippage(sym: str, direction: str, is_entry: bool) -> float:
    pip = _pip_value(sym)
    pips = random.uniform(0, 2.0) if is_entry else random.uniform(0, 3.0)
    return pips * pip


def _check_limit_fill(signal: Signal, bars: list[Bar], start_idx: int, max_forward: int = 25, reality: bool = True) -> tuple[Optional[float], Optional[int]]:
    entry = signal.entry_price
    if entry is None:
        return None, None
    if not reality:
        return bars[start_idx].c if start_idx < len(bars) else entry, start_idx
    for i in range(start_idx, min(start_idx + max_forward, len(bars))):
        b = bars[i]
        if b.l <= entry <= b.h:
            return entry, i
    return None, None


def _run_trade(signal: Signal, bars: list[Bar], fill_idx: int, lot_size: float = 0.01, reality: bool = True) -> Trade:
    sym = signal.symbol
    direction = signal.direction
    entry_raw = signal.entry_price
    sl_raw = signal.sl
    tp1 = signal.tp
    tp2 = signal.tp2

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

    for i in range(fill_idx + 1, min(fill_idx + max_hold, len(bars))):
        b = bars[i]
        if direction == "BULL":
            if b.l <= sl_raw:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = sl_raw - exit_slip
                exit_bar = i
                exit_reason = "SL"
                break
            if tp1 is not None and b.h >= tp1:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = tp1 - exit_slip
                exit_bar = i
                exit_reason = "TP1"
                break
        else:
            if b.h >= sl_raw:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = sl_raw + exit_slip
                exit_bar = i
                exit_reason = "SL"
                break
            if tp1 is not None and b.l <= tp1:
                exit_slip = _simulate_slippage(sym, direction, False) if reality else 0.0
                exit_price = tp1 + exit_slip
                exit_bar = i
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
        symbol=sym, direction=direction, entry_price_raw=round(entry_raw, 5),
        entry_price_filled=round(entry_filled, 5), sl_raw=round(sl_raw, 5),
        sl_filled=round(sl_filled, 5), tp1_raw=round(tp1, 5) if tp1 else 0.0,
        tp2_raw=round(tp2, 5) if tp2 else 0.0, fill_bar=fill_idx,
        exit_price=round(exit_price, 5), exit_bar=exit_bar,
        exit_reason=exit_reason, commission_usd=round(comm, 2),
        slippage_pips=round(slip_pips, 2),
        profit_raw=round(profit_pips_raw, 2), profit_net=round(profit_pips_net, 2),
    )


def run_backtest(symbol: str, bars: list[Bar], session_window: str = "LONDON",
                 htf_bars: Optional[list[Bar]] = None, reality: bool = True) -> BacktestResult:
    engine = DeterministicICTEngine(session_window=session_window)
    result = BacktestResult()
    if len(bars) < 55:
        return result

    htf = htf_bars if htf_bars else bars
    for i in range(50, len(bars) - 1):
        ltf_window = bars[:i + 1]
        htf_window = htf[:min(i + 1, len(htf))]

        def _to_raw(bb: list[Bar]):
            return [{"time": b.time, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "broker_ts": b.broker_ts} for b in bb]

        sigs = engine.generate_signals(symbol, _to_raw(htf_window), _to_raw(ltf_window), broker_ts=bars[i].broker_ts)
        for sig in sigs:
            result.total_signals += 1
            fill_price, fill_idx = _check_limit_fill(sig, bars, i + 1, max_forward=25, reality=reality)
            if fill_price is None:
                continue
            trade = _run_trade(sig, bars, fill_idx, lot_size=0.01, reality=reality)
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
                "profit_raw_pips": trade.profit_raw, "profit_net_pips": trade.profit_net,
                "commission": trade.commission_usd, "slippage_pips": trade.slippage_pips,
                "reality_mode": reality,
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


def print_summary(raw: BacktestResult, reality: BacktestResult):
    print("\n" + "=" * 70)
    print("  OMNI DETERMINISTIC ICT — HONEST BACKTEST SUMMARY")
    print("=" * 70)
    print(f"")
    print(f"{'Metric':<30} {'RAW (Optimistic)':<20} {'REALITY-ADJUSTED':<20}")
    print("-" * 70)
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
    print("=" * 70)
    print("NOTE: Pips are per-lot-equivalent. Commission in $/lot.")
    print("      Reality mode applies limit-fill simulation + commission + slippage.")
    print("=" * 70)


def _synthetic_structural_bars() -> list[Bar]:
    """
    Hand-crafted bars satisfying EVERY structural condition for a valid
    BULL deterministic ICT signal. No randomness — pure structural math.

    Structural narrative:
    1. Accumulation: bars 0-9, tight range 2340.0-2341.5 (low ATR)
    2. Leg down: bars 10-20, making structural low at bar 15 (2332.5)
       - flanked by higher lows: bar13.l=2335, bar14.l=2333.5, bar16.l=2333, bar17.l=2334
    3. Induced pivot: bars 21-28, higher lows trending up
    4. Manipulation sweep: bar 29 breaks below 2332.5 to 2330.0, closes back at 2336.0
       - lower wick = body_bottom(2336) - low(2330) = 6.0, range=8.0, ratio=0.75 > 0.4 ✓
    5. Bearish OB root: bar 30 — o=2336, c=2335 (bearish, body=[2335,2336])
    6. Displacement: bar 31 — strong bullish, h=2344, c=2344
       - Must create swing high at bar 31: bar30.h=2337 < 2344, bar32.h=2343 < 2344 ✓
    7. Continuation bar 32: o=2344, c=2342, l=2341
       - FVG check: bar30.h=2337 < bar32.l=2341 → bullish FVG! gap=[2337,2341], top=2341
    8. CHoCH bar 33: close above bar31.h? No, bar31 is already the swing high.
       - Actually CHoCH: close above the last swing high BEFORE the displacement.
       - The last swing high BEFORE displacement was bar 17 at h=2340.0
       - bar 33 close = 2345 > 2340.0 → CHoCH ✓
       - But wait, bar31 IS a swing high. CHoCH means first close above prior swing high.
       - Prior swing high before the down-move was bar 17 at 2340. Bar 31 at 2344 already broke it.
       - So bar31 close (2344) > bar17 high (2340) → CHoCH occurs at bar31.
       - Let me reassign: bar 31 is displacement + CHoCH both.
    
    Actually the MSSDetector needs a swing high IN the lookback, then a close past it.
    The displacement candle itself breaks prior structure. So:
    - Swing high exists at bar 17 (h=2340)
    - Bar 31 closes at 2344 > 2340 → CHoCH ✓
    
    9. Continuation bars 34+ so TP1 hits
       - Entry = FVG top = 2341.0
       - SL = sweep low - 2 pips = 2330.0 - 0.02 = 2329.98
       - Risk = 2341.0 - 2329.98 = 11.02
       - TP1 = entry + 2*risk = 2341.0 + 22.04 = 2363.04
       - Need bars to hit ~2363
    """
    bars: list[Bar] = []
    idx = 0
    ts_base = 1716200000
    def _add(o, h, l, c, v=100):
        nonlocal idx
        bars.append(Bar(idx=idx, time=f"t{idx}", o=round(o,2), h=round(h,2), l=round(l,2), c=round(c,2), v=v, broker_ts=ts_base + idx*3600))
        idx += 1

    # Phase 1: accumulation (bars 0-9), tight range
    for _ in range(10):
        _add(2340.5, 2341.2, 2340.0, 2340.8)
    
    # Phase 2: leg down (bars 10-18), creating swing low at bar 15
    _add(2340.8, 2341.0, 2339.5, 2339.8)    # 10
    _add(2339.8, 2340.0, 2338.0, 2338.5)    # 11
    _add(2338.5, 2338.8, 2336.0, 2336.5)    # 12
    _add(2336.5, 2337.0, 2335.0, 2335.5)    # 13 — higher low candidate
    _add(2335.5, 2336.0, 2333.5, 2334.0)    # 14 — higher low candidate
    _add(2334.0, 2334.5, 2330.0, 2331.0)    # 15 — NOT low enough, change

    # Let me rebuild with a proper swing low structure
    # Delete bars 10-15 and rebuild
    while len(bars) > 10:
        bars.pop()
    idx = 10

    _add(2340.8, 2341.0, 2339.5, 2339.8)    # 10
    _add(2339.8, 2340.0, 2338.0, 2338.5)    # 11
    _add(2338.5, 2338.8, 2337.0, 2337.5)    # 12 — higher low 2337 vs later
    _add(2337.5, 2338.0, 2336.5, 2337.0)    # 13
    _add(2337.0, 2337.5, 2335.0, 2335.5)    # 14
    _add(2335.5, 2336.0, 2333.0, 2333.5)    # 15
    _add(2333.5, 2334.0, 2332.0, 2332.5)    # 16 — this will be the swing low at 2332.0
    # For swing_low(left=2,right=2), bar16 needs:
    #   bar14.l >= 2332.0, bar15.l >= 2332.0, bar17.l >= 2332.0, bar18.l >= 2332.0
    _add(2332.5, 2335.0, 2332.2, 2334.5)    # 17 — l=2332.2 > 2332.0 ✓
    _add(2334.5, 2336.0, 2333.0, 2335.5)    # 18 — l=2333.0 > 2332.0 ✓

    # Phase 3: induced pivot (bars 19-27), higher lows trending up
    _add(2335.5, 2337.0, 2334.0, 2336.5)    # 19
    _add(2336.5, 2338.0, 2335.5, 2337.5)    # 20
    _add(2337.5, 2339.0, 2336.5, 2338.5)    # 21
    _add(2338.5, 2340.0, 2337.5, 2339.5)    # 22
    _add(2339.5, 2341.0, 2338.5, 2340.5)    # 23
    _add(2340.5, 2342.0, 2339.5, 2341.5)    # 24
    _add(2341.5, 2343.0, 2340.5, 2342.5)    # 25
    _add(2342.5, 2344.0, 2341.5, 2343.5)    # 26
    _add(2343.5, 2345.0, 2342.5, 2344.5)    # 27

    # Phase 4: manipulation sweep (bar 28)
    # MUST break below swing_low=2332.0, close back above it
    # lower_wick_ratio >= 0.4
    # o=2344.5, h=2345.0, low=2331.5 (sweep below 2332), c=2338.0
    # body_bottom = min(o,c) = 2338.0, lower_wick = 2338.0-2331.5 = 6.5
    # range = 2345.0-2331.5 = 13.5, ratio = 6.5/13.5 = 0.48 > 0.4 ✓
    _add(2344.5, 2345.0, 2331.5, 2338.0, v=800)    # 28 — sweep!

    # Phase 5: OB root + displacement (bars 29-31)
    # Bar 29: bearish candle = OB root (last down-close before displacement)
    # Needs to be followed by displacement that breaks structure
    _add(2338.0, 2339.0, 2337.0, 2337.2, v=200)    # 29 — bearish OB root, body=[2337.0,2338.0]
    
    # Bar 30: displacement — must close above prior swing high
    # Prior structural swing high before down-move was bar 18 at h=2336.0
    # Actually we need a swing high IN the post-sweep window for MSSDetector
    # Let's make bar 30 high enough to be a swing high: bar29.h=2339, bar31.h must be < bar30.h
    _add(2337.2, 2346.0, 2337.0, 2345.0, v=1000)   # 30 — displacement, h=2346
    # This also breaks prior swing high at bar 18 (h=2336) → CHoCH 

    # Bar 31: continuation, lower than bar30 so bar30 is swing high
    # Also must create FVG: bar29.h < bar31.l
    _add(2345.0, 2345.5, 2342.0, 2343.0, v=300)    # 31 — h=2345.5 < bar30.h=2346 ✓ swing high at bar30
    # FVG check: bar29.h=2339.0 < bar31.l=2342.0 → bullish FVG! top=2342.0

    # Actually wait — the FVGDetector uses 3-candle pattern:
    # c1.h < c3.l where c1=bar29, c2=bar30, c3=bar31
    # bar29.h = 2339.0, bar31.l = 2342.0 → 2339.0 < 2342.0 ✓ bullish FVG
    # FVG.top = bar31.l = 2342.0

    # Phase 6: distribution / continuation to hit TP1
    # Entry = FVG top = 2342.0
    # SL = sweep_candle.l - 2 pips = 2331.5 - 0.02 = 2331.48
    # Risk = 2342.0 - 2331.48 = 10.52
    # TP1 = entry + 2*risk = 2342.0 + 21.04 = 2363.04
    _add(2343.0, 2347.0, 2342.5, 2346.5)    # 32
    _add(2346.5, 2350.0, 2346.0, 2349.5)    # 33
    _add(2349.5, 2353.0, 2349.0, 2352.5)    # 34
    _add(2352.5, 2356.0, 2352.0, 2355.5)    # 35
    _add(2355.5, 2359.0, 2355.0, 2358.5)    # 36
    _add(2358.5, 2362.0, 2358.0, 2361.5)    # 37 — approaching TP1
    _add(2361.5, 2365.0, 2361.0, 2364.5)    # 38 — hits ~2363 TP1 ✓
    _add(2364.5, 2368.0, 2364.0, 2367.5)    # 39
    _add(2367.5, 2371.0, 2367.0, 2370.5)    # 40

    # Phase 7: more bars for walk-forward
    for i in range(20):
        base = 2370.5 + i * 0.5
        _add(base, base+0.5, base-0.3, base+0.2)

    return bars


def main():
    data_candidates = [
        HERE / ".." / "shared" / "xauusd_m5.json",
        HERE / ".." / "shared" / "xauusd_h1.json",
        HERE / "xauusd_sample.json",
        Path.home() / "Downloads" / "XAUUSD.csv",
    ]
    bars: list[Bar] = []
    for cand in data_candidates:
        if cand.exists():
            try:
                raw = json.loads(cand.read_text())
                if isinstance(raw, list):
                    bars = [Bar(idx=i, time=str(r.get("time", f"t{i}")), o=float(r.get("o", r.get("open", 0))), h=float(r.get("h", r.get("high", 0))), l=float(r.get("l", r.get("low", 0))), c=float(r.get("c", r.get("close", 0))), v=int(r.get("v", 0)), broker_ts=0.0) for i, r in enumerate(raw)]
                    print(f"[INFO] Loaded {len(bars)} bars from {cand}")
                    break
            except Exception:
                try:
                    import csv
                    with open(cand) as f:
                        reader = csv.DictReader(f)
                        rows = list(reader)[:10000]
                        bars = [Bar(idx=i, time=str(r.get("time", f"t{i}")), o=float(r.get("open", 0)), h=float(r.get("high", 0)), l=float(r.get("low", 0)), c=float(r.get("close", 0)), v=int(float(r.get("volume", 0))), broker_ts=0.0) for i, r in enumerate(rows)]
                        print(f"[INFO] Loaded {len(bars)} bars from CSV {cand}")
                        break
                except Exception:
                    continue

    if not bars:
        print("[WARN] No real data found. Using deterministic STRUCTURAL test data.")
        print("       Results are for STRUCTURAL VALIDATION ONLY — not predictive.")
        bars = _synthetic_structural_bars()

    symbol = "XAUUSD"
    print(f"\nRunning RAW backtest on {len(bars)} bars...")
    raw_res = run_backtest(symbol, bars, reality=False)
    print(f"\nRunning REALITY backtest on {len(bars)} bars...")
    reality_res = run_backtest(symbol, bars, reality=True)
    print_summary(raw_res, reality_res)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "total_bars": len(bars),
        "raw_mode": {"total_signals": raw_res.total_signals, "filled_trades": raw_res.filled_trades, "wins": raw_res.wins, "losses": raw_res.losses, "win_rate_pct": round(raw_res.win_rate*100, 2), "avg_win_pips": raw_res.avg_win, "avg_loss_pips": raw_res.avg_loss, "avg_rr": raw_res.avg_rr, "expectancy_pips": raw_res.expectancy, "total_profit_pips": raw_res.total_profit_raw, "trades": raw_res.trades},
        "reality_mode": {"total_signals": reality_res.total_signals, "filled_trades": reality_res.filled_trades, "wins": reality_res.wins, "losses": reality_res.losses, "win_rate_pct": round(reality_res.win_rate*100, 2), "avg_win_pips": reality_res.avg_win, "avg_loss_pips": reality_res.avg_loss, "avg_rr": reality_res.avg_rr, "expectancy_pips": reality_res.expectancy, "total_profit_pips": reality_res.total_profit_net, "trades": reality_res.trades},
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\n[OK] Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
