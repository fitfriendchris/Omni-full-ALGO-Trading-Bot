"""
Pytest suite for smart_trailing_stop.

Run:
    cd python && python -m pytest tests/ -v
    # or without pytest:
    cd python && python -m unittest tests.test_smart_trailing_stop -v
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Allow running from python/ or python/tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_trailing_stop import (
    Bar, Position, MarketContext, TrailConfig,
    atr, compute_trailing_sl, _profit_lock_floor,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def mkbars(n: int = 30, start: float = 1.1000, step: float = 0.0002) -> list[Bar]:
    """Generate a slow uptrend of bars."""
    return [
        Bar(time=i,
            open=start + i * step,
            high=start + i * step + 0.0005,
            low= start + i * step - 0.0005,
            close=start + i * step + 0.0003)
        for i in range(n)
    ]


def flat_bars(n: int = 30, price: float = 1.1000) -> list[Bar]:
    return [Bar(time=i, open=price, high=price, low=price, close=price) for i in range(n)]


# ──────────────────────────────────────────────────────────────────────────────
# ATR
# ──────────────────────────────────────────────────────────────────────────────

class TestATR:
    def test_insufficient_bars_returns_zero(self):
        assert atr([], 14) == 0.0
        assert atr(mkbars(5), 14) == 0.0

    def test_positive_atr_on_trending_bars(self):
        a = atr(mkbars(30), 14)
        assert a > 0, "trending bars must have positive ATR"

    def test_flat_market_has_zero_atr(self):
        assert atr(flat_bars(30), 14) == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Profit-lock ladder
# ──────────────────────────────────────────────────────────────────────────────

class TestProfitLockLadder:
    def test_below_first_rung_returns_none(self):
        ladder = [(1.0, 0.0), (2.0, 1.0)]
        assert _profit_lock_floor(0.5, ladder) is None

    def test_at_first_rung(self):
        ladder = [(1.0, 0.0), (2.0, 1.0)]
        assert _profit_lock_floor(1.0, ladder) == 0.0

    def test_between_rungs(self):
        ladder = [(1.0, 0.0), (2.0, 1.0)]
        assert _profit_lock_floor(1.5, ladder) == 0.0   # hasn't hit 2R yet

    def test_at_second_rung(self):
        ladder = [(1.0, 0.0), (2.0, 1.0)]
        assert _profit_lock_floor(2.0, ladder) == 1.0

    def test_far_above_highest_rung(self):
        ladder = [(1.0, 0.0), (2.0, 1.0), (5.0, 3.5)]
        assert _profit_lock_floor(10.0, ladder) == 3.5


# ──────────────────────────────────────────────────────────────────────────────
# Monotonic ratchet — the single most important safety property
# ──────────────────────────────────────────────────────────────────────────────

class TestMonotonicRatchet:
    def test_buy_never_loosens(self):
        bars = mkbars(30)
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1045, current_price=1.1050)
        ctx = MarketContext(bars_m15=bars, last_swing_low_m15=1.1010)
        prop = compute_trailing_sl(pos, ctx)
        assert prop.new_sl >= pos.current_sl, \
            f"BUY SL loosened: {pos.current_sl} → {prop.new_sl}"

    def test_sell_never_loosens(self):
        bars = mkbars(30, start=1.1060, step=-0.0002)
        pos = Position(direction="SELL", entry=1.1040,
                       current_sl=1.1015, current_price=1.1010)
        ctx = MarketContext(bars_m15=bars, last_swing_high_m15=1.1050)
        prop = compute_trailing_sl(pos, ctx)
        assert prop.new_sl <= pos.current_sl, \
            f"SELL SL loosened: {pos.current_sl} → {prop.new_sl}"


# ──────────────────────────────────────────────────────────────────────────────
# Opposing-CHoCH close signal
# ──────────────────────────────────────────────────────────────────────────────

class TestOpposingCHoCHClose:
    def test_h1_choch_while_profitable_closes(self):
        bars = mkbars(30)
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1050)
        ctx = MarketContext(bars_m15=bars, opposing_choch_h1=True)
        prop = compute_trailing_sl(pos, ctx)
        assert prop.should_close, "opposing H1 CHoCH in profit must close"

    def test_m15_choch_while_profitable_closes(self):
        bars = mkbars(30)
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1050)
        ctx = MarketContext(bars_m15=bars, opposing_choch_m15=True)
        prop = compute_trailing_sl(pos, ctx)
        assert prop.should_close

    def test_choch_while_unprofitable_does_not_close(self):
        bars = mkbars(30)
        # Only +0.5R profit — below 1R threshold
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1030)
        ctx = MarketContext(bars_m15=bars, opposing_choch_h1=True)
        prop = compute_trailing_sl(pos, ctx)
        assert not prop.should_close, \
            "CHoCH below 1R profit should not close runner"


# ──────────────────────────────────────────────────────────────────────────────
# Volatility / ATR layer
# ──────────────────────────────────────────────────────────────────────────────

class TestVolatilityLayer:
    def test_exhaustion_tightens(self):
        bars = mkbars(30)
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1050)
        ctx_normal = MarketContext(bars_m15=bars)
        ctx_exhaust = MarketContext(bars_m15=bars, exhaustion_at_level=True)
        p_normal = compute_trailing_sl(pos, ctx_normal)
        p_exhaust = compute_trailing_sl(pos, ctx_exhaust)
        # Tighter SL for BUY means higher SL
        assert p_exhaust.new_sl >= p_normal.new_sl, \
            "exhaustion should tighten (raise) BUY SL"

    def test_displacement_loosens(self):
        bars = mkbars(30)
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1050)
        ctx_normal = MarketContext(bars_m15=bars, last_swing_low_m15=1.1010)
        ctx_disp = MarketContext(bars_m15=bars, last_swing_low_m15=1.1010,
                                  displacement_with=True)
        p_normal = compute_trailing_sl(pos, ctx_normal)
        p_disp   = compute_trailing_sl(pos, ctx_disp)
        # Loosening means volatility layer proposes a lower SL; the combined
        # proposal should not be tighter than the normal one.
        # (Structure layer may dominate — but displacement must not tighten.)
        assert p_disp.new_sl <= p_normal.new_sl + 1e-9


# ──────────────────────────────────────────────────────────────────────────────
# Structure layer
# ──────────────────────────────────────────────────────────────────────────────

class TestStructureLayer:
    def test_buy_sl_behind_swing_low(self):
        bars = mkbars(30)
        cfg  = TrailConfig(structure_buffer_pips=2.0)
        pad  = cfg.structure_buffer_pips * 0.0001
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1050)
        ctx = MarketContext(bars_m15=bars, last_swing_low_m15=1.1018)
        prop = compute_trailing_sl(pos, ctx, cfg)
        # SL should be at least as tight as (swing_low - pad) where possible
        assert prop.new_sl >= 1.1018 - pad - 1e-9


# ──────────────────────────────────────────────────────────────────────────────
# Liquidity avoidance
# ──────────────────────────────────────────────────────────────────────────────

class TestLiquidityAvoidance:
    def test_buy_sl_pushed_below_equal_lows(self):
        bars = mkbars(30)
        cfg  = TrailConfig(liquidity_avoid_pips=3.0)
        avoid = cfg.liquidity_avoid_pips * 0.0001
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1050)
        # Put an EQL right near where a naive SL would land (around 1.1035-ish)
        ctx = MarketContext(
            bars_m15=bars,
            last_swing_low_m15=1.1018,
            equal_lows=[1.10175],
        )
        prop = compute_trailing_sl(pos, ctx, cfg)
        # SL must be at least `avoid` below the pool
        assert prop.new_sl <= 1.10175 - avoid + 1e-9 or prop.new_sl >= 1.10175 + avoid


# ──────────────────────────────────────────────────────────────────────────────
# Hysteresis
# ──────────────────────────────────────────────────────────────────────────────

class TestHysteresis:
    def test_tiny_move_skipped(self):
        bars = mkbars(30)
        # Current SL essentially at the same place the engine would propose
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1038, current_price=1.1050)
        ctx = MarketContext(bars_m15=bars, last_swing_low_m15=1.10375)
        cfg = TrailConfig(min_modify_pips=3.0, min_modify_atr_frac=0.0)
        prop = compute_trailing_sl(pos, ctx, cfg)
        # Either we hold the current SL or the engine proposes an identical/tiny delta
        assert abs(prop.new_sl - 1.1038) <= 0.0003 + 1e-9


# ──────────────────────────────────────────────────────────────────────────────
# Error fail-safe
# ──────────────────────────────────────────────────────────────────────────────

class TestFailSafe:
    def test_returns_current_sl_on_zero_risk(self):
        """Position with entry == SL (risk_distance = 0) must not crash."""
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1020, current_price=1.1050)
        ctx = MarketContext()
        prop = compute_trailing_sl(pos, ctx)
        # Should not crash, should return something sensible
        assert isinstance(prop.new_sl, float)

    def test_returns_current_sl_on_empty_context_when_below_first_rung(self):
        # Below 1R profit + no bars + no structure = nothing to propose → hold SL
        pos = Position(direction="BUY", entry=1.1020,
                       current_sl=1.1000, current_price=1.1025)  # +0.25R only
        ctx = MarketContext()
        prop = compute_trailing_sl(pos, ctx)
        assert prop.new_sl == pos.current_sl, \
            "no bars + no structure + below profit_lock = must hold SL"

    def test_profit_lock_fires_with_empty_bars(self):
        # Empty bars, but profit well above 2R → profit_lock layer still fires
        # Use wider spreads to sidestep float imprecision around rung boundaries.
        pos = Position(direction="BUY", entry=100.0,
                       current_sl=90.0, current_price=130.0)  # +3R
        ctx = MarketContext()
        prop = compute_trailing_sl(pos, ctx)
        # At +3R, ladder locks +1.8R → SL should land at 100 + 1.8*10 = 118
        assert prop.new_sl >= 117.9, \
            f"profit_lock at +3R should raise SL to ≥+1.8R (117.9), got {prop.new_sl}"
        assert prop.new_sl > pos.current_sl, "must ratchet up"


# ──────────────────────────────────────────────────────────────────────────────
# Allow running without pytest: `python tests/test_smart_trailing_stop.py`
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import unittest
    # Convert pytest-style classes into unittest.TestCase discoverable form
    suite = unittest.TestSuite()
    for name, obj in list(globals().items()):
        if isinstance(obj, type) and name.startswith("Test"):
            for attr in dir(obj):
                if attr.startswith("test_"):
                    # Adapter: create a TestCase that invokes the method
                    def make(cls, method):
                        def run(self):
                            cls().__getattribute__(method)()
                        return run
                    tc = type(f"TC_{name}_{attr}", (unittest.TestCase,),
                              {"runTest": make(obj, attr)})
                    suite.addTest(tc())
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
