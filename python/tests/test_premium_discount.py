"""
test_premium_discount.py — Unit tests for Premium/Discount placement validation.

Per user's ICT protocol:
  - Discount for buys (BULL): entry must sit between 21%-50% retracement
    of the displacement leg (from sweep_low to swing_high broken).
  - Premium for sells (BEAR): entry must sit between 50%-79% retracement
    of the displacement leg (from swing_low broken to sweep_high).
"""

from __future__ import annotations
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from deterministic_ict_engine import _fib_retracement, _premium_discount_check


class PremiumDiscountTests(unittest.TestCase):
    def test_fib_retracement_21_and_50(self):
        """Fib levels at low=2330, high=2345."""
        low, high = 2330.0, 2345.0
        self.assertAlmostEqual(_fib_retracement(low, high, 0.21), low + 0.21 * (high - low))
        self.assertAlmostEqual(_fib_retracement(low, high, 0.50), low + 0.50 * (high - low))

    def test_bullish_discount_below_midline(self):
        """BULL entry at 37.5% retracement should pass (between 21%-50%)."""
        # displacement leg: low=2330, high=2345
        # entry at 2335.625 = 2330 + 0.375*(2345-2330)
        # This is between 21% (2333.15) and 50% (2337.5)
        entry = 2335.625
        low, high = 2330.0, 2345.0
        self.assertTrue(_premium_discount_check(entry, low, high, "BULL", "XAUUSD"))

    def test_bullish_discount_at_midline(self):
        """BULL entry exactly at 50% = midline should pass (upper boundary)."""
        entry = 2337.5  # 50% of 2330-2345
        low, high = 2330.0, 2345.0
        self.assertTrue(_premium_discount_check(entry, low, high, "BULL", "XAUUSD"))

    def test_bullish_discount_too_deep(self):
        """BULL entry at 10% (too close to low) should FAIL."""
        entry = 2331.5  # ~10% retracement
        low, high = 2330.0, 2345.0
        self.assertFalse(_premium_discount_check(entry, low, high, "BULL", "XAUUSD"))

    def test_bullish_discount_above_midline(self):
        """BULL entry above 50% (e.g. 60%) should FAIL — that's premium."""
        entry = 2339.0  # ~60% retracement
        low, high = 2330.0, 2345.0
        self.assertFalse(_premium_discount_check(entry, low, high, "BULL", "XAUUSD"))

    def test_bearish_premium_above_midline(self):
        """BEAR entry at 65% retracement should pass (between 50%-79%)."""
        entry = 2339.75  # 2330 + 0.65*(2345-2330)
        low, high = 2330.0, 2345.0
        self.assertTrue(_premium_discount_check(entry, low, high, "BEAR", "XAUUSD"))

    def test_bearish_premium_at_79(self):
        """BEAR entry exactly at 79% (upper bound) should pass."""
        entry = 2330 + 0.79 * (2345 - 2330)
        low, high = 2330.0, 2345.0
        self.assertTrue(_premium_discount_check(entry, low, high, "BEAR", "XAUUSD"))

    def test_bearish_premium_too_high(self):
        """BEAR entry above 79% should FAIL."""
        entry = 2343.0  # ~87% retracement
        low, high = 2330.0, 2345.0
        self.assertFalse(_premium_discount_check(entry, low, high, "BEAR", "XAUUSD"))

    def test_bearish_premium_below_midline(self):
        """BEAR entry below 50% should FAIL — that's discount zone."""
        entry = 2333.0  # ~20% retracement
        low, high = 2330.0, 2345.0
        self.assertFalse(_premium_discount_check(entry, low, high, "BEAR", "XAUUSD"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
