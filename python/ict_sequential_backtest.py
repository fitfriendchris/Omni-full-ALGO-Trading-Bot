"""
ict_sequential_backtest.py — Honest, walk-forward backtest of ict_sequential.

Goal: measure whether the SEQUENTIAL ICT engine has a real edge BEFORE any live
money — the "paper-first" proof. Reports results in R-multiples (signal quality,
independent of sizing) AND in dollars using the real risk_sizing rules.

Honesty rules baked in:
  - Walk-forward: at LTF bar i, the engine only sees bars[:i+1] and HTF bars
    with time <= now. No lookahead.
  - Limit-fill model: the engine returns a limit entry in the OTE (below price
    for longs). We only fill if price actually trades to it within `fill_window`
    LTF bars; otherwise the setup is cancelled (no fantasy fills).
  - Costs: spread + slippage applied to BOTH entry and exit; commission per trade.
  - Intrabar SL/TP: if a bar's range spans both SL and TP, we assume SL hit first
    (pessimistic / conservative).
  - One position at a time (isolates signal edge; sizing overlay reported separately).

Data: gold futures GC=F via yfinance. Intraday history is limited (~60d for 15m),
so this is a 60-day, recent-regime test — same horizon as the prior HONEST report.
It is NOT a multi-year walk-forward; treat it as a go/no-go screen, not proof of
long-run profitability.

Usage:
  ../.venv/bin/python ict_sequential_backtest.py
  ../.venv/bin/python ict_sequential_backtest.py --htf 1h --ltf 15m --days 60
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import List, Optional

from smc_engine import Bar
from ict_sequential import evaluate, SequentialConfig
from risk_sizing import RiskConfig, size_position, trade_risk_usd, base_lot_for_equity


# ──────────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────────

MT5_COMMON = ("/Users/yuhfriendchris/Library/Application Support/"
              "net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/"
              "MetaQuotes/Terminal/Common/Files")


def load_mt5_csv(symbol: str, tf: str) -> List[Bar]:
    """Load history exported by OmniHistoryExport.mq5 (real broker prices).
    CSV columns: time,open,high,low,close,volume ; time = YYYY.MM.DD HH:MM:SS."""
    import csv, os
    from datetime import datetime, timezone
    path = os.path.join(MT5_COMMON, f"hist_{symbol}_{tf}.csv")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}\n  -> run OmniHistoryExport.mq5 in MT5 first.")
    bars: List[Bar] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                t = datetime.strptime(row["time"], "%Y.%m.%d %H:%M:%S").replace(
                    tzinfo=timezone.utc).timestamp()
                bars.append(Bar(time=t, open=float(row["open"]), high=float(row["high"]),
                                low=float(row["low"]), close=float(row["close"])))
            except Exception:
                continue
    bars.sort(key=lambda b: b.time)
    return bars


def load_yf(symbol: str, interval: str, days: int) -> List[Bar]:
    import yfinance as yf
    period = f"{days}d"
    df = yf.download(symbol, interval=interval, period=period,
                     auto_adjust=False, progress=False)
    if df is None or len(df) == 0:
        raise SystemExit(f"no data for {symbol} {interval} {period}")
    # Flatten possible MultiIndex columns.
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    bars: List[Bar] = []
    for ts, row in df.iterrows():
        t = ts.timestamp()  # tz-aware (UTC) for intraday yfinance
        o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        if any(v != v for v in (o, h, l, c)):  # NaN guard
            continue
        bars.append(Bar(time=t, open=o, high=h, low=l, close=c))
    return bars


# ──────────────────────────────────────────────────────────────────────────────
# Backtest
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    direction: str
    entry: float
    sl: float
    tp: float
    rr_planned: float
    fill_time: float
    exit: float = 0.0
    r_realized: float = 0.0
    outcome: str = ""        # WIN | LOSS | CANCELLED
    lots: float = 0.0
    pnl_usd: float = 0.0


def _manage_partials(ltf: List[Bar], filled_at: int, direction: str, entry: float,
                     sl: float, tp1: float, tp2: float, frac: float):
    """Two-phase management. Phase 1: run the full position to tp1 or SL. On tp1,
    bank `frac` of the position and move the stop to break-even (entry). Phase 2:
    run the remaining (1-frac) to tp2 (runner) or BE. Pessimistic intrabar (a bar
    spanning both levels resolves to the adverse one first).
    Returns (legs, outcome, exit_bar_idx); legs = [(fraction, exit_price), ...]."""
    n = len(ltf)
    # Phase 1 → tp1 or SL.
    k = filled_at
    while k < n:
        b = ltf[k]
        if direction == "BULL":
            hit_sl, hit_tp1 = b.low <= sl, b.high >= tp1
        else:
            hit_sl, hit_tp1 = b.high >= sl, b.low <= tp1
        if hit_sl:                                   # adverse first
            return [(1.0, sl)], "LOSS", k
        if hit_tp1:
            break
        k += 1
    else:
        return [(1.0, ltf[-1].close)], "OPEN_END", n - 1   # never reached tp1

    legs = [(frac, tp1)]                              # banked partial
    rem = 1.0 - frac
    be = entry                                       # stop now at break-even
    for k2 in range(k + 1, n):
        b = ltf[k2]
        if direction == "BULL":
            hit_be, hit_tp2 = b.low <= be, b.high >= tp2
        else:
            hit_be, hit_tp2 = b.high >= be, b.low <= tp2
        if hit_be:                                   # adverse first (BE scratch)
            legs.append((rem, be)); return legs, "WIN_BE", k2
        if hit_tp2:
            legs.append((rem, tp2)); return legs, "WIN_RUN", k2
    legs.append((rem, ltf[-1].close))
    return legs, "WIN_OPEN", n - 1


def run(htf: List[Bar], ltf: List[Bar], symbol: str,
        cfg: SequentialConfig, risk_cfg: RiskConfig,
        start_equity: float, spread: float, slippage: float,
        commission_per_lot: float, fill_window: int,
        cooldown_bars: int, evaluator=None,
        ltf_window: int = 800, htf_window: int = 300, stride: int = 1,
        partials: bool = False, partial_frac: float = 0.5) -> dict:
    # `evaluator(htf, ltf, cfg, now_ts) -> Setup`. Defaults to the pure ICT engine;
    # pass kronos_filter.evaluate_with_kronos to A/B the confirmation overlay.
    if evaluator is None:
        evaluator = lambda h, l, now_ts: evaluate(h, l, cfg=cfg, now_ts=now_ts)
    # Rolling lookback: the engine only needs recent structure (sweeps≤25 bars,
    # CHoCH≤15, recent swings/OB/FVG). Re-analyzing the full growing history every
    # bar is O(n^2) — intractable on multi-year data — AND wrong (ancient structure
    # leaks in). Pass only a trailing window. 0 disables (use full history).

    # Pre-index HTF by time for fast slicing.
    htf_times = [b.time for b in htf]

    def htf_upto(t: float) -> List[Bar]:
        # bars with time <= t
        lo, hi = 0, len(htf_times)
        while lo < hi:
            mid = (lo + hi) // 2
            if htf_times[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        return htf[:lo]

    trades: List[Trade] = []
    i = 60                                   # warmup
    warm_htf_min = 20
    equity = start_equity
    cooldown_until = 0
    half_cost = (spread + slippage) / 2.0
    # When flat, advance `stride` bars between evaluations. An ICT setup (sweep
    # within 25 bars, CHoCH within 15) persists across several bars, so striding
    # 2-3 barely changes which setups are caught but multiplies throughput on
    # multi-year data. After a fill we still advance exactly past the exit.
    step = max(1, stride)

    while i < len(ltf):
        if i < cooldown_until:
            i += step
            continue
        htf_slice = htf_upto(ltf[i].time)
        if len(htf_slice) < warm_htf_min:
            i += step
            continue
        if htf_window > 0:
            htf_slice = htf_slice[-htf_window:]
        ltf_slice = ltf[max(0, i + 1 - ltf_window):i + 1] if ltf_window > 0 else ltf[:i + 1]

        s = evaluator(htf_slice, ltf_slice, ltf[i].time)
        if not s.actionable:
            i += step
            continue

        # ── Limit-fill model: wait for price to trade to the entry ──
        filled_at = None
        fill_j = None
        for j in range(i + 1, min(i + 1 + fill_window, len(ltf))):
            b = ltf[j]
            if s.direction == "BULL" and b.low <= s.entry:
                filled_at = j; break
            if s.direction == "BEAR" and b.high >= s.entry:
                filled_at = j; break
            # invalidation: SL taken before fill
            if s.direction == "BULL" and b.low <= s.sl:
                break
            if s.direction == "BEAR" and b.high >= s.sl:
                break
        if filled_at is None:
            i += step
            continue

        # Apply adverse entry cost.
        entry_px = s.entry + half_cost if s.direction == "BULL" else s.entry - half_cost
        risk_dist = abs(entry_px - s.sl)
        if risk_dist <= 0:
            i += step
            continue

        lots, risk_usd, _why = size_position(equity, entry_px, s.sl, symbol,
                                              open_risk_usd=0.0, cfg=risk_cfg)
        if lots <= 0:
            i += step
            continue

        tr = Trade(direction=s.direction, entry=round(entry_px, 2), sl=s.sl, tp=s.tp,
                   rr_planned=s.rr, fill_time=ltf[filled_at].time, lots=lots)

        from risk_sizing import _value_per_unit

        def leg_gross(exit_px: float) -> float:
            adj = exit_px - half_cost if s.direction == "BULL" else exit_px + half_cost
            return (adj - entry_px) if s.direction == "BULL" else (entry_px - adj)

        use_partials = partials and s.tp1 is not None and s.tp2 is not None
        if use_partials:
            # Phase 1 → tp1 or SL; Phase 2 (remainder) with SL at BE → tp2 or BE.
            legs, outcome, exit_bar = _manage_partials(
                ltf, filled_at, s.direction, entry_px, s.sl, s.tp1, s.tp2, partial_frac)
            blended = sum(frac * leg_gross(px) for frac, px in legs)
            commission = commission_per_lot * lots * 1.5        # entry + 2 partial exits
            tr.exit = round(legs[-1][1], 2)
        else:
            exit_px = None; outcome = None; exit_bar = len(ltf) - 1
            for k in range(filled_at, len(ltf)):
                b = ltf[k]
                if s.direction == "BULL":
                    hit_sl, hit_tp = b.low <= s.sl, b.high >= s.tp
                else:
                    hit_sl, hit_tp = b.high >= s.sl, b.low <= s.tp
                if hit_sl:                       # pessimistic: SL before TP on a spanning bar
                    exit_px, outcome, exit_bar = s.sl, "LOSS", k; break
                if hit_tp:
                    exit_px, outcome, exit_bar = s.tp, "WIN", k; break
            if exit_px is None:
                exit_px, outcome = ltf[-1].close, "OPEN_END"
            blended = leg_gross(exit_px)
            commission = commission_per_lot * lots
            tr.exit = round(exit_px - half_cost if s.direction == "BULL"
                            else exit_px + half_cost, 2)

        pnl = blended * _value_per_unit(symbol) * lots - commission
        r = blended / risk_dist if risk_dist else 0.0
        # Normalize outcome to WIN/LOSS by realized sign (partials bank a profit at
        # tp1 even if the runner scratches at BE).
        tr.outcome = "WIN" if r > 0 else ("LOSS" if outcome != "OPEN_END" else "OPEN_END")
        tr.r_realized = round(r, 2)
        tr.pnl_usd = round(pnl, 2)
        equity = round(equity + pnl, 2)
        trades.append(tr)

        # advance past the exit bar + cooldown
        i = exit_bar + 1 + cooldown_bars
        cooldown_until = i

    return _report(trades, start_equity, equity)


def _report(trades: List[Trade], start_eq: float, end_eq: float) -> dict:
    closed = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    wins = [t for t in closed if t.outcome == "WIN"]
    losses = [t for t in closed if t.outcome == "LOSS"]
    n = len(closed)
    wr = (len(wins) / n * 100) if n else 0.0
    total_r = sum(t.r_realized for t in closed)
    avg_r = (total_r / n) if n else 0.0
    gross_win = sum(t.r_realized for t in wins)
    gross_loss = abs(sum(t.r_realized for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # Max consecutive losses.
    streak = mx = 0
    for t in closed:
        if t.outcome == "LOSS":
            streak += 1; mx = max(mx, streak)
        else:
            streak = 0

    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(wr, 1), "total_R": round(total_r, 2),
        "avg_R": round(avg_r, 3), "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "max_consec_losses": mx,
        "start_equity": start_eq, "end_equity": end_eq,
        "return_pct": round((end_eq / start_eq - 1) * 100, 1) if start_eq else 0.0,
        "_trades": trades,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="mt5", choices=["mt5", "yf"],
                    help="mt5 = real broker CSV from OmniHistoryExport (recommended); yf = yfinance")
    ap.add_argument("--symbol", default="XAUUSD", help="symbol (XAUUSD for mt5; GC=F for yf)")
    ap.add_argument("--htf", default="h1")
    ap.add_argument("--ltf", default="m15")
    ap.add_argument("--days", type=int, default=60, help="yfinance lookback only")
    ap.add_argument("--equity", type=float, default=133.42)
    ap.add_argument("--spread", type=float, default=0.30, help="gold spread in $")
    ap.add_argument("--slippage", type=float, default=0.10)
    ap.add_argument("--commission", type=float, default=0.07, help="$ per 0.01 lot round turn")
    ap.add_argument("--fill-window", type=int, default=6)
    ap.add_argument("--cooldown", type=int, default=4)
    ap.add_argument("--kronos", action="store_true",
                    help="apply the Kronos confirmation overlay (G7) on top of pure ICT")
    ap.add_argument("--kronos-min-prob", type=float, default=0.45,
                    help="veto setups below this modeled P(TP before SL)")
    ap.add_argument("--kronos-paths", type=int, default=12,
                    help="Monte-Carlo forecast paths per setup")
    ap.add_argument("--ltf-window", type=int, default=800,
                    help="trailing LTF bars fed to the engine each step (0=full history)")
    ap.add_argument("--htf-window", type=int, default=300,
                    help="trailing HTF bars fed to the engine each step (0=full history)")
    ap.add_argument("--stride", type=int, default=1,
                    help="evaluate every Nth LTF bar when flat (speed on multi-year data)")
    ap.add_argument("--tp-mode", default="draw", choices=["draw", "ladder"],
                    help="draw=legacy single far TP; ladder=reachable tp1 + runner to draw")
    ap.add_argument("--tp1-rr", type=float, default=1.5, help="ladder tp1 distance in R")
    ap.add_argument("--partials", action="store_true",
                    help="partial exit at tp1 + move stop to break-even (needs --tp-mode ladder)")
    ap.add_argument("--partial-frac", type=float, default=0.5,
                    help="fraction banked at tp1 (rest runs to tp2)")
    ap.add_argument("--regime", action="store_true",
                    help="P3 regime filter: only trade with the HTF macro trend; skip chop")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print(f"Loading [{args.source}] {args.symbol}: HTF={args.htf} LTF={args.ltf}…")
    if args.source == "mt5":
        htf = load_mt5_csv(args.symbol, args.htf)
        ltf = load_mt5_csv(args.symbol, args.ltf)
    else:
        htf = load_yf(args.symbol, args.htf, args.days)
        ltf = load_yf(args.symbol, args.ltf, args.days)
    from datetime import datetime, timezone
    if ltf:
        span = (f"{datetime.fromtimestamp(ltf[0].time, timezone.utc):%Y-%m-%d} -> "
                f"{datetime.fromtimestamp(ltf[-1].time, timezone.utc):%Y-%m-%d}")
    else:
        span = "(no LTF bars)"
    print(f"  HTF bars={len(htf)}  LTF bars={len(ltf)}  span {span}")

    cfg = SequentialConfig(symbol="XAUUSD", tp_mode=args.tp_mode, tp1_rr=args.tp1_rr,
                           regime_filter=args.regime)
    risk_cfg = RiskConfig()
    if args.tp_mode == "ladder" or args.partials:
        print(f"  [TP ladder] tp1={args.tp1_rr}R runner→draw"
              + (f"; partials {args.partial_frac:.0%}@tp1 + BE" if args.partials else ""))
    if args.regime:
        print("  [Regime filter ON] HTF-trend aligned, chop skipped")

    evaluator = None
    if args.kronos:
        from kronos_filter import evaluate_with_kronos, KronosConfig
        kcfg = KronosConfig(min_win_prob=args.kronos_min_prob, mc_paths=args.kronos_paths)
        evaluator = lambda h, l, now_ts: evaluate_with_kronos(
            h, l, cfg=cfg, kronos_cfg=kcfg, now_ts=now_ts)
        print(f"  [Kronos overlay ON] min_prob={kcfg.min_win_prob} paths={kcfg.mc_paths} "
              f"model={kcfg.model_repo}")

    rep = run(htf, ltf, "XAUUSD", cfg, risk_cfg, args.equity,
              args.spread, args.slippage, args.commission * 100,  # per-lot from per-0.01
              args.fill_window, args.cooldown, evaluator=evaluator,
              ltf_window=args.ltf_window, htf_window=args.htf_window, stride=args.stride,
              partials=args.partials, partial_frac=args.partial_frac)

    print("\n" + "=" * 60)
    print("  SEQUENTIAL ICT — HONEST 60-DAY GOLD BACKTEST")
    print("=" * 60)
    for k in ("trades", "wins", "losses", "win_rate", "total_R", "avg_R",
              "profit_factor", "max_consec_losses", "start_equity",
              "end_equity", "return_pct"):
        print(f"  {k:<20} {rep[k]}")
    print("=" * 60)
    # Honest verdict thresholds.
    edge = (rep["trades"] >= 10 and rep["avg_R"] > 0.0 and
            isinstance(rep["profit_factor"], (int, float)) and rep["profit_factor"] >= 1.3)
    print("  VERDICT:", "POSITIVE EDGE on this window (proceed to longer/paper test)"
          if edge else "NO PROVEN EDGE on this window — do NOT deploy live")
    print("  NOTE: 60-day recent-regime screen only. Not multi-year proof.")

    if args.verbose:
        print("\n  Trades:")
        for t in rep["_trades"]:
            print(f"   {t.direction} {t.entry}->{t.exit} SL{t.sl} TP{t.tp} "
                  f"{t.outcome} {t.r_realized:+}R ${t.pnl_usd:+} lot{t.lots}")


if __name__ == "__main__":
    main()
