#!/usr/bin/env python3
"""
Tests for ZeroMQ MT5 Connector (mock-based, no live MT5 or ZMQ needed).
"""

import os
import unittest
import sys
import json
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock zmq before importing the module
zmq_mock = MagicMock()
sys.modules["zmq"] = zmq_mock

from src.core.mt5_zmq_connector import (
    MT5ZMQConnector,
    ZMQTick,
    ZMQAccountInfo,
    ZMQPosition,
)
from src.core.zmq_order_manager import ZMQOrderManager, OrderSide


class TestZMQConnector(unittest.TestCase):
    """Test the ZMQ connector's message parsing and API."""

    def setUp(self):
        self.config = {
            "zmq": {
                "pull_addr": "tcp://localhost:5555",
                "push_addr": "tcp://localhost:5556",
                "timeout_ms": 100,
                "reconnect_delay": 0.1,
            }
        }

    def test_connect_mock(self):
        """Should connect in mock mode when pyzmq is mocked."""
        conn = MT5ZMQConnector(self.config)
        result = conn.connect()
        self.assertTrue(result)
        conn.disconnect()

    def test_process_tick_message(self):
        """Should parse tick JSON correctly."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        tick_msg = {
            "type": "tick",
            "symbol": "XAUUSD",
            "bid": 1950.50,
            "ask": 1950.90,
            "last": 1950.70,
            "volume": 100,
            "time": 1234567890,
        }

        conn._process_message(tick_msg)
        self.assertIsNotNone(conn._last_tick)
        self.assertEqual(conn._last_tick.bid, 1950.50)
        self.assertEqual(conn._last_tick.ask, 1950.90)
        self.assertEqual(conn._last_tick.symbol, "XAUUSD")

    def test_get_last_tick(self):
        """get_last_tick should return compatible dict format."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        tick_msg = {
            "type": "tick",
            "symbol": "XAUUSD",
            "bid": 1950.30,
            "ask": 1950.70,
            "last": 1950.50,
            "volume": 50,
            "time": 1234567890,
        }
        conn._process_message(tick_msg)

        tick = conn.get_last_tick()
        self.assertIsNotNone(tick)
        self.assertEqual(tick["bid"], 1950.30)
        self.assertEqual(tick["ask"], 1950.70)
        self.assertEqual(tick["time"], 1234567890)
        self.assertEqual(tick["volume"], 50)

    def test_process_account_message(self):
        """Should parse account JSON correctly."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        acct_msg = {
            "type": "account",
            "login": 12345,
            "server": "ICMarkets-Demo",
            "balance": 10000.0,
            "equity": 10500.0,
            "margin": 500.0,
            "margin_free": 9500.0,
            "margin_level": 2000.0,
            "currency": "USD",
            "leverage": 100,
        }

        conn._process_message(acct_msg)
        self.assertIsNotNone(conn._last_account)
        self.assertEqual(conn._last_account.balance, 10000.0)
        self.assertEqual(conn._last_account.margin_level, 2000.0)

    def test_get_account_info(self):
        """get_account_info should return compatible dict format."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        conn._process_message({
            "type": "account",
            "login": 12345,
            "server": "ICMarkets-Demo",
            "balance": 10000.0,
            "equity": 10500.0,
            "margin": 500.0,
            "margin_free": 9500.0,
            "margin_level": 2000.0,
            "currency": "USD",
            "leverage": 100,
        })

        info = conn.get_account_info()
        self.assertIsNotNone(info)
        self.assertEqual(info["balance"], 10000.0)
        self.assertEqual(info["margin_level"], 2000.0)

    def test_process_positions_message(self):
        """Should parse positions JSON correctly."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        pos_msg = {
            "type": "positions",
            "data": [
                {
                    "ticket": 1001,
                    "symbol": "XAUUSD",
                    "type": "buy",
                    "volume": 0.1,
                    "price_open": 1950.0,
                    "sl": 1940.0,
                    "tp": 1970.0,
                    "profit": 50.0,
                    "swap": -2.0,
                    "commission": -7.0,
                    "time": 1234567890,
                    "magic": 202405,
                }
            ],
        }

        conn._process_message(pos_msg)
        self.assertEqual(len(conn._last_positions), 1)
        self.assertEqual(conn._last_positions[0].ticket, 1001)
        self.assertEqual(conn._last_positions[0].type, "buy")

    def test_get_open_positions(self):
        """get_open_positions should return compatible dict list."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        conn._process_message({
            "type": "positions",
            "data": [
                {
                    "ticket": 1001,
                    "symbol": "XAUUSD",
                    "type": "buy",
                    "volume": 0.1,
                    "price_open": 1950.0,
                    "sl": 1940.0,
                    "tp": 1970.0,
                    "profit": 50.0,
                    "swap": 0,
                    "commission": 0,
                    "time": 1234567890,
                    "magic": 202405,
                }
            ],
        })

        positions = conn.get_open_positions("XAUUSD")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticket"], 1001)
        self.assertEqual(positions[0]["type"], "buy")
        self.assertEqual(positions[0]["volume"], 0.1)

    def test_send_command(self):
        """Should send command in mock mode."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        result = conn.send_command({"action": "BUY", "symbol": "XAUUSD", "volume": 0.1})
        self.assertTrue(result)

    def test_disconnect(self):
        """Disconnect should clean up."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True
        conn.disconnect()
        self.assertFalse(conn._connected)

    def test_get_symbol_info_from_tick(self):
        """get_symbol_info should derive from last tick data."""
        conn = MT5ZMQConnector(self.config)
        conn._connected = True

        conn._process_message({
            "type": "tick",
            "symbol": "XAUUSD",
            "bid": 1950.30,
            "ask": 1950.90,
            "last": 1950.60,
            "volume": 100,
            "time": 1234567890,
        })

        info = conn.get_symbol_info("XAUUSD")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "XAUUSD")
        self.assertGreater(info["spread"], 0)


class TestZMQOrderManager(unittest.TestCase):
    """Test the ZMQ order manager."""

    def setUp(self):
        self.config = {"trading": {"symbol": "XAUUSD", "magic_number": 202405, "comment": "AurumFlow", "max_slippage": 30}}
        self.mock_conn = MagicMock()
        self.mock_conn.send_command.return_value = True

    def test_open_buy_order(self):
        """Should send BUY command via ZMQ."""
        mgr = ZMQOrderManager(self.config, self.mock_conn)
        ticket = mgr.open_market_order(OrderSide.BUY, 0.1, stop_loss=1940.0)
        self.assertIsNotNone(ticket)
        self.mock_conn.send_command.assert_called_once()

    def test_open_sell_order(self):
        """Should send SELL command via ZMQ."""
        mgr = ZMQOrderManager(self.config, self.mock_conn)
        ticket = mgr.open_market_order(OrderSide.SELL, 0.05)
        self.assertIsNotNone(ticket)

    def test_close_position(self):
        """Should send CLOSE command."""
        mgr = ZMQOrderManager(self.config, self.mock_conn)
        result = mgr.close_position(1001)
        self.assertTrue(result)
        self.mock_conn.send_command.assert_called_once_with({"action": "CLOSE", "ticket": 1001})

    def test_modify_sl_tp(self):
        """Should send MODIFY command with SL."""
        mgr = ZMQOrderManager(self.config, self.mock_conn)
        result = mgr.modify_position_sl_tp(1001, stop_loss=1945.0)
        self.assertTrue(result)
        self.mock_conn.send_command.assert_called_once_with({"action": "MODIFY", "ticket": 1001, "sl": 1945.0})

    def test_compute_position_size(self):
        """Should compute lot size correctly."""
        mgr = ZMQOrderManager(self.config, self.mock_conn)
        volume = mgr.compute_position_size(10000.0, 0.01, 1950.0, 1920.0)
        # risk_amount = 10000 * 0.01 = 100
        # risk_per_unit = 1950 - 1920 = 30
        # units = 100 / 30 = 3.33
        # volume = 3.33 / 100 = 0.0333 -> rounded to 0.03
        self.assertEqual(volume, 0.03)

    def test_close_all_positions(self):
        """Should send CLOSE_ALL command."""
        mgr = ZMQOrderManager(self.config, self.mock_conn)
        self.mock_conn.get_open_positions.return_value = []
        result = mgr.close_all_positions()
        self.mock_conn.send_command.assert_called_once_with({"action": "CLOSE_ALL"})


if __name__ == "__main__":
    unittest.main()