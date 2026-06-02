#!/usr/bin/env python3
"""
Bar/Candle Builder — aggregates raw tick data from MT5 into OHLC candles.

Maintains a scrolling buffer of recent candles (1-min and 5-min timeframes)
for computing indicators (EMA, RSI, ATR) used by the StrategyAdapter.
"""

import time
import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("aurumflow.bar")

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    """An OHLC candle with volume."""
    open: float
    high: float
    low: float
    close: float
    volume: float
    time: int          # Unix timestamp (start of candle)
    timeframe: str     # "1m" or "5m"

    def to_dict(self) -> dict:
        return {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "time": self.time,
            "timeframe": self.timeframe,
        }


@dataclass
class TickSnapshot:
    """Minimal tick data required for bar building."""
    bid: float
    ask: float
    volume: float
    time: int           # Unix timestamp

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


# ---------------------------------------------------------------------------
# BarBuilder
# ---------------------------------------------------------------------------

class BarBuilder:
    """
    Aggregates ticks into OHLC candles.

    Usage:
        bb = BarBuilder(timeframe_sec=60, buffer_size=100)
        bb.update(bid, ask, volume, timestamp)
        candle = bb.get_current_candle()   # in-progress candle
        candles = bb.get_candles()         # completed candles
        df = bb.to_dataframe()             # for indicator calculation
    """

    def __init__(self, timeframe_sec: int = 60, buffer_size: int = 100, symbol: str = "XAUUSD"):
        """
        Args:
            timeframe_sec: Candle duration in seconds (60 = 1m, 300 = 5m).
            buffer_size: Max number of completed candles to retain.
            symbol: Trading symbol (for logging).
        """
        self._timeframe_sec = timeframe_sec
        self._buffer_size = buffer_size
        self._symbol = symbol

        # Current in-progress candle
        self._current: Optional[dict] = None

        # Completed candles (oldest first)
        self._completed: List[Candle] = []

        # Tick counter for the current candle
        self._tick_count = 0

        # Candle boundary alignment
        self._aligned = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, bid: float, ask: float, volume: float, timestamp: Optional[int] = None) -> Optional[Candle]:
        """
        Feed a new tick into the builder.

        Args:
            bid: Current bid price.
            ask: Current ask price.
            volume: Tick volume (from MT5).
            timestamp: Unix timestamp. Defaults to time.time().

        Returns:
            The newly completed Candle if a boundary was crossed, else None.
        """
        now = timestamp if timestamp is not None else int(time.time())
        mid = (bid + ask) / 2.0

        # Align to period boundary
        period_start = (now // self._timeframe_sec) * self._timeframe_sec

        if not self._aligned:
            # Initialise the first candle
            self._current = {
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                "volume": volume,
                "time": period_start,
                "tick_count": 1,
            }
            self._aligned = True
            return None

        # Check if we've moved to a new period
        if period_start > self._current["time"]:
            # Finalise the completed candle
            completed = Candle(
                open=self._current["open"],
                high=self._current["high"],
                low=self._current["low"],
                close=self._current["close"],
                volume=self._current["volume"],
                time=self._current["time"],
                timeframe=self._timeframe_label(),
            )
            self._completed.append(completed)

            # Trim buffer if needed
            if len(self._completed) > self._buffer_size:
                self._completed = self._completed[-self._buffer_size:]

            self._tick_count = 0

            # Start new candle
            self._current = {
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                "volume": volume,
                "time": period_start,
                "tick_count": 1,
            }

            return completed

        # Update current candle
        self._current["high"] = max(self._current["high"], mid)
        self._current["low"] = min(self._current["low"], mid)
        self._current["close"] = mid
        self._current["volume"] += volume
        self._current["tick_count"] += 1

        return None

    def update_from_tick_dict(self, tick: dict) -> Optional[Candle]:
        """
        Feed a tick dict (as returned by MT5Connector.get_last_tick()).

        Accepts dicts with keys: 'bid', 'ask', 'volume', 'time'.
        """
        return self.update(
            bid=tick.get("bid", 0),
            ask=tick.get("ask", 0),
            volume=tick.get("volume", 0),
            timestamp=tick.get("time"),
        )

    def get_current_candle(self) -> Optional[dict]:
        """Get the in-progress candle (for real-time price monitoring)."""
        return self._current

    def get_completed_candles(self) -> List[Candle]:
        """Get all completed candles (oldest first)."""
        return list(self._completed)

    def get_last_n_candles(self, n: int) -> List[Candle]:
        """Get the last N completed candles (newest last)."""
        if n >= len(self._completed):
            return list(self._completed)
        return list(self._completed[-n:])

    def to_dataframe(self, n: Optional[int] = None) -> pd.DataFrame:
        """
        Convert completed candles to a pandas DataFrame with OHLCV columns.

        Args:
            n: Number of most-recent candles to include. Default = all.

        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume, Time
        """
        candles = self._completed if n is None else self._completed[-n:]
        if not candles:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Time"])

        data = {
            "Open": [c.open for c in candles],
            "High": [c.high for c in candles],
            "Low": [c.low for c in candles],
            "Close": [c.close for c in candles],
            "Volume": [c.volume for c in candles],
            "Time": [c.time for c in candles],
        }
        df = pd.DataFrame(data)
        df.index = pd.to_datetime(df["Time"], unit="s")
        return df

    def compute_indicators(self, n: Optional[int] = None) -> dict:
        """
        Compute trading indicators (EMA_fast, EMA_slow, RSI, ATR) from
        the completed candle buffer.

        Args:
            n: Number of candles to use for computation. Default = all.

        Returns:
            dict with keys: ema_fast, ema_slow, rsi, atr, close, volume_sma
        """
        df = self.to_dataframe(n)
        if df.empty or len(df) < 20:
            return {
                "ema_fast": 0.0,
                "ema_slow": 0.0,
                "rsi": 50.0,
                "atr": 0.0,
                "close": 0.0,
                "volume_sma": 0.0,
            }

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        # EMAs
        ema_fast = close.ewm(span=20, adjust=False).mean().iloc[-1]
        ema_slow = close.ewm(span=50, adjust=False).mean().iloc[-1]

        # RSI (14-period)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi_series = 100 - (100 / (1 + rs))
        rsi = rsi_series.iloc[-1] if not pd.isna(rsi_series.iloc[-1]) else 50.0

        # ATR (14-period)
        high_low = high - low
        high_close = (high - close.shift()).abs()
        low_close = (low - close.shift()).abs()
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = ranges.max(axis=1)
        atr = true_range.rolling(window=14).mean().iloc[-1]
        if pd.isna(atr):
            atr = 0.0

        # Volume SMA
        volume_sma = volume.rolling(window=20).mean().iloc[-1]
        if pd.isna(volume_sma):
            volume_sma = 0.0

        return {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi,
            "atr": atr,
            "close": close.iloc[-1],
            "volume_sma": volume_sma,
        }

    def get_candle_count(self) -> int:
        """Number of completed candles in the buffer."""
        return len(self._completed)

    def reset(self) -> None:
        """Clear all candles and reset state."""
        self._current = None
        self._completed = []
        self._tick_count = 0
        self._aligned = False

    def _timeframe_label(self) -> str:
        """Human-readable timeframe label."""
        if self._timeframe_sec == 60:
            return "1m"
        elif self._timeframe_sec == 300:
            return "5m"
        else:
            return f"{self._timeframe_sec}s"


class MultiTimeframeBarBuilder:
    """
    Manages multiple BarBuilders (1m and 5m) with a unified interface.

    Strategy:
        - 1m candles for fast indicator updates
        - 5m candles for trend detection
    """

    def __init__(self, buffer_size: int = 100, symbol: str = "XAUUSD"):
        self._builders: Dict[str, BarBuilder] = {
            "1m": BarBuilder(timeframe_sec=60, buffer_size=buffer_size, symbol=symbol),
            "5m": BarBuilder(timeframe_sec=300, buffer_size=buffer_size, symbol=symbol),
        }
        self._symbol = symbol
        self._last_update_time: Optional[int] = None
        self._tick_history: List[dict] = []  # Keep last 100 ticks for debugging

    def update(self, tick: dict) -> dict:
        """
        Feed a tick dict to all timeframe builders.

        Returns a dict with keys '1m' and '5m' mapping to any newly
        completed candles (or None if no boundary crossed).
        """
        now = tick.get("time", int(time.time()))
        self._last_update_time = now

        # Keep a rolling tick history (last 100 ticks)
        self._tick_history.append({
            "bid": tick.get("bid"),
            "ask": tick.get("ask"),
            "time": now,
        })
        if len(self._tick_history) > 100:
            self._tick_history = self._tick_history[-100:]

        results = {}
        for tf, builder in self._builders.items():
            completed = builder.update_from_tick_dict(tick)
            results[tf] = completed

        return results

    def get_builder(self, timeframe: str = "1m") -> BarBuilder:
        """Get a specific timeframe builder."""
        return self._builders[timeframe]

    def get_market_state(self, bid: float, ask: float, config: dict) -> "MarketState":
        """
        Compute a MarketState from the 1m candle indicators.

        This is the primary method used by the bot loop.
        """
        from src.strategies.adapter import MarketState

        builder = self._builders["1m"]
        indicators = builder.compute_indicators()

        spread = (ask - bid) if ask and bid else 0.0
        atr = indicators["atr"] if indicators["atr"] > 0 else (spread * 10 if spread > 0 else 10.0)

        return MarketState(
            bid=bid,
            ask=ask,
            spread=spread,
            atr=atr,
            ema_fast=indicators["ema_fast"],
            ema_slow=indicators["ema_slow"],
            rsi=indicators["rsi"],
            volume=indicators.get("volume", 0),
            volume_sma=indicators["volume_sma"],
        )

    def reset(self) -> None:
        """Reset all builders."""
        for builder in self._builders.values():
            builder.reset()
        self._tick_history = []
        self._last_update_time = None