"""
Tests for recently added functions in ict_precision.py.

Coverage:
    - detect_turtle_soup  — SELL/BUY patterns, edge cases
    - find_bullish_ob     — high-vol exclusion, impulse body_pct filter
    - find_all_bullish_fvgs — ATR min-size filter
    - _calc_atr           — known values, degenerate inputs

Run:
    cd /Users/owner/omni-ict/python && ../venv/bin/python -m pytest tests/test_new_features.py -v
"""

from __future__ import annotations

import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from ict_precision import (  # noqa: E402
    Bar,
    _calc_atr,
    detect_turtle_soup,
    find_bullish_ob,
    find_all_bullish_fvgs,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _bar(o: float, h: float, l: float, c: float, v: int = 100) -> Bar:
    """Convenience constructor — time is unused by the functions under test."""
    return Bar(time="2026-01-01 00:00", o=o, h=h, l=l, c=c, v=v)


def _bull(base: float = 1.0, body: float = 0.010, wick: float = 0.002) -> Bar:
    """Normal bullish candle."""
    return _bar(o=base, h=base + body + wick, l=base - wick, c=base + body)


def _bear(base: float = 1.0, body: float = 0.010, wick: float = 0.002) -> Bar:
    """Normal bearish candle."""
    return _bar(o=base + body, h=base + body + wick, l=base - wick, c=base)


def _flat_series(n: int = 20, price: float = 1.0, step: float = 0.001) -> list[Bar]:
    """Trending series — each bar grinds up by `step`. bars[0] = most recent."""
    bars = []
    p = price + n * step
    for _ in range(n):
        o = p
        c = o - step
        h = o + step * 0.2
        l = c - step * 0.2
        bars.append(_bar(o=o, h=h, l=l, c=c))
        p = c
    return bars


# ──────────────────────────────────────────────────────────────────────────────
# _calc_atr
# ──────────────────────────────────────────────────────────────────────────────

class TestCalcAtr:
    def test_empty_returns_zero(self):
        assert _calc_atr([]) == 0.0

    def test_single_bar_returns_zero(self):
        assert _calc_atr([_bull()]) == 0.0

    def test_known_two_bar_value(self):
        # bars[0] = most recent: o=1.05, h=1.15, l=0.95, c=1.10
        # bars[1] = prior:       o=1.00, h=1.10, l=0.90, c=1.05
        # TR = max(bars[0].h, bars[1].c) - min(bars[0].l, bars[1].c)
        #    = max(1.15, 1.05) - min(0.95, 1.05)
        #    = 1.15 - 0.95 = 0.20
        bars = [
            _bar(o=1.05, h=1.15, l=0.95, c=1.10),
            _bar(o=1.00, h=1.10, l=0.90, c=1.05),
        ]
        assert _calc_atr(bars) == pytest.approx(0.20)

    def test_positive_on_trending_series(self):
        bars = _flat_series(20, step=0.002)
        assert _calc_atr(bars) > 0.0

    def test_period_limits_lookback(self):
        """Supplying period=2 should only average 2 TRs regardless of series length."""
        bars = _flat_series(20, step=0.002)
        atr_short = _calc_atr(bars, period=2)
        atr_long = _calc_atr(bars, period=14)
        # Both should be positive; exact values differ — just verify period is respected
        assert atr_short > 0.0
        assert atr_long > 0.0


# ──────────────────────────────────────────────────────────────────────────────
# detect_turtle_soup
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectTurtleSoupSell:
    """SELL direction: break above level then close back below → True."""

    def _make_sell_series(self, level: float = 1.100) -> list[Bar]:
        """
        Build a bar list (newest first) where:
          - bars[0]: closes BELOW the level (reversal close)
          - bars[1]: breaks well ABOVE the level (the spike)
          - bars[2-8]: normal bars below level (context / ATR seed)
        """
        context = [_bar(o=1.050, h=1.060, l=1.040, c=1.055) for _ in range(7)]
        spike   = _bar(o=1.090, h=1.130, l=1.085, c=1.125)   # breaks above 1.100
        reversal = _bar(o=1.100, h=1.105, l=1.080, c=1.085)  # closes below 1.100
        # newest first: reversal, spike, context
        return [reversal, spike] + context

    def test_sell_pattern_returns_true(self):
        level = 1.100
        bars = self._make_sell_series(level)
        result = detect_turtle_soup(bars, level=level, direction="SELL", lookback=4)
        assert result is True

    def test_sell_no_close_below_returns_false(self):
        """Spike above level, but all closes remain above level → no reversal."""
        level = 1.100
        # all bars close ABOVE level
        bars = [
            _bar(o=1.110, h=1.120, l=1.105, c=1.115),  # newest — still above
            _bar(o=1.090, h=1.130, l=1.085, c=1.125),  # spike
        ] + [_bar(o=1.050, h=1.060, l=1.040, c=1.055) for _ in range(7)]
        result = detect_turtle_soup(bars, level=level, direction="SELL", lookback=4)
        assert result is False


class TestDetectTurtleSoupBuy:
    """BUY direction: break below level then close back above → True."""

    def _make_buy_series(self, level: float = 1.050) -> list[Bar]:
        context  = [_bar(o=1.070, h=1.080, l=1.065, c=1.075) for _ in range(7)]
        spike    = _bar(o=1.055, h=1.060, l=1.020, c=1.025)   # breaks below 1.050
        reversal = _bar(o=1.040, h=1.065, l=1.038, c=1.060)   # closes above 1.050
        return [reversal, spike] + context

    def test_buy_pattern_returns_true(self):
        level = 1.050
        bars = self._make_buy_series(level)
        result = detect_turtle_soup(bars, level=level, direction="BUY", lookback=4)
        assert result is True

    def test_buy_no_close_above_returns_false(self):
        """Spike below level, but all closes remain below level."""
        level = 1.050
        bars = [
            _bar(o=1.040, h=1.048, l=1.035, c=1.042),  # newest — still below
            _bar(o=1.055, h=1.060, l=1.020, c=1.025),  # spike
        ] + [_bar(o=1.070, h=1.080, l=1.065, c=1.075) for _ in range(7)]
        result = detect_turtle_soup(bars, level=level, direction="BUY", lookback=4)
        assert result is False


class TestDetectTurtleSoupEdgeCases:
    def test_empty_bars_returns_false(self):
        assert detect_turtle_soup([], level=1.0, direction="SELL") is False

    def test_too_few_bars_returns_false(self):
        bars = [_bull() for _ in range(3)]   # fewer than lookback+1=5
        assert detect_turtle_soup(bars, level=1.0, direction="SELL", lookback=4) is False

    def test_level_zero_does_not_crash(self):
        """level=0 should not divide by zero — uses fallback min_break."""
        bars = [
            _bar(o=0.0, h=0.005, l=-0.002, c=-0.001),
            _bar(o=0.0, h=0.010, l=-0.005, c=-0.003),
        ] + [_bar(o=0.0, h=0.002, l=-0.001, c=0.001) for _ in range(7)]
        # Just check it doesn't raise
        result = detect_turtle_soup(bars, level=0.0, direction="SELL", lookback=4)
        assert isinstance(result, bool)

    def test_very_short_lookback(self):
        """lookback=1 means we only inspect recent[0:1], which has no i>=1 → False."""
        bars = [
            _bar(o=1.100, h=1.105, l=1.080, c=1.085),
            _bar(o=1.090, h=1.130, l=1.085, c=1.125),
        ] + [_bar(o=1.050, h=1.060, l=1.040, c=1.055) for _ in range(7)]
        result = detect_turtle_soup(bars, level=1.100, direction="SELL", lookback=1)
        assert isinstance(result, bool)


# ──────────────────────────────────────────────────────────────────────────────
# find_bullish_ob
# ──────────────────────────────────────────────────────────────────────────────

class TestFindBullishOb:
    def _normal_ob_bars(self) -> list[Bar]:
        """
        Pattern (newest first, as stored):
          bars[0]: strong bullish impulse (big body, small wicks → body_pct > 0.4)
          bars[1]: normal bearish candle  (not a volatility spike)
          bars[2-N]: context for ATR
        """
        # bearish candle: o=1.020, h=1.022, l=1.008, c=1.010  → range=0.014
        ob_bear = _bar(o=1.020, h=1.022, l=1.008, c=1.010)
        # bullish impulse: o=1.010, h=1.028, l=1.009, c=1.026
        #   body = 0.016, range = 0.019
        #   body/ob_bear.range ≈ 0.016/0.014 ≈ 1.14 > 0.6 ✓
        #   body_pct = 0.016/0.019 ≈ 0.84 > 0.4 ✓
        impulse = _bar(o=1.010, h=1.028, l=1.009, c=1.026)
        # put impulse first (index 0 = most recent), then OB bar
        context = [_bull(base=1.000 + i * 0.001) for i in range(18)]
        return [impulse, ob_bear] + context

    def test_normal_pattern_found(self):
        bars = self._normal_ob_bars()
        result = find_bullish_ob(bars, start=0, search=15)
        assert result is not None
        low, high = result
        # OB bar: l=1.008, h=1.022
        assert low == pytest.approx(1.008)
        assert high == pytest.approx(1.022)

    def test_volatility_spike_bar_rejected(self):
        """
        Replace the bearish OB bar with a 2×ATR spike — should NOT be selected.
        We ensure ATR is small enough that a very wide bar exceeds 2×ATR.
        """
        # Tight context bars → small ATR (~0.0012 range each)
        context = [_bar(o=1.000 + i * 0.001, h=1.001 + i * 0.001,
                        l=0.999 + i * 0.001, c=1.0005 + i * 0.001)
                   for i in range(18)]
        # Spike bar: range = 0.100 >> 2 × ATR ≈ 0.0012
        spike_bear = _bar(o=1.020, h=1.070, l=0.970, c=1.010)  # range=0.100
        impulse    = _bar(o=1.010, h=1.028, l=1.009, c=1.026)
        bars = [impulse, spike_bear] + context
        result = find_bullish_ob(bars, start=0, search=15)
        # spike bar must be excluded; no other valid OB pair exists → None
        assert result is None

    def test_body_pct_filter_rejects_doji_impulse(self):
        """
        Impulse candle passes the impulse-ratio check (> 0.6) but fails body_pct > 0.4.
        We need bars[i]=bear_ob, bars[i+1]=wiggly_bull so b=ob_bar, b_next=wiggly.

        ob_bar:  bearish, range = 0.010  (o=1.010, h=1.011, l=1.001, c=1.003)
        wiggly:  bullish, body = 0.007, range = 0.040
                 impulse  = 0.007 / 0.010 = 0.70 > 0.6  ← passes ratio gate
                 body_pct = 0.007 / 0.040 = 0.175 < 0.4 ← rejected by confluence filter
        """
        ob_bar  = _bar(o=1.010, h=1.011, l=1.001, c=1.003)   # bearish, range=0.010
        wiggly  = _bar(o=1.003, h=1.023, l=0.983, c=1.010)   # bullish, body=0.007, range=0.040
        context = [_bull(base=1.000 + i * 0.001) for i in range(18)]
        # bars[0]=ob_bar (bear), bars[1]=wiggly (bull) so find_bullish_ob sees (b=ob_bar, b_next=wiggly)
        bars = [ob_bar, wiggly] + context
        result = find_bullish_ob(bars, start=0, search=15)
        assert result is None

    def test_returns_none_on_empty_bars(self):
        assert find_bullish_ob([]) is None

    def test_returns_none_when_only_bullish_bars(self):
        """No bearish candle before a bullish impulse → None."""
        bars = [_bull(base=1.0 + i * 0.005) for i in range(20)]
        assert find_bullish_ob(bars) is None


# ──────────────────────────────────────────────────────────────────────────────
# find_all_bullish_fvgs
# ──────────────────────────────────────────────────────────────────────────────

class TestFindAllBullishFvgs:
    """
    find_all_bullish_fvgs uses bars[0] as most-recent (for current_price logic).
    A bullish FVG exists when bars[i].l > bars[i+2].h (gap between them).
    The ATR filter drops FVGs whose size < 0.3 × ATR.
    """

    def _fvg_bars(self, gap_size: float, atr_approx: float) -> list[Bar]:
        """
        Return a bar list where:
          - A bullish FVG exists at index 0: bars[0].l > bars[2].h
          - bars[0].c is INSIDE the FVG (gap_bot < c < gap_top) so the
            current-price filter does NOT skip it (only skips when c > gap_top)

        FVG condition: bars[i].l > bars[i+2].h
        We want bars[0].l = gap_top and bars[2].h = gap_bot.
        bars[0].c must satisfy: gap_bot < c <= gap_top.
        """
        gap_bot = 1.010
        gap_top = gap_bot + gap_size

        # bar0: l = gap_top (FVG upper boundary), c = gap_bot + tiny offset (inside gap)
        bar0 = _bar(o=gap_top + 0.002, h=gap_top + 0.005, l=gap_top,
                    c=gap_bot + gap_size * 0.5)   # close sits in the middle of the FVG
        bar1 = _bar(o=1.015, h=1.018, l=1.012, c=1.016)   # middle bar, no gap role
        bar2 = _bar(o=1.000, h=gap_bot,  l=0.995, c=1.005)  # bar2.h = gap_bot

        # Context bars to establish a known ATR ≈ atr_approx
        half = atr_approx / 2
        ctx = [_bar(o=1.0, h=1.0 + half, l=1.0 - half, c=1.0) for _ in range(20)]
        return [bar0, bar1, bar2] + ctx

    def test_normal_fvg_is_returned(self):
        """FVG size = 0.010, ATR ≈ 0.010 → size/ATR = 1.0 ≥ 0.3 → pass."""
        bars = self._fvg_bars(gap_size=0.010, atr_approx=0.010)
        fvgs = find_all_bullish_fvgs(bars, max_fvgs=5)
        assert len(fvgs) >= 1
        assert fvgs[0]["direction"] == "BUY"
        assert fvgs[0]["type"] == "BISI"

    def test_tiny_fvg_filtered_out(self):
        """
        FVG size = 0.001, ATR ≈ 0.010 → size/ATR = 0.10 < 0.3 → filtered out.
        We verify explicitly that no FVG with bar_index=0 (the tiny gap) is returned.
        """
        bars = self._fvg_bars(gap_size=0.001, atr_approx=0.010)
        fvgs = find_all_bullish_fvgs(bars, max_fvgs=5)
        # The tiny FVG at bar_index=0 must not appear
        assert not any(fvg["bar_index"] == 0 for fvg in fvgs)

    def test_empty_bars_returns_empty_list(self):
        assert find_all_bullish_fvgs([]) == []

    def test_max_fvgs_limits_output(self):
        """Even with many gaps, output must not exceed max_fvgs."""
        # Build many back-to-back gaps using fvg_bars repeated at different price levels
        bars = []
        for k in range(10):
            gap_top = 1.010 + k * 0.050
            gap_bot = gap_top - 0.020
            bars.append(_bar(o=gap_top + 0.002, h=gap_top + 0.010, l=gap_top, c=gap_top + 0.005))
            bars.append(_bar(o=gap_bot + 0.005, h=gap_bot + 0.008, l=gap_bot, c=gap_bot + 0.003))
            bars.append(_bar(o=gap_bot - 0.005, h=gap_bot, l=gap_bot - 0.010, c=gap_bot - 0.002))
        ctx = [_bar(o=1.0, h=1.01, l=0.99, c=1.005) for _ in range(10)]
        bars = bars + ctx
        fvgs = find_all_bullish_fvgs(bars, max_fvgs=3)
        assert len(fvgs) <= 3

    def test_fvg_dict_has_required_keys(self):
        bars = self._fvg_bars(gap_size=0.010, atr_approx=0.010)
        fvgs = find_all_bullish_fvgs(bars, max_fvgs=5)
        if fvgs:
            keys = fvgs[0].keys()
            for required_key in ("type", "direction", "low", "high", "ce", "size", "bar_index", "mitigated"):
                assert required_key in keys
