"""
micro_amd_detector.py — M15 micro AMD cycle detection + redistribution estimator.

ICT teaches that AMD phases rotate on ALL timeframes — not just daily.
A M15 chart shows its own accumulation (3-5 candles), manipulation (1-2 candles),
and redistribution (4-12 candles) within the macro phase.

This module detects micro cycles independently of session-wide AMD phase,
giving the confluence engine a granular, structural entry filter.

Key outputs:
  - MicroAMDResult : the detected phase, direction, quality, entry/SL/TP
  - RedistributionEstimate : candles_to_poi, session multiplier, feasibility

Detection order (CRITICAL — must scan backward to find manip BEFORE accum):
  1. Find manipulation spike in last 3-5 bars
  2. Look 3-5 bars BEFORE it for accumulation base
  3. Look AFTER it for displacement (redistribution)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from smc_engine import Bar

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Defaults (overridable via kwargs)
# ──────────────────────────────────────────────────────────────────────────────

_MICRO_MAX_SCAN_BARS = 20       # bars to scan from right edge
_MICRO_MANIP_LOOKBACK = 5      # look for manipulation in last N bars
_MICRO_ACCUM_BARS = 5          # max candles to scan back BEFORE manip for accumulation
_MIN_ACCUM_RANGE_ATR = 0.25    # accumulation range must exceed 0.25× current ATR
_MIN_MANIP_EXCESS_PCT = 0.20   # must sweep beyond accum by 20% of range

# Session multipliers for redistribution speed (expected move per candle relative to ATR)
_SESSION_MULTIPLIERS = {
    "LONDON_OVERLAP": 1.50,
    "LONDON_OPEN":    1.20,
    "NY_OPEN":        0.80,
    "SILVER_BULLET":  0.80,
    "EUROPEAN":       1.00,
    "POST_NY":        0.50,
    "ASIA":           0.40,
    "DEFAULT":        0.70,
}

# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

class MicroPhase:
    ACCUMULATION = "ACCUMULATION"
    MANIPULATION = "MANIPULATION"
    REDISTRIBUTION = "REDISTRIBUTION"
    UNKNOWN = "UNKNOWN"

@dataclass
class MicroAccum:
    """Micro accumulation range (3-5 tight candles)."""
    high: float
    low: float
    start_idx: int
    end_idx: int
    bar_count: int
    avg_body: float
    width: float = field(init=False)
    def __post_init__(self):
        self.width = self.high - self.low

@dataclass
class MicroManip:
    """Micro manipulation spike (1-2 candles beyond accumulation)."""
    side: str          # "HIGH" or "LOW"
    extreme: float     # the spike extreme price
    close_back: bool   # closed back inside accumulation
    bar_idx: int
    excess_pct: float  # excess beyond accumulation / accumulation width
    wick_ratio: float
    close_price: float = 0.0

@dataclass
class MicroDisplace:
    """Displacement candles AFTER manipulation."""
    direction: str     # "BULL" or "BEAR"
    start_price: float
    current_price: float
    bars_traced: int
    pullback_candles: int
    displacement_pct: float

@dataclass
class RedistributionEstimate:
    """How many candles to reach opposing liquidity."""
    manip_extreme: float
    opposing_liquidity: float
    distance: float
    m15_atr: float
    session_label: str
    session_multiplier: float
    est_candles: float
    est_minutes: float
    feasibility_score: float
    reasons: List[str] = field(default_factory=list)

@dataclass
class MicroAMDResult:
    detected: bool = False
    phase: str = MicroPhase.UNKNOWN
    direction: str = "NEUTRAL"
    confidence: float = 0.0
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    accum: Optional[MicroAccum] = None
    manip: Optional[MicroManip] = None
    displace: Optional[MicroDisplace] = None
    redistribution: Optional[RedistributionEstimate] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def is_entry_ready(self) -> bool:
        return self.detected and self.phase == MicroPhase.REDISTRIBUTION and self.entry is not None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _bar_hour_utc(bar: Bar) -> int:
    try:
        if isinstance(bar.time, (int, float)) and bar.time > 1_000_000_000:
            dt = datetime.fromtimestamp(bar.time, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(bar.time).replace("Z", "+00:00"))
        return dt.hour
    except Exception:
        return 0

def _calc_atr(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = [max(b.high - b.low, abs(b.high - bars[i-1].close), abs(b.low - bars[i-1].close))
           for i, b in enumerate(bars[1:], 1)]
    return sum(trs[-period:]) / period if len(trs) >= period else sum(trs) / max(1, len(trs))

def _session_label(hour_utc: int) -> str:
    if 7 <= hour_utc < 10:
        return "LONDON_OPEN"
    elif 10 <= hour_utc < 13:
        return "EUROPEAN"
    elif 13 <= hour_utc < 17:
        return "NY_OPEN"
    elif hour_utc >= 17:
        return "POST_NY"
    else:
        return "ASIA"

def _session_multiplier(label: str, overlap: bool = False) -> float:
    if overlap:
        return _SESSION_MULTIPLIERS.get("LONDON_OVERLAP", 1.50)
    return _SESSION_MULTIPLIERS.get(label, _SESSION_MULTIPLIERS["DEFAULT"])


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1: Detect manipulation (the ANCHOR — find the spike first)
# ──────────────────────────────────────────────────────────────────────────────

def _find_recent_manipulation(bars: List[Bar], lookback: int = _MICRO_MANIP_LOOKBACK) -> Optional[Tuple[MicroManip, int]]:
    """
    Scan last `lookback` bars from RIGHT for a manipulation spike.
    Returns (MicroManip, accum_start_idx_hint) where accum_start_idx_hint
    is where to start scanning backward for accumulation.
    """
    if len(bars) < 4:
        return None
    end = len(bars) - 1
    start = max(0, end - lookback + 1)

    for i in range(end, start - 1, -1):
        bar = bars[i]
        if i <= 0:
            continue
        context = bars[max(0, i-5):i]
        if len(context) < 3:
            continue
        context_hi = max(b.high for b in context)
        context_lo = min(b.low for b in context)
        context_rng = context_hi - context_lo
        if context_rng <= 0:
            continue

        # HIGH side manipulation
        high_excess = bar.high - context_hi
        if high_excess >= _MIN_MANIP_EXCESS_PCT * context_rng:
            body = max(1e-12, abs(bar.close - bar.open))
            wick = bar.high - max(bar.open, bar.close)
            wick_ratio = wick / body
            close_back = bar.close <= context_hi
            excess_pct = high_excess / context_rng
            return MicroManip(
                side="HIGH",
                extreme=bar.high,
                close_back=close_back,
                bar_idx=i,
                excess_pct=excess_pct,
                wick_ratio=wick_ratio,
                close_price=bar.close,
            ), max(0, i - _MICRO_ACCUM_BARS)

        # LOW side manipulation
        low_excess = context_lo - bar.low
        if low_excess >= _MIN_MANIP_EXCESS_PCT * context_rng:
            body = max(1e-12, abs(bar.close - bar.open))
            wick = min(bar.open, bar.close) - bar.low
            wick_ratio = wick / body
            close_back = bar.close >= context_lo
            excess_pct = low_excess / context_rng
            return MicroManip(
                side="LOW",
                extreme=bar.low,
                close_back=close_back,
                bar_idx=i,
                excess_pct=excess_pct,
                wick_ratio=wick_ratio,
                close_price=bar.close,
            ), max(0, i - _MICRO_ACCUM_BARS)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2: Find accumulation immediately BEFORE manipulation
# ──────────────────────────────────────────────────────────────────────────────

def _find_accum_before_manip(bars: List[Bar], manip_idx: int,
                              max_lookback: int = _MICRO_ACCUM_BARS,
                              min_bars: int = 3) -> Optional[MicroAccum]:
    """
    Scan [manip_idx - max_lookback .. manip_idx - 1] for tight accumulation.
    Must have multiple touches on range boundaries.
    """
    if manip_idx <= min_bars:
        return None
    accum_start = max(0, manip_idx - max_lookback)
    window = bars[accum_start:manip_idx]
    if len(window) < min_bars:
        return None

    atr = _calc_atr(bars[max(0, accum_start-5):manip_idx])
    if atr <= 0:
        atr = sum(b.high - b.low for b in window) / max(1, len(window))

    best: Optional[MicroAccum] = None
    best_tightness = float("inf")

    for n in range(min_bars, min(max_lookback + 1, len(window) + 1)):
        for s in range(len(window) - n + 1):
            sub = window[s:s+n]
            hi = max(b.high for b in sub)
            lo = min(b.low for b in sub)
            rng = hi - lo
            if rng <= 0:
                continue
            if atr > 0 and rng < _MIN_ACCUM_RANGE_ATR * atr:
                continue
            touches_hi = sum(1 for b in sub if abs(b.high - hi) < atr * 0.15)
            touches_lo = sum(1 for b in sub if abs(b.low - lo) < atr * 0.15)
            if max(touches_hi, touches_lo) < 2 and n >= 4:
                continue
            tightness = rng / atr if atr > 0 else rng
            if tightness < best_tightness:
                best_tightness = tightness
                best = MicroAccum(
                    high=hi,
                    low=lo,
                    start_idx=accum_start + s,
                    end_idx=accum_start + s + n - 1,
                    bar_count=n,
                    avg_body=sum(abs(b.close - b.open) for b in sub) / max(1, len(sub)),
                )
    return best


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3: Detect displacement AFTER manipulation
# ──────────────────────────────────────────────────────────────────────────────

def _find_micro_displace(bars: List[Bar], manip: MicroManip,
                          accum: MicroAccum) -> Optional[MicroDisplace]:
    """Measure displacement AFTER manipulation bar."""
    post = bars[manip.bar_idx + 1:]
    if len(post) < 1:
        return None

    direction = "BEAR" if manip.side == "HIGH" else "BULL"
    start_price = bars[manip.bar_idx].close
    current_price = post[-1].close if post else start_price

    favorable = 0
    pullback_bars = 0

    for bar in post:
        if direction == "BULL":
            if bar.close > bar.open:
                favorable += 1
            elif bar.close < bar.open:
                pullback_bars += 1
        else:
            if bar.close < bar.open:
                favorable += 1
            elif bar.close > bar.open:
                pullback_bars += 1

    moved = abs(current_price - start_price)
    displacement_pct = moved / accum.width if accum.width > 0 else 0.0

    return MicroDisplace(
        direction=direction,
        start_price=start_price,
        current_price=current_price,
        bars_traced=len(post),
        pullback_candles=pullback_bars,
        displacement_pct=displacement_pct,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Redistribution estimator
# ──────────────────────────────────────────────────────────────────────────────

def _find_opposing_liquidity(bars: List[Bar], manip: MicroManip,
                              accum: MicroAccum) -> Tuple[float, List[str]]:
    reasons: List[str] = []
    if manip.side == "HIGH":
        target = accum.low
        reasons.append(f"Primary target: accumulation low={target}")
        for i in range(len(bars)-3, max(0, len(bars)-60), -1):
            if i < 1 or i >= len(bars)-1:
                continue
            if bars[i].low < bars[i-1].low and bars[i].low < bars[i+1].low:
                if bars[i].low < target:
                    target = bars[i].low
                    reasons.append(f"Prior swing low: {target}")
                    break
    else:
        target = accum.high
        reasons.append(f"Primary target: accumulation high={target}")
        for i in range(len(bars)-3, max(0, len(bars)-60), -1):
            if i < 1 or i >= len(bars)-1:
                continue
            if bars[i].high > bars[i-1].high and bars[i].high > bars[i+1].high:
                if bars[i].high > target:
                    target = bars[i].high
                    reasons.append(f"Prior swing high: {target}")
                    break
    return target, reasons


def estimate_redistribution(
    bars: List[Bar],
    accum: MicroAccum,
    manip: MicroManip,
    displace: MicroDisplace,
    symbol: str = "XAUUSD",
) -> RedistributionEstimate:
    reasons: List[str] = []
    target, liq_reasons = _find_opposing_liquidity(bars, manip, accum)
    reasons.extend(liq_reasons)

    distance = abs(manip.extreme - target)
    atr = _calc_atr(bars, period=14)
    if atr <= 0:
        atr = displace.current_price * 0.002
        reasons.append(f"ATR fallback: {atr:.5f}")

    last_hour = _bar_hour_utc(bars[-1])
    session_label = _session_label(last_hour)
    overlap = (13 <= last_hour < 15)
    mult = _session_multiplier(session_label, overlap)

    expected_per_candle = max(atr * mult, 1e-12)
    remaining = max(abs(displace.current_price - target), 0.01)
    est_candles = remaining / expected_per_candle
    est_minutes = est_candles * 15.0

    feas = 0.50
    if displace.displacement_pct >= 0.50:
        feas += 0.25
        reasons.append(f"Already displaced {displace.displacement_pct:.0%} -> +0.25")
    elif displace.displacement_pct >= 0.30:
        feas += 0.15
        reasons.append(f"Already displaced {displace.displacement_pct:.0%} -> +0.15")
    else:
        reasons.append(f"Only displaced {displace.displacement_pct:.0%} -> neutral")

    if est_candles <= 3:
        feas += 0.15
        reasons.append("Target within ~3 candles -> +0.15")
    elif est_candles <= 6:
        feas += 0.05
        reasons.append("Target within ~6 candles -> +0.05")
    elif est_candles <= 12:
        pass
    else:
        feas -= 0.20
        reasons.append(f"Target >12 candles away ({est_candles:.1f}) -> -0.20")

    if displace.pullback_candles >= 3:
        feas -= 0.15
        reasons.append("3+ pullback candles after manip -> stalling -0.15")

    feas = max(0.0, min(1.0, feas))

    return RedistributionEstimate(
        manip_extreme=manip.extreme,
        opposing_liquidity=target,
        distance=distance,
        m15_atr=atr,
        session_label=session_label,
        session_multiplier=mult,
        est_candles=round(est_candles, 1),
        est_minutes=round(est_minutes, 1),
        feasibility_score=round(feas, 3),
        reasons=reasons,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main entry: detect micro AMD cycle
# ──────────────────────────────────────────────────────────────────────────────

def detect_micro_amd(
    bars: List[Bar],
    max_scan: int = _MICRO_MAX_SCAN_BARS,
    symbol: str = "XAUUSD",
) -> MicroAMDResult:
    """Scan last bars for micro AMD cycle: manip -> accum-before -> displacement."""
    result = MicroAMDResult()
    if len(bars) < 5:
        result.reasons.append("Need >=5 bars for micro AMD")
        return result

    # PHASE 1: Find manipulation spike (the ANCHOR)
    mani_out = _find_recent_manipulation(bars)
    if not mani_out:
        result.phase = MicroPhase.ACCUMULATION
        result.reasons.append("No manipulation spike in recent bars")
        return result

    manip, accum_hint = mani_out
    result.manip = manip
    result.reasons.append(
        f"Micro manipulation: {manip.side} at bar-{manip.bar_idx}, "
        f"excess={manip.excess_pct:.0%}, close_back={manip.close_back}, wick={manip.wick_ratio:.2f}"
    )

    # PHASE 2: Find accumulation BEFORE manipulation
    accum = _find_accum_before_manip(bars, manip.bar_idx)
    if not accum:
        result.phase = MicroPhase.MANIPULATION
        result.reasons.append("Manipulation found but no accumulation base BEFORE it")
        return result

    result.accum = accum
    result.reasons.append(
        f"Micro accumulation: {accum.bar_count} bars [{accum.start_idx}:{accum.end_idx}], "
        f"range={accum.width:.5f}"
    )

    # PHASE 3: Check displacement AFTER manipulation
    displace = _find_micro_displace(bars, manip, accum)
    if not displace:
        result.phase = MicroPhase.MANIPULATION
        result.direction = "BEAR" if manip.side == "HIGH" else "BULL"
        result.reasons.append("Awaiting displacement confirmation")
        return result

    result.displace = displace
    result.direction = displace.direction

    if displace.bars_traced < 2:
        result.phase = MicroPhase.MANIPULATION
        result.reasons.append(f"Displacement only {displace.bars_traced} bar(s) -- awaiting redistribution")
        return result

    # PHASE 4: Redistributon confirmed
    result.phase = MicroPhase.REDISTRIBUTION
    result.detected = True
    result.reasons.append(
        f"REDISTRIBUTION: {displace.direction}, {displace.bars_traced} bars, "
        f"disp={displace.displacement_pct:.0%}, pullbacks={displace.pullback_candles}"
    )

    # Entry / SL / TP
    rng = accum.width
    if manip.side == "HIGH":
        entry = manip.extreme if not manip.close_back else accum.high
        if bars and abs(bars[-1].close - accum.high) < rng * 0.30:
            entry = max(bars[-1].close, entry)
        sl = manip.extreme + rng * 0.15
        tp = accum.low - rng * 0.50
    else:
        entry = manip.extreme if not manip.close_back else accum.low
        if bars and abs(bars[-1].close - accum.low) < rng * 0.30:
            entry = min(bars[-1].close, entry)
        sl = manip.extreme - rng * 0.15
        tp = accum.high + rng * 0.50

    result.entry = round(entry, 5)
    result.sl = round(sl, 5)
    result.tp = round(tp, 5)

    # Confidence
    conf = 0.30
    if manip.close_back:
        conf += 0.15
        result.reasons.append("Close-back (+0.15)")
    if manip.wick_ratio >= 2.0:
        conf += 0.10
    elif manip.wick_ratio >= 1.5:
        conf += 0.05
    if displace.bars_traced >= 3:
        conf += 0.08
    if displace.displacement_pct >= 0.30:
        conf += 0.10
    if displace.pullback_candles == 0:
        conf += 0.05
    result.confidence = round(max(0.0, min(1.0, conf)), 3)

    # Redistribution estimate
    result.redistribution = estimate_redistribution(bars, accum, manip, displace, symbol=symbol)
    for r in result.redistribution.reasons:
        result.reasons.append(f"  {r}")

    return result


if __name__ == "__main__":
    print("="*70)
    print("MICRO AMD DETECTOR SELF-TEST")
    print("="*70)

    base = 4500.0
    bars: List[Bar] = []
    for i in range(3):
        bars.append(Bar(time=i*900, open=base-5, high=base+2, low=base-8, close=base-3))
    for i in range(5):
        bars.append(Bar(time=(i+3)*900, open=base+0.5, high=base+3.0, low=base-3.0, close=base+0.5))
    bars.append(Bar(time=8*900, open=base+1, high=base+48, low=base+1, close=base+2))
    bars.append(Bar(time=9*900, open=base+2, high=base+5, low=base-6, close=base-4))
    bars.append(Bar(time=10*900, open=base-4, high=base-1, low=base-13, close=base-11))
    bars.append(Bar(time=11*900, open=base-11, high=base-8, low=base-19, close=base-15))

    res = detect_micro_amd(bars, symbol="XAUUSD")
    print(f"Detected:    {res.detected}")
    print(f"Phase:       {res.phase}")
    print(f"Direction:   {res.direction}")
    print(f"Confidence:  {res.confidence}")
    print(f"Entry/SL/TP: {res.entry} / {res.sl} / {res.tp}")
    if res.accum:
        print(f"Accum:       [{res.accum.start_idx}:{res.accum.end_idx}] range={res.accum.width:.2f}")
    if res.manip:
        print(f"Manip:       idx={res.manip.bar_idx} side={res.manip.side} excess={res.manip.excess_pct:.0%}")
    if res.redistribution:
        re = res.redistribution
        print(f"\n--- Redistribution Estimate ---")
        print(f"  Session:        {re.session_label} (mult={re.session_multiplier})")
        print(f"  M15 ATR:        {re.m15_atr:.2f}")
        print(f"  Target:         {re.opposing_liquidity}")
        print(f"  Est. candles:   {re.est_candles}")
        print(f"  Est. minutes:   {re.est_minutes:.0f}")
        print(f"  Feasibility:    {re.feasibility_score}")
    print("\nReasons:")
    for r in res.reasons:
        print(f"  * {r}")
    print("="*70)
