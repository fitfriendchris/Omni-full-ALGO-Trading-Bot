#!/usr/bin/env python3
"""
AurumFlow Bot — main application loop.

Orchestrates the MT5 connector, order manager, risk manager,
strategy adapter, and monitoring into a production-ready trading loop.
"""

import os
import sys
import time
import json
import signal
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

# Ensure project root is on the path
_proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from src.utils.logger import setup_logging, get_logger
from src.core.risk_manager import RiskManager
from src.core.bar_builder import MultiTimeframeBarBuilder
from src.core.shadow_mode import ShadowMode
from src.strategies.adapter import StrategyAdapter, MarketState, Signal, load_strategy
from src.utils.config_loader import load_config

# Connector selection: ZMQ (cross-platform) or legacy MT5 DLL (Windows only)
_USE_ZMQ = os.environ.get("AURUM_USE_ZMQ", "true").lower() in ("1", "true", "yes")
if _USE_ZMQ:
    from src.core.mt5_zmq_connector import MT5ZMQConnector as MT5Connector
    from src.core.zmq_order_manager import ZMQOrderManager as OrderManager, OrderSide
else:
    from src.core.mt5_connector import MT5Connector  # noqa: F811
    from src.core.order_manager import OrderManager, OrderSide  # noqa: F811

logger = get_logger("bot")


class AurumFlowBot:
    """
    Main trading bot class.

    Usage:
        bot = AurumFlowBot()
        bot.initialize()
        bot.run()
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config = load_config(config_path)
        self._running = False
        self._paused = False
        self._last_heartbeat = time.time()

        # Core components (initialized in initialize())
        self.mt5: Optional[MT5Connector] = None
        self.orders: Optional[OrderManager] = None
        self.risk: Optional[RiskManager] = None
        self.strategy: Optional[StrategyAdapter] = None
        self.bar_builder: Optional[MultiTimeframeBarBuilder] = None
        self.shadow: Optional[ShadowMode] = None

        # Runtime state
        self._open_positions_cache: list = []
        self._last_check_time: Optional[float] = None
        self._tick_buffer: list = []  # Buffer recent ticks for bar building
        self._trading_enabled: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """Initialize all components. Returns True on success."""
        setup_logging(self.config)
        logger.info("=" * 50)
        logger.info("AurumFlow Bot initializing...")
        logger.info("=" * 50)

        # Connect to MT5
        self.mt5 = MT5Connector(self.config)
        if not self.mt5.connect():
            logger.critical("Failed to connect to MT5. Exiting.")
            return False

        # Initialize managers (ZMQ order manager needs the connector; legacy takes config only)
        self.orders = OrderManager(self.config, self.mt5) if _USE_ZMQ else OrderManager(self.config)
        self.risk = RiskManager(self.config, self.orders)

        # Load strategy
        self.strategy = load_strategy(self.config)
        logger.info(f"Strategy loaded: {self.config.get('strategy', {}).get('type', 'compounder')}")

        # Initialize BarBuilder for proper OHLC candle aggregation
        symbol = self.config.get("trading", {}).get("symbol", "XAUUSD")
        self.bar_builder = MultiTimeframeBarBuilder(buffer_size=100, symbol=symbol)
        logger.info(f"BarBuilder initialized for {symbol} (1m/5m timeframes)")

        # Log account info
        acct = self.mt5.get_account_info()
        if acct:
            logger.info(f"Account: {acct.get('login')} | Balance: {acct.get('balance')} | "
                       f"Equity: {acct.get('equity')} | Leverage: 1:{acct.get('leverage')}")
            self.risk.update_peak_balance(acct.get("balance", 0))

        # Check if trading is enabled (or shadow-only mode)
        self._trading_enabled = self.config.get("trading", {}).get("enabled", False)

        # Initialize Shadow Mode (logs signals and tracks virtual portfolio)
        self.shadow = ShadowMode(self.config)
        if not self._trading_enabled:
            logger.info("Trading DISABLED — running in SHADOW MODE (signals logged, no live orders)")
        else:
            logger.info("Trading ENABLED — live orders will be placed")

        # Bind signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("Initialization complete.")
        return True

    def run(self) -> None:
        """Main trading loop."""
        if not self.mt5 or not self.mt5.is_connected():
            logger.error("Not connected. Call initialize() first.")
            return

        self._running = True
        check_interval = self.config.get("monitoring", {}).get("check_interval_seconds", 60)
        heartbeat_interval = self.config.get("monitoring", {}).get("heartbeat_interval", 300)

        logger.info(f"Bot running. Check interval: {check_interval}s")

        try:
            while self._running:
                if not self._paused:
                    self._trading_cycle()

                # Heartbeat
                now = time.time()
                if now - self._last_heartbeat > heartbeat_interval:
                    self._heartbeat()
                    self._last_heartbeat = now

                time.sleep(check_interval)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down AurumFlow Bot...")
        self._running = False

        # Export shadow mode data
        if self.shadow:
            signals_path = self.shadow.export_signals()
            report_path = self.shadow.generate_daily_report()
            logger.info(f"Shadow data exported: signals={signals_path}, report={report_path}")

        if self.mt5:
            self.mt5.disconnect()

        logger.info("Shutdown complete.")

    # ------------------------------------------------------------------
    # Internal trading cycle
    # ------------------------------------------------------------------

    def _trading_cycle(self) -> None:
        """Execute one cycle: fetch data -> evaluate -> act."""
        try:
            # 1. Ensure connection
            if not self.mt5.ensure_connected():
                logger.error("Cannot connect to MT5. Skipping cycle.")
                return

            # 2. Get account info
            account = self.mt5.get_account_info()
            if account is None:
                return

            balance = account.get("balance", 0)
            margin_level = account.get("margin_level")
            self.risk.update_peak_balance(balance)

            # 3. Get open positions
            positions = self.orders.get_open_positions(self.config.get("trading", {}).get("symbol", "XAUUSD"))
            self._open_positions_cache = positions

            # 4. Get tick data
            tick = self.mt5.get_last_tick()
            if tick is None:
                return

            # 4b. Feed tick into the BarBuilder for candle aggregation
            self.bar_builder.update(tick)
            self._tick_buffer.append(tick)
            if len(self._tick_buffer) > 1000:
                self._tick_buffer = self._tick_buffer[-1000:]

            # 5. Get market state from the BarBuilder (proper OHLC candles)
            bid = tick.get("bid", 0)
            ask = tick.get("ask", 0)
            market_state = self.bar_builder.get_market_state(bid, ask, self.config)
            if market_state is None:
                return

            # 6. Evaluate strategy
            signal = self.strategy.evaluate(market_state)

            # 6b. Log signal in shadow mode (always, even when trading is enabled)
            self.shadow.log_signal(signal, market_state)

            # 7. Act on signal (only if trading is enabled)
            if self._trading_enabled:
                self._execute_signal(signal, market_state, balance, positions, margin_level)
            else:
                logger.debug(f"[SHADOW] Signal: {signal.action} at {bid:.2f} | reason: {signal.reason}")

            # 8. Manage trailing stops on existing positions
            self._manage_trailing_stops(market_state, positions)

        except Exception as e:
            logger.exception(f"Error in trading cycle: {e}")

    def _execute_signal(self, signal: Signal, state: MarketState, balance: float, positions: list, margin_level: Optional[float] = None) -> None:
        """Execute the strategy signal with full risk management."""
        if signal.action == "hold":
            return

        if signal.action == "close":
            # Close all positions if reversal signal
            if positions:
                logger.info(f"Closing all positions: {signal.reason}")
                for pos in positions:
                    self.orders.close_position(pos["ticket"])
                    self.risk.record_trade_result(pos.get("profit", 0), balance)
            return

        if signal.action == "buy":
            mid_price = (state.bid + state.ask) / 2
            
            # Check if this is an initial entry or a pyramid entry
            if not positions:
                # 1. Initial Entry / Re-entry
                allowed, reason = self.risk.can_open_trade(balance, 0, margin_level)
                if not allowed:
                    logger.warning(f"Initial trade blocked: {reason}")
                    return

                sl_price = signal.stop_loss or (mid_price - 1.5 * state.atr)
                volume = self.risk.compute_position_size(balance, mid_price, sl_price)

                if volume <= 0:
                    logger.warning(f"Trade skipped: position size {volume} below min lot")
                    return

                logger.info(f"Opening INITIAL BUY: vol={volume:.2f}, price={mid_price:.2f}, SL={sl_price:.2f}")
                ticket = self.orders.open_market_order(
                    side=OrderSide.BUY,
                    volume=volume,
                    stop_loss=sl_price,
                    take_profit=signal.take_profit,
                )

                if ticket:
                    logger.info(f"INITIAL BUY order placed: ticket={ticket}")
                    # Update peak balance for drawdown tracking
                    self.risk.update_peak_balance(balance)
            else:
                # 2. Pyramiding Logic
                # Check global risk limits first (drawdown, daily loss, max positions, margin level)
                allowed, reason = self.risk.can_open_trade(balance, len(positions), margin_level)
                if not allowed:
                    logger.debug(f"Pyramid trade blocked by global risk: {reason}")
                    return

                # Sort positions by time to find the most recent one
                sorted_pos = sorted(positions, key=lambda x: x['time'], reverse=True)
                last_pos = sorted_pos[0]
                last_entry_price = last_pos['price_open']

                allowed, reason = self.risk.can_pyramid(
                    current_pyramid_level=len(positions),
                    current_price=mid_price,
                    last_entry_price=last_entry_price,
                    atr=state.atr
                )

                if not allowed:
                    # Periodically log why we're not pyramiding if the signal is still buy
                    logger.debug(f"Pyramid entry condition not met: {reason}")
                    return

                # Pyramid SL is usually tighter (trailing)
                sl_price = mid_price - (self.config.get("risk", {}).get("trailing_atr_mult", 1.8) * state.atr)
                volume = self.risk.compute_position_size(balance, mid_price, sl_price)

                if volume <= 0:
                    return

                logger.info(f"Opening PYRAMID BUY: level={len(positions)}, vol={volume:.2f}, price={mid_price:.2f}, SL={sl_price:.2f}")
                ticket = self.orders.open_market_order(
                    side=OrderSide.BUY,
                    volume=volume,
                    stop_loss=sl_price,
                    take_profit=signal.take_profit,
                )

                if ticket:
                    logger.info(f"PYRAMID BUY order placed: ticket={ticket}")
                    # SYNC SL: Tighten SL for all existing positions to the new SL
                    for pos in positions:
                        if pos["sl"] is None or sl_price > pos["sl"]:
                            self.orders.modify_position_sl_tp(pos["ticket"], stop_loss=round(sl_price, 2))

    def _manage_trailing_stops(self, state: MarketState, positions: list) -> None:
        """Update trailing stops on open positions."""
        if not positions:
            return

        mid_price = (state.bid + state.ask) / 2
        trail_sl = self.strategy.compute_trailing_stop(mid_price, state.atr)

        for pos in positions:
            if pos["type"] == "buy":
                # Only move SL up, never down
                if pos["sl"] is None or trail_sl > pos["sl"]:
                    logger.info(f"Trailing SL: {pos['sl']} -> {trail_sl:.2f} (ticket={pos['ticket']})")
                    self.orders.modify_position_sl_tp(pos["ticket"], stop_loss=round(trail_sl, 2))

    def _heartbeat(self) -> None:
        """Log a periodic health status."""
        try:
            account = self.mt5.get_account_info() if self.mt5 else None
            if account:
                balance = account.get("balance", 0)
                equity = account.get("equity", 0)
                drawdown = self.risk.get_drawdown(balance) if self.risk else 0
                pos_count = len(self._open_positions_cache)
                logger.info(
                    f"[HEARTBEAT] Balance={balance:.2f} Equity={equity:.2f} "
                    f"Drawdown={drawdown:.2%} Positions={pos_count} "
                    f"Connected={self.mt5.is_connected() if self.mt5 else False}"
                )
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

    def _signal_handler(self, signum, frame) -> None:
        """Handle OS signals."""
        logger.info(f"Received signal {signum}")
        self._running = False

    # ------------------------------------------------------------------
    # Control methods (for CLI / API)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        self._paused = True
        logger.info("Bot paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Bot resumed")

    def status(self) -> Dict[str, Any]:
        account = self.mt5.get_account_info() if self.mt5 else {}
        balance = account.get("balance", 0)
        return {
            "running": self._running,
            "paused": self._paused,
            "connected": self.mt5.is_connected() if self.mt5 else False,
            "account": account,
            "positions": len(self._open_positions_cache),
            "risk": self.risk.get_status(balance) if self.risk else {},
            "config": {
                "symbol": self.config.get("trading", {}).get("symbol", "XAUUSD"),
                "strategy": self.config.get("strategy", {}).get("type", "compounder"),
                "risk_per_trade": self.config.get("risk", {}).get("risk_per_trade", 0.01),
            },
        }


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="AurumFlow Trading Bot")
    parser.add_argument(
        "-c", "--config",
        default=os.path.join(_proj_root, "config", "default.yaml"),
        help="Path to configuration file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect and validate config but do not trade"
    )
    args = parser.parse_args()

    bot = AurumFlowBot(args.config)
    if bot.initialize():
        if args.dry_run:
            logger.info("DRY RUN mode — connection verified. Exiting.")
            print(json.dumps(bot.status(), indent=2))
            bot.shutdown()
        else:
            bot.run()


if __name__ == "__main__":
    main()
