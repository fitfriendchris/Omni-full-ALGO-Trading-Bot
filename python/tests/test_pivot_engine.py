"""Unit tests for pivot_engine.py — validates pivot formulas and scoring."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from pivot_engine import (
    Bar, PivotLevel, PivotType,
    calculate_standard_pivots, calculate_fibonacci_pivots,
    calculate_pivots,
    score_pivot_strength,
    detect_multi_tf_confluence,
    score_pivot_reliability,
    identify_reversal_probability,
    find_nearest_pivot_level,
)


def mkbar(o, h, l, c, t=0, v=1000):
    return Bar(open_price=o, high=h, low=l, close=c, volume=v, time=t)


class TestStandardPivots(unittest.TestCase):
    """Standard pivot formula vs. known reference values (TradingView convention)."""

    def test_standard_pivot_basic(self):
        bar = mkbar(o=1.0850, h=1.0900, l=1.0800, c=1.0875)
        pivots = calculate_standard_pivots(bar)
        # Pivot = (H+L+C)/3 = (1.09+1.08+1.0875)/3 = 1.0858333
        self.assertAlmostEqual(pivots["PIVOT"], 1.0858333, places=5)
        # R1 = 2*P - L = 2*1.0858333 - 1.08 = 1.0916667
        self.assertAlmostEqual(pivots["R1"], 1.0916667, places=5)
        # S1 = 2*P - H = 2*1.0858333 - 1.09 = 1.0816667
        self.assertAlmostEqual(pivots["S1"], 1.0816667, places=5)
        # R2 = P + (H-L) = 1.0858333 + 0.01 = 1.0958333
        self.assertAlmostEqual(pivots["R2"], 1.0958333, places=5)
        # S2 = P - (H-L) = 1.0858333 - 0.01 = 1.0758333
        self.assertAlmostEqual(pivots["S2"], 1.0758333, places=5)

    def test_standard_pivot_round_numbers(self):
        bar = mkbar(o=100, h=110, l=90, c=105)
        pivots = calculate_standard_pivots(bar)
        # Pivot = (110+90+105)/3 = 101.667
        self.assertAlmostEqual(pivots["PIVOT"], 101.6667, places=4)
        self.assertAlmostEqual(pivots["R1"], 113.3333, places=4)
        self.assertAlmostEqual(pivots["S1"], 93.3333, places=4)


class TestFibonacciPivots(unittest.TestCase):
    """Fibonacci pivot formula validation."""

    def test_fib_pivot_basic(self):
        bar = mkbar(o=100, h=110, l=90, c=105)
        pivots = calculate_fibonacci_pivots(bar)
        # Pivot = (110+90+105)/3 = 101.667; range = 20
        self.assertAlmostEqual(pivots["PIVOT"], 101.6667, places=4)
        # R1 = P + 0.382*range = 101.667 + 7.64 = 109.307
        self.assertAlmostEqual(pivots["R1"], 109.3067, places=4)
        # S1 = P - 0.382*range
        self.assertAlmostEqual(pivots["S1"], 94.0267, places=4)
        # R2 = P + 0.618*range
        self.assertAlmostEqual(pivots["R2"], 114.0267, places=4)
        # S2 = P - 0.618*range
        self.assertAlmostEqual(pivots["S2"], 89.3067, places=4)

    def test_fib_pivot_symmetry(self):
        """R1/S1 and R2/S2 should be equidistant from PIVOT."""
        bar = mkbar(o=1.20, h=1.25, l=1.15, c=1.22)
        p = calculate_fibonacci_pivots(bar)
        self.assertAlmostEqual(p["R1"] - p["PIVOT"], p["PIVOT"] - p["S1"], places=6)
        self.assertAlmostEqual(p["R2"] - p["PIVOT"], p["PIVOT"] - p["S2"], places=6)


class TestCalculatePivots(unittest.TestCase):
    """High-level calculate_pivots() returns correct PivotLevel objects."""

    def test_returns_both_types(self):
        bars = [mkbar(o=1.08, h=1.09, l=1.07, c=1.085, t=1714000000)]
        result = calculate_pivots(bars, "EURUSD", "M15")
        self.assertIn("STANDARD", result)
        self.assertIn("FIBONACCI", result)

    def test_returns_5_levels_each(self):
        bars = [mkbar(o=1.08, h=1.09, l=1.07, c=1.085, t=1714000000)]
        result = calculate_pivots(bars, "EURUSD", "M15")
        self.assertEqual(len(result["STANDARD"]), 5)
        self.assertEqual(len(result["FIBONACCI"]), 5)
        types = sorted([p.level_type for p in result["STANDARD"]])
        self.assertEqual(types, ["PIVOT", "R1", "R2", "S1", "S2"])

    def test_metadata_populated(self):
        bars = [mkbar(o=1.08, h=1.09, l=1.07, c=1.085, t=1714000000)]
        result = calculate_pivots(bars, "EURUSD", "M15")
        for p in result["STANDARD"]:
            self.assertEqual(p.symbol, "EURUSD")
            self.assertEqual(p.timeframe, "M15")
            self.assertEqual(p.bar_time, 1714000000)
            self.assertEqual(p.pivot_type, "STANDARD")

    def test_empty_bars(self):
        self.assertEqual(calculate_pivots([], "EURUSD", "M15"), {})

    def test_only_standard(self):
        bars = [mkbar(o=1.08, h=1.09, l=1.07, c=1.085, t=1714000000)]
        result = calculate_pivots(bars, "EURUSD", "M15", pivot_types=["STANDARD"])
        self.assertEqual(list(result.keys()), ["STANDARD"])


class TestPivotStrength(unittest.TestCase):
    """Touch count detection."""

    def test_no_touches(self):
        bars = [mkbar(o=1.10, h=1.11, l=1.09, c=1.10) for _ in range(10)]
        touches, last_idx, _ = score_pivot_strength(bars, level=1.20, tolerance_pips=0.001)
        self.assertEqual(touches, 0)
        self.assertEqual(last_idx, -1)

    def test_three_touches_with_recency(self):
        bars = []
        for i in range(20):
            if i in (3, 10, 18):
                bars.append(mkbar(o=1.10, h=1.105, l=1.0999, c=1.10))  # touches 1.0999
            else:
                bars.append(mkbar(o=1.10, h=1.105, l=1.095, c=1.10))
        touches, last_idx, strength = score_pivot_strength(bars, level=1.0999, tolerance_pips=0.0005)
        self.assertEqual(touches, 3)
        self.assertEqual(last_idx, 18)
        self.assertGreater(strength, 0.9)  # Most recent touch is at idx 18 of 20


class TestMultiTFConfluence(unittest.TestCase):
    """Multi-timeframe alignment detection."""

    def test_three_tfs_aligned(self):
        # Build PivotLevels at price 1.0850 across 3 timeframes
        pivots_by_tf = {
            "M15": {"STANDARD": [PivotLevel("EURUSD","M15",0,"STANDARD",1.0850,"S1")]},
            "H1":  {"STANDARD": [PivotLevel("EURUSD","H1", 0,"STANDARD",1.0852,"PIVOT")]},
            "D1":  {"STANDARD": [PivotLevel("EURUSD","D1", 0,"STANDARD",1.0848,"R1")]},
        }
        confluence = detect_multi_tf_confluence(pivots_by_tf, tolerance_pips=0.0005)
        # 1.0850 should align with all 3 timeframes
        self.assertGreaterEqual(max(confluence.values()), 2)

    def test_no_alignment(self):
        pivots_by_tf = {
            "M15": {"STANDARD": [PivotLevel("EURUSD","M15",0,"STANDARD",1.0850,"S1")]},
            "H1":  {"STANDARD": [PivotLevel("EURUSD","H1", 0,"STANDARD",1.1000,"PIVOT")]},
        }
        confluence = detect_multi_tf_confluence(pivots_by_tf, tolerance_pips=0.0005)
        # Far apart — confluence count should be 0 for each
        for level, count in confluence.items():
            self.assertEqual(count, 0)


class TestReliability(unittest.TestCase):
    """Reliability score composition."""

    def test_reliability_in_range(self):
        bars = [mkbar(o=1.10, h=1.11, l=1.09, c=1.10) for _ in range(50)]
        level = PivotLevel("EURUSD","M15",0,"STANDARD",1.105,"PIVOT",
                          touches=2, confluence_count=1)
        score = score_pivot_reliability(level, bars, atr=0.005)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_high_touch_count_high_score(self):
        bars = [mkbar(o=1.10, h=1.11, l=1.09, c=1.10) for _ in range(50)]
        weak = PivotLevel("EURUSD","M15",0,"STANDARD",1.105,"PIVOT", touches=0)
        strong = PivotLevel("EURUSD","M15",0,"STANDARD",1.105,"PIVOT", touches=4, confluence_count=3)
        weak_score = score_pivot_reliability(weak, bars, atr=0.005)
        strong_score = score_pivot_reliability(strong, bars, atr=0.005)
        self.assertGreater(strong_score, weak_score)


class TestReversalProbability(unittest.TestCase):
    """Reversal probability based on candle patterns."""

    def test_bullish_rejection_at_support(self):
        # Build a bullish rejection candle: long lower wick, close near high
        bars = [
            mkbar(o=1.085, h=1.0855, l=1.080, c=1.0853),  # Strong rejection from 1.080
            mkbar(o=1.0853, h=1.0858, l=1.0850, c=1.0854),
        ]
        prob = identify_reversal_probability(bars, pivot_level=1.080, direction="UP", atr=0.001)
        self.assertGreater(prob, 0.5)  # Should detect bullish rejection

    def test_bearish_rejection_at_resistance(self):
        bars = [
            mkbar(o=1.089, h=1.090, l=1.0885, c=1.0892),
            mkbar(o=1.090, h=1.095, l=1.0890, c=1.0893),  # Bearish rejection from 1.095
        ]
        prob = identify_reversal_probability(bars, pivot_level=1.095, direction="DOWN", atr=0.001)
        self.assertGreater(prob, 0.5)


class TestNearestPivot(unittest.TestCase):
    """find_nearest_pivot_level() correctness."""

    def test_finds_nearest(self):
        pivots = [
            PivotLevel("EURUSD","M15",0,"STANDARD",1.0850,"S1"),
            PivotLevel("EURUSD","M15",0,"STANDARD",1.0900,"PIVOT"),
            PivotLevel("EURUSD","M15",0,"STANDARD",1.0950,"R1"),
        ]
        nearest, dist = find_nearest_pivot_level(pivots, 1.0905, max_distance_pips=0.005)
        self.assertEqual(nearest.level, 1.0900)
        self.assertAlmostEqual(dist, 0.0005, places=5)

    def test_max_distance_filter(self):
        pivots = [PivotLevel("EURUSD","M15",0,"STANDARD",1.0850,"S1")]
        # Price is 1.10 — far from 1.0850
        nearest, dist = find_nearest_pivot_level(pivots, 1.10, max_distance_pips=0.005)
        self.assertIsNone(nearest)

    def test_empty(self):
        nearest, dist = find_nearest_pivot_level([], 1.0850)
        self.assertIsNone(nearest)
        self.assertEqual(dist, float('inf'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
