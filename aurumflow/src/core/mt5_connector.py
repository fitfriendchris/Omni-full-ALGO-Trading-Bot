#!/usr/bin/env python3
"""
MetaTrader 5 Connector — handles connection lifecycle, terminal state, and account info.

Production-ready with retry logic, heartbeat monitoring, and graceful disconnection.
"""

import time
import logging
from typing import Optional, Dict, Any, Tuple

import MetaTrader5 as mt5

logger = logging.getLogger("aurumflow.mt5")


class MT5Connector:
    """Manages the MT5 terminal connection lifecycle."""

    def __init__(self, config: dict):
        mt5_cfg = config.get("mt5", {})
        self._server = mt5_cfg.get("server", "")
        self._login = mt5_cfg.get("login", 0)
        self._password = mt5_cfg.get("password", "")
        self._timeout = mt5_cfg.get("timeout_seconds", 30)
        self._max_retries = mt5_cfg.get("max_retry_attempts", 3)
        self._connected = False
        self._account_info: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Establish connection to the MT5 terminal.
        Returns True if successful, False otherwise.
        """
        if self._connected:
            logger.info("Already connected to MT5")
            return True

        if not mt5.initialize():
            error = mt5.last_error()
            logger.error(f"MT5 initialize failed: {error}")
            return False

        logger.info("MT5 terminal initialized")

        # Attempt login if credentials are provided
        if self._login and self._password:
            authorized = self._login_with_retry()
            if not authorized:
                mt5.shutdown()
                return False
        else:
            # Check if terminal is already logged in
            account_info = mt5.account_info()
            if account_info is None:
                logger.warning(
                    "No credentials provided and terminal not logged in. "
                    "Set AURUM_MT5_LOGIN and AURUM_MT5_PASSWORD env vars."
                )
                mt5.shutdown()
                return False

        self._connected = True
        self._refresh_account_info()
        logger.info(f"Connected to MT5 — Account: {self._account_info.get('login')}, "
                    f"Balance: {self._account_info.get('balance')}, "
                    f"Server: {self._account_info.get('server')}")
        return True

    def disconnect(self) -> None:
        """Gracefully shut down the MT5 connection."""
        if self._connected:
            mt5.shutdown()
            self._connected = False
            self._account_info = None
            logger.info("Disconnected from MT5")

    def is_connected(self) -> bool:
        """Check if we're connected and terminal is alive."""
        if not self._connected:
            return False
        if not mt5.terminal_info():
            logger.warning("MT5 terminal disconnected unexpectedly")
            self._connected = False
            return False
        return True

    def ensure_connected(self) -> bool:
        """Reconnect if not connected. Returns True if connected."""
        if self.is_connected():
            return True
        logger.info("Attempting reconnection...")
        self.disconnect()
        return self.connect()

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Refresh and return account information."""
        if not self.ensure_connected():
            return None
        self._refresh_account_info()
        return self._account_info

    def get_symbol_info(self, symbol: str = "XAUUSD") -> Optional[Dict[str, Any]]:
        """Get symbol specification and trading details."""
        if not self.ensure_connected():
            return None
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error(f"Symbol {symbol} not found")
            return None

        # Select symbol in MarketWatch if not active
        if not info.visible:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)

        return {
            "name": info.name,
            "digits": info.digits,
            "point": info.point,
            "spread": info.spread,
            "trade_mode": info.trade_mode,
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "trade_tick_size": info.trade_tick_size,
            "trade_contract_size": info.trade_contract_size,
            "ask": info.ask,
            "bid": info.bid,
            "last": info.last,
            "trade_stops_level": info.trade_stops_level,
            "free_margin": info.free_margin if hasattr(info, "free_margin") else 0,
        }

    def get_last_tick(self, symbol: str = "XAUUSD") -> Optional[Dict[str, float]]:
        """Get the latest tick data for a symbol."""
        if not self.ensure_connected():
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.warning(f"No tick data for {symbol}")
            return None
        return {
            "time": tick.time,
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "volume": tick.volume,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _login_with_retry(self) -> bool:
        """Attempt login with retry logic."""
        for attempt in range(1, self._max_retries + 1):
            logger.info(f"Login attempt {attempt}/{self._max_retries}...")
            authorized = mt5.login(
                login=self._login,
                password=self._password,
                server=self._server,
            )
            if authorized:
                logger.info("Login successful")
                return True

            error = mt5.last_error()
            logger.warning(f"Login attempt {attempt} failed: {error}")

            if attempt < self._max_retries:
                wait = min(2 ** attempt, 10)
                logger.info(f"Retrying in {wait}s...")
                time.sleep(wait)

        logger.error("All login attempts exhausted")
        return False

    def _refresh_account_info(self) -> None:
        """Pull latest account info into cache."""
        info = mt5.account_info()
        if info is None:
            self._account_info = None
            return
        self._account_info = {
            "login": info.login,
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "margin_free": info.margin_free,
            "margin_level": info.margin_level,
            "currency": info.currency,
            "profit": info.profit,
            "leverage": info.leverage,
            "name": info.name,
        }

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
