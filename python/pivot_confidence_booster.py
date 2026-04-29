"""
Pivot Confidence Booster

Glue layer that combines pivot_engine.py output with market_structure_validator.py
output and produces a single confidence boost (0-15 points) for an ICT setup.

Called from ict_precision.py after the 7-layer ICT confluence is computed.
The boost is added to the ICT setup's `confidence` field, and a list of
human-readable reasons is appended to the setup's `reasons` for logging.

Public API:
- boost_setup(setup, symbol, bars_dict, ict_patterns, atr, params=None)
    → (boost_points, reasons, metadata)
"""

from typing import Dict, List, Optional, Tuple, Any
import logging

from pivot_engine import (
    Bar, PivotLevel, calculate_pivots,
    score_pivot_strength, detect_multi_tf_confluence,
    score_pivot_reliability, identify_reversal_probability,
    find_nearest_pivot_level,
)
from market_structure_validator import validate_pivot_on_structure

log = logging.getLogger(__name__)

# Tunable parameters — Phase 2f's parameter_optimizer overrides these from
# learned_parameters.json. Keep defaults conservative.
DEFAULT_PARAMS = {
    "max_total_boost":       15,    # Hard cap — pivot cannot dominate ICT
    "strong_pivot_boost":    5,     # Distance < 1 pip + STRONG strength
    "medium_pivot_boost":    3,     # Distance < 5 pips + 2+ touches
    "multi_tf_bonus":        3,     # 3+ timeframes align
    "reversal_pattern_bonus": 2,    # Wick rejection / engulfing > 0.7 prob
    "alignment_distance_pips": 0.0010,  # 10 pips = "aligned"
    "tight_alignment_pips":   0.0001,   # 1 pip = "tight alignment"
    "min_confluence_count":  3,     # 3+ TFs aligned = bonus
    "min_reversal_prob":     0.7,
}


def _bars_to_pivot_dict(bars_dict: Dict[str, List[Bar]],
                        symbol: str,
                        timeframes: List[str]) -> Dict[str, Dict[str, List[PivotLevel]]]:
    """Compute pivots for each timeframe in `timeframes` for the given symbol."""
    out: Dict[str, Dict[str, List[PivotLevel]]] = {}
    for tf in timeframes:
        bars = bars_dict.get(tf) or []
        if not bars:
            continue
        try:
            pivots = calculate_pivots(bars, symbol, tf)
            if pivots:
                out[tf] = pivots
        except Exception as e:
            log.debug(f"Pivot calc failed for {symbol}/{tf}: {e}")
    return out


def _flatten_pivots(pivots_by_tf: Dict[str, Dict[str, List[PivotLevel]]]) -> List[PivotLevel]:
    flat: List[PivotLevel] = []
    for tf, pivots_by_type in pivots_by_tf.items():
        for ptype, levels in pivots_by_type.items():
            flat.extend(levels)
    return flat


def boost_setup(
    setup: Any,
    symbol: str,
    bars_dict: Dict[str, List[Bar]],
    ict_patterns: Optional[Dict[str, List[Dict]]] = None,
    atr: float = 0.001,
    params: Optional[Dict] = None,
    pivot_timeframes: Optional[List[str]] = None,
) -> Tuple[int, List[str], Dict]:
    """
    Compute a pivot-based confidence boost for an ICT setup.

    Args:
        setup: ICTSetup object with at minimum .entry_price, .direction (str)
        symbol: trading symbol (e.g. "EURUSD")
        bars_dict: {timeframe: [Bar]} for the symbol — should include several TFs
                   for multi-TF confluence detection
        ict_patterns: optional dict from smc_engine output (OBs, FVGs, BOS, swings)
                      for market_structure_validator
        atr: ATR value used for distance calibration
        params: optional override of DEFAULT_PARAMS
        pivot_timeframes: which TFs to compute pivots on; defaults to a sensible mix

    Returns:
        (boost_points, reasons, metadata)
        - boost_points: int 0-15
        - reasons: list[str] for logging / telegram
        - metadata: dict with {nearest_pivot, structure_type, alignment_score, ...}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    if pivot_timeframes is None:
        pivot_timeframes = ["M5", "M15", "H1", "H4", "D1"]

    entry_price = float(getattr(setup, "entry_price", 0) or 0)
    direction = str(getattr(setup, "direction", "")).upper()
    if entry_price == 0 or direction not in ("BUY", "SELL"):
        return (0, [], {})

    # 1. Compute pivots across timeframes
    pivots_by_tf = _bars_to_pivot_dict(bars_dict, symbol, pivot_timeframes)
    if not pivots_by_tf:
        return (0, [], {"reason": "no pivots computed"})

    flat_pivots = _flatten_pivots(pivots_by_tf)

    # 2. Multi-TF confluence map
    confluence = detect_multi_tf_confluence(pivots_by_tf,
                                            tolerance_pips=p["alignment_distance_pips"])

    # 3. Find nearest pivot to entry
    nearest, distance = find_nearest_pivot_level(
        flat_pivots, entry_price, max_distance_pips=p["alignment_distance_pips"]
    )
    if nearest is None:
        return (0, [], {"reason": "no pivot within alignment range"})

    # 4. Annotate pivot with strength/confluence/reliability/reversal_prob
    primary_tf_bars = bars_dict.get(nearest.timeframe) or bars_dict.get("M15") or []
    if primary_tf_bars:
        touches, last_idx, _ = score_pivot_strength(
            primary_tf_bars, nearest.level, tolerance_pips=p["alignment_distance_pips"]
        )
        nearest.touches = touches
        nearest.last_touch_idx = last_idx
        nearest.distance_pips = distance
        nearest.confluence_count = confluence.get(nearest.level, 0)

        if touches >= 3:
            nearest.strength = "STRONG"
        elif touches >= 1:
            nearest.strength = "MEDIUM"
        else:
            nearest.strength = "WEAK"

        nearest.reversal_probability = identify_reversal_probability(
            primary_tf_bars,
            pivot_level=nearest.level,
            direction="UP" if direction == "BUY" else "DOWN",
            atr=atr,
        )

    # 5. Build the boost
    boost = 0
    reasons: List[str] = []

    # Base boost: alignment with strong/medium pivot
    if distance <= p["tight_alignment_pips"] and nearest.strength == "STRONG":
        boost += p["strong_pivot_boost"]
        reasons.append(
            f"Tight alignment with STRONG {nearest.pivot_type} {nearest.level_type} "
            f"({nearest.touches} touches) on {nearest.timeframe} @ {nearest.level:.5f}"
        )
    elif distance <= p["alignment_distance_pips"] and nearest.strength in ("MEDIUM", "STRONG"):
        boost += p["medium_pivot_boost"]
        reasons.append(
            f"Alignment with {nearest.strength} {nearest.pivot_type} {nearest.level_type} "
            f"({nearest.touches} touches) on {nearest.timeframe}"
        )

    # Multi-timeframe confluence
    if nearest.confluence_count >= p["min_confluence_count"]:
        boost += p["multi_tf_bonus"]
        reasons.append(
            f"Multi-TF confluence: {nearest.confluence_count + 1} timeframes align at {nearest.level:.5f}"
        )

    # Reversal pattern bonus
    if nearest.reversal_probability >= p["min_reversal_prob"]:
        boost += p["reversal_pattern_bonus"]
        reasons.append(
            f"High reversal probability ({nearest.reversal_probability:.0%}) at pivot — "
            f"candle pattern aligned"
        )

    # 6. Market structure overlay (the heart of "pivots on OBs/FVGs")
    structure_type = "NONE"
    structure_boost = 0
    structure_reasons: List[str] = []
    alignment_score = 0.0
    if ict_patterns:
        structure_type, structure_boost, structure_reasons, alignment_score = (
            validate_pivot_on_structure(
                pivot_level=nearest.level,
                ict_patterns=ict_patterns,
                symbol=symbol,
                timeframe=nearest.timeframe,
                atr=atr,
            )
        )
        if structure_boost > 0:
            boost += structure_boost
            reasons.extend(structure_reasons)

    # 7. Hard cap at max boost
    boost = min(boost, p["max_total_boost"])

    metadata = {
        "nearest_pivot_level":   round(nearest.level, 5),
        "nearest_pivot_tf":      nearest.timeframe,
        "nearest_pivot_type":    f"{nearest.pivot_type}/{nearest.level_type}",
        "distance_pips":         round(distance, 5),
        "touches":               nearest.touches,
        "confluence_count":      nearest.confluence_count + 1,  # include self
        "reversal_probability":  round(nearest.reversal_probability, 3),
        "structure_type":        structure_type,
        "structure_boost":       structure_boost,
        "alignment_score":       round(alignment_score, 3),
        "boost_total":           boost,
    }

    return (boost, reasons, metadata)
