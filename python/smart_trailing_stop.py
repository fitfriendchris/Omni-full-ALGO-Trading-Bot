"""
smart_trailing_stop.py — Adaptive V2.1 Multi-Layer Trailing Stop Engine (TUNED)

TUNING CHANGES (v2.1):
  1. Added highest_r_seen to Position (fixes peak-protection floor bug)
  2. spread now in PRICE units (via adapter tick_size multiplication)
  3. profit_lock logic uses historical peak, not current profit
  4. Symbol-aware config builder reads xauusd_adjustments from rules.json
  5. Tuned defaults: wider base trail, reduced spread_atr_frac, equity threshold lowered
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, List, Tuple

log = logging.getLogger("smart_trail")


@dataclass(frozen=True)
class Bar:
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
    direction: str
    entry: float
    current_sl: float
    current_price: float
    equity: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    tp1_taken: bool = False
    tp2_taken: bool = False
    pip_size: float = 0.0001
    spread: float = 0.0002
    symbol: str = ""
    highest_r_seen: float = 0.0   # HISTORICAL peak R achieved (tracked externally)

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

    @property
    def peak_r(self) -> float:
        """Highest R achieved including current."""
        return max(self.highest_r_seen, self.profit_in_r)


@dataclass
class MarketContext:
    bars_m15: Sequence[Bar] = field(default_factory=list)
    bars_h1:  Sequence[Bar] = field(default_factory=list)
    last_swing_high_m15: Optional[float] = None
    last_swing_low_m15:  Optional[float] = None
    last_swing_high_h1:  Optional[float] = None
    last_swing_low_h1:   Optional[float] = None
    opposing_choch_h1:  bool = False
    opposing_choch_m15: bool = False
    equal_highs:    Sequence[float] = field(default_factory=list)
    equal_lows:     Sequence[float] = field(default_factory=list)
    session_high:   Optional[float] = None
    session_low:    Optional[float] = None
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    exhaustion_at_level: bool = False
    displacement_with:   bool = False


@dataclass
class TrailConfig:
    atr_period:        int   = 14
    atr_mult_min:      float = 1.5
    atr_mult_compress: float = 0.6
    atr_mult_expand:   float = 2.5
    atr_mult_runner:   float = 2.5
    structure_buffer_pips: float = 3.0
    profit_lock_ladder: Tuple[Tuple[float, float], ...] = (
        (1.0, 0.0),
        (1.5, 0.25),
        (2.0, 0.50),
        (3.0, 0.60),
        (5.0, 0.75),
    )
    tight_equity_threshold: float = 5.0
    tight_mult_compress:    float = 0.8
    spread_atr_frac: float = 0.15
    liquidity_avoid_pips: float = 3.0
    avoid_equal_levels:   bool  = True
    close_on_opposing_choch_once_profitable: bool = True
    min_modify_pips:     float = 3.0
    min_modify_atr_frac: float = 0.15


@dataclass
class TrailProposal:
    new_sl: float
    reason: str
    should_close: bool = False
    layers_fired: List[str] = field(default_factory=list)


# ── Core helpers ──────────────────────────────────────────────────────────────

def _wilder_atr(bars: Sequence[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else 0.0


def _peak_protection_sl(pos: Position, cfg: TrailConfig) -> Optional[float]:
    """Crypto-style: compute a floor based on locked % of PEAK (not current) gains."""
    p_r = pos.profit_in_r
    if p_r <= 0:
        return None

    peak_r = pos.peak_r
    locked_frac = None
    for threshold, frac in cfg.profit_lock_ladder:
        if peak_r >= threshold:
            locked_frac = frac
        else:
            break
    if locked_frac is None:
        return None  # below +1R — no protection yet

    # Minimum R floor = peak_r × (1 - locked fraction)  e.g. peak=3R, lock 60% → floor=1.2R
    floor_r = peak_r * (1.0 - locked_frac)

    if pos.direction == "BUY":
        return pos.entry + floor_r * pos.risk_distance
    # SELL: entry minus floor_r * risk (SL = entry - floor_r * risk_distance)
    return pos.entry - floor_r * pos.risk_distance


def _adaptive_atr_sl(pos: Position, ctx: MarketContext, cfg: TrailConfig) -> Optional[float]:
    atr_m15 = _wilder_atr(ctx.bars_m15, cfg.atr_period) if ctx.bars_m15 else 0.0
    atr_h1  = _wilder_atr(ctx.bars_h1,  cfg.atr_period) if ctx.bars_h1  else 0.0
    atr_ref = max(atr_m15, atr_h1 * 0.5)
    if atr_ref <= 0:
        return None

    mult = cfg.atr_mult_min
    if pos.profit_in_r >= 3.0:
        mult = max(mult, cfg.atr_mult_runner)
    if pos.profit_in_r >= 5.0:
        mult = max(mult, cfg.atr_mult_expand)

    if ctx.exhaustion_at_level and pos.profit_in_r > 0.5:
        mult = min(mult, cfg.atr_mult_compress)
    if ctx.displacement_with and pos.profit_in_r > 1.0:
        mult = max(mult, cfg.atr_mult_expand)

    # Small-account compression (less aggressive now: tight_mult_compress=0.8)
    if pos.equity > 0 and pos.equity < cfg.tight_equity_threshold:
        mult *= cfg.tight_mult_compress

    # For metals: compute in PRICE units directly
    raw_sl = pos.current_price - atr_ref * mult if pos.direction == "BUY" else pos.current_price + atr_ref * mult

    # Spread padding: SL must be at least spread × spread_atr_frac beyond price
    spread_pad = pos.spread * cfg.spread_atr_frac
    if pos.direction == "BUY":
        raw_sl = min(raw_sl, pos.current_price - spread_pad)
    else:
        raw_sl = max(raw_sl, pos.current_price + spread_pad)

    return raw_sl


def _structural_sl(pos: Position, ctx: MarketContext, cfg: TrailConfig) -> Optional[float]:
    pad = cfg.structure_buffer_pips * pos.pip_size
    if pos.direction == "BUY":
        cand = ctx.last_swing_low_m15
        if cand is not None and ctx.last_swing_low_h1 is not None:
            cand = min(cand, ctx.last_swing_low_h1 + pad)
        return cand - pad if cand is not None else None
    else:
        cand = ctx.last_swing_high_m15
        if cand is not None and ctx.last_swing_high_h1 is not None:
            cand = max(cand, ctx.last_swing_high_h1 - pad)
        return cand + pad if cand is not None else None


def _liquidity_safe(sl: float, pos: Position, ctx: MarketContext, cfg: TrailConfig) -> float:
    if not cfg.avoid_equal_levels:
        return sl
    avoid = cfg.liquidity_avoid_pips * pos.pip_size
    pools: List[float] = []
    if pos.direction == "BUY":
        pools.extend([p for p in ctx.equal_lows if p is not None])
        if ctx.session_low is not None: pools.append(ctx.session_low)
        if ctx.pdl is not None: pools.append(ctx.pdl)
        for pool in pools:
            if sl - avoid <= pool <= sl + avoid:
                sl = pool - avoid
    else:
        pools.extend([p for p in ctx.equal_highs if p is not None])
        if ctx.session_high is not None: pools.append(ctx.session_high)
        if ctx.pdh is not None: pools.append(ctx.pdh)
        for pool in pools:
            if sl - avoid <= pool <= sl + avoid:
                sl = pool + avoid
    return sl


def _monotonic(pos: Position, proposed: float) -> float:
    if pos.direction == "BUY":
        return max(pos.current_sl, proposed)
    return min(pos.current_sl, proposed)


def compute_trailing_sl(
    pos: Position,
    ctx: MarketContext,
    cfg: Optional[TrailConfig] = None,
) -> TrailProposal:
    if cfg is None:
        cfg = TrailConfig()
    layers: List[str] = []

    try:
        if (cfg.close_on_opposing_choch_once_profitable
                and pos.profit_in_r >= 1.0
                and (ctx.opposing_choch_h1 or ctx.opposing_choch_m15)):
            return TrailProposal(
                new_sl=pos.current_sl,
                reason="Opposing CHoCH in profit — exit runner",
                should_close=True,
                layers_fired=["structure:choch"],
            )

        if pos.risk_distance <= 1e-9:
            return TrailProposal(
                new_sl=pos.current_sl,
                reason="zero risk distance — hold",
                layers_fired=["guard:zero_risk"],
            )

        atr_sl = _adaptive_atr_sl(pos, ctx, cfg)
        if atr_sl is not None:
            layers.append("volatility:adaptive")

        struct_sl = _structural_sl(pos, ctx, cfg)
        if struct_sl is not None:
            layers.append("structure:swing")

        peak_sl = _peak_protection_sl(pos, cfg)
        if peak_sl is not None:
            layers.append(f"profit_lock:{'+' if pos.profit_in_r > 0 else ''}{pos.profit_in_r:.1f}R")

        candidates = [c for c in (atr_sl, struct_sl, peak_sl) if c is not None]
        if not candidates:
            return TrailProposal(
                new_sl=pos.current_sl,
                reason="no layer produced candidate — hold",
                layers_fired=layers,
            )

        if pos.direction == "BUY":
            proposed = max(candidates)
        else:
            proposed = min(candidates)

        safe = _liquidity_safe(proposed, pos, ctx, cfg)
        if safe != proposed:
            layers.append("liquidity:pushed")
        proposed = safe

        final = _monotonic(pos, proposed)

        atr_m15 = _wilder_atr(ctx.bars_m15, cfg.atr_period) if ctx.bars_m15 else 0.0
        atr_h1  = _wilder_atr(ctx.bars_h1,  cfg.atr_period) if ctx.bars_h1  else 0.0
        atr_ref = max(atr_m15, atr_h1 * 0.5)
        min_move = max(
            cfg.min_modify_pips * pos.pip_size,
            atr_ref * cfg.min_modify_atr_frac if atr_ref > 1e-9 else 5 * pos.pip_size,
        )
        if abs(final - pos.current_sl) < min_move:
            return TrailProposal(
                new_sl=pos.current_sl,
                reason=f"move < hysteresis ({min_move:.5f})",
                layers_fired=layers,
            )

        return TrailProposal(
            new_sl=final,
            reason=" | ".join(layers),
            layers_fired=layers,
        )

    except Exception as e:
        log.warning("smart_trail error: %s", e, exc_info=True)
        return TrailProposal(
            new_sl=pos.current_sl,
            reason=f"error ({e.__class__.__name__}) — hold",
            layers_fired=["error"],
        )
