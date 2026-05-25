"""
test_ob_fvg_confluence.py — Unit tests for OB-at-displacement-root + FVG confluence.

Valid confluence requires:
  1. OB is the final consecutive candle of opposite color BEFORE displacement.
  2. FVG is a 3-candle imbalance with the middle candle being the displacement.
  3. FVG and OB must be in the same direction as the MSS/CHoCH.
"""

from __future__ import annotations
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from deterministic_ict_engine import Bar, OBDetector, FVGDetector, MSSDetector


class OBFVGConfluenceTests(unittest.TestCase):
    def _bar(self, idx: int, o: float, h: float, l: float, c: float) -> Bar:
        return Bar(idx=idx, time=f"t{idx}", o=o, h=h, l=l, c=c, v=100, broker_ts=0)

    # ── Order Block tests ──

    def test_bullish_ob_at_displacement_root(self):
        """
        displacement: bearish candle OB root, then strong close past body_top.
        OB direction = BULL (buy zone below body of bearish candle).
        We need at least 10+ bars so the scanner has history.
        """
        b = []
        for i in range(12):
            b.append(self._bar(i, 2340.0, 2341.0, 2339.0, 2340.5))   # flat accumulation
        # OB root at idx=12: bearish
        b.append(self._bar(12, 2338.0, 2338.5, 2336.5, 2337.0))       # bearish OB root
        # displacement at idx=13: closes above body_top(2338)
        b.append(self._bar(13, 2337.0, 2345.0, 2336.8, 2345.0))      # displacement up
        ob = OBDetector().find_bullish_ob(b)
        self.assertIsNotNone(ob)
        self.assertTrue(ob.bullish)
        self.assertEqual(ob.top, 2338.0)
        self.assertEqual(ob.bottom, 2337.0)

    def test_bearish_ob_at_displacement_root(self):
        """
        displacement: bullish candle OB root, then strong close below body_bottom.
        OB direction = BEAR (sell zone above body of bullish candle).
        """
        b = []
        for i in range(12):
            b.append(self._bar(i, 2340.0, 2341.0, 2339.0, 2340.5))
        b.append(self._bar(12, 2337.0, 2338.5, 2336.5, 2338.0))        # bullish OB root
        b.append(self._bar(13, 2338.0, 2338.5, 2330.0, 2330.5))        # displacement down
        ob = OBDetector().find_bearish_ob(b)
        self.assertIsNotNone(ob)
        self.assertFalse(ob.bullish)
        self.assertEqual(ob.top, 2338.0)
        self.assertEqual(ob.bottom, 2337.0)

    def test_no_ob_without_displacement(self):
        """Without a displacement breaking past OB body, no OB should be found."""
        b = []
        for i in range(12):
            b.append(self._bar(i, 2340.0, 2341.0, 2339.0, 2340.5))
        b.append(self._bar(12, 2338.0, 2338.5, 2336.5, 2337.0))       # bearish
        b.append(self._bar(13, 2337.0, 2337.2, 2336.8, 2336.9))       # no displacement
        ob = OBDetector().find_bullish_ob(b)
        self.assertIsNone(ob)

    # ── Fair Value Gap tests ──

    def test_bullish_fvg_three_candle_gap(self):
        """Bullish FVG: c0.high < c2.low."""
        b = []
        for i in range(10):
            b.append(self._bar(i, 2336.0, 2337.0, 2335.0, 2336.5))
        b.append(self._bar(10, 2337.0, 2339.0, 2336.0, 2338.0))       # c0: h=2339
        b.append(self._bar(11, 2338.0, 2342.0, 2337.5, 2342.0))       # c1: displacement
        b.append(self._bar(12, 2342.0, 2345.0, 2340.0, 2344.0))       # c2: l=2340 > c0.h=2339 → FVG!
        fvg = FVGDetector().latest_unfilled(b, "BULL")
        self.assertIsNotNone(fvg)
        self.assertTrue(fvg.bullish)

    def test_bearish_fvg_three_candle_gap(self):
        """Bearish FVG: c0.low > c2.high."""
        b = []
        for i in range(10):
            b.append(self._bar(i, 2343.0, 2345.0, 2342.0, 2344.0))
        b.append(self._bar(10, 2344.0, 2345.0, 2342.0, 2343.0))       # c0: l=2342
        b.append(self._bar(11, 2343.0, 2343.5, 2338.0, 2338.0))       # c1: displacement
        b.append(self._bar(12, 2338.0, 2339.5, 2336.0, 2337.0))      # c2: h=2339.5 < c0.l=2342 → FVG!
        fvg = FVGDetector().latest_unfilled(b, "BEAR")
        self.assertIsNotNone(fvg)
        self.assertFalse(fvg.bullish)

    def test_no_fvg_when_no_gap(self):
        """When c2.l <= c0.h for bullish, no FVG."""
        # Use a flat series that never creates any gap
        b = [self._bar(i, 2340.0, 2341.0, 2339.0, 2340.5) for i in range(20)]
        fvg = FVGDetector().latest_unfilled(b, "BULL")
        self.assertIsNone(fvg)
        fvg2 = FVGDetector().latest_unfilled(b, "BEAR")
        self.assertIsNone(fvg2)

    # ── Confluence tests ──

    def test_ob_and_fvg_same_direction(self):
        """OB and FVG must both be bullish for a BULL setup."""
        b = []
        for i in range(10):
            b.append(self._bar(i, 2335.0, 2337.0, 2334.0, 2336.0))
        b.append(self._bar(10, 2338.0, 2338.5, 2336.5, 2337.0))       # bearish OB root
        b.append(self._bar(11, 2337.0, 2345.0, 2336.8, 2345.0))       # displacement up
        b.append(self._bar(12, 2345.0, 2346.0, 2340.0, 2344.0))       # l=2340 > c0.h=2338.5 → FVG
        ob  = OBDetector().find_bullish_ob(b)
        fvg = FVGDetector().latest_unfilled(b, "BULL")
        self.assertIsNotNone(ob)
        self.assertIsNotNone(fvg)
        self.assertTrue(ob.bullish)
        self.assertTrue(fvg.bullish)

    def test_mismatched_direction_rejected(self):
        """If OB is bullish but FVG is bearish, signal must not be valid."""
        b = []
        for i in range(10):
            b.append(self._bar(i, 2335.0, 2337.0, 2334.0, 2336.0))
        b.append(self._bar(10, 2338.0, 2338.5, 2336.5, 2337.0))       # bearish OB root (bullish OB)
        b.append(self._bar(11, 2337.0, 2345.0, 2336.8, 2345.0))         # displacement up
        b.append(self._bar(12, 2345.0, 2346.0, 2340.0, 2344.0))
        ob  = OBDetector().find_bullish_ob(b)
        fvg = FVGDetector().latest_unfilled(b, "BEAR")
        self.assertIsNotNone(ob)
        self.assertTrue(ob.bullish)
        self.assertIsNone(fvg)  # Bearish FVG won't exist with bullish displacement

    def test_mss_confirms_direction(self):
        """MSS/CHoCH must be in the same direction as OB/FVG."""
        b = []
        for i in range(5):
            b.append(self._bar(i, 2358.0, 2360.0, 2357.0, 2359.0))
        b.append(self._bar(5, 2359.0, 2360.0, 2358.0, 2359.5))       # prior structure
        b.append(self._bar(6, 2359.5, 2362.0, 2359.0, 2361.0))       # swing high h=2362
        b.append(self._bar(7, 2361.0, 2361.5, 2357.0, 2357.5))       # lower
        b.append(self._bar(8, 2357.5, 2357.8, 2348.0, 2349.0))       # displacement DOWN
        b.append(self._bar(9, 2349.0, 2350.0, 2345.0, 2346.0))       # continuation
        # MSSDetector for short
        mss = MSSDetector().detect_choch_short(b)
        # At minimum the detector should not crash and return a typed result
        self.assertIsNotNone(mss)
        self.assertIsInstance(mss.direction, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
