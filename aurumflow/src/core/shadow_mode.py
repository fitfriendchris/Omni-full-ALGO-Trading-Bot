#!/usr/bin/env python3
"""
Shadow Mode — tracks virtual trades alongside live signals for backtest alignment.

When `trading.enabled: false` in config, the bot logs every strategy signal
and maintains a virtual portfolio so the team can compare live behavior
against backtested results before risking real capital.
"""

import os
import json
import time
import logging
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from collections import defaultdict

logger = logging.getLogger("aurumflow.shadow")

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "shadow")


@dataclass
class ShadowSignal:
    """A signal that was emitted by the strategy in shadow mode."""
    timestamp: str
    action: str          # "buy", "sell", "close", "hold"
    price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    confidence: float = 0.0
    atr: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    rsi: float = 50.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ShadowTrade:
    """A virtual trade opened in shadow mode."""
    entry_time: str
    entry_price: float
    volume: float
    stop_loss: float
    take_profit: Optional[float] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    profit: Optional[float] = None
    reason: str = ""
    pyramid_level: int = 0


class ShadowMode:
    """
    Tracks virtual trades and logs all signals for offline analysis.

    Designed to run side-by-side with the live bot when `trading.enabled = false`.
    """

    def __init__(self, config: dict):
        self._config = config
        trading_cfg = config.get("trading", {})
        risk_cfg = config.get("risk", {})

        self._initial_balance = trading_cfg.get("shadow_initial_balance", 10000.0)
        self._virtual_balance = self._initial_balance
        self._risk_per_trade = risk_cfg.get("risk_per_trade", 0.01)
        self._trailing_atr_mult = risk_cfg.get("trailing_atr_mult", 1.8)
        self._pyramiding_max = risk_cfg.get("pyramiding_max", 4)
        self._pyramiding_step_atr = risk_cfg.get("pyramiding_step_atr", 0.7)

        # Virtual portfolio state
        self._virtual_positions: List[ShadowTrade] = []
        self._closed_trades: List[ShadowTrade] = []
        self._signals: List[ShadowSignal] = []
        self._equity_curve: List[float] = []
        self._peak_balance: float = self._initial_balance
        self._daily_signals: Dict[str, List[ShadowSignal]] = defaultdict(list)

        # Create log directory
        os.makedirs(LOG_DIR, exist_ok=True)

        # Session tracking
        self._session_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self._signal_count = 0

        logger.info(
            f"Shadow mode initialized — virtual balance: ${self._virtual_balance:.2f}, "
            f"log dir: {LOG_DIR}"
        )

    # ------------------------------------------------------------------
    # Signal logging
    # ------------------------------------------------------------------

    def log_signal(self, signal: "Signal", state: "MarketState") -> None:
        """
        Record a strategy signal. Also executes it on the virtual portfolio
        if it's a buy or close action.
        """
        mid_price = (state.bid + state.ask) / 2.0

        shadow_signal = ShadowSignal(
            timestamp=datetime.utcnow().isoformat(),
            action=signal.action,
            price=mid_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            reason=signal.reason,
            confidence=signal.confidence,
            atr=state.atr,
            ema_fast=state.ema_fast,
            ema_slow=state.ema_slow,
            rsi=state.rsi,
        )

        self._signals.append(shadow_signal)
        self._daily_signals[date.today().isoformat()].append(shadow_signal)
        self._signal_count += 1

        # Execute virtually
        if signal.action == "buy":
            self._virtual_open_buy(mid_price, state.atr, signal)
        elif signal.action == "close":
            self._virtual_close_all(mid_price, signal.reason)

        # Track equity
        equity = self._virtual_balance + self._virtual_unrealized_pnl(mid_price)
        self._equity_curve.append(equity)
        self._peak_balance = max(self._peak_balance, equity)

    # ------------------------------------------------------------------
    # Virtual trading
    # ------------------------------------------------------------------

    def _virtual_open_buy(self, price: float, atr: float, signal: "Signal") -> None:
        """Open a virtual buy position (or pyramid)."""
        if not self._virtual_positions:
            # Initial entry
            sl_price = signal.stop_loss or (price - 1.5 * atr)
            risk_per_unit = price - sl_price
            if risk_per_unit <= 0:
                risk_per_unit = 1.0

            risk_amount = self._virtual_balance * self._risk_per_trade
            units = risk_amount / risk_per_unit
            volume = units / 100.0  # Convert to lots

            if volume < 0.01:
                logger.debug(f"[SHADOW] Trade skipped: volume {volume:.4f} < 0.01 min lot")
                return

            trade = ShadowTrade(
                entry_time=datetime.utcnow().isoformat(),
                entry_price=price,
                volume=volume,
                stop_loss=sl_price,
                take_profit=signal.take_profit,
                reason=signal.reason,
                pyramid_level=0,
            )
            self._virtual_positions.append(trade)
            logger.info(f"[SHADOW] Virtual BUY: vol={volume:.4f}, price={price:.2f}, SL={sl_price:.2f}")

        elif len(self._virtual_positions) < self._pyramiding_max:
            # Pyramid entry
            last_pos = self._virtual_positions[-1]
            price_diff = price - last_pos.entry_price
            if price_diff >= self._pyramiding_step_atr * atr:
                sl_price = price - (self._trailing_atr_mult * atr)
                risk_amount = self._virtual_balance * self._risk_per_trade
                risk_per_unit = price - sl_price
                if risk_per_unit <= 0:
                    risk_per_unit = 1.0

                units = risk_amount / risk_per_unit
                volume = units / 100.0

                if volume < 0.01:
                    return

                trade = ShadowTrade(
                    entry_time=datetime.utcnow().isoformat(),
                    entry_price=price,
                    volume=volume,
                    stop_loss=sl_price,
                    reason=f"Pyramid level {len(self._virtual_positions) + 1}",
                    pyramid_level=len(self._virtual_positions),
                )
                self._virtual_positions.append(trade)

                # Sync SL for all positions
                for pos in self._virtual_positions:
                    pos.stop_loss = max(pos.stop_loss, sl_price)

                logger.info(f"[SHADOW] Virtual PYRAMID: level={trade.pyramid_level + 1}, "
                           f"vol={volume:.4f}, price={price:.2f}")

    def _virtual_close_all(self, price: float, reason: str) -> None:
        """Close all virtual positions."""
        if not self._virtual_positions:
            return

        for pos in self._virtual_positions:
            pos.exit_time = datetime.utcnow().isoformat()
            pos.exit_price = price
            pos.profit = (price - pos.entry_price) * pos.volume * 100.0  # Convert lots to units
            pos.reason = reason
            self._virtual_balance += pos.profit
            self._closed_trades.append(pos)

        logger.info(
            f"[SHADOW] Virtual CLOSE ALL: {len(self._virtual_positions)} positions, "
            f"PnL={sum(p.profit or 0 for p in self._virtual_positions):+.2f}"
        )
        self._virtual_positions = []

    def _virtual_unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized PnL from open virtual positions."""
        total = 0.0
        for pos in self._virtual_positions:
            if pos.stop_loss and current_price <= pos.stop_loss:
                # Virtual SL hit
                total += (pos.stop_loss - pos.entry_price) * pos.volume * 100.0
            else:
                total += (current_price - pos.entry_price) * pos.volume * 100.0
        return total

    def _update_trailing_stops(self, current_price: float, atr: float) -> None:
        """Update trailing stops on virtual positions (used in cycle)."""
        if not self._virtual_positions or atr <= 0:
            return

        trail_sl = current_price - (self._trailing_atr_mult * atr)
        for pos in self._virtual_positions:
            if trail_sl > pos.stop_loss:
                pos.stop_loss = trail_sl

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current shadow mode status."""
        mid_price = 0.0
        if self._virtual_positions:
            mid_price = self._virtual_positions[-1].entry_price

        unrealized = self._virtual_unrealized_pnl(mid_price)
        equity = self._virtual_balance + unrealized
        drawdown = (self._peak_balance - equity) / self._peak_balance if self._peak_balance > 0 else 0.0

        wins = sum(1 for t in self._closed_trades if t.profit and t.profit > 0)
        losses = sum(1 for t in self._closed_trades if t.profit and t.profit <= 0)
        total_trades = len(self._closed_trades)

        return {
            "enabled": True,
            "virtual_balance": round(self._virtual_balance, 2),
            "equity": round(equity, 2),
            "peak_balance": round(self._peak_balance, 2),
            "drawdown": round(drawdown, 4),
            "open_positions": len(self._virtual_positions),
            "closed_trades": total_trades,
            "win_rate": round(wins / total_trades, 4) if total_trades > 0 else 0.0,
            "total_signals": self._signal_count,
            "initial_balance": self._initial_balance,
            "total_pnl": round(self._virtual_balance - self._initial_balance, 2),
        }

    def generate_daily_report(self) -> str:
        """
        Generate a daily alignment report comparing shadow mode performance.

        Returns the path to the saved report file.
        """
        today = date.today().isoformat()
        today_signals = self._daily_signals.get(today, [])

        if not today_signals:
            report = {
                "date": today,
                "message": "No signals recorded today.",
            }
        else:
            buy_signals = [s for s in today_signals if s.action == "buy"]
            close_signals = [s for s in today_signals if s.action == "close"]
            hold_signals = [s for s in today_signals if s.action == "hold"]

            today_trades = [
                t for t in self._closed_trades
                if t.exit_time and t.exit_time.startswith(today)
            ]
            wins = sum(1 for t in today_trades if t.profit and t.profit > 0)
            losses = sum(1 for t in today_trades if t.profit and t.profit <= 0)

            report = {
                "date": today,
                "session_id": self._session_id,
                "signals": {
                    "total": len(today_signals),
                    "buy": len(buy_signals),
                    "close": len(close_signals),
                    "hold": len(hold_signals),
                },
                "virtual_portfolio": self.get_status(),
                "trades_today": {
                    "total": len(today_trades),
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(wins / len(today_trades), 4) if today_trades else 0.0,
                    "total_pnl": round(sum(t.profit or 0 for t in today_trades), 2),
                },
            }

        os.makedirs(LOG_DIR, exist_ok=True)
        report_path = os.path.join(LOG_DIR, f"daily_report_{today}.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"[SHADOW] Daily report saved to {report_path}")
        return report_path

    def export_signals(self) -> str:
        """Export all signals to a JSON file for analysis."""
        os.makedirs(LOG_DIR, exist_ok=True)

        # Convert signals to serialisable format
        signals_data = [s.to_dict() for s in self._signals]

        path = os.path.join(LOG_DIR, f"signals_{self._session_id}.json")
        with open(path, "w") as f:
            json.dump({
                "session_id": self._session_id,
                "initial_balance": self._initial_balance,
                "signal_count": len(signals_data),
                "signals": signals_data,
                "closed_trades": [asdict(t) for t in self._closed_trades],
            }, f, indent=2, default=str)

        logger.info(f"[SHADOW] Signals exported to {path}")
        return path

    def reset(self) -> None:
        """Reset virtual portfolio for a fresh session."""
        self._virtual_balance = self._initial_balance
        self._virtual_positions = []
        self._closed_trades = []
        self._equity_curve = []
        self._peak_balance = self._initial_balance
        self._signal_count = 0

        # Start new session
        self._session_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        logger.info("[SHADOW] Virtual portfolio reset for new session")