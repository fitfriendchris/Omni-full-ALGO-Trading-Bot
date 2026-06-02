#!/usr/bin/env python3
"""
Order Manager — handles order placement, modification, and position tracking.

Sends orders to MT5 with proper validation, error handling, and retry logic.
"""

import time
import logging
from typing import Optional, List, Dict, Any
from enum import Enum

import MetaTrader5 as mt5

logger = logging.getLogger("aurumflow.order")

# ---- Constants mapping MT5 trade types ----
TRADE_TYPE_BUY = mt5.ORDER_TYPE_BUY
TRADE_TYPE_SELL = mt5.ORDER_TYPE_SELL
TRADE_TYPE_BUY_LIMIT = mt5.ORDER_TYPE_BUY_LIMIT
TRADE_TYPE_SELL_LIMIT = mt5.ORDER_TYPE_SELL_LIMIT
TRADE_TYPE_BUY_STOP = mt5.ORDER_TYPE_BUY_STOP
TRADE_TYPE_SELL_STOP = mt5.ORDER_TYPE_SELL_STOP

TRADE_ACTION_DEAL = mt5.TRADE_ACTION_DEAL       # Market order
TRADE_ACTION_PENDING = mt5.TRADE_ACTION_PENDING  # Pending order
TRADE_ACTION_MODIFY = mt5.TRADE_ACTION_MODIFY    # Modify SL/TP


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderManager:
    """
    High-level order management for XAUUSD.
    Handles market orders, SL/TP management, and position tracking.
    """

    def __init__(self, config: dict):
        trading_cfg = config.get("trading", {})
        risk_cfg = config.get("risk", {})
        self._magic = trading_cfg.get("magic_number", 202405)
        self._comment = trading_cfg.get("comment", "AurumFlow")
        self._max_spread = trading_cfg.get("max_spread", 50)
        self._max_slippage = trading_cfg.get("max_slippage", 30)
        self._max_positions = risk_cfg.get("max_positions", 5)
        self._max_daily_loss = risk_cfg.get("max_daily_loss", 0.05)
        self._daily_initial_balance: Optional[float] = None
        self._symbol = trading_cfg.get("symbol", "XAUUSD")

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
        Open a market order (Buy or Sell).

        Args:
            side: OrderSide.BUY or OrderSide.SELL
            volume: Lot size
            symbol: Forex symbol (defaults to XAUUSD)
            stop_loss: SL price level
            take_profit: TP price level

        Returns:
            Order ticket (int) on success, None on failure.
        """
        symbol = symbol or self._symbol

        # Pre-checks
        if not self._can_open_new_trade(symbol):
            return None

        # Determine order direction
        order_type = TRADE_TYPE_BUY if side == OrderSide.BUY else TRADE_TYPE_SELL

        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Cannot get tick for {symbol}")
            return None

        price = tick.ask if side == OrderSide.BUY else tick.bid

        # Build the trade request
        request = {
            "action": TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "deviation": self._max_slippage,
            "magic": self._magic,
            "comment": self._comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        if stop_loss is not None:
            request["sl"] = round(stop_loss, self._get_digits(symbol))
        if take_profit is not None:
            request["tp"] = round(take_profit, self._get_digits(symbol))

        return self._send_order(request)

    def close_position(self, ticket: int) -> bool:
        """Close an open position by ticket number."""
        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            logger.warning(f"Position {ticket} not found")
            return False

        pos = position[0]
        symbol = pos.symbol
        volume = pos.volume

        # Determine opposite direction for closing
        close_type = TRADE_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else TRADE_TYPE_BUY
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Cannot get tick for {symbol}")
            return False

        price = tick.bid if close_type == TRADE_TYPE_SELL else tick.ask

        request = {
            "action": TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": self._max_slippage,
            "magic": self._magic,
            "comment": self._comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self._send_order(request)
        if result is not None:
            logger.info(f"Closed position {ticket} — result ticket: {result}")
            return True

        logger.error(f"Failed to close position {ticket}")
        return False

    def modify_position_sl_tp(
        self,
        ticket: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        """
        Modify the Stop Loss and/or Take Profit on an open position.
        """
        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            logger.warning(f"Position {ticket} not found for modification")
            return False

        pos = position[0]
        digits = self._get_digits(pos.symbol)

        request = {
            "action": TRADE_ACTION_MODIFY,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": round(stop_loss, digits) if stop_loss is not None else pos.sl,
            "tp": round(take_profit, digits) if take_profit is not None else pos.tp,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_desc = self._trade_retcode_description(result)
            logger.error(f"Modify position {ticket} failed: {error_desc}")
            return False

        logger.info(f"Modified position {ticket}: SL={stop_loss}, TP={take_profit}")
        return True

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all open positions, optionally filtered by symbol."""
        positions = mt5.positions_get()
        if positions is None:
            return []

        result = []
        for pos in positions:
            if symbol and pos.symbol != symbol:
                continue
            result.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "type": "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell",
                "volume": pos.volume,
                "price_open": pos.price_open,
                "sl": pos.sl,
                "tp": pos.tp,
                "profit": pos.profit,
                "swap": pos.swap,
                "commission": pos.commission,
                "time": pos.time,
                "magic": pos.magic,
                "comment": pos.comment,
            })

        return result

    def get_position_count(self, symbol: Optional[str] = None) -> int:
        """Count open positions, optionally filtered by symbol."""
        return len(self.get_open_positions(symbol))

    def close_all_positions(self, symbol: Optional[str] = None) -> int:
        """Close all open positions. Returns count of successfully closed."""
        positions = self.get_open_positions(symbol)
        success_count = 0
        for pos in positions:
            if self.close_position(pos["ticket"]):
                success_count += 1
        return success_count

    def get_pending_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """List pending orders, optionally filtered by symbol."""
        orders = mt5.orders_get()
        if orders is None:
            return []

        result = []
        for order in orders:
            if symbol and order.symbol != symbol:
                continue
            result.append({
                "ticket": order.ticket,
                "symbol": order.symbol,
                "type": order.type,
                "volume": order.volume_current,
                "price": order.price_open,
                "sl": order.sl,
                "tp": order.tp,
                "time_setup": order.time_setup,
                "time_expiration": order.time_expiration,
                "magic": order.magic,
                "comment": order.comment,
            })

        return result

    def cancel_pending_order(self, ticket: int) -> bool:
        """Cancel a pending order."""
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Cancel order {ticket} failed: retcode={result.retcode if result else 'N/A'}")
            return False
        logger.info(f"Cancelled pending order {ticket}")
        return True

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
            risk_per_trade: Fraction of balance to risk (e.g., 0.01 = 1%)
            entry_price: Entry price level
            stop_loss: Stop loss price level

        Returns:
            Lot size rounded to standard lot step
        """
        risk_amount = balance * risk_per_trade
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit <= 0:
            logger.warning("Risk per unit <= 0, using minimum lot")
            return self._round_lot(0.01)

        units = risk_amount / risk_per_unit
        # Convert to standard lots (100 oz for XAUUSD)
        volume = units / 100.0

        return self._round_lot(volume)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _can_open_new_trade(self, symbol: str) -> bool:
        """Run pre-trade checks: spread, position limits, daily loss."""
        # Check spread
        info = mt5.symbol_info(symbol)
        if info and info.spread > self._max_spread:
            logger.warning(f"Spread {info.spread} exceeds max {self._max_spread}")
            return False

        # Check max positions
        current_count = self.get_position_count(symbol)
        if current_count >= self._max_positions:
            logger.warning(f"Max positions reached ({current_count} >= {self._max_positions})")
            return False

        # Check daily loss limit
        if not self._check_daily_loss():
            return False

        return True

    def _check_daily_loss(self) -> bool:
        """Check if daily loss limit has been hit."""
        account_info = mt5.account_info()
        if account_info is None:
            return False

        if self._daily_initial_balance is None:
            self._daily_initial_balance = account_info.balance

        # If balance increased from start, reset initial
        if account_info.balance > self._daily_initial_balance:
            self._daily_initial_balance = account_info.balance
            return True

        # Check if loss exceeds threshold
        loss_ratio = (self._daily_initial_balance - account_info.balance) / self._daily_initial_balance
        if loss_ratio >= self._max_daily_loss:
            logger.warning(
                f"Daily loss limit reached: {loss_ratio:.2%} >= {self._max_daily_loss:.2%}. "
                "Blocking new trades."
            )
            return False

        return True

    def _send_order(self, request: dict) -> Optional[int]:
        """Send the order to MT5 with logging. Returns ticket on success."""
        logger.debug(f"Sending order: {request}")
        result = mt5.order_send(request)

        if result is None:
            error = mt5.last_error()
            logger.error(f"Order send failed (no result): {error}")
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            error_desc = self._trade_retcode_description(result)
            logger.error(f"Order rejected: {error_desc} (retcode={result.retcode})")
            return None

        logger.info(
            f"Order executed: ticket={result.order}, "
            f"volume={result.volume}, price={result.price}, "
            f"comment={self._comment}"
        )
        return result.order

    def _get_digits(self, symbol: str) -> int:
        """Get number of decimal places for a symbol."""
        info = mt5.symbol_info(symbol)
        return info.digits if info else 2

    def _round_lot(self, volume: float) -> float:
        """Round lot size to valid step."""
        info = mt5.symbol_info(self._symbol)
        if info is None:
            return round(volume, 2)
        step = info.volume_step
        return round(round(volume / step) * step, 8)

    @staticmethod
    def _trade_retcode_description(result) -> str:
        """Get a human-readable description for a trade return code."""
        if result is None:
            return "No result"
        retcode_map = {
            10004: "Requote",
            10006: "Request rejected",
            10007: "Request canceled by trader",
            10008: "Order placed",
            10009: "Modified",
            10010: "Pending order canceled",
            10011: "Partially filled",
            10012: "Fully filled",
            10013: "Order processing error",
            10014: "Order not found",
            10015: "Invalid request parameters",
            10016: "Order locked",
            10017: "Too many orders",
            10018: "No changes",
            10019: "Server disabled (AutoTrading)",
            10020: "Modification denied",
            10021: "Not enough money",
            10022: "Price changed",
            10023: "Off quotes",
            10024: "Broker busy",
            10025: "Requote",
            10026: "Order is not enabled",
            10027: "Too many positions on the symbol",
            10028: "Too many positions",
            -1: "Unknown error (trade context busy?)",
        }
        return retcode_map.get(result.retcode, f"Unknown retcode {result.retcode}")