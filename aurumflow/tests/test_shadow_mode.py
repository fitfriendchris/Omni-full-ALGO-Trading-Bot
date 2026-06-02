#!/usr/bin/env python3
"""
Tests for Shadow Mode — virtual portfolio tracking & signal logging.
"""

import unittest
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.shadow_mode import ShadowMode, ShadowSignal
from src.strategies.adapter import Signal, MarketState


class TestShadowMode(unittest.TestCase):
    def setUp(self):
        self.config = {
            "trading": {
                "shadow_initial_balance": 10000.0,
                "enabled": False,
            },
            "risk": {
                "risk_per_trade": 0.01,
                "trailing_atr_mult": 1.8,
                "pyramiding_max": 4,
                "pyramiding_step_atr": 0.7,
            },
        }
        self.shadow = ShadowMode(self.config)

    def tearDown(self):
        # Clean up shadow log files
        log_dir = os.path.dirname(self.shadow.export_signals())
        for f in os.listdir(log_dir):
            os.remove(os.path.join(log_dir, f))

    def _make_state(self, bid=1950.0, ask=1950.4, atr=10.0, ema_fast=1955.0, ema_slow=1945.0, rsi=60.0):
        return MarketState(
            bid=bid, ask=ask, spread=ask - bid, atr=atr,
            ema_fast=ema_fast, ema_slow=ema_slow, rsi=rsi,
            volume=1000, volume_sma=800,
        )

    def test_initialization(self):
        """Shadow mode should start with initial balance."""
        self.assertEqual(self.shadow._virtual_balance, 10000.0)
        self.assertEqual(self.shadow.get_status()["open_positions"], 0)

    def test_log_buy_signal(self):
        """A buy signal should open a virtual position."""
        signal = Signal(action="buy", stop_loss=1920.0, reason="EMA bullish")
        state = self._make_state()
        self.shadow.log_signal(signal, state)

        status = self.shadow.get_status()
        self.assertEqual(status["open_positions"], 1)
        self.assertGreater(status["total_signals"], 0)

    def test_log_hold_signal(self):
        """A hold signal should not open a position."""
        signal = Signal(action="hold", reason="No conditions")
        state = self._make_state()
        self.shadow.log_signal(signal, state)

        status = self.shadow.get_status()
        self.assertEqual(status["open_positions"], 0)

    def test_log_close_signal(self):
        """A close signal should close all virtual positions."""
        buy_signal = Signal(action="buy", stop_loss=1920.0, reason="Entry")
        close_signal = Signal(action="close", reason="Reversal")
        state = self._make_state()

        self.shadow.log_signal(buy_signal, state)
        self.assertEqual(self.shadow.get_status()["open_positions"], 1)

        self.shadow.log_signal(close_signal, state)
        self.assertEqual(self.shadow.get_status()["open_positions"], 0)
        self.assertGreater(self.shadow.get_status()["closed_trades"], 0)

    def test_virtual_pnl(self):
        """Closing at a higher price should show profit."""
        buy_signal = Signal(action="buy", stop_loss=1900.0, reason="Entry")
        state_low = self._make_state(bid=1950.0, ask=1950.4)
        self.shadow.log_signal(buy_signal, state_low)

        # Close at a higher price
        close_signal = Signal(action="close", reason="Take profit")
        state_high = self._make_state(bid=1970.0, ask=1970.4)
        self.shadow.log_signal(close_signal, state_high)

        status = self.shadow.get_status()
        self.assertGreater(status["total_pnl"], 0)
        self.assertGreater(status["virtual_balance"], self.shadow._initial_balance)

    def test_virtual_loss(self):
        """Closing at a lower price should show a loss."""
        buy_signal = Signal(action="buy", stop_loss=1900.0, reason="Entry")
        state_high = self._make_state(bid=1960.0, ask=1960.4)
        self.shadow.log_signal(buy_signal, state_high)

        # Close at a much lower price
        close_signal = Signal(action="close", reason="Stop loss")
        state_low = self._make_state(bid=1940.0, ask=1940.4)
        self.shadow.log_signal(close_signal, state_low)

        status = self.shadow.get_status()
        self.assertLess(status["total_pnl"], 0)

    def test_win_rate(self):
        """Win rate should be calculated correctly."""
        for i in range(5):
            s = Signal(action="buy", stop_loss=1900.0, reason=f"Entry {i}")
            st = self._make_state(bid=1950.0 + i * 0.1, ask=1950.4 + i * 0.1)
            self.shadow.log_signal(s, st)

            s2 = Signal(action="close", reason=f"Exit {i}")
            st2 = self._make_state(bid=1960.0 + i * 0.1, ask=1960.4 + i * 0.1)  # Higher = win
            self.shadow.log_signal(s2, st2)

        status = self.shadow.get_status()
        self.assertEqual(status["closed_trades"], 5)
        self.assertAlmostEqual(status["win_rate"], 1.0, places=4)

    def test_signal_export(self):
        """Exported signals should be valid JSON."""
        signal = Signal(action="buy", stop_loss=1920.0, reason="Test")
        state = self._make_state()
        self.shadow.log_signal(signal, state)
        self.shadow.log_signal(Signal(action="hold"), state)

        path = self.shadow.export_signals()
        self.assertTrue(os.path.exists(path))

        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["signal_count"], 2)

    def test_daily_report(self):
        """Daily report should contain signal stats."""
        signal = Signal(action="buy", stop_loss=1920.0, reason="Test")
        state = self._make_state()
        self.shadow.log_signal(signal, state)

        path = self.shadow.generate_daily_report()
        self.assertTrue(os.path.exists(path))

        with open(path) as f:
            report = json.load(f)
        self.assertIn("signals", report)
        self.assertIn("virtual_portfolio", report)
        self.assertGreater(report["signals"]["total"], 0)

    def test_reset(self):
        """Reset should clear all virtual state."""
        signal = Signal(action="buy", stop_loss=1920.0, reason="Entry")
        state = self._make_state()
        self.shadow.log_signal(signal, state)
        self.assertGreater(self.shadow.get_status()["open_positions"], 0)

        self.shadow.reset()
        self.assertEqual(self.shadow.get_status()["open_positions"], 0)
        self.assertEqual(self.shadow._virtual_balance, 10000.0)

    def test_get_status_keys(self):
        """Status dict should contain expected keys."""
        status = self.shadow.get_status()
        expected_keys = [
            "enabled", "virtual_balance", "equity", "peak_balance",
            "drawdown", "open_positions", "closed_trades", "win_rate",
            "total_signals", "initial_balance", "total_pnl",
        ]
        for key in expected_keys:
            self.assertIn(key, status)


if __name__ == "__main__":
    unittest.main()