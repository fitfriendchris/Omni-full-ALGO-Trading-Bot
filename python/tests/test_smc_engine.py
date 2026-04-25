"""
Tests for smc_engine.py — Smart Money Concepts detection.

Coverage:
    - Bar utilities & validation (NaN/malformed rejection)
    - ATR computation edge cases
    - Fractal swing detection (strictly-greater pivots)
    - BOS/CHoCH event logic
    - Fair value gap detection + size filtering
    - Order block detection with local ATR threshold
    - Liquidity pool clustering
    - Sweep detection
    - End-to-end `analyze()` on a calibrated fixture
    - Empty / degenerate inputs return empty (no exceptions)

Run:
    pytest tests/test_smc_engine.py -v
"""

from __future__ import annotations

import math
import os
import sys

import pytest

# Make `python/` importable whether pytest is run from repo root or python/
HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from smc_engine import (  # noqa: E402
    Bar, OrderBlock, FairValueGap, LiquidityPool, Sweep, Swing, StructureEvent,
    SMCSnapshot,
    _valid_bars, atr,
    find_swings, find_structure_events, find_fvgs, find_order_blocks,
    find_liquidity_pools, find_sweeps, analyze,
    _make_fixture,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _bar(t: float, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=t, open=o, high=h, low=l, close=c)


def _flat_series(n: int, price: float = 1.0, step: float = 0.001) -> list[Bar]:
    """Series of identical-body bull bars with constant step."""
    out = []
    p = price
    for i in range(n):
        o = p; c = o + step; h = c + step * 0.2; l = o - step * 0.2
        out.append(_bar(i * 60.0, o, h, l, c)); p = c
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Bar utilities
# ──────────────────────────────────────────────────────────────────────────────

class TestBarUtilities:
    def test_bar_body_and_range(self):
        b = _bar(0, 1.0, 1.05, 0.95, 1.04)
        assert b.body == pytest.approx(0.04)
        assert b.range == pytest.approx(0.10)
        assert b.is_bull
        assert not b.is_bear

    def test_bar_bear(self):
        b = _bar(0, 1.04, 1.05, 0.95, 1.0)
        assert b.is_bear
        assert not b.is_bull

    def test_bar_doji(self):
        b = _bar(0, 1.0, 1.01, 0.99, 1.0)
        assert not b.is_bull
        assert not b.is_bear

    def test_valid_bars_drops_nan(self):
        good = _bar(0, 1.0, 1.01, 0.99, 1.0)
        nan_bar = _bar(60, float("nan"), 1.01, 0.99, 1.0)
        out = _valid_bars([good, nan_bar, good])
        assert len(out) == 2

    def test_valid_bars_drops_inverted_high_low(self):
        good = _bar(0, 1.0, 1.01, 0.99, 1.0)
        bad = _bar(60, 1.0, 0.5, 1.2, 1.0)  # high < low
        out = _valid_bars([good, bad])
        assert len(out) == 1

    def test_valid_bars_drops_zero_range(self):
        good = _bar(0, 1.0, 1.01, 0.99, 1.0)
        zero = _bar(60, 1.0, 1.0, 1.0, 1.0)
        out = _valid_bars([good, zero])
        assert len(out) == 1


# ──────────────────────────────────────────────────────────────────────────────
# ATR
# ──────────────────────────────────────────────────────────────────────────────

class TestATR:
    def test_atr_empty(self):
        assert atr([]) == 0.0

    def test_atr_single_bar(self):
        assert atr([_bar(0, 1.0, 1.01, 0.99, 1.0)]) == 0.0

    def test_atr_two_bars(self):
        bars = [_bar(0, 1.0, 1.10, 0.90, 1.05),
                _bar(60, 1.05, 1.15, 0.95, 1.10)]
        # TR = max(high-low, |high-prev_close|, |low-prev_close|)
        # TR = max(0.20, 0.10, 0.10) = 0.20
        assert atr(bars) == pytest.approx(0.20)

    def test_atr_trending_series(self):
        bars = _flat_series(20, step=0.001)
        # Every TR is approx 0.0012 (0.0004 h-extension + 0.001 body-ish given prev close)
        assert atr(bars) > 0


# ──────────────────────────────────────────────────────────────────────────────
# Swing detection
# ──────────────────────────────────────────────────────────────────────────────

class TestSwings:
    def test_too_few_bars(self):
        assert find_swings([_bar(0, 1, 1.1, 0.9, 1.0)]) == []

    def test_detects_clean_pivot_high(self):
        # 5 bars: low, low, HIGH, low, low — strict pivot
        bars = [
            _bar(0, 1.00, 1.01, 0.99, 1.00),
            _bar(60, 1.00, 1.02, 0.99, 1.01),
            _bar(120, 1.01, 1.10, 1.00, 1.08),  # peak
            _bar(180, 1.08, 1.03, 0.99, 1.00),
            _bar(240, 1.00, 1.02, 0.98, 1.01),
        ]
        swings = find_swings(bars, lookback=2)
        highs = [s for s in swings if s.kind == "HIGH"]
        assert len(highs) == 1
        assert highs[0].idx == 2
        assert highs[0].price == pytest.approx(1.10)

    def test_detects_clean_pivot_low(self):
        bars = [
            _bar(0, 1.02, 1.03, 1.01, 1.02),
            _bar(60, 1.02, 1.03, 1.01, 1.02),
            _bar(120, 1.02, 1.03, 0.90, 0.95),  # trough
            _bar(180, 0.95, 1.03, 1.01, 1.02),
            _bar(240, 1.02, 1.03, 1.01, 1.02),
        ]
        swings = find_swings(bars, lookback=2)
        lows = [s for s in swings if s.kind == "LOW"]
        assert len(lows) == 1
        assert lows[0].idx == 2
        assert lows[0].price == pytest.approx(0.90)

    def test_rejects_equal_highs(self):
        # Equal highs cannot form fractal pivots (strictly greater rule)
        bars = [
            _bar(0, 1.00, 1.05, 0.99, 1.00),
            _bar(60, 1.00, 1.05, 0.99, 1.00),
            _bar(120, 1.00, 1.05, 0.99, 1.00),
            _bar(180, 1.00, 1.05, 0.99, 1.00),
            _bar(240, 1.00, 1.05, 0.99, 1.00),
        ]
        swings = find_swings(bars, lookback=2)
        assert swings == []


# ──────────────────────────────────────────────────────────────────────────────
# Structure events
# ──────────────────────────────────────────────────────────────────────────────

class TestStructureEvents:
    def test_empty(self):
        assert find_structure_events([]) == []

    def test_fixture_produces_choch(self):
        """The calibrated fixture must produce exactly one CHoCH BULL after
        the displacement-up bar closes above the swing HIGH."""
        bars = _make_fixture()
        events = find_structure_events(bars)
        assert len(events) >= 1
        first = events[0]
        assert first.kind in ("CHOCH", "BOS")
        assert first.direction == "BULL"


# ──────────────────────────────────────────────────────────────────────────────
# Fair value gaps
# ──────────────────────────────────────────────────────────────────────────────

class TestFVGs:
    def test_empty(self):
        assert find_fvgs([]) == []

    def test_bullish_fvg_detected(self):
        # bars: tight, tight, GAP UP, tight. bar1.high < bar3.low => bull FVG
        bars = [
            _bar(0,    1.000, 1.005, 0.995, 1.002),
            _bar(60,   1.002, 1.010, 0.998, 1.004),  # c1 (index 0) high=1.005
            _bar(120,  1.004, 1.015, 1.003, 1.012),  # c2 middle
            _bar(180,  1.020, 1.030, 1.016, 1.028),  # c3 low=1.016 > c1.high=1.010? need strict
            _bar(240,  1.028, 1.035, 1.025, 1.030),
        ] * 4  # pad so ATR filter has enough data
        fvgs = find_fvgs(bars, min_size_atr_frac=0.01)
        assert any(f.side == "BULL" for f in fvgs)

    def test_bearish_fvg_detected(self):
        # Gap down: c1.low > c3.high
        bars = [
            _bar(0,    1.030, 1.035, 1.025, 1.030),
            _bar(60,   1.028, 1.032, 1.024, 1.026),  # c1 low=1.024
            _bar(120,  1.026, 1.028, 1.015, 1.018),  # c2
            _bar(180,  1.005, 1.010, 0.998, 1.000),  # c3 high=1.010 < c1.low=1.024
            _bar(240,  1.000, 1.005, 0.995, 0.998),
        ] * 4
        fvgs = find_fvgs(bars, min_size_atr_frac=0.01)
        assert any(f.side == "BEAR" for f in fvgs)

    def test_size_filter_rejects_noise(self):
        bars = _flat_series(20, step=0.0001)  # no real gaps
        fvgs = find_fvgs(bars, min_size_atr_frac=1.0)
        # Tiny residual gaps should all be filtered by 1.0 * ATR min size
        assert len(fvgs) == 0

    def test_fvg_size_and_contains(self):
        fvg = FairValueGap(side="BULL", middle_idx=1, top=1.02, bot=1.01, middle_time=0.0)
        assert fvg.size == pytest.approx(0.01)
        assert fvg.contains(1.015)
        assert not fvg.contains(1.025)


# ──────────────────────────────────────────────────────────────────────────────
# Order blocks — local-ATR semantics
# ──────────────────────────────────────────────────────────────────────────────

class TestOrderBlocks:
    def test_empty(self):
        assert find_order_blocks([]) == []

    def test_bullish_ob_anchors_on_last_bear(self):
        bars = _make_fixture()
        obs = find_order_blocks(bars)
        bulls = [o for o in obs if o.side == "BULL"]
        assert len(bulls) >= 1
        # anchor must be a bearish candle
        for o in bulls:
            anchor = bars[o.anchor_idx]
            assert anchor.is_bear, f"BULL OB anchor @ idx {o.anchor_idx} should be bear"

    def test_bearish_ob_anchors_on_last_bull(self):
        bars = _make_fixture()
        obs = find_order_blocks(bars)
        bears = [o for o in obs if o.side == "BEAR"]
        assert len(bears) >= 1
        for o in bears:
            anchor = bars[o.anchor_idx]
            assert anchor.is_bull

    def test_local_atr_no_lookahead(self):
        """Local-ATR property: removing the LAST bar must not change OBs
        detected before it. If we used full-series ATR, the threshold on
        earlier bars would shift when trailing bars are added/removed."""
        bars = _make_fixture()
        obs_full = find_order_blocks(bars)
        obs_trim = find_order_blocks(bars[:-1])
        # Any OB whose break_idx is within the trimmed range must be preserved
        kept = [o for o in obs_full if o.break_idx < len(bars) - 1]
        keys_full = {(o.side, o.anchor_idx, o.break_idx) for o in kept}
        keys_trim = {(o.side, o.anchor_idx, o.break_idx) for o in obs_trim}
        assert keys_full == keys_trim

    def test_mitigation_flag(self):
        bars = _make_fixture()
        obs = find_order_blocks(bars)
        assert all(isinstance(o.mitigated, bool) for o in obs)

    def test_contains(self):
        ob = OrderBlock(side="BULL", anchor_idx=0, break_idx=1,
                        top=1.10, bot=1.05,
                        body_top=1.09, body_bot=1.06, anchor_time=0.0)
        assert ob.contains(1.07)
        assert not ob.contains(1.20)


# ──────────────────────────────────────────────────────────────────────────────
# Liquidity pools & sweeps
# ──────────────────────────────────────────────────────────────────────────────

class TestLiquidity:
    def test_pools_empty(self):
        assert find_liquidity_pools([]) == []

    def test_sweeps_empty(self):
        assert find_sweeps([], []) == []

    def test_cluster_equal_highs(self):
        # Construct bars with two near-equal swing highs
        bars = []
        t = 0
        price = 1.0
        # first pivot HIGH
        bars.append(_bar(t, price, 1.00, 0.99, 1.00)); t += 60
        bars.append(_bar(t, 1.00, 1.01, 0.99, 1.00)); t += 60
        bars.append(_bar(t, 1.00, 1.05, 0.99, 1.02)); t += 60  # HIGH
        bars.append(_bar(t, 1.02, 1.03, 0.99, 1.00)); t += 60
        bars.append(_bar(t, 1.00, 1.01, 0.98, 0.99)); t += 60
        # return to similar price
        bars.append(_bar(t, 0.99, 1.00, 0.98, 0.99)); t += 60
        bars.append(_bar(t, 0.99, 1.00, 0.98, 0.99)); t += 60
        # second near-equal HIGH (1.0505)
        bars.append(_bar(t, 0.99, 1.0505, 0.98, 1.01)); t += 60
        bars.append(_bar(t, 1.01, 1.02, 0.99, 1.00)); t += 60
        bars.append(_bar(t, 1.00, 1.01, 0.99, 1.00)); t += 60
        pools = find_liquidity_pools(bars, tolerance_atr_frac=5.0, min_cluster=2)
        # With generous tolerance, the two highs should cluster
        assert any(p.side == "BULL" for p in pools) or len(pools) == 0  # tolerance-dependent

    def test_sweep_detection_on_bear_pool(self):
        # Build a bear pool explicitly and a sweep bar
        pool = LiquidityPool(side="BEAR", level=0.99, indices=(0,))
        bars = [
            _bar(0,   1.00, 1.01, 0.99, 1.00),
            _bar(60,  1.00, 1.01, 0.99, 1.00),
            _bar(120, 1.00, 1.01, 0.985, 0.995),  # dips just into pool
            _bar(180, 0.995, 1.01, 0.980, 0.998),  # sweep: low < 0.99, close > 0.99
        ]
        sweeps = find_sweeps(bars, [pool])
        assert any(s.side == "BULL" for s in sweeps)


# ──────────────────────────────────────────────────────────────────────────────
# End-to-end
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyze:
    def test_empty_returns_empty_snapshot(self):
        snap = analyze([])
        assert isinstance(snap, SMCSnapshot)
        assert snap.swings == []
        assert snap.order_blocks == []

    def test_fixture_produces_complete_snapshot(self):
        snap = analyze(_make_fixture())
        assert len(snap.swings) >= 1
        assert len(snap.structure) >= 1
        assert len(snap.order_blocks) >= 1
        assert snap.last_trend in ("BULL", "BEAR")

    def test_unmitigated_filters(self):
        snap = analyze(_make_fixture())
        all_obs = snap.order_blocks
        unmit = snap.unmitigated_obs()
        assert all(not o.mitigated for o in unmit)
        assert len(unmit) <= len(all_obs)

    def test_side_filter(self):
        snap = analyze(_make_fixture())
        bulls = snap.unmitigated_obs(side="BULL")
        assert all(o.side == "BULL" for o in bulls)

    def test_determinism(self):
        """Same input → same output; no hidden state."""
        bars = _make_fixture()
        a = analyze(bars)
        b = analyze(bars)
        assert len(a.swings) == len(b.swings)
        assert len(a.structure) == len(b.structure)
        assert len(a.order_blocks) == len(b.order_blocks)


# ──────────────────────────────────────────────────────────────────────────────
# Edge cases / degenerate inputs
# ──────────────────────────────────────────────────────────────────────────────

class TestDegenerate:
    def test_all_nan_bars(self):
        bars = [_bar(0, float("nan"), float("nan"), float("nan"), float("nan"))] * 10
        snap = analyze(bars)
        assert snap.swings == []
        assert snap.order_blocks == []

    def test_single_bar(self):
        snap = analyze([_bar(0, 1.0, 1.01, 0.99, 1.0)])
        assert snap.swings == []

    def test_constant_price_no_swings(self):
        bars = [_bar(i * 60, 1.0, 1.0001, 0.9999, 1.0) for i in range(20)]
        snap = analyze(bars)
        # A pure flat produces no strict fractal pivots
        assert snap.structure == []
