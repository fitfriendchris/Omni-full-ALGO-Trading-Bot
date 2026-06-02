#!/usr/bin/env python3
"""
Tests for Order Manager.
"""

import os
import unittest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Create a fresh mock and install it before any module-level imports
mt5_mock = MagicMock()
mt5_mock.ORDER_TYPE_BUY = 0
mt5_mock.ORDER_TYPE_SELL = 1
sys.modules["MetaTrader5"] = mt5_mock

# Import order_manager directly to trigger the module-level imports
import importlib
import src.core.order_manager as om
importlib.reload(om)
from src.core.order_manager import OrderManager, OrderSide


class TestOrderManager(unittest.TestCase):
    def setUp(self):
        self.config = {
            "trading": {
                "magic_number": 202405,
                "comment": "AurumFlow v1.0",
                "max_spread": 50,
                "max_slippage": 30,
                "min_volume": 0.01,
                "max_volume": 100.0,
                "symbol": "XAUUSD",
            },
            "risk": {
                "max_positions": 5,
                "max_daily_loss": 0.05,
            },
        }

    def test_compute_position_size(self):
        """Test position sizing based on risk percentage."""
        mock_info = MagicMock()
        mock_info.volume_step = 0.01
        mt5_mock.symbol_info.return_value = mock_info

        mgr = OrderManager(self.config)
        balance = 10000.0
        entry = 1950.0
        stop_loss = 1920.0

        volume = mgr.compute_position_size(balance, 0.01, entry, stop_loss)
        self.assertAlmostEqual(volume, 0.03, places=2)

    def test_get_open_positions(self):
        """Test fetching open positions."""
        mock_pos = MagicMock()
        mock_pos.ticket = 1001
        mock_pos.symbol = "XAUUSD"
        mock_pos.type = mt5_mock.ORDER_TYPE_BUY
        mock_pos.volume = 0.1
        mock_pos.price_open = 1950.0
        mock_pos.sl = 1940.0
        mock_pos.tp = 1970.0
        mock_pos.profit = 50.0
        mock_pos.swap = -2.0
        mock_pos.commission = -7.0
        mock_pos.time = 1234567890
        mock_pos.magic = 202405
        mock_pos.comment = "AurumFlow"

        mt5_mock.positions_get.return_value = [mock_pos]
        mt5_mock.symbol_info_tick.return_value = None

        mgr = OrderManager(self.config)
        positions = mgr.get_open_positions("XAUUSD")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticket"], 1001)
        self.assertEqual(positions[0]["type"], "buy")
        self.assertEqual(positions[0]["volume"], 0.1)

    def test_get_empty_positions(self):
        """Should return empty list when no positions."""
        mt5_mock.positions_get.return_value = None
        mgr = OrderManager(self.config)
        positions = mgr.get_open_positions()
        self.assertEqual(len(positions), 0)


if __name__ == "__main__":
    unittest.main()