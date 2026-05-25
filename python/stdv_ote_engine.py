"""
stdv_ote_engine.py — STDV / OTE Confluence Level Calculator

Anchors standard-deviation projections to confirmed manipulation legs,
producing the exact price levels the user trades from manually:

  CE(0.5)         — midpoint of manipulation leg wick-to-wick range
  OTE(-0.705)     — 0.705 stdv from CE toward the displacement side
  Reaccum(-1)     — 1 stdv from CE
  Reversal(-2)    — 2 stdv from CE
  Max Expansion   — 3/4/5 stdv from CE

PLUS Fibonacci retracement levels of the manipulation leg:
  0.50, 0.63, 0.65, 0.705, 0.79, 0.886, 1.0

These are "hints" — the confluence engine checks if price is at/near a level
AND has OB/FVG/BOS/CHoCH confluence. NEVER enters solely on a fib/STDV level.

Usage:
    from stdv_ote_engine import STDVOTEProfile, compute_profile, nearest_level
    from manipulation_leg_detector import get_primary_manipulation_leg

    leg = get_primary_manipulation_leg(bars)
    profile = compute_profile(leg, bars)
    level, dist = nearest_level(profile, current_price, direction="BULL")

Run `python stdv_ote_engine.py` for self-test.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from manipulation_leg_detector import ManipulationLeg, Bar

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Proximity tolerance: price must be within this fraction of ATR to count as "at" a level
_LEVEL_PROXIMITY_ATR_FRAC = 0.25

# OTE zone boundaries (what counts as "in the OTE zone")
OTE_ZONE_MIN = 0.50
OTE_ZONE_MAX = 0.886

# STDV computation: stdv = wick_range / STDV_DIVISOR (approximates 1-sigma)
_STDV_DIVISOR = 4.0

# Key STDV multipliers from user's playbook
_STDV_MULTIPLIERS = {
    "CE": 0.0,
    "OTE": 0.705,
    "OTE_0.63": 0.63,
    "OTE_0.65": 0.65,
    "OTE_0.79": 0.79,
    "OTE_0.886": 0.886,
    "Reaccum": 1.0,
    "Reversal": 2.0,
    "MaxExp3": 3.0,
    "MaxExp4": 4.0,
    "MaxExp5": 5.0,
}


# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class STDVLevel:
    """A single projected price level."""
    name:       str
    price:      float
    multiplier: float   # raw multiplier used (stdv or fib ratio)
    level_type: str     # "STDV" | "FIB"


@dataclass
class STDVOTEProfile:
    """Complete STDV/OTE level set for a manipulation leg."""
    leg: ManipulationLeg

    # Core reference prices
    wick_high:  float
    wick_low:   float
    wick_range: float
    ce:         float           # center (0.5) = midpoint of wick range
    stdv:       float           # 1 standard deviation unit

    # STDV-projected levels (direction-aware: + for BULL, - for BEAR from CE)
    stdv_levels: List[STDVLevel] = field(default_factory=list)

    # Fibonacci retracement levels of the wick range
    fib_levels:  List[STDVLevel] = field(default_factory=list)

    # The OTE zone (between CE and the deepest OTE level in displacement direction)
    ote_zone_top:    float = 0.0
    ote_zone_bottom: float = 0.0

    # ATR at time of computation (for proximity checks)
    atr: float = 0.0

    reasons: List[str] = field(default_factory=list)

    @property
    def all_levels(self) -> List[STDVLevel]:
        return self.stdv_levels + self.fib_levels

    def level_by_name(self, name: str) -> Optional[STDVLevel]:
        for lv in self.all_levels:
            if lv.name == name:
                return lv
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _atr(bars, period: int = 14, end_idx = None) -> float:
    """Wilder ATR up to end_idx. Handles both smc_engine.Bar (.high/.low) and MLD Bar (.h/.l)."""
    if not bars or len(bars) < 2:
        return 0.0
    def _h(b):
        return getattr(b, 'h', getattr(b, 'high', 0))
    def _l(b):
        return getattr(b, 'l', getattr(b, 'low', 0))
    def _c(b):
        return getattr(b, 'c', getattr(b, 'close', 0))
    end = end_idx if end_idx is not None else len(bars)
    start = max(1, end - period)
    trs = []
    for i in range(start, end):
        b, p = bars[i], bars[i - 1]
        tr = max(_h(b) - _l(b), abs(_h(b) - _c(p)), abs(_l(b) - _c(p)))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _direction_sign(direction: str) -> int:
    """+1 for BULL (levels above CE), -1 for BEAR (levels below CE)."""
    return 1 if direction == "BULL" else -1


def _compute_stdv_levels(ce: float, stdv: float, direction: str) -> List[STDVLevel]:
    """Project STDV levels from CE in the displacement direction."""
    sign = _direction_sign(direction)
    levels = []
    for name, mult in _STDV_MULTIPLIERS.items():
        price = ce + (sign * mult * stdv)
        levels.append(STDVLevel(name=name, price=round(price, 5),
                                multiplier=mult, level_type="STDV"))
    return levels


def _compute_fib_levels(wick_low: float, wick_high: float, direction: str) -> List[STDVLevel]:
    """
    Compute Fibonacci retracement levels of the manipulation leg.

    For BULL (displacement UP from a swept LOW):
      Base = wick_low, Range = wick_high - wick_low
      Levels measured UPWARD from base.
      0.0 = wick_low (the manipulation extreme)
      0.5 = midpoint
      0.705 = 70.5% of the range up from base
      These are entry zones for when price pulls back down toward the base.

    For BEAR (displacement DOWN from a swept HIGH):
      Base = wick_high, Range = wick_high - wick_low
      Levels measured DOWNWARD from base.
      0.0 = wick_high (the manipulation extreme)
      0.5 = midpoint
      0.705 = 70.5% of the range down from base
    """
    rng = wick_high - wick_low
    if rng <= 0:
        return []

    ratios = [0.0, 0.50, 0.63, 0.65, 0.705, 0.79, 0.886, 1.0]
    levels = []

    if direction == "BULL":
        base = wick_low
        for r in ratios:
            price = base + r * rng
            name = "WICK_LOW" if r == 0.0 else ("CE" if r == 0.5 else f"FIB_{r:.3f}")
            levels.append(STDVLevel(name=name, price=round(price, 5),
                                    multiplier=r, level_type="FIB"))
    else:
        base = wick_high
        for r in ratios:
            price = base - r * rng
            name = "WICK_HIGH" if r == 0.0 else ("CE" if r == 0.5 else f"FIB_{r:.3f}")
            levels.append(STDVLevel(name=name, price=round(price, 5),
                                    multiplier=r, level_type="FIB"))

    return levels


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def compute_profile(leg: ManipulationLeg,
                    bars: List[Bar],
                    atr_period: int = 14) -> STDVOTEProfile:
    """
    Compute the complete STDV/OTE profile for a manipulation leg.

    The profile includes:
      - CE(0.5): midpoint of wick-to-wick range
      - STDV projections: OTE(-0.705), Reaccum(-1), Reversal(-2), MaxExp(-3/-4/-5)
      - Fib retracements: 0.50, 0.63, 0.65, 0.705, 0.79, 0.886 of the range
      - OTE zone boundaries for quick "is price in OTE zone?" checks
    """
    if not leg.detected:
        return STDVOTEProfile(
            leg=leg, wick_high=0, wick_low=0, wick_range=0,
            ce=0, stdv=0, reasons=["no manipulation leg — no profile"]
        )

    wh = leg.wick_high
    wl = leg.wick_low
    rng = wh - wl
    ce = (wh + wl) / 2.0
    stdv = rng / _STDV_DIVISOR if rng > 0 else 0.0

    atr_val = _atr(bars, period=atr_period, end_idx=leg.end_idx + 1)

    stdv_levels = _compute_stdv_levels(ce, stdv, leg.direction)
    fib_levels = _compute_fib_levels(wl, wh, leg.direction)

    # OTE zone: between CE and the deepest key OTE level
    sign = _direction_sign(leg.direction)
    ote_top = ce
    ote_bottom = ce + sign * OTE_ZONE_MAX * stdv

    # But also bound by fib levels for practical use
    if leg.direction == "BULL":
        ote_zone_low = ce
        ote_zone_high = ce + OTE_ZONE_MAX * stdv
    else:
        ote_zone_high = ce
        ote_zone_low = ce - OTE_ZONE_MAX * stdv

    profile = STDVOTEProfile(
        leg=leg,
        wick_high=wh,
        wick_low=wl,
        wick_range=rng,
        ce=round(ce, 5),
        stdv=round(stdv, 5),
        stdv_levels=stdv_levels,
        fib_levels=fib_levels,
        ote_zone_top=round(ote_zone_high, 5),
        ote_zone_bottom=round(ote_zone_low, 5),
        atr=round(atr_val, 5),
        reasons=[
            f"Manipulation leg {leg.leg_type}: wick {wl:.5f}-{wh:.5f}",
            f"CE(0.5) = {ce:.5f}",
            f"STDV unit = {stdv:.5f} (range/{_STDV_DIVISOR:.1f})",
            f"OTE zone: {ote_zone_low:.5f} to {ote_zone_high:.5f}",
            f"Displacement direction: {leg.direction}",
        ],
    )

    if leg.is_high_quality:
        profile.reasons.append("High-quality manipulation leg (strong rejection)")

    return profile


def nearest_level(profile: STDVOTEProfile, price: float,
                  direction: str = "",
                  use_stdv: bool = True,
                  use_fib: bool = True) -> Tuple[Optional[STDVLevel], float]:
    """
    Find the nearest STDV or Fib level to `price`.

    Returns (level, distance_in_price). Distance is always positive.
    If direction is provided, only considers levels on the displacement side
    (above CE for BULL, below CE for BEAR).
    """
    candidates = []
    if use_stdv:
        candidates.extend(profile.stdv_levels)
    if use_fib:
        candidates.extend(profile.fib_levels)

    if not candidates:
        return None, float("inf")

    sign = _direction_sign(direction) if direction else 0
    best = None
    best_dist = float("inf")

    for lv in candidates:
        # Skip levels on the wrong side if direction is specified
        if sign != 0:
            # For BULL, only levels >= CE (above midpoint)
            # For BEAR, only levels <= CE (below midpoint)
            if sign > 0 and lv.price < profile.ce - profile.stdv * 0.1:
                continue
            if sign < 0 and lv.price > profile.ce + profile.stdv * 0.1:
                continue

        dist = abs(lv.price - price)
        if dist < best_dist:
            best_dist = dist
            best = lv

    return best, best_dist


def is_price_at_level(profile: STDVOTEProfile, price: float,
                      level_name: str,
                      atr_frac: float = _LEVEL_PROXIMITY_ATR_FRAC) -> bool:
    """True if `price` is within `atr_frac` * ATR of the named level."""
    lv = profile.level_by_name(level_name)
    if lv is None:
        return False
    tol = profile.atr * atr_frac if profile.atr > 0 else profile.stdv * 0.25
    return abs(lv.price - price) <= tol


def is_price_in_ote_zone(profile: STDVOTEProfile, price: float) -> bool:
    """True if price is inside the OTE zone (between CE and deepest OTE level)."""
    if profile.ote_zone_top > profile.ote_zone_bottom:
        return profile.ote_zone_bottom <= price <= profile.ote_zone_top
    return profile.ote_zone_top <= price <= profile.ote_zone_bottom


def get_entry_candidates(profile: STDVOTEProfile, price: float,
                         direction: str) -> List[Tuple[STDVLevel, float, str]]:
    """
    Return all levels that `price` is near, sorted by quality.

    Each entry: (level, distance, reason)
    Quality order: OTE > Reaccum > CE > Fib_0.705 > Fib_0.79 > etc.
    """
    if not profile.leg.detected:
        return []

    tol = profile.atr * _LEVEL_PROXIMITY_ATR_FRAC if profile.atr > 0 else profile.stdv * 0.25
    tol = max(tol, 0.0001)  # absolute minimum tolerance

    candidates = []
    sign = _direction_sign(direction)

    for lv in profile.all_levels:
        # Only levels on the correct side
        if sign > 0 and lv.price < profile.ce - profile.stdv * 0.1:
            continue
        if sign < 0 and lv.price > profile.ce + profile.stdv * 0.1:
            continue

        dist = abs(lv.price - price)
        if dist <= tol:
            quality = _level_quality(lv.name)
            candidates.append((lv, dist, quality))

    # Sort by quality score (higher = better)
    candidates.sort(key=lambda x: x[2], reverse=True)
    return [(lv, dist, q) for lv, dist, q in candidates]


def _level_quality(name: str) -> float:
    """Quality score for sorting entry candidates. Higher = better entry level."""
    scores = {
        "OTE": 10.0,
        "OTE_0.79": 9.5,
        "OTE_0.705": 9.0,
        "OTE_0.886": 8.5,
        "Reaccum": 8.0,
        "CE": 7.0,
        "FIB_0.705": 7.0,
        "FIB_0.79": 6.5,
        "FIB_0.886": 6.0,
        "FIB_0.65": 6.0,
        "FIB_0.63": 5.5,
        "FIB_0.50": 5.0,
        "Reversal": 4.0,
        "MaxExp3": 3.0,
        "MaxExp4": 2.0,
        "MaxExp5": 1.0,
        "WICK_LOW": 0.5,
        "WICK_HIGH": 0.5,
        "FIB_1.000": 0.5,
    }
    return scores.get(name, 0.0)


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from manipulation_leg_detector import _make_fixture, get_primary_manipulation_leg

    bars = _make_fixture()
    leg = get_primary_manipulation_leg(bars, bias_direction="BEAR",
                                       pip_size=0.01, min_recent_bars=25)

    print("[TEST] Manipulation leg:")
    print(f"       Type: {leg.leg_type}, Direction: {leg.direction}")
    print(f"       Wick range: {leg.wick_low:.2f} - {leg.wick_high:.2f}")

    profile = compute_profile(leg, bars)
    print(f"\n[TEST] STDV/OTE Profile:")
    print(f"       CE = {profile.ce:.5f}")
    print(f"       STDV unit = {profile.stdv:.5f}")
    print(f"       ATR = {profile.atr:.5f}")
    print(f"       OTE zone: {profile.ote_zone_bottom:.5f} to {profile.ote_zone_top:.5f}")

    print(f"\n       STDV Levels ({len(profile.stdv_levels)}):")
    for lv in profile.stdv_levels:
        print(f"         {lv.name:12s} = {lv.price:.5f}  (mult={lv.multiplier})")

    print(f"\n       Fib Levels ({len(profile.fib_levels)}):")
    for lv in profile.fib_levels:
        print(f"         {lv.name:12s} = {lv.price:.5f}  (ratio={lv.multiplier})")

    # Test nearest level
    test_price = profile.ce - profile.stdv * 0.7  # near OTE for BEAR
    nearest, dist = nearest_level(profile, test_price, direction="BEAR")
    print(f"\n[TEST] Price {test_price:.5f} nearest level: {nearest.name if nearest else 'none'}")
    print(f"       Distance: {dist:.5f}")

    # Test OTE zone
    in_zone = is_price_in_ote_zone(profile, test_price)
    print(f"       In OTE zone: {in_zone}")

    # Test entry candidates
    candidates = get_entry_candidates(profile, test_price, "BEAR")
    print(f"\n[TEST] Entry candidates at {test_price:.5f}:")
    for lv, d, q in candidates:
        print(f"       {lv.name:12s} = {lv.price:.5f}  dist={d:.5f}  quality={q:.1f}")

    assert profile.ce > 0, "CE must be computed"
    assert len(profile.stdv_levels) >= 8, "Must have all STDV levels"
    assert len(profile.fib_levels) >= 7, "Must have all Fib levels"
    assert nearest is not None, "Must find nearest level"
    print("\n[OK] All assertions passed")


if __name__ == "__main__":
    _self_test()
