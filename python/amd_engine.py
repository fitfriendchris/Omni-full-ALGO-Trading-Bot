"""
amd_engine.py — ICT AMD (Accumulation / Manipulation / Distribution) detector.

AMD Model (ICT):
  Accumulation : Asian session. Smart money quietly builds positions inside
                 a tight consolidation. The range boundaries become liquidity
                 magnets — equal highs above, equal lows below.
  Manipulation : London open (07–10 UTC) or NY open (12–15 UTC). Smart money
                 runs stops on ONE side of the Asian range (Judas swing). Price
                 spikes beyond the boundary and closes back inside — retail
                 traders are trapped in the wrong direction.
  Distribution : True directional delivery. Price moves OPPOSITE to the
                 manipulation spike through the rest of the session.

Detection rules (all pure, deterministic, no I/O):
  1. Asian range     — max-high / min-low of M15 bars inside ASIAN window.
                       Filtered: range must exceed MIN_RANGE_PIPS.
  2. Manipulation    — first bar after a kill-zone open whose HIGH (or LOW)
                       exceeds the Asian range boundary by >= MANIP_BUFFER_PCT
                       of the range.  Candle must show spike character:
                       wick-to-body ratio >= WICK_RATIO (default 1.5).
                       OR the candle closes back inside the range on the same bar.
  3. Confirmation    — manipulation candle OR the next bar closes BACK inside
                       the Asian range (hard re-entry confirmation).
  4. Direction       — opposite to the manipulation side
                       (spike above high → BEAR distribution,
                        spike below low  → BULL distribution).

Entry / SL / TP:
  Entry  — close of the first bar that closes back inside the range.
  SL     — manipulation extreme + SL_BUFFER_PCT × range beyond the spike.
  TP1    — opposite side of Asian range   (minimum target, ~1–2 R).
  TP2    — range × TP2_MULT from entry    (default 1.5×, medium target).
  TP3    — range × TP3_MULT from entry    (default 2.5×, runner).

Confidence score (0.0–1.0):
  Base 0.40, bonuses layered:
  +0.15  kill-zone alignment (London > NY weight)
  +0.10  wick-to-body >= 2.0 (very clean spike)
  +0.10  hard close-back confirmation on SAME manipulation candle
  +0.10  Asian range width > 0.75× ATR(20) of the pre-session bars
  +0.08  manipulation exceeds range by >= 30% of range (strong sweep)
  −0.15  if confirming candle is NOT a close-back (next-bar only)
  Score clamped to [0.0, 1.0].

Usage:
    from amd_engine import detect_amd, AMDResult, AMDPhase
    bars_m15 = [Bar(time=..., open=..., high=..., low=..., close=...), ...]
    result = detect_amd(bars_m15)
    if result and result.phase == AMDPhase.DISTRIBUTION:
        print(result.direction, result.entry, result.sl, result.tp1)

Run `python amd_engine.py` for a built-in self-test.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from smc_engine import Bar  # re-use the same Bar type

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Constants / defaults (all overridable via detect_amd() kwargs)
# ──────────────────────────────────────────────────────────────────────────────

# Session windows in UTC hours (inclusive start, exclusive end)
_ASIAN_START_H   = 0     # 00:00 UTC
_ASIAN_END_H     = 7     # 07:00 UTC

# Kill zones: (start_h, end_h, label, confidence_weight)
_KILL_ZONES = [
    (7,  10, "LONDON_OPEN",   1.00),
    (12, 15, "NY_OPEN",       0.85),
    (15, 17, "LONDON_CLOSE",  0.60),
]

_MIN_RANGE_PIPS  = 5.0    # minimum Asian range in pip-equivalent points
_MANIP_BUFFER_PCT = 0.10  # manipulation must exceed range by ≥ 10%
_WICK_RATIO       = 1.3   # manipulation candle: wick/body ≥ 1.3 (spike shape)
_SL_BUFFER_PCT    = 0.25  # SL placed 25% of range beyond manipulation extreme
_TP2_MULT         = 1.5   # TP2 = entry ± (range × 1.5)
_TP3_MULT         = 2.5   # TP3 = entry ± (range × 2.5)
_ATR_PERIOD       = 20    # bars for ATR used in confidence scoring
_MAX_MANIP_BARS   = 6     # look at most 6 bars into kill zone for manipulation


# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

class AMDPhase(str, Enum):
    ACCUMULATION = "ACCUMULATION"
    MANIPULATION = "MANIPULATION"
    DISTRIBUTION = "DISTRIBUTION"
    NONE         = "NONE"


@dataclass
class AsianRange:
    high:        float
    low:         float
    start_time:  float   # unix ts of first bar
    end_time:    float   # unix ts of last bar
    bar_count:   int

    @property
    def width(self) -> float:
        return max(1e-12, self.high - self.low)

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class ManipulationEvent:
    side:           str          # "HIGH" (Judas up) or "LOW" (Judas down)
    extreme:        float        # the spike high or spike low
    bar_idx:        int          # index in bars list
    bar_time:       float
    wick_ratio:     float        # wick / body of the manipulation candle
    excess_pct:     float        # how much % of range the spike exceeded the boundary
    close_back:     bool         # did this same candle close back inside the range?
    confirmed:      bool         # True if next bar also closes back (if not same-bar)
    kill_zone:      str          # which kill zone triggered this
    kz_weight:      float        # confidence weight of the kill zone


@dataclass
class LiquidityLevel:
    """An HTF swing extreme or equal-highs/lows pool used as a TP target."""
    price:      float
    kind:       str       # "PDL", "PDH", "PWL", "PWH", "EQ_LOW", "EQ_HIGH", "SWING_LOW", "SWING_HIGH"
    distance:   float     # |price − reference_price|
    bar_time:   float     # timestamp the level was set


@dataclass
class ReAccumulation:
    """A consolidation that forms after Distribution-1."""
    high:        float
    low:         float
    start_time:  float
    end_time:    float
    bar_count:   int
    midpoint:    float

    @property
    def width(self) -> float:
        return max(1e-12, self.high - self.low)


@dataclass
class ContinuationAMD:
    """
    Second-leg AMD setup: re-accumulation → re-manipulation → expanded
    distribution toward deeper HTF liquidity.
    """
    direction:        str               # same as parent AMD distribution direction
    re_accum:         ReAccumulation
    re_manip:         ManipulationEvent

    entry:            float
    sl:               float
    tp1:              float             # nearest HTF liquidity level
    tp2:              float             # next HTF liquidity (PDL/PDH)
    tp3:              float             # deepest HTF liquidity (PWL/PWH)

    rr_tp1:           Optional[float]
    rr_tp2:           Optional[float]
    rr_tp3:           Optional[float]

    targets:          List[LiquidityLevel]   # which liquidity pools were chosen
    confidence:       float
    reasons:          List[str]


@dataclass
class AMDResult:
    """Full AMD setup result. None fields mean that phase was not detected."""
    phase:          AMDPhase
    direction:      str          # "BULL" or "BEAR" (distribution direction)
    asian_range:    AsianRange
    manipulation:   Optional[ManipulationEvent]

    entry:          Optional[float]
    sl:             Optional[float]
    tp1:            Optional[float]   # opposite side of Asian range
    tp2:            Optional[float]   # range × TP2_MULT from entry
    tp3:            Optional[float]   # range × TP3_MULT from entry

    confidence:     float            # 0.0 – 1.0
    reasons:        List[str]        # human-readable breakdown

    # Metadata useful for downstream signal writers
    entry_bar_time: Optional[float]  = None
    rr_tp1:         Optional[float]  = None
    rr_tp2:         Optional[float]  = None
    rr_tp3:         Optional[float]  = None

    # Optional continuation — set when a second-leg setup is detected
    continuation:   Optional[ContinuationAMD] = None
    htf_liquidity:  List[LiquidityLevel]      = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _bar_hour_utc(bar: Bar) -> int:
    """Return the UTC hour of a bar's timestamp."""
    try:
        return datetime.fromtimestamp(bar.time, tz=timezone.utc).hour
    except Exception:
        return -1


def _atr(bars: List[Bar], period: int = _ATR_PERIOD) -> float:
    """Simple ATR over the last `period` bars."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, min(period + 1, len(bars))):
        b, p = bars[-i], bars[-(i + 1)]
        tr = max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _wick_ratio(bar: Bar, side: str) -> float:
    """
    Wick-to-body ratio for the manipulation side.
    side == "HIGH" → upper wick / body
    side == "LOW"  → lower wick / body
    """
    body = max(1e-12, abs(bar.close - bar.open))
    if side == "HIGH":
        wick = bar.high - max(bar.open, bar.close)
    else:
        wick = min(bar.open, bar.close) - bar.low
    return wick / body


def _in_asian(h: int) -> bool:
    return _ASIAN_START_H <= h < _ASIAN_END_H


def _kill_zone(h: int):
    """Return (label, weight) for the given UTC hour, or None."""
    for start, end, label, weight in _KILL_ZONES:
        if start <= h < end:
            return label, weight
    return None, 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1: Accumulation — build Asian range
# ──────────────────────────────────────────────────────────────────────────────

def _build_asian_range(bars: List[Bar], pip_size: float = 0.0001) -> Optional[AsianRange]:
    """
    Scan bars for the most recent completed Asian session.
    Returns None if no Asian bars found or range < MIN_RANGE_PIPS.
    """
    asian_bars = [b for b in bars if _in_asian(_bar_hour_utc(b)) and b.time > 0]
    if len(asian_bars) < 3:
        log.debug("amd: not enough Asian bars (%d)", len(asian_bars))
        return None

    # Use only the MOST RECENT Asian session block (contiguous or within same day)
    # Sort by time and take the last window
    asian_bars.sort(key=lambda b: b.time)

    # Group into sessions by finding gaps > 14 hours
    sessions: List[List[Bar]] = []
    current: List[Bar] = [asian_bars[0]]
    for b in asian_bars[1:]:
        if b.time - current[-1].time > 14 * 3600:
            sessions.append(current)
            current = [b]
        else:
            current.append(b)
    sessions.append(current)

    session = sessions[-1]  # most recent
    hi = max(b.high for b in session)
    lo = min(b.low  for b in session)
    rng = hi - lo

    min_range = _MIN_RANGE_PIPS * pip_size
    if rng < min_range:
        log.debug("amd: Asian range %.5f < min %.5f — skip", rng, min_range)
        return None

    return AsianRange(
        high=hi,
        low=lo,
        start_time=session[0].time,
        end_time=session[-1].time,
        bar_count=len(session),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2: Manipulation — detect Judas swing
# ──────────────────────────────────────────────────────────────────────────────

def _detect_manipulation(
    bars: List[Bar],
    asian: AsianRange,
    pip_size: float = 0.0001,
) -> Optional[ManipulationEvent]:
    """
    Scan bars AFTER the Asian session for a kill-zone manipulation spike.
    Returns the first qualifying ManipulationEvent found.
    """
    rng = asian.width
    min_excess = rng * _MANIP_BUFFER_PCT

    post_asian = [
        (i, b) for i, b in enumerate(bars)
        if b.time > asian.end_time
    ]

    kill_zone_bars: List[tuple] = []
    for i, b in post_asian:
        h = _bar_hour_utc(b)
        kz, kw = _kill_zone(h)
        if kz:
            kill_zone_bars.append((i, b, kz, kw))

    if not kill_zone_bars:
        return None

    # Scan up to MAX_MANIP_BARS inside each kill zone
    # Group by kill zone start
    kz_groups: dict = {}
    for i, b, kz, kw in kill_zone_bars:
        kz_groups.setdefault(kz, []).append((i, b, kw))

    for kz_label, group in kz_groups.items():
        group_bars = group[:_MAX_MANIP_BARS]
        kw = group_bars[0][2]

        for idx, (bar_idx, bar, _) in enumerate(group_bars):
            # Check HIGH side manipulation (Judas up → BEAR distribution)
            high_excess = bar.high - asian.high
            if high_excess >= min_excess:
                wr = _wick_ratio(bar, "HIGH")
                close_back = bar.close <= asian.high
                excess_pct = high_excess / rng

                # Check next bar for close-back confirmation
                confirmed = close_back
                if not close_back and idx + 1 < len(group_bars):
                    next_bar = group_bars[idx + 1][1]
                    confirmed = next_bar.close <= asian.high

                if wr >= _WICK_RATIO or close_back:
                    return ManipulationEvent(
                        side="HIGH",
                        extreme=bar.high,
                        bar_idx=bar_idx,
                        bar_time=bar.time,
                        wick_ratio=wr,
                        excess_pct=excess_pct,
                        close_back=close_back,
                        confirmed=confirmed,
                        kill_zone=kz_label,
                        kz_weight=kw,
                    )

            # Check LOW side manipulation (Judas down → BULL distribution)
            low_excess = asian.low - bar.low
            if low_excess >= min_excess:
                wr = _wick_ratio(bar, "LOW")
                close_back = bar.close >= asian.low
                excess_pct = low_excess / rng

                confirmed = close_back
                if not close_back and idx + 1 < len(group_bars):
                    next_bar = group_bars[idx + 1][1]
                    confirmed = next_bar.close >= asian.low

                if wr >= _WICK_RATIO or close_back:
                    return ManipulationEvent(
                        side="LOW",
                        extreme=bar.low,
                        bar_idx=bar_idx,
                        bar_time=bar.time,
                        wick_ratio=wr,
                        excess_pct=excess_pct,
                        close_back=close_back,
                        confirmed=confirmed,
                        kill_zone=kz_label,
                        kz_weight=kw,
                    )

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3: Confirmation bar — entry, SL, TP
# ──────────────────────────────────────────────────────────────────────────────

def _build_levels(
    manip: ManipulationEvent,
    asian: AsianRange,
) -> tuple:
    """
    Returns (direction, entry, sl, tp1, tp2, tp3, entry_bar_time).
    """
    rng = asian.width

    if manip.side == "HIGH":
        # Judas up → distribution is BEAR
        direction  = "BEAR"
        entry      = manip.extreme if manip.close_back else asian.high
        sl         = manip.extreme + rng * _SL_BUFFER_PCT
        tp1        = asian.low               # first target: opposite side of range
        tp2        = asian.low - rng * _TP2_MULT   # extension beyond Asian low
        tp3        = asian.low - rng * _TP3_MULT   # runner extension
    else:
        # Judas down → distribution is BULL
        direction  = "BULL"
        entry      = manip.extreme if manip.close_back else asian.low
        sl         = manip.extreme - rng * _SL_BUFFER_PCT
        tp1        = asian.high              # first target: opposite side of range
        tp2        = asian.high + rng * _TP2_MULT  # extension beyond Asian high
        tp3        = asian.high + rng * _TP3_MULT  # runner extension

    return direction, entry, sl, tp1, tp2, tp3


# ──────────────────────────────────────────────────────────────────────────────
# Confidence scoring
# ──────────────────────────────────────────────────────────────────────────────

def _score(
    manip: ManipulationEvent,
    asian: AsianRange,
    bars: List[Bar],
) -> tuple[float, List[str]]:
    reasons: List[str] = []
    score = 0.40
    reasons.append("base=0.40")

    # Kill zone alignment
    kz_bonus = manip.kz_weight * 0.15
    score += kz_bonus
    reasons.append(f"{manip.kill_zone} +{kz_bonus:.2f}")

    # Wick quality
    if manip.wick_ratio >= 2.0:
        score += 0.10
        reasons.append("wick_ratio>=2.0 +0.10")
    elif manip.wick_ratio >= 1.5:
        score += 0.05
        reasons.append("wick_ratio>=1.5 +0.05")

    # Same-bar close-back (hardest confirmation)
    if manip.close_back:
        score += 0.10
        reasons.append("same_bar_close_back +0.10")
    elif manip.confirmed:
        reasons.append("next_bar_confirm +0.00")
    else:
        score -= 0.15
        reasons.append("no_confirmation -0.15")

    # Asian range width vs ATR
    atr = _atr(bars[:manip.bar_idx] if manip.bar_idx > 0 else bars)
    if atr > 0 and asian.width >= 0.75 * atr:
        score += 0.10
        reasons.append("range_vs_atr +0.10")

    # Sweep depth
    if manip.excess_pct >= 0.30:
        score += 0.08
        reasons.append("deep_sweep>=30% +0.08")
    elif manip.excess_pct >= 0.15:
        score += 0.04
        reasons.append("sweep>=15% +0.04")

    score = max(0.0, min(1.0, score))
    return score, reasons


# ──────────────────────────────────────────────────────────────────────────────
# HTF liquidity scanner — finds deep pools below/above current price
# ──────────────────────────────────────────────────────────────────────────────

def _swing_lows(bars: List[Bar], left: int = 3, right: int = 3) -> List[tuple]:
    """Return list of (idx, low, time) for fractal swing lows."""
    out = []
    for i in range(left, len(bars) - right):
        b = bars[i]
        if all(b.low <= bars[i - k].low for k in range(1, left + 1)) and \
           all(b.low <= bars[i + k].low for k in range(1, right + 1)):
            out.append((i, b.low, b.time))
    return out


def _swing_highs(bars: List[Bar], left: int = 3, right: int = 3) -> List[tuple]:
    """Return list of (idx, high, time) for fractal swing highs."""
    out = []
    for i in range(left, len(bars) - right):
        b = bars[i]
        if all(b.high >= bars[i - k].high for k in range(1, left + 1)) and \
           all(b.high >= bars[i + k].high for k in range(1, right + 1)):
            out.append((i, b.high, b.time))
    return out


def _equal_levels(points: List[tuple], tolerance: float) -> List[tuple]:
    """Cluster points whose price is within `tolerance`. Returns clusters with ≥ 2 members."""
    if not points:
        return []
    sorted_pts = sorted(points, key=lambda p: p[1])
    clusters: List[List[tuple]] = []
    cur = [sorted_pts[0]]
    for p in sorted_pts[1:]:
        if abs(p[1] - cur[-1][1]) <= tolerance:
            cur.append(p)
        else:
            if len(cur) >= 2:
                clusters.append(cur)
            cur = [p]
    if len(cur) >= 2:
        clusters.append(cur)
    return [(c[0][0], sum(p[1] for p in c) / len(c), c[-1][2]) for c in clusters]


def find_htf_liquidity(
    bars: List[Bar],
    reference_price: float,
    direction: str,
    *,
    pip_size: float = 0.0001,
    eq_tolerance_pips: float = 5.0,
    max_levels: int = 6,
) -> List[LiquidityLevel]:
    """
    Find significant HTF liquidity pools below (BEAR) or above (BULL) the
    reference price. Returns up to `max_levels` levels sorted by distance.

    For BEAR: scan for swing lows below reference, equal lows clusters,
              previous-day-low equivalents (lowest low in last 96 M15 bars = 1 day).
    For BULL: same logic on the upside.
    """
    if len(bars) < 20:
        return []

    out: List[LiquidityLevel] = []
    eq_tol = eq_tolerance_pips * pip_size

    if direction == "BEAR":
        swings = _swing_lows(bars)
        # Individual swing lows below reference
        for idx, lo, t in swings:
            if lo < reference_price:
                out.append(LiquidityLevel(
                    price=lo, kind="SWING_LOW",
                    distance=reference_price - lo, bar_time=t))
        # Equal-lows clusters (relative-equal lows = ICT inducement liquidity)
        for idx, lo, t in _equal_levels(swings, eq_tol):
            if lo < reference_price:
                out.append(LiquidityLevel(
                    price=lo, kind="EQ_LOW",
                    distance=reference_price - lo, bar_time=t))
        # Previous-day-low (lowest low in last ~96 M15 bars before today's session)
        if len(bars) >= 96:
            day_lookback = bars[-192:-96] if len(bars) >= 192 else bars[:-96]
            if day_lookback:
                pdl = min(day_lookback, key=lambda b: b.low)
                if pdl.low < reference_price:
                    out.append(LiquidityLevel(
                        price=pdl.low, kind="PDL",
                        distance=reference_price - pdl.low, bar_time=pdl.time))
        # Previous-week-low (lowest low across full lookback)
        if len(bars) >= 480:  # ~5 days of M15
            wk = bars[-480:]
            pwl = min(wk, key=lambda b: b.low)
            if pwl.low < reference_price:
                out.append(LiquidityLevel(
                    price=pwl.low, kind="PWL",
                    distance=reference_price - pwl.low, bar_time=pwl.time))
    else:  # BULL
        swings = _swing_highs(bars)
        for idx, hi, t in swings:
            if hi > reference_price:
                out.append(LiquidityLevel(
                    price=hi, kind="SWING_HIGH",
                    distance=hi - reference_price, bar_time=t))
        for idx, hi, t in _equal_levels(swings, eq_tol):
            if hi > reference_price:
                out.append(LiquidityLevel(
                    price=hi, kind="EQ_HIGH",
                    distance=hi - reference_price, bar_time=t))
        if len(bars) >= 96:
            day_lookback = bars[-192:-96] if len(bars) >= 192 else bars[:-96]
            if day_lookback:
                pdh = max(day_lookback, key=lambda b: b.high)
                if pdh.high > reference_price:
                    out.append(LiquidityLevel(
                        price=pdh.high, kind="PDH",
                        distance=pdh.high - reference_price, bar_time=pdh.time))
        if len(bars) >= 480:
            wk = bars[-480:]
            pwh = max(wk, key=lambda b: b.high)
            if pwh.high > reference_price:
                out.append(LiquidityLevel(
                    price=pwh.high, kind="PWH",
                    distance=pwh.high - reference_price, bar_time=pwh.time))

    # Sort by distance, dedupe near-duplicates, cap at max_levels
    out.sort(key=lambda lv: lv.distance)
    deduped: List[LiquidityLevel] = []
    for lv in out:
        if not any(abs(lv.price - x.price) <= eq_tol for x in deduped):
            deduped.append(lv)
        if len(deduped) >= max_levels:
            break
    return deduped


# ──────────────────────────────────────────────────────────────────────────────
# Continuation AMD — re-accumulation → re-manipulation → expanded distribution
# ──────────────────────────────────────────────────────────────────────────────

def _detect_re_accumulation(
    bars: List[Bar],
    after_idx: int,
    direction: str,
    *,
    min_bars: int = 4,
    max_bars: int = 20,
    range_atr_frac: float = 1.5,
) -> Optional[ReAccumulation]:
    """
    Find a consolidation that forms AFTER the initial distribution leg.
    A re-accumulation is a contiguous block of bars whose total range is
    less than `range_atr_frac × ATR` — i.e. price stops trending.

    ATR is measured on bars BEFORE the search start, so the threshold
    reflects pre-consolidation volatility (not the next leg's expansion).

    Scans `bars` starting from `after_idx`. Returns the longest qualifying
    block, or None.
    """
    if after_idx >= len(bars) - min_bars:
        return None

    # ATR from the trending bars before the search window
    pre_window = bars[max(0, after_idx - 20):after_idx]
    atr = _atr(pre_window, period=20) if len(pre_window) >= 5 else _atr(bars, period=20)
    if atr <= 0:
        return None
    max_width = atr * range_atr_frac

    best: Optional[ReAccumulation] = None
    n = len(bars)

    for start in range(after_idx, n - min_bars):
        for end in range(start + min_bars, min(start + max_bars + 1, n + 1)):
            window = bars[start:end]
            hi = max(b.high for b in window)
            lo = min(b.low  for b in window)
            if (hi - lo) < max_width:
                cand = ReAccumulation(
                    high=hi, low=lo,
                    start_time=window[0].time,
                    end_time=window[-1].time,
                    bar_count=end - start,
                    midpoint=(hi + lo) / 2.0,
                )
                # Prefer the longest qualifying block
                if best is None or cand.bar_count > best.bar_count:
                    best = cand
            else:
                break  # window broke out, stop extending this start

    return best


def _detect_re_manipulation(
    bars: List[Bar],
    re_accum: ReAccumulation,
    direction: str,
) -> Optional[ManipulationEvent]:
    """
    After re-accumulation, look for the COUNTER-TREND sweep that breaks the
    re-accumulation boundary in the OPPOSITE direction of the parent
    distribution — then fails. This is the classic "pullback that traps
    counter-trend traders before the bigger leg."

    For BEAR distribution: re-manipulation = sweep ABOVE re_accum.high (Judas up)
    For BULL distribution: re-manipulation = sweep BELOW re_accum.low (Judas down)
    """
    rng = re_accum.width
    min_excess = rng * _MANIP_BUFFER_PCT

    # Bars after the re-accumulation block ends
    post = [(i, b) for i, b in enumerate(bars) if b.time > re_accum.end_time]
    if not post:
        return None

    target_side = "HIGH" if direction == "BEAR" else "LOW"

    for idx, (bar_idx, bar) in enumerate(post[:_MAX_MANIP_BARS * 2]):
        if target_side == "HIGH":
            excess = bar.high - re_accum.high
            if excess >= min_excess:
                wr = _wick_ratio(bar, "HIGH")
                close_back = bar.close <= re_accum.high
                excess_pct = excess / rng

                confirmed = close_back
                if not close_back and idx + 1 < len(post):
                    nb = post[idx + 1][1]
                    confirmed = nb.close <= re_accum.high

                if (wr >= _WICK_RATIO or close_back) and confirmed:
                    return ManipulationEvent(
                        side="HIGH", extreme=bar.high, bar_idx=bar_idx,
                        bar_time=bar.time, wick_ratio=wr, excess_pct=excess_pct,
                        close_back=close_back, confirmed=confirmed,
                        kill_zone="CONTINUATION", kz_weight=0.75,
                    )
        else:  # LOW
            excess = re_accum.low - bar.low
            if excess >= min_excess:
                wr = _wick_ratio(bar, "LOW")
                close_back = bar.close >= re_accum.low
                excess_pct = excess / rng

                confirmed = close_back
                if not close_back and idx + 1 < len(post):
                    nb = post[idx + 1][1]
                    confirmed = nb.close >= re_accum.low

                if (wr >= _WICK_RATIO or close_back) and confirmed:
                    return ManipulationEvent(
                        side="LOW", extreme=bar.low, bar_idx=bar_idx,
                        bar_time=bar.time, wick_ratio=wr, excess_pct=excess_pct,
                        close_back=close_back, confirmed=confirmed,
                        kill_zone="CONTINUATION", kz_weight=0.75,
                    )

    return None


def _build_continuation(
    bars: List[Bar],
    direction: str,
    re_accum: ReAccumulation,
    re_manip: ManipulationEvent,
    *,
    pip_size: float,
) -> Optional[ContinuationAMD]:
    """
    Build entry / SL / TP for a continuation leg, anchored to HTF liquidity.
    """
    rng = re_accum.width
    reasons: List[str] = ["continuation_setup_detected"]

    if direction == "BEAR":
        entry = re_manip.extreme if re_manip.close_back else re_accum.high
        sl    = re_manip.extreme + rng * _SL_BUFFER_PCT
        ref   = re_accum.low
    else:
        entry = re_manip.extreme if re_manip.close_back else re_accum.low
        sl    = re_manip.extreme - rng * _SL_BUFFER_PCT
        ref   = re_accum.high

    # Pull HTF liquidity below/above the re-accumulation boundary
    targets = find_htf_liquidity(bars, ref, direction, pip_size=pip_size)

    # Need at least three meaningful pools
    if len(targets) < 3:
        # Fall back to range-based projections if HTF liquidity is sparse
        if direction == "BEAR":
            tp1 = re_accum.low - rng * 1.0
            tp2 = re_accum.low - rng * 2.0
            tp3 = re_accum.low - rng * 3.5
        else:
            tp1 = re_accum.high + rng * 1.0
            tp2 = re_accum.high + rng * 2.0
            tp3 = re_accum.high + rng * 3.5
        chosen: List[LiquidityLevel] = []
        reasons.append("HTF_liquidity_sparse_used_range_projection")
    else:
        tp1 = targets[0].price
        tp2 = targets[1].price
        tp3 = targets[-1].price  # deepest available
        chosen = [targets[0], targets[1], targets[-1]]
        reasons.append(f"TP1={chosen[0].kind} TP2={chosen[1].kind} TP3={chosen[2].kind}")

    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk > 0 else None
    rr2 = abs(tp2 - entry) / risk if risk > 0 else None
    rr3 = abs(tp3 - entry) / risk if risk > 0 else None

    # Confidence: continuation setups score on alignment + liquidity quality
    score = 0.45
    reasons.append("base=0.45")
    if re_manip.close_back:
        score += 0.10; reasons.append("re_manip_close_back +0.10")
    if re_manip.wick_ratio >= 1.5:
        score += 0.08; reasons.append("re_manip_wick>=1.5 +0.08")
    if len(targets) >= 3:
        score += 0.10; reasons.append("HTF_liquidity_clean +0.10")
    if any(t.kind in ("PDL", "PDH", "PWL", "PWH") for t in chosen):
        score += 0.10; reasons.append("major_HTF_target +0.10")
    if rr3 and rr3 >= 5.0:
        score += 0.07; reasons.append("rr3>=5R +0.07")
    score = max(0.0, min(1.0, score))

    return ContinuationAMD(
        direction=direction,
        re_accum=re_accum,
        re_manip=re_manip,
        entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        rr_tp1=round(rr1, 2) if rr1 else None,
        rr_tp2=round(rr2, 2) if rr2 else None,
        rr_tp3=round(rr3, 2) if rr3 else None,
        targets=chosen,
        confidence=score,
        reasons=reasons,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def detect_amd(
    bars: List[Bar],
    pip_size: float = 0.0001,
    asian_start_h: int = _ASIAN_START_H,
    asian_end_h: int   = _ASIAN_END_H,
    min_confidence: float = 0.45,
) -> Optional[AMDResult]:
    """
    Run the full AMD detection pipeline on a list of M15 bars.

    Parameters
    ----------
    bars          : M15 (or M30) bars sorted by time ascending.
    pip_size      : Instrument pip size.  0.0001 = forex, 0.01 = gold (XAUUSD).
    asian_start_h : UTC hour the Asian session starts (default 0 = midnight).
    asian_end_h   : UTC hour the Asian session ends (default 7 = 07:00).
    min_confidence: Minimum score to return a DISTRIBUTION result.

    Returns
    -------
    AMDResult with phase=DISTRIBUTION if a valid setup is found,
    AMDResult with phase=ACCUMULATION if only the range was found,
    or None if the data is insufficient.
    """
    if len(bars) < 10:
        return None

    # Phase 1: Accumulation
    asian = _build_asian_range(bars, pip_size=pip_size)
    if asian is None:
        return None

    # Phase 2: Manipulation
    manip = _detect_manipulation(bars, asian, pip_size=pip_size)
    if manip is None:
        return AMDResult(
            phase=AMDPhase.ACCUMULATION,
            direction="NEUTRAL",
            asian_range=asian,
            manipulation=None,
            entry=None, sl=None, tp1=None, tp2=None, tp3=None,
            confidence=0.0,
            reasons=["Asian range identified; no manipulation detected yet"],
        )

    if not manip.confirmed:
        return AMDResult(
            phase=AMDPhase.MANIPULATION,
            direction="NEUTRAL",
            asian_range=asian,
            manipulation=manip,
            entry=None, sl=None, tp1=None, tp2=None, tp3=None,
            confidence=0.0,
            reasons=["Manipulation spike detected; awaiting close-back confirmation"],
        )

    # Phase 3: Distribution
    direction, entry, sl, tp1, tp2, tp3 = _build_levels(manip, asian)
    confidence, reasons = _score(manip, asian, bars)

    # R:R calculation
    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk > 0 else None
    rr2 = abs(tp2 - entry) / risk if risk > 0 else None
    rr3 = abs(tp3 - entry) / risk if risk > 0 else None

    if confidence < min_confidence:
        log.debug("amd: confidence %.2f below min %.2f — skip", confidence, min_confidence)
        return AMDResult(
            phase=AMDPhase.MANIPULATION,
            direction=direction,
            asian_range=asian,
            manipulation=manip,
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            confidence=confidence,
            reasons=reasons + [f"confidence={confidence:.2f} < min={min_confidence}"],
        )

    # Continuation pass: after the first distribution, look for re-accumulation
    # → re-manipulation → expanded second leg targeting deeper HTF liquidity.
    continuation: Optional[ContinuationAMD] = None
    htf_liq: List[LiquidityLevel] = []
    try:
        re_accum = _detect_re_accumulation(bars, manip.bar_idx + 1, direction)
        if re_accum is not None:
            re_manip = _detect_re_manipulation(bars, re_accum, direction)
            if re_manip is not None:
                continuation = _build_continuation(
                    bars, direction, re_accum, re_manip, pip_size=pip_size
                )

        # Always surface HTF liquidity from the manipulation reference point —
        # useful for the dashboard/Pine overlay even when no continuation fired.
        htf_ref = asian.low if direction == "BEAR" else asian.high
        htf_liq = find_htf_liquidity(bars, htf_ref, direction, pip_size=pip_size)
    except Exception as _e:
        log.debug("continuation pass skipped: %s", _e)

    return AMDResult(
        phase=AMDPhase.DISTRIBUTION,
        direction=direction,
        asian_range=asian,
        manipulation=manip,
        entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        confidence=confidence,
        reasons=reasons,
        entry_bar_time=manip.bar_time,
        rr_tp1=round(rr1, 2) if rr1 else None,
        rr_tp2=round(rr2, 2) if rr2 else None,
        rr_tp3=round(rr3, 2) if rr3 else None,
        continuation=continuation,
        htf_liquidity=htf_liq,
    )


def pip_size_for(symbol: str) -> float:
    """Return the standard pip size for a symbol."""
    sym = symbol.upper()
    if sym in ("XAUUSD", "GOLD"):
        return 0.01
    if sym in ("XAGUSD", "SILVER"):
        return 0.001
    if "JPY" in sym or sym in ("US30", "NAS100", "GBPJPY", "USDJPY"):
        return 0.01
    return 0.0001


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _make_test_bars() -> List[Bar]:
    """
    Synthetic M15 bars reproducing the two-stage AMD on the May 6-7 chart:
      - Day before: prior swing lows ~1.1700 and 1.1685 (HTF liquidity below)
      - Asian session: tight range 1.1720–1.1735 (00:00–07:00 UTC)
      - London Judas spike up to 1.1762, close back at 1.1728
      - Distribution-1: price delivers down through Asian low to ~1.1700
      - Re-accumulation: ~10 bars consolidating 1.1700–1.1712
      - Re-manipulation: pullback to 1.1716 (sweeps re-accum high), fails
      - Distribution-2: expansion down to ~1.1670 seeking PDL/equal-lows liquidity
    """
    base_ts = 1746576000.0   # 2026-05-07 00:00 UTC
    bar_secs = 15 * 60
    bars: List[Bar] = []

    # ── Prior day (96 M15 bars before our session) — establishes HTF liquidity ──
    prev_day_start = base_ts - 96 * bar_secs
    # Slow grind with two clear swing lows that will become HTF targets
    prev_prices = []
    for i in range(96):
        # Generate price action with swing lows around 1.1700 and 1.1685
        if i == 30:
            prev_prices.append((1.1715, 1.1720, 1.1700, 1.1710))   # swing low @ 1.1700
        elif i == 60:
            prev_prices.append((1.1710, 1.1715, 1.1685, 1.1695))   # swing low @ 1.1685
        elif i == 31 or i == 61:
            prev_prices.append((1.1710, 1.1722, 1.1708, 1.1718))
        else:
            o = 1.1718 + ((i % 7) - 3) * 0.0002
            prev_prices.append((o, o + 0.0006, o - 0.0006, o + 0.0001))

    for i, (o, h, l, c) in enumerate(prev_prices):
        bars.append(Bar(time=prev_day_start + i * bar_secs,
                        open=o, high=h, low=l, close=c))

    # ── Asian session: 28 bars, tight consolidation 1.1720–1.1735 ──
    prices_asian = [
        (1.1728, 1.1735, 1.1720, 1.1725),
        (1.1725, 1.1732, 1.1722, 1.1730),
        (1.1730, 1.1733, 1.1721, 1.1724),
        (1.1724, 1.1731, 1.1720, 1.1729),
        (1.1729, 1.1734, 1.1722, 1.1723),
        (1.1723, 1.1730, 1.1721, 1.1728),
        (1.1728, 1.1732, 1.1722, 1.1725),
        (1.1725, 1.1733, 1.1720, 1.1730),
        (1.1730, 1.1734, 1.1721, 1.1724),
        (1.1724, 1.1731, 1.1720, 1.1729),
        (1.1729, 1.1733, 1.1722, 1.1723),
        (1.1723, 1.1731, 1.1721, 1.1729),
    ] + [(1.1727, 1.1733, 1.1721, 1.1726)] * 16

    for i, (o, h, l, c) in enumerate(prices_asian):
        bars.append(Bar(time=base_ts + i * bar_secs,
                        open=o, high=h, low=l, close=c))

    london_base = base_ts + 28 * bar_secs   # 07:00 UTC

    # ── London Judas spike up + close-back ──
    bars.append(Bar(time=london_base,
                    open=1.1730, high=1.1762, low=1.1726, close=1.1728))
    bars.append(Bar(time=london_base + bar_secs,
                    open=1.1728, high=1.1731, low=1.1718, close=1.1720))

    # ── Distribution-1: drop from Asian range down to ~1.1700 ──
    dist1_prices = [
        (1.1720, 1.1722, 1.1710, 1.1712),
        (1.1712, 1.1715, 1.1704, 1.1706),
        (1.1706, 1.1708, 1.1700, 1.1702),
    ]
    for i, (o, h, l, c) in enumerate(dist1_prices):
        bars.append(Bar(time=london_base + (2 + i) * bar_secs,
                        open=o, high=h, low=l, close=c))

    # ── Re-accumulation: 10 bars consolidating 1.1700–1.1712 ──
    reaccum_start = london_base + 5 * bar_secs
    reaccum_prices = [
        (1.1702, 1.1710, 1.1700, 1.1708),
        (1.1708, 1.1712, 1.1702, 1.1704),
        (1.1704, 1.1709, 1.1701, 1.1707),
        (1.1707, 1.1711, 1.1703, 1.1705),
        (1.1705, 1.1710, 1.1700, 1.1708),
        (1.1708, 1.1712, 1.1704, 1.1706),
        (1.1706, 1.1710, 1.1702, 1.1709),
        (1.1709, 1.1712, 1.1703, 1.1705),
        (1.1705, 1.1709, 1.1701, 1.1707),
        (1.1707, 1.1711, 1.1703, 1.1706),
    ]
    for i, (o, h, l, c) in enumerate(reaccum_prices):
        bars.append(Bar(time=reaccum_start + i * bar_secs,
                        open=o, high=h, low=l, close=c))

    re_accum_end = reaccum_start + len(reaccum_prices) * bar_secs

    # ── Re-manipulation: clear spike well ABOVE re-accum high (1.1712),
    #    closes back inside the range — counter-trend stop hunt ──
    bars.append(Bar(time=re_accum_end,
                    open=1.1706, high=1.1730, low=1.1704, close=1.1708))
    bars.append(Bar(time=re_accum_end + bar_secs,
                    open=1.1708, high=1.1710, low=1.1700, close=1.1702))

    # ── Distribution-2: bigger expansion down toward HTF liquidity ──
    dist2_prices = [
        (1.1702, 1.1704, 1.1690, 1.1692),
        (1.1692, 1.1694, 1.1680, 1.1683),
        (1.1683, 1.1685, 1.1672, 1.1675),
        (1.1675, 1.1678, 1.1668, 1.1670),
    ]
    for i, (o, h, l, c) in enumerate(dist2_prices):
        bars.append(Bar(time=re_accum_end + (2 + i) * bar_secs,
                        open=o, high=h, low=l, close=c))

    return bars


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG,
                        format="%(levelname)s %(name)s %(message)s")

    bars = _make_test_bars()
    print(f"Testing with {len(bars)} synthetic M15 bars...\n")

    result = detect_amd(bars, pip_size=0.0001)

    if result is None:
        print("FAIL: detect_amd returned None")
    else:
        print(f"Phase      : {result.phase}")
        print(f"Direction  : {result.direction}")
        print(f"Confidence : {result.confidence:.2f}")
        if result.asian_range:
            ar = result.asian_range
            print(f"Asian range: {ar.low:.5f} – {ar.high:.5f}  "
                  f"(width={ar.width:.5f}, bars={ar.bar_count})")
        if result.manipulation:
            m = result.manipulation
            print(f"Manipulation: side={m.side}  extreme={m.extreme:.5f}  "
                  f"wick_ratio={m.wick_ratio:.2f}  close_back={m.close_back}  "
                  f"kill_zone={m.kill_zone}")
        if result.entry:
            print(f"Entry : {result.entry:.5f}")
            print(f"SL    : {result.sl:.5f}")
            print(f"TP1   : {result.tp1:.5f}  (R:R {result.rr_tp1})")
            print(f"TP2   : {result.tp2:.5f}  (R:R {result.rr_tp2})")
            print(f"TP3   : {result.tp3:.5f}  (R:R {result.rr_tp3})")
        print("\nReasons:")
        for r in result.reasons:
            print(f"  {r}")

        if result.htf_liquidity:
            print(f"\nHTF liquidity ({len(result.htf_liquidity)} pools below):")
            for lv in result.htf_liquidity:
                print(f"  {lv.kind:10s} @ {lv.price:.5f}  (dist={lv.distance:.5f})")

        if result.continuation:
            c = result.continuation
            print(f"\n── CONTINUATION (Stage 2) ──")
            print(f"Direction  : {c.direction}")
            print(f"Confidence : {c.confidence:.2f}")
            print(f"Re-accum   : {c.re_accum.low:.5f} – {c.re_accum.high:.5f}  "
                  f"({c.re_accum.bar_count} bars)")
            print(f"Re-manip   : side={c.re_manip.side}  extreme={c.re_manip.extreme:.5f}")
            print(f"Entry  : {c.entry:.5f}")
            print(f"SL     : {c.sl:.5f}")
            print(f"TP1    : {c.tp1:.5f}  (R:R {c.rr_tp1})")
            print(f"TP2    : {c.tp2:.5f}  (R:R {c.rr_tp2})")
            print(f"TP3    : {c.tp3:.5f}  (R:R {c.rr_tp3})")
            if c.targets:
                print("Targets:")
                for t in c.targets:
                    print(f"  {t.kind:10s} @ {t.price:.5f}")
            print("Continuation reasons:")
            for r in c.reasons:
                print(f"  {r}")

    # Verify primary AMD detected
    assert result is not None, "should find result"
    assert result.phase == AMDPhase.DISTRIBUTION, f"expected DISTRIBUTION, got {result.phase}"
    assert result.direction == "BEAR", f"expected BEAR, got {result.direction}"
    assert result.confidence >= 0.45, f"confidence {result.confidence:.2f} too low"
    assert result.rr_tp1 is not None and result.rr_tp1 >= 0.5, \
        f"TP1 RR {result.rr_tp1} seems wrong"

    # Verify continuation detected (the second stage)
    assert result.continuation is not None, "expected continuation AMD to be detected"
    c = result.continuation
    assert c.direction == "BEAR", f"continuation direction wrong: {c.direction}"
    assert c.tp3 < c.tp1 < c.entry, \
        f"continuation TP cascade wrong: {c.tp3} < {c.tp1} < {c.entry}"
    assert c.rr_tp3 is not None and c.rr_tp3 >= 1.0, \
        f"continuation TP3 RR too low: {c.rr_tp3}"

    # Verify HTF liquidity discovered
    assert len(result.htf_liquidity) > 0, "expected HTF liquidity pools"

    print("\nAll assertions passed (primary + continuation + HTF liquidity).")
