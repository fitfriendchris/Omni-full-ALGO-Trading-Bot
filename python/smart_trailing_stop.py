"""
smart_trailing_stop.py — Adaptive V2.2 with Discrete Structural OB-Step Trail Engine

v2.2 ADDITIONS:
  1. Discrete OB-step trailing: after breakeven, on confirmed BOS in trade direction,
     steps SL to 2 pips behind the newly formed OB. No continuous smoothing.
  2. OrderBlock dataclass for structural pillar tracking.
  3. _detect_bos() and _find_bullish_ob() / _find_bearish_ob() for BOS-OB coupling.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, List, Tuple

log = logging.getLogger("smart_trail")


# ── New V2.2 structural types ───────────────────────────────────────────────

@dataclass
class OrderBlock:
    """Minimal structural Order Block for trailing-stop stepping."""
    top: float
    bottom: float
    direction: str        # "BULL" or "BEAR"
    pivot_idx: int        # index of the OB root candle in the bar sequence


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
    direction: str          # "BUY" or "SELL"
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
    highest_r_seen: float = 0.0

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
    atr_mult_min:      float = 2.5
    atr_mult_compress: float = 1.5
    atr_mult_expand:   float = 4.0
    atr_mult_runner:   float = 4.0
    structure_buffer_pips: float = 5.0
    profit_lock_ladder: Tuple[Tuple[float, float], ...] = (
        (1.0, 0.50),  # allow 50% give-back from peak at 1R (was 0.0 = lock at peak)
        (2.5, 0.30),  # 2.5R: allow 30% give-back from peak
        (5.0, 0.25),  # 5R: allow 25% give-back
        (7.0, 0.20),  # 7R: allow 20% give-back
        (10.0, 0.15), # 10R: allow 15% give-back
    )
    tight_equity_threshold: float = 5.0
    tight_mult_compress:    float = 0.8
    spread_atr_frac: float = 0.15
    liquidity_avoid_pips: float = 3.0
    avoid_equal_levels:   bool  = True
    close_on_opposing_choch_once_profitable: bool = True
    min_modify_pips:     float = 25.0   # v27.1: $2.50 move minimum for XAUUSD
    min_modify_atr_frac: float = 0.50   # v27.1: 50% ATR hysteresis — only move on structural move

    # V2.2 — discrete OB-step trail parameters
    ob_step_enabled:     bool  = True
    ob_step_buffer_pips: float = 2.0   # pip buffer behind OB body for new SL
    ob_step_min_r:       float = 2.5   # v27.1: only step after TP2+ structural profit


@dataclass
class TrailProposal:
    new_sl: float
    reason: str
    should_close: bool = False
    layers_fired: List[str] = field(default_factory=list)


# ── Discrete structural helpers (V2.2) ─────────────────────────────────────

def _detect_bos(bars: Sequence[Bar], direction: str, lookback: int = 20) -> tuple[bool, int]:
    """Return (BOS_found, pivot_index_in_recent_window)."""
    if len(bars) < lookback + 2:
        return False, -1
    recent = bars[-lookback:]
    if direction == "BUY":
        for i in range(len(recent) - 2, 0, -1):
            if recent[i - 1].high < recent[i].high and recent[i + 1].high <= recent[i].high:
                pivot_high = recent[i].high
                for j in range(i + 1, len(recent)):
                    if recent[j].close > pivot_high:
                        return True, j
                return False, -1
    else:
        for i in range(len(recent) - 2, 0, -1):
            if recent[i - 1].low > recent[i].low and recent[i + 1].low >= recent[i].low:
                pivot_low = recent[i].low
                for j in range(i + 1, len(recent)):
                    if recent[j].close < pivot_low:
                        return True, j
                return False, -1
    return False, -1


def _find_bullish_ob(bars: Sequence[Bar]) -> Optional[OrderBlock]:
    """Last bearish candle (down-close) immediately before a displacement that breaks above it."""
    if len(bars) < 3:
        return None
    for i in range(len(bars) - 1, 0, -1):
        c0 = bars[i - 1]
        if c0.close < c0.open:
            if bars[i].close > c0.open:
                return OrderBlock(top=c0.open, bottom=c0.close, direction="BULL", pivot_idx=i - 1)
    return None


def _find_bearish_ob(bars: Sequence[Bar]) -> Optional[OrderBlock]:
    """Last bullish candle (up-close) immediately before a displacement that breaks below it."""
    if len(bars) < 3:
        return None
    for i in range(len(bars) - 1, 0, -1):
        c0 = bars[i - 1]
        if c0.close > c0.open:
            if bars[i].close < c0.open:
                return OrderBlock(top=c0.close, bottom=c0.open, direction="BEAR", pivot_idx=i - 1)
    return None


def _discrete_ob_step_trail(
    pos: Position, ctx: MarketContext, cfg: TrailConfig
) -> tuple[Optional[float], str]:
    """
    Step-trailing: only after breakeven/profitable (>=1R).
    On confirmed BOS in trade direction → scan for newly formed OB →
    move SL to 2 pips behind that OB's body.
    """
    if not cfg.ob_step_enabled:
        return None, "ob_step:disabled"
    if pos.profit_in_r < cfg.ob_step_min_r:
        return None, "ob_step:below_min_r"

    bars = ctx.bars_m15 if ctx.bars_m15 else ctx.bars_h1
    if len(bars) < 5:
        return None, "ob_step:insufficient_bars"

    bos_ok, _ = _detect_bos(bars, pos.direction)
    if not bos_ok:
        return None, "ob_step:no_bos"

    buf = cfg.ob_step_buffer_pips * pos.pip_size
    if pos.direction == "BUY":
        ob = _find_bullish_ob(bars)
        if ob is None:
            return None, "ob_step:no_bullish_ob"
        proposed = ob.bottom - buf
        if proposed <= pos.current_sl:
            return None, f"ob_step:proposed {proposed:.5f} <= current {pos.current_sl:.5f}"
        return proposed, f"ob_step:bullish_ob[pivot={ob.pivot_idx}]"
    else:
        ob = _find_bearish_ob(bars)
        if ob is None:
            return None, "ob_step:no_bearish_ob"
        proposed = ob.top + buf
        if proposed >= pos.current_sl:
            return None, f"ob_step:proposed {proposed:.5f} >= current {pos.current_sl:.5f}"
        return proposed, f"ob_step:bearish_ob[pivot={ob.pivot_idx}]"


# ── Core helpers (V2.1 preserved) ─────────────────────────────────────────────

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
        return None

    floor_r = peak_r * (1.0 - locked_frac)

    if pos.direction == "BUY":
        return pos.entry + floor_r * pos.risk_distance
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

    if pos.equity > 0 and pos.equity < cfg.tight_equity_threshold:
        mult *= cfg.tight_mult_compress

    raw_sl = pos.current_price - atr_ref * mult if pos.direction == "BUY" else pos.current_price + atr_ref * mult

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


# ── Main trailing engine (V2.2) ───────────────────────────────────────────────

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

        # V2.2 — discrete OB-step takes priority over continuous ATR when triggered
        ob_sl, ob_reason = _discrete_ob_step_trail(pos, ctx, cfg)
        if ob_sl is not None:
            layers.append(ob_reason)

        atr_sl = _adaptive_atr_sl(pos, ctx, cfg)
        if atr_sl is not None:
            layers.append("volatility:adaptive")

        struct_sl = _structural_sl(pos, ctx, cfg)
        if struct_sl is not None:
            layers.append("structure:swing")

        peak_sl = _peak_protection_sl(pos, cfg)
        if peak_sl is not None:
            layers.append(f"profit_lock:{'+' if pos.profit_in_r > 0 else ''}{pos.profit_in_r:.1f}R")

        candidates = [c for c in (ob_sl, atr_sl, struct_sl, peak_sl) if c is not None]
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


# ── Backward-compatible aliases (pre-existing tests rely on these) ────────────

def atr(bars: Sequence[Bar], period: int = 14) -> float:
    """Backward-compatible alias for _wilder_atr."""
    return _wilder_atr(bars, period)


def _profit_lock_floor(r: float, ladder: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Backward-compatible profit-lock helper.
    Returns the locked fraction for a given R, or None if below first rung."""
    locked_frac = None
    for threshold, frac in ladder:
        if r >= threshold:
            locked_frac = frac
        else:
            break
    if locked_frac is None:
        return None
    return r * (1.0 - locked_frac)
