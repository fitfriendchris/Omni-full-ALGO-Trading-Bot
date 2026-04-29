"""
Scalp + Pyramid Engine

Two modes layered on top of the standard ICT setup execution:

1. ACCUMULATION SCALP MODE
   During Asia/ACCUMULATION phase (where standard rules block all non-SWEEP
   entries), this engine permits high-confidence scalp-style entries with:
     - Tight SL (default: 5 pips)
     - Small TP (default: 8 pips, ~1.5R)
     - Reduced risk (default: 0.25% of equity, vs 1% normal)
     - Higher confidence floor (default: 75)
   The idea: Asia is the range — scalp the chop while waiting for London.

2. PYRAMID SCALE MODE
   During DISTRIBUTION phase, when an existing trade is in profit ≥ 0.5R,
   this engine looks for pivot-bounce continuations in the same direction
   and queues "pyramid" entries that:
     - Use parent's SL (they close together if reverse)
     - Decay in size (each new pyramid = half the prior)
     - Stop when opposing liquidity is swept (sweep_detected → halt pyramiding)
     - Cap at 3 adds per parent trade
   The idea: ride the distribution wave by scaling into the winner until
   the market mechanic that launched the move (sweep of opposing liquidity)
   completes — at which point the move is mature and we stop adding.

Public API:
- ScalpDecision dataclass
- evaluate_accumulation_scalp(setup, state, atr, params) → ScalpDecision
- evaluate_pyramid_scale(parent_trade, current_price, pivots, sweep_detected,
                         params, current_pyramid_count) → ScalpDecision

Both functions are pure — no I/O, no MT5 calls. The caller (auto_trader.py)
applies the decision via place_order / close_position.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import logging

log = logging.getLogger(__name__)


# Defaults — overridable via params dict from rules.json or learned_parameters.json
DEFAULT_SCALP_PARAMS = {
    # Accumulation scalp gate
    "scalp_min_conf":          75,         # Only A/A+ confidence
    "scalp_tp_pips":           8.0,        # Tight target
    "scalp_sl_pips":           5.0,        # Tight stop
    "scalp_risk_pct":          0.25,       # Quarter of normal risk per trade
    "scalp_max_per_session":   4,          # Cap scalps per session (prevent overtrading)
    "scalp_min_rr":            1.4,        # Modest RR — scalps don't need 2:1
    "scalp_allowed_phases":    ("ACCUMULATION", "DISTRIBUTION", "MANIPULATION"),
    "scalp_block_after_loss":  True,       # Stop scalping after a scalp loss in same session

    # Pyramid scale gate
    "pyramid_min_profit_r":    0.5,        # Parent must be ≥ 0.5R in profit
    "pyramid_max_adds":        3,          # Max 3 pyramid scales per parent
    "pyramid_lot_decay":       0.5,        # Each new add = 0.5 × previous lot
    "pyramid_min_pivot_bounce": 0.0003,    # Pivot bounce must be ≥ 3 pips clear
    "pyramid_min_conf":        65,         # Pyramid setup must still be decent
    "pyramid_required_phase":  ("DISTRIBUTION", "MANIPULATION"),
    "pyramid_stop_on_sweep":   True,       # Halt pyramiding when opposing liquidity swept
}


@dataclass
class ScalpDecision:
    """Result of a scalp/pyramid evaluation."""
    accept:          bool
    mode:            str = "NONE"          # "ACCUM_SCALP" / "PYRAMID" / "NONE"
    lot_multiplier:  float = 1.0           # Multiplier on standard lot for sizing
    tp_pips:         float = 0.0           # Override TP if non-zero
    sl_pips:         float = 0.0           # Override SL if non-zero
    risk_pct:        float = 0.0           # Override risk_pct if non-zero
    parent_ticket:   str = ""              # For PYRAMID: parent trade ticket
    pyramid_count:   int = 0               # For PYRAMID: which add # this is (1, 2, 3)
    reason:          str = ""              # Human-readable rationale
    metadata:        Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def reject(cls, reason: str) -> "ScalpDecision":
        return cls(accept=False, reason=reason)


# ─────────────────────────────────────────────────────────────────────────
# Mode 1: Accumulation Scalp
# ─────────────────────────────────────────────────────────────────────────

def evaluate_accumulation_scalp(
    setup: Any,
    state: Any,
    atr: float = 0.001,
    params: Optional[Dict] = None,
    current_phase: str = "",
    scalps_taken_this_session: int = 0,
) -> ScalpDecision:
    """
    Decide whether to take this setup as a scalp during ACCUMULATION (or any
    phase where standard rules would block it).

    Args:
        setup: ICTSetup-like object with .symbol, .direction, .confidence,
               .entry_price, .sl_price, .entry_type
        state: TraderState (for sym_loss_streak, session_loss_streak)
        atr: current ATR for SL sizing fallback
        params: optional override of DEFAULT_SCALP_PARAMS
        current_phase: AMD phase ("ACCUMULATION" / "MANIPULATION" / "DISTRIBUTION")
        scalps_taken_this_session: how many scalps already opened this session

    Returns: ScalpDecision (accept=True if it should be taken as a scalp)
    """
    p = {**DEFAULT_SCALP_PARAMS, **(params or {})}

    # Phase gate
    if current_phase and current_phase not in p["scalp_allowed_phases"]:
        return ScalpDecision.reject(f"phase {current_phase} not in scalp_allowed_phases")

    # Confidence floor
    conf = int(getattr(setup, "confidence", 0) or 0)
    if conf < p["scalp_min_conf"]:
        return ScalpDecision.reject(f"conf {conf} below scalp floor {p['scalp_min_conf']}")

    # Per-session cap
    if scalps_taken_this_session >= p["scalp_max_per_session"]:
        return ScalpDecision.reject(
            f"scalp cap reached: {scalps_taken_this_session} ≥ {p['scalp_max_per_session']}"
        )

    # Per-symbol loss streak — refuse to scalp a symbol that's been losing
    sym = getattr(setup, "symbol", "")
    if state is not None:
        sym_losses = getattr(state, "sym_loss_streak", {}).get(sym, 0)
        if sym_losses >= 2:
            return ScalpDecision.reject(f"{sym} has {sym_losses} consecutive losses today")
        # Optional: refuse to scalp if last close in this session was a loss
        if p["scalp_block_after_loss"]:
            recent = getattr(state, "recent_closes", [])
            session = getattr(setup, "session", "")
            recent_in_session = [r for r in recent if r.get("session") == session]
            if recent_in_session and recent_in_session[-1].get("outcome") == "LOSS":
                # Only block if the very last close in this session was a loss
                return ScalpDecision.reject(
                    f"last close in {session} was LOSS — pause scalping"
                )

    # SL/TP sizing — prefer scalp pip values, but never tighter than 1×ATR for SL
    sl_pips = max(p["scalp_sl_pips"], atr * 10000 * 0.6)  # 60% of ATR-pips, floor at param
    tp_pips = p["scalp_tp_pips"]
    rr = tp_pips / sl_pips if sl_pips > 0 else 0
    if rr < p["scalp_min_rr"]:
        return ScalpDecision.reject(f"scalp RR {rr:.2f} below floor {p['scalp_min_rr']}")

    return ScalpDecision(
        accept=True,
        mode="ACCUM_SCALP",
        lot_multiplier=1.0,
        tp_pips=tp_pips,
        sl_pips=sl_pips,
        risk_pct=p["scalp_risk_pct"],
        reason=f"Scalp accepted: conf={conf}, phase={current_phase}, "
               f"RR={rr:.1f} (SL {sl_pips:.1f}p / TP {tp_pips:.1f}p), "
               f"risk={p['scalp_risk_pct']}%",
        metadata={
            "scalps_taken": scalps_taken_this_session,
            "phase":        current_phase,
            "atr":          atr,
        },
    )


# ─────────────────────────────────────────────────────────────────────────
# Mode 2: Pyramid Scale on Winning Distribution Trades
# ─────────────────────────────────────────────────────────────────────────

def _pivot_bounce_in_direction(
    pivots: List[Dict],
    current_price: float,
    direction: str,
    min_clearance: float,
) -> Optional[Dict]:
    """
    Has price just bounced off a pivot in `direction`?
    Returns the pivot dict if a fresh same-direction bounce is detected.

    A "bounce" = current_price has moved at least `min_clearance` away from
    a recent pivot level in the trade's direction.
    """
    best = None
    best_clearance = 0.0
    for pivot in pivots or []:
        level = float(pivot.get("level") or pivot.get("price") or 0)
        if level == 0:
            continue
        if direction == "BUY":
            # For BUY: pivot should be BELOW price, price has bounced up
            clearance = current_price - level
            if clearance >= min_clearance and clearance > best_clearance:
                best = pivot
                best_clearance = clearance
        elif direction == "SELL":
            # For SELL: pivot should be ABOVE price, price has bounced down
            clearance = level - current_price
            if clearance >= min_clearance and clearance > best_clearance:
                best = pivot
                best_clearance = clearance
    return best


def evaluate_pyramid_scale(
    parent_trade: Dict,
    current_price: float,
    pivots: List[Dict],
    sweep_detected: bool = False,
    params: Optional[Dict] = None,
    current_pyramid_count: int = 0,
    current_phase: str = "",
    parent_initial_lot: float = 0.0,
) -> ScalpDecision:
    """
    Decide whether to add a pyramid scale to an existing winning trade.

    Args:
        parent_trade: dict with at least:
            - ticket, symbol, direction, entry, current_sl, current_profit_r
        current_price: live bid/ask of the symbol
        pivots: list of {level, tf, ...} — current pivots on M5/M15
        sweep_detected: True if opposing liquidity has been swept (stop pyramiding)
        params: optional override
        current_pyramid_count: how many pyramids already added to this parent
        current_phase: AMD phase
        parent_initial_lot: parent's initial lot size for decay calculation

    Returns: ScalpDecision (accept=True if a pyramid add is justified)
    """
    p = {**DEFAULT_SCALP_PARAMS, **(params or {})}

    # Phase gate
    if current_phase and current_phase not in p["pyramid_required_phase"]:
        return ScalpDecision.reject(f"phase {current_phase} not in pyramid_required_phase")

    # Sweep gate — if opposing liquidity was just swept, stop pyramiding (move is mature)
    if p["pyramid_stop_on_sweep"] and sweep_detected:
        return ScalpDecision.reject("opposing liquidity swept — stop pyramiding (move mature)")

    # Cap on adds
    if current_pyramid_count >= p["pyramid_max_adds"]:
        return ScalpDecision.reject(
            f"pyramid cap reached: {current_pyramid_count} ≥ {p['pyramid_max_adds']}"
        )

    # Parent must be in profit
    profit_r = float(parent_trade.get("current_profit_r") or 0)
    if profit_r < p["pyramid_min_profit_r"]:
        return ScalpDecision.reject(
            f"parent profit {profit_r:+.2f}R below floor {p['pyramid_min_profit_r']}"
        )

    direction = str(parent_trade.get("direction", "")).upper()
    if direction not in ("BUY", "SELL"):
        return ScalpDecision.reject(f"unknown direction {direction}")

    # Find a same-direction pivot bounce
    bounce = _pivot_bounce_in_direction(
        pivots, current_price, direction, p["pyramid_min_pivot_bounce"]
    )
    if bounce is None:
        return ScalpDecision.reject("no fresh same-direction pivot bounce")

    # Compute decayed lot size: parent_lot × (decay ^ pyramid_count)
    decay = float(p["pyramid_lot_decay"])
    lot_multiplier = decay ** (current_pyramid_count + 1)
    if lot_multiplier <= 0.05:  # Floor — too small to bother
        return ScalpDecision.reject(f"decayed lot multiplier {lot_multiplier:.3f} below 0.05 floor")

    return ScalpDecision(
        accept=True,
        mode="PYRAMID",
        lot_multiplier=lot_multiplier,
        # No TP/SL override — pyramid uses parent's SL; TP managed by parent's structure
        parent_ticket=str(parent_trade.get("ticket", "")),
        pyramid_count=current_pyramid_count + 1,
        reason=(
            f"Pyramid add #{current_pyramid_count + 1}: parent {direction} at "
            f"{profit_r:+.2f}R, bounce off pivot {bounce.get('level', 0):.5f} "
            f"({bounce.get('tf','?')}), lot×{lot_multiplier:.3f}"
        ),
        metadata={
            "bounce_pivot":     bounce.get("level"),
            "bounce_tf":        bounce.get("tf"),
            "parent_profit_r":  profit_r,
            "phase":            current_phase,
            "parent_initial_lot": parent_initial_lot,
        },
    )


# ─────────────────────────────────────────────────────────────────────────
# Sweep detection helper — used by manage_open_trades
# ─────────────────────────────────────────────────────────────────────────

def detect_opposing_liquidity_swept(
    parent_trade: Dict,
    bars: List[Any],
    swing_levels: Optional[List[float]] = None,
) -> bool:
    """
    Detects whether the move has reached the "opposing liquidity" target,
    signaling the move is mature and pyramiding should stop.

    Heuristic:
    - For BUY: "opposing" = sell-side liquidity = recent swing-highs above entry
    - For SELL: "opposing" = buy-side liquidity = recent swing-lows below entry

    A "sweep" = price has wicked through one of those swing levels by ≥ 1 pip
    in the last 5 bars.

    Args:
        parent_trade: dict with direction, entry
        bars: recent bars (need at least 5)
        swing_levels: optional pre-computed swing levels (high or low) to test

    Returns: True if opposing liquidity has been swept
    """
    if not bars or len(bars) < 5:
        return False
    direction = str(parent_trade.get("direction", "")).upper()
    entry = float(parent_trade.get("entry", 0))
    if entry == 0:
        return False

    # If no explicit swing levels passed, derive from bars (last 50 bars)
    if not swing_levels:
        recent = bars[-50:]
        if direction == "BUY":
            # Sell-side liquidity = highs above entry
            swing_levels = sorted({float(b.high if hasattr(b, "high") else b.get("high", 0))
                                  for b in recent}, reverse=True)
            swing_levels = [s for s in swing_levels if s > entry][:5]
        else:
            swing_levels = sorted({float(b.low if hasattr(b, "low") else b.get("low", 0))
                                  for b in recent})
            swing_levels = [s for s in swing_levels if s < entry][:5]

    if not swing_levels:
        return False

    last_5 = bars[-5:]
    for b in last_5:
        bh = float(b.high if hasattr(b, "high") else b.get("high", 0))
        bl = float(b.low  if hasattr(b, "low")  else b.get("low",  0))
        if direction == "BUY":
            for lvl in swing_levels:
                if bh >= lvl + 0.0001:  # 1 pip clearance
                    return True
        else:
            for lvl in swing_levels:
                if bl <= lvl - 0.0001:
                    return True
    return False
