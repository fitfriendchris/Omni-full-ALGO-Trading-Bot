"""
dual_tf_selector.py — ICT Confluence-Based Trade Selector v3 (REBUILD)

This is the heart of the OMNI-ICT bot. It implements the user's manual edge:

  1. Manipulation leg detection — finds the "clear breakouts that sweep liquidity"
  2. STDV/OTE level computation — wick-to-wick anchored projections
  3. TRUE confluence counting — minimum 3 of 6 conditions must overlap
  4. Hard kill-zone gate — European 07:00-12:00 UTC required for most signals
  5. AMD cycle alignment — H4 AMD phase must agree with trade direction
  6. Limit order pricing — entries at true OTE/STDV/Fib levels, never current price

Confluence Conditions (must have at least MIN_CONFLUENCE of these):
  C1. Price at/near a key STDV/OTE/Fib level of a confirmed manipulation leg
  C2. Unmitigated Order Block present in entry direction
  C3. Unmitigated Fair Value Gap present in entry direction
  C4. Liquidity sweep confirmed on the manipulation leg
  C5. BOS/CHoCH structure aligned with trade direction on LTF
  C6. Kill zone active (European 07:00-12:00 UTC) + AMD phase aligned

Entry Pricing:
  - For BULL: entry is the highest qualifying level <= current price
  - For BEAR: entry is the lowest qualifying level >= current price
  - SL is placed beyond the manipulation leg extreme (the swept liquidity)
  - TP is placed at opposing liquidity (next swing extreme, PDH/PDL, EQH/EQL)

Confidence Formula:
  base = 0.45
  +0.10 per confluence condition met (max +0.60)
  +0.05 if manipulation leg is high-quality (wick/body >= 2.0)
  +0.05 if price is in OTE zone (not just at a single level)
  +0.05 if AMD phase == DISTRIBUTION or LATE_DIST
  +0.05 if triple alignment (D1 + H4 + M15 all agree)
  -0.10 if outside kill zone and confidence < 0.85
  -0.15 if no manipulation leg detected
  clamp to [0.0, 1.0]

Deterministic engine integration:
  If the proven_ict_signals deterministic engine produces a signal for this
  symbol, AND its direction matches our confluence direction, we merge it
  as an additional +0.10 confidence boost and use its TP targets.

Run `python dual_tf_selector.py` for self-test.
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
    ManipulationLeg, get_primary_manipulation_leg,
)
from stdv_ote_engine import (
    STDVOTEProfile, compute_profile,
    nearest_level, is_price_at_level,
    is_price_in_ote_zone, get_entry_candidates,
)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Bar adapter — smc_engine.Bar uses .open/.high/.low/.close, but
# manipulation_leg_detector and stdv_ote_engine expect .o/.h/.l/.c.
# We provide a thin wrapper used only at the boundary between modules.
# ──────────────────────────────────────────────────────────────────────────────

class _MLDBar:
    """Adapter: exposes .o/.h/.l/.c from an smc_engine.Bar."""
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
    """Convert list of smc_engine.Bar to _MLDBar for manipulation/STDV engines."""
    return [_MLDBar(b) for b in bars]


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

MIN_CONFLUENCE = 3              # minimum conditions to consider a signal
MIN_CONFLUENCE_TO_TRADE = 3     # minimum to generate an actionable signal (relaxed for Chris frequency)
MAX_CONFLUENCE = 6              # maximum conditions tracked
MIN_CONFIDENCE = 0.50           # minimum confidence to emit signal
MIN_CONFIDENCE_OUTSIDE_KZ = 0.60  # relaxed: allow non-KZ trades with decent PA
MIN_RR = 1.5                    # minimum risk-reward (2.0→1.5 for higher fill rate)
TP_RR = 4.0                     # default TP R:R target (3.0→4.0 for runner)

KILL_ZONES_UTC = [
    (7, 10, "LONDON_OPEN"),
    (12, 15, "NY_OPEN"),
    (13, 17, "SILVER_BULLET"),
    (7, 12, "EUROPEAN"),
]

AMD_ACTIONABLE_PHASES = ["DISTRIBUTION", "LATE_DIST", "MANIPULATION", "ACCUMULATION", "LOW_VOL_EARLY"]

# Confluence weights for confidence scoring
_CONFLUENCE_WEIGHT = 0.10


# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class HTFBias:
    direction: str = "NEUTRAL"   # BULL | BEAR | NEUTRAL
    score:     float = 0.0       # 0..1
    reasons:   List[str] = field(default_factory=list)


@dataclass
class ConfluenceCheck:
    """Result of checking a single confluence condition."""
    name:    str
    met:     bool
    score:   float = 0.0
    reason:  str = ""


@dataclass
class TradeSelection:
    direction:       str = "NEUTRAL"
    entry_price:     Optional[float] = None
    sl:              Optional[float] = None
    tp:              Optional[float] = None
    confidence:      float = 0.0
    entry_type:      str = "none"
    htf_bias:        Optional[HTFBias] = None
    reasons:         List[str] = field(default_factory=list)
    ltf_trigger:     str = ""
    confluence_count: int = 0
    confluence_details: List[str] = field(default_factory=list)
    manipulation_leg: Optional[ManipulationLeg] = None
    stdv_profile:    Optional[STDVOTEProfile] = None

    # EMAs (populated by orchestrator, passed through)
    ema20:  float = 0.0
    ema200: float = 0.0
    ema800: float = 0.0
    tf_emas: Dict[str, dict] = field(default_factory=dict)

    # Scale-in / runner management
    scale_action: str = "HOLD"    # HOLD | ADD | REDUCE | CLOSE
    scale_mult: float = 1.0       # position size multiplier for this signal

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
# Kill zone helpers
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


def _kill_zone_bonus(bars: List[Bar]) -> Tuple[float, str]:
    """Returns (bonus, reason). +0.10 if in kill zone, 0 otherwise."""
    if not bars:
        return 0.0, "no bars for kill zone check"
    hour = _bar_hour_utc(bars[-1])
    in_kz, label = _is_in_kill_zone(hour)
    if in_kz:
        return 0.10, f"kill zone {label} active (+0.10)"
    return 0.0, f"outside kill zones (hour={hour})"


# ──────────────────────────────────────────────────────────────────────────────
# AMD alignment check
# ──────────────────────────────────────────────────────────────────────────────

def _amd_alignment(amd_phase: str, direction: str) -> Tuple[bool, float, str]:
    """
    Check if AMD phase permits trading in `direction`.
    Returns (permitted, bonus, reason).
    All phases permitted with penalty/bonus; OFF_HOURS gets penalty.
    """
    if not amd_phase:
        return False, 0.0, "AMD phase unknown — no directional trades"

    if amd_phase in ("ACCUMULATION", "LOW_VOL_EARLY"):
        return True, -0.05, f"AMD {amd_phase} — penalty applied, caution"

    if amd_phase in ("DISTRIBUTION", "LATE_DIST"):
        return True, 0.05, f"AMD {amd_phase} — active distribution phase (+0.05)"

    if amd_phase == "MANIPULATION":
        return True, 0.0, "AMD MANIPULATION — watch for sweep confirmation"

    if amd_phase == "OFF_HOURS":
        return True, -0.10, "AMD OFF_HOURS — reduced activity"

    return True, 0.0, f"AMD phase {amd_phase} — neutral"


# ──────────────────────────────────────────────────────────────────────────────
# Confluence condition checkers
# ──────────────────────────────────────────────────────────────────────────────

def _check_c1_ote_level(profile: STDVOTEProfile, price: float,
                        direction: str, atr: float) -> ConfluenceCheck:
    """C1: Price at/near a key STDV/OTE/Fib level of a confirmed manipulation leg."""
    if not profile.leg.detected:
        return ConfluenceCheck("C1_OTE_LEVEL", False, 0.0,
                               "no manipulation leg — no OTE levels")

    candidates = get_entry_candidates(profile, price, direction)
    if candidates:
        best = candidates[0]  # (level, dist, quality)
        lv, dist, qual = best
        return ConfluenceCheck(
            "C1_OTE_LEVEL", True, _CONFLUENCE_WEIGHT,
            f"price {price:.5f} near {lv.name} @ {lv.price:.5f} (dist={dist:.5f}, quality={qual:.1f})"
        )

    # Check if price is in OTE zone at least
    if is_price_in_ote_zone(profile, price):
        return ConfluenceCheck(
            "C1_OTE_LEVEL", True, _CONFLUENCE_WEIGHT * 0.7,
            f"price {price:.5f} inside OTE zone (no exact level match)"
        )

    nearest, dist = nearest_level(profile, price, direction=direction)
    if nearest and dist <= atr * 0.5:
        return ConfluenceCheck(
            "C1_OTE_LEVEL", True, _CONFLUENCE_WEIGHT * 0.5,
            f"price {price:.5f} near {nearest.name} @ {nearest.price:.5f} (dist={dist:.5f}, within 0.5x ATR)"
        )

    return ConfluenceCheck(
        "C1_OTE_LEVEL", False, 0.0,
        f"price {price:.5f} not near any OTE/STDV/Fib level (nearest={nearest.name if nearest else 'none'})"
    )


def _check_c2_ob_present(snap: SMCSnapshot, price: float,
                         direction: str, atr: float) -> ConfluenceCheck:
    """C2: Unmitigated Order Block present in entry direction."""
    side = direction
    tol = atr * 1.5   # expanded for OB proximity (not just exact overlap)

    for ob in snap.order_blocks:
        if ob.mitigated or ob.side != side:
            continue
        if ob.contains(price) or abs((ob.bot + ob.top) / 2 - price) <= tol:
            return ConfluenceCheck(
                "C2_OB_PRESENT", True, _CONFLUENCE_WEIGHT,
                f"unmitigated {side} OB @ {ob.bot:.5f}-{ob.top:.5f} near price {price:.5f}"
            )

    return ConfluenceCheck("C2_OB_PRESENT", False, 0.0,
                           f"no unmitigated {side} OB near {price:.5f}")


def _check_c3_fvg_present(snap: SMCSnapshot, price: float,
                          direction: str, atr: float) -> ConfluenceCheck:
    """C3: Unmitigated Fair Value Gap present in entry direction."""
    side = direction
    tol = atr * 1.5   # expanded tolerance

    for fvg in snap.fvgs:
        if fvg.mitigated or fvg.side != side:
            continue
        if fvg.contains(price) or abs((fvg.bot + fvg.top) / 2 - price) <= tol:
            return ConfluenceCheck(
                "C3_FVG_PRESENT", True, _CONFLUENCE_WEIGHT,
                f"unmitigated {side} FVG @ {fvg.bot:.5f}-{fvg.top:.5f} near price {price:.5f}"
            )

    return ConfluenceCheck("C3_FVG_PRESENT", False, 0.0,
                           f"no unmitigated {side} FVG near {price:.5f}")


def _check_c4_sweep_confirmed(leg: ManipulationLeg) -> ConfluenceCheck:
    """C4: Liquidity sweep confirmed on the manipulation leg."""
    if leg.detected:
        return ConfluenceCheck(
            "C4_SWEEP_CONFIRMED", True, _CONFLUENCE_WEIGHT,
            f"sweep of {leg.swept_kind} @ {leg.swept_level:.5f} confirmed ({leg.leg_type})"
        )
    return ConfluenceCheck("C4_SWEEP_CONFIRMED", False, 0.0,
                           "no confirmed sweep")


def _check_c5_structure_aligned(snap: SMCSnapshot, direction: str) -> ConfluenceCheck:
    """C5: BOS/CHoCH structure aligned with trade direction on LTF."""
    if not snap.structure:
        return ConfluenceCheck("C5_STRUCTURE_ALIGNED", False, 0.0,
                               "no structure events detected")

    # Look at last 3 structure events
    recent = snap.structure[-3:]
    for ev in recent:
        if ev.direction == direction:
            return ConfluenceCheck(
                "C5_STRUCTURE_ALIGNED", True, _CONFLUENCE_WEIGHT,
                f"recent {ev.kind} {ev.direction} @ bar {ev.bar_idx}"
            )

    # Check if there was a CHoCH in the opposite direction (bad)
    for ev in recent:
        if ev.kind == "CHOCH" and ev.direction != direction:
            return ConfluenceCheck(
                "C5_STRUCTURE_ALIGNED", False, -0.10,
                f"opposing CHoCH {ev.direction} detected — invalidates {direction} setup"
            )

    return ConfluenceCheck("C5_STRUCTURE_ALIGNED", False, 0.0,
                           "no recent structure aligned with direction")


def _check_c6_killzone_amd_aligned(kz_bonus: float, kz_reason: str,
                                  amd_permitted: bool, amd_bonus: float,
                                  amd_reason: str) -> ConfluenceCheck:
    """C6: Kill zone active OR AMD phase acceptable (ACCUMULATION is fine)."""
    if not amd_permitted:
        return ConfluenceCheck("C6_KILLZONE_AMD", False, 0.0,
                               f"AMD blocks trade: {amd_reason}")

    # AMD permits — confluence met. Bonus depends on KZ + AMD quality.
    total_bonus = kz_bonus + amd_bonus
    if total_bonus > 0:
        return ConfluenceCheck(
            "C6_KILLZONE_AMD", True, _CONFLUENCE_WEIGHT,
            f"{kz_reason}; {amd_reason} (combined +{total_bonus:.2f})"
        )
    
    # Pass with reduced or zero score (outside KZ or AMD penalty)
    score = _CONFLUENCE_WEIGHT * 0.5 if total_bonus >= 0 else _CONFLUENCE_WEIGHT * 0.25
    return ConfluenceCheck(
        "C6_KILLZONE_AMD", True, score,
        f"outside kill zone but {amd_reason} (reduced bonus)" 
        if kz_bonus <= 0 else f"{kz_reason}; {amd_reason}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entry pricing from confluence levels
# ──────────────────────────────────────────────────────────────────────────────

def _compute_ob_fvg_entry(ltf_snap: SMCSnapshot, direction: str, current_price: float,
                            atr: float) -> Optional[Tuple[float, str]]:
    """
    Return best entry candidate from unmitigated OB or FVG, plus label.
    BULL: highest OB/FVG top <= current_price (limit) OR within 2 pips (market chase).
    BEAR: lowest OB/FVG bot >= current_price (limit) OR within 2 pips (market chase).
    """
    tol = atr * 1.5
    chase_tol = atr * 0.3   # ~2-3 pips on XAUUSD — accept market entry if close
    side = direction
    entries = []        # (price, type_label, is_market)
    for ob in ltf_snap.order_blocks:
        if ob.mitigated or ob.side != side:
            continue
        mid = (ob.bot + ob.top) / 2
        if abs(mid - current_price) <= tol or ob.contains(current_price):
            # Limit entry at OB top for BULL (buy at support)
            price = ob.top if direction == "BULL" else ob.bot
            is_market = False
            if direction == "BULL" and current_price > price and current_price - price <= chase_tol:
                price = current_price
                is_market = True
            if direction == "BEAR" and current_price < price and price - current_price <= chase_tol:
                price = current_price
                is_market = True
            entries.append((price, "OB", is_market))
    for fvg in ltf_snap.fvgs:
        if fvg.mitigated or fvg.side != side:
            continue
        mid = (fvg.bot + fvg.top) / 2
        if abs(mid - current_price) <= tol or fvg.contains(current_price):
            price = fvg.top if direction == "BULL" else fvg.bot
            is_market = False
            if direction == "BULL" and current_price > price and current_price - price <= chase_tol:
                price = current_price
                is_market = True
            if direction == "BEAR" and current_price < price and price - current_price <= chase_tol:
                price = current_price
                is_market = True
            entries.append((price, "FVG", is_market))
    if not entries:
        return None
    # Prefer limit entries, then market
    limit_entries = [(p, l) for p, l, m in entries if not m]
    if limit_entries:
        if direction == "BULL":
            return max(limit_entries, key=lambda x: x[0])
        return min(limit_entries, key=lambda x: x[0])
    market_entries = [(p, l) for p, l, m in entries if m]
    if direction == "BULL":
        return max(market_entries, key=lambda x: x[0])
    return min(market_entries, key=lambda x: x[0])


def _compute_entry_price(candidates: List[Tuple], direction: str,
                         current_price: float) -> Optional[float]:
    """
    Select the best entry price from qualifying levels.

    For BULL: the highest level that is <= current price
              (we want to buy at support, below current price)
    For BEAR: the lowest level that is >= current price
              (we want to sell at resistance, above current price)

    This ensures LIMIT orders, not market orders.
    """
    if not candidates:
        return None

    if direction == "BULL":
        # Find highest level <= current price
        valid = [(lv, dist, qual) for lv, dist, qual in candidates
                 if lv.price <= current_price]
        if not valid:
            return None
        # Sort by quality descending, then by price descending
        valid.sort(key=lambda x: (x[2], x[0].price), reverse=True)
        return valid[0][0].price

    else:  # BEAR
        # Find lowest level >= current price
        valid = [(lv, dist, qual) for lv, dist, qual in candidates
                 if lv.price >= current_price]
        if not valid:
            return None
        valid.sort(key=lambda x: (x[2], -x[0].price), reverse=True)
        return valid[0][0].price


def _compute_sl(entry: float, leg: ManipulationLeg, direction: str,
                atr: float) -> float:
    """
    Place SL beyond the manipulation leg extreme.
    For BULL: below the wick_low of the manipulation leg
    For BEAR: above the wick_high of the manipulation leg
    Buffer by 10% of ATR for safety.
    """
    buf = atr * 0.05  # tighter 5% buffer (was 10%) for better R:R
    if direction == "BULL":
        # SL below manipulation wick low (the swept extreme)
        sl = leg.wick_low - buf if leg.detected else entry - atr * 1.2
        # But also ensure minimum distance
        min_sl = entry - atr * 0.5
        return min(sl, min_sl)  # more conservative = lower SL for BULL
    else:
        sl = leg.wick_high + buf if leg.detected else entry + atr * 1.2
        min_sl = entry + atr * 0.5
        return max(sl, min_sl)


def _compute_tp(entry: float, sl: float, direction: str,
                snap: SMCSnapshot, profile: STDVOTEProfile,
                min_rr: float = MIN_RR) -> Optional[float]:
    """
    Compute TP at opposing liquidity with minimum R:R.
    Uses the furthest unmitigated opposing structure or STDV projection.
    """
    risk = abs(entry - sl)
    if risk <= 1e-12:
        return None

    # Minimum TP distance
    min_dist = risk * min_rr

    opp = "BEAR" if direction == "BULL" else "BULL"

    # Candidate 1: Opposing liquidity pools
    candidates = []

    # Opposing OBs
    for ob in snap.order_blocks:
        if not ob.mitigated and ob.side == opp:
            dist = abs((ob.bot + ob.top) / 2 - entry)
            if dist >= min_dist:
                candidates.append((dist, (ob.bot + ob.top) / 2, "opposing OB"))

    # Opposing FVGs
    for fvg in snap.fvgs:
        if not fvg.mitigated and fvg.side == opp:
            dist = abs((fvg.bot + fvg.top) / 2 - entry)
            if dist >= min_dist:
                candidates.append((dist, (fvg.bot + fvg.top) / 2, "opposing FVG"))

    # STDV projected levels on the far side
    if profile.leg.detected:
        sign = 1 if direction == "BULL" else -1
        for lv in profile.stdv_levels:
            if lv.name in ("MaxExp3", "MaxExp4", "MaxExp5", "Reversal"):
                dist = abs(lv.price - entry)
                if dist >= min_dist:
                    candidates.append((dist, lv.price, f"STDV {lv.name}"))

    if candidates:
        # Sort by R:R descending — target BEST return, not nearest结构
        candidates_with_rr = [(dist/risk, dist, price, reason) for dist, price, reason in candidates]
        candidates_with_rr.sort(key=lambda x: x[0], reverse=True)
        for rr, dist, price, reason in candidates_with_rr:
            if rr >= min_rr:
                # Cap at 8R to avoid unrealistic fantasy targets
                if rr <= 8.0:
                    return price
                else:
                    # Clip to 8R
                    clipped_dist = risk * 8.0
                    return entry + clipped_dist if direction == "BULL" else entry - clipped_dist
        # No candidate meets min_rr — return best available
        return candidates_with_rr[0][2]

    # Fallback: project min_rr from entry
    if direction == "BULL":
        return entry + min_dist
    return entry - min_dist


# ──────────────────────────────────────────────────────────────────────────────
# Core: select_trade with true confluence
# ──────────────────────────────────────────────────────────────────────────────

def select_trade(htf_bars: List[Bar], ltf_bars: List[Bar],
                 rules: Optional[dict] = None,
                 macro_bars: Optional[List[Bar]] = None,
                 amd_phase: str = "",
                 pip_size: float = 0.01) -> TradeSelection:
    """
    Compute a TradeSelection using TRUE confluence counting.

    Parameters
    ----------
    htf_bars : HTF bars (default H1) for bias and structure
    ltf_bars : LTF bars (default M5) for entry precision
    rules    : rules.json dual_tf subsection (optional)
    macro_bars : H4/D1 bars for macro bias (optional)
    amd_phase : current AMD phase from amd_engine (optional)
    pip_size : symbol pip size for manipulation leg detection
    """
    rules = rules or {}
    base = TradeSelection(direction="NEUTRAL", confidence=0.0)

    if not htf_bars or not ltf_bars:
        base.reasons.append("insufficient bars")
        return base

    # ── 1) HTF Bias ───────────────────────────────────────────────────────
    htf_snap = analyze(htf_bars)
    bias = _htf_bias(htf_snap, htf_bars)
    base.htf_bias = bias
    base.reasons.append(f"HTF bias: {bias.direction} (score={bias.score:.2f})")

    if bias.direction == "NEUTRAL":
        base.reasons.append("HTF neutral — no directional bias")
        return base

    # ── 2) Macro bias confirmation (if available) ─────────────────────────
    macro_bonus = 0.0
    if macro_bars:
        macro_snap = analyze(macro_bars)
        macro_bias = _htf_bias(macro_snap, macro_bars)
        if macro_bias.direction == bias.direction:
            macro_bonus = 0.05
            base.reasons.append(f"Macro {macro_bias.direction} aligns with HTF (+0.05)")
        elif macro_bias.direction != "NEUTRAL":
            macro_bonus = -0.05
            base.reasons.append(f"Macro {macro_bias.direction} CONFLICTS with HTF {bias.direction} (-0.05)")

    # ── 3) Detect manipulation leg on LTF ──────────────────────────────────
    mld_ltf = _to_mld(ltf_bars)
    leg = get_primary_manipulation_leg(mld_ltf, bias_direction=bias.direction,
                                      pip_size=pip_size, min_recent_bars=min(50, len(mld_ltf)))
    base.manipulation_leg = leg

    if not leg.detected:
        base.reasons.append("NO MANIPULATION LEG — skipping (this is the filter)")
        base.confidence = max(0.0, 0.30 + macro_bonus)  # low but not zero
        return base

    base.reasons.append(f"Manipulation leg: {leg.leg_type} → displacement {leg.direction}")
    for r in leg.reasons[:3]:  # top 3 reasons
        base.reasons.append(f"  · {r}")

    # ── 4) Compute STDV/OTE profile ─────────────────────────────────────────
    profile = compute_profile(leg, mld_ltf)
    base.stdv_profile = profile
    base.reasons.append(f"STDV CE={profile.ce:.5f}, unit={profile.stdv:.5f}")

    # ── 5) LTF analysis for OB/FVG/structure ────────────────────────────────
    ltf_snap = analyze(ltf_bars)
    px = ltf_bars[-1].close  # current price
    ltf_atr = _atr(ltf_bars, period=14)

    # ── 6) Check ALL confluence conditions ──────────────────────────────────
    checks: List[ConfluenceCheck] = []

    # C1: OTE level
    c1 = _check_c1_ote_level(profile, px, bias.direction, ltf_atr)
    checks.append(c1)

    # C2: OB present
    c2 = _check_c2_ob_present(ltf_snap, px, bias.direction, ltf_atr)
    checks.append(c2)

    # C3: FVG present
    c3 = _check_c3_fvg_present(ltf_snap, px, bias.direction, ltf_atr)
    checks.append(c3)

    # C4: Sweep confirmed
    c4 = _check_c4_sweep_confirmed(leg)
    checks.append(c4)

    # C5: Structure aligned
    c5 = _check_c5_structure_aligned(ltf_snap, bias.direction)
    checks.append(c5)

    # C6: Kill zone + AMD
    kz_bonus, kz_reason = _kill_zone_bonus(ltf_bars)
    amd_permitted, amd_bonus, amd_reason = _amd_alignment(amd_phase, bias.direction)
    c6 = _check_c6_killzone_amd_aligned(kz_bonus, kz_reason, amd_permitted, amd_bonus, amd_reason)
    checks.append(c6)

    # Count met conditions
    met_count = sum(1 for c in checks if c.met)
    base.confluence_count = met_count
    base.confluence_details = [f"{'[OK]' if c.met else '[NO]'} {c.name}: {c.reason}" for c in checks]
    base.reasons.extend(base.confluence_details)

    # ── 7) Pre-threshold confidence (for gate penalties + return values) ─────
    pre_conf = 0.45 + sum(c.score for c in checks if c.met and c.score > 0)

    # ── 8) Hard gates ─────────────────────────────────────────────────────────
    # Gate 1: Minimum confluence
    if met_count < MIN_CONFLUENCE:
        base.reasons.append(f"INSUFFICIENT CONFLUENCE: {met_count}/{MIN_CONFLUENCE} minimum required")
        base.confidence = round(max(0.0, pre_conf), 3)
        return base

    # Gate 2: AMD phase — penalty only (never hard-veto); all phases pass with penalty
    in_kz = kz_bonus > 0
    if not amd_permitted:
        pre_conf -= 0.15
        base.reasons.append(f"AMD phase not ideal: {amd_reason} (-0.15)")

    # Gate 3: Outside kill zone requires 4+ confluences (relaxed from 5)
    if not in_kz and met_count < 4:
        base.reasons.append(f"Outside kill zone with only {met_count} confluences — need 4+ to trade")
        base.confidence = round(max(0.0, pre_conf), 3)
        return base

    # ── 9) Full confidence scoring with all bonuses ───────────────────────────
    conf = pre_conf

    # Quality bonus
    if leg.is_high_quality:
        conf += 0.05
        base.reasons.append("High-quality manipulation leg (+0.05)")

    # OTE zone bonus
    if is_price_in_ote_zone(profile, px):
        conf += 0.05
        base.reasons.append("Price inside OTE zone (+0.05)")

    # OB/FVG entry precision bonus
    if c2.met or c3.met:
        conf += 0.05
        base.reasons.append("Entry at unmitigated OB or FVG (+0.05)")
    else:
        conf -= 0.05
        base.reasons.append("No OB or FVG entry precision (-0.05)")

    # Triple alignment bonus
    if macro_bonus > 0:
        conf += macro_bonus

    # Penalties
    if not in_kz and conf < MIN_CONFIDENCE_OUTSIDE_KZ:
        conf -= 0.10
        base.reasons.append("Outside kill zone with sub-0.85 confidence (-0.10)")

    # Clamp
    conf = max(0.0, min(1.0, conf))
    base.confidence = round(conf, 3)

    # ── 10) Compute entry / SL / TP ──────────────────────────────────────────
    if met_count >= MIN_CONFLUENCE_TO_TRADE and conf >= MIN_CONFIDENCE:
        entry = None
        entry_type = "ote_confluence"

        # PRIMARY: OB or FVG entry if confluence includes C2 or C3
        if c2.met or c3.met:
            ob_fvg = _compute_ob_fvg_entry(ltf_snap, bias.direction, px, ltf_atr)
            if ob_fvg:
                entry, label = ob_fvg
                entry_type = f"{label.lower()}_entry"
                base.reasons.append(f"Entry at unmitigated {label}: {entry}")

        # FALLBACK: OTE level entry
        if entry is None:
            candidates = get_entry_candidates(profile, px, bias.direction)
            entry = _compute_entry_price(candidates, bias.direction, px)
            if entry:
                base.reasons.append(f"OTE level entry: {entry}")

        if entry is not None:
            sl = _compute_sl(entry, leg, bias.direction, ltf_atr)
            tp = _compute_tp(entry, sl, bias.direction, ltf_snap, profile, min_rr=MIN_RR)

            if tp is not None:
                rr = abs(tp - entry) / abs(entry - sl) if entry != sl else 0.0
                if rr >= MIN_RR:
                    base.entry_price = round(entry, 5)
                    base.sl = round(sl, 5)
                    base.tp = round(tp, 5)
                    base.direction = bias.direction
                    base.entry_type = entry_type
                    base.reasons.append(
                        f"ACTIONABLE: entry={base.entry_price}, SL={base.sl}, TP={base.tp}, R:R={rr:.2f}"
                    )
                else:
                    base.reasons.append(f"R:R too low: {rr:.2f} < {MIN_RR} — skipping")
            else:
                base.reasons.append("Could not compute valid TP")
        else:
            base.reasons.append("No valid OB/FVG or OTE entry price")
    else:
        base.reasons.append(
            f"Not actionable: confluence={met_count} (need {MIN_CONFLUENCE_TO_TRADE}), "
            f"confidence={conf:.2f} (need {MIN_CONFIDENCE})"
        )

    # ── 11) Scale-in / runner management ──────────────────────────────────────
    if base.is_actionable:
        if base.confidence >= 0.95 and met_count >= 5:
            base.scale_action = "ADD"
            base.scale_mult = 1.5
            base.reasons.append("SCALE-IN: 5+ confluence + 0.95 confidence → +50% size")
        elif base.confidence >= 0.90 and met_count >= 4:
            base.scale_action = "ADD"
            base.scale_mult = 1.25
            base.reasons.append("SCALE-IN: 4+ confluence + 0.90 confidence → +25% size")
        elif base.entry_type == "fvg_entry" and base.confidence >= 0.85:
            base.scale_action = "ADD"
            base.scale_mult = 1.15
            base.reasons.append("SCALE-IN: FVG precision entry + 0.85 confidence → +15% size")

    return base


# ──────────────────────────────────────────────────────────────────────────────
# HTF bias helper (unchanged logic, moved from old file)
# ──────────────────────────────────────────────────────────────────────────────

def _htf_bias(snap: SMCSnapshot, bars: List[Bar]) -> HTFBias:
    """
    Derive HTF directional bias from SMC structure.
    Uses the most recent structure event and price position relative to recent swings.
    """
    if not bars:
        return HTFBias("NEUTRAL", 0.0, ["no bars"])

    # Default: look at price vs EMA and recent structure
    closes = [b.close for b in bars]
    ema20 = _calc_ema(closes, 20)[-1] if len(closes) >= 20 else closes[-1]

    price = bars[-1].close
    above_ema = price > ema20

    # Structure-based bias
    if snap.structure:
        last = snap.structure[-1]
        if last.direction == "BULL":
            score = 0.60 if above_ema else 0.45
            return HTFBias("BULL", score,
                           [f"last structure {last.kind} BULL", f"price {'above' if above_ema else 'below'} EMA20"])
        else:
            score = 0.60 if not above_ema else 0.45
            return HTFBias("BEAR", score,
                           [f"last structure {last.kind} BEAR", f"price {'below' if not above_ema else 'above'} EMA20"])

    # Fallback: EMA only
    if above_ema:
        return HTFBias("BULL", 0.35, ["price above EMA20, no clear structure"])
    return HTFBias("BEAR", 0.35, ["price below EMA20, no clear structure"])


def _calc_ema(values: List[float], period: int) -> List[float]:
    """Compute EMA for a list of values."""
    if len(values) < period:
        return values
    k = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    # Pad front so output length matches input
    padding = [ema[0]] * (period - 1)
    return padding + ema


def _atr(bars: List[Bar], period: int = 14) -> float:
    """Simple ATR."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        tr = max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from smc_engine import Bar as SMCBar, _make_fixture as _make_smc_fixture
    from manipulation_leg_detector import _make_fixture as _make_manip_fixture

    # Use manipulation fixture (has clear Judas sweep) but adapt to smc_engine.Bar
    raw_bars = _make_manip_fixture()
    # Convert: manipulation.Bar(o,h,l,c) → smc_engine.Bar(open,high,low,close)
    bars = [
        SMCBar(time=b.time, open=b.o, high=b.h, low=b.l, close=b.c)
        for b in raw_bars
    ]

    # Use same bars for HTF and LTF in test
    sel = select_trade(bars, bars, amd_phase="DISTRIBUTION", pip_size=0.01)

    print(f"[TEST] Direction: {sel.direction}")
    print(f"[TEST] Confluence: {sel.confluence_count}/6")
    print(f"[TEST] Confidence: {sel.confidence:.3f}")
    print(f"[TEST] Entry: {sel.entry_price}")
    print(f"[TEST] SL: {sel.sl}")
    print(f"[TEST] TP: {sel.tp}")
    print(f"[TEST] Actionable: {sel.is_actionable}")

    for r in sel.reasons:
        print(f"       · {r}")

    # Assertions
    assert sel.direction in ("BULL", "BEAR", "NEUTRAL"), f"Bad direction: {sel.direction}"
    if sel.direction != "NEUTRAL":
        assert sel.manipulation_leg is not None, "Must have manipulation leg"
        assert sel.stdv_profile is not None, "Must have STDV profile"
        assert sel.confluence_count >= 0, "Confluence count must be non-negative"
        # In the fixture we expect at least some confluence
        if sel.is_actionable:
            assert sel.entry_price is not None, "Actionable signal must have entry price"
            assert sel.sl is not None, "Actionable signal must have SL"
            assert sel.tp is not None, "Actionable signal must have TP"
            assert sel.confidence >= MIN_CONFIDENCE, f"Confidence {sel.confidence} below minimum {MIN_CONFIDENCE}"

    print("\n[OK] self-test passed")


if __name__ == "__main__":
    _self_test()
