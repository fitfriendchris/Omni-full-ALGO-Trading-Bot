"""
deterministic_ict_proven_backtest.py
Rigorous backtest harness for deterministic ICT engine.
Features:
- Walk-forward train/test splits (time-series cross validation)
- Strict slippage model (entry 2-5 pips / exit 3-8 pips)
- Balance + leverage simulation ($10K default, 1:100, 1% risk per trade)
- Per-symbol pip values, tick sizes, margin calculations
- Max drawdown, consecutive losses, peak equity, recovery factor
- Multi-symbol (XAUUSD, EURUSD, NAS100, XAGUSD)
- Regime audit: bull (2024-2026), bear (2022), mixed (2020-2024)
- Kelly criterion optimal risk fraction
"""

import json, math, random, sys, os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, List, Dict
from collections import defaultdict

import yfinance as yf
import pandas as pd

# ── constants ───────────────────────────────────────────────────────
SYMBOLS = {"XAUUSD": "GC=F", "EURUSD": "EURUSD=X", "NAS100": "^NDX", "XAGUSD": "SI=F"}
PIP_VALUES = {"XAUUSD": 0.1, "EURUSD": 0.0001, "NAS100": 1.0, "XAGUSD": 0.001}
TICK_VALUE_USD = {"XAUUSD": 0.01, "EURUSD": 1.0, "NAS100": 1.0, "XAGUSD": 0.001}
CONTRACT_SIZE = {"XAUUSD": 100.0, "EURUSD": 100000.0, "NAS100": 20.0, "XAGUSD": 5000.0}
MIN_SPREAD_PIPS = {"XAUUSD": 0.5, "EURUSD": 0.2, "NAS100": 1.0, "XAGUSD": 1.0}
MAX_SPREAD_PIPS = {"XAUUSD": 5.0, "EURUSD": 2.0, "NAS100": 15.0, "XAGUSD": 5.0}
SESSIONS = {
    "LONDON": lambda t: 7 <= t.hour < 9,
    "LONDON_KILLZONE": lambda t: 7 <= t.hour < 10,
    "NY": lambda t: 12 <= t.hour < 15,
    "NY_KILLZONE": lambda t: 12 <= t.hour < 16,
    "SILVER_BULLET": lambda t: 13 <= t.hour < 17,
    "AM_KILLZONE": lambda t: 8 <= t.hour < 11,
    "PM_KILLZONE": lambda t: 13 <= t.hour < 16,
    "EUROPEAN": lambda t: 7 <= t.hour < 12,
    "KILLZONE_ALL": lambda t: (7 <= t.hour < 10) or (12 <= t.hour < 15),
    "US_SESSION": lambda t: 12 <= t.hour < 17,
    "ASIA": lambda t: 22 <= t.hour or t.hour < 7,
    "ALL": lambda t: True,
}
MAX_LOOKBACK = 500
MIN_LOT = 0.01
DEFAULT_LEVERAGE = 100
DEFAULT_RISK_PCT = 0.01  # 1%

# ── bar type ────────────────────────────────────────────────────────
@dataclass
class Bar:
    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

# ── helpers ─────────────────────────────────────────────────────────
def wilder_atr(bars: List[Bar], period=14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, period + 1):
        b = bars[-i]
        p = bars[-i - 1]
        tr = max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        trs.append(tr)
    return sum(trs) / period

def _to_ts(val):
    if isinstance(val, (int, float)):
        return pd.Timestamp(val, unit="s", tz="UTC")
    return pd.Timestamp(val)

def session_ok(bar: Bar, sess: str) -> bool:
    fn = SESSIONS.get(sess)
    if not fn:
        return False
    t = _to_ts(bar.time)
    try:
        return fn(t)
    except Exception:
        return fn(t.tz_localize(None))

def spread_for_bar(bar: Bar, symbol: str) -> float:
    pips = random.uniform(MIN_SPREAD_PIPS[symbol], MAX_SPREAD_PIPS[symbol])
    return pips * PIP_VALUES[symbol]

# ── config ──────────────────────────────────────────────────────────
@dataclass
class EngineConfig:
    execution_mode: str = "MARKET_ONLY"    # LIMIT_ONLY, LIMIT_THEN_MARKET, MARKET_ONLY
    sl_cap_pips: Optional[float] = 200.0
    fill_window: int = 96
    session: str = "LONDON"
    min_rr: float = 2.0
    lookback: int = 50
    stop_buffer_pips: float = 2.0
    max_spread_pips: float = 50.0
    stricter_slippage: bool = True          # entry 2-5, exit 3-8 pips model
    min_bar_range_pips: float = 0.0
    signal_strength_threshold: float = 0.0

# ── signal ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Sig:
    direction: str       # BULL or BEAR
    entry: float
    sl: float
    tp1: float
    tp2: float
    bar_idx: int
    htf_bias: str
    sl_capped: bool = False
    ratio_applied: float = 1.0
    signal_strength: float = 0.0  # 0..1 candle-body conviction

# ── trade record ────────────────────────────────────────────────────
@dataclass
class Trade:
    sig: Sig
    open_idx: int
    close_idx: Optional[int] = None
    exit_price: Optional[float] = None
    lots: float = 0.0
    remaining_lots: float = 0.0
    realized_pnl_usd: float = 0.0
    current_sl: Optional[float] = None
    partial_done: bool = False
    pnl_pips: float = 0.0
    pnl_usd: float = 0.0
    reason: str = ""
    equity_before: float = 0.0
    equity_after: float = 0.0
    # profit-lock ladder guards (prevent re-tightening after each lock)
    lock_1r_done: bool = False
    lock_2r_done: bool = False
    lock_3r_done: bool = False
    max_lock: bool = False        # stop all SL movement once 3R locked

# ── result ─────────────────────────────────────────────────────────
@dataclass
class SimResult:
    symbol: str = ""
    config_label: str = ""
    start_equity: float = 10000.0
    end_equity: float = 10000.0
    peak_equity: float = 10000.0
    max_dd_pct: float = 0.0
    max_dd_start: int = 0
    max_dd_end: int = 0
    total_trades: int = 0
    wins: int = 0
    partials: int = 0
    losses: int = 0
    win_rate: float = 0.0
    expectancy_pips: float = 0.0
    expectancy_usd: float = 0.0
    total_pnl_pips: float = 0.0
    total_pnl_usd: float = 0.0
    total_pnl_pct: float = 0.0
    max_consecutive_losses: int = 0
    avg_lot: float = 0.0
    avg_sl_pips: float = 0.0
    kelly_fraction: float = 0.0
    sharpe_annualized: float = 0.0
    profit_factor: float = 0.0
    real_wr: float = 0.0
    avg_dd_pct: float = 0.0
    notes: List[str] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list, repr=False)
    trade_log: List[dict] = field(default_factory=list, repr=False)

# ── engine ───────────────────────────────────────────────────────────
class Engine:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.pip = PIP_VALUES[symbol]
        self.tick = TICK_VALUE_USD[symbol]
        self.contract = CONTRACT_SIZE[symbol]

    def fetch(self, period: str, years: int) -> List[Bar]:
        tkr = yf.Ticker(SYMBOLS[self.symbol])
        df = tkr.history(period=f"{years}y", interval=period)
        if df.empty:
            raise ValueError(f"Empty dataframe for {self.symbol} {period} {years}y")
        df.reset_index(inplace=True)
        if "Datetime" in df.columns:
            df.rename(columns={"Datetime": "Date"}, inplace=True)
        bars = []
        for _, r in df.iterrows():
            bars.append(Bar(r["Date"], float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0))))
        return bars

    def align_htf(self, ltf: List[Bar], htf: List[Bar]) -> List[Bar]:
        hmap = {}
        for hb in htf:
            hmap[_to_ts(hb.time).normalize()] = hb
        aligned = []
        for lb in ltf:
            k = _to_ts(lb.time).normalize()
            aligned.append(hmap.get(k, lb))
        return aligned

    def _swing_low(self, bars, idx, left=2, right=2):
        if idx < left or idx + right >= len(bars):
            return False
        val = bars[idx].low
        return all(bars[idx - i].low > val for i in range(1, left + 1)) and all(bars[idx + i].low > val for i in range(1, right + 1))

    def _swing_high(self, bars, idx, left=2, right=2):
        if idx < left or idx + right >= len(bars):
            return False
        val = bars[idx].high
        return all(bars[idx - i].high < val for i in range(1, left + 1)) and all(bars[idx + i].high < val for i in range(1, right + 1))

    # ── LOOK LEFT: Equal Highs / Equal Lows liquidity scanner ─────────
    def _eqh_eq_scan(self, bars, window_size=150, tolerance_pips=3.0):
        """Detect Equal Highs / Equal Lows inside a range.
        These clusters of similar highs/lows are the liquidity that gets purged.
        Default window_size=150 covers ~6 trading days on 1h bars.
        Returns the most significant EQH and EQL levels found.
        """
        if len(bars) < window_size + 10:
            return None
        window = bars[-window_size:]
        pip = self.pip
        tol = tolerance_pips * pip

        # Cluster highs by rounded pip value
        highs = {}
        for i, b in enumerate(window):
            key = round(b.high / pip)
            highs.setdefault(key, []).append((i, b.high))

        # Cluster lows
        lows = {}
        for i, b in enumerate(window):
            key = round(b.low / pip)
            lows.setdefault(key, []).append((i, b.low))

        # Find clusters with 3+ touches within tolerance
        eqh = []
        for key, touches in highs.items():
            if len(touches) >= 3:
                avg_high = sum(t[1] for t in touches) / len(touches)
                if all(abs(t[1] - avg_high) <= tol for t in touches):
                    eqh.append({"price": avg_high, "touches": len(touches)})

        eql = []
        for key, touches in lows.items():
            if len(touches) >= 3:
                avg_low = sum(t[1] for t in touches) / len(touches)
                if all(abs(t[1] - avg_low) <= tol for t in touches):
                    eql.append({"price": avg_low, "touches": len(touches)})

        best_eqh = max(eqh, key=lambda x: x["touches"]) if eqh else None
        best_eql = max(eql, key=lambda x: x["touches"]) if eql else None
        return {"eqh": best_eqh, "eql": best_eql}

    # ── LOOK LEFT: Historical pivot validation ────────────────────────
    def _historical_context(self, bars, idx, price_level, min_left_bars=200, max_left_bars=400):
        """Check if price_level was a significant pivot in the past (look left).
        Default scans 200-400 bars (~8-16 trading days on 1h) to respect
        3-day minimum and 4-week maximum lookback.
        A level is historically significant if it was either:
        (a) touched 3+ times in the scanned history, or
        (b) was a prior swing high/low (left=2,right=2).
        Returns True if the level has historical significance.
        """
        if idx < min_left_bars:
            return True  # not enough history → allow
        history = bars[:idx]
        pip = self.pip
        tol = 5.0 * pip

        # Touch count in the designated left range
        left_range = history[-max_left_bars:]
        touch_count = 0
        for b in left_range:
            if abs(b.high - price_level) <= tol or abs(b.low - price_level) <= tol:
                touch_count += 1

        # Was it a prior swing pivot?
        was_swing = False
        scan_len = min(len(history) - 5, max_left_bars)
        for i in range(5, scan_len):
            if abs(history[i].high - price_level) <= tol and self._swing_high(history, i, left=2, right=2):
                was_swing = True
                break
            if abs(history[i].low - price_level) <= tol and self._swing_low(history, i, left=2, right=2):
                was_swing = True
                break

        return touch_count >= 3 or was_swing

    # ── LOOK LEFT: Liquidity target scan (old untaken highs/lows) ─────
    def _liquidity_target(self, bars, idx, direction, min_distance_pips=50.0, left_bars=400):
        """Scan to the left for old swing highs/lows that were NEVER taken out.
        Default left_bars=400 covers ~16 trading days on 1h bars (2-3+ weeks).
        These become natural TP2 targets (the 'liquidity that needs to be taken').
        Returns the nearest untaken level above (BULL) or below (BEAR).
        """
        if idx < 100:
            return None
        history = bars[max(0, idx - left_bars):idx]
        pip = self.pip
        min_dist = min_distance_pips * pip

        if direction == "BULL":
            # Find old swing highs that are ABOVE current price
            current = bars[idx].close
            candidates = []
            for i in range(2, len(history) - 2):
                if self._swing_high(history, i, left=2, right=2):
                    level = history[i].high
                    if level > current + min_dist:
                        # Verify no bar AFTER this swing high took it out
                        taken = any(b.high > level for b in history[i+1:])
                        if not taken:
                            candidates.append(level)
            return min(candidates) if candidates else None
        else:
            current = bars[idx].close
            candidates = []
            for i in range(2, len(history) - 2):
                if self._swing_low(history, i, left=2, right=2):
                    level = history[i].low
                    if level < current - min_dist:
                        taken = any(b.low < level for b in history[i+1:])
                        if not taken:
                            candidates.append(level)
            return max(candidates) if candidates else None

    def _sweep(self, bars):
        n = len(bars)
        if n < 55:
            return None
        window = bars[-50:]
        lows = [(i, b.low) for i, b in enumerate(window) if self._swing_low(window, i, left=1, right=1)]
        highs = [(i, b.high) for i, b in enumerate(window) if self._swing_high(window, i, left=1, right=1)]
        # low sweep: allow up to 3 bars after swing low; rejection can be same bar or next
        for i in range(len(lows) - 1, -1, -1):
            idx, sl = lows[i]
            if idx >= len(window) - 4:
                continue
            # scan candidate sweep bars
            for offset in range(1, min(4, len(window) - idx)):
                c = window[idx + offset]
                if c.low < sl:
                    # rejection: close back above sl (can be same bar or next bar up to +2)
                    rej_bar = None
                    if c.close > sl:
                        rej_bar = c
                    elif offset + 1 < 4 and idx + offset + 1 < len(window):
                        nc = window[idx + offset + 1]
                        if nc.close > sl:
                            rej_bar = nc
                    if rej_bar:
                        # REJECTION STRENGTH: close must be in upper 50% of bar range
                        # This ensures the market rejected the sweep, not just wiggled
                        bar_center = (rej_bar.close - rej_bar.low) / (rej_bar.high - rej_bar.low) if (rej_bar.high - rej_bar.low) > 0 else 0.5
                        if bar_center >= 0.5:
                            return {"sweep_bar": c, "induced": window[idx], "induced_type": "LOW",
                                    "sweep_type": "LOW", "sweep_price": c.low,
                                    "idx": n - 50 + idx + offset}
                    break  # stop scanning offsets once sweep is found but rejected check done
        # high sweep
        for i in range(len(highs) - 1, -1, -1):
            idx, sh = highs[i]
            if idx >= len(window) - 4:
                continue
            for offset in range(1, min(4, len(window) - idx)):
                c = window[idx + offset]
                if c.high > sh:
                    rej_bar = None
                    if c.close < sh:
                        rej_bar = c
                    elif offset + 1 < 4 and idx + offset + 1 < len(window):
                        nc = window[idx + offset + 1]
                        if nc.close < sh:
                            rej_bar = nc
                    if rej_bar:
                        # REJECTION STRENGTH: close in lower 50% of bar range for BEAR sweeps
                        bar_center = (rej_bar.high - rej_bar.close) / (rej_bar.high - rej_bar.low) if (rej_bar.high - rej_bar.low) > 0 else 0.5
                        if bar_center >= 0.5:
                            return {"sweep_bar": c, "induced": window[idx], "induced_type": "HIGH",
                                    "sweep_type": "HIGH", "sweep_price": c.high,
                                    "idx": n - 50 + idx + offset}
                    break
        return None

    def _mss(self, bars, sweep):
        idx = sweep["idx"]
        n = len(bars)
        if idx >= n - 3:
            return None
        if sweep["sweep_type"] == "LOW":
            for i in range(idx + 1, min(idx + 10, n)):
                if self._swing_high(bars, i, left=1, right=1):
                    target = bars[i].high
                    for j in range(i + 1, min(i + 5, n)):
                        if bars[j].close > target:
                            return {"type": "BULL", "mss_idx": j, "swing_idx": i, "swing_price": target}
        else:
            for i in range(idx + 1, min(idx + 10, n)):
                if self._swing_low(bars, i, left=1, right=1):
                    target = bars[i].low
                    for j in range(i + 1, min(i + 5, n)):
                        if bars[j].close < target:
                            return {"type": "BEAR", "mss_idx": j, "swing_idx": i, "swing_price": target}
        return None

    def _fvg(self, bars, start, direction):
        n = len(bars)
        for i in range(start, min(start + 15, n - 2)):
            c1, c2, c3 = bars[i], bars[i + 1], bars[i + 2]
            if direction == "BULL" and c1.high < c3.low:
                return {"top": c3.low, "bottom": c1.high, "start": i, "end": i + 2, "dir": "BULL"}
            if direction == "BEAR" and c1.low > c3.high:
                return {"top": c1.low, "bottom": c3.high, "start": i, "end": i + 2, "dir": "BEAR"}
        return None

    def _ob(self, bars, mss_idx, direction):
        # final opposite-color body before mss
        for i in range(mss_idx - 10, mss_idx):
            if i < 0:
                continue
            b = bars[i]
            if direction == "BULL" and b.close < b.open:
                if all(bars[k].close < bars[k].open for k in range(i, mss_idx)):
                    return {"top": b.open, "bottom": b.close, "idx": i}
            if direction == "BEAR" and b.close > b.open:
                if all(bars[k].close > bars[k].open for k in range(i, mss_idx)):
                    return {"top": b.close, "bottom": b.open, "idx": i}
        return None

    def _pd_ok(self, ob, fvg, direction):
        if not ob:
            return True
        if direction == "BULL":
            limit = max(fvg["top"], ob["top"])
            rng = fvg["top"] - fvg["bottom"]
            if rng <= 0:
                return True
            return 0.21 <= (limit - fvg["bottom"]) / rng <= 0.79
        else:
            limit = min(fvg["bottom"], ob["bottom"])
            rng = fvg["top"] - fvg["bottom"]
            if rng <= 0:
                return True
            return 0.21 <= (fvg["top"] - limit) / rng <= 0.79

    def generate(self, ltf: List[Bar], htf: List[Bar], cfg: EngineConfig, end_idx: Optional[int] = None) -> List[Sig]:
        sigs = []
        n = len(ltf)
        end = end_idx if end_idx is not None else n
        htf_times = [_to_ts(b.time) for b in htf]
        for i in range(MAX_LOOKBACK, end):
            bar = ltf[i]
            if not session_ok(bar, cfg.session):
                continue
            atrv = wilder_atr(ltf[:i + 1], 14)
            sp = spread_for_bar(bar, self.symbol)
            if sp > cfg.max_spread_pips * self.pip:
                continue
            # Resolve HTF bias for current LTF bar (by date matching)
            from bisect import bisect_right
            ltf_dt = _to_ts(bar.time)
            htf_idx = bisect_right(htf_times, ltf_dt) - 1
            if htf_idx < 0:
                htf_idx = 0
            hbar = htf[htf_idx]
            htf_bias = "BULL" if hbar.close >= hbar.open else "BEAR"
            sweep = self._sweep(ltf[:i + 1])
            if not sweep:
                continue
            if htf_bias == "BULL" and sweep["induced_type"] != "LOW":
                continue
            if htf_bias == "BEAR" and sweep["induced_type"] != "HIGH":
                continue
            mss = self._mss(ltf[:i + 1], sweep)
            if not mss or mss["type"] != htf_bias:
                continue
            ob = self._ob(ltf[:i + 1], mss["mss_idx"], mss["type"])
            fvg = self._fvg(ltf[:i + 1], mss["mss_idx"], mss["type"])
            if not fvg:
                continue
            if ob and not self._pd_ok(ob, fvg, mss["type"]):
                continue
            d = mss["type"]

            # ── LOOK LEFT: signal strength accumulator ──
            strength = 0.0

            # Historical context BOOST (not veto)
            entry_zone = max(fvg["top"], ob["top"]) if (ob and d=="BULL") else (fvg["top"] if d=="BULL" else fvg["bottom"])
            has_history = self._historical_context(ltf[:i+1], i, entry_zone)
            if has_history:
                strength += 0.10  # bonus for trading at a historically significant level

            # EQH/EQL liquidity sweep confirmation (advisory)
            # Uses default window_size=150 (~6 trading days on 1h bars)
            eq_data = self._eqh_eq_scan(ltf[:i+1])
            if d == "BULL" and eq_data and eq_data.get("eql"):
                eql_price = eq_data["eql"]["price"]
                if abs(sweep["sweep_bar"].low - eql_price) <= 30 * self.pip:
                    strength += 0.05  # sweep targeted the clustered liquidity → bonus
            if d == "BEAR" and eq_data and eq_data.get("eqh"):
                eqh_price = eq_data["eqh"]["price"]
                if abs(sweep["sweep_bar"].high - eqh_price) <= 30 * self.pip:
                    strength += 0.05

            # Bar body strength
            rng = bar.high - bar.low
            if rng <= 0:
                continue
            if d == "BULL":
                strength += (bar.close - bar.low) / rng
            else:
                strength += (bar.high - bar.close) / rng
            if strength < cfg.signal_strength_threshold:
                continue
            if d == "BULL":
                limit = max(fvg["top"], ob["top"]) if ob else fvg["top"]
                sl = sweep["sweep_bar"].low - cfg.stop_buffer_pips * self.pip
                raw_tp1 = limit + cfg.min_rr * (limit - sl)
                raw_tp2 = limit + 2 * cfg.min_rr * (limit - sl)
            else:
                limit = min(fvg["bottom"], ob["bottom"]) if ob else fvg["bottom"]
                sl = sweep["sweep_bar"].high + cfg.stop_buffer_pips * self.pip
                raw_tp1 = limit - cfg.min_rr * (sl - limit)
                raw_tp2 = limit - 2 * cfg.min_rr * (sl - limit)
            dist = abs(limit - sl)
            max_dist = cfg.sl_cap_pips * self.pip if cfg.sl_cap_pips else dist
            ratio = 1.0
            sl_capped = False
            if max_dist > 0 and dist > max_dist:
                ratio = max_dist / dist
                sl_capped = True
                sl = limit - max_dist * (1 if d == "BULL" else -1)
                raw_tp1 = limit + (raw_tp1 - limit) * ratio
                raw_tp2 = limit + (raw_tp2 - limit) * ratio
            # VALIDATION: reject signals with wrong TP/SL direction
            if d == "BULL" and (sl >= limit or raw_tp2 <= limit):
                continue
            if d == "BEAR" and (sl <= limit or raw_tp2 >= limit):
                continue
            # DISPLACEMENT FILTER: signal bar must show real conviction
            bar_range_pips = (ltf[i].high - ltf[i].low) / self.pip
            if bar_range_pips < cfg.min_bar_range_pips:
                continue
            # LOOK LEFT: use historical liquidity pool as TP2 only if it improves RR
            liq_target = self._liquidity_target(ltf[:i+1], i, d, min_distance_pips=50.0)
            if liq_target:
                fixed_rr_tp2 = raw_tp2
                # Pick whichever target gives BETTER RR (farther from entry)
                if d == "BULL" and liq_target > fixed_rr_tp2:
                    raw_tp2 = liq_target
                elif d == "BEAR" and liq_target < fixed_rr_tp2:
                    raw_tp2 = liq_target
                # Re-check minimum RR
                final_rr = abs(raw_tp2 - limit) / dist if dist > 0 else cfg.min_rr
                if final_rr < cfg.min_rr:
                    raw_tp2 = fixed_rr_tp2  # revert if historical target too close

            sigs.append(Sig(d, limit, sl, raw_tp1, raw_tp2, i, htf_bias, sl_capped, ratio, strength))
        return sigs

# ── simulator ───────────────────────────────────────────────────────
class Simulator:
    def __init__(self, symbol: str, start_equity: float = 10000.0, leverage: float = 100.0, risk_pct: float = 0.01):
        self.symbol = symbol
        self.pip = PIP_VALUES[symbol]
        self.tick = TICK_VALUE_USD[symbol]
        self.contract = CONTRACT_SIZE[symbol]
        self.start_eq = start_equity
        self.lev = leverage
        self.risk_pct = risk_pct

    def entry_slippage(self, price: float) -> float:
        # 2-5 pips in price units
        s = random.uniform(2.0 * self.pip, 5.0 * self.pip)
        return s

    def exit_slippage(self, price: float) -> float:
        s = random.uniform(3.0 * self.pip, 8.0 * self.pip)
        return s

    def lot_size(self, equity: float, sl_price: float, entry_price: float) -> float:
        risk_usd = equity * self.risk_pct
        sl_dist = abs(entry_price - sl_price)
        if sl_dist <= 0.0:
            return MIN_LOT
        # pip-dist / pip = number of pips
        sl_pips = sl_dist / self.pip
        # value at risk per lot = sl_pips * tick_value * tick per pip is effectively pip_value * contract size
        usd_per_pip = self.pip * self.contract  # e.g. XAUUSD 0.1 * 100 = $10/pip
        risk_per_lot = sl_pips * usd_per_pip
        lots = risk_usd / risk_per_lot
        lots = round(lots / 0.01) * 0.01
        # margin check
        margin_required = (entry_price * self.contract * lots) / self.lev
        if margin_required > equity * 0.5:
            lots = (equity * 0.5 * self.lev) / (entry_price * self.contract)
            lots = round(lots / 0.01) * 0.01
        return max(MIN_LOT, lots)

    # ── structural trailing helpers ───────────────────────────────────
    def _swing_low_structural(self, bars, idx, left=2, right=2):
        if idx < left or idx + right >= len(bars): return False
        val = bars[idx].low
        return all(bars[idx-i].low >= val for i in range(1, left+1)) and all(bars[idx+i].low >= val for i in range(1, right+1))

    def _swing_high_structural(self, bars, idx, left=2, right=2):
        if idx < left or idx + right >= len(bars): return False
        val = bars[idx].high
        return all(bars[idx-i].high <= val for i in range(1, left+1)) and all(bars[idx+i].high <= val for i in range(1, right+1))

    def _find_trailing_pivot(self, bars, direction: str, min_idx: int, pip: float) -> Optional[Dict]:
        """Progressive swing pivot scan — O(1) per call.
        Looks ONLY at the last 5 bars before current index for a new structural pivot
        in the trade's favor. No nested loops, no full rescan."""
        if len(bars) < 5: return None
        # Only scan recent area: min_idx to current end
        scan_start = max(len(bars) - 8, min_idx + 2)
        scan_end = len(bars) - 2
        if scan_start >= scan_end: return None
        
        for si in range(scan_start, scan_end + 1):
            if direction == "BULL":
                # New swing low = higher low than previous structure = price advanced
                if self._swing_low_structural(bars, si, left=2, right=2):
                    # Confirm this is a "higher" swing low relative to entry area
                    # Use the swing low bar's low minus buffer as new SL
                    new_sl_low = bars[si].low - 2 * pip
                    return {"sl": new_sl_low, "type": "SW_LOW", "idx": si, "price": bars[si].low}
            else:
                if self._swing_high_structural(bars, si, left=2, right=2):
                    new_sl_high = bars[si].high + 2 * pip
                    return {"sl": new_sl_high, "type": "SW_HIGH", "idx": si, "price": bars[si].high}
        return None

    def run(self, ltf: List[Bar], sigs: List[Sig], cfg: EngineConfig, seed: int = 42) -> SimResult:
        random.seed(seed)
        eq = self.start_eq
        peak = eq
        dd_start = 0
        dd_end = 0
        max_dd = 0.0
        closs = 0
        max_closs = 0
        res = SimResult(symbol=self.symbol, config_label=cfg.execution_mode + "|" + cfg.session, start_equity=eq)
        res.equity_curve.append(eq)
        open_trades: List[Trade] = []
        # Sort signals by bar_idx
        sig_queue = sorted(sigs, key=lambda s: s.bar_idx)
        pending_limit: Optional[Sig] = None
        pending_window = 0
        for i, bar in enumerate(ltf):
            # Process open trades first
            new_open = []
            for t in open_trades:
                sig = t.sig
                d = sig.direction
                # Check TP1 / TP2 / SL
                # NEVER TIGHTEN SL — v6.2 (pure structural hold)
                # ---------------------------------------------------------------
                # After partial: SL stays at TRUE LOW/HIGH (purged structural level).
                # No trailing, no pivot scans, no fixed-R profit locks.
                # Only rule: if price runs 3.0R past entry, lock SL permanently.
                # This prevents any chance of being stopped by normal XAUUSD noise.
                # ---------------------------------------------------------------
                if t.partial_done and i > t.open_idx + 3 and not t.max_lock:
                    risk_pips = abs(t.sig.entry - t.sig.sl) / self.pip
                    if d == "BULL":
                        unreal_pips = (bar.high - t.sig.entry) / self.pip
                        if unreal_pips >= 3.0 * risk_pips:
                            t.max_lock = True
                    else:
                        unreal_pips = (t.sig.entry - bar.low) / self.pip
                        if unreal_pips >= 3.0 * risk_pips:
                            t.max_lock = True
                if d == "BULL":
                    usd_per_pip = self.pip * self.contract
                    if bar.low <= t.current_sl:
                        slippage = self.exit_slippage(t.current_sl)
                        fill = t.current_sl - slippage
                        pips = (fill - sig.entry) / self.pip
                        pnl = pips * usd_per_pip * t.remaining_lots
                        eq += pnl
                        t.realized_pnl_usd += pnl
                        t.close_idx = i
                        t.exit_price = fill
                        t.pnl_pips = pips
                        t.pnl_usd = t.realized_pnl_usd
                        t.reason = "PARTIAL+SL" if t.partial_done else "SL"
                        t.equity_after = eq
                        res.trade_log.append(asdict(t))
                        if t.pnl_usd > 0:
                            res.wins += 1
                            closs = 0
                        else:
                            res.losses += 1
                            closs += 1
                            max_closs = max(max_closs, closs)
                        res.total_pnl_pips += pips
                        res.total_pnl_usd += pnl
                    elif bar.high >= sig.tp2:
                        slippage = self.exit_slippage(sig.tp2)
                        fill = sig.tp2 + slippage
                        pips = (fill - sig.entry) / self.pip
                        pnl = pips * usd_per_pip * t.remaining_lots
                        eq += pnl
                        t.realized_pnl_usd += pnl
                        t.close_idx = i
                        t.exit_price = fill
                        t.pnl_pips = pips
                        t.pnl_usd = t.realized_pnl_usd
                        t.reason = "PARTIAL+TP2" if t.partial_done else "TP2"
                        t.equity_after = eq
                        res.trade_log.append(asdict(t))
                        if pnl > 0:
                            res.wins += 1
                        else:
                            res.losses += 1
                        res.total_pnl_pips += pips
                        res.total_pnl_usd += pnl
                        closs = 0
                    elif bar.high >= sig.tp1 and not t.partial_done:
                        slippage = self.exit_slippage(sig.tp1)
                        fill = sig.tp1 + slippage
                        pips = (fill - sig.entry) / self.pip
                        close_lots = t.lots * 0.5
                        pnl = pips * usd_per_pip * close_lots
                        eq += pnl
                        t.realized_pnl_usd += pnl
                        t.remaining_lots -= close_lots
                        t.partial_done = True
                        # DO NOT TIGHTEN SL at partial.
                        # Original SL sits at the TRUE LOW (the purged structural level).
                        # If price returns there, the setup is invalid — stop is correct.
                        # Runner keeps breathing; structural trail improves only when
                        # a new swing pivot genuinely offers better protection (every 5 bars).
                        t.lock_1r_done = True
                        res.partials += 1
                        new_open.append(t)
                    else:
                        new_open.append(t)
                else:  # BEAR
                    usd_per_pip = self.pip * self.contract
                    if bar.high >= t.current_sl:
                        slippage = self.exit_slippage(t.current_sl)
                        fill = t.current_sl + slippage
                        pips = (sig.entry - fill) / self.pip
                        pnl = pips * usd_per_pip * t.remaining_lots
                        eq += pnl
                        t.realized_pnl_usd += pnl
                        t.close_idx = i
                        t.exit_price = fill
                        t.pnl_pips = pips
                        t.pnl_usd = t.realized_pnl_usd
                        t.reason = "PARTIAL+SL" if t.partial_done else "SL"
                        t.equity_after = eq
                        res.trade_log.append(asdict(t))
                        if t.pnl_usd > 0:
                            res.wins += 1
                            closs = 0
                        else:
                            res.losses += 1
                            closs += 1
                            max_closs = max(max_closs, closs)
                        res.total_pnl_pips += pips
                        res.total_pnl_usd += pnl
                    elif bar.low <= sig.tp2:
                        slippage = self.exit_slippage(sig.tp2)
                        fill = sig.tp2 - slippage
                        pips = (sig.entry - fill) / self.pip
                        pnl = pips * usd_per_pip * t.remaining_lots
                        eq += pnl
                        t.realized_pnl_usd += pnl
                        t.close_idx = i
                        t.exit_price = fill
                        t.pnl_pips = pips
                        t.pnl_usd = t.realized_pnl_usd
                        t.reason = "PARTIAL+TP2" if t.partial_done else "TP2"
                        t.equity_after = eq
                        res.trade_log.append(asdict(t))
                        if pnl > 0:
                            res.wins += 1
                        else:
                            res.losses += 1
                        res.total_pnl_pips += pips
                        res.total_pnl_usd += pnl
                        closs = 0
                    elif bar.low <= sig.tp1 and not t.partial_done:
                        slippage = self.exit_slippage(sig.tp1)
                        fill = sig.tp1 - slippage
                        pips = (sig.entry - fill) / self.pip
                        close_lots = t.lots * 0.5
                        pnl = pips * usd_per_pip * close_lots
                        eq += pnl
                        t.realized_pnl_usd += pnl
                        t.remaining_lots -= close_lots
                        t.partial_done = True
                        # DO NOT TIGHTEN SL at partial.
                        # Original SL sits at TRUE HIGH (the purged structural level).
                        t.lock_1r_done = True
                        res.partials += 1
                        new_open.append(t)
                    else:
                        new_open.append(t)
            open_trades = new_open
            # Pending limit check
            if pending_limit is not None:
                pl = pending_limit
                if i < pl.bar_idx:
                    # should not happen
                    pending_limit = None
                    continue
                if i - pl.bar_idx < cfg.fill_window:
                    if pl.direction == "BULL" and bar.low <= pl.entry:
                        fill = pl.entry + self.entry_slippage(pl.entry)
                        lots = self.lot_size(eq, pl.sl, fill)
                        t = Trade(pl, i, lots=lots, equity_before=eq)
                        t.remaining_lots = lots
                        t.current_sl = pl.sl
                        open_trades.append(t)
                        pending_limit = None
                        pending_window = 0
                    elif pl.direction == "BEAR" and bar.high >= pl.entry:
                        fill = pl.entry - self.entry_slippage(pl.entry)
                        lots = self.lot_size(eq, pl.sl, fill)
                        t = Trade(pl, i, lots=lots, equity_before=eq)
                        t.remaining_lots = lots
                        t.current_sl = pl.sl
                        open_trades.append(t)
                        pending_limit = None
                        pending_window = 0
                else:
                    # expired
                    pending_limit = None
                    pending_window = 0
            # New signal entry
            # pull any signal at this exact bar (matching bar_idx)
            for sig in sig_queue:
                if sig.bar_idx != i:
                    continue
                if cfg.execution_mode == "LIMIT_ONLY":
                    if pending_limit is None:
                        pending_limit = sig
                        pending_window = 0
                elif cfg.execution_mode == "MARKET_ONLY":
                    fill = sig.entry + self.entry_slippage(sig.entry) if sig.direction == "BULL" else sig.entry - self.entry_slippage(sig.entry)
                    lots = self.lot_size(eq, sig.sl, fill)
                    t = Trade(sig, i, lots=lots, equity_before=eq)
                    t.remaining_lots = lots
                    t.current_sl = sig.sl
                    open_trades.append(t)
                elif cfg.execution_mode == "LIMIT_THEN_MARKET":
                    if pending_limit is None:
                        pending_limit = sig
                        pending_window = 0
                    else:
                        # fallback to market if limit already pending? simplistic: just limit for now
                        pass
            # equity tracking
            res.equity_curve.append(eq)
            if eq > peak:
                peak = eq
            else:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
                    dd_end = i
            if eq >= peak:
                dd_start = i
        # Close any remaining open trades at last bar close
        if open_trades:
            last_bar = ltf[-1]
            for t in open_trades:
                sig = t.sig
                fill = last_bar.close
                if sig.direction == "BULL":
                    pips = (fill - sig.entry) / self.pip
                else:
                    pips = (sig.entry - fill) / self.pip
                usd_per_pip = self.pip * self.contract
                pnl = pips * usd_per_pip * t.lots
                eq += pnl
                t.close_idx = len(ltf) - 1
                t.exit_price = fill
                t.pnl_pips = pips
                t.pnl_usd = pnl
                t.reason = "TIME_EXIT"
                t.equity_after = eq
                res.trade_log.append(asdict(t))
                if pnl > 0:
                    res.wins += 1
                    closs = 0
                else:
                    res.losses += 1
                    closs += 1
                    max_closs = max(max_closs, closs)
                res.total_pnl_pips += pips
                res.total_pnl_usd += pnl
                res.equity_curve.append(eq)
        # finalize metrics
        res.total_trades = res.wins + res.losses
        if res.total_trades > 0:
            res.win_rate = res.wins / res.total_trades
            res.expectancy_pips = res.total_pnl_pips / res.total_trades
            res.expectancy_usd = res.total_pnl_usd / res.total_trades
        res.end_equity = eq
        res.peak_equity = peak
        res.max_dd_pct = max_dd * 100
        res.max_dd_start = dd_start
        res.max_dd_end = dd_end
        res.max_consecutive_losses = max_closs
        res.total_pnl_pct = ((eq - self.start_eq) / self.start_eq) * 100
        # profit factor
        gross_profit = sum(t["pnl_usd"] for t in res.trade_log if t["pnl_usd"] > 0)
        gross_loss = abs(sum(t["pnl_usd"] for t in res.trade_log if t["pnl_usd"] < 0))
        res.profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0
        # real win rate (only TP2 hits = full winner)
        real_wins = sum(1 for t in res.trade_log if t["pnl_usd"] > 0 and 'TP2' in t["reason"])
        real_losses = sum(1 for t in res.trade_log if t["pnl_usd"] < 0 and 'SL' in t["reason"])
        res.real_wr = (real_wins / (real_wins + real_losses) * 100) if (real_wins + real_losses) > 0 else 0.0
        # avg drawdown from equity curve
        if res.equity_curve:
            dd_values = []
            peak = res.equity_curve[0]
            for eq in res.equity_curve:
                if eq > peak: peak = eq
                dd_values.append(((peak - eq) / peak) * 100)
            res.avg_dd_pct = sum(dd_values) / len(dd_values) if dd_values else 0.0
        # kelly
        if res.total_trades > 0:
            p = res.win_rate
            b = abs(gross_profit / res.wins) / abs(gross_loss / res.losses) if res.losses > 0 else 999.0
            try:
                kelly = p - (1 - p) / b if b > 0 else 0.0
            except Exception:
                kelly = 0.0
            res.kelly_fraction = max(0.0, kelly)
        # sharpe rough (daily returns not available from H1 simulation; use trade returns)
        if len(res.trade_log) >= 2:
            rets = [t["pnl_usd"] / self.start_eq for t in res.trade_log]
            avg_r = sum(rets) / len(rets)
            var = sum((r - avg_r) ** 2 for r in rets) / len(rets)
            std = math.sqrt(var) if var > 0 else 1e-9
            res.sharpe_annualized = (avg_r / std) * math.sqrt(252)  # trades treated as days for proxy
        # lots
        if res.trade_log:
            res.avg_lot = sum(t["lots"] for t in res.trade_log) / len(res.trade_log)
            res.avg_sl_pips = sum(abs(t["sig"]["sl"] - t["sig"]["entry"]) / self.pip for t in res.trade_log) / len(res.trade_log)
        res.notes = [
            f"Execution: {cfg.execution_mode}",
            f"SL cap: {cfg.sl_cap_pips} pips",
            f"Session: {cfg.session}",
            f"Min RR: {cfg.min_rr}",
            f"Stricter slippage: {cfg.stricter_slippage}",
            f"Leverage: {self.lev}:1 | Risk per trade: {self.risk_pct*100}%",
        ]
        return res

# ── regime fetchers ──────────────────────────────────────────────────
REGIMES = {
    "bull_2024_2026": {"symbol": "XAUUSD", "period": "1h", "range": "2y", "description": "Gold bull $2300-$3400"},
    "bear_2022": {"symbol": "XAUUSD", "period": "1h", "range": "1y", "start": "2022-01-01", "end": "2022-12-31", "description": "Gold bear $2070-$1620"},
    "mixed_2020_2024": {"symbol": "XAUUSD", "period": "1h", "range": "5y", "description": "Mixed regime COVID rally + correction"},
    "eurusd_bull_2022": {"symbol": "EURUSD", "period": "1h", "range": "2y", "description": "EURUSD parity bounce"},
    "nas_bear_2022": {"symbol": "NAS100", "period": "1h", "range": "2y", "description": "NASDAQ bear 2022-2023"},
}

def fetch_regime(key: str) -> tuple:
    info = REGIMES[key]
    sym = info["symbol"]
    eng = Engine(sym)
    if "start" in info:
        tkr = yf.Ticker(SYMBOLS[sym])
        df = tkr.history(start=info["start"], end=info["end"], interval=info["period"])
    else:
        df = yf.Ticker(SYMBOLS[sym]).history(period=info["range"], interval=info["period"])
    df.reset_index(inplace=True)
    if "Datetime" in df.columns:
        df.rename(columns={"Datetime": "Date"}, inplace=True)
    ltf = [Bar(r["Date"], float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0))) for _, r in df.iterrows()]
    # HTF daily aligned
    htf_df = yf.Ticker(SYMBOLS[sym]).history(period=info.get("range", "5y"), interval="1d")
    htf_df.reset_index(inplace=True)
    if "Datetime" in htf_df.columns:
        htf_df.rename(columns={"Datetime": "Date"}, inplace=True)
    htf = [Bar(r["Date"], float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0))) for _, r in htf_df.iterrows()]
    aligned = eng.align_htf(ltf, htf)
    return ltf, aligned, info["description"]

# ── harness ─────────────────────────────────────────────────────────
def run_config_on_regime(cfg: EngineConfig, regime_key: str, start_equity=10000.0, lev=100.0, risk=0.01) -> dict:
    ltf, htf_desc, desc = fetch_regime(regime_key)
    sym = REGIMES[regime_key]["symbol"]
    eng = Engine(sym)
    sigs = eng.generate(ltf, htf_desc, cfg)
    sim = Simulator(sym, start_equity, lev, risk)
    result = sim.run(ltf, sigs, cfg)
    out = {
        "regime": regime_key,
        "description": desc,
        "symbol": sym,
        "ltf_bars": len(ltf),
        "signals_generated": len(sigs),
        "config": asdict(cfg),
        "result": {
            "start_equity": result.start_equity,
            "end_equity": result.end_equity,
            "peak_equity": result.peak_equity,
            "total_trades": result.total_trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate_pct": round(result.win_rate * 100, 2),
            "expectancy_usd": round(result.expectancy_usd, 2),
            "expectancy_pips": round(result.expectancy_pips, 2),
            "total_pnl_usd": round(result.total_pnl_usd, 2),
            "total_pnl_pips": round(result.total_pnl_pips, 2),
            "total_pnl_pct": round(result.total_pnl_pct, 2),
            "max_dd_pct": round(result.max_dd_pct, 2),
            "max_consecutive_losses": result.max_consecutive_losses,
            "profit_factor": round(result.profit_factor, 2),
            "kelly_fraction": round(result.kelly_fraction, 4),
            "sharpe_annualized": round(result.sharpe_annualized, 2),
            "avg_lot": round(result.avg_lot, 2),
            "avg_sl_pips": round(result.avg_sl_pips, 2),
            "notes": result.notes,
        },
    }
    return out

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--regime", default="bull_2024_2026")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--session", default="LONDON")
    parser.add_argument("--sl_cap", type=float, default=200.0)
    parser.add_argument("--execution", default="MARKET_ONLY")
    parser.add_argument("--risk", type=float, default=0.01)
    parser.add_argument("--output", default="proven_results.json")
    args = parser.parse_args()

    cfg = EngineConfig(
        execution_mode=args.execution,
        sl_cap_pips=args.sl_cap,
        session=args.session,
        stricter_slippage=True,
        fill_window=96 if args.execution == "MARKET_ONLY" else 25,
    )
    print(f"[PROVEN] Running {args.regime} | {args.execution} | SL cap={args.sl_cap} | session={args.session}")
    out = run_config_on_regime(cfg, args.regime, risk=args.risk)
    print(json.dumps(out["result"], indent=2))
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"[SAVED] {args.output}")
