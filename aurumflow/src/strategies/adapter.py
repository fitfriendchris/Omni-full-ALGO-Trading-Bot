#!/usr/bin/env python3
"""
Strategy Adapter — bridges the Quant Researcher's strategy classes with live execution.

Provides a common interface so any strategy (baseline, compounder, etc.) can be
plugged into the execution engine without modification.
"""

import os
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import numpy as np

logger = logging.getLogger("aurumflow.strategy")


@dataclass
class Signal:
    """A trading signal produced by the strategy engine."""
    action: str                     # "buy", "sell", "hold", "close"
    confidence: float = 0.0         # 0.0 to 1.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class MarketState:
    """Current market state snapshot fed to the strategy."""
    bid: float
    ask: float
    spread: float
    atr: float
    ema_fast: float
    ema_slow: float
    rsi: float
    volume: float
    volume_sma: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def from_dict(cls, data: dict) -> "MarketState":
        return cls(
            bid=data.get("bid", 0.0),
            ask=data.get("ask", 0.0),
            spread=data.get("spread", 0.0),
            atr=data.get("atr", 0.0),
            ema_fast=data.get("ema_fast", 0.0),
            ema_slow=data.get("ema_slow", 0.0),
            rsi=data.get("rsi", 50.0),
            volume=data.get("volume", 0.0),
            volume_sma=data.get("volume_sma", 0.0),
            timestamp=data.get("timestamp", datetime.utcnow()),
        )


class StrategyAdapter:
    """
    Wraps a quant strategy class to provide a clean live-trading interface.

    The strategy object must have a `calculate_indicators(df)` method that computes
    indicators on a DataFrame with columns: Open, High, Low, Close, Volume.
    """

    def __init__(self, strategy_instance, config: dict):
        """
        Args:
            strategy_instance: An instance of a quant strategy class
                               (e.g., AurumCompounderStrategy or BaselineAurumStrategy).
            config: Full AurumFlow config dict.
        """
        self._strategy = strategy_instance
        self._config = config
        self._price_buffer: pd.DataFrame = pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"]
        )
        self._buffer_maxlen = 100  # Number of candles to keep for indicator calc

    def update_price(self, tick: Dict[str, float]) -> None:
        """
        Feed a new tick into the price buffer. The adapter maintains a lightweight
        OHLC history so the strategy can compute indicators on demand.

        In production, you'd feed 1-minute or 5-minute candles instead.
        For now, we approximate by appending raw ticks.
        """
        # For true OHLC, use the bot's bar builder. Here we simulate.
        pass  # Will be implemented by the bar builder

    def evaluate(self, state: MarketState) -> Signal:
        """
        Given current market state, determine what action to take.

        This translates the strategy's backtest logic into a live signal.
        """
        mid_price = (state.bid + state.ask) / 2

        # --- Replicate the compounder strategy logic ---
        ema_bullish = state.ema_fast > state.ema_slow
        rsi_ok = state.rsi > 50
        volume_ok = state.volume > state.volume_sma if state.volume_sma > 0 else True

        if ema_bullish and rsi_ok and volume_ok:
            return Signal(
                action="buy",
                confidence=min(1.0, (state.rsi - 50) / 30),
                stop_loss=mid_price - (1.5 * state.atr),
                reason=f"EMA bullish ({state.ema_fast:.1f}>{state.ema_slow:.1f}), "
                       f"RSI {state.rsi:.1f}>50, vol={state.volume}>vol_sma={state.volume_sma:.0f}"
            )

        # Exit conditions (Reversal or Overbought)
        reversal_signal = state.ema_fast < state.ema_slow or state.rsi > 80
        if reversal_signal:
            return Signal(
                action="close",
                reason=f"Reversal: EMA cross={state.ema_fast < state.ema_slow}, RSI overbought={state.rsi:.1f}"
            )

        return Signal(action="hold", reason="No conditions met")

    def compute_trailing_stop(self, current_price: float, atr: float) -> float:
        """Compute trailing stop level using ATR multiplier."""
        mult = self._config.get("risk", {}).get("trailing_atr_mult", 1.8)
        return current_price - (mult * atr)

    def compute_pyramid_price(self, last_entry_price: float, atr: float) -> float:
        """Compute the price level needed for the next pyramid entry."""
        step = self._config.get("risk", {}).get("pyramiding_step_atr", 0.7)
        return last_entry_price + (step * atr)

    @property
    def strategy_instance(self):
        return self._strategy


def load_strategy(config: dict):
    """
    Factory function — loads the correct strategy based on config.
    Returns a StrategyAdapter wrapping the chosen strategy instance.

    The strategy classes must be importable from src.strategies.
    """
    strategy_type = config.get("strategy", {}).get("type", "compounder")
    risk_config = config.get("risk", {})
    strat_config = config.get("strategy", {})

    import sys
    strategies_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")
    if strategies_dir not in sys.path:
        sys.path.insert(0, strategies_dir)

    if strategy_type == "baseline":
        from baseline_strategy import BaselineAurumStrategy
        instance = BaselineAurumStrategy(
            risk_per_trade=risk_config.get("risk_per_trade", 0.01),
            atr_period=strat_config.get("atr_period", 14),
            ema_fast=strat_config.get("ema_fast", 20),
            ema_slow=strat_config.get("ema_slow", 50),
            rsi_period=strat_config.get("rsi_period", 14),
        )
    else:
        # Default: compounder
        from strategy import AurumCompounderStrategy
        instance = AurumCompounderStrategy(
            risk_per_trade=risk_config.get("risk_per_trade", 0.01),
            atr_period=strat_config.get("atr_period", 14),
            ema_fast=strat_config.get("ema_fast", 20),
            ema_slow=strat_config.get("ema_slow", 50),
            rsi_period=strat_config.get("rsi_period", 14),
            pyramiding_max=risk_config.get("pyramiding_max", 4),
            pyramiding_step=risk_config.get("pyramiding_step_atr", 0.7),
            trailing_atr_mult=risk_config.get("trailing_atr_mult", 1.8),
        )

    logger.info(f"Loaded strategy: {strategy_type}")
    return StrategyAdapter(instance, config)