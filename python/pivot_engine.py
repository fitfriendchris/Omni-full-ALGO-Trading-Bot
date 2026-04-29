"""
Pivot Point Analysis Engine for OMNI-ICT Trading Bot

Detects support/resistance pivot levels across multiple timeframes,
scores their confluence and reversal probability, and validates ICT entries.

Core functions:
- calculate_pivots(bars) — compute Standard and Fibonacci pivots from latest bar
- score_pivot_strength(bars, level, tolerance) — count touches, measure recency
- detect_multi_tf_confluence(pivots_by_tf, tolerance) — find aligned levels across TFs
- score_pivot_reliability(level, recent_reversals, atr) — composite 0-1 score
- identify_reversal_probability(bars, level, direction, atr) — estimate reversal odds
"""

from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from enum import Enum
import math
import logging

log = logging.getLogger(__name__)


class PivotStrength(str, Enum):
    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


class LevelType(str, Enum):
    S2 = "S2"
    S1 = "S1"
    PIVOT = "PIVOT"
    R1 = "R1"
    R2 = "R2"


class PivotType(str, Enum):
    STANDARD = "STANDARD"
    FIBONACCI = "FIBONACCI"


@dataclass
class Bar:
    """Single OHLCV bar of price data."""
    open_price: float
    high: float
    low: float
    close: float
    volume: float
    time: float  # Unix timestamp


@dataclass
class PivotLevel:
    """Detected pivot support/resistance level with metadata."""
    symbol: str
    timeframe: str  # "M1", "M5", "M15", "H1", "D1", "W1", etc.
    bar_time: float  # Unix timestamp of the bar this pivot was calculated from
    pivot_type: str  # "STANDARD" or "FIBONACCI"
    level: float  # The actual price level
    level_type: str  # "PIVOT", "R1", "R2", "S1", "S2"
    strength: str = "WEAK"  # "WEAK", "MEDIUM", "STRONG"
    touches: int = 0  # How many times price has tested this level
    last_touch_idx: int = -1  # Index of most recent touch in bars array
    distance_pips: float = 0.0  # Distance from current price to this level
    reversal_probability: float = 0.0  # 0-1; higher = more likely to reverse
    confluence_count: int = 0  # How many other timeframes have aligned levels
    reversal_info: Dict = field(default_factory=dict)  # Context for reversal: wick_rejection, engulfing, etc.


# ─────────────────────────────────────────────────────────────────────────────
# CORE PIVOT FORMULAS
# ─────────────────────────────────────────────────────────────────────────────

def calculate_standard_pivots(bar: Bar) -> Dict[str, float]:
    """
    Standard pivot formula: most widely used institutional levels.

    Pivot = (H + L + C) / 3
    R1 = (2 × Pivot) - L
    S1 = (2 × Pivot) - H
    R2 = Pivot + (H - L)
    S2 = Pivot - (H - L)
    """
    h, l, c = bar.high, bar.low, bar.close
    pivot = (h + l + c) / 3.0
    range_val = h - l

    return {
        "PIVOT": pivot,
        "R1": (2 * pivot) - l,
        "R2": pivot + range_val,
        "S1": (2 * pivot) - h,
        "S2": pivot - range_val,
    }


def calculate_fibonacci_pivots(bar: Bar) -> Dict[str, float]:
    """
    Fibonacci pivot formula: tighter levels, better for breakout detection.

    Pivot = (H + L + C) / 3
    R1 = Pivot + 0.382 × (H - L)
    S1 = Pivot - 0.382 × (H - L)
    R2 = Pivot + 0.618 × (H - L)
    S2 = Pivot - 0.618 × (H - L)
    """
    h, l, c = bar.high, bar.low, bar.close
    pivot = (h + l + c) / 3.0
    range_val = h - l

    return {
        "PIVOT": pivot,
        "R1": pivot + (0.382 * range_val),
        "R2": pivot + (0.618 * range_val),
        "S1": pivot - (0.382 * range_val),
        "S2": pivot - (0.618 * range_val),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PIVOT CALCULATION & DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def calculate_pivots(
    bars: List[Bar],
    symbol: str,
    timeframe: str,
    pivot_types: Optional[List[str]] = None
) -> Dict[str, List[PivotLevel]]:
    """
    Calculate all specified pivot types for the most recent complete bar.

    Args:
        bars: List of Bar objects (last bar is most recent)
        symbol: Trading symbol (e.g., "EURUSD")
        timeframe: Timeframe identifier (e.g., "M15", "H1")
        pivot_types: List of pivot types to calculate ["STANDARD", "FIBONACCI"]
                    Defaults to both if not specified

    Returns:
        Dict keyed by pivot_type, each containing list of PivotLevel objects
        {
            "STANDARD": [PivotLevel(S2), PivotLevel(S1), ..., PivotLevel(R2)],
            "FIBONACCI": [...],
        }
    """
    if not bars or len(bars) < 1:
        return {}

    if pivot_types is None:
        pivot_types = [PivotType.STANDARD.value, PivotType.FIBONACCI.value]

    latest_bar = bars[-1]
    results = {}

    for ptype in pivot_types:
        results[ptype] = []

        if ptype == PivotType.STANDARD.value:
            levels = calculate_standard_pivots(latest_bar)
        elif ptype == PivotType.FIBONACCI.value:
            levels = calculate_fibonacci_pivots(latest_bar)
        else:
            continue

        for level_type in ["S2", "S1", "PIVOT", "R1", "R2"]:
            pivot_level = PivotLevel(
                symbol=symbol,
                timeframe=timeframe,
                bar_time=latest_bar.time,
                pivot_type=ptype,
                level=levels[level_type],
                level_type=level_type,
            )
            results[ptype].append(pivot_level)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# PIVOT STRENGTH SCORING (TOUCH COUNT)
# ─────────────────────────────────────────────────────────────────────────────

def score_pivot_strength(
    bars: List[Bar],
    level: float,
    tolerance_pips: float = 5.0
) -> Tuple[int, int, float]:
    """
    Count how many times price has tested this pivot level.

    A "touch" occurs when a bar's high or low is within tolerance_pips of the level.
    Returns how many touches and the index of the most recent touch.

    Args:
        bars: List of bars to scan
        level: The pivot level to test against
        tolerance_pips: Price tolerance for considering a "touch" (in pips)

    Returns:
        (touches, last_touch_idx, test_strength)
        - touches: count of bars that touched the level
        - last_touch_idx: index of most recent touch (-1 if none)
        - test_strength: 0-1 score (recency weighted)
    """
    touches = 0
    last_touch_idx = -1

    for idx in range(len(bars) - 1, -1, -1):  # Scan backwards (most recent first)
        bar = bars[idx]
        distance_pips = min(abs(bar.high - level), abs(bar.low - level))

        if distance_pips <= tolerance_pips:
            touches += 1
            if last_touch_idx == -1:
                last_touch_idx = idx

    # Test strength: recent touches score higher
    # If we just touched it (last_touch_idx == len-1), strength = 1.0
    # If it's older, decay exponentially
    test_strength = 0.0
    if last_touch_idx >= 0:
        bars_since_touch = len(bars) - 1 - last_touch_idx
        # Decay: 20 bars ago = 50%, 40 bars ago = 25%, etc.
        test_strength = math.exp(-0.035 * bars_since_touch)

    return touches, last_touch_idx, test_strength


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-TIMEFRAME CONFLUENCE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_multi_tf_confluence(
    pivots_by_tf: Dict[str, Dict[str, List[PivotLevel]]],
    tolerance_pips: float = 10.0
) -> Dict[float, int]:
    """
    For each detected pivot level, check how many OTHER timeframes have a level
    within tolerance_pips. Higher confluence = stronger pivot.

    Args:
        pivots_by_tf: Dict of {timeframe: {pivot_type: [PivotLevel]}}
        tolerance_pips: Price proximity threshold for "alignment"

    Returns:
        Dict mapping pivot price level → confluence_count
        {1.0875: 3, 1.0860: 2, ...}
    """
    all_levels = []

    for tf, pivots_by_type in pivots_by_tf.items():
        for ptype, levels_list in pivots_by_type.items():
            for level in levels_list:
                all_levels.append((level.level, tf))

    confluence_map = {}

    for target_level, _ in all_levels:
        count = 0
        aligned_tfs = set()

        for other_level, other_tf in all_levels:
            if abs(other_level - target_level) <= tolerance_pips and other_tf != _:
                count += 1
                aligned_tfs.add(other_tf)

        # Only count unique TF alignments (not same level from different pivot types)
        confluence_map[target_level] = len(aligned_tfs)

    return confluence_map


# ─────────────────────────────────────────────────────────────────────────────
# PIVOT RELIABILITY SCORING
# ─────────────────────────────────────────────────────────────────────────────

def score_pivot_reliability(
    level: PivotLevel,
    bars: List[Bar],
    recent_reversals: Optional[List[float]] = None,
    atr: float = 0.001
) -> float:
    """
    Composite pivot reliability score (0-1).

    Considers:
    - Touch count (more touches = higher score, but with diminishing returns)
    - Distance from current price (very close can be unreliable, mid-range is best)
    - ATR proximity (if close to current price, reduce score)
    - Confluence alignment from other TFs

    Args:
        level: PivotLevel to score
        bars: Recent bars for context
        recent_reversals: List of price levels where reversals occurred (for validation)
        atr: Average true range (for distance calibration)

    Returns:
        float 0.0-1.0 representing reliability
    """
    if not bars or atr <= 0:
        return 0.0

    current_price = bars[-1].close
    distance = abs(current_price - level.level)
    distance_atr_ratio = distance / atr if atr > 0 else 0

    # Touch count score: 0 touches = 0.2, 1 = 0.4, 2 = 0.65, 3+ = 0.85+
    touch_score = min(0.85, 0.2 + (level.touches * 0.25))

    # Distance score: sweet spot is 0.5-2.0 ATR away
    if distance_atr_ratio < 0.1:
        distance_score = 0.4  # Too close, might be noise
    elif distance_atr_ratio < 0.5:
        distance_score = 0.8  # Very close but not immediately
    elif distance_atr_ratio < 2.0:
        distance_score = 1.0  # Sweet spot
    elif distance_atr_ratio < 5.0:
        distance_score = 0.7  # A bit far but reasonable
    else:
        distance_score = 0.3  # Very far, low relevance

    # Confluence score: each additional TF alignment adds 0.1
    confluence_score = min(1.0, 0.5 + (level.confluence_count * 0.1))

    # Weight: touch count (40%) + distance (35%) + confluence (25%)
    reliability = (touch_score * 0.4) + (distance_score * 0.35) + (confluence_score * 0.25)

    return max(0.0, min(1.0, reliability))


# ─────────────────────────────────────────────────────────────────────────────
# REVERSAL PROBABILITY ESTIMATION
# ─────────────────────────────────────────────────────────────────────────────

def identify_reversal_probability(
    bars: List[Bar],
    pivot_level: float,
    direction: str,  # "UP" or "DOWN" — expected reversal direction
    atr: float = 0.001
) -> float:
    """
    Estimate the probability that price will reverse at this pivot level.

    Checks for:
    - Wick rejection at level (strong up-wick at Support, down-wick at Resistance)
    - Engulfing patterns at level
    - Pinbar patterns (reversal candles)

    Args:
        bars: Recent bars for pattern detection
        pivot_level: The pivot level to evaluate
        direction: "UP" (expect reversal at support, price bounces up)
                  "DOWN" (expect reversal at resistance, price bounces down)
        atr: Average true range for scale reference

    Returns:
        float 0.0-1.0 probability of reversal at this level
    """
    if not bars or len(bars) < 2:
        return 0.0

    score = 0.5  # Base probability

    latest = bars[-1]
    prev = bars[-2]

    # --- Wick Rejection Pattern ---
    # At Support (direction="UP"): high wick down, close near open/high (bullish rejection)
    # At Resistance (direction="DOWN"): high wick up, close near open/low (bearish rejection)

    if direction == "UP":
        # Looking for bullish reversal at support
        wick_size = latest.high - latest.low
        lower_wick = latest.low - min(latest.open_price, latest.close)
        upper_wick = latest.high - max(latest.open_price, latest.close)

        # Strong lower wick relative to body = rejection of lower prices
        if lower_wick > (wick_size * 0.6) and latest.close > (latest.open_price):
            score += 0.25  # Strong bullish rejection

        # Price closed at level = strong commitment
        if abs(latest.close - pivot_level) < (atr * 0.1):
            score += 0.15

    elif direction == "DOWN":
        # Looking for bearish reversal at resistance
        wick_size = latest.high - latest.low
        upper_wick = latest.high - max(latest.open_price, latest.close)
        lower_wick = latest.low - min(latest.open_price, latest.close)

        # Strong upper wick relative to body = rejection of higher prices
        if upper_wick > (wick_size * 0.6) and latest.close < latest.open_price:
            score += 0.25  # Strong bearish rejection

        # Price closed at level = strong commitment
        if abs(latest.close - pivot_level) < (atr * 0.1):
            score += 0.15

    # --- Pinbar Pattern (indecision) ---
    # Pinbar = long wick on one side, small body on opposite side
    if len(bars) >= 1:
        wick_size = latest.high - latest.low
        body_size = abs(latest.close - latest.open_price)

        if wick_size > 0 and body_size > 0:
            wick_ratio = wick_size / body_size
            if wick_ratio > 3.0:  # Long wick, small body
                score += 0.1

    # --- Engulfing at Level ---
    if len(bars) >= 2:
        if (latest.high >= prev.high and latest.low <= prev.low and
            abs(latest.close - pivot_level) < (atr * 0.2)):
            score += 0.15  # Engulfing near pivot = strong

    return max(0.0, min(1.0, score))


# ─────────────────────────────────────────────────────────────────────────────
# PIVOT VALIDATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def find_nearest_pivot_level(
    pivots: List[PivotLevel],
    price: float,
    max_distance_pips: float = 10.0
) -> Tuple[Optional[PivotLevel], float]:
    """
    Find the nearest pivot level to a given price.

    Args:
        pivots: List of PivotLevel objects to search
        price: Reference price
        max_distance_pips: Maximum distance to consider (0 = no limit)

    Returns:
        (nearest_pivot, distance) or (None, float('inf')) if no match
    """
    if not pivots:
        return None, float('inf')

    nearest = None
    min_distance = float('inf')

    for pivot in pivots:
        distance = abs(pivot.level - price)
        if distance < min_distance and (max_distance_pips == 0 or distance <= max_distance_pips):
            min_distance = distance
            nearest = pivot

    return nearest, min_distance


def get_pivot_levels_summary(
    pivots_by_tf: Dict[str, Dict[str, List[PivotLevel]]]
) -> Dict[str, any]:
    """
    Summarize pivot levels detected across timeframes for logging/diagnostics.

    Returns:
        Dict with summary of pivots by TF and type
    """
    summary = {}
    for tf, pivots_by_type in pivots_by_tf.items():
        summary[tf] = {}
        for ptype, levels in pivots_by_type.items():
            summary[tf][ptype] = {
                "pivot": next((l.level for l in levels if l.level_type == "PIVOT"), None),
                "s1": next((l.level for l in levels if l.level_type == "S1"), None),
                "s2": next((l.level for l in levels if l.level_type == "S2"), None),
                "r1": next((l.level for l in levels if l.level_type == "R1"), None),
                "r2": next((l.level for l in levels if l.level_type == "R2"), None),
            }
    return summary
