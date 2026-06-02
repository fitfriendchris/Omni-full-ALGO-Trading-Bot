#!/usr/bin/env python3
"""
ZeroMQ-based MT5 Connector — cross-platform replacement for the Windows-only MetaTrader5 package.

Connects to the AurumFlow MQL5 EA via ZeroMQ sockets to receive ticks,
account info, and positions, and to send trading commands.

Works identically on Windows, macOS, and Linux.
"""

import json
import time
import logging
import threading
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from datetime import datetime
from queue import Queue, Empty

logger = logging.getLogger("aurumflow.zmq")

# Try to import zmq; if unavailable, provide a mock for testing
try:
    import zmq
    HAS_ZMQ = True
except ImportError:
    HAS_ZMQ = False
    logger.warning("pyzmq not installed — ZMQ connector will use mock mode for testing")


# ---------------------------------------------------------------------------
# Default ZMQ addresses
# ---------------------------------------------------------------------------
DEFAULT_PULL_ADDR = "tcp://localhost:5555"   # Receive from EA (PUSH -> PULL)
DEFAULT_PUSH_ADDR = "tcp://localhost:5556"   # Send to EA (PULL -> PUSH)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ZMQTick:
    """Tick data received from EA."""
    symbol: str
    bid: float
    ask: float
    last: float
    volume: int
    time: int
    raw: dict = field(default_factory=dict)


@dataclass
class ZMQAccountInfo:
    """Account info received from EA."""
    login: int
    server: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    currency: str
    leverage: int
    raw: dict = field(default_factory=dict)


@dataclass
class ZMQPosition:
    """Position data received from EA."""
    ticket: int
    symbol: str
    type: str          # "buy" or "sell"
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    swap: float
    commission: float
    time: int
    magic: int
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "type": self.type,
            "volume": self.volume,
            "price_open": self.price_open,
            "sl": self.sl,
            "tp": self.tp,
            "profit": self.profit,
            "swap": self.swap,
            "commission": self.commission,
            "time": self.time,
            "magic": self.magic,
        }


# ---------------------------------------------------------------------------
# ZMQ Connector
# ---------------------------------------------------------------------------

class MT5ZMQConnector:
    """
    Connects to the AurumFlow MQL5 EA via ZeroMQ.

    Compatible constructor interface with the legacy MT5Connector (accepts config dict).

    Two sockets:
      - PULL: receives ticks, account info, positions from EA's PUSH socket
      - PUSH: sends trading commands to EA's PULL socket

    Runs a background receiver thread that queues incoming messages.
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: Full AurumFlow config dict. ZMQ settings read from 'zmq' section.
        """
        zmq_cfg = config.get("zmq", {}) if config else {}
        self._pull_addr = zmq_cfg.get("pull_addr", DEFAULT_PULL_ADDR)
        self._push_addr = zmq_cfg.get("push_addr", DEFAULT_PUSH_ADDR)
        self._timeout_ms = zmq_cfg.get("timeout_ms", 1000)
        self._reconnect_delay = zmq_cfg.get("reconnect_delay", 2.0)

        # State
        self._context: Optional[Any] = None
        self._pull_socket: Optional[Any] = None
        self._push_socket: Optional[Any] = None
        self._connected = False
        self._running = False
        self._receiver_thread: Optional[threading.Thread] = None

        # Message queue (thread-safe)
        self._inbox: Queue = Queue()

        # Callbacks
        self._on_tick: Optional[Callable] = None
        self._on_account: Optional[Callable] = None
        self._on_positions: Optional[Callable] = None

        # Last known state cache
        self._last_tick: Optional[ZMQTick] = None
        self._last_account: Optional[ZMQAccountInfo] = None
        self._last_positions: List[ZMQPosition] = []

        # Pending command responses
        self._last_command_result: Optional[dict] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Initialize ZMQ context and connect sockets.
        Returns True if successful.
        """
        if self._connected:
            return True

        if not HAS_ZMQ:
            logger.warning("pyzmq not available — using mock mode")
            self._connected = True
            return True

        try:
            if self._context is None:
                self._context = zmq.Context()

            # PULL socket: receive data from EA
            self._pull_socket = self._context.socket(zmq.PULL)
            self._pull_socket.connect(self._pull_addr)
            self._pull_socket.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
            logger.info(f"PULL socket connected to {self._pull_addr}")

            # PUSH socket: send commands to EA
            self._push_socket = self._context.socket(zmq.PUSH)
            self._push_socket.connect(self._push_addr)
            logger.info(f"PUSH socket connected to {self._push_addr}")

            self._connected = True

            # Start background receiver
            self._running = True
            self._receiver_thread = threading.Thread(
                target=_receiver_loop,
                args=(self,),
                daemon=True,
                name="zmq-receiver",
            )
            self._receiver_thread.start()

            logger.info("ZMQ connector initialized successfully")
            return True

        except Exception as e:
            logger.error(f"ZMQ connect failed: {e}")
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Shutdown ZMQ sockets and context."""
        self._running = False
        self._connected = False

        if self._pull_socket:
            try:
                self._pull_socket.close()
            except Exception:
                pass
            self._pull_socket = None

        if self._push_socket:
            try:
                self._push_socket.close()
            except Exception:
                pass
            self._push_socket = None

        if self._receiver_thread:
            self._receiver_thread.join(timeout=2)
            self._receiver_thread = None

        logger.info("ZMQ connector disconnected")

    def is_connected(self) -> bool:
        """Check if the connector is connected."""
        return self._connected

    def ensure_connected(self) -> bool:
        """Reconnect if not connected. Returns True if connected."""
        if self._connected:
            return True
        logger.info("Attempting ZMQ reconnection...")
        self.disconnect()
        return self.connect()

    # ------------------------------------------------------------------
    # Callback setters
    # ------------------------------------------------------------------

    def on_tick(self, callback: Callable):
        """Register a callback for incoming ticks."""
        self._on_tick = callback

    def on_account(self, callback: Callable):
        """Register a callback for account updates."""
        self._on_account = callback

    def on_positions(self, callback: Callable):
        """Register a callback for position updates."""
        self._on_positions = callback

    # ------------------------------------------------------------------
    # API — receive data
    # ------------------------------------------------------------------

    def get_last_tick(self, symbol: str = "XAUUSD") -> Optional[dict]:
        """Get the latest tick data. Compatible with old MT5Connector API."""
        # Process any pending messages first
        self._drain_inbox()

        if self._last_tick is None:
            return None

        return {
            "time": self._last_tick.time,
            "bid": self._last_tick.bid,
            "ask": self._last_tick.ask,
            "last": self._last_tick.last,
            "volume": self._last_tick.volume,
        }

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Get account information. Compatible with old MT5Connector API."""
        self._drain_inbox()

        if self._last_account is None:
            return None

        return {
            "login": self._last_account.login,
            "server": self._last_account.server,
            "balance": self._last_account.balance,
            "equity": self._last_account.equity,
            "margin": self._last_account.margin,
            "margin_free": self._last_account.margin_free,
            "margin_level": self._last_account.margin_level,
            "currency": self._last_account.currency,
            "leverage": self._last_account.leverage,
        }

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open positions. Compatible with old MT5Connector API."""
        self._drain_inbox()

        positions = self._last_positions
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]

        return [p.to_dict() for p in positions]

    def get_position_count(self, symbol: Optional[str] = None) -> int:
        """Count open positions."""
        return len(self.get_open_positions(symbol))

    def get_symbol_info(self, symbol: str = "XAUUSD") -> Optional[Dict[str, Any]]:
        """Get symbol info. For ZMQ, we approximate from the last tick."""
        self._drain_inbox()

        if self._last_tick is None or self._last_tick.symbol != symbol:
            return None

        spread = abs(self._last_tick.ask - self._last_tick.bid)
        return {
            "name": symbol,
            "digits": 2,
            "point": 0.01,
            "spread": spread * 10000 if spread > 0 else 25,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "ask": self._last_tick.ask,
            "bid": self._last_tick.bid,
            "last": self._last_tick.last,
        }

    # ------------------------------------------------------------------
    # API — send commands
    # ------------------------------------------------------------------

    def send_command(self, command: dict) -> bool:
        """
        Send a trading command to the EA.

        Args:
            command: Dict with action key ("BUY", "SELL", "CLOSE", "MODIFY", "CLOSE_ALL")

        Returns:
            True if the command was sent successfully.
        """
        if not self._connected:
            logger.error("Cannot send command: not connected")
            return False

        if not HAS_ZMQ or self._push_socket is None:
            logger.info(f"[MOCK ZMQ] Would send: {command}")
            return True

        try:
            msg = json.dumps(command)
            self._push_socket.send_string(msg)
            logger.debug(f"Sent command: {msg}")
            return True
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _drain_inbox(self) -> None:
        """Process all pending messages from the inbox queue."""
        if not HAS_ZMQ:
            return  # No messages in mock mode

        while True:
            try:
                msg = self._inbox.get_nowait()
                self._process_message(msg)
            except Empty:
                break

    def _process_message(self, msg: dict) -> None:
        """Parse and store a received message."""
        msg_type = msg.get("type", "")

        if msg_type == "tick":
            self._last_tick = ZMQTick(
                symbol=msg.get("symbol", "XAUUSD"),
                bid=msg.get("bid", 0.0),
                ask=msg.get("ask", 0.0),
                last=msg.get("last", 0.0),
                volume=msg.get("volume", 0),
                time=msg.get("time", 0),
                raw=msg,
            )
            if self._on_tick:
                self._on_tick(self._last_tick)

        elif msg_type == "account":
            self._last_account = ZMQAccountInfo(
                login=msg.get("login", 0),
                server=msg.get("server", ""),
                balance=msg.get("balance", 0.0),
                equity=msg.get("equity", 0.0),
                margin=msg.get("margin", 0.0),
                margin_free=msg.get("margin_free", 0.0),
                margin_level=msg.get("margin_level", 0.0),
                currency=msg.get("currency", "USD"),
                leverage=msg.get("leverage", 0),
                raw=msg,
            )
            if self._on_account:
                self._on_account(self._last_account)

        elif msg_type == "positions":
            positions_data = msg.get("data", [])
            self._last_positions = []
            for p in positions_data:
                self._last_positions.append(ZMQPosition(
                    ticket=p.get("ticket", 0),
                    symbol=p.get("symbol", "XAUUSD"),
                    type=p.get("type", "buy"),
                    volume=p.get("volume", 0.0),
                    price_open=p.get("price_open", 0.0),
                    sl=p.get("sl", 0.0),
                    tp=p.get("tp", 0.0),
                    profit=p.get("profit", 0.0),
                    swap=p.get("swap", 0.0),
                    commission=p.get("commission", 0.0),
                    time=p.get("time", 0),
                    magic=p.get("magic", 0),
                    raw=p,
                ))
            if self._on_positions:
                self._on_positions(self._last_positions)

        else:
            logger.debug(f"Unknown message type: {msg_type}")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


# ---------------------------------------------------------------------------
# Background receiver thread
# ---------------------------------------------------------------------------

def _receiver_loop(connector: MT5ZMQConnector) -> None:
    """Background thread: receives messages from EA and puts them in the inbox."""
    while connector._running:
        try:
            if connector._pull_socket is None:
                time.sleep(0.1)
                continue

            msg_str = connector._pull_socket.recv_string()
            if msg_str:
                msg = json.loads(msg_str)
                connector._inbox.put(msg)

        except zmq.Again:
            # Timeout — no message available, that's fine
            pass
        except zmq.ZMQError as e:
            if connector._running:
                logger.warning(f"ZMQ receive error: {e}")
                time.sleep(connector._reconnect_delay)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON from EA: {e}")
        except Exception as e:
            if connector._running:
                logger.error(f"Receiver error: {e}")