#!/usr/bin/env python3
"""
Tests for Risk Manager — small account, cent account, margin level, min lot validation.
"""

import os
import unittest
from unittest.mock import MagicMock
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.risk_manager import RiskManager


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.config = {
            "risk": {
                "risk_per_trade": 0.01,
                "max_daily_loss": 0.05,
                "max_positions": 5,
                "max_drawdown_limit": 0.15,
                "pyramiding_max": 4,
                "pyramiding_step_atr": 0.7,
                "trailing_atr_mult": 1.8,
                "min_margin_level": 100.0,
                "min_position_size": 0.01,
                "skip_below_min_lot": True,
            },
            "trading": {
                "cent_account": False,
                "account_currency": "USD",
            },
        }
        self.mock_pos_mgr = MagicMock()
        self.rm = RiskManager(self.config, self.mock_pos_mgr)

    # ---- Existing tests (unchanged) ----

    def test_can_open_trade_initial(self):
        allowed, reason = self.rm.can_open_trade(10000.0, 0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "OK")

    def test_max_positions_reached(self):
        allowed, reason = self.rm.can_open_trade(10000.0, 5)
        self.assertFalse(allowed)
        self.assertIn("Max positions", reason)

    def test_positions_below_max(self):
        allowed, reason = self.rm.can_open_trade(10000.0, 3)
        self.assertTrue(allowed)

    def test_drawdown_limit(self):
        self.rm.update_peak_balance(10000.0)
        allowed, reason = self.rm.can_open_trade(8000.0, 0)
        self.assertFalse(allowed)
        self.assertIn("drawdown", reason.lower())

    def test_within_drawdown(self):
        self.rm.update_peak_balance(10000.0)
        allowed, reason = self.rm.can_open_trade(9200.0, 0)
        self.assertTrue(allowed)

    def test_consecutive_losses(self):
        for _ in range(3):
            self.rm.record_trade_result(-100, 9900)
        allowed, reason = self.rm.can_open_trade(9900.0, 0)
        self.assertFalse(allowed)
        self.assertIn("consecutive", reason.lower())

    def test_consecutive_losses_reset_on_win(self):
        for _ in range(3):
            self.rm.record_trade_result(-100, 9900)
        self.rm.record_trade_result(200, 10100)
        allowed, reason = self.rm.can_open_trade(10100.0, 0)
        self.assertTrue(allowed)

    def test_pyramiding_limits(self):
        allowed, reason = self.rm.can_pyramid(0, 1950.0, 1940.0, 10.0)
        self.assertTrue(allowed)
        allowed, reason = self.rm.can_pyramid(4, 1950.0, 1940.0, 10.0)
        self.assertFalse(allowed)
        self.assertIn("Pyramiding max", reason)

    def test_pyramiding_price_distance(self):
        allowed, reason = self.rm.can_pyramid(1, 1945.0, 1940.0, 10.0)
        self.assertFalse(allowed)
        allowed, reason = self.rm.can_pyramid(1, 1950.0, 1940.0, 10.0)
        self.assertTrue(allowed)

    def test_get_drawdown(self):
        self.rm.update_peak_balance(10000.0)
        self.assertAlmostEqual(self.rm.get_drawdown(9500.0), 0.05)
        self.assertAlmostEqual(self.rm.get_drawdown(8500.0), 0.15)
        self.assertEqual(self.rm.get_drawdown(10000.0), 0.0)

    def test_get_drawdown_zero_balance(self):
        self.assertEqual(self.rm.get_drawdown(10000.0), 0.0)

    def test_get_status(self):
        self.rm.update_peak_balance(10000.0)
        status = self.rm.get_status(9500.0)
        self.assertIn("balance", status)
        self.assertIn("drawdown", status)
        self.assertIn("can_trade", status)
        self.assertIn("daily_pnl", status)
        self.assertIn("cent_account", status)
        self.assertIn("min_position_size", status)
        self.assertIn("min_margin_level", status)

    # ---- NEW: Margin Level checks ----

    def test_margin_level_below_min(self):
        """Should block trades when margin level is too low."""
        allowed, reason = self.rm.can_open_trade(10000.0, 0, margin_level=50.0)
        self.assertFalse(allowed)
        self.assertIn("Margin level", reason)
        self.assertIn("over-leveraged", reason)

    def test_margin_level_above_min(self):
        """Should allow trades when margin level is sufficient."""
        allowed, reason = self.rm.can_open_trade(10000.0, 0, margin_level=200.0)
        self.assertTrue(allowed)

    def test_margin_level_at_exact_min(self):
        """Should allow trades when margin level is exactly at minimum."""
        allowed, reason = self.rm.can_open_trade(10000.0, 0, margin_level=100.0)
        self.assertTrue(allowed)

    def test_margin_level_none_skips_check(self):
        """Should not block when margin_level is None (e.g., no account data)."""
        allowed, reason = self.rm.can_open_trade(10000.0, 0, margin_level=None)
        self.assertTrue(allowed)

    # ---- NEW: Small $100 account ----

    def test_small_account_position_size(self):
        """$100 account with 1% risk should produce very small lots."""
        volume = self.rm.compute_position_size(100.0, 1950.0, 1920.0)
        # risk_amount = 100 * 0.01 = $1
        # risk_per_unit = 1950 - 1920 = $30
        # units = 1 / 30 = 0.0333 oz
        # volume = 0.0333 / 100 = 0.00033 lots
        # This is below min_lot (0.01), so with skip_below_min_lot=True -> returns 0.0
        self.assertEqual(volume, 0.0)

    def test_small_account_skip_below_min_lot(self):
        """Should skip below-min-lot trades and return 0.0."""
        config = {
            "risk": {
                "risk_per_trade": 0.01,
                "max_daily_loss": 0.05,
                "max_positions": 5,
                "max_drawdown_limit": 0.15,
                "min_margin_level": 100.0,
                "min_position_size": 0.01,
                "skip_below_min_lot": True,
            },
            "trading": {"cent_account": False, "account_currency": "USD"},
        }
        rm = RiskManager(config, self.mock_pos_mgr)
        # $100, risk=$1, SL=30 -> 0.0003 lots < 0.01
        volume = rm.compute_position_size(100.0, 1950.0, 1920.0)
        self.assertEqual(volume, 0.0)

    def test_no_skip_below_min_lot_uses_min(self):
        """When skip_below_min_lot=False, should use min lot and warn."""
        config = {
            "risk": {
                "risk_per_trade": 0.01,
                "max_daily_loss": 0.05,
                "max_positions": 5,
                "max_drawdown_limit": 0.15,
                "min_margin_level": 100.0,
                "min_position_size": 0.01,
                "skip_below_min_lot": False,
            },
            "trading": {"cent_account": False, "account_currency": "USD"},
        }
        rm = RiskManager(config, self.mock_pos_mgr)
        volume = rm.compute_position_size(100.0, 1950.0, 1920.0)
        self.assertEqual(volume, 0.01)  # Uses min lot even though risk would be higher

    # ---- NEW: Cent account tests ----

    def test_cent_account_position_size(self):
        """$100 balance with cent_account=true means 10000 cents -> divide by 100 first."""
        config = {
            "risk": {
                "risk_per_trade": 0.01,
                "max_daily_loss": 0.05,
                "max_positions": 5,
                "max_drawdown_limit": 0.15,
                "min_margin_level": 100.0,
                "min_position_size": 0.01,
                "skip_below_min_lot": True,
            },
            "trading": {"cent_account": True, "account_currency": "USD"},
        }
        rm = RiskManager(config, self.mock_pos_mgr)

        # $100 balance as cents = 10000. 10000 / 100 = $100 effective
        # risk_amount = 100 * 0.01 = $1
        # With SL = 10: risk_per_unit = 10, units = 0.1, volume = 0.001 -> below min
        volume = rm.compute_position_size(10000.0, 1950.0, 1940.0)
        self.assertEqual(volume, 0.0)  # Below min lot, skipped

    def test_cent_account_sufficient_balance(self):
        """$5000 cent balance = $50 -> with tight SL should compute properly."""
        config = {
            "risk": {
                "risk_per_trade": 0.01,
                "max_daily_loss": 0.05,
                "max_positions": 5,
                "max_drawdown_limit": 0.15,
                "min_margin_level": 100.0,
                "min_position_size": 0.01,
                "skip_below_min_lot": True,
            },
            "trading": {"cent_account": True, "account_currency": "USD"},
        }
        rm = RiskManager(config, self.mock_pos_mgr)

        # 500000 cents / 100 = $5000 balance
        # risk_amount = 5000 * 0.01 = $50
        # risk_per_unit = 5, units = 10, volume = 10/100 = 0.1 lots
        volume = rm.compute_position_size(500000.0, 1950.0, 1945.0)
        self.assertEqual(volume, 0.10)

    # ---- NEW: compute_position_size_with_warning ----

    def test_compute_size_with_warning_normal(self):
        """Should return no warning when risk is within tolerance."""
        volume, warning = self.rm.compute_position_size_with_warning(10000.0, 1950.0, 1920.0)
        self.assertGreater(volume, 0)
        self.assertIsNone(warning)

    def test_compute_size_with_warning_below_min(self):
        """Should return warning and 0.0 when below min lot."""
        volume, warning = self.rm.compute_position_size_with_warning(100.0, 1950.0, 1920.0)
        self.assertEqual(volume, 0.0)
        self.assertIsNotNone(warning)
        self.assertIn("skipped", warning.lower())


if __name__ == "__main__":
    unittest.main()