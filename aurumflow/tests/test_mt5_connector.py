#!/usr/bin/env python3
"""
Tests for MT5 Connector (mock-aware, no live MT5 needed).
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock MetaTrader5 before importing
mt5_mock = MagicMock()
sys.modules["MetaTrader5"] = mt5_mock

from src.core.mt5_connector import MT5Connector


class TestMT5Connector(unittest.TestCase):
    def setUp(self):
        self.config = {"mt5": {"server": "ICMarkets-Demo", "login": 0, "password": "", "timeout_seconds": 30, "max_retry_attempts": 3}}
        mt5_mock.reset_mock()
        mt5_mock.initialize.return_value = True
        mt5_mock.terminal_info.return_value = True

    def test_connect_without_creds_fails(self):
        """Should fail when no credentials and terminal not logged in."""
        mt5_mock.account_info.return_value = None
        connector = MT5Connector(self.config)
        result = connector.connect()
        self.assertFalse(result)

    def test_connect_success_with_creds(self):
        """Should connect successfully with valid credentials."""
        mt5_mock.login.return_value = True
        mock_a = MagicMock()
        mock_a.login = 12345
        mock_a.balance = 10000.0
        mock_a.equity = 10500.0
        mock_a.margin = 500.0
        mock_a.margin_free = 9500.0
        mock_a.margin_level = 2000.0
        mock_a.currency = "USD"
        mock_a.profit = 500.0
        mock_a.leverage = 100
        mock_a.server = "ICMarkets-Demo"
        mock_a.name = "Test"
        mt5_mock.account_info.return_value = mock_a

        config = {"mt5": {"server": "ICMarkets-Demo", "login": 12345, "password": "secret", "timeout_seconds": 30, "max_retry_attempts": 3}}
        connector = MT5Connector(config)
        result = connector.connect()
        self.assertTrue(result)

    def test_disconnect(self):
        """Should shutdown MT5 gracefully."""
        connector = MT5Connector(self.config)
        connector._connected = True
        connector.disconnect()
        self.assertTrue(mt5_mock.shutdown.called)
        self.assertFalse(connector._connected)

    def test_get_symbol_info(self):
        """Should fetch symbol info correctly."""
        mock_sym = MagicMock()
        mock_sym.name = "XAUUSD"
        mock_sym.digits = 2
        mock_sym.point = 0.01
        mock_sym.spread = 25
        mock_sym.volume_min = 0.01
        mock_sym.volume_max = 100.0
        mock_sym.volume_step = 0.01
        mock_sym.tick_size = 0.01
        mock_sym.contract_size = 100
        mock_sym.ask = 1950.50
        mock_sym.bid = 1950.30
        mock_sym.last = 1950.40
        mock_sym.visible = True
        mock_sym.free_margin = 9500.0
        mock_sym.trade_mode = 0
        mock_sym.trade_stops_level = 0

        mt5_mock.symbol_info.return_value = mock_sym
        mt5_mock.symbol_info_tick.return_value = None

        connector = MT5Connector(self.config)
        connector._connected = True
        info = connector.get_symbol_info("XAUUSD")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "XAUUSD")
        self.assertEqual(info["spread"], 25)
        self.assertEqual(info["ask"], 1950.50)

    def test_get_last_tick(self):
        """Should fetch tick data."""
        mock_tick = MagicMock()
        mock_tick.time = 1234567890
        mock_tick.bid = 1950.30
        mock_tick.ask = 1950.50
        mock_tick.last = 1950.40
        mock_tick.volume = 100
        mt5_mock.symbol_info_tick.return_value = mock_tick

        connector = MT5Connector(self.config)
        connector._connected = True
        tick = connector.get_last_tick("XAUUSD")
        self.assertIsNotNone(tick)
        self.assertEqual(tick["bid"], 1950.30)
        self.assertEqual(tick["ask"], 1950.50)


if __name__ == "__main__":
    unittest.main()
