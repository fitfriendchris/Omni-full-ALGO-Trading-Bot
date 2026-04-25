"""
backtester.py — OMNI ICT AI Backtesting Engine
Replays historical bars and simulates ICT strategy trades
Uses real MT5 data exported by OmniExport EA

Run standalone: python backtester.py
Or import:      from backtester import run_backtest
"""

import json, re, os, math, random
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
try:
    from config import cfg
    JSON_PATH = cfg.JSON_PATH
except ImportError:
    from pathlib import Path as _Path
    JSON_PATH = str(
        _Path.home() / "Library/Application Support"
        / "net.metaquotes.wine.metatrader5/drive_c/users/user"
        / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json"
    )
RESULTS_PATH = str(Path(__file__).parent / "backtest_results.json")

# ── Data structures ───────────────────────────────────────────────────────────
@dataclass
class Bar:
    time: str
    o: float; h: float; l: float; c: float; v: int = 0

    @property
    def bullish(self): return self.c > self.o
    @property
    def bearish(self): return self.c < self.o
    @property
    def body(self): return abs(self.c - self.o)
    @property
    def range(self): return self.h - self.l if self.h > self.l else 0.0001
    @property
    def mid(self): return (self.h + self.l) / 2


@dataclass
class SimTrade:
    id:           int
    symbol:       str
    direction:    str      # BUY / SELL
    entry_type:   str
    entry_time:   str
    entry_price:  float
    sl:           float
    tp1:          float
    tp2:          float
    tp3:          float
    lot_size:     float
    risk_amount:  float
    confidence:   int
    session:      str
    amd_phase:    str

    # Fill in on close
    exit_time:    str = ""
    exit_price:   float = 0.0
    exit_reason:  str = ""   # TP1/TP2/TP3/SL/MANUAL
    pnl:          float = 0.0
    pnl_r:        float = 0.0   # P&L in R multiples
    partial_exits: list = field(default_factory=list)


@dataclass
class BacktestConfig:
    symbol:         str   = "XAUUSD"
    start_date:     str   = ""          # "2024-01-01" or "" for all available
    end_date:       str   = ""
    initial_equity: float = 10000.0
    base_risk_pct:  float = 2.0
    max_risk_pct:   float = 3.0
    min_risk_pct:   float = 1.0
    max_open:       int   = 3
    min_rr:         float = 1.5
    min_confidence: int   = 50
    compound:       bool  = True
    win_streak_boost: int = 3     # Wins before risk increase
    loss_streak_cut:  int = 2     # Losses before risk decrease
    daily_loss_limit: float = 6.0
    tp1_pct:        float = 0.50  # Close 50% at TP1
    tp2_pct:        float = 0.30  # Close 30% at TP2
    tp3_pct:        float = 0.20  # Runner 20% to TP3
    spread_points:  int   = 20    # Simulated spread
    # ICT params
    ob_lookback:    int   = 10
    fvg_lookback:   int   = 8
    sweep_confirm:  int   = 2     # Bars to confirm sweep
    swing_lookback: int   = 20


@dataclass
class BacktestResult:
    config:           BacktestConfig
    symbol:           str
    trades:           list = field(default_factory=list)
    equity_curve:     list = field(default_factory=list)  # [{time, equity, drawdown}]

    # Summary stats
    initial_equity:   float = 0.0
    final_equity:     float = 0.0
    total_return_pct: float = 0.0
    total_trades:     int   = 0
    winning_trades:   int   = 0
    losing_trades:    int   = 0
    win_rate:         float = 0.0
    profit_factor:    float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    avg_win_r:        float = 0.0
    avg_loss_r:       float = 0.0
    expectancy_r:     float = 0.0
    sharpe_ratio:     float = 0.0
    best_trade_pct:   float = 0.0
    worst_trade_pct:  float = 0.0
    avg_trade_pct:    float = 0.0
    longest_win_streak:  int = 0
    longest_loss_streak: int = 0
    total_pnl:        float = 0.0
    gross_profit:     float = 0.0
    gross_loss:       float = 0.0
    monthly_returns:  dict  = field(default_factory=dict)
    bars_processed:   int   = 0
    setups_found:     int   = 0
    start_date:       str   = ""
    end_date:         str   = ""
    duration_days:    int   = 0
    run_time_ms:      int   = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_bars(raw: list) -> list:
    return [Bar(b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v",0)) for b in raw]

def load_data() -> dict:
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r',\s*([\]}])', r'\1', f.read())
        return json.loads(raw)
    except Exception as e:
        print(f"[BT] Load error: {e}")
        return {}

def get_session(bar_time: str) -> str:
    try:
        h = int(bar_time[11:13])
        if h >= 22 or h < 7:  return "ASIA"
        if 7  <= h < 12:      return "LONDON"
        if 12 <= h < 17:      return "NEW_YORK"
        return "OVERLAP"
    except: return "UNKNOWN"

def get_amd(bar_time: str) -> str:
    try:
        h = int(bar_time[11:13])
        m = int(bar_time[14:16])
        tot = h * 60 + m
        if h >= 22 or h < 7:     return "ACCUMULATION"
        if 420 <= tot < 570:     return "MANIPULATION"
        if 720 <= tot < 900:     return "DISTRIBUTION"
        if 900 <= tot < 1020:    return "LONDON_CLOSE"
        return "REBALANCE"
    except: return "UNKNOWN"

def lot_size(equity, risk_pct, entry, sl, tick_size=0.1, tick_value=1.0,
             min_lot=0.01, max_lot=500.0, lot_step=0.01) -> float:
    risk_amt = equity * (risk_pct / 100)
    sl_dist  = abs(entry - sl)
    if sl_dist == 0 or tick_size == 0: return min_lot
    rpl = (sl_dist / tick_size) * tick_value
    if rpl <= 0: return min_lot
    ls = math.floor((risk_amt / rpl) / lot_step) * lot_step
    return max(min_lot, min(ls, max_lot))


# ── ICT Pattern Detection (bar-by-bar) ───────────────────────────────────────

def find_swing_highs(bars, lookback=3):
    highs = []
    for i in range(lookback, len(bars)-lookback):
        if all(bars[i].h >= bars[i-j].h and bars[i].h >= bars[i+j].h
               for j in range(1, lookback+1)):
            highs.append((i, bars[i].h))
    return highs

def find_swing_lows(bars, lookback=3):
    lows = []
    for i in range(lookback, len(bars)-lookback):
        if all(bars[i].l <= bars[i-j].l and bars[i].l <= bars[i+j].l
               for j in range(1, lookback+1)):
            lows.append((i, bars[i].l))
    return lows

def find_eq_highs(bars, tol=0.002):
    highs = [b.h for b in bars if b.h > 0]
    result = []
    for h in highs:
        count = sum(1 for x in highs if abs(x-h)/h < tol)
        if count >= 2 and not any(abs(h-r)/h < tol for r in result):
            result.append(h)
    return sorted(result, reverse=True)

def find_eq_lows(bars, tol=0.002):
    lows = [b.l for b in bars if b.l > 0]
    result = []
    for l in lows:
        count = sum(1 for x in lows if abs(x-l)/l < tol)
        if count >= 2 and not any(abs(l-r)/l < tol for r in result):
            result.append(l)
    return sorted(result)

def find_bull_ob(bars, search=10):
    for i in range(min(search, len(bars)-2)):
        b, bn = bars[i], bars[i+1]
        if b.bearish and bn.bullish and bn.body/b.range > 0.6:
            return (b.l, b.h)
    return None

def find_bear_ob(bars, search=10):
    for i in range(min(search, len(bars)-2)):
        b, bn = bars[i], bars[i+1]
        if b.bullish and bn.bearish and bn.body/b.range > 0.6:
            return (b.l, b.h)
    return None

def find_bull_fvg(bars, search=8):
    for i in range(min(search, len(bars)-2)):
        if bars[i].l > bars[i+2].h:
            return (bars[i+2].h, bars[i].l)
    return None

def find_bear_fvg(bars, search=8):
    for i in range(min(search, len(bars)-2)):
        if bars[i].h < bars[i+2].l:
            return (bars[i].h, bars[i+2].l)
    return None

def d1_bias(bars):
    if len(bars) < 6: return "NEUTRAL"
    recent = bars[:10]
    mid = max(len(recent)//2, 1)
    ah1 = sum(b.h for b in recent[:mid])/mid
    ah2 = sum(b.h for b in recent[mid:])/mid
    al1 = sum(b.l for b in recent[:mid])/mid
    al2 = sum(b.l for b in recent[mid:])/mid
    if ah1 > ah2 and al1 > al2: return "BULLISH"
    if ah1 < ah2 and al1 < al2: return "BEARISH"
    return "NEUTRAL"

def score_setup(direction, bias, h4struct, amd, session,
                is_pdh=False, is_pdl=False, is_pwh=False, is_pwl=False) -> int:
    s = 35
    if direction == "BUY":
        if bias == "BULLISH":     s += 20
        if h4struct == "BULL":    s += 15
        if amd == "DISTRIBUTION": s += 15
        if amd == "MANIPULATION": s += 10
        if session in ("LONDON","NEW_YORK"): s += 10
        if is_pdl: s += 15
        if is_pwl: s += 20
    else:
        if bias == "BEARISH":     s += 20
        if h4struct == "BEAR":    s += 15
        if amd == "DISTRIBUTION": s += 15
        if amd == "MANIPULATION": s += 10
        if session in ("LONDON","NEW_YORK"): s += 10
        if is_pdh: s += 15
        if is_pwh: s += 20
    return min(s, 100)


# ── Core Backtest Engine ──────────────────────────────────────────────────────

def run_backtest(config: BacktestConfig) -> BacktestResult:
    import time as _time
    t0 = _time.time()

    result = BacktestResult(config=config, symbol=config.symbol,
                            initial_equity=config.initial_equity)
    equity  = config.initial_equity
    risk    = config.base_risk_pct
    peak    = equity
    win_str = loss_str = 0
    max_ws  = max_ls = 0
    open_trades: list[SimTrade] = []
    trade_id = 0
    gross_p = gross_l = 0.0
    all_r = []
    monthly = {}

    # Load data
    data = load_data()
    charts = data.get("charts", {})
    sym_data = charts.get(config.symbol, {})
    if not sym_data:
        print(f"[BT] No chart data for {config.symbol}. Run EA v4 first to export multi-TF bars.")
        result.bars_processed = 0
        return result

    # Use H1 as the replay timeframe
    h1_raw  = sym_data.get("H1",  [])
    h4_raw  = sym_data.get("H4",  [])
    d1_raw  = sym_data.get("D1",  [])
    m15_raw = sym_data.get("M15", [])

    if len(h1_raw) < 10:
        print(f"[BT] Not enough H1 bars ({len(h1_raw)}). Need more history in MT5.")
        # Generate synthetic data for demo
        h1_raw = _generate_synthetic_bars(config.symbol, 500)
        print(f"[BT] Using {len(h1_raw)} synthetic bars for demo")

    h1_bars  = parse_bars(h1_raw)
    h4_bars  = parse_bars(h4_raw) if h4_raw else []
    d1_bars  = parse_bars(d1_raw) if d1_raw else []

    # Symbol info
    tick_size  = sym_data.get("tick_size",  0.1)
    tick_value = sym_data.get("tick_value", 1.0)
    min_lot    = sym_data.get("min_lot",    0.01)
    max_lot    = sym_data.get("max_lot",    500.0)
    lot_step   = sym_data.get("lot_step",   0.01)
    spread     = config.spread_points * tick_size

    # Key levels from last D1 bars
    pdh = d1_bars[1].h if len(d1_bars) > 1 else 0
    pdl = d1_bars[1].l if len(d1_bars) > 1 else 0
    pwh = max(b.h for b in d1_bars[:5]) if len(d1_bars) >= 5 else 0
    pwl = min(b.l for b in d1_bars[:5]) if len(d1_bars) >= 5 else 0

    n = len(h1_bars)
    result.bars_processed = n
    result.start_date = h1_bars[-1].time if h1_bars else ""
    result.end_date   = h1_bars[0].time  if h1_bars else ""

    daily_start = equity
    current_day = ""

    # Replay bars (oldest first → reverse the list)
    bars_asc = list(reversed(h1_bars))

    for idx in range(20, len(bars_asc)):
        bar      = bars_asc[idx]
        prev_bars= list(reversed(bars_asc[max(0,idx-20):idx]))  # recent bars, newest first

        # Daily reset
        bar_day = bar.time[:10]
        if bar_day != current_day:
            current_day  = bar_day
            daily_start  = equity

        session = get_session(bar.time)
        amd     = get_amd(bar.time)

        # ── Manage open trades ───────────────────────────────────────
        still_open = []
        for tr in open_trades:
            closed = False
            exit_p = 0.0
            exit_r = ""

            if tr.direction == "BUY":
                # Check SL hit
                if bar.l <= tr.sl:
                    exit_p = tr.sl
                    exit_r = "SL"
                    closed = True
                # Check TPs
                elif bar.h >= tr.tp3:
                    exit_p = tr.tp3
                    exit_r = "TP3"
                    closed = True
                elif bar.h >= tr.tp2:
                    exit_p = tr.tp2
                    exit_r = "TP2"
                    closed = True
                elif bar.h >= tr.tp1:
                    exit_p = tr.tp1
                    exit_r = "TP1"
                    closed = True
                # Trail to BE after 1R
                elif bar.h >= tr.entry_price + (tr.entry_price - tr.sl):
                    if tr.sl < tr.entry_price:
                        tr.sl = tr.entry_price + tick_size

            else:  # SELL
                if bar.h >= tr.sl:
                    exit_p = tr.sl
                    exit_r = "SL"
                    closed = True
                elif bar.l <= tr.tp3:
                    exit_p = tr.tp3
                    exit_r = "TP3"
                    closed = True
                elif bar.l <= tr.tp2:
                    exit_p = tr.tp2
                    exit_r = "TP2"
                    closed = True
                elif bar.l <= tr.tp1:
                    exit_p = tr.tp1
                    exit_r = "TP1"
                    closed = True
                elif bar.l <= tr.entry_price - (tr.sl - tr.entry_price):
                    if tr.sl > tr.entry_price:
                        tr.sl = tr.entry_price - tick_size

            if closed:
                # Calculate P&L
                if tr.direction == "BUY":
                    pips = (exit_p - tr.entry_price) / tick_size
                else:
                    pips = (tr.entry_price - exit_p) / tick_size

                pnl  = pips * tick_value * tr.lot_size
                r    = pnl / tr.risk_amount if tr.risk_amount > 0 else 0

                tr.exit_time  = bar.time
                tr.exit_price = exit_p
                tr.exit_reason= exit_r
                tr.pnl        = round(pnl, 2)
                tr.pnl_r      = round(r, 2)

                equity += pnl
                if pnl > 0:
                    gross_p  += pnl
                    win_str  += 1; loss_str = 0
                    result.winning_trades += 1
                    max_ws = max(max_ws, win_str)
                    if win_str >= config.win_streak_boost and config.compound:
                        risk = min(risk + 0.25, config.max_risk_pct)
                else:
                    gross_l  += abs(pnl)
                    loss_str += 1; win_str = 0
                    result.losing_trades += 1
                    max_ls = max(max_ls, loss_str)
                    if loss_str >= config.loss_streak_cut and config.compound:
                        risk = max(risk - 0.25, config.min_risk_pct)

                all_r.append(r)
                result.trades.append(tr)

                # Monthly tracking
                month_key = bar.time[:7]
                monthly[month_key] = monthly.get(month_key, 0) + pnl
            else:
                still_open.append(tr)

        open_trades = still_open

        # Update peak / drawdown
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0

        result.equity_curve.append({
            "time":     bar.time,
            "equity":   round(equity, 2),
            "drawdown": round(dd, 2),
            "trades":   result.winning_trades + result.losing_trades,
            "session":  session,
        })

        # Daily loss limit
        day_loss_pct = (equity - daily_start) / daily_start * 100 if daily_start > 0 else 0
        if day_loss_pct <= -config.daily_loss_limit:
            # Skip rest of day
            while idx + 1 < len(bars_asc) and bars_asc[idx+1].time[:10] == bar_day:
                idx += 1
            continue

        # ── Detect new setups ────────────────────────────────────────
        if len(open_trades) >= config.max_open:
            continue

        if len(prev_bars) < 8:
            continue

        bias    = d1_bias(list(reversed(bars_asc[max(0,idx-10):idx])))
        h4_str  = "BULL" if h4_bars and h4_bars[0].c > h4_bars[0].o else "BEAR"
        cur     = bar.c

        eq_highs = find_eq_highs(prev_bars[:15], tol=0.002)
        eq_lows  = find_eq_lows(prev_bars[:15],  tol=0.002)
        liq_highs = [l for l in eq_highs + [pdh, pwh] if l > cur * 0.999 and l > 0]
        liq_lows  = [l for l in eq_lows  + [pdl, pwl] if l < cur * 1.001 and l > 0]

        new_setup = None

        # ── SELL setup: sweep of high ────────────────────────────────
        for level in sorted(liq_highs)[:4]:
            b1 = prev_bars[0] if prev_bars else None
            b2 = prev_bars[1] if len(prev_bars) > 1 else None
            if b1 and b1.h > level * 1.001 and b1.c < level:
                ob = find_bear_ob(prev_bars[:config.ob_lookback])
                fvg= find_bear_fvg(prev_bars[:config.fvg_lookback])

                if ob:
                    ob_l, ob_h = ob
                    entry   = ob_h + spread
                    sl      = ob_h + (ob_h - ob_l) * 2.0
                    risk_r  = sl - entry
                    if risk_r <= 0: continue
                    tp1 = entry - risk_r * 1.5
                    tp2 = entry - risk_r * 2.5
                    tp3 = entry - risk_r * 4.0

                    conf = score_setup("SELL", bias, h4_str, amd, session,
                                       is_pdh=(level==pdh), is_pwh=(level==pwh))

                    if conf >= config.min_confidence:
                        rr = abs(entry-tp1)/risk_r
                        if rr >= config.min_rr:
                            ls = lot_size(equity, risk, entry, sl,
                                          tick_size, tick_value, min_lot, max_lot, lot_step)
                            ra = equity * risk / 100
                            new_setup = SimTrade(
                                id=trade_id, symbol=config.symbol,
                                direction="SELL", entry_type="SWEEP_HIGH_OB",
                                entry_time=bar.time, entry_price=entry,
                                sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                                lot_size=ls, risk_amount=ra,
                                confidence=conf, session=session, amd_phase=amd,
                            )
                            trade_id += 1
                            result.setups_found += 1
                            break

        # ── BUY setup: sweep of low ──────────────────────────────────
        if not new_setup:
            for level in sorted(liq_lows, reverse=True)[:4]:
                b1 = prev_bars[0] if prev_bars else None
                if b1 and b1.l < level * 0.999 and b1.c > level:
                    ob = find_bull_ob(prev_bars[:config.ob_lookback])
                    fvg= find_bull_fvg(prev_bars[:config.fvg_lookback])

                    if ob:
                        ob_l, ob_h = ob
                        entry   = ob_l - spread
                        sl      = ob_l - (ob_h - ob_l) * 2.0
                        risk_r  = entry - sl
                        if risk_r <= 0: continue
                        tp1 = entry + risk_r * 1.5
                        tp2 = entry + risk_r * 2.5
                        tp3 = entry + risk_r * 4.0

                        conf = score_setup("BUY", bias, h4_str, amd, session,
                                           is_pdl=(level==pdl), is_pwl=(level==pwl))

                        if conf >= config.min_confidence:
                            rr = abs(tp1-entry)/risk_r
                            if rr >= config.min_rr:
                                ls = lot_size(equity, risk, entry, sl,
                                              tick_size, tick_value, min_lot, max_lot, lot_step)
                                ra = equity * risk / 100
                                new_setup = SimTrade(
                                    id=trade_id, symbol=config.symbol,
                                    direction="BUY", entry_type="SWEEP_LOW_OB",
                                    entry_time=bar.time, entry_price=entry,
                                    sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                                    lot_size=ls, risk_amount=ra,
                                    confidence=conf, session=session, amd_phase=amd,
                                )
                                trade_id += 1
                                result.setups_found += 1
                                break

        # ── FVG entry (bias-aligned) ─────────────────────────────────
        if not new_setup and bias == "BULLISH":
            fvg = find_bull_fvg(prev_bars[:6])
            if fvg:
                fvg_l, fvg_h = fvg
                entry  = fvg_l + spread
                sl     = fvg_l - (fvg_h - fvg_l) * 2
                risk_r = entry - sl
                if risk_r > 0:
                    tp1 = entry + risk_r * 1.5
                    tp2 = entry + risk_r * 2.5
                    tp3 = entry + risk_r * 4.0
                    conf = score_setup("BUY", bias, h4_str, amd, session)
                    if conf >= config.min_confidence and abs(tp1-entry)/risk_r >= config.min_rr:
                        ls = lot_size(equity, risk, entry, sl,
                                      tick_size, tick_value, min_lot, max_lot, lot_step)
                        ra = equity * risk / 100
                        new_setup = SimTrade(
                            id=trade_id, symbol=config.symbol,
                            direction="BUY", entry_type="FVG_BULLISH",
                            entry_time=bar.time, entry_price=entry,
                            sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                            lot_size=ls, risk_amount=ra,
                            confidence=conf, session=session, amd_phase=amd,
                        )
                        trade_id += 1
                        result.setups_found += 1

        if new_setup:
            open_trades.append(new_setup)

    # Close any remaining open trades at last price
    last_price = bars_asc[-1].c if bars_asc else 0
    for tr in open_trades:
        pips = ((last_price - tr.entry_price) / tick_size
                if tr.direction == "BUY"
                else (tr.entry_price - last_price) / tick_size)
        pnl = pips * tick_value * tr.lot_size
        tr.exit_time = bars_asc[-1].time
        tr.exit_price = last_price
        tr.exit_reason = "END"
        tr.pnl = round(pnl, 2)
        tr.pnl_r = round(pnl / tr.risk_amount if tr.risk_amount else 0, 2)
        equity += pnl
        all_r.append(tr.pnl_r)
        if pnl >= 0:
            result.winning_trades += 1
            gross_p += pnl
        else:
            result.losing_trades += 1
            gross_l += abs(pnl)
        result.trades.append(tr)

    # ── Final Statistics ──────────────────────────────────────────────
    result.final_equity   = round(equity, 2)
    result.total_pnl      = round(equity - config.initial_equity, 2)
    result.total_return_pct = round((equity - config.initial_equity) / config.initial_equity * 100, 2)
    result.total_trades   = len(result.trades)
    result.gross_profit   = round(gross_p, 2)
    result.gross_loss     = round(gross_l, 2)
    result.win_rate       = round(result.winning_trades / result.total_trades * 100, 1) if result.total_trades else 0
    result.profit_factor  = round(gross_p / gross_l, 2) if gross_l > 0 else 999.0
    result.monthly_returns= {k: round(v, 2) for k, v in monthly.items()}
    result.longest_win_streak  = max_ws
    result.longest_loss_streak = max_ls

    # Drawdown
    equities = [e["equity"] for e in result.equity_curve]
    if equities:
        peak_eq = config.initial_equity
        max_dd_pct = max_dd_usd = 0
        for eq in equities:
            if eq > peak_eq: peak_eq = eq
            dd_usd = peak_eq - eq
            dd_pct = dd_usd / peak_eq * 100 if peak_eq > 0 else 0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_usd = dd_usd
        result.max_drawdown_pct = round(max_dd_pct, 2)
        result.max_drawdown_usd = round(max_dd_usd, 2)

    # R stats
    if all_r:
        wins_r  = [r for r in all_r if r > 0]
        loss_r  = [r for r in all_r if r < 0]
        result.avg_win_r  = round(sum(wins_r)/len(wins_r), 2) if wins_r else 0
        result.avg_loss_r = round(sum(loss_r)/len(loss_r), 2) if loss_r else 0
        wr = result.win_rate / 100
        result.expectancy_r = round(wr * result.avg_win_r + (1-wr) * result.avg_loss_r, 3)

    # P&L percentages
    pnls = [t.pnl for t in result.trades]
    if pnls:
        result.best_trade_pct  = round(max(pnls)/config.initial_equity*100, 2)
        result.worst_trade_pct = round(min(pnls)/config.initial_equity*100, 2)
        result.avg_trade_pct   = round(sum(pnls)/len(pnls)/config.initial_equity*100, 3)

    # Sharpe (simplified annualized on daily returns)
    daily_rets = list(monthly.values())
    if len(daily_rets) > 1:
        import statistics
        avg_r = statistics.mean(daily_rets)
        std_r = statistics.stdev(daily_rets)
        result.sharpe_ratio = round((avg_r / std_r) * math.sqrt(12), 2) if std_r > 0 else 0

    result.run_time_ms = int((_time.time() - t0) * 1000)

    # Save results
    _save_results(result)
    return result


def _save_results(result: BacktestResult):
    """Save backtest results to JSON for dashboard to read."""
    try:
        # Convert to serializable dict
        d = {
            "symbol":           result.symbol,
            "start_date":       result.start_date,
            "end_date":         result.end_date,
            "bars_processed":   result.bars_processed,
            "initial_equity":   result.initial_equity,
            "final_equity":     result.final_equity,
            "total_pnl":        result.total_pnl,
            "total_return_pct": result.total_return_pct,
            "total_trades":     result.total_trades,
            "winning_trades":   result.winning_trades,
            "losing_trades":    result.losing_trades,
            "win_rate":         result.win_rate,
            "profit_factor":    result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "max_drawdown_usd": result.max_drawdown_usd,
            "avg_win_r":        result.avg_win_r,
            "avg_loss_r":       result.avg_loss_r,
            "expectancy_r":     result.expectancy_r,
            "sharpe_ratio":     result.sharpe_ratio,
            "best_trade_pct":   result.best_trade_pct,
            "worst_trade_pct":  result.worst_trade_pct,
            "gross_profit":     result.gross_profit,
            "gross_loss":       result.gross_loss,
            "longest_win_streak":  result.longest_win_streak,
            "longest_loss_streak": result.longest_loss_streak,
            "setups_found":     result.setups_found,
            "run_time_ms":      result.run_time_ms,
            "monthly_returns":  result.monthly_returns,
            "equity_curve": result.equity_curve,
            "trades": [{
                "id":          t.id,
                "symbol":      t.symbol,
                "direction":   t.direction,
                "entry_type":  t.entry_type,
                "entry_time":  t.entry_time,
                "entry_price": t.entry_price,
                "exit_time":   t.exit_time,
                "exit_price":  t.exit_price,
                "exit_reason": t.exit_reason,
                "sl":          t.sl,
                "tp1":         t.tp1,
                "tp2":         t.tp2,
                "tp3":         t.tp3,
                "lot_size":    t.lot_size,
                "pnl":         t.pnl,
                "pnl_r":       t.pnl_r,
                "confidence":  t.confidence,
                "session":     t.session,
                "amd_phase":   t.amd_phase,
            } for t in result.trades],
        }
        with open(RESULTS_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        print(f"[BT] Save error: {e}")


def _generate_synthetic_bars(symbol: str, count: int) -> list:
    """Generate realistic synthetic OHLCV bars for demo when no real data."""
    random.seed(42)
    bars = []
    # Base prices by symbol
    prices = {"XAUUSD": 2300.0, "XAGUSD": 27.0, "EURUSD": 1.0850,
              "GBPUSD": 1.2650, "USDJPY": 149.50, "AUDUSD": 0.6550}
    price = prices.get(symbol, 1.1000)
    volatility = price * 0.003  # 0.3% per bar

    now = datetime.utcnow()
    for i in range(count, 0, -1):
        bar_time = now - timedelta(hours=i)
        change   = random.gauss(0, volatility)
        o = price
        c = price + change
        h = max(o, c) + abs(random.gauss(0, volatility * 0.5))
        l = min(o, c) - abs(random.gauss(0, volatility * 0.5))
        bars.append({"t": bar_time.strftime("%Y-%m-%d %H:%M:%S"),
                     "o": round(o,5), "h": round(h,5),
                     "l": round(l,5), "c": round(c,5), "v": random.randint(100,2000)})
        price = c

    return bars


def print_report(result: BacktestResult):
    r = result
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              OMNI ICT BACKTEST REPORT                        ║
╠══════════════════════════════════════════════════════════════╣
║  Symbol:  {r.symbol:<10}  Bars: {r.bars_processed:<6}  Setups: {r.setups_found:<5}    ║
║  Period:  {r.start_date[:10]} → {r.end_date[:10]}               ║
╠══════════════════════════════════════════════════════════════╣
║  EQUITY                                                      ║
║  Initial:  ${r.initial_equity:>10,.2f}                              ║
║  Final:    ${r.final_equity:>10,.2f}  ({r.total_return_pct:+.1f}%)              ║
║  Total P&L:${r.total_pnl:>+10,.2f}                              ║
╠══════════════════════════════════════════════════════════════╣
║  TRADES                                                      ║
║  Total:    {r.total_trades:<5}  Wins: {r.winning_trades:<5}  Losses: {r.losing_trades:<5}          ║
║  Win Rate: {r.win_rate:.1f}%                                        ║
║  Profit Factor: {r.profit_factor:.2f}     Sharpe: {r.sharpe_ratio:.2f}              ║
╠══════════════════════════════════════════════════════════════╣
║  RISK                                                        ║
║  Max DD:   {r.max_drawdown_pct:.2f}%  (${r.max_drawdown_usd:,.2f})                    ║
║  Avg Win:  {r.avg_win_r:.2f}R      Avg Loss: {r.avg_loss_r:.2f}R              ║
║  Expectancy: {r.expectancy_r:.3f}R per trade                       ║
║  Best:    {r.best_trade_pct:+.2f}%   Worst: {r.worst_trade_pct:.2f}%                 ║
╠══════════════════════════════════════════════════════════════╣
║  STREAKS                                                     ║
║  Best win streak:  {r.longest_win_streak:<3}   Best loss streak: {r.longest_loss_streak:<3}          ║
╠══════════════════════════════════════════════════════════════╣
║  MONTHLY RETURNS                                             ║""")
    for month, ret in sorted(r.monthly_returns.items())[-6:]:
        bar = "█" * min(int(abs(ret)/50), 20)
        sign = "+" if ret >= 0 else ""
        print(f"║  {month}:  {sign}{ret:>8.2f}  {bar:<20}        ║")
    print(f"╚══════════════════════════════════════════════════════════════╝")
    print(f"  Completed in {r.run_time_ms}ms\n")

    if r.total_return_pct > 20 and r.max_drawdown_pct < 15 and r.win_rate > 45:
        print("  ✅ Strategy looks solid. Consider running live with small lots.")
    elif r.total_return_pct > 0 and r.profit_factor > 1.2:
        print("  ⚠️  Profitable but needs more data. Run on more symbols.")
    else:
        print("  ❌ Strategy needs tuning. Adjust parameters and re-run.")


if __name__ == "__main__":
    print("\n  OMNI ICT Backtester — starting...\n")
    cfg = BacktestConfig(
        symbol="XAUUSD",
        initial_equity=10000.0,
        base_risk_pct=2.0,
        min_confidence=50,
        min_rr=1.5,
        compound=True,
    )
    result = run_backtest(cfg)
    print_report(result)
    print(f"  Results saved to: {RESULTS_PATH}")
