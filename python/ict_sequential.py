"""
ict_sequential.py — Sequential ICT decision engine (gold-tuned, paper-first).

WHY THIS EXISTS
===============
The legacy `dual_tf_selector.py` models ICT as a WEIGHTED CHECKLIST: it counts
8 independent "confluence" boxes and trades when ANY 3 tick at confidence >= 0.50
(thresholds the code admits were "relaxed for frequency"). The two things that
actually DEFINE an ICT entry — the LTF CHoCH confirmation and the OTE location —
are optional bonuses, and the kill-zone / AMD / HTF-POI gates are decorative.
That is structurally a noise-trader, and the honest 60-day backtest proved it
(17–25% win rate, negative expectancy).

Real ICT (and Chris's discretionary edge) is NOT a checklist. It is a STRICT
DEPENDENT SEQUENCE. Each step is a hard gate; if a gate fails, evaluation stops
and there is NO trade. You cannot "make up" a missing CHoCH with extra FVGs.

THE SEQUENCE (each is mandatory, evaluated in order):
  G1  HTF bias + draw-on-liquidity   — direction AND the target liquidity
  G2  Price in HTF POI (OB/FVG) on the correct side of equilibrium (premium/discount)
  G3  Liquidity sweep on LTF          — the manipulation (stop-hunt)
  G4  LTF CHoCH / MSS after the sweep — THE TRIGGER (confirms the reversal)
  G5  Entry in OTE 0.62–0.79 of the displacement leg, at an FVG/OB
  G6  Kill zone active (London / NY)
  TP  = nearest opposing liquidity (the draw), NOT a max-R fantasy target
  SL  = beyond the swept extreme + gold-tuned ATR buffer

Design:
  - Pure, deterministic, no I/O. Caller passes bars + the current UTC hour.
  - Reuses smc_engine primitives (OB/FVG/sweep/structure/swings) — they are sound.
  - Walk-forward safe: only uses bars up to and including the last one passed in.
  - Returns a Setup with a per-gate trace so every YES/NO is explainable.

Run `python ict_sequential.py` for a self-test on synthetic bars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from smc_engine import (
    Bar, SMCSnapshot, analyze, atr,
    find_swings, find_structure_events,
    find_liquidity_pools, find_sweeps,
    OrderBlock, FairValueGap, Swing, Sweep, StructureEvent,
)

# ──────────────────────────────────────────────────────────────────────────────
# Config (gold-tuned defaults; override per symbol via SequentialConfig)
# ──────────────────────────────────────────────────────────────────────────────

# Kill zones in UTC. ICT: London + NY only. No Asian-range trading.
KILL_ZONES_UTC = [(7, 10), (12, 16)]   # London open, NY open/PM

# OTE retracement window of the displacement leg.
OTE_LO, OTE_HI = 0.62, 0.79
OTE_OPTIMAL = 0.705                      # the 0.705 "sweet spot" limit if no FVG/OB

MIN_RR = 2.0                             # hard minimum reward:risk. Below this = no trade.


@dataclass
class SequentialConfig:
    symbol: str = "XAUUSD"
    # Gold moves ~$3–5 intra-candle; the SL buffer beyond the swept extreme must
    # absorb that noise or you get wicked out. Expressed as a fraction of LTF ATR.
    sl_atr_buffer: float = 0.40
    # How recent the sweep must be (LTF bars) to still be "fresh".
    sweep_lookback: int = 25
    # How recent the CHoCH must be after the sweep.
    choch_max_lag: int = 15
    # POI proximity tolerance as a fraction of HTF ATR.
    poi_tol_atr: float = 1.0
    # Premium/discount filter. Instead of a hard 50% equilibrium binary (pure
    # reversal model, which blocks all valid continuation entries), block only
    # DEEP extremes: no longs in the top `pd_block` of the dealing range, no
    # shorts in the bottom `pd_block`. 0.25 => block longs >75%, shorts <25%.
    require_premium_discount: bool = True
    pd_block: float = 0.25
    min_rr: float = MIN_RR
    kill_zones: Tuple[Tuple[int, int], ...] = tuple(KILL_ZONES_UTC)


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Gate:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Setup:
    direction: str = "NEUTRAL"           # BULL | BEAR | NEUTRAL
    actionable: bool = False
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    rr: float = 0.0
    entry_type: str = "none"             # fvg | ob | ote_limit
    draw_target: Optional[float] = None
    gates: List[Gate] = field(default_factory=list)
    # diagnostics
    htf_bias: str = "NEUTRAL"
    equilibrium: Optional[float] = None
    sweep_idx: Optional[int] = None
    choch_idx: Optional[int] = None

    @property
    def failed_at(self) -> str:
        for g in self.gates:
            if not g.passed:
                return g.name
        return ""

    def trace(self) -> str:
        lines = [f"{'PASS' if g.passed else 'FAIL'}  {g.name}: {g.detail}" for g in self.gates]
        if self.actionable:
            lines.append(f"==> {self.direction} entry={self.entry} sl={self.sl} "
                         f"tp={self.tp} rr={self.rr:.2f} ({self.entry_type})")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _hour_utc(ts: float) -> int:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).hour
    except Exception:
        return -1


def _in_kill_zone(hour: int, zones) -> bool:
    return any(lo <= hour < hi for lo, hi in zones)


def _dealing_range(swings: List[Swing]) -> Optional[Tuple[float, float, float]]:
    """Return (range_low, range_high, equilibrium) from the most recent
    confirmed swing high and swing low. None if not enough swings."""
    last_high = next((s.price for s in reversed(swings) if s.kind == "HIGH"), None)
    last_low = next((s.price for s in reversed(swings) if s.kind == "LOW"), None)
    if last_high is None or last_low is None or last_high <= last_low:
        return None
    return last_low, last_high, (last_high + last_low) / 2.0


# ──────────────────────────────────────────────────────────────────────────────
# Gates
# ──────────────────────────────────────────────────────────────────────────────

def _g1_htf_bias(htf: SMCSnapshot, price: float, cfg: SequentialConfig
                 ) -> Tuple[Gate, str, Optional[Tuple[float, float, float]], Optional[float]]:
    """HTF bias from the last confirmed structure event + draw-on-liquidity.
    Returns (gate, direction, dealing_range, draw_target) where dealing_range
    is (low, high, equilibrium) or None."""
    if not htf.structure:
        return Gate("G1_HTF_BIAS", False, "no HTF BOS/CHoCH — no directional bias"), "NEUTRAL", None, None

    bias = htf.structure[-1].direction  # latest break sets bias
    rng = _dealing_range(htf.swings)
    equilibrium = rng[2] if rng else None

    # draw-on-liquidity = nearest unswept opposing pool price is being pulled toward.
    # BULL draw = buy-side liquidity (swing highs) ABOVE price.
    # BEAR draw = sell-side liquidity (swing lows) BELOW price.
    draw = None
    if bias == "BULL":
        highs = [p.level for p in htf.pools if p.side == "BULL" and not p.swept and p.level > price]
        if not highs:
            highs = [s.price for s in htf.swings if s.kind == "HIGH" and s.price > price]
        draw = min(highs) if highs else None      # nearest pool above
    else:
        lows = [p.level for p in htf.pools if p.side == "BEAR" and not p.swept and p.level < price]
        if not lows:
            lows = [s.price for s in htf.swings if s.kind == "LOW" and s.price < price]
        draw = max(lows) if lows else None         # nearest pool below

    if draw is None:
        return Gate("G1_HTF_BIAS", False,
                    f"{bias} bias but no opposing liquidity draw to target"), bias, rng, None

    ev = htf.structure[-1]
    return (Gate("G1_HTF_BIAS", True,
                 f"{bias} (last HTF {ev.kind}); draw->{draw:.2f}; eq={equilibrium:.2f}"
                 if equilibrium else f"{bias} (last HTF {ev.kind}); draw->{draw:.2f}"),
            bias, rng, draw)


def _g2_htf_poi(htf: SMCSnapshot, price: float, direction: str,
                rng: Optional[Tuple[float, float, float]], htf_atr: float,
                cfg: SequentialConfig) -> Gate:
    """Price must be retracing into an unmitigated HTF OB/FVG on the bias side,
    and not at a DEEP premium/discount extreme (don't chase tops/bottoms)."""
    # Deep premium/discount check — block only the extreme `pd_block` of the range.
    if cfg.require_premium_discount and rng is not None:
        lo, hi, _eq = rng
        span = hi - lo
        if span > 0:
            pos = (price - lo) / span                 # 0=range low, 1=range high
            if direction == "BULL" and pos > (1.0 - cfg.pd_block):
                return Gate("G2_HTF_POI", False,
                            f"price at {pos*100:.0f}% of range — DEEP PREMIUM, no chasing longs")
            if direction == "BEAR" and pos < cfg.pd_block:
                return Gate("G2_HTF_POI", False,
                            f"price at {pos*100:.0f}% of range — DEEP DISCOUNT, no chasing shorts")

    tol = htf_atr * cfg.poi_tol_atr
    for ob in htf.order_blocks:
        if ob.mitigated or ob.side != direction:
            continue
        if ob.contains(price) or abs((ob.bot + ob.top) / 2 - price) <= tol:
            return Gate("G2_HTF_POI", True, f"price in {direction} HTF OB {ob.bot:.2f}-{ob.top:.2f}")
    for fvg in htf.fvgs:
        if fvg.mitigated or fvg.side != direction:
            continue
        if fvg.contains(price) or abs((fvg.bot + fvg.top) / 2 - price) <= tol:
            return Gate("G2_HTF_POI", True, f"price in {direction} HTF FVG {fvg.bot:.2f}-{fvg.top:.2f}")
    return Gate("G2_HTF_POI", False, f"price {price:.2f} not in any {direction} HTF OB/FVG (tol={tol:.2f})")


def _g3_sweep(ltf: SMCSnapshot, direction: str, n_bars: int,
              cfg: SequentialConfig) -> Tuple[Gate, Optional[Sweep]]:
    """A fresh LTF liquidity sweep in the trade direction (manipulation).
    BULL trade needs a sell-side sweep (low taken, closed back up) -> Sweep.side==BULL."""
    fresh_floor = n_bars - cfg.sweep_lookback
    matching = [s for s in ltf.sweeps if s.side == direction and s.bar_idx >= fresh_floor]
    if not matching:
        return Gate("G3_SWEEP", False,
                    f"no fresh {direction} liquidity sweep in last {cfg.sweep_lookback} bars"), None
    sweep = max(matching, key=lambda s: s.bar_idx)   # most recent
    return Gate("G3_SWEEP", True,
                f"{direction} sweep of {sweep.pool_level:.2f} @ bar {sweep.bar_idx}"), sweep


def _g4_choch(ltf: SMCSnapshot, direction: str, sweep: Sweep,
              cfg: SequentialConfig) -> Tuple[Gate, Optional[StructureEvent]]:
    """An LTF CHoCH/BOS in the trade direction AFTER the sweep — the trigger."""
    after = [e for e in ltf.structure
             if e.direction == direction and e.bar_idx > sweep.bar_idx
             and (e.bar_idx - sweep.bar_idx) <= cfg.choch_max_lag]
    if not after:
        return Gate("G4_CHOCH", False,
                    f"no {direction} CHoCH/BOS within {cfg.choch_max_lag} bars after sweep"), None
    ev = min(after, key=lambda e: e.bar_idx)   # first confirmation after sweep
    return Gate("G4_CHOCH", True, f"{ev.kind} {direction} @ bar {ev.bar_idx} (trigger)"), ev


def _g5_entry_ote(ltf: SMCSnapshot, bars: List[Bar], direction: str,
                  sweep: Sweep, choch: StructureEvent, ltf_atr: float,
                  cfg: SequentialConfig) -> Tuple[Gate, Optional[float], Optional[float], str]:
    """Entry in the OTE 0.62–0.79 of the displacement leg (sweep extreme -> CHoCH break),
    at an unmitigated FVG/OB if one sits in the zone. Returns (gate, entry, sl, type)."""
    # Displacement leg extremes.
    if direction == "BULL":
        leg_lo = min(b.low for b in bars[sweep.bar_idx:choch.bar_idx + 1])
        leg_hi = max(b.high for b in bars[sweep.bar_idx:choch.bar_idx + 1])
        leg = leg_hi - leg_lo
        if leg <= 0:
            return Gate("G5_ENTRY_OTE", False, "degenerate displacement leg"), None, None, "none"
        ote_hi = leg_hi - OTE_LO * leg     # shallower retr
        ote_lo = leg_hi - OTE_HI * leg     # deeper retr
        zone_lo, zone_hi = ote_lo, ote_hi  # entry zone (a price band below current)
        extreme = leg_lo
    else:
        leg_hi = max(b.high for b in bars[sweep.bar_idx:choch.bar_idx + 1])
        leg_lo = min(b.low for b in bars[sweep.bar_idx:choch.bar_idx + 1])
        leg = leg_hi - leg_lo
        if leg <= 0:
            return Gate("G5_ENTRY_OTE", False, "degenerate displacement leg"), None, None, "none"
        ote_lo = leg_lo + OTE_LO * leg
        ote_hi = leg_lo + OTE_HI * leg
        zone_lo, zone_hi = ote_lo, ote_hi
        extreme = leg_hi

    # Prefer an unmitigated FVG/OB whose midpoint lands inside the OTE band.
    entry = None
    etype = "ote_limit"
    cands = []
    for fvg in ltf.fvgs:
        if fvg.mitigated or fvg.side != direction:
            continue
        mid = (fvg.bot + fvg.top) / 2
        if zone_lo <= mid <= zone_hi:
            cands.append((mid, fvg.top if direction == "BULL" else fvg.bot, "fvg"))
    for ob in ltf.order_blocks:
        if ob.mitigated or ob.side != direction:
            continue
        mid = (ob.bot + ob.top) / 2
        if zone_lo <= mid <= zone_hi:
            cands.append((mid, ob.top if direction == "BULL" else ob.bot, "ob"))

    if cands:
        # Deepest discount for BULL (lowest), highest premium for BEAR (highest).
        cands.sort(key=lambda c: c[0], reverse=(direction == "BEAR"))
        _, entry, etype = cands[0]
    else:
        # No FVG/OB in OTE -> use the 0.705 optimal level as a limit.
        if direction == "BULL":
            entry = leg_hi - OTE_OPTIMAL * leg
        else:
            entry = leg_lo + OTE_OPTIMAL * leg
        etype = "ote_limit"

    buf = ltf_atr * cfg.sl_atr_buffer
    sl = (extreme - buf) if direction == "BULL" else (extreme + buf)
    # Sanity: SL must be the correct side of entry.
    if direction == "BULL" and sl >= entry:
        return Gate("G5_ENTRY_OTE", False, "SL not below entry (leg too shallow)"), None, None, "none"
    if direction == "BEAR" and sl <= entry:
        return Gate("G5_ENTRY_OTE", False, "SL not above entry (leg too shallow)"), None, None, "none"

    return (Gate("G5_ENTRY_OTE", True,
                 f"entry {entry:.2f} ({etype}) in OTE {zone_lo:.2f}-{zone_hi:.2f}, SL {sl:.2f}"),
            entry, sl, etype)


def _g6_killzone(ts: float, cfg: SequentialConfig) -> Gate:
    hour = _hour_utc(ts)
    if _in_kill_zone(hour, cfg.kill_zones):
        return Gate("G6_KILLZONE", True, f"kill zone active (hour={hour} UTC)")
    return Gate("G6_KILLZONE", False, f"outside London/NY kill zones (hour={hour} UTC)")


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def evaluate(htf_bars: List[Bar], ltf_bars: List[Bar],
             cfg: Optional[SequentialConfig] = None,
             now_ts: Optional[float] = None) -> Setup:
    """Evaluate the full sequential ICT setup on the given bars.

    htf_bars : higher TF (H4 or H1) for bias / POI / draw.
    ltf_bars : lower TF (M5 or M15) for sweep / CHoCH / OTE entry.
    now_ts   : timestamp used for the kill-zone gate (defaults to last LTF bar).
    Only bars up to the last element are used (walk-forward safe).
    """
    cfg = cfg or SequentialConfig()
    s = Setup()

    if len(htf_bars) < 20 or len(ltf_bars) < 30:
        s.gates.append(Gate("G0_DATA", False, "insufficient bars"))
        return s

    htf = analyze(htf_bars)
    ltf = analyze(ltf_bars)
    price = ltf_bars[-1].close
    htf_atr = atr(htf_bars, 14)
    ltf_atr = atr(ltf_bars, 14)
    ts = now_ts if now_ts is not None else ltf_bars[-1].time

    # G1 — HTF bias + draw
    g1, direction, rng, draw = _g1_htf_bias(htf, price, cfg)
    s.gates.append(g1); s.htf_bias = direction
    s.equilibrium = rng[2] if rng else None; s.draw_target = draw
    if not g1.passed:
        return s
    s.direction = direction

    # G2 — HTF POI + premium/discount
    g2 = _g2_htf_poi(htf, price, direction, rng, htf_atr, cfg)
    s.gates.append(g2)
    if not g2.passed:
        return s

    # G3 — LTF liquidity sweep
    g3, sweep = _g3_sweep(ltf, direction, len(ltf_bars), cfg)
    s.gates.append(g3)
    if not g3.passed or sweep is None:
        return s
    s.sweep_idx = sweep.bar_idx

    # G4 — LTF CHoCH after sweep (the trigger)
    g4, choch = _g4_choch(ltf, direction, sweep, cfg)
    s.gates.append(g4)
    if not g4.passed or choch is None:
        return s
    s.choch_idx = choch.bar_idx

    # G5 — entry in OTE at FVG/OB
    g5, entry, sl, etype = _g5_entry_ote(ltf, ltf.bars, direction, sweep, choch, ltf_atr, cfg)
    s.gates.append(g5)
    if not g5.passed or entry is None or sl is None:
        return s

    # G6 — kill zone
    g6 = _g6_killzone(ts, cfg)
    s.gates.append(g6)
    if not g6.passed:
        return s

    # TP — draw-on-liquidity (nearest opposing liquidity from G1), enforce min R:R.
    risk = abs(entry - sl)
    if risk <= 0:
        s.gates.append(Gate("TP", False, "zero risk distance"))
        return s
    tp = draw
    # If the draw is too close for min R:R, push to the min-R:R distance (still
    # toward the draw) rather than inventing a far target.
    rr = abs(tp - entry) / risk if tp is not None else 0.0
    if tp is None or rr < cfg.min_rr:
        # require the draw to at least exist beyond min RR; else no trade
        s.gates.append(Gate("TP_RR", False,
                            f"draw target gives only {rr:.2f}R (< {cfg.min_rr}) — skip"))
        return s

    s.entry = round(entry, 2)
    s.sl = round(sl, 2)
    s.tp = round(tp, 2)
    s.rr = round(rr, 2)
    s.entry_type = etype
    s.actionable = True
    s.gates.append(Gate("TP_RR", True, f"TP {tp:.2f} at draw liquidity, {rr:.2f}R"))
    return s


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from smc_engine import _make_fixture
    bars = _make_fixture()
    # Use the same fixture for both TFs just to exercise the gate plumbing.
    s = evaluate(bars, bars, now_ts=datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc).timestamp())
    print("Sequential engine self-test")
    print(s.trace())
    print(f"\nfailed_at = {s.failed_at or '(none — actionable)'}")
    assert isinstance(s, Setup)
    assert s.gates, "must produce a gate trace"
    print("\n[OK] self-test ran (gate plumbing intact)")


if __name__ == "__main__":
    _self_test()
