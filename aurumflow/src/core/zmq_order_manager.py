#!/usr/bin/env python3
"""
ZeroMQ Order Manager — sends trading commands to the MQL5 EA via ZMQ.

Provides a drop-in replacement for OrderManager that works cross-platform
by sending JSON commands over ZeroMQ instead of calling the Windows-only
MetaTrader5 Python package.
"""

import logging
from typing import Optional, List, Dict, Any
from enum import Enum

logger = logging.getLogger("aurumflow.zmq_order")


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class ZMQOrderManager:
    """
    Sends trading commands to the MQL5 EA via ZeroMQ.

    Compatible API with the original OrderManager for easy drop-in replacement.
    """

    def __init__(self, config: dict, zmq_connector):
        trading_cfg = config.get("trading", {})
        self._zmq = zmq_connector
        self._symbol = trading_cfg.get("symbol", "XAUUSD")
        self._magic = trading_cfg.get("magic_number", 202405)
        self._comment = trading_cfg.get("comment", "AurumFlow")
        self._max_slippage = trading_cfg.get("max_slippage", 30)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_market_order(
        self,
        side: OrderSide,
        volume: float,
        symbol: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Optional[int]:
        """
        Send a market order to the EA.

        Args:
            side: OrderSide.BUY or OrderSide.SELL
            volume: Lot size
            symbol: Forex symbol (defaults to configured symbol)
            stop_loss: SL price level
            take_profit: TP price level

        Returns:
            Simulated ticket ID (the EA assigns the real ticket).
        """
        symbol = symbol or self._symbol
        action = "BUY" if side == OrderSide.BUY else "SELL"

        command = {
            "action": action,
            "symbol": symbol,
            "volume": float(volume),
            "magic": self._magic,
            "comment": self._comment,
        }
        if stop_loss is not None:
            command["sl"] = stop_loss
        if take_profit is not None:
            command["tp"] = take_profit

        logger.info(f"Sending {action} order: vol={volume}, sym={symbol}, SL={stop_loss}")
        success = self._zmq.send_command(command)

        if success:
            # The EA assigns the real ticket. We return a placeholder
            # that will match when positions are polled.
            return hash((action, symbol, volume, stop_loss)) & 0x7FFFFFFF
        return None

    def close_position(self, ticket: int) -> bool:
        """Send a close command for a specific position."""
        command = {
            "action": "CLOSE",
            "ticket": ticket,
        }
        logger.info(f"Sending CLOSE: ticket={ticket}")
        return self._zmq.send_command(command)

    def modify_position_sl_tp(
        self,
        ticket: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        """Send a modify command to update SL/TP on a position."""
        command = {"action": "MODIFY", "ticket": ticket}
        if stop_loss is not None:
            command["sl"] = stop_loss
        if take_profit is not None:
            command["tp"] = take_profit

        logger.info(f"Sending MODIFY: ticket={ticket}, SL={stop_loss}, TP={take_profit}")
        return self._zmq.send_command(command)

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open positions from the ZMQ connector's cache."""
        return self._zmq.get_open_positions(symbol)

    def get_position_count(self, symbol: Optional[str] = None) -> int:
        """Count open positions."""
        return len(self.get_open_positions(symbol))

    def close_all_positions(self, symbol: Optional[str] = None) -> int:
        """Close all positions for a symbol (or all if no symbol)."""
        command = {"action": "CLOSE_ALL"}
        if symbol:
            command["symbol"] = symbol

        logger.info(f"Sending CLOSE_ALL: symbol={symbol}")
        if self._zmq.send_command(command):
            count = self.get_position_count(symbol)
            return count
        return 0

    def get_pending_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get pending orders. Not yet implemented via ZMQ."""
        return []

    def cancel_pending_order(self, ticket: int) -> bool:
        """Cancel a pending order."""
        logger.warning("Pending order operations not yet supported via ZMQ")
        return False

    def compute_position_size(
        self,
        balance: float,
        risk_per_trade: float,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """
        Compute lot size based on risk percentage.

        Args:
            balance: Account balance
            risk_per_trade: Fraction of balance to risk
            entry_price: Entry price level
            stop_loss: Stop loss price level

        Returns:
            Lot size rounded to standard step (0.01)
        """
        risk_amount = balance * risk_per_trade
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit <= 0:
            logger.warning("Risk per unit <= 0, using minimum lot")
            return 0.01

        # For XAUUSD: 1 lot = 100 oz
        units = risk_amount / risk_per_unit
        volume = units / 100.0
        return max(0.01, round(round(volume / 0.01) * 0.01, 8))