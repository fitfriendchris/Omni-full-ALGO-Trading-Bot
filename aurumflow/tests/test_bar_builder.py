#!/usr/bin/env python3
"""
Tests for Bar/Candle Builder.
Tests tick aggregation into OHLC candles, indicator computation,
and multi-timeframe management.
"""

import os
import unittest
import sys
import time
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.core.bar_builder import BarBuilder, MultiTimeframeBarBuilder, Candle


class TestBarBuilder(unittest.TestCase):
    """Test the core BarBuilder - 1-minute candle aggregation."""

    def setUp(self):
        self.bb = BarBuilder(timeframe_sec=60, buffer_size=10)

    def test_initialization(self):
        self.assertEqual(self.bb.get_candle_count(), 0)
        self.assertIsNone(self.bb.get_current_candle())

    def test_single_tick_creates_candle(self):
        tick = {"time": int(time.time()), "bid": 1950.0, "ask": 1950.4, "volume": 1.0}
        result = self.bb.update_from_tick_dict(tick)
        self.assertIsNone(result)
        current = self.bb.get_current_candle()
        self.assertIsNotNone(current)
        self.assertAlmostEqual(current["open"], 1950.2)
        self.assertAlmostEqual(current["high"], 1950.2)
        self.assertAlmostEqual(current["low"], 1950.2)
        self.assertAlmostEqual(current["close"], 1950.2)

    def test_two_ticks_same_candle(self):
        base_time = int(time.time()) // 60 * 60
        t1 = {"time": base_time + 5, "bid": 1950.0, "ask": 1950.4, "volume": 1.0}
        self.bb.update_from_tick_dict(t1)
        t2 = {"time": base_time + 10, "bid": 1951.0, "ask": 1951.4, "volume": 2.0}
        result = self.bb.update_from_tick_dict(t2)
        self.assertIsNone(result)
        cur = self.bb.get_current_candle()
        self.assertAlmostEqual(cur["open"], 1950.2)
        self.assertAlmostEqual(cur["high"], 1951.2)
        self.assertAlmostEqual(cur["low"], 1950.2)
        self.assertAlmostEqual(cur["close"], 1951.2)
        self.assertAlmostEqual(cur["volume"], 3.0)

    def test_tick_crosses_boundary(self):
        base_time = int(time.time()) // 60 * 60
        t1 = {"time": base_time + 5, "bid": 1950.0, "ask": 1950.4, "volume": 1.0}
        self.bb.update_from_tick_dict(t1)
        t2 = {"time": base_time + 65, "bid": 1952.0, "ask": 1952.4, "volume": 2.0}
        result = self.bb.update_from_tick_dict(t2)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.open, 1950.2)
        self.assertEqual(self.bb.get_candle_count(), 1)

    def test_candle_count_and_buffer(self):
        base_time = int(time.time()) // 60 * 60
        for i in range(20):
            t = base_time + i * 60 + 5
            tk = {"time": t, "bid": 1950.0 + i * 0.1, "ask": 1950.4 + i * 0.1, "volume": 1.0}
            self.bb.update_from_tick_dict(tk)
        self.assertGreater(self.bb.get_candle_count(), 0)

    def test_to_dataframe(self):
        base_time = int(time.time()) // 60 * 60
        for i in range(4):
            t = base_time + i * 60 + 5
            tk = {"time": t, "bid": 1950.0 + i, "ask": 1950.4 + i, "volume": 1.0 + i}
            self.bb.update_from_tick_dict(tk)
        df = self.bb.to_dataframe()
        # 3 completed (4th is in-progress)
        self.assertEqual(len(df), 3)
        self.assertIn("Open", df.columns)
        self.assertIn("Close", df.columns)
        self.assertIn("High", df.columns)
        self.assertIn("Low", df.columns)
        self.assertIn("Volume", df.columns)

    def test_reset(self):
        base_time = int(time.time()) // 60 * 60
        tk = {"time": base_time + 5, "bid": 1950.0, "ask": 1950.4, "volume": 1.0}
        self.bb.update_from_tick_dict(tk)
        self.assertIsNotNone(self.bb.get_current_candle())
        self.bb.reset()
        self.assertEqual(self.bb.get_candle_count(), 0)
        self.assertIsNone(self.bb.get_current_candle())

    def test_with_random_walk(self):
        import random
        base_time = int(time.time()) // 60 * 60
        price = 1950.0
        for i in range(100):
            t = base_time + i * 5
            price += random.gauss(0, 0.3)
            tk = {"time": t, "bid": price - 0.1, "ask": price + 0.1, "volume": random.random() * 5}
            self.bb.update_from_tick_dict(tk)
        future = base_time + 100 * 5 + 70
        tk = {"time": future, "bid": price, "ask": price + 0.2, "volume": 1.0}
        self.bb.update_from_tick_dict(tk)
        df = self.bb.to_dataframe()
        self.assertGreater(len(df), 0)
        for _, row in df.iterrows():
            self.assertGreater(row["Open"], 0)
            self.assertGreater(row["High"], 0)
            self.assertGreater(row["Low"], 0)
            self.assertGreater(row["Close"], 0)

    def test_get_last_n_candles(self):
        base_time = int(time.time()) // 60 * 60
        for i in range(10):
            t = base_time + i * 60 + 5
            tk = {"time": t, "bid": 1950.0 + i * 0.1, "ask": 1950.4 + i * 0.1, "volume": 1.0}
            self.bb.update_from_tick_dict(tk)
        candles = self.bb.get_last_n_candles(5)
        self.assertEqual(len(candles), 5)

    def test_compute_indicators_empty(self):
        indicators = self.bb.compute_indicators()
        self.assertEqual(indicators["rsi"], 50.0)
        self.assertEqual(indicators["atr"], 0.0)
        self.assertEqual(indicators["close"], 0.0)

    def test_volume_accumulation(self):
        base_time = int(time.time()) // 60 * 60
        t1 = {"time": base_time + 5, "bid": 1950.0, "ask": 1950.4, "volume": 1.5}
        self.bb.update_from_tick_dict(t1)
        t2 = {"time": base_time + 10, "bid": 1950.5, "ask": 1950.9, "volume": 2.5}
        self.bb.update_from_tick_dict(t2)
        cur = self.bb.get_current_candle()
        self.assertAlmostEqual(cur["volume"], 4.0)


class TestMultiTimeframeBarBuilder(unittest.TestCase):

    def setUp(self):
        self.mtbb = MultiTimeframeBarBuilder(buffer_size=20)

    def test_initialization(self):
        self.assertIn("1m", self.mtbb._builders)
        self.assertIn("5m", self.mtbb._builders)

    def test_update_feeds_all_timeframes(self):
        tk = {"time": int(time.time()), "bid": 1950.0, "ask": 1950.4, "volume": 1.0}
        results = self.mtbb.update(tk)
        self.assertIn("1m", results)
        self.assertIn("5m", results)

    def test_get_builder(self):
        b = self.mtbb.get_builder("5m")
        self.assertIsNotNone(b)
        self.assertEqual(b._timeframe_sec, 300)
        b = self.mtbb.get_builder("1m")
        self.assertIsNotNone(b)
        self.assertEqual(b._timeframe_sec, 60)

    def test_get_market_state_with_data(self):
        base_time = int(time.time()) // 3600 * 3600
        for i in range(25):
            t = base_time + i * 60 + 5
            price = 1950.0 + math.sin(i * 0.5) * 2.0
            tk = {"time": t, "bid": price - 0.2, "ask": price + 0.2, "volume": 1.0 + i}
            self.mtbb.update(tk)
        state = self.mtbb.get_market_state(1951.0, 1951.4, {})
        self.assertIsNotNone(state)
        self.assertGreater(state.atr, 0)
        self.assertNotEqual(state.ema_fast, 0)
        self.assertNotEqual(state.ema_slow, 0)
        self.assertEqual(state.bid, 1951.0)
        self.assertEqual(state.ask, 1951.4)

    def test_get_market_state_empty(self):
        state = self.mtbb.get_market_state(1950.0, 1950.4, {})
        self.assertIsNotNone(state)
        self.assertEqual(state.rsi, 50.0)
        # Spread = 0.4, fallback atr = spread * 10 = 4.0
        self.assertAlmostEqual(state.atr, 4.0, places=1)

    def test_reset_all(self):
        base_time = int(time.time()) // 60 * 60
        tk = {"time": base_time + 5, "bid": 1950.0, "ask": 1950.4, "volume": 1.0}
        self.mtbb.update(tk)
        self.mtbb.reset()
        self.assertEqual(self.mtbb.get_builder("1m").get_candle_count(), 0)
        self.assertEqual(self.mtbb.get_builder("5m").get_candle_count(), 0)


if __name__ == "__main__":
    unittest.main()