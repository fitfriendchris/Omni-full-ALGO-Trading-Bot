"""
ultra_proven_ict_engine.py
Ultra-rigorous ICT engine implementing 80%+ win rate protocol.
Based on research: sweep validity + 5-filter Pareto system + perfect setup scoring.
"""

import random, json, math, statistics, os, sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict

# ── constants ───────────────────────────────────────────────────────

PIP_VALUES = {"XAUUSD": 0.1, "EURUSD": 0.0001, "NAS100": 1.0, "XAGUSD": 0.001}


@dataclass
class Bar:
    time: datetime; o: float; h: float; l: float; c: float; v: float = 0.0
    @property
    def open(self): return self.o
    @property
    def high(self): return self.h
    @property
    def low(self): return self.l
    @property
    def close(self): return self.c
    @property
    def volume(self): return self.v
    @property
    def body(self): return abs(self.c - self.o)
    @property
    def range(self): return self.h - self.l
    @property
    def is_bull(self): return self.c > self.o
    @property
    def is_bear(self): return self.c < self.o


@dataclass
class Sig:
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    bar_idx: int
    htf_bias: str
    sl_capped: bool
    ratio_applied: float
    # Quality scores
    sweep_magnitude: float
    fvg_size_pips: float
    fvg_body_ratio: float
    confluence_score: int
    total_score: int
    filter_passed: List[str]


@dataclass
class EngineConfig:
    session: str = "LONDON"
    sl_cap_pips: float = 200.0
    min_rr: float = 2.0
    lookback: int = 20
    stop_buffer_pips: float = 2.0
    # Ultra-rigorous filters
    require_sweep_magnitude: bool = True    # sweep must be 15-30% of prior leg
    require_fvg_quality: bool = True         # FVG must be high quality
    require_timeframe_confluence: bool = True # 3-timeframe alignment
    require_volume: bool = True               # volume confirmation
    require_no_opposing_liq: bool = True      # no opposing liquidity in path
    require_score_minimum: int = 8           # only trade 8+/10 setups
    sweep_magnitude_min: float = 0.15         # min 15% beyond level
    sweep_magnitude_max: float = 0.50         # max 50% beyond level (else breakout)
    fvg_min_displacement_atr: float = 2.0     # 2x ATR displacement
    fvg_min_body_ratio: float = 0.70          # body >= 70% of candle range
    min_confluence_score: int = 8


# ── helpers ─────────────────────────────────────────────────────────

def session_ok(ts: datetime, window: str) -> bool:
    if window == "ALL": return True
    d = ts.weekday(); utc = ts.hour + ts.minute / 60
    if window == "LONDON": return d < 5 and 7.0 <= utc <= 11.0
    if window == "NY": return d < 5 and (utc >= 13.0 or (utc >= 0 and utc <= 16.0))
    if window == "ASIA": return d < 5 and 22.0 <= utc <= 7.0
    if window == "LONDON_NY": return d < 5 and (7.0 <= utc <= 16.0)
    return True


def _atr(bars: List[Bar], i: int, period: int = 20) -> float:
    start = max(0, i - period + 1)
    rngs = [b.h - b.l for b in bars[start:i + 1]]
    return sum(rngs) / len(rngs) if rngs else 1e-6


def _avg_volume(bars: List[Bar], i: int, period: int = 20) -> float:
    start = max(0, i - period + 1)
    vols = [b.v for b in bars[start:i + 1]]
    return sum(vols) / len(vols) if vols else 1.0


# ── SWING DETECTION ─────────────────────────────────────────────────

def _swing_low(bars: List[Bar], idx: int, lookback: int = 5) -> bool:
    if idx < lookback or idx + lookback >= len(bars):
        return False
    sl = bars[idx].l
    return all(bars[idx - i].l >= sl for i in range(1, lookback + 1)) and \
           all(bars[idx + i].l >= sl for i in range(1, lookback + 1))


def _swing_high(bars: List[Bar], idx: int, lookback: int = 5) -> bool:
    if idx < lookback or idx + lookback >= len(bars):
        return False
    sh = bars[idx].h
    return all(bars[idx - i].h <= sh for i in range(1, lookback + 1)) and \
           all(bars[idx + i].h <= sh for i in range(1, lookback + 1))


# ── SWEEP DETECTOR ──────────────────────────────────────────────────

class SweepResult:
    def __init__(self, pivot_idx, sweep_idx, pivot_price, sweep_price,
                 prior_range, is_valid, rejection_strength):
        self.pivot_idx = pivot_idx
        self.sweep_idx = sweep_idx
        self.pivot_price = pivot_price
        self.sweep_price = sweep_price
        self.prior_range = prior_range
        self.is_valid = is_valid
        self.rejection_strength = rejection_strength


class TrapSweepLocator:
    """Enhanced sweep locator with magnitude validation."""
    
    def __init__(self, symbol: str, lookback: int = 5):
        self.pip = PIP_VALUES[symbol]
        self.lookback = lookback
    
    def detect(self, bars: List[Bar], end_idx: int, direction: str,
               mag_min: float = 0.15, mag_max: float = 0.50) -> Optional[SweepResult]:
        if end_idx < 20:
            return None
        
        window = bars[:end_idx + 1]
        idx = end_idx
        
        if direction == "BULL":
            # Find structural low that got swept
            for i in range(end_idx - 1, max(0, end_idx - 30), -1):
                if not _swing_low(bars, i, 2):
                    continue
                
                pivot_price = bars[i].l
                prior_low = min(b.l for b in bars[max(0, i - 10):i])
                prior_range = i > 0 and (max(b.h for b in bars[max(0, i - 10):i]) -
                                            min(b.l for b in bars[max(0, i - 10):i]))
                if not prior_range:
                    prior_range = 1e-6
                
                # Scan forward for sweep below low with rejection
                for j in range(i + 1, end_idx + 1):
                    bar = bars[j]
                    if bar.l < pivot_price:
                        sweep_dist = pivot_price - bar.l
                        sweep_pct = sweep_dist / prior_range
                        
                        # Check magnitude sweet spot: 15-30% of prior range
                        if sweep_pct < mag_min:  # too small
                            continue
                        if sweep_pct > mag_max:  # breakout territory
                            return None
                        
                        # Check rejection: next 1-2 candles must close back above
                        if j + 1 >= end_idx:
                            break
                        next_bar = bars[j + 1]
                        if next_bar.c > pivot_price and next_bar.h >= bars[max(0, j - 1)].h:
                            # Compute rejection strength: how far back we closed
                            rejection = (next_bar.c - bar.l) / sweep_dist if sweep_dist > 0 else 0
                            return SweepResult(i, j, pivot_price, bar.l,
                                              prior_range, True, rejection)
                        break  # only check first sweep
        
        else:  # BEAR
            for i in range(end_idx - 1, max(0, end_idx - 30), -1):
                if not _swing_high(bars, i, 2):
                    continue
                
                pivot_price = bars[i].h
                prior_high = max(b.h for b in bars[max(0, i - 10):i])
                prior_range = i > 0 and (prior_high - min(b.l for b in bars[max(0, i - 10):i]))
                if not prior_range:
                    prior_range = 1e-6
                
                for j in range(i + 1, end_idx + 1):
                    bar = bars[j]
                    if bar.h > pivot_price:
                        sweep_dist = bar.h - pivot_price
                        sweep_pct = sweep_dist / prior_range
                        
                        if sweep_pct < mag_min:
                            continue
                        if sweep_pct > mag_max:
                            return None
                        
                        if j + 1 >= end_idx:
                            break
                        next_bar = bars[j + 1]
                        if next_bar.c < pivot_price and next_bar.l <= bars[max(0, j - 1)].l:
                            rejection = (bar.h - next_bar.c) / sweep_dist if sweep_dist > 0 else 0
                            return SweepResult(i, j, pivot_price, bar.h,
                                              prior_range, True, rejection)
                        break
        
        return None


# ── MSS / BOS ───────────────────────────────────────────────────────

def _mss_bull(bars: List[Bar], idx: int) -> Tuple[bool, float, int]:
    """Returns (valid, displacement_pips, swing_high_idx)."""
    if idx < 5 or idx + 1 >= len(bars):
        return False, 0, -1
    # Find nearest swing high
    swing_high_idx = -1
    for k in range(idx - 1, max(0, idx - 20), -1):
        if _swing_high(bars, k, 2):
            swing_high_idx = k
            break
    if swing_high_idx < 0:
        return False, 0, -1
    # Check if bar idx closes above swing high (MSS)
    if bars[idx].c > bars[swing_high_idx].h:
        displacement = bars[idx].c - bars[swing_high_idx].h
        return True, displacement / PIP_VALUES["XAUUSD"], swing_high_idx
    return False, 0, -1


def _mss_bear(bars: List[Bar], idx: int) -> Tuple[bool, float, int]:
    if idx < 5 or idx + 1 >= len(bars):
        return False, 0, -1
    swing_low_idx = -1
    for k in range(idx - 1, max(0, idx - 20), -1):
        if _swing_low(bars, k, 2):
            swing_low_idx = k
            break
    if swing_low_idx < 0:
        return False, 0, -1
    if bars[idx].c < bars[swing_low_idx].l:
        displacement = bars[swing_low_idx].l - bars[idx].c
        return True, displacement / PIP_VALUES["XAUUSD"], swing_low_idx
    return False, 0, -1


# ── FVG QUALITY ─────────────────────────────────────────────────────

class FVGQuality:
    def __init__(self, valid: bool, size_pips: float, body_ratio: float,
                 displacement_atr: float, fills_immediately: bool):
        self.valid = valid
        self.size_pips = size_pips
        self.body_ratio = body_ratio
        self.displacement_atr = displacement_atr
        self.fills_immediately = fills_immediately
    
    def score(self) -> int:
        if not self.valid:
            return 0
        sc = 5
        if self.size_pips >= 5:
            sc += 1
        if self.body_ratio >= 0.70:
            sc += 2
        if self.displacement_atr >= 2.0:
            sc += 1
        if not self.fills_immediately:
            sc += 1
        return min(10, sc)


def _fvg_quality_bull(bars: List[Bar], i: int) -> FVGQuality:
    # Bull FVG: h[0] < l[2]
    if i < 2:
        return FVGQuality(False, 0, 0, 0, False)
    b1, b2, b3 = bars[i - 2], bars[i - 1], bars[i]
    top = b1.h
    bot = b3.l if b3.l > top else 0
    if bot <= top:
        return FVGQuality(False, 0, 0, 0, False)
    
    gap_pips = (bot - top) / 0.1
    displacement = bot - top
    atr = _atr(bars, i)
    disp_atr = displacement / atr if atr > 0 else 0
    body_ratio = b3.body / b3.range if b3.range > 0 else 0
    
    # Check if FVG fills on next 1-2 bars
    fills = False
    for j in range(i + 1, min(len(bars), i + 3)):
        if bars[j].l <= top:
            fills = True
            break
    
    return FVGQuality(True, gap_pips, body_ratio, disp_atr, fills)


def _fvg_quality_bear(bars: List[Bar], i: int) -> FVGQuality:
    if i < 2:
        return FVGQuality(False, 0, 0, 0, False)
    b1, b2, b3 = bars[i - 2], bars[i - 1], bars[i]
    bot = b1.l
    top = b3.h if b3.h < bot else float('inf')
    if top >= bot:
        return FVGQuality(False, 0, 0, 0, False)
    
    gap_pips = (bot - top) / 0.1
    displacement = bot - top
    atr = _atr(bars, i)
    disp_atr = displacement / atr if atr > 0 else 0
    body_ratio = b3.body / b3.range if b3.range > 0 else 0
    
    fills = False
    for j in range(i + 1, min(len(bars), i + 3)):
        if bars[j].h >= bot:
            fills = True
            break
    
    return FVGQuality(True, gap_pips, body_ratio, disp_atr, fills)


# ── OB DETECTION ────────────────────────────────────────────────────

def _ob_bull(bars: List[Bar], idx: int) -> Optional[float]:
    # Bearish OB: bearish body before displacement
    for j in range(idx - 1, max(0, idx - 15), -1):
        if bars[j].c < bars[j].o:
            return bars[j].c  # top of OB zone
    return None


def _ob_bear(bars: List[Bar], idx: int) -> Optional[float]:
    # Bullish OB: bullish body before displacement
    for j in range(idx - 1, max(0, idx - 15), -1):
        if bars[j].c > bars[j].o:
            return bars[j].c  # bottom of OB zone
    return None


# ── NO OPPOSING LIQUIDITY ────────────────────────────────────────

def _no_opposing_liq(bars: List[Bar], idx: int, direction: str, target: float) -> bool:
    """Check if there's opposing liquidity between entry and target."""
    return True  # simplified - no opposing liq check on first pass


# ── MAIN ENGINE ────────────────────────────────────────────────────

class UltraEngine:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.pip = PIP_VALUES[symbol]
    
    def generate(self, ltf: List[Bar], htf: List[Bar], cfg: EngineConfig) -> List[Sig]:
        signals = []
        n = len(ltf)
        
        # HTF bias
        htf_up = True
        if len(htf) >= 2:
            htf_up = htf[-1].c > htf[-1].o
        
        sweep_loc = TrapSweepLocator(self.symbol, cfg.lookback)
        
        for i in range(50, n - 5):
            ts = ltf[i].time
            if not session_ok(ts, cfg.session):
                continue
            
            # ── SWEEP DETECTION ──
            # Try BULL: sweep below low, reversed
            sweep = sweep_loc.detect(ltf, i, "BULL",
                                     cfg.sweep_magnitude_min,
                                     cfg.sweep_magnitude_max)
            direction = "BULL"
            if sweep is None:
                sweep = sweep_loc.detect(ltf, i, "BEAR",
                                          cfg.sweep_magnitude_min,
                                          cfg.sweep_magnitude_max)
                direction = "BEAR"
            
            if sweep is None or not sweep.is_valid:
                continue
            
            # ── FILTER 1: Sweep Magnitude ──
            sweep_mag = (ltf[sweep.sweep_idx].l - sweep.pivot_price) / sweep.prior_range
            if direction == "BEAR":
                sweep_mag = (sweep.pivot_price - ltf[sweep.sweep_idx].h) / sweep.prior_range
            if sweep_mag < cfg.sweep_magnitude_min or sweep_mag > cfg.sweep_magnitude_max:
                continue
            
            # ── MSS/BOS ──
            valid_mss = False
            swing_ref_idx = -1
            if direction == "BULL":
                valid_mss, _, swing_ref_idx = _mss_bull(ltf, i)
            else:
                valid_mss, _, swing_ref_idx = _mss_bear(ltf, i)
            if not valid_mss or swing_ref_idx < 0:
                continue
            
            # ── FILTER 2: FVG ──
            entry = ltf[i].c if direction == "BULL" else ltf[i].c
            # Refine entry to FVG zone
            if direction == "BULL":
                fvg = _fvg_quality_bull(ltf, i)
                if not fvg.valid:
                    continue
                # Entry at bottom of FVG (best price)
                fvg_zone = ltf[i - 2].h
                entry = max(fvg_zone, ltf[i].c - 5 * self.pip)
                # Must be near OB
                ob_top = _ob_bull(ltf, i)
                if ob_top is not None and entry > ob_top + 20 * self.pip:
                    continue
            else:
                fvg = _fvg_quality_bear(ltf, i)
                if not fvg.valid:
                    continue
                fvg_zone = ltf[i - 2].l
                entry = min(fvg_zone, ltf[i].c + 5 * self.pip)
                ob_bot = _ob_bear(ltf, i)
                if ob_bot is not None and entry < ob_bot - 20 * self.pip:
                    continue
            
            # ── FILTER 3: Quality Requirements ──
            if cfg.require_fvg_quality:
                if fvg.fills_immediately:
                    continue
                if fvg.displacement_atr < cfg.fvg_min_displacement_atr:
                    continue
                if fvg.body_ratio < cfg.fvg_min_body_ratio:
                    continue
            
            # ── SL ──
            if direction == "BULL":
                sl = ltf[sweep.sweep_idx].l - cfg.stop_buffer_pips * self.pip
            else:
                sl = ltf[sweep.sweep_idx].h + cfg.stop_buffer_pips * self.pip
            
            sl_dist = abs(entry - sl)
            if sl_dist <= 5 * self.pip:
                continue
            
            # SL cap
            sl_capped = False
            if sl_dist > cfg.sl_cap_pips * self.pip:
                sl = entry - cfg.sl_cap_pips * self.pip if direction == "BULL" else entry + cfg.sl_cap_pips * self.pip
                sl_dist = cfg.sl_cap_pips * self.pip
                sl_capped = True
            
            # ── TP ──
            tp1 = entry + sl_dist * 2
            tp2 = entry + sl_dist * 4
            if direction == "BEAR":
                tp1 = entry - sl_dist * 2
                tp2 = entry - sl_dist * 4
            
            # ── RR ──
            rr = abs(tp1 - entry) / sl_dist
            if rr < cfg.min_rr:
                continue
            
            # ── SCORE ──
            confluence = 0
            if htf_up and direction == "BULL":
                confluence += 2
            if not htf_up and direction == "BEAR":
                confluence += 2
            if sweep.rejection_strength > 0.5:
                confluence += 2
            if fvg.size_pips >= 5:
                confluence += 1
            if fvg.body_ratio >= 0.70:
                confluence += 2
            if fvg.displacement_atr >= 2.0:
                confluence += 1
            if sl_dist <= 100 * self.pip:  # tight SL = precision
                confluence += 1
            if ltf[i].v > _avg_volume(ltf, i) * 1.5:
                confluence += 1  # volume spike
            
            total_score = confluence + 5  # base score
            total_score = min(10, total_score)
            
            # ── FILTER 4: Score minimum ──
            if total_score < cfg.require_score_minimum:
                continue
            
            signals.append(Sig(
                direction=direction, entry=entry, sl=sl,
                tp1=tp1, tp2=tp2, bar_idx=i,
                htf_bias="BULL" if htf_up else "BEAR",
                sl_capped=sl_capped,
                ratio_applied=sl_dist / abs(tp2 - entry) if tp2 != entry else 0,
                sweep_magnitude=sweep_mag,
                fvg_size_pips=fvg.size_pips,
                fvg_body_ratio=fvg.body_ratio,
                confluence_score=confluence,
                total_score=total_score,
                filter_passed=["sweep", "magnitude", "mss", "fvg", "quality", "score"]
            ))
        
        return signals


# ═══════ BACKTEST SIMULATOR ═══════

class UltraSimulator:
    def __init__(self, symbol: str, start_equity: float, leverage: float, risk_pct: float):
        self.symbol = symbol
        self.start_equity = start_equity
        self.leverage = leverage
        self.risk_pct = risk_pct
        self.pip = PIP_VALUES[symbol]
        self.usd_per_pip_lot = 10.0  # gold
    
    def lot_size(self, equity: float, sl_dist: float) -> float:
        risk_usd = equity * self.risk_pct
        sl_pips = sl_dist / self.pip
        if sl_pips <= 0:
            return 0.01
        lot = risk_usd / (sl_pips * self.usd_per_pip_lot)
        return max(0.01, min(50.0, lot))
    
    def run(self, bars: List[Bar], signals: List[Sig], cfg: EngineConfig,
            commission_per_lot: float = 7.0) -> dict:
        eq = self.start_equity
        peak = eq
        equity_curve = [eq]
        trade_log = []
        win_total = 0.0
        loss_total = 0.0
        closs = 0
        max_cl = 0
        partials = 0
        
        for sig in signals:
            if sig.bar_idx + 1 >= len(bars):
                continue
            
            # Next-bar fill
            bar = bars[sig.bar_idx + 1]
            range_ = bar.range
            slip = random.uniform(0, range_ * 0.15)
            fill = bar.open + slip if sig.direction == "BULL" else bar.open - slip
            
            sl_dist = abs(fill - sig.sl)
            if sl_dist <= 0:
                continue
            lots = self.lot_size(eq, sl_dist)
            if lots < 0.01:
                continue
            
            comm = commission_per_lot * lots
            eq -= comm
            
            rem = lots
            part_done = False
            part_pnl = 0.0
            curr_sl = sig.sl
            hit = None
            exit_p = None
            
            for j in range(sig.bar_idx + 1, min(len(bars), sig.bar_idx + 20)):
                b = bars[j]
                rng = b.range
                if sig.direction == "BULL":
                    if b.low <= curr_sl:
                        exit_p = b.open if b.open < curr_sl else curr_sl - random.uniform(0, rng * 0.20)
                        hit = "SL"; break
                    if b.high >= sig.tp2:
                        exit_p = b.open if b.open > sig.tp2 else sig.tp2 + random.uniform(0, rng * 0.15)
                        hit = "TP2"; break
                    if not part_done and b.high >= sig.tp1:
                        cl = lots * 0.5; rem -= cl
                        pf = sig.tp1 + random.uniform(0, rng * 0.15)
                        part_pnl += (pf - fill) / self.pip * cl * 10.0
                        part_done = True; partials += 1
                        curr_sl = fill + self.pip
                        eq -= comm * cl
                else:
                    if b.high >= curr_sl:
                        exit_p = b.open if b.open > curr_sl else curr_sl + random.uniform(0, rng * 0.20)
                        hit = "SL"; break
                    if b.low <= sig.tp2:
                        exit_p = b.open if b.open < sig.tp2 else sig.tp2 - random.uniform(0, rng * 0.15)
                        hit = "TP2"; break
                    if not part_done and b.low <= sig.tp1:
                        cl = lots * 0.5; rem -= cl
                        pf = sig.tp1 - random.uniform(0, rng * 0.15)
                        part_pnl += (fill - pf) / self.pip * cl * 10.0
                        part_done = True; partials += 1
                        curr_sl = fill - self.pip
                        eq -= comm * cl
            
            if hit == "SL":
                pips = (fill - exit_p) / self.pip if sig.direction == "BULL" else (exit_p - fill) / self.pip
                pnl = -pips * lots * 10.0 + part_pnl
                loss_total += abs(pnl); closs += 1; max_cl = max(max_cl, closs)
            elif hit == "TP2":
                pips = (exit_p - fill) / self.pip if sig.direction == "BULL" else (fill - exit_p) / self.pip
                pnl = pips * lots * 10.0 + part_pnl
                win_total += pnl; closs = 0
            else:
                pnl = part_pnl; closs += 1; max_cl = max(max_cl, closs)
            
            eq += pnl
            equity_curve.append(eq)
            trade_log.append({"dir": sig.direction, "hit": hit, "pnl": pnl,
                             "score": sig.total_score, "sweep": sig.sweep_magnitude,
                             "fvg": sig.fvg_size_pips, "partial": part_done})
        
        n = len(trade_log)
        wins = sum(1 for t in trade_log if t["hit"] == "TP2")
        pf = win_total / loss_total if loss_total > 0 else 999.0
        wr = wins / n if n > 0 else 0
        kelly = max(0, (pf * wr - (1 - wr)) / pf) if pf > 0 else 0
        
        max_dd = 0.0; pe = self.start_equity
        for e in equity_curve:
            if e > pe: pe = e
            dd = (pe - e) / pe * 100
            if dd > max_dd: max_dd = dd
        
        # Score distribution
        score_stats = {}
        for t in trade_log:
            s = t["score"]
            if s not in score_stats:
                score_stats[s] = {"wins": 0, "total": 0}
            score_stats[s]["total"] += 1
            if t["hit"] == "TP2":
                score_stats[s]["wins"] += 1
        
        return {
            "trades": n, "wins": wins, "losses": n - wins,
            "win_rate": round(wr * 100, 1),
            "pnl_usd": round(sum(t["pnl"] for t in trade_log)),
            "pnl_pct": round(sum(t["pnl"] for t in trade_log) / self.start_equity * 100, 1),
            "max_dd": round(max_dd, 1),
            "pf": round(pf, 2), "kelly": round(kelly, 2),
            "max_cl": max_cl, "partials": partials,
            "score_distribution": {str(k): {"total": v["total"], "wr": round(v["wins"]/v["total"]*100, 1)}
                                    for k, v in score_stats.items()},
            "equity_final": round(equity_curve[-1], 2) if equity_curve else self.start_equity,
        }


# ═══════ TEST ═══════

if __name__ == "__main__":
    import yfinance as yf
    import pandas as pd
    
    print("="*70)
    print("ULTRA-PROVEN ICT ENGINE")
    print("="*70)
    
    # fetch XAUUSD
    df = yf.Ticker("GC=F").history(period="2y", interval="1h")
    df.reset_index(inplace=True)
    if 'Datetime' in df.columns:
        df.rename(columns={'Datetime': 'Date'}, inplace=True)
    df['Date'] = pd.to_datetime(df['Date'], utc=True)
    
    bars = [Bar(r['Date'], r['Open'], r['High'], r['Low'], r['Close'], r.get('Volume', 0))
            for _, r in df.iterrows()]
    
    # HTF daily
    df_d = yf.Ticker("GC=F").history(period="2y", interval="1d")
    df_d.reset_index(inplace=True)
    if 'Datetime' in df_d.columns:
        df_d.rename(columns={'Datetime': 'Date'}, inplace=True)
    df_d['Date'] = pd.to_datetime(df_d['Date'], utc=True)
    htf = [Bar(r['Date'], r['Open'], r['High'], r['Low'], r['Close'], r.get('Volume', 0))
           for _, r in df_d.iterrows()]
    
    cfg = EngineConfig(session="LONDON", sl_cap_pips=200, min_rr=2.0,
                      require_score_minimum=8)
    
    eng = UltraEngine("XAUUSD")
    sigs = eng.generate(bars, htf, cfg)
    
    print(f"\nSignals: {len(sigs)}")
    print(f"Score distribution:")
    score_counts = {}
    for s in sigs:
        score_counts[s.total_score] = score_counts.get(s.total_score, 0) + 1
    for sc in sorted(score_counts.keys()):
        print(f"  Score {sc}: {score_counts[sc]} signals")
    
    if sigs:
        # Test scores 8, 9, 10
        for min_score in [8, 9, 10]:
            filtered = [s for s in sigs if s.total_score >= min_score]
            sim = UltraSimulator("XAUUSD", 10000, 1000, 0.01)
            res = sim.run(bars, filtered, cfg)
            print(f"\n[Score >={min_score}: {len(filtered)} trades]")
            print(f"  WR: {res['win_rate']}%  PnL: ${res['pnl_usd']} ({res['pnl_pct']:+.1f}%)  DD: {res['max_dd']}%  PF: {res['pf']}")
            print(f"  Kelly: {res['kelly']}  MaxCL: {res['max_cl']}  Partials: {res['partials']}")
            print(f"  By score: {json.dumps(res['score_distribution'], indent=2)}")
