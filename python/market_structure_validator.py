"""
Market Structure Validator

Correlates pivot levels with ICT market-structure patterns (Order Blocks,
Fair Value Gaps, Breaks of Structure). A pivot that sits at institutional
liquidity (an OB low or FVG boundary) is far more likely to reverse than an
isolated standalone pivot.

This module is the bridge between pivot_engine.py and smc_engine.py — it
takes detected pivots + detected ICT patterns and returns a "structure
boost" score that captures how strongly the pivot is anchored to actual
market-maker liquidity.

Public API:
- validate_pivot_on_structure(pivot_level, ict_patterns, symbol, tf, atr)
    → (structure_type, boost_points, reasons, alignment_score)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import logging

log = logging.getLogger(__name__)


@dataclass
class StructureMatch:
    """One pivot-on-structure correlation."""
    structure_type: str        # "ORDER_BLOCK", "FVG", "BOS", "LIQUIDITY_POOL"
    timeframe: str             # TF where pattern was detected
    boost: int                 # Confidence boost contribution (points)
    distance_pips: float       # How far the pivot is from the structure edge
    reason: str                # Human-readable for logging/telegram


# Boost tiers — must align with rules.json or learned_parameters.json eventually.
# Phase 2f's parameter_optimizer will tune these per regime.
DEFAULT_BOOSTS = {
    "ORDER_BLOCK_HTF":  12,   # Pivot on D1/H4 OB — institutional accumulation
    "ORDER_BLOCK_LTF":  8,    # Pivot on M15/H1 OB — entry-grade liquidity
    "FVG_BOUNDARY":     8,    # Pivot at FVG edge — price MUST fill imbalance
    "FVG_INSIDE":       4,    # Pivot inside FVG — partial alignment
    "BOS_LEVEL":        6,    # Pivot at recent break of structure
    "LIQUIDITY_POOL":   5,    # Pivot at recent swing high/low (stop pool)
    "MTF_OB_ALIGNMENT": 5,    # HTF + LTF both have OBs aligned (bonus)
}


def _within_zone(price: float, zone_low: float, zone_high: float, tolerance: float = 0.0) -> bool:
    """Check if a price sits inside a structure zone (with tolerance for the boundary)."""
    return (zone_low - tolerance) <= price <= (zone_high + tolerance)


def _at_zone_boundary(price: float, zone_low: float, zone_high: float, tolerance: float) -> bool:
    """Check if price is at one of the zone edges (within tolerance)."""
    return (abs(price - zone_low) <= tolerance) or (abs(price - zone_high) <= tolerance)


def validate_pivot_on_structure(
    pivot_level: float,
    ict_patterns: Dict[str, List[Dict]],
    symbol: str,
    timeframe: str,
    atr: float = 0.001,
) -> Tuple[str, int, List[str], float]:
    """
    Check whether a pivot price level aligns with detected ICT structures.

    Args:
        pivot_level: The pivot price (e.g. S1 or R1 from pivot_engine)
        ict_patterns: Dict of detected patterns keyed by type:
            {
              "order_blocks": [{tf, low, high, direction, ts, is_htf}, ...],
              "fvgs":          [{tf, low, high, direction, ts}, ...],
              "bos_levels":    [{tf, level, direction, ts}, ...],
              "swing_highs":   [{tf, level, ts}, ...],
              "swing_lows":    [{tf, level, ts}, ...],
            }
        symbol: trading symbol
        timeframe: TF of the pivot itself (used for log context)
        atr: ATR used to size the alignment tolerance

    Returns:
        (structure_type, boost_points, reasons, alignment_score)
        - structure_type: most-significant structure found ("ORDER_BLOCK", "FVG", ...) or "NONE"
        - boost_points: total boost (capped at 15)
        - reasons: list of human-readable strings explaining the boost
        - alignment_score: 0.0-1.0 measure of overall structure alignment
    """
    if not ict_patterns:
        return ("NONE", 0, [], 0.0)

    matches: List[StructureMatch] = []
    # Tolerance is 0.5 ATR for pivots — they don't have to land exactly on the level
    tolerance = max(atr * 0.5, 0.0001)
    htf_set = {"D1", "H4", "W1", "MN1", "3M", "6M"}

    # ── Order Block alignment ───────────────────────────────────────────────
    for ob in ict_patterns.get("order_blocks", []) or []:
        ob_tf = ob.get("tf") or ob.get("timeframe", "")
        ob_low = float(ob.get("low", 0))
        ob_high = float(ob.get("high", 0))
        if ob_low == 0 or ob_high == 0:
            continue
        is_htf = ob.get("is_htf") or ob_tf in htf_set
        if _within_zone(pivot_level, ob_low, ob_high, tolerance):
            distance = min(abs(pivot_level - ob_low), abs(pivot_level - ob_high))
            tier = "ORDER_BLOCK_HTF" if is_htf else "ORDER_BLOCK_LTF"
            matches.append(StructureMatch(
                structure_type="ORDER_BLOCK",
                timeframe=ob_tf,
                boost=DEFAULT_BOOSTS[tier],
                distance_pips=distance,
                reason=f"Pivot on {ob_tf} Order Block ({ob.get('direction','')})",
            ))

    # ── Fair Value Gap alignment ───────────────────────────────────────────
    for fvg in ict_patterns.get("fvgs", []) or []:
        fvg_tf = fvg.get("tf") or fvg.get("timeframe", "")
        fvg_low = float(fvg.get("low", 0))
        fvg_high = float(fvg.get("high", 0))
        if fvg_low == 0 or fvg_high == 0:
            continue
        if _at_zone_boundary(pivot_level, fvg_low, fvg_high, tolerance):
            distance = min(abs(pivot_level - fvg_low), abs(pivot_level - fvg_high))
            matches.append(StructureMatch(
                structure_type="FVG",
                timeframe=fvg_tf,
                boost=DEFAULT_BOOSTS["FVG_BOUNDARY"],
                distance_pips=distance,
                reason=f"Pivot at {fvg_tf} FVG boundary ({fvg.get('direction','')})",
            ))
        elif _within_zone(pivot_level, fvg_low, fvg_high, 0):
            # Inside FVG but not at edge — still meaningful, lower boost
            matches.append(StructureMatch(
                structure_type="FVG",
                timeframe=fvg_tf,
                boost=DEFAULT_BOOSTS["FVG_INSIDE"],
                distance_pips=0.0,
                reason=f"Pivot inside {fvg_tf} FVG",
            ))

    # ── Break of Structure alignment ───────────────────────────────────────
    for bos in ict_patterns.get("bos_levels", []) or []:
        bos_tf = bos.get("tf") or bos.get("timeframe", "")
        bos_lvl = float(bos.get("level", 0))
        if bos_lvl == 0:
            continue
        if abs(pivot_level - bos_lvl) <= tolerance:
            matches.append(StructureMatch(
                structure_type="BOS",
                timeframe=bos_tf,
                boost=DEFAULT_BOOSTS["BOS_LEVEL"],
                distance_pips=abs(pivot_level - bos_lvl),
                reason=f"Pivot at {bos_tf} BOS ({bos.get('direction','')})",
            ))

    # ── Liquidity Pool (swing high/low) alignment ──────────────────────────
    for sh in ict_patterns.get("swing_highs", []) or []:
        lvl = float(sh.get("level", 0))
        if lvl and abs(pivot_level - lvl) <= tolerance:
            matches.append(StructureMatch(
                structure_type="LIQUIDITY_POOL",
                timeframe=sh.get("tf", ""),
                boost=DEFAULT_BOOSTS["LIQUIDITY_POOL"],
                distance_pips=abs(pivot_level - lvl),
                reason=f"Pivot at {sh.get('tf','?')} swing-high (sell-side liquidity)",
            ))
    for sl in ict_patterns.get("swing_lows", []) or []:
        lvl = float(sl.get("level", 0))
        if lvl and abs(pivot_level - lvl) <= tolerance:
            matches.append(StructureMatch(
                structure_type="LIQUIDITY_POOL",
                timeframe=sl.get("tf", ""),
                boost=DEFAULT_BOOSTS["LIQUIDITY_POOL"],
                distance_pips=abs(pivot_level - lvl),
                reason=f"Pivot at {sl.get('tf','?')} swing-low (buy-side liquidity)",
            ))

    # ── Multi-TF OB alignment bonus ────────────────────────────────────────
    ob_matches = [m for m in matches if m.structure_type == "ORDER_BLOCK"]
    htf_ob_tfs = {m.timeframe for m in ob_matches if m.timeframe in htf_set}
    ltf_ob_tfs = {m.timeframe for m in ob_matches if m.timeframe not in htf_set}
    if htf_ob_tfs and ltf_ob_tfs:
        matches.append(StructureMatch(
            structure_type="MTF_OB_ALIGNMENT",
            timeframe=f"{','.join(htf_ob_tfs)}+{','.join(ltf_ob_tfs)}",
            boost=DEFAULT_BOOSTS["MTF_OB_ALIGNMENT"],
            distance_pips=0.0,
            reason="HTF and LTF Order Blocks both align with pivot",
        ))

    if not matches:
        return ("NONE", 0, [], 0.0)

    # Aggregate: take the strongest single match + smaller bonuses, cap at 15
    matches.sort(key=lambda m: m.boost, reverse=True)
    primary = matches[0]
    total = primary.boost
    reasons = [primary.reason]

    # Add up to 2 secondary matches (different structure types) at half-boost
    seen_types = {primary.structure_type}
    for m in matches[1:]:
        if m.structure_type in seen_types:
            continue
        secondary_boost = max(2, m.boost // 2)
        total += secondary_boost
        reasons.append(m.reason)
        seen_types.add(m.structure_type)
        if len(reasons) >= 3:
            break

    total = min(total, 15)  # Hard cap — pivots cannot dominate ICT scoring
    alignment_score = min(1.0, len(matches) * 0.25)

    return (primary.structure_type, total, reasons, alignment_score)


def find_structures_near_price(
    price: float,
    ict_patterns: Dict[str, List[Dict]],
    atr: float,
    max_distance_atr: float = 2.0,
) -> List[Dict]:
    """
    Diagnostic helper: returns all ICT structures within `max_distance_atr * atr`
    of a given price. Useful for logging "what's around the entry?"
    """
    out: List[Dict] = []
    threshold = atr * max_distance_atr
    for kind in ("order_blocks", "fvgs", "bos_levels", "swing_highs", "swing_lows"):
        for s in ict_patterns.get(kind, []) or []:
            level = float(s.get("level") or s.get("low") or 0)
            if level and abs(price - level) <= threshold:
                out.append({"kind": kind, **s, "distance": round(abs(price - level), 5)})
    return sorted(out, key=lambda s: s["distance"])
