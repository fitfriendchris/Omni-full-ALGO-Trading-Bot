"""
smart_trailing_stop.py — Reactive, multi-layer trailing stop for OMNI-ICT

Replaces the static R-multiple ladder in auto_trader.manage_open_trades with a
reactive engine that considers:

    1. VOLATILITY     — ATR-based minimum distance (never tighter than N×ATR)
    2. STRUCTURE      — trail behind the most recent opposing HL/LH swing;
                        invalidate immediately on opposing CHoCH
    3. MOMENTUM       — tighten during exhaustion, loosen during displacement
    4. LIQUIDITY      — never place SL *inside* a liquidity pool; push it
                        beyond equal-H/L clusters and session H/L
    5. PROFIT LOCK    — R-multiple ratchet floors so locked gains never give back

The module is side-effect free: it takes a Position + market context snapshot
and returns a proposed SL. The caller (auto_trader) decides whether to modify.

Interface:
    proposal = compute_trailing_sl(pos, context, cfg)
    # proposal.new_sl, proposal.reason, proposal.should_close

Safety:
    * Will never loosen an existing SL (monotonic ratchet).
    * Will never propose an SL that locks in a loss once in profit >= 1R
      unless the structure has flipped (CHoCH) — in which case it proposes
      should_close=True.
    * Returns unchanged SL on any internal error (fail-safe).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, List

log = logging.getLogger("smart_trail")


# ──────────────────────────────────────────────────────────────────────────────
# Data contracts
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Bar:
    """A single OHLC bar. Time can be any monotonic unit."""
    time: float
    open: float
    high: float
    low: float
    close: float

    @property
    def body_high(self) -> float: return max(self.open, self.close)
    @property
    def body_low(self)  -> float: return min(self.open, self.close)
    @property
    def range(self)     -> float: return self.high - self.low
    @property
    def body(self)      -> float: return abs(self.close - self.open)


@dataclass
class Position:
    direction: str          # "BUY" or "SELL"
    entry: float
    current_sl: float
    current_price: float
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    tp1_taken: bool = False
    tp2_taken: bool = False
    pip_size: float = 0.0001
    symbol: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.current_sl)

    @property
    def profit_in_r(self) -> float:
        r = self.risk_distance
        if r <= 0:
            return 0.0
        if self.direction == "BUY":
            return (self.current_price - self.entry) / r
        return (self.entry - self.current_price) / r


@dataclass
class MarketContext:
    """Snapshot of structure + volatility at the moment we're evaluating."""
    bars_m15: Sequence[Bar] = field(default_factory=list)
    bars_h1:  Sequence[Bar] = field(default_factory=list)

    # Structure points (already computed by ict_precision)
    last_swing_high_m15: Optional[float] = None
    last_swing_low_m15:  Optional[float] = None
    last_swing_high_h1:  Optional[float] = None
    last_swing_low_h1:   Optional[float] = None

    # Opposing CHoCH flag — set True by structure layer when market breaks
    # the most recent swing *against* our position's direction.
    opposing_choch_h1:  bool = False
    opposing_choch_m15: bool = False

    # Liquidity pools to avoid placing SL inside of
    equal_highs:    Sequence[float] = field(default_factory=list)
    equal_lows:     Sequence[float] = field(default_factory=list)
    session_high:   Optional[float] = None
    session_low:    Optional[float] = None
    pdh: Optional[float] = None      # previous day high
    pdl: Optional[float] = None

    # Momentum flags
    exhaustion_at_level: bool = False    # body slope flattening + long wicks
    displacement_with:   bool = False    # 3+ bars strong bodies in our direction


@dataclass
class TrailConfig:
    # Volatility layer
    atr_period:        int   = 14
    atr_mult_min:      float = 1.2     # never tighter than this × ATR from price
    atr_mult_runner:   float = 1.8     # looser once we're past 3R

    # Structure layer
    structure_buffer_pips: float = 2.0  # pad beyond swing

    # Momentum layer
    exhaustion_tighten_atr_mult: float = 0.8   # tighten to 0.8×ATR at exhaustion
    displacement_loosen_atr_mult: float = 2.2  # give 2.2×ATR during displacement

    # Liquidity layer
    liquidity_avoid_pips: float = 3.0    # never within 3 pips of a pool
    avoid_equal_levels:   bool  = True

    # Profit lock ladder (R multiples → minimum locked R)
    # {profit_r: locked_r_floor}
    profit_lock_ladder: tuple = (
        (1.0, 0.0),     # at +1R → lock breakeven
        (1.5, 0.3),     # at +1.5R → lock +0.3R
        (2.0, 1.0),     # at +2R → lock +1R
        (3.0, 1.8),     # at +3R → lock +1.8R
        (5.0, 3.5),     # at +5R → lock +3.5R
    )

    # Invalidation
    close_on_opposing_choch_once_profitable: bool = True

    # Hysteresis — don't modify unless meaningful move
    min_modify_pips:     float = 3.0
    min_modify_atr_frac: float = 0.15    # or 15% of ATR


@dataclass
class TrailProposal:
    new_sl: float
    reason: str
    should_close: bool = False
    layers_fired: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Core computation
# ──────────────────────────────────────────────────────────────────────────────

def atr(bars: Sequence[Bar], period: int = 14) -> float:
    """Wilder's ATR. Returns 0.0 if insufficient bars."""
    if len(bars) < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Simple average of last `period` TRs (Wilder's smoothing is equivalent
    # in steady state and avoids seeding ambiguity).
    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else 0.0


def _profit_lock_floor(profit_r: float, ladder: Sequence[tuple]) -> Optional[float]:
    """Return the max R-floor whose threshold ≤ profit_r. None if below first rung."""
    floor = None
    for threshold, locked_r in ladder:
        if profit_r >= threshold:
            floor = locked_r
        else:
            break
    return floor


def _structural_sl(pos: Position, ctx: MarketContext, pad: float) -> Optional[float]:
    """Return SL from structure: behind last opposing swing on M15 (with H1 sanity)."""
    if pos.direction == "BUY":
        # Prefer M15 swing low, but never above H1 swing low (protects against noise)
        cand = ctx.last_swing_low_m15
        if cand is not None and ctx.last_swing_low_h1 is not None:
            cand = min(cand, ctx.last_swing_low_h1 + pad)   # don't go below H1 low
        if cand is not None:
            return cand - pad
    else:
        cand = ctx.last_swing_high_m15
        if cand is not None and ctx.last_swing_high_h1 is not None:
            cand = max(cand, ctx.last_swing_high_h1 - pad)
        if cand is not None:
            return cand + pad
    return None


def _liquidity_safe(sl: float, pos: Position, ctx: MarketContext, cfg: TrailConfig) -> float:
    """Push SL beyond any liquidity pool it currently sits inside."""
    if not cfg.avoid_equal_levels:
        return sl
    avoid_dist = cfg.liquidity_avoid_pips * pos.pip_size

    pools: List[float] = []
    if pos.direction == "BUY":
        pools.extend([p for p in ctx.equal_lows if p is not None])
        if ctx.session_low is not None: pools.append(ctx.session_low)
        if ctx.pdl is not None:         pools.append(ctx.pdl)
        # For longs, SL is below price — we want SL to be BELOW each pool by ≥ avoid_dist
        for pool in pools:
            if sl - avoid_dist <= pool <= sl + avoid_dist:
                sl = pool - avoid_dist
    else:
        pools.extend([p for p in ctx.equal_highs if p is not None])
        if ctx.session_high is not None: pools.append(ctx.session_high)
        if ctx.pdh is not None:          pools.append(ctx.pdh)
        for pool in pools:
            if sl - avoid_dist <= pool <= sl + avoid_dist:
                sl = pool + avoid_dist
    return sl


def _apply_monotonic(pos: Position, proposed: float) -> float:
    """Never loosen: BUY SL can only go up, SELL SL can only go down."""
    if pos.direction == "BUY":
        return max(pos.current_sl, proposed)
    return min(pos.current_sl, proposed)


def compute_trailing_sl(
    pos: Position,
    ctx: MarketContext,
    cfg: Optional[TrailConfig] = None,
) -> TrailProposal:
    """
    Pure function. Returns a proposal; caller decides whether to act on it.
    Fail-safe: on any error returns current SL unchanged.
    """
    if cfg is None:
        cfg = TrailConfig()
    layers: List[str] = []

    try:
        # ── 0. Opposing CHoCH while in profit → signal close ──────────────────
        if (cfg.close_on_opposing_choch_once_profitable
                and pos.profit_in_r >= 1.0
                and (ctx.opposing_choch_h1 or ctx.opposing_choch_m15)):
            return TrailProposal(
                new_sl=pos.current_sl,
                reason="Opposing CHoCH while in profit — close runner",
                should_close=True,
                layers_fired=["structure:choch"],
            )

        # ── zero-risk guard ───────────────────────────────────────────────────
        if pos.risk_distance <= 1e-9:
            return TrailProposal(
                new_sl=pos.current_sl,
                reason="risk_distance near zero — hold SL unchanged",
                layers_fired=["guard:zero_risk"],
            )

        # ── 1. Volatility floor (ATR) ─────────────────────────────────────────
        atr_m15 = atr(ctx.bars_m15, cfg.atr_period) if ctx.bars_m15 else 0.0
        atr_h1  = atr(ctx.bars_h1,  cfg.atr_period) if ctx.bars_h1  else 0.0
        atr_ref = max(atr_m15, atr_h1 * 0.5)   # weight M15 ATR, floor with half of H1

        # Choose multiplier based on profit phase + momentum
        mult = cfg.atr_mult_min
        if pos.profit_in_r >= 3.0:
            mult = max(mult, cfg.atr_mult_runner)
        if ctx.exhaustion_at_level:
            mult = min(mult, cfg.exhaustion_tighten_atr_mult)
            layers.append("momentum:exhaustion")
        if ctx.displacement_with:
            mult = max(mult, cfg.displacement_loosen_atr_mult)
            layers.append("momentum:displacement")

        atr_sl: Optional[float] = None
        if atr_ref > 0:
            if pos.direction == "BUY":
                atr_sl = pos.current_price - atr_ref * mult
            else:
                atr_sl = pos.current_price + atr_ref * mult
            layers.append(f"volatility:{mult:.2f}xATR")

        # ── 2. Structure ──────────────────────────────────────────────────────
        pad = cfg.structure_buffer_pips * pos.pip_size
        struct_sl = _structural_sl(pos, ctx, pad)
        if struct_sl is not None:
            layers.append("structure:swing")

        # ── 3. Profit lock floor ──────────────────────────────────────────────
        locked_r = _profit_lock_floor(pos.profit_in_r, cfg.profit_lock_ladder)
        lock_sl: Optional[float] = None
        if locked_r is not None:
            if pos.direction == "BUY":
                lock_sl = pos.entry + locked_r * pos.risk_distance
            else:
                lock_sl = pos.entry - locked_r * pos.risk_distance
            layers.append(f"profit_lock:+{locked_r:.1f}R")

        # ── 4. Combine — pick TIGHTEST (closest to price) of the candidates ───
        # For BUYs that's the MAX; for SELLs that's the MIN.
        candidates = [c for c in (atr_sl, struct_sl, lock_sl) if c is not None]
        if not candidates:
            return TrailProposal(
                new_sl=pos.current_sl,
                reason="No layer produced a candidate — hold SL",
                layers_fired=layers,
            )

        if pos.direction == "BUY":
            proposed = max(candidates)
        else:
            proposed = min(candidates)

        # ── 5. Liquidity avoidance ────────────────────────────────────────────
        safe = _liquidity_safe(proposed, pos, ctx, cfg)
        if safe != proposed:
            layers.append("liquidity:pushed_beyond_pool")
        proposed = safe

        # ── 6. Monotonic ratchet ──────────────────────────────────────────────
        final = _apply_monotonic(pos, proposed)

        # ── 7. Hysteresis — skip tiny moves ───────────────────────────────────
        min_move = max(
            cfg.min_modify_pips * pos.pip_size,
            atr_ref * cfg.min_modify_atr_frac if atr_ref > 1e-9 else 5 * pos.pip_size,
        )
        if abs(final - pos.current_sl) < min_move:
            return TrailProposal(
                new_sl=pos.current_sl,
                reason=f"Move < hysteresis ({min_move:.5f})",
                layers_fired=layers,
            )

        return TrailProposal(
            new_sl=final,
            reason=" | ".join(layers) or "unchanged",
            layers_fired=layers,
        )

    except Exception as e:   # fail-safe — never crash the trading loop
        log.warning("smart_trail error: %s", e, exc_info=True)
        return TrailProposal(
            new_sl=pos.current_sl,
            reason=f"smart_trail error ({e.__class__.__name__}: {e}) — hold SL",
            layers_fired=["error"],
        )


# ──────────────────────────────────────────────────────────────────────────────
# Self-test — run `python smart_trailing_stop.py` to sanity-check core paths
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    # Fabricate 30 bars of slow uptrend on M15
    bars_m15 = [
        Bar(time=i, open=1.1000 + i*0.0002,
            high=1.1005 + i*0.0002,
            low=1.0995 + i*0.0002,
            close=1.1003 + i*0.0002)
        for i in range(30)
    ]
    bars_h1 = bars_m15[::4]

    pos = Position(
        direction="BUY",
        entry=1.1020,
        current_sl=1.1000,          # 20 pips risk
        current_price=1.1050,       # +1.5R
        pip_size=0.0001,
    )
    ctx = MarketContext(
        bars_m15=bars_m15,
        bars_h1=bars_h1,
        last_swing_low_m15=1.1015,  # recent HL
        last_swing_low_h1=1.1005,
        equal_lows=[1.1012],        # liquidity pool near our candidate SL
        session_low=1.0998,
    )
    prop = compute_trailing_sl(pos, ctx)
    assert prop.new_sl > pos.current_sl, f"BUY trail should ratchet UP, got {prop.new_sl}"
    assert not prop.should_close
    print(f"[PASS] BUY @ 1.5R → new SL {prop.new_sl:.5f} ({prop.reason})")

    # Opposing CHoCH while in profit → close
    ctx_choch = MarketContext(
        bars_m15=bars_m15,
        bars_h1=bars_h1,
        last_swing_low_m15=1.1015,
        opposing_choch_h1=True,
    )
    pos.current_price = 1.1055      # +1.75R in profit
    prop = compute_trailing_sl(pos, ctx_choch)
    assert prop.should_close, "Opposing CHoCH in profit should signal close"
    print(f"[PASS] Opposing CHoCH → {prop.reason}")

    # Monotonic — if current SL is already higher than proposal, hold
    pos_tight = Position(
        direction="BUY", entry=1.1020, current_sl=1.1045,
        current_price=1.1050, pip_size=0.0001,
    )
    prop = compute_trailing_sl(pos_tight, ctx)
    assert prop.new_sl >= 1.1045, "Must never loosen"
    print(f"[PASS] Monotonic ratchet → SL held at {prop.new_sl:.5f}")

    print("All self-tests passed.")


if __name__ == "__main__":
    _self_test()
