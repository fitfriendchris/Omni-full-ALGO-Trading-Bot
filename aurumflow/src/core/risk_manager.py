#!/usr/bin/env python3
"""
Risk Manager — monitors portfolio-level risk, drawdown, and exposure limits.

Enforces max drawdown, daily loss limits, position sizing, and pyramiding rules.
Supports cent accounts ($1 = 100 cents) for micro-account compatibility.
"""

import time
import logging
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime, date

logger = logging.getLogger("aurumflow.risk")

# Minimum lot size for XAUUSD on most MT5 brokers
MIN_LOT = 0.01
# Standard XAUUSD contract size (100 oz per lot)
CONTRACT_SIZE = 100


class RiskManager:
    """Monitors and enforces risk constraints across the trading system."""

    def __init__(self, config: dict, position_manager):
        risk_cfg = config.get("risk", {})
        trading_cfg = config.get("trading", {})
        self._risk_per_trade = risk_cfg.get("risk_per_trade", 0.01)
        self._max_daily_loss = risk_cfg.get("max_daily_loss", 0.05)
        self._max_positions = risk_cfg.get("max_positions", 5)
        self._max_drawdown_limit = risk_cfg.get("max_drawdown_limit", 0.15)
        self._pyramiding_max = risk_cfg.get("pyramiding_max", 4)
        self._pyramiding_step_atr = risk_cfg.get("pyramiding_step_atr", 0.7)
        self._trailing_atr_mult = risk_cfg.get("trailing_atr_mult", 1.8)
        self._min_margin_level = risk_cfg.get("min_margin_level", 100.0)
        self._min_position_size = risk_cfg.get("min_position_size", MIN_LOT)
        self._skip_below_min_lot = risk_cfg.get("skip_below_min_lot", True)
        self._position_manager = position_manager

        # Cent account support
        self._cent_account = trading_cfg.get("cent_account", False)
        self._account_currency = trading_cfg.get("account_currency", "USD")

        # Runtime state
        self._peak_balance: Optional[float] = None
        self._daily_start_balance: Optional[float] = None
        self._trade_date: Optional[date] = None
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._max_consecutive_losses: int = 3

        if self._cent_account:
            logger.info(
                f"Cent account mode enabled — balance will be treated as cents. "
                f"Min lot: {self._min_position_size}"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_position_size(self, balance: float, entry_price: float, stop_loss: float) -> float:
        """
        Calculate position size based on risk per trade.

        For cent accounts, balance is treated as cents (100 units = 1 dollar).
        Example: $100 balance with cent_account=true means balance=10000 cents.

        Args:
            balance: Account balance (in account units: dollars or cents)
            entry_price: Entry price level
            stop_loss: Stop loss price level

        Returns:
            Lot volume (rounded to broker step).
            Returns 0.0 if the calculated size is below min_lot and skip_below_min_lot is true.
        """
        if self._cent_account:
            balance = balance / 100.0  # Convert cents to dollars for calculation

        risk_amount = balance * self._risk_per_trade
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit <= 0:
            logger.warning("Risk per unit <= 0, returning min lot")
            return self._round_lot(self._min_position_size)

        units = risk_amount / risk_per_unit
        volume = units / CONTRACT_SIZE  # Convert to lots (100 oz per lot for XAUUSD)

        volume = self._round_lot(volume)

        # Validate minimum position size
        if volume < self._min_position_size:
            actual_risk_pct = self._compute_actual_risk(balance, entry_price, stop_loss, self._min_position_size)
            msg = (
                f"Risk-based lot {volume:.4f} below minimum {self._min_position_size}. "
                f"Actual risk would be {actual_risk_pct:.2%} (target: {self._risk_per_trade:.2%})."
            )
            if self._skip_below_min_lot:
                logger.warning(f"{msg} Skipping trade.")
                return 0.0
            else:
                logger.warning(f"{msg} Using minimum lot {self._min_position_size}.")
                return self._min_position_size

        return volume

    def compute_position_size_with_warning(
        self, balance: float, entry_price: float, stop_loss: float
    ) -> Tuple[float, Optional[str]]:
        """
        Like compute_position_size but also returns a warning string if the
        actual risk exceeds the target risk_per_trade.

        Returns:
            (volume: float, warning: Optional[str])
        """
        volume = self.compute_position_size(balance, entry_price, stop_loss)
        warning = None

        if volume <= 0:
            return 0.0, "Trade skipped: position size below minimum lot"

        actual_risk = self._compute_actual_risk(balance, entry_price, stop_loss, volume)
        if actual_risk > self._risk_per_trade * 1.1:  # 10% tolerance
            warning = (
                f"Actual risk {actual_risk:.4%} exceeds target {self._risk_per_trade:.2%} "
                f"(volume: {volume:.4f})"
            )

        return volume, warning

    def can_open_trade(self, balance: float, current_positions: int, margin_level: Optional[float] = None) -> Tuple[bool, str]:
        """
        Check if a new trade is allowed given current risk constraints.

        Args:
            balance: Current account balance
            current_positions: Number of open positions
            margin_level: Current margin level % (from MT5 account info). None = skip check.

        Returns:
            (allowed: bool, reason: str)
        """
        # Update daily tracking
        self._update_daily_state(balance)

        # Check margin level
        if margin_level is not None and margin_level < self._min_margin_level:
            return False, (
                f"Margin level {margin_level:.1f}% below minimum "
                f"{self._min_margin_level:.1f}% — account over-leveraged"
            )

        # Check max positions
        if current_positions >= self._max_positions:
            return False, f"Max positions ({self._max_positions}) reached"

        # Check daily loss
        if self._daily_start_balance is not None:
            daily_loss_ratio = (self._daily_start_balance - balance) / self._daily_start_balance
            if daily_loss_ratio >= self._max_daily_loss:
                return False, f"Daily loss limit ({self._max_daily_loss:.0%}) hit"

        # Check drawdown
        if self._peak_balance is not None:
            drawdown = (self._peak_balance - balance) / self._peak_balance
            if drawdown >= self._max_drawdown_limit:
                return False, f"Max drawdown ({self._max_drawdown_limit:.0%}) hit"

        # Check consecutive losses
        if self._consecutive_losses >= self._max_consecutive_losses:
            return False, f"{self._consecutive_losses} consecutive losses — cooling off"

        return True, "OK"

    def can_pyramid(self, current_pyramid_level: int, current_price: float, last_entry_price: float, atr: float) -> Tuple[bool, str]:
        """
        Check if pyramiding is allowed.
        """
        if current_pyramid_level >= self._pyramiding_max:
            return False, f"Pyramiding max ({self._pyramiding_max}) reached"

        price_diff = current_price - last_entry_price
        if price_diff < self._pyramiding_step_atr * atr:
            return False, f"Price diff {price_diff:.2f} < pyramid step {self._pyramiding_step_atr * atr:.2f}"

        return True, "OK"

    def record_trade_result(self, profit: float, balance: float):
        """Update risk state after a closed trade."""
        self._update_daily_state(balance)

        if profit < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        self._daily_pnl += profit

        logger.debug(
            f"Trade result: {profit:+.2f} | Daily PnL: {self._daily_pnl:+.2f} | "
            f"Consecutive losses: {self._consecutive_losses}"
        )

    def update_peak_balance(self, balance: float):
        """Update the peak balance tracker for drawdown calculation."""
        if self._peak_balance is None or balance > self._peak_balance:
            self._peak_balance = balance

    def get_drawdown(self, balance: float) -> float:
        """Calculate current drawdown from peak."""
        if self._peak_balance is None or self._peak_balance == 0:
            return 0.0
        return (self._peak_balance - balance) / self._peak_balance

    def get_status(self, balance: float) -> Dict[str, Any]:
        """Get a snapshot of current risk state."""
        self._update_daily_state(balance)
        drawdown = self.get_drawdown(balance)
        daily_loss_ratio = 0.0
        if self._daily_start_balance and self._daily_start_balance > 0:
            daily_loss_ratio = (self._daily_start_balance - balance) / self._daily_start_balance

        return {
            "balance": balance,
            "peak_balance": self._peak_balance,
            "drawdown": drawdown,
            "daily_loss_ratio": daily_loss_ratio,
            "daily_pnl": self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "can_trade": drawdown < self._max_drawdown_limit and daily_loss_ratio < self._max_daily_loss,
            "max_drawdown_limit": self._max_drawdown_limit,
            "max_daily_loss": self._max_daily_loss,
            "max_positions": self._max_positions,
            "pyramiding_max": self._pyramiding_max,
            "cent_account": self._cent_account,
            "min_position_size": self._min_position_size,
            "min_margin_level": self._min_margin_level,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_actual_risk(self, balance: float, entry_price: float, stop_loss: float, volume: float) -> float:
        """Calculate what % of balance is actually at risk with a given lot size."""
        if self._cent_account:
            balance = balance / 100.0

        if balance <= 0:
            return 0.0

        risk_amount = volume * CONTRACT_SIZE * abs(entry_price - stop_loss)
        return risk_amount / balance

    def _update_daily_state(self, balance: float):
        """Reset daily counters on new trading day."""
        today = date.today()
        if self._trade_date != today:
            logger.info(f"New trading day: resetting daily counters")
            self._trade_date = today
            self._daily_start_balance = balance
            self._daily_pnl = 0.0
            self._consecutive_losses = 0

    def _round_lot(self, volume: float) -> float:
        """Round lot to standard step for XAUUSD."""
        step = 0.01
        return round(round(volume / step) * step, 8)