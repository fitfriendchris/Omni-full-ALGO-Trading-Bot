"""
scaling_engine.py — Position scaling decisions for OMNI-ICT (Phase 2).

Purpose
-------
Given an open position + current market context, decide whether to:
  · ADD to the winner (pyramid up on confirmation)
  · REDUCE exposure (de-risk into the move)
  · HOLD (no change)
  · CLOSE (exit entirely — rare, usually the trail engine does this)

This runs AFTER the trailing stop decision, NOT instead of it. Scaling is
about *size adjustment*, not *risk management*. The trail engine already
protects the downside via stop; this engine looks for confluence to
pyramid on strength or trim on weakening structure.

Inputs
------
    position   : PositionCtx    — current open position state
    htf        : SMCSnapshot    — higher timeframe analysis
    ltf        : SMCSnapshot    — lower timeframe analysis
    rules      : dict           — rules.json subsection `scaling`

Output
------
    ScaleAction — {action, size_multiplier, reason, details}

Design notes
------------
 * Pyramid (ADD) fires on: fresh BOS in direction + price retraced into new
   OB/FVG + current profit_r ≥ `min_add_profit_r`. Max `max_adds` per position.
 * Reduce (REDUCE) fires on: LTF CHoCH against position + price at extension
   target (≥ `reduce_at_r` in profit). Scales out by `reduce_frac`.
 * Close (CLOSE) is conservative — only on clear structural reversal AND price
   beyond entry by > `close_at_adverse_r` (rare; usually trail catches this).
 * All thresholds in rules.json; defaults inline.

Deterministic, pure-function. No broker I/O.

Run `python scaling_engine.py` for self-test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Literal

from smc_engine import Bar, SMCSnapshot, analyze

log = logging.getLogger(__name__)

Action = Literal["ADD", "REDUCE", "HOLD", "CLOSE"]


# ──────────────────────────────────────────────────────────────────────────────
# Inputs / outputs
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionCtx:
    """Snapshot of an open position the engine needs to reason about."""
    symbol:        str
    direction:     Literal["BULL", "BEAR"]   # matches smc_engine side labels
    entry_price:   float
    current_price: float
    initial_sl:    float                     # SL at trade open (defines 1R)
    current_sl:    float                     # latest SL (possibly trailed)
    volume:        float                     # current lot size
    add_count:     int = 0                   # how many scale-in actions already executed

    @property
    def profit_r(self) -> float:
        """Current profit in R-multiples, based on initial risk."""
        risk = abs(self.entry_price - self.initial_sl)
        if risk <= 1e-12:
            return 0.0
        if self.direction == "BULL":
            return (self.current_price - self.entry_price) / risk
        else:
            return (self.entry_price - self.current_price) / risk


@dataclass
class ScaleAction:
    action:            Action
    size_multiplier:   float = 1.0         # applied to original volume (ADD/REDUCE)
    reason:            str = ""
    details:           dict = field(default_factory=dict)

    @property
    def is_no_op(self) -> bool:
        return self.action == "HOLD"


DEFAULT_RULES: dict = {
    "scaling": {
        "enabled":             True,
        "max_adds":            2,            # hard cap on pyramid count
        "min_add_profit_r":    1.0,          # require +1R before first add
        "add_size_frac":       0.5,          # each ADD is 50% of original size
        "add_require_bos":     True,         # require BOS in same direction since last add
        "reduce_at_r":         2.0,          # trim once ≥2R in profit…
        "reduce_frac":         0.33,         # …by 33% of current size
        "reduce_on_ltf_choch": True,         # only trim on LTF reversal signal
        "close_at_adverse_r":  -0.2,         # only close if already adverse
        "close_on_htf_reversal": True,
        # Tiered pyramid sizing: each tier = (min_profit_r, size_fraction_of_original)
        "add_size_ladder": [
            [1.0, 0.50],   # 1st add at +1.0R: 50% of original
            [1.5, 0.33],   # 2nd add at +1.5R: 33% of original
            [2.0, 0.25],   # 3rd add at +2.0R: 25% of original
        ],
    }
}


# ──────────────────────────────────────────────────────────────────────────────
# Core evaluator
# ──────────────────────────────────────────────────────────────────────────────

def _get_add_params(add_count: int, rules: dict) -> tuple[float, float]:
    """Return (min_profit_r, size_fraction) for the next pyramid tier."""
    ladder = rules.get("add_size_ladder", [[1.0, 0.5], [1.5, 0.33], [2.0, 0.25]])
    idx = min(add_count, len(ladder) - 1)
    entry = ladder[idx]
    return float(entry[0]), float(entry[1])


def _opposite(direction: str) -> str:
    return "BEAR" if direction == "BULL" else "BULL"


def _last_structure_in_direction(snap: SMCSnapshot, direction: str):
    for ev in reversed(snap.structure):
        if ev.direction == direction:
            return ev
    return None


def _htf_reversal_against(snap: SMCSnapshot, direction: str) -> bool:
    """True if the LATEST HTF structure event is a CHoCH in the opposite direction."""
    ev = snap.last_event
    if ev is None:
        return False
    return ev.kind == "CHOCH" and ev.direction == _opposite(direction)


def _ltf_reversal_against(snap: SMCSnapshot, direction: str) -> bool:
    """True if any CHoCH against `direction` exists in the last part of LTF."""
    opp = _opposite(direction)
    return any(ev.kind == "CHOCH" and ev.direction == opp for ev in snap.structure[-3:])


def _price_at_unmitigated_zone(snap: SMCSnapshot, direction: str,
                                price: float, atr_val: float,
                                proximity_frac: float = 0.25) -> bool:
    """True if `price` is in/near an unmitigated OB or FVG in `direction`."""
    tol = atr_val * proximity_frac
    for ob in snap.order_blocks:
        if ob.mitigated or ob.side != direction:
            continue
        if (ob.bot - tol) <= price <= (ob.top + tol):
            return True
    for f in snap.fvgs:
        if f.mitigated or f.side != direction:
            continue
        if (f.bot - tol) <= price <= (f.top + tol):
            return True
    return False


def evaluate(pos: PositionCtx, htf: SMCSnapshot, ltf: SMCSnapshot,
             rules: dict = None) -> ScaleAction:
    """
    Decide ADD / REDUCE / HOLD / CLOSE for `pos`.
    """
    from smc_engine import atr as _atr

    rules = rules or DEFAULT_RULES
    cfg = (rules.get("scaling") or {}) if isinstance(rules, dict) else {}

    if not cfg.get("enabled", True):
        return ScaleAction(action="HOLD", reason="scaling disabled")

    max_adds        = int(cfg.get("max_adds", 2))
    # min_add_r and add_frac come from ladder per tier; keep flat fallback for compat
    add_require_bos = bool(cfg.get("add_require_bos", True))
    reduce_at_r    = float(cfg.get("reduce_at_r", 2.0))
    reduce_frac    = float(cfg.get("reduce_frac", 0.33))
    reduce_on_choch = bool(cfg.get("reduce_on_ltf_choch", True))
    close_adv_r    = float(cfg.get("close_at_adverse_r", -0.2))
    close_on_htf   = bool(cfg.get("close_on_htf_reversal", True))

    # ── 1) CLOSE on clear HTF reversal while adverse ──────────────────────
    if close_on_htf and _htf_reversal_against(htf, pos.direction) \
            and pos.profit_r <= close_adv_r:
        return ScaleAction(
            action="CLOSE",
            size_multiplier=0.0,
            reason="HTF CHoCH against position while in drawdown",
            details={"profit_r": pos.profit_r},
        )

    # ── 2) REDUCE when ≥reduce_at_r AND LTF reverses (or reduce-on-r alone) ─
    if pos.profit_r >= reduce_at_r:
        if not reduce_on_choch or _ltf_reversal_against(ltf, pos.direction):
            return ScaleAction(
                action="REDUCE",
                size_multiplier=max(0.0, 1.0 - reduce_frac),
                reason=f"profit_r={pos.profit_r:.2f} ≥ {reduce_at_r} and LTF shows reversal",
                details={"profit_r": pos.profit_r, "reduce_frac": reduce_frac},
            )

    # ── 3) ADD on pullback into a fresh zone with momentum confirmation ─
    min_add_r, add_frac = _get_add_params(pos.add_count, cfg)
    can_add = (pos.add_count < max_adds) and (pos.profit_r >= min_add_r)
    if can_add:
        # Use period=7 for LTF ATR (faster-responding than default 14)
        ltf_atr = _atr(ltf.bars, period=7) if ltf.bars else 0.0
        if ltf_atr > 0 and _price_at_unmitigated_zone(
            ltf, pos.direction, pos.current_price, ltf_atr, proximity_frac=0.25
        ):
            if add_require_bos:
                bos_ok = any(
                    ev.kind in ("BOS", "CHoCH") and ev.direction == pos.direction
                    for ev in (htf.structure or [])[-3:]
                )
                if not bos_ok:
                    return ScaleAction(action="HOLD",
                                       reason="pullback but no recent BOS on HTF")
            return ScaleAction(
                action="ADD",
                size_multiplier=add_frac,
                reason=f"pullback into {pos.direction} zone with BOS confluence (tier {pos.add_count})",
                details={"profit_r": pos.profit_r, "add_count": pos.add_count,
                         "max_adds": max_adds, "size_frac": add_frac},
            )

    return ScaleAction(action="HOLD", reason="no scaling condition met",
                       details={"profit_r": pos.profit_r,
                                "add_count": pos.add_count})


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from smc_engine import _make_fixture
    bars = _make_fixture()
    snap = analyze(bars)

    # Simulate a BULL position that entered earlier and is now in +1.5R profit
    last_price = bars[-1].close
    entry = last_price - 0.0060
    initial_sl = entry - 0.0040    # 1R = 0.0040
    pos = PositionCtx(
        symbol="EURUSD", direction="BULL",
        entry_price=entry, current_price=last_price,
        initial_sl=initial_sl, current_sl=initial_sl,
        volume=1.0, add_count=0,
    )
    assert pos.profit_r > 0, f"expected positive profit_r, got {pos.profit_r}"
    act = evaluate(pos, snap, snap)
    print(f"[OK] profit_r={pos.profit_r:.2f} action={act.action} mult={act.size_multiplier:.2f} reason='{act.reason}'")

    # Adverse HTF scenario — flip direction so CHoCH(BULL) becomes a reversal
    # against a BEAR position in drawdown
    pos_adv = PositionCtx(
        symbol="EURUSD", direction="BEAR",
        entry_price=last_price - 0.0010,   # entered just below current
        current_price=last_price,          # in drawdown (price > entry for BEAR)
        initial_sl=last_price - 0.0010 + 0.0040,
        current_sl=last_price - 0.0010 + 0.0040,
        volume=1.0, add_count=0,
    )
    act2 = evaluate(pos_adv, snap, snap)
    print(f"[OK] adverse profit_r={pos_adv.profit_r:.2f} action={act2.action} reason='{act2.reason}'")


if __name__ == "__main__":
    _self_test()
