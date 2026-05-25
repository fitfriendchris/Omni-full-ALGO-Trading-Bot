"""
multi_tf_selector.py — ICT Multi-Timeframe Confluence Engine v4

Chris's hierarchy:
  H4 = Mark macro structure, determine bias
  H1 = Minor structure, manipulation legs, LIQUIDITY_SWEEP valid here only
  M15 = Entry confirmation (BOS/CHoCH, FVG, OTE)
  M1  = Precision execution (optional)

Key rules:
  1. LIQUIDITY_SWEEP only counts on H1 or higher. M15 LIQUIDITY_SWEEP = noise.
  2. H4 bias MUST agree with trade direction. No counter-H4 trades.
  3. H1 manipulation leg anchors STDV wick-to-wick.
  4. M15 must show confirming BOS/CHoCH + FVG at the OTE entry zone.
  5. Kill zone applies to M15 entry bar timestamp.
  6. Minimum 5 confluences for live execution. 4 = monitor only.

Confluence Conditions (C1-C8, need 5+ to trade):
  C1. H4 bias aligned with trade direction
  C2. H1 manipulation leg detected (JUDAS or EQH/EQL sweep — LIQUIDITY_SWEEP only on H1+)
  C3. Price at/near OTE/STDV level of the H1 manipulation leg
  C4. M15 BOS/CHoCH confirming the direction
  C5. M15 unmitigated FVG present at entry zone
  C6. M15 unmitigated OB present at entry zone
  C7. Kill zone active at entry time
  C8. AMD phase aligned with direction

Entry Pricing:
  - Limit order at true OTE/STDV level (never market price)
  - SL beyond H1 manipulation leg extreme + buffer
  - TP at opposing H4 liquidity (next H4 swing extreme, PDH/PDL)
  - Minimum R:R 3:1

Confidence:
  base = 0.45
  +0.08 per confluence (max +0.64)
  +0.10 if H4 bias is strong (recent BOS/CHoCH on H4)
  +0.05 if JUDAS manipulation (not LIQUIDITY_SWEEP)
  +0.05 if 5+ confluences
  -0.15 if LIQUIDITY_SWEEP on M15 (rejected)
  clamp [0.0, 1.0]

"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple

from smc_engine import (
    Bar, SMCSnapshot, analyze,
    OrderBlock, FairValueGap, StructureEvent,
)
from manipulation_leg_detector import (
    ManipulationLeg, detect_manipulation_legs, get_primary_manipulation_leg,
)
from stdv_ote_engine import (
    STDVOTEProfile, compute_profile,
    nearest_level, is_price_at_level,
    is_price_in_ote_zone, get_entry_candidates,
)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

MIN_CONFLUENCE = 4              # minimum to consider
MIN_CONFLUENCE_TO_TRADE = 5     # minimum to generate actionable signal
MIN_CONFIDENCE = 0.55
MIN_CONFIDENCE_OUTSIDE_KZ = 0.90
MIN_RR = 3.0
TP_RR = 4.0

KILL_ZONES_UTC = [
    (7, 10, "LONDON_OPEN"),
    (12, 15, "NY_OPEN"),
    (13, 17, "SILVER_BULLET"),
    (7, 12, "EUROPEAN"),
]

# Manipulation types allowed per timeframe
MANIP_VALID_ON_TF = {
    "H1": ["JUDAS_HIGH", "JUDAS_LOW", "EQH_SWEEP", "EQL_SWEEP", "PDH_SWEEP", "PDL_SWEEP", "LIQUIDITY_SWEEP", "ASIAN_HIGH", "ASIAN_LOW"],
    "M15": ["JUDAS_HIGH", "JUDAS_LOW", "EQH_SWEEP", "EQL_SWEEP"],  # NO LIQUIDITY_SWEEP
}


# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class H4Bias:
    direction: str = "NEUTRAL"   # BULL | BEAR | NEUTRAL
    score: float = 0.0           # 0..1 strength
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    bos_choch_count: int = 0
    reasons: List[str] = field(default_factory=list)


@dataclass
class ConfluenceCheck:
    name: str
    met: bool
    score: float = 0.0
    reason: str = ""


@dataclass
class MultiTFSelection:
    direction: str = "NEUTRAL"
    entry_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    confidence: float = 0.0
    entry_type: str = "none"
    h4_bias: Optional[H4Bias] = None
    reasons: List[str] = field(default_factory=list)
    confluence_count: int = 0
    confluence_details: List[str] = field(default_factory=list)
    manipulation_leg: Optional[ManipulationLeg] = None
    stdv_profile: Optional[STDVOTEProfile] = None
    h1_bars_used: int = 0
    m15_bars_used: int = 0
    h4_bars_used: int = 0

    @property
    def is_actionable(self) -> bool:
        return (
            self.direction in ("BULL", "BEAR")
            and self.entry_price is not None
            and self.sl is not None
            and self.tp is not None
            and self.confluence_count >= MIN_CONFLUENCE_TO_TRADE
            and self.confidence >= MIN_CONFIDENCE
        )


# ──────────────────────────────────────────────────────────────────────────────
# H4 Bias — Macro Structure
# ──────────────────────────────────────────────────────────────────────────────

def compute_h4_bias(h4_bars: List[Bar]) -> H4Bias:
    """
    Read H4 bars and determine macro directional bias.

    Logic:
      - Find last 2 swing highs and swing lows
      - Higher highs + higher lows = BULL
      - Lower highs + lower lows = BEAR
      - Count BOS/CHoCH events on H4
      - Score strength by recency and magnitude
    """
    if len(h4_bars) < 6:
        return H4Bias(direction="NEUTRAL", reasons=["insufficient H4 bars"])

    # Simple swing detection on H4
    swings_high: List[Tuple[int, float]] = []  # (idx, price)
    swings_low: List[Tuple[int, float]] = []

    for i in range(2, len(h4_bars) - 2):
        b = h4_bars[i]
        if b.high > h4_bars[i-1].high and b.high > h4_bars[i-2].high and \
           b.high > h4_bars[i+1].high and b.high > h4_bars[i+2].high:
            swings_high.append((i, b.high))
        if b.low < h4_bars[i-1].low and b.low < h4_bars[i-2].low and \
           b.low < h4_bars[i+1].low and b.low < h4_bars[i+2].low:
            swings_low.append((i, b.low))

    if not swings_high or not swings_low:
        return H4Bias(direction="NEUTRAL", reasons=["no clear H4 swings"])

    # Take last 2 of each
    last_sh = swings_high[-2:]
    last_sl = swings_low[-2:]

    sh_higher = len(last_sh) >= 2 and last_sh[-1][1] > last_sh[-2][1]
    sh_lower = len(last_sh) >= 2 and last_sh[-1][1] < last_sh[-2][1]
    sl_higher = len(last_sl) >= 2 and last_sl[-1][1] > last_sl[-2][1]
    sl_lower = len(last_sl) >= 2 and last_sl[-1][1] < last_sl[-2][1]

    direction = "NEUTRAL"
    score = 0.0
    reasons = []

    if sh_higher and sl_higher:
        direction = "BULL"
        score = 0.8
        reasons.append("H4: higher highs + higher lows (BULL)")
    elif sh_lower and sl_lower:
        direction = "BEAR"
        score = 0.8
        reasons.append("H4: lower highs + lower lows (BEAR)")
    elif sh_higher and sl_lower:
        direction = "NEUTRAL"
        score = 0.3
        reasons.append("H4: HH + LL = range/chop (NEUTRAL)")
    elif sh_lower and sl_higher:
        direction = "NEUTRAL"
        score = 0.3
        reasons.append("H4: LH + HL = range/chop (NEUTRAL)")
    else:
        direction = "NEUTRAL"
        score = 0.2
        reasons.append("H4: mixed structure (NEUTRAL)")

    # BOS/CHoCH count (simple: count bars that break prior swing)
    bos_count = 0
    for i in range(1, len(swings_high)):
        if swings_high[i][1] > swings_high[i-1][1]:
            bos_count += 1
    for i in range(1, len(swings_low)):
        if swings_low[i][1] < swings_low[i-1][1]:
            bos_count += 1

    if bos_count >= 2:
        score = min(1.0, score + 0.15)
        reasons.append(f"H4: {bos_count} BOS/CHoCH events (+0.15)")

    return H4Bias(
        direction=direction, score=score,
        swing_high=last_sh[-1][1] if last_sh else None,
        swing_low=last_sl[-1][1] if last_sl else None,
        bos_choch_count=bos_count,
        reasons=reasons,
    )


# ──────────────────────────────────────────────────────────────────────────────
# H1 Manipulation Leg — with timeframe filtering
# ──────────────────────────────────────────────────────────────────────────────

class _MLDBar:
    """Adapter for manipulation_leg_detector internal format."""
    __slots__ = ("time", "o", "h", "l", "c")
    def __init__(self, b):
        self.time = b.time
        self.o = b.open
        self.h = b.high
        self.l = b.low
        self.c = b.close

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

    @property
    def wick_body_ratio(self) -> float:
        body = self.body
        if body <= 0:
            return float('inf')
        return (self.upper_wick + self.lower_wick) / body

    @property
    def close_back_inside(self) -> bool:
        """True if close is inside the body range (not beyond either extreme)."""
        return min(self.o, self.c) <= self.c <= max(self.o, self.c)


def _to_mld(bars):
    return [_MLDBar(b) for b in bars]


def get_h1_manipulation_leg(h1_bars: List[Bar],
                            bias_direction: str = "",
                            pip_size: float = 0.01,
                            min_recent_bars: int = 50) -> ManipulationLeg:
    """
    Detect manipulation legs on H1 timeframe.
    LIQUIDITY_SWEEP is valid here (H1 pools are real).
    """
    mld = _to_mld(h1_bars)
    # We use the existing detector but post-filter to ensure H1-quality legs
    leg = get_primary_manipulation_leg(mld, bias_direction, pip_size, min_recent_bars)
    if not leg.detected:
        return leg

    # Reject LIQUIDITY_SWEEP if the leg doesn't show genuine H1-scale rejection
    if leg.leg_type == "LIQUIDITY_SWEEP":
        # Require the sweep to exceed 50% ATR in excess wick
        # and show close-back inside within 3 bars
        if leg.wick_body_ratio < 1.5 or leg.excess_pips < 2.0:
            return ManipulationLeg(
                detected=False,
                reasons=[f"H1 LIQUIDITY_SWEEP rejected: wick/body={leg.wick_body_ratio:.2f}, excess={leg.excess_pips:.2f} — insufficient H1 rejection"]
            )

    leg.reasons.insert(0, f"H1 manipulation leg validated: {leg.leg_type}")
    return leg


def get_m15_manipulation_leg(m15_bars: List[Bar],
                             bias_direction: str = "",
                             pip_size: float = 0.01,
                             min_recent_bars: int = 20) -> ManipulationLeg:
    """
    Detect manipulation legs on M15 timeframe.
    LIQUIDITY_SWEEP is REJECTED here — M15 LIQUIDITY_SWEEP = noise.
    Only JUDAS_HIGH, JUDAS_LOW, EQH_SWEEP, EQL_SWEEP allowed.
    """
    mld = _to_mld(m15_bars)
    leg = get_primary_manipulation_leg(mld, bias_direction, pip_size, min_recent_bars)
    if not leg.detected:
        return leg

    # STRICT: Reject LIQUIDITY_SWEEP on M15 entirely
    if leg.leg_type == "LIQUIDITY_SWEEP":
        return ManipulationLeg(
            detected=False,
            reasons=["M15 LIQUIDITY_SWEEP rejected — only valid on H1+ timeframe"]
        )

    # For M15, require higher quality: wick/body >= 2.0
    if leg.wick_body_ratio < 2.0:
        return ManipulationLeg(
            detected=False,
            reasons=[f"M15 leg rejected: wick/body={leg.wick_body_ratio:.2f} < 2.0 — M15 requires strong rejection"]
        )

    leg.reasons.insert(0, f"M15 manipulation leg validated: {leg.leg_type}")
    return leg


# ──────────────────────────────────────────────────────────────────────────────
# Kill zone & AMD
# ──────────────────────────────────────────────────────────────────────────────

def _bar_hour_utc(bar: Bar) -> int:
    try:
        return datetime.fromtimestamp(bar.time, tz=timezone.utc).hour
    except Exception:
        return -1


def _is_in_kill_zone(hour: int) -> Tuple[bool, str]:
    for start, end, label in KILL_ZONES_UTC:
        if start <= hour < end:
            return True, label
    return False, ""


def _kill_zone_check(m15_bar: Bar) -> Tuple[bool, str, float]:
    hour = _bar_hour_utc(m15_bar)
    in_kz, label = _is_in_kill_zone(hour)
    if in_kz:
        return True, label, 0.10
    return False, f"hour={hour}", 0.0


def _amd_alignment(amd_phase: str, direction: str) -> Tuple[bool, float, str]:
    if not amd_phase or amd_phase == "ACCUMULATION":
        return False, 0.0, "AMD ACCUMULATION — no directional trades"
    if amd_phase in ("DISTRIBUTION", "LATE_DIST"):
        return True, 0.05, f"AMD {amd_phase} — aligned (+0.05)"
    if amd_phase == "MANIPULATION":
        return True, 0.0, "AMD MANIPULATION — watch for sweep confirmation"
    if amd_phase == "OFF_HOURS":
        return False, 0.0, "AMD OFF_HOURS"
    return True, 0.0, f"AMD {amd_phase} — neutral"


# ──────────────────────────────────────────────────────────────────────────────
# M15 SMC Analysis (OB, FVG, BOS/CHoCH)
# ──────────────────────────────────────────────────────────────────────────────

def analyze_m15(m15_bars: List[Bar]) -> SMCSnapshot:
    """Run SMC analysis on M15 bars for entry confirmation."""
    if len(m15_bars) < 10:
        return SMCSnapshot()
    return analyze(m15_bars)


def _check_m15_bos_choch(snap: SMCSnapshot, direction: str) -> ConfluenceCheck:
    """C4: M15 BOS/CHoCH confirming direction."""
    if not snap.structure:
        return ConfluenceCheck("C4_M15_BOS", False, 0.0, "no M15 structure events")

    # StructureEvent fields: kind ("BOS"|"CHOCH"), direction ("BULL"|"BEAR")
    recent = [e for e in snap.structure if e.kind in ("BOS", "CHOCH")]
    if not recent:
        return ConfluenceCheck("C4_M15_BOS", False, 0.0, "no M15 BOS/CHoCH")

    # Check last 2 events for direction alignment
    for ev in reversed(recent[-3:]):
        if ev.kind == "BOS":
            if direction == "BULL" and ev.direction == "BULL":
                return ConfluenceCheck("C4_M15_BOS", True, 0.10,
                                       f"M15 BOS bullish aligned with BULL bias")
            if direction == "BEAR" and ev.direction == "BEAR":
                return ConfluenceCheck("C4_M15_BOS", True, 0.10,
                                       f"M15 BOS bearish aligned with BEAR bias")
        if ev.kind == "CHOCH":
            if direction == "BULL" and ev.direction == "BULL":
                return ConfluenceCheck("C4_M15_CHoCH", True, 0.12,
                                       f"M15 CHoCH bullish — strong confirmation (+0.12)")
            if direction == "BEAR" and ev.direction == "BEAR":
                return ConfluenceCheck("C4_M15_CHoCH", True, 0.12,
                                       f"M15 CHoCH bearish — strong confirmation (+0.12)")

    # Opposing structure = penalty
    for ev in reversed(recent[-2:]):
        if ev.kind in ("BOS", "CHOCH"):
            if direction == "BULL" and ev.direction == "BEAR":
                return ConfluenceCheck("C4_M15_BOS", False, -0.05,
                                       f"M15 {ev.kind} BEAR opposes BULL — rejected")
            if direction == "BEAR" and ev.direction == "BULL":
                return ConfluenceCheck("C4_M15_BOS", False, -0.05,
                                       f"M15 {ev.kind} BULL opposes BEAR — rejected")

    return ConfluenceCheck("C4_M15_BOS", False, 0.0, "M15 structure not aligned")


def _check_m15_fvg(snap: SMCSnapshot, price: float, direction: str,
                   atr: float) -> ConfluenceCheck:
    """C5: M15 unmitigated FVG present at entry zone."""
    tol = atr * 0.3
    for fvg in snap.fvgs:
        if fvg.mitigated:
            continue
        if direction == "BULL" and fvg.side == "BULL":
            if abs(fvg.bot - price) <= tol or fvg.contains(price):
                return ConfluenceCheck("C5_M15_FVG", True, 0.10,
                                       f"M15 bullish FVG @ {fvg.bot:.5f} near entry {price:.5f}")
        if direction == "BEAR" and fvg.side == "BEAR":
            if abs(fvg.top - price) <= tol or fvg.contains(price):
                return ConfluenceCheck("C5_M15_FVG", True, 0.10,
                                       f"M15 bearish FVG @ {fvg.top:.5f} near entry {price:.5f}")
    return ConfluenceCheck("C5_M15_FVG", False, 0.0,
                           f"no unmitigated M15 FVG near {price:.5f}")


def _check_m15_ob(snap: SMCSnapshot, price: float, direction: str,
                  atr: float) -> ConfluenceCheck:
    """C6: M15 unmitigated OB present at entry zone."""
    tol = atr * 0.3
    for ob in snap.order_blocks:
        if ob.mitigated or ob.side != direction:
            continue
        # OB has various attributes — check the most common ones
        ob_top = getattr(ob, 'top', getattr(ob, 'body_top', getattr(ob, 'high', 0)))
        ob_bot = getattr(ob, 'bot', getattr(ob, 'body_bot', getattr(ob, 'low', 0)))
        mid = (ob_top + ob_bot) / 2
        if abs(mid - price) <= tol:
            return ConfluenceCheck("C6_M15_OB", True, 0.10,
                                   f"M15 {direction} OB near entry {price:.5f}")
    return ConfluenceCheck("C6_M15_OB", False, 0.0,
                           f"no unmitigated M15 {direction} OB near {price:.5f}")


# ──────────────────────────────────────────────────────────────────────────────
# Main Selector — Multi-TF
# ──────────────────────────────────────────────────────────────────────────────

def select_trade_multi_tf(
    h4_bars: List[Bar],
    h1_bars: List[Bar],
    m15_bars: List[Bar],
    current_price: float,
    amd_phase: str = "ACCUMULATION",
    pip_size: float = 0.01,
    rules: Optional[dict] = None,
) -> MultiTFSelection:
    """
    Multi-timeframe confluence selector.

    Args:
      h4_bars:  Last 20-50 H4 bars for macro bias
      h1_bars:  Last 20-50 H1 bars for manipulation leg detection
      m15_bars: Last 20-40 M15 bars for entry confirmation (FVG, BOS/CHoCH)
      current_price: Current market price (for confluence checking)
      amd_phase: Current AMD cycle phase
      pip_size: Symbol pip size
      rules: Optional strategy rules override
    """
    result = MultiTFSelection()
    result.h4_bars_used = len(h4_bars)
    result.h1_bars_used = len(h1_bars)
    result.m15_bars_used = len(m15_bars)

    # ── Step 1: H4 Bias ─────────────────────────────────────────────────────
    h4_bias = compute_h4_bias(h4_bars)
    result.h4_bias = h4_bias
    if h4_bias.direction == "NEUTRAL" or h4_bias.score < 0.3:
        result.reasons.append(f"H4 bias {h4_bias.direction} (score={h4_bias.score:.2f}) — no clear macro direction")
        # Continue but note it — some setups can still form in neutral with strong confluence

    # ── Step 2: H1 Manipulation Leg ─────────────────────────────────────────
    # Use H4 bias direction to prefer aligned manipulation legs
    h1_leg = get_h1_manipulation_leg(h1_bars, h4_bias.direction, pip_size)
    if not h1_leg.detected:
        result.reasons.append("No valid H1 manipulation leg detected")
        return result

    result.manipulation_leg = h1_leg
    leg_dir = h1_leg.direction  # displacement direction (BULL = swept low, goes up)

    # ── Step 3: H4 Bias Alignment (C1) ────────────────────────────────────
    c1 = ConfluenceCheck("C1_H4_BIAS", False, 0.0, "")
    if h4_bias.direction != "NEUTRAL":
        if h4_bias.direction == leg_dir:
            c1 = ConfluenceCheck("C1_H4_BIAS", True, 0.10,
                                 f"H4 bias {h4_bias.direction} aligned with H1 leg direction {leg_dir}")
        else:
            c1 = ConfluenceCheck("C1_H4_BIAS", False, -0.15,
                                 f"H4 bias {h4_bias.direction} OPPOSES H1 leg {leg_dir} — COUNTER-TREND")
            # Counter-trend trades are heavily penalized but not blocked entirely
            # (sometimes the best entries are against weak H4 structure)
    else:
        c1 = ConfluenceCheck("C1_H4_BIAS", True, 0.05,
                             "H4 neutral — no bias conflict (weak C1)")

    # ── Step 4: OTE/STDV Level (C3) ────────────────────────────────────────
    profile = compute_profile(h1_leg, h1_bars)
    result.stdv_profile = profile
    atr = sum(b.high - b.low for b in h1_bars[-14:]) / 14 if len(h1_bars) >= 14 else 5.0

    c3 = ConfluenceCheck("C3_OTE_LEVEL", False, 0.0, "")
    candidates = get_entry_candidates(profile, current_price, leg_dir)
    entry_price = None
    if candidates:
        best = candidates[0]
        lv, dist, qual = best
        entry_price = lv.price
        c3 = ConfluenceCheck("C3_OTE_LEVEL", True, 0.10,
                              f"entry at {lv.name} @ {lv.price:.5f} (quality={qual:.1f})")
    else:
        if is_price_in_ote_zone(profile, current_price):
            entry_price = current_price
            c3 = ConfluenceCheck("C3_OTE_LEVEL", True, 0.07,
                                  f"price in OTE zone (no exact level match)")
        else:
            nearest, dist = nearest_level(profile, current_price, leg_dir)
            if nearest and dist <= atr * 0.5:
                entry_price = nearest.price
                c3 = ConfluenceCheck("C3_OTE_LEVEL", True, 0.05,
                                      f"near {nearest.name} (dist={dist:.5f}, within 0.5 ATR)")
            else:
                c3 = ConfluenceCheck("C3_OTE_LEVEL", False, 0.0,
                                      f"price {current_price:.5f} not near any OTE level")

    # ── Step 5: M15 Confirmation ────────────────────────────────────────────
    m15_snap = analyze_m15(m15_bars)
    c4 = _check_m15_bos_choch(m15_snap, leg_dir)
    c5 = _check_m15_fvg(m15_snap, entry_price or current_price, leg_dir, atr)
    c6 = _check_m15_ob(m15_snap, entry_price or current_price, leg_dir, atr)

    # ── Step 6: H1 Manipulation Leg Quality (C2) ────────────────────────────
    c2 = ConfluenceCheck("C2_H1_MANIP", True, 0.10,
                         f"H1 {h1_leg.leg_type} detected, wick/body={h1_leg.wick_body_ratio:.2f}")
    if h1_leg.leg_type in ("JUDAS_HIGH", "JUDAS_LOW"):
        c2.score = 0.12
        c2.reason = f"H1 {h1_leg.leg_type} — high quality manipulation (+0.12)"
    elif h1_leg.leg_type == "LIQUIDITY_SWEEP":
        c2.score = 0.08
        c2.reason = f"H1 LIQUIDITY_SWEEP — valid on H1 (+0.08)"

    # ── Step 7: Kill Zone (C7) ──────────────────────────────────────────────
    if m15_bars:
        in_kz, kz_label, kz_score = _kill_zone_check(m15_bars[-1])
    else:
        in_kz, kz_label, kz_score = False, "no M15 bars", 0.0
    c7 = ConfluenceCheck("C7_KILL_ZONE", in_kz, kz_score,
                         f"kill zone: {kz_label}" if in_kz else f"outside kill zone: {kz_label}")

    # ── Step 8: AMD (C8) ────────────────────────────────────────────────────
    amd_ok, amd_score, amd_reason = _amd_alignment(amd_phase, leg_dir)
    c8 = ConfluenceCheck("C8_AMD", amd_ok, amd_score, amd_reason)

    # ── Step 9: Aggregate Confluences ───────────────────────────────────────
    checks = [c1, c2, c3, c4, c5, c6, c7, c8]
    met_checks = [c for c in checks if c.met]
    confluence_count = len(met_checks)
    total_score = sum(c.score for c in checks)

    # Counter-trend from H4 blocks execution
    if c1.met is False and c1.score < 0:
        result.reasons.append(f"COUNTER-TREND: H4 {h4_bias.direction} opposes H1 leg {leg_dir}")
        # Still compute but mark as non-actionable unless extreme confluence
        if confluence_count < 6:
            result.confluence_count = confluence_count
            result.confluence_details = [c.reason for c in checks]
            result.direction = leg_dir
            result.confidence = max(0.0, min(1.0, 0.45 + total_score))
            return result

    # ── Step 10: Compute Pricing ────────────────────────────────────────────
    if not entry_price:
        result.reasons.append("No valid entry price from OTE/STDV levels")
        result.confluence_count = confluence_count
        result.confluence_details = [c.reason for c in checks]
        return result

    # SL beyond manipulation leg extreme
    if leg_dir == "BULL":
        sl = h1_leg.wick_low - atr * 0.15
        # TP at opposing liquidity: next H4 swing high or PDH
        tp = h4_bias.swing_high if h4_bias.swing_high else entry_price + atr * 4
    else:
        sl = h1_leg.wick_high + atr * 0.15
        tp = h4_bias.swing_low if h4_bias.swing_low else entry_price - atr * 4

    # R:R check
    risk = abs(entry_price - sl)
    reward = abs(tp - entry_price)
    rr = reward / risk if risk > 0 else 0
    if rr < MIN_RR:
        result.reasons.append(f"R:R {rr:.2f} < minimum {MIN_RR} — TP too close")
        result.confluence_count = confluence_count
        result.confluence_details = [c.reason for c in checks]
        result.direction = leg_dir
        result.entry_price = entry_price
        result.sl = sl
        result.tp = tp
        result.confidence = max(0.0, min(1.0, 0.45 + total_score))
        return result

    # ── Step 11: Confidence ───────────────────────────────────────────────
    base = 0.45
    confidence = base + total_score
    if confluence_count >= 5:
        confidence += 0.05
    if confluence_count >= 6:
        confidence += 0.05
    if h1_leg.leg_type in ("JUDAS_HIGH", "JUDAS_LOW"):
        confidence += 0.05
    confidence = max(0.0, min(1.0, confidence))

    # Outside kill zone penalty
    if not in_kz:
        if confidence < MIN_CONFIDENCE_OUTSIDE_KZ:
            result.reasons.append(f"Outside kill zone and confidence {confidence:.2f} < {MIN_CONFIDENCE_OUTSIDE_KZ} — signal blocked")
            result.confluence_count = confluence_count
            result.confluence_details = [c.reason for c in checks]
            result.direction = leg_dir
            result.entry_price = entry_price
            result.sl = sl
            result.tp = tp
            result.confidence = confidence
            return result
        else:
            confidence -= 0.10

    # ── Build Result ────────────────────────────────────────────────────────
    result.direction = leg_dir
    result.entry_price = round(entry_price, 5)
    result.sl = round(sl, 5)
    result.tp = round(tp, 5)
    result.confidence = round(confidence, 3)
    result.entry_type = "LIMIT"
    result.confluence_count = confluence_count
    result.confluence_details = [c.reason for c in checks]
    result.reasons.append(f"Multi-TF signal: {confluence_count}/8 confluences, confidence={confidence:.2f}, R:R={rr:.1f}:1")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import random
    from datetime import datetime

    def make_bars(n, start_price=3300.0, trend="up", timeframe_min=60):
        bars = []
        price = start_price
        t = datetime(2024, 1, 1, 0, 0, 0).timestamp()
        for i in range(n):
            if trend == "up":
                o = price
                c = price + random.uniform(2, 8)
                h = max(o, c) + random.uniform(0, 3)
                l = min(o, c) - random.uniform(0, 2)
            elif trend == "down":
                o = price
                c = price - random.uniform(2, 8)
                h = max(o, c) + random.uniform(0, 2)
                l = min(o, c) - random.uniform(0, 3)
            else:
                o = price
                c = price + random.uniform(-5, 5)
                h = max(o, c) + random.uniform(0, 3)
                l = min(o, c) - random.uniform(0, 3)

            bars.append(Bar(time=t + i * timeframe_min * 60, open=o, high=h, low=l, close=c))
            price = c
        return bars

    # Create synthetic multi-TF data
    h4 = make_bars(30, 3300.0, "up", 240)   # H4 uptrend
    h1 = make_bars(50, 3300.0, "up", 60)   # H1 uptrend with some pullback
    m15 = make_bars(60, 3300.0, "up", 15)  # M15 uptrend

    # Inject a manipulation leg into H1 (sweep of last few bars' low)
    # Create a fake sweep bar
    sweep_idx = len(h1) - 10
    h1[sweep_idx] = Bar(
        time=h1[sweep_idx].time,
        open=h1[sweep_idx].open,
        high=h1[sweep_idx].high + 15,
        low=h1[sweep_idx].low - 5,
        close=h1[sweep_idx].open - 2,  # close back inside
    )

    sel = select_trade_multi_tf(h4, h1, m15, current_price=3320.0,
                                 amd_phase="DISTRIBUTION", pip_size=0.01)

    print("=" * 60)
    print("MULTI-TF SELECTOR SELF-TEST")
    print("=" * 60)
    print(f"H4 Bias:        {sel.h4_bias.direction if sel.h4_bias else 'N/A'} (score={sel.h4_bias.score if sel.h4_bias else 0})")
    print(f"Direction:      {sel.direction}")
    print(f"Entry:          {sel.entry_price}")
    print(f"SL:             {sel.sl}")
    print(f"TP:             {sel.tp}")
    print(f"Confidence:     {sel.confidence}")
    print(f"Confluences:    {sel.confluence_count}/8")
    print(f"Actionable:     {sel.is_actionable}")
    print("\nConfluence Details:")
    for d in sel.confluence_details:
        print(f"  · {d}")
    print("\nReasons:")
    for r in sel.reasons:
        print(f"  · {r}")
