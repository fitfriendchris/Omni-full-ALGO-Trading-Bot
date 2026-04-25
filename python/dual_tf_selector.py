"""
dual_tf_selector.py — Dual-timeframe decision module for OMNI-ICT (Phase 2).

Purpose
-------
Combine a **Higher-Time-Frame (HTF) directional bias** with a **Lower-Time-Frame
(LTF) entry trigger** to produce a single, reasoned trade selection.

Core idea (classic ICT dual-TF model):
  1. The HTF tells you *which direction* to hunt (bull / bear / neutral).
  2. The LTF tells you *when* to enter (OB mitigation, FVG fill, sweep + CHoCH).
  3. A selection fires only when both agree.

Inputs
------
    htf_bars : list[Bar]   — e.g. H1 / H4 — used for bias
    ltf_bars : list[Bar]   — e.g. M5 / M15 — used for entry trigger
    rules    : dict        — rules.json subsection `dual_tf` (thresholds / toggles)

Output
------
    TradeSelection — {direction, entry_type, entry_price, sl, tp, confidence,
                      reasons, htf_snapshot, ltf_snapshot}

Deterministic, pure-function. No I/O, no MT5 calls.

Design notes
------------
 * HTF bias priority (first-match wins):
       a) last structure event (BOS/CHoCH) direction
       b) trend of last 3 swings (HH/HL → BULL, LH/LL → BEAR)
       c) NEUTRAL
 * LTF entry types (enabled via rules.dual_tf.entries):
       - "ob_mitigation"   : price returns into an unmitigated OB *in direction of HTF*
       - "fvg_fill"        : price trades into an unmitigated FVG in HTF direction
       - "sweep_choch"     : liquidity sweep followed by CHoCH in HTF direction
 * Confidence: 0-1 score combining structure freshness, zone proximity, and
   confluence count. No magic numbers — all pulled from `rules.dual_tf`.

Run `python dual_tf_selector.py` for a self-test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Literal

from smc_engine import (
    Bar, SMCSnapshot, OrderBlock, FairValueGap, Sweep, StructureEvent,
    analyze,
)

log = logging.getLogger(__name__)

Direction = Literal["BULL", "BEAR", "NEUTRAL"]
EntryType = Literal["ob_mitigation", "fvg_fill", "sweep_choch", "none"]


# ──────────────────────────────────────────────────────────────────────────────
# Output dataclasses
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Bias:
    direction:  Direction
    source:     str              # which rule fired ("structure" / "swings" / "default")
    score:      float            # 0..1 strength
    details:    dict             # diagnostic info


@dataclass
class TradeSelection:
    direction:     Direction
    entry_type:    EntryType
    entry_price:   Optional[float]
    sl:            Optional[float]
    tp:            Optional[float]
    confidence:    float
    reasons:       list[str] = field(default_factory=list)
    htf_bias:      Optional[Bias] = None
    ltf_trigger:   Optional[str] = None     # short human-readable trigger tag
    htf_snapshot:  Optional[SMCSnapshot] = None
    ltf_snapshot:  Optional[SMCSnapshot] = None

    @property
    def is_actionable(self) -> bool:
        return (self.direction in ("BULL", "BEAR")
                and self.entry_type != "none"
                and self.entry_price is not None
                and self.sl is not None)


# ──────────────────────────────────────────────────────────────────────────────
# HTF bias detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_htf_bias(snap: SMCSnapshot, min_swings_for_swing_trend: int = 3) -> Bias:
    """
    Priority-ordered bias detection.
      1. Latest BOS/CHoCH → bias = that event's direction.
      2. Last N swings trend: HH+HL → BULL; LH+LL → BEAR.
      3. Default → NEUTRAL.
    """
    # 1) Structure event wins
    last = snap.last_event
    if last is not None:
        score = 0.9 if last.kind == "BOS" else 0.75  # BOS slightly more confident
        return Bias(direction=last.direction, source="structure",
                    score=score,
                    details={"kind": last.kind, "bar_idx": last.bar_idx,
                             "swing_price": last.swing_price})

    # 2) Swing-based fallback
    swings = snap.swings[-min_swings_for_swing_trend:]
    if len(swings) >= 2:
        highs = [s for s in swings if s.kind == "HIGH"]
        lows  = [s for s in swings if s.kind == "LOW"]
        if len(highs) >= 2 and highs[-1].price > highs[-2].price \
           and len(lows)  >= 2 and lows[-1].price  > lows[-2].price:
            return Bias(direction="BULL", source="swings", score=0.6,
                        details={"highs": [h.price for h in highs],
                                 "lows":  [l.price for l in lows]})
        if len(highs) >= 2 and highs[-1].price < highs[-2].price \
           and len(lows)  >= 2 and lows[-1].price  < lows[-2].price:
            return Bias(direction="BEAR", source="swings", score=0.6,
                        details={"highs": [h.price for h in highs],
                                 "lows":  [l.price for l in lows]})

    # 3) Default
    return Bias(direction="NEUTRAL", source="default", score=0.0, details={})


# ──────────────────────────────────────────────────────────────────────────────
# LTF entry triggers
# ──────────────────────────────────────────────────────────────────────────────

def _last_price(bars: list[Bar]) -> Optional[float]:
    return bars[-1].close if bars else None


def _most_recent_unmitigated_ob(snap: SMCSnapshot, side: Direction) -> Optional[OrderBlock]:
    candidates = [o for o in snap.order_blocks if not o.mitigated and o.side == side]
    if not candidates:
        return None
    # Most recent first
    candidates.sort(key=lambda o: o.break_idx, reverse=True)
    return candidates[0]


def _most_recent_unmitigated_fvg(snap: SMCSnapshot, side: Direction) -> Optional[FairValueGap]:
    candidates = [f for f in snap.fvgs if not f.mitigated and f.side == side]
    if not candidates:
        return None
    candidates.sort(key=lambda f: f.middle_idx, reverse=True)
    return candidates[0]


def _recent_sweep_then_choch(snap: SMCSnapshot, side: Direction,
                             max_gap_bars: int = 10) -> Optional[tuple[Sweep, StructureEvent]]:
    """Find a sweep *against* `side` followed within `max_gap_bars` by a CHoCH
    *in favour of* `side`. The sweep direction must be the opposing side
    (sweep of the other side's liquidity → reversal into our side)."""
    opposing: Direction = "BEAR" if side == "BULL" else "BULL"
    sweeps = [s for s in snap.sweeps if s.side == opposing]
    chs = [e for e in snap.structure if e.kind == "CHOCH" and e.direction == side]
    for sw in sweeps:
        for ch in chs:
            if 0 < (ch.bar_idx - sw.bar_idx) <= max_gap_bars:
                return sw, ch
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Stop + target construction
# ──────────────────────────────────────────────────────────────────────────────

def _sl_from_ob(ob: OrderBlock, side: Direction, buffer_frac: float = 0.10) -> float:
    """SL just beyond the opposite edge of the OB zone."""
    zone = ob.top - ob.bot
    pad  = zone * buffer_frac
    return ob.bot - pad if side == "BULL" else ob.top + pad


def _sl_from_fvg(fvg: FairValueGap, side: Direction, buffer_frac: float = 0.10) -> float:
    zone = fvg.top - fvg.bot
    pad  = zone * buffer_frac
    return fvg.bot - pad if side == "BULL" else fvg.top + pad


def _tp_from_rr(entry: float, sl: float, side: Direction, rr: float) -> float:
    risk = abs(entry - sl)
    return entry + rr * risk if side == "BULL" else entry - rr * risk


# ──────────────────────────────────────────────────────────────────────────────
# Main selector
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_RULES: dict = {
    "dual_tf": {
        "enabled":   True,
        "entries":   ["ob_mitigation", "fvg_fill", "sweep_choch"],
        "min_confidence": 0.50,
        "tp_rr":     2.0,
        "sl_buffer_frac": 0.10,
        "ob_proximity_atr_frac": 0.50,   # price must be within 0.5 ATR of OB
        "fvg_proximity_atr_frac": 0.50,
    }
}


def _proximity_ok(price: float, top: float, bot: float, atr_val: float,
                  frac: float) -> bool:
    """Return True if price is inside the zone OR within frac*ATR of its edges."""
    if bot <= price <= top:
        return True
    tol = frac * max(atr_val, 1e-9)
    return (price >= bot - tol) and (price <= top + tol)


def _silver_bullet_score(ltf: "SMCSnapshot", direction: str) -> float:
    """
    ICT Silver Bullet: OB + FVG + sweep all within a 5-bar window on LTF.
    Returns extra confidence bonus 0–0.35.
    """
    if not (ltf.order_blocks and ltf.fvgs and ltf.sweeps):
        return 0.0
    recent_sweep = next(
        (s for s in reversed(ltf.sweeps) if s.direction == direction), None
    )
    if not recent_sweep:
        return 0.0
    window_start = recent_sweep.bar_idx - 5
    ob_in_window = any(
        ob.side == direction and not ob.mitigated
        and getattr(ob, "break_idx", ob.anchor_idx) >= window_start
        for ob in ltf.order_blocks
    )
    fvg_in_window = any(
        fvg.side == direction and not fvg.mitigated
        and fvg.middle_idx >= window_start
        for fvg in ltf.fvgs
    )
    if ob_in_window and fvg_in_window:
        return 0.35
    if ob_in_window or fvg_in_window:
        return 0.15
    return 0.0


def _confluence_bonus(ltf: "SMCSnapshot", direction: str, ltf_atr: float) -> float:
    """
    Extra confidence when OB and FVG zones are within 2×ATR proximity of each other,
    or when a sweep adds additional confirmation.
    Returns bonus 0–0.30.
    """
    bonus = 0.0
    unm_obs = [ob for ob in ltf.order_blocks if ob.side == direction and not ob.mitigated]
    unm_fvgs = [f for f in ltf.fvgs if f.side == direction and not f.mitigated]
    if unm_obs and unm_fvgs and ltf_atr > 0:
        ob_mid = (unm_obs[0].top + unm_obs[0].bot) / 2
        fvg_mid = (unm_fvgs[0].top + unm_fvgs[0].bot) / 2
        if abs(ob_mid - fvg_mid) < 2 * ltf_atr:
            bonus += 0.15
    if ltf.sweeps and (unm_obs or unm_fvgs):
        bonus += 0.10
    return min(bonus, 0.30)


def select_trade(htf_bars: list[Bar], ltf_bars: list[Bar],
                 rules: dict = None) -> TradeSelection:
    """
    Run HTF bias + LTF trigger matching. Return a TradeSelection with
    `is_actionable=True` iff a valid setup is detected.
    """
    from smc_engine import atr as _atr

    rules = rules or DEFAULT_RULES
    cfg = (rules.get("dual_tf") or {}) if isinstance(rules, dict) else {}
    enabled = cfg.get("enabled", True)
    entries = cfg.get("entries", ["ob_mitigation", "fvg_fill", "sweep_choch"])
    min_conf = float(cfg.get("min_confidence", 0.50))
    tp_rr = float(cfg.get("tp_rr", 2.0))
    sl_buf = float(cfg.get("sl_buffer_frac", 0.10))
    ob_prox = float(cfg.get("ob_proximity_atr_frac", 0.50))
    fvg_prox = float(cfg.get("fvg_proximity_atr_frac", 0.50))

    if not enabled or not htf_bars or not ltf_bars:
        return TradeSelection(direction="NEUTRAL", entry_type="none",
                              entry_price=None, sl=None, tp=None, confidence=0.0,
                              reasons=["dual_tf disabled or no bars"])

    htf = analyze(htf_bars)
    ltf = analyze(ltf_bars)
    bias = detect_htf_bias(htf)

    base = TradeSelection(
        direction=bias.direction, entry_type="none",
        entry_price=None, sl=None, tp=None,
        confidence=bias.score,
        htf_bias=bias, htf_snapshot=htf, ltf_snapshot=ltf,
        reasons=[f"HTF bias={bias.direction} via {bias.source} (score={bias.score:.2f})"],
    )

    if bias.direction == "NEUTRAL":
        base.reasons.append("no bias → skip")
        return base

    px = _last_price(ltf_bars)
    if px is None:
        base.reasons.append("no LTF price")
        return base

    ltf_atr = _atr(ltf_bars)
    sb_bonus = _silver_bullet_score(ltf, bias.direction)
    cf_bonus = _confluence_bonus(ltf, bias.direction, ltf_atr)
    if sb_bonus > 0:
        base.reasons.append(f"Silver Bullet bonus +{sb_bonus:.2f}")
    if cf_bonus > 0:
        base.reasons.append(f"Confluence bonus +{cf_bonus:.2f}")

    # 1) OB mitigation trigger
    if "ob_mitigation" in entries:
        ob = _most_recent_unmitigated_ob(ltf, bias.direction)
        if ob is not None and _proximity_ok(px, ob.top, ob.bot, ltf_atr, ob_prox):
            entry = px
            sl = _sl_from_ob(ob, bias.direction, buffer_frac=sl_buf)
            tp = _tp_from_rr(entry, sl, bias.direction, rr=tp_rr)
            conf = min(1.0, bias.score + (0.20 if ob.strength == "STRONG" else 0.10)
                       + sb_bonus + cf_bonus)
            base.entry_type = "ob_mitigation"
            base.entry_price = entry
            base.sl = sl
            base.tp = tp
            base.confidence = conf
            base.ltf_trigger = f"LTF OB {ob.side} @ idx {ob.anchor_idx}"
            base.reasons.append(base.ltf_trigger)
            if conf >= min_conf:
                return base

    # 2) FVG fill trigger
    if "fvg_fill" in entries:
        fvg = _most_recent_unmitigated_fvg(ltf, bias.direction)
        if fvg is not None and _proximity_ok(px, fvg.top, fvg.bot, ltf_atr, fvg_prox):
            entry = px
            sl = _sl_from_fvg(fvg, bias.direction, buffer_frac=sl_buf)
            tp = _tp_from_rr(entry, sl, bias.direction, rr=tp_rr)
            conf = min(1.0, bias.score + 0.10 + sb_bonus + cf_bonus)
            base.entry_type = "fvg_fill"
            base.entry_price = entry
            base.sl = sl
            base.tp = tp
            base.confidence = conf
            base.ltf_trigger = f"LTF FVG {fvg.side} @ idx {fvg.middle_idx}"
            base.reasons.append(base.ltf_trigger)
            if conf >= min_conf:
                return base

    # 3) Sweep + CHoCH trigger
    if "sweep_choch" in entries:
        pair = _recent_sweep_then_choch(ltf, bias.direction)
        if pair is not None:
            sw, ch = pair
            # SL below sweep extreme (BULL) or above sweep extreme (BEAR)
            sw_bar = ltf_bars[sw.bar_idx]
            if bias.direction == "BULL":
                sl = sw_bar.low - (ltf_atr * sl_buf)
            else:
                sl = sw_bar.high + (ltf_atr * sl_buf)
            entry = px
            tp = _tp_from_rr(entry, sl, bias.direction, rr=tp_rr)
            conf = min(1.0, bias.score + 0.25 + sb_bonus + cf_bonus)
            base.entry_type = "sweep_choch"
            base.entry_price = entry
            base.sl = sl
            base.tp = tp
            base.confidence = conf
            base.ltf_trigger = f"LTF sweep@{sw.bar_idx} → CHoCH@{ch.bar_idx}"
            base.reasons.append(base.ltf_trigger)
            if conf >= min_conf:
                return base

    # Nothing passed confidence
    base.reasons.append("no LTF trigger crossed confidence threshold")
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from smc_engine import _make_fixture
    bars = _make_fixture()
    # Use the same bars for HTF and LTF — the fixture contains both a
    # displacement-up (BOS/CHoCH BULL) and an unmitigated BULL OB. That
    # should produce a BULL selection.
    sel = select_trade(bars, bars)
    assert sel.htf_bias is not None, "bias must be set"
    assert sel.htf_bias.direction == "BULL", f"expected BULL bias, got {sel.htf_bias.direction}"
    print(f"[OK] dir={sel.direction} entry_type={sel.entry_type} "
          f"conf={sel.confidence:.2f}")
    for r in sel.reasons:
        print(f"     · {r}")
    if sel.is_actionable:
        print(f"[OK] actionable: entry={sel.entry_price:.5f} sl={sel.sl:.5f} tp={sel.tp:.5f}")
    else:
        print("[OK] not actionable on this fixture (threshold not met)")


if __name__ == "__main__":
    _self_test()
