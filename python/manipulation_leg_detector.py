"""
manipulation_leg_detector.py — ICT Manipulation Leg Detection Engine

Identifies the displacement candles that form "clear breakouts that sweep liquidity"
and anchor STDV wick-to-wick. This is the foundational engine for ALL precision
entries — without a confirmed manipulation leg, there is no valid OTE/STDV level.

ICT Concept Mapping
-------------------
Manipulation Leg  = The candle or sequence that BREAKS a prior structural extreme
                    (swing high/low, equal high/low, or Asian range boundary) and
                    CLOSES back inside or shows clear rejection (wick > body).

Sweep            = Price takes out a liquidity pool (EQH/EQL, PDH/PDL, Asian extreme)
                    then reverses. The wick beyond the pool = the manipulation.

Displacement     = The move AFTER the sweep that confirms the reversal direction.
                    This is NOT the manipulation leg — it is the leg we trade INTO.

Leg Types Detected
------------------
  1. London Killzone Judas  — 07:00-10:00 UTC sweep of Asian range
  2. NY Open Judas          — 12:00-15:00 UTC sweep of London range / PDH/PDL
  3. Silver Bullet Sweep    — 13:00-17:00 UTC sweep of session midpoint
  4. Final Leg Before Move  — last aggressive push before reversal (5+ ATR wick)
  5. Liquidity Sweep Leg    — sweep of EQH/EQL with immediate rejection
  6. AMD Manipulation       — Asian range boundary break + close back inside

Detection Requirements (ALL must be true for a valid manipulation leg):
  A. Price must BREAK a known liquidity level (swing extreme, EQH/EQL, PDH/PDL)
  B. The break must show REJECTION character (wick/body >= 1.3 OR close back inside)
  C. The break must occur inside a kill zone (London, NY, Silver Bullet)
  D. At least ONE confirming candle must close back inside the swept range
  E. The leg must exceed MIN_MANIP_PCT of ATR(14) in its wick beyond the boundary

Output: ManipulationLeg dataclass with full metadata for STDV anchoring.

Run `python manipulation_leg_detector.py` for self-test.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_MIN_WICK_BODY_RATIO = 1.0      # rejection character: wick must be 1.0x body (was 1.3, too strict for XAUUSD)
_MIN_MANIP_PCT_OF_ATR = 0.25    # wick beyond boundary must be >= 25% of ATR(14) (was 0.35)
_MAX_MANIP_BARS = 5             # manipulation sequence can span up to 5 bars (was 3, ICT legs often multi-bar)
_MIN_SWEEP_BARS = 2             # need at least 2 bars to confirm close-back
_KILL_ZONES_UTC = [
    (7, 10, "LONDON_OPEN"),     # 07:00-10:00 UTC
    (12, 15, "NY_OPEN"),        # 12:00-15:00 UTC
    (13, 17, "SILVER_BULLET"),  # 13:00-17:00 UTC
    (7, 12, "EUROPEAN"),        # 07:00-12:00 UTC
]


# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    """Minimal bar for manipulation detection."""
    time: float        # unix timestamp
    o: float
    h: float
    l: float
    c: float

    @property
    def bullish(self) -> bool:
        return self.c > self.o

    @property
    def bearish(self) -> bool:
        return self.c < self.o

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def range(self) -> float:
        return self.h - self.l

    @property
    def upper_wick(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower_wick(self) -> float:
        return min(self.o, self.c) - self.l


@dataclass
class LiquidityPool:
    """A structural extreme that acts as a liquidity magnet."""
    price:      float
    kind:       str     # "EQH" | "EQL" | "SWING_HIGH" | "SWING_LOW" | "PDH" | "PDL" | "ASIAN_HIGH" | "ASIAN_LOW"
    bar_idx:    int
    touch_count: int = 1


@dataclass
class ManipulationLeg:
    """A confirmed manipulation leg ready for STDV anchoring."""
    detected:       bool = False
    direction:      str = ""          # "BULL" | "BEAR" — the direction the DISPLACEMENT will go
    leg_type:       str = ""          # "JUDAS_HIGH" | "JUDAS_LOW" | "FINAL_LEG" | "LIQUIDITY_SWEEP" | "AMD_MANIP"

    # Wick-to-wick extremes of the manipulation sequence
    wick_high:      float = 0.0
    wick_low:       float = 0.0

    # The swept liquidity level
    swept_level:    float = 0.0
    swept_kind:     str = ""          # what kind of pool was swept

    # Where price closed after the sweep (the entry reference)
    close_after_sweep: float = 0.0

    # Indices in the bar array
    start_idx:      int = 0           # first bar of manipulation sequence
    end_idx:        int = 0           # last bar of manipulation sequence (close-back confirmation)

    # Quality metrics
    wick_body_ratio: float = 0.0
    excess_pips:     float = 0.0      # how far beyond the liquidity level price went
    atr_at_time:     float = 0.0
    kill_zone:       str = ""         # which session window

    # Structural context
    prior_swing_idx: int = 0          # the swing extreme that was swept
    confirmation_bar_idx: int = 0     # bar that confirmed close-back

    reasons:        List[str] = field(default_factory=list)

    @property
    def wick_range(self) -> float:
        """Wick-to-wick range — this is what STDV anchors to."""
        return self.wick_high - self.wick_low

    @property
    def is_high_quality(self) -> bool:
        """True if this manipulation leg has strong rejection character."""
        return self.wick_body_ratio >= 2.0 and self.excess_pips >= self.atr_at_time * 0.5


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _bar_hour_utc(bar: Bar) -> int:
    try:
        return datetime.fromtimestamp(bar.time, tz=timezone.utc).hour
    except Exception:
        return -1


def _in_kill_zone(hour: int) -> Tuple[bool, str]:
    for start, end, label in _KILL_ZONES_UTC:
        if start <= hour < end:
            return True, label
    return False, ""


def _atr(bars: List[Bar], period: int = 14, end_idx: Optional[int] = None) -> float:
    """Wilder ATR up to end_idx (default: all bars)."""
    if len(bars) < 2:
        return 0.0
    end = end_idx if end_idx is not None else len(bars)
    start = max(1, end - period)
    trs = []
    for i in range(start, end):
        b, p = bars[i], bars[i - 1]
        tr = max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _find_swing_highs(bars: List[Bar], lookback: int = 2,
                      start: int = 0, end: Optional[int] = None) -> List[Tuple[int, float]]:
    """Swing highs in [start, end)."""
    end = end or len(bars)
    out = []
    for i in range(max(start, lookback), min(end, len(bars)) - lookback):
        val = bars[i].h
        if all(bars[i - j].h < val for j in range(1, lookback + 1)) and \
           all(bars[i + j].h < val for j in range(1, lookback + 1)):
            out.append((i, val))
    return out


def _find_swing_lows(bars: List[Bar], lookback: int = 2,
                     start: int = 0, end: Optional[int] = None) -> List[Tuple[int, float]]:
    """Swing lows in [start, end)."""
    end = end or len(bars)
    out = []
    for i in range(max(start, lookback), min(end, len(bars)) - lookback):
        val = bars[i].l
        if all(bars[i - j].l > val for j in range(1, lookback + 1)) and \
           all(bars[i + j].l > val for j in range(1, lookback + 1)):
            out.append((i, val))
    return out


def _find_equal_levels(bars: List[Bar], tolerance: float,
                       max_bars: int = 100) -> Tuple[List[float], List[float]]:
    """
    Find equal highs and equal lows in the most recent `max_bars`.
    Returns (eq_highs, eq_lows) as deduplicated price levels.
    A level qualifies if touched 3+ times within tolerance.
    """
    recent = bars[-max_bars:] if len(bars) > max_bars else bars
    if len(recent) < 10:
        return [], []

    # Cluster highs
    highs: dict = {}
    for b in recent:
        key = round(b.h / tolerance)
        highs.setdefault(key, []).append(b.h)

    eq_highs = []
    for key, touches in highs.items():
        if len(touches) >= 3:
            avg = sum(touches) / len(touches)
            if all(abs(t - avg) <= tolerance for t in touches):
                eq_highs.append(avg)

    # Cluster lows
    lows: dict = {}
    for b in recent:
        key = round(b.l / tolerance)
        lows.setdefault(key, []).append(b.l)

    eq_lows = []
    for key, touches in lows.items():
        if len(touches) >= 3:
            avg = sum(touches) / len(touches)
            if all(abs(t - avg) <= tolerance for t in touches):
                eq_lows.append(avg)

    return sorted(set(eq_highs), reverse=True), sorted(set(eq_lows))


def _find_asian_range(bars: List[Bar], pip_size: float = 0.01) -> Optional[Tuple[float, float, int, int]]:
    """
    Find the most recent Asian session range (00:00-07:00 UTC).
    Returns (high, low, start_idx, end_idx) or None.
    """
    asian_bars = [(i, b) for i, b in enumerate(bars) if 0 <= _bar_hour_utc(b) < 7]
    if len(asian_bars) < 3:
        return None

    # Group into contiguous sessions by day
    sessions: List[List[Tuple[int, Bar]]] = []
    current: List[Tuple[int, Bar]] = [asian_bars[0]]
    for item in asian_bars[1:]:
        if item[1].time - current[-1][1].time > 14 * 3600:
            sessions.append(current)
            current = [item]
        else:
            current.append(item)
    sessions.append(current)

    session = sessions[-1]  # most recent
    hi = max(b.h for _, b in session)
    lo = min(b.l for _, b in session)
    start_idx = session[0][0]
    end_idx = session[-1][0]
    min_range = 5.0 * pip_size
    if hi - lo < min_range:
        return None
    return hi, lo, start_idx, end_idx


def _find_pdh_pdl(bars: List[Bar]) -> Tuple[Optional[float], Optional[float]]:
    """Previous day's high/low from daily grouped bars."""
    if len(bars) < 24:
        return None, None
    # Group by calendar day
    days: dict = {}
    for b in bars:
        try:
            day = datetime.fromtimestamp(b.time, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            continue
        days.setdefault(day, []).append(b)

    if len(days) < 2:
        return None, None

    day_keys = sorted(days.keys())
    yesterday = days[day_keys[-2]]
    pdh = max(b.h for b in yesterday)
    pdl = min(b.l for b in yesterday)
    return pdh, pdl


# ──────────────────────────────────────────────────────────────────────────────
# Core: Detect manipulation leg at a specific index
# ──────────────────────────────────────────────────────────────────────────────

def _check_sweep_rejection(bars: List[Bar], idx: int,
                           level: float, level_kind: str,
                           atr: float, pip_size: float) -> Optional[ManipulationLeg]:
    """
    Check if bars[idx] (and up to _MAX_MANIP_BARS following) form a sweep+rejection
    of `level`.

    For a HIGH sweep (level is a high, e.g., EQH, PDH):
      - bar[idx].h must exceed level
      - Either bar[idx] or a subsequent bar within _MAX_MANIP_BARS must close <= level
      - Wick/body ratio >= _MIN_WICK_BODY_RATIO
      - Excess beyond level >= _MIN_MANIP_PCT_OF_ATR * atr

    For a LOW sweep (level is a low, e.g., EQL, PDL):
      - bar[idx].l must be below level
      - Either bar[idx] or subsequent bar must close >= level
      - Wick/body ratio >= _MIN_WICK_BODY_RATIO
      - Excess beyond level >= _MIN_MANIP_PCT_OF_ATR * atr

    Returns ManipulationLeg if confirmed, None otherwise.
    """
    if idx >= len(bars) or atr <= 0:
        return None

    b0 = bars[idx]
    hour = _bar_hour_utc(b0)
    in_kz, kz_label = _in_kill_zone(hour)

    # Determine sweep direction
    if b0.h > level:   # sweeping a high
        sweep_direction = "HIGH"
        excess = b0.h - level
        wick = b0.upper_wick
        body = max(1e-12, b0.body)
        wb_ratio = wick / body
    elif b0.l < level:  # sweeping a low
        sweep_direction = "LOW"
        excess = level - b0.l
        wick = b0.lower_wick
        body = max(1e-12, b0.body)
        wb_ratio = wick / body
    else:
        return None  # no sweep of this level

    # Quality gate 1: excess must be meaningful
    if excess < _MIN_MANIP_PCT_OF_ATR * atr:
        return None

    # Quality gate 2: rejection character
    if wb_ratio < _MIN_WICK_BODY_RATIO:
        return None

    # Look for close-back confirmation within _MAX_MANIP_BARS
    close_back_idx = None
    for j in range(idx, min(idx + _MAX_MANIP_BARS, len(bars))):
        bj = bars[j]
        if sweep_direction == "HIGH" and bj.c <= level:
            close_back_idx = j
            break
        if sweep_direction == "LOW" and bj.c >= level:
            close_back_idx = j
            break

    if close_back_idx is None:
        return None  # no close-back confirmation

    # Build the manipulation leg
    wick_high = max(b.h for b in bars[idx:close_back_idx + 1])
    wick_low = min(b.l for b in bars[idx:close_back_idx + 1])

    if sweep_direction == "HIGH":
        leg = ManipulationLeg(
            detected=True,
            direction="BEAR",  # swept high → price will distribute down
            leg_type="JUDAS_HIGH" if in_kz else "LIQUIDITY_SWEEP",
            wick_high=wick_high,
            wick_low=wick_low,
            swept_level=level,
            swept_kind=level_kind,
            close_after_sweep=bars[close_back_idx].c,
            start_idx=idx,
            end_idx=close_back_idx,
            wick_body_ratio=wb_ratio,
            excess_pips=excess,
            atr_at_time=atr,
            kill_zone=kz_label if in_kz else "",
            confirmation_bar_idx=close_back_idx,
            reasons=[
                f"Swept {level_kind} @ {level:.5f} with wick to {wick_high:.5f}",
                f"Close-back confirmed on bar {close_back_idx} @ {bars[close_back_idx].c:.5f}",
                f"Wick/body ratio {wb_ratio:.2f} (min {_MIN_WICK_BODY_RATIO})",
                f"Excess {excess:.5f} >= {(_MIN_MANIP_PCT_OF_ATR * atr):.5f} ATR",
            ],
        )
    else:
        leg = ManipulationLeg(
            detected=True,
            direction="BULL",  # swept low → price will distribute up
            leg_type="JUDAS_LOW" if in_kz else "LIQUIDITY_SWEEP",
            wick_high=wick_high,
            wick_low=wick_low,
            swept_level=level,
            swept_kind=level_kind,
            close_after_sweep=bars[close_back_idx].c,
            start_idx=idx,
            end_idx=close_back_idx,
            wick_body_ratio=wb_ratio,
            excess_pips=excess,
            atr_at_time=atr,
            kill_zone=kz_label if in_kz else "",
            confirmation_bar_idx=close_back_idx,
            reasons=[
                f"Swept {level_kind} @ {level:.5f} with wick to {wick_low:.5f}",
                f"Close-back confirmed on bar {close_back_idx} @ {bars[close_back_idx].c:.5f}",
                f"Wick/body ratio {wb_ratio:.2f} (min {_MIN_WICK_BODY_RATIO})",
                f"Excess {excess:.5f} >= {(_MIN_MANIP_PCT_OF_ATR * atr):.5f} ATR",
            ],
        )

    if in_kz:
        leg.reasons.append(f"Kill zone: {kz_label}")
    else:
        leg.reasons.append("Outside kill zone — lower confidence")

    return leg


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def detect_manipulation_legs(bars: List[Bar],
                             pip_size: float = 0.01,
                             min_recent_bars: int = 50) -> List[ManipulationLeg]:
    """
    Scan the most recent `min_recent_bars` for ALL confirmed manipulation legs.

    Returns list sorted by recency (most recent first). Each leg is independently
    confirmed and includes full metadata for STDV anchoring.
    """
    if len(bars) < min_recent_bars:
        return []

    recent = bars[-min_recent_bars:]
    atr_val = _atr(bars, period=14)
    if atr_val <= 0:
        return []

    # Build liquidity pools
    pools: List[LiquidityPool] = []

    # 1. Swing highs/lows
    swings_high = _find_swing_highs(bars, lookback=2,
                                    start=len(bars) - min_recent_bars)
    swings_low = _find_swing_lows(bars, lookback=2,
                                  start=len(bars) - min_recent_bars)
    for idx, price in swings_high:
        pools.append(LiquidityPool(price, "SWING_HIGH", idx))
    for idx, price in swings_low:
        pools.append(LiquidityPool(price, "SWING_LOW", idx))

    # 2. Equal highs/lows
    tol = atr_val * 0.15  # tolerance for equal levels
    eq_highs, eq_lows = _find_equal_levels(bars, tolerance=tol, max_bars=min_recent_bars)
    for price in eq_highs:
        pools.append(LiquidityPool(price, "EQH", len(bars) - 1))
    for price in eq_lows:
        pools.append(LiquidityPool(price, "EQL", len(bars) - 1))

    # 3. Asian range
    asian = _find_asian_range(bars, pip_size=pip_size)
    if asian:
        ahi, alo, _, _ = asian
        pools.append(LiquidityPool(ahi, "ASIAN_HIGH", len(bars) - 1))
        pools.append(LiquidityPool(alo, "ASIAN_LOW", len(bars) - 1))

    # 4. Previous day H/L
    pdh, pdl = _find_pdh_pdl(bars)
    if pdh:
        pools.append(LiquidityPool(pdh, "PDH", len(bars) - 1))
    if pdl:
        pools.append(LiquidityPool(pdl, "PDL", len(bars) - 1))

    # Scan for sweeps of each pool in the most recent bars
    found: List[ManipulationLeg] = []
    checked_levels = set()  # avoid duplicate detections at same price

    for pool in sorted(pools, key=lambda p: p.bar_idx, reverse=True):
        key = round(pool.price / (pip_size * 0.1))
        if key in checked_levels:
            continue
        checked_levels.add(key)

        # Scan recent bars for a sweep of this level
        # Use min_recent_bars as scan range, not hardcoded 20
        scan_start = max(len(bars) - min_recent_bars, 0)
        for i in range(scan_start, len(bars)):
            leg = _check_sweep_rejection(bars, i, pool.price, pool.kind,
                                         atr_val, pip_size)
            if leg and leg.detected:
                # Avoid duplicates at same start_idx
                if not any(f.start_idx == leg.start_idx for f in found):
                    found.append(leg)

    # Sort by recency (most recent first)
    found.sort(key=lambda x: x.start_idx, reverse=True)
    return found


def get_primary_manipulation_leg(bars: List[Bar],
                                 bias_direction: str = "",
                                 pip_size: float = 0.01,
                                 min_recent_bars: int = 50) -> ManipulationLeg:
    """
    Return the SINGLE best manipulation leg for the current context.

    If `bias_direction` is provided, prefer legs that align with the expected
    displacement direction (e.g., BULL bias → prefer JUDAS_LOW / ASIAN_LOW sweeps).
    Otherwise return the most recent high-quality leg.
    """
    legs = detect_manipulation_legs(bars, pip_size, min_recent_bars)
    if not legs:
        return ManipulationLeg(detected=False, reasons=["no manipulation leg detected"])

    # Score each leg
    def score(leg: ManipulationLeg) -> float:
        s = 0.0
        # Recency bonus
        s += max(0, 20 - (len(bars) - leg.start_idx)) * 0.5
        # Quality bonus
        if leg.is_high_quality:
            s += 10.0
        # Kill zone bonus
        if leg.kill_zone:
            s += 5.0
        # Bias alignment bonus
        if bias_direction and leg.direction == bias_direction:
            s += 8.0
        return s

    legs.sort(key=score, reverse=True)
    best = legs[0]
    best.reasons.insert(0, f"Selected as primary leg (score={score(best):.1f}, {len(legs)} candidates)")
    return best


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _make_fixture() -> List[Bar]:
    """
    Build a synthetic bar sequence with a clear manipulation leg:
    - Asian range 00:00-07:00 UTC: 3300.00 - 3305.00
    - London 07:00 UTC: sweep to 3308.50 (Judas high), close back at 3304.00
    - Distribution: moves down to 3295.00
    """
    base_time = 1704067200  # 2024-01-01 00:00:00 UTC
    bars = []
    # Asian session: flat consolidation 3300-3305
    for i in range(28):  # 7 hours of 15-min bars
        h = (i // 4) % 24
        if 0 <= h < 7:
            bars.append(Bar(
                time=base_time + i * 900,
                o=3302.0, h=3305.0, l=3300.0, c=3303.0,
            ))
        else:
            # Pre/Post Asian padding
            bars.append(Bar(
                time=base_time + i * 900,
                o=3303.0, h=3304.0, l=3302.0, c=3303.0,
            ))

    # London open: Judas sweep above Asian high
    bars.append(Bar(
        time=base_time + 28 * 900,
        o=3305.0, h=3308.50, l=3304.0, c=3304.0,  # swept 3305, closed back inside
    ))
    # Confirmation bar
    bars.append(Bar(
        time=base_time + 29 * 900,
        o=3304.0, h=3304.5, l=3302.0, c=3302.5,
    ))
    # Distribution down
    for i in range(5):
        bars.append(Bar(
            time=base_time + (30 + i) * 900,
            o=3302.5 - i * 1.5,
            h=3303.0 - i * 1.5,
            l=3301.0 - i * 1.5,
            c=3301.5 - i * 1.5,
        ))

    return bars


def _self_test() -> None:
    bars = _make_fixture()
    legs = detect_manipulation_legs(bars, pip_size=0.01, min_recent_bars=25)
    print(f"[TEST] Found {len(legs)} manipulation leg(s)")
    for leg in legs:
        print(f"  Leg: {leg.leg_type} → displacement {leg.direction}")
        print(f"       Wick range: {leg.wick_low:.2f} - {leg.wick_high:.2f}")
        print(f"       Swept {leg.swept_kind} @ {leg.swept_level:.2f}")
        print(f"       Quality: wick/body={leg.wick_body_ratio:.2f}, excess={leg.excess_pips:.2f}")
        print(f"       Kill zone: {leg.kill_zone or 'none'}")
        for r in leg.reasons:
            print(f"       · {r}")

    primary = get_primary_manipulation_leg(bars, bias_direction="BEAR",
                                           pip_size=0.01, min_recent_bars=25)
    assert primary.detected, "Primary leg must be detected"
    assert primary.direction == "BEAR", f"Expected BEAR displacement, got {primary.direction}"
    assert primary.leg_type == "JUDAS_HIGH", f"Expected JUDAS_HIGH, got {primary.leg_type}"
    print(f"\n[OK] Primary leg validated: {primary.leg_type} → {primary.direction}")
    print(f"     Wick-to-wick range for STDV: {primary.wick_range:.2f}")


if __name__ == "__main__":
    _self_test()
