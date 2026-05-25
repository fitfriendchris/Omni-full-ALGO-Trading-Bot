#!/usr/bin/env python3
"""
forward_sim_multi_tf.py — Multi-Timeframe Monte Carlo Forward Simulator

Hierarchy:
  H4 = Macro bias (from resampled H1)
  H1 = Manipulation leg detection, STDV anchoring
  M15 = Entry confirmation (BOS/CHoCH, FVG, OB) — synthetically generated from H1

Uses multi_tf_selector.select_trade_multi_tf() for entry decisions.
Reality model unchanged: Brownian ticks, limit fill, commission, slippage.
"""

from __future__ import annotations

import csv, json, math, random, statistics, sys, os, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from smc_engine import Bar
from multi_tf_selector import select_trade_multi_tf, MultiTFSelection

# ── Config ───────────────────────────────────────────────────────────────────
INITIAL_EQUITY = 100.0
MAX_RISK_PER_TRADE = 0.05
COMMISSION_PER_LOT = 10.0
PIPSIZE = {"XAUUSD": 0.01, "XAGUSD": 0.001, "EURUSD": 0.0001}

LIMIT_FILL_BASE = 0.75
LIMIT_FILL_OTE = 0.10
SLIPPAGE_BASE_PIPS = 0.8
SLIPPAGE_VOL_MULT = 1.5
ATR_WINDOW = 50
SKIP_BARS = 4          # Process every 4th H1 bar (4H effective)
TICKS_PER_BAR = 100

AMD_TRANSITION = {
    "ACCUMULATION": [("MANIPULATION", 0.40), ("ACCUMULATION", 0.40), ("OFF_HOURS", 0.20)],
    "MANIPULATION": [("DISTRIBUTION", 0.50), ("MANIPULATION", 0.30), ("ACCUMULATION", 0.20)],
    "DISTRIBUTION": [("LATE_DIST", 0.35), ("ACCUMULATION", 0.35), ("DISTRIBUTION", 0.30)],
    "LATE_DIST":    [("ACCUMULATION", 0.50), ("OFF_HOURS", 0.30), ("LATE_DIST", 0.20)],
    "OFF_HOURS":    [("ACCUMULATION", 0.60), ("OFF_HOURS", 0.40)],
}


@dataclass
class SimConfig:
    symbol: str = "XAUUSD"
    initial_equity: float = INITIAL_EQUITY
    max_risk_per_trade: float = MAX_RISK_PER_TRADE
    commission_per_lot: float = COMMISSION_PER_LOT
    pip_size: float = 0.01
    seed: int = 0


@dataclass
class SimTrade:
    entry_time: float
    direction: str
    entry_price: float
    sl: float
    tp: float
    lots: float
    risk_usd: float
    commission: float
    slippage_entry: float
    fill_prob: float
    filled: bool
    exit_time: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    confluence_count: int = 0
    confidence: float = 0.0
    manipulation_type: str = ""
    h4_bias: str = ""


@dataclass
class SimResult:
    trades: List[SimTrade] = field(default_factory=list)
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    total_trades: int = 0
    filled_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    avg_trade_net: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    total_commission: float = 0.0
    setups_seen: int = 0
    bars_processed: int = 0
    seed: int = 0


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_h1_bars(symbol: str, period: str = "1y") -> List[Bar]:
    try:
        import yfinance as yf
        ticker_map = {"XAUUSD": "GC=F", "XAGUSD": "SI=F", "EURUSD": "EURUSD=X"}
        ticker = ticker_map.get(symbol, symbol + "=X")
        df = yf.download(ticker, period=period, interval="1h", progress=False, auto_adjust=True)
        if df.empty:
            return []
        if hasattr(df.columns, 'levels'):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        bars = []
        for ts, row in df.iterrows():
            bars.append(Bar(
                time=ts.timestamp(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
            ))
        return bars
    except Exception as e:
        print(f"[SIM] Data fetch error: {e}")
        return []


def resample_bars(bars: List[Bar], n: int) -> List[Bar]:
    out = []
    for i in range(0, len(bars), n):
        chunk = bars[i:i+n]
        if not chunk:
            continue
        out.append(Bar(
            time=chunk[0].time, open=chunk[0].open,
            high=max(b.high for b in chunk),
            low=min(b.low for b in chunk),
            close=chunk[-1].close,
        ))
    return out


def generate_m15_from_h1(h1_bars: List[Bar], seed: int = 0) -> List[Bar]:
    """Generate synthetic M15 bars from H1 bars using constrained Brownian bridge."""
    rng = random.Random(seed)
    m15 = []
    for b in h1_bars:
        sigma = max(0.0001, (b.high - b.low) / 6.0)
        price = b.open
        for j in range(4):
            t = b.time + j * 900
            rem = 4 - j
            drift = (b.close - price) / rem if rem > 0 else 0
            noise = rng.gauss(0, sigma)
            close = price + drift + noise
            close = max(b.low * 0.9999, min(b.high * 1.0001, close))
            o = price
            c = close
            h = max(o, c) + rng.uniform(0, sigma * 0.5)
            l = min(o, c) - rng.uniform(0, sigma * 0.5)
            h = min(h, b.high)
            l = max(l, b.low)
            m15.append(Bar(time=t, open=o, high=h, low=l, close=c))
            price = c
    return m15


# ── ATR ───────────────────────────────────────────────────────────────────────

def calc_atr(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        prev_c = bars[i - 1].close
        tr = max(bars[i].high - bars[i].low,
                 abs(bars[i].high - prev_c),
                 abs(bars[i].low - prev_c))
        trs.append(tr)
    return sum(trs[-period:]) / len(trs[-period:]) if trs else 0.0


# ── Ticks ──────────────────────────────────────────────────────────────────────

def simulate_ticks(bar: Bar, n: int = 100, seed: int = 0) -> List[float]:
    rng = random.Random(seed)
    sigma = max(0.0001, (bar.high - bar.low) / 4.0)
    dt = 1.0 / n
    path = [bar.open]
    cur = bar.open
    for i in range(1, n):
        rem = n - i
        drift = (bar.close - cur) / rem if rem > 0 else 0
        cur += rng.gauss(drift * dt, sigma * math.sqrt(dt))
        if cur > bar.high:
            cur = bar.high - (cur - bar.high)
        if cur < bar.low:
            cur = bar.low + (bar.low - cur)
        path.append(cur)
    path[-1] = bar.close
    return path


def limit_crossed(ticks: List[float], limit: float, direction: str) -> bool:
    return any(t <= limit for t in ticks) if direction == "BULL" else any(t >= limit for t in ticks)


def best_fill(ticks: List[float], limit: float, direction: str) -> float:
    if direction == "BULL":
        valid = [t for t in ticks if t <= limit]
        return min(valid) if valid else limit
    valid = [t for t in ticks if t >= limit]
    return max(valid) if valid else limit


# ── AMD ──────────────────────────────────────────────────────────────────────

def next_amd(prev: str, rng: random.Random) -> str:
    trans = AMD_TRANSITION.get(prev, [(prev, 1.0)])
    r = rng.random()
    cum = 0.0
    for state, prob in trans:
        cum += prob
        if r <= cum:
            return state
    return trans[-1][0]


# ── Sizing ───────────────────────────────────────────────────────────────────

def calc_lots(equity: float, risk_pct: float, entry: float, sl: float, pip: float) -> Tuple[float, float]:
    risk_usd = equity * risk_pct
    sl_pips = abs(entry - sl) / pip
    if sl_pips < 0.1:
        sl_pips = 0.1
    lots = risk_usd / (sl_pips * 1.0)
    return max(0.01, lots), risk_usd


# ── Single run ──────────────────────────────────────────────────────────────

def run_single(seed: int, h1_bars: List[Bar], h4_bars: List[Bar],
               m15_bars: List[Bar], cfg: SimConfig, atr_norm: float) -> SimResult:
    rng = random.Random(seed)
    result = SimResult(seed=seed)
    equity = cfg.initial_equity
    peak = equity
    max_dd = 0.0
    open_trades: List[SimTrade] = []
    amd = "ACCUMULATION"

    # Window sizes for context
    h4_w = 20
    h1_w = 50
    m15_w = 40

    # Process every SKIP_BARS-th H1 bar
    for h1_idx in range(h1_w + 10, len(h1_bars), SKIP_BARS):
        bar = h1_bars[h1_idx]
        result.bars_processed += 1
        if (h1_idx // SKIP_BARS) % 4 == 0:
            amd = next_amd(amd, rng)

        # Build multi-TF context
        h4_ctx = h4_bars[max(0, (h1_idx // 4) - h4_w):(h1_idx // 4)]
        h1_ctx = h1_bars[max(0, h1_idx - h1_w):h1_idx]
        m15_end = h1_idx * 4
        m15_start = max(0, m15_end - m15_w)
        m15_ctx = m15_bars[m15_start:m15_end]

        if len(h4_ctx) < 6 or len(h1_ctx) < 20:
            continue

        try:
            sel = select_trade_multi_tf(
                h4_bars=h4_ctx,
                h1_bars=h1_ctx,
                m15_bars=m15_ctx,
                current_price=bar.close,
                amd_phase=amd,
                pip_size=cfg.pip_size,
            )
        except Exception as e:
            continue

        result.setups_seen += 1
        if not sel.is_actionable:
            continue
        if not sel.entry_price or not sel.sl or not sel.tp:
            continue
        if len(open_trades) >= 3:
            continue

        lots, risk_usd = calc_lots(equity, cfg.max_risk_per_trade,
                                   sel.entry_price, sel.sl, cfg.pip_size)

        fp = LIMIT_FILL_BASE
        if sel.stdv_profile and hasattr(sel.stdv_profile, 'is_price_in_ote'):
            # Quick OTE zone check
            pass
        dist = abs(sel.entry_price - bar.close)
        atr_cur = calc_atr(h1_ctx, 14)
        if atr_cur > 0 and dist > atr_cur * 2:
            fp -= 0.20
        fp = max(0.10, min(0.95, fp))

        ticks = simulate_ticks(bar, n=TICKS_PER_BAR, seed=seed + h1_idx)
        crossed = limit_crossed(ticks, sel.entry_price, sel.direction)
        filled = crossed and rng.random() < fp

        atr_now = calc_atr(h1_ctx, 14)
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
            h4_bias=sel.h4_bias.direction if sel.h4_bias else "NEUTRAL",
        )
        if filled:
            open_trades.append(trade)
            equity -= comm
            result.total_commission += comm

        # Exits
        still_open: List[SimTrade] = []
        for t in open_trades:
            eticks = simulate_ticks(bar, n=50, seed=seed + h1_idx + 5000)
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

    # Close open trades at EOD
    last = h1_bars[-1].close if h1_bars else 0.0
    for t in open_trades:
        t.exit_time = h1_bars[-1].time if h1_bars else 0.0
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


# ── Driver ───────────────────────────────────────────────────────────────────

def run_multi_tf(symbol: str = "XAUUSD", n_runs: int = 100, period: str = "1y",
                 workers: int = None) -> dict:
    if workers is None:
        import multiprocessing
        workers = min(multiprocessing.cpu_count(), 12)

    print(f"[MULTI-TF SIM] {n_runs} runs | {symbol} | {period} | workers={workers}")
    t0 = time.time()

    h1 = fetch_h1_bars(symbol, period)
    if not h1:
        print("[MULTI-TF SIM] No data!")
        return {}
    h4 = resample_bars(h1, 4)
    # Generate M15 once per process — but for parallel runs, each worker will re-generate
    # To optimize, we'll generate in the worker. For now, generate once here and pass.
    m15 = generate_m15_from_h1(h1, seed=12345)
    print(f"[MULTI-TF SIM] {len(h1)} H1 bars → {len(h4)} H4 → {len(m15)} M15 (synthetic)")

    atr_norm = calc_atr(h1[:ATR_WINDOW]) if len(h1) > ATR_WINDOW else 5.0
    if atr_norm < 0.5:
        atr_norm = 0.5

    cfg = SimConfig(symbol=symbol, pip_size=PIPSIZE.get(symbol, 0.01))

    results: List[SimResult] = []
    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_single, i + 42, h1, h4, m15, cfg, atr_norm): i
                   for i in range(n_runs)}
        for future in as_completed(futures):
            try:
                results.append(future.result())
                completed += 1
                if completed % 20 == 0:
                    print(f"[MULTI-TF SIM] {completed}/{n_runs} done ({time.time() - t0:.1f}s)")
            except Exception as e:
                print(f"[MULTI-TF SIM] Run failed: {e}")

    total = time.time() - t0
    print(f"[MULTI-TF SIM] All {completed}/{n_runs} done in {total:.1f}s")

    returns = [r.total_return_pct for r in results]
    wrs = [r.win_rate for r in results]
    dds = [r.max_drawdown_pct for r in results]
    pfs = [r.profit_factor for r in results if r.profit_factor < 900]
    exps = [r.expectancy for r in results]
    ntr = [r.total_trades for r in results]
    comms = [r.total_commission for r in results]
    profitable = [r for r in results if r.final_equity > INITIAL_EQUITY]

    def st(arr):
        if not arr:
            return {"mean": 0, "median": 0, "min": 0, "max": 0, "std": 0, "p5": 0, "p95": 0}
        s = sorted(arr)
        n = len(s)
        return {
            "mean": statistics.mean(s), "median": statistics.median(s),
            "min": min(s), "max": max(s),
            "std": statistics.stdev(s) if n > 1 else 0.0,
            "p5": s[max(0, int(n * 0.05) - 1)],
            "p95": s[min(n - 1, int(n * 0.95) - 1)],
        }

    summary = {
        "symbol": symbol, "n_runs": n_runs, "period": period,
        "h1_bars": len(h1), "h4_bars": len(h4), "m15_bars": len(m15),
        "workers": workers, "total_time_sec": round(total, 2),
        "profitable_runs": len(profitable),
        "profitable_pct": round(len(profitable) / n_runs * 100, 2),
        "return_pct": st(returns), "win_rate": st(wrs),
        "max_drawdown_pct": st(dds), "profit_factor": st(pfs),
        "expectancy_usd": st(exps), "trades_per_run": st(ntr),
        "commission_per_run": st(comms),
    }

    out = Path(__file__).parent
    with open(out / "multi_tf_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    with open(out / "multi_tf_runs.csv", "w") as f:
        f.write("run,seed,return_pct,win_rate,max_dd_pct,pf,expectancy,trades,final_equity\n")
        for i, r in enumerate(results):
            pf = r.profit_factor if r.profit_factor < 900 else 999.0
            f.write(f"{i},{r.seed},{r.total_return_pct:.2f},{r.win_rate:.2f},"
                    f"{r.max_drawdown_pct:.2f},{pf:.2f},{r.expectancy:.2f},"
                    f"{r.total_trades},{r.final_equity:.2f}\n")

    print("\n" + "=" * 60)
    print("MULTI-TF SIMULATION — SUMMARY")
    print("=" * 60)
    print(json.dumps(summary, indent=2))
    print("=" * 60)
    return summary


if __name__ == "__main__":
    run_multi_tf("XAUUSD", 100, "1y")
