#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cycle_tracker.py — OMNI ICT Production Bot v28.0
Phase 2B: Accumulation / Manipulation / Distribution state machine
Tracks H4 candles over 5-day window to detect which phase of the ICT 3-5 day cycle
we are in. This gates position sizing and setup aggression.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timezone, timedelta
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class CyclePhase(Enum):
    ACCUMULATION = "accumulation"
    MANIPULATION = "manipulation"
    DISTRIBUTION = "distribution"
    UNKNOWN = "unknown"


@dataclass
class H4Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class CycleState:
    phase: CyclePhase = CyclePhase.UNKNOWN
    day_number: int = 0               # 1-5 within current cycle
    avg_range_5d: float = 0.0         # Average H4 range over last 5 days (5*6 bars)
    current_h4_range: float = 0.0     # Current forming H4 range
    sweep_magnitude: float = 0.0      # Largest sweep relative to avg range
    choch_success_rate: float = 0.0   # % of CHoCH that followed through
    last_sweep_time: Optional[datetime] = None
    confidence: float = 0.0             # 0.0-1.0 how sure we are of phase
    notes: str = ""


class CycleTracker:
    """
    State machine tracking ICT 3-5 day cycle.
    
    ACCUMULATION:  H4 consolidating, range < 1.5x ATR, choppy, low follow-through.
                   → Skip most setups, reduce size 50% if any taken.
    
    MANIPULATION:  First major sweep + possible false CHoCH. Often on news/session open.
                   → WAIT for confirmed CHoCH. Do NOT enter on first sweep.
    
    DISTRIBUTION: Clear sweep + CHoCH + expanding candles in one direction.
                   → Full size, scale into winners, runners.
    """
    
    def __init__(self, lookback_days: int = 5, h4_bars_per_day: int = 6):
        self.lookback_days = lookback_days
        self.h4_bars_per_day = h4_bars_per_day
        self.h4_history: List[H4Candle] = []
        self.current_state = CycleState()
        self._sweep_log: List[Dict] = []      # Recent sweeps with outcomes
        self._choch_log: List[Dict] = []    # Recent CHoCH with follow-through
    
    def feed_h4_candle(self, candle: H4Candle) -> CycleState:
        """Add a new H4 candle and recalculate cycle phase."""
        self.h4_history.append(candle)
        max_bars = self.lookback_days * self.h4_bars_per_day + 20  # Buffer
        if len(self.h4_history) > max_bars:
            self.h4_history = self.h4_history[-max_bars:]
        return self._recalculate()
    
    def feed_sweep(self, sweep_time: datetime, magnitude_pips: float, direction: str,
                   choch_followed: bool, profit_pips: Optional[float] = None) -> None:
        """Log a sweep event and whether CHoCH produced follow-through."""
        self._sweep_log.append({
            "time": sweep_time,
            "magnitude_pips": magnitude_pips,
            "direction": direction,
            "choch_followed": choch_followed,
            "profit_pips": profit_pips or 0,
        })
        self._sweep_log = self._sweep_log[-50:]
        self._recalculate()
    
    def feed_choch(self, choch_time: datetime, direction: str, follow_through: bool,
                  bars_to_target: Optional[int] = None) -> None:
        """Log a CHoCH event and whether price reached its target."""
        self._choch_log.append({
            "time": choch_time,
            "direction": direction,
            "follow_through": follow_through,
            "bars_to_target": bars_to_target,
        })
        self._choch_log = self._choch_log[-50:]
    
    def _recalculate(self) -> CycleState:
        if len(self.h4_history) < self.h4_bars_per_day * 2:
            self.current_state.phase = CyclePhase.UNKNOWN
            self.current_state.confidence = 0.0
            return self.current_state
        
        # Calculate 5-day average H4 range
        recent = self.h4_history[-(self.lookback_days * self.h4_bars_per_day):]
        ranges = [c.high - c.low for c in recent if c.high and c.low]
        if ranges:
            self.current_state.avg_range_5d = sum(ranges) / len(ranges)
        
        # Current forming H4 range (last candle)
        last = self.h4_history[-1]
        self.current_state.current_h4_range = last.high - last.low
        
        # Sweep magnitude: largest sweep in last 24h vs average
        day_ago = last.time - timedelta(hours=24)
        recent_sweeps = [s for s in self._sweep_log if s["time"] >= day_ago]
        if recent_sweeps:
            max_sweep = max(s["magnitude_pips"] for s in recent_sweeps)
            self.current_state.sweep_magnitude = max_sweep / (self.current_state.avg_range_5d or 1)
            self.current_state.last_sweep_time = recent_sweeps[-1]["time"]
        else:
            self.current_state.sweep_magnitude = 0.0
        
        # CHoCH success rate
        recent_choch = self._choch_log[-20:]
        if recent_choch:
            successes = sum(1 for c in recent_choch if c["follow_through"])
            self.current_state.choch_success_rate = successes / len(recent_choch)
        else:
            self.current_state.choch_success_rate = 0.5
        
        # Phase classification
        self.current_state = self._classify_phase(self.current_state)
        return self.current_state
    
    def _classify_phase(self, state: CycleState) -> CycleState:
        """
        Classification logic:
        
        DISTRIBUTION if:
          - sweep_magnitude > 2.0x avg_range (big move)
          - AND choch_success_rate >= 0.5 (follow-through working)
          - AND current_h4_range > avg_range_5d * 1.2 (expanding)
        
        MANIPULATION if:
          - sweep_magnitude > 1.5x avg_range
          - AND choch_success_rate < 0.5 (CHoCH failing = false breaks)
          - OR first sweep after quiet period
        
        ACCUMULATION if:
          - sweep_magnitude <= 1.2x avg_range
          - AND current_h4_range < avg_range_5d * 0.8 (contracting)
          - AND choch_success_rate < 0.4
        """
        new_state = CycleState()
        new_state.avg_range_5d = state.avg_range_5d
        new_state.current_h4_range = state.current_h4_range
        new_state.sweep_magnitude = state.sweep_magnitude
        new_state.choch_success_rate = state.choch_success_rate
        new_state.last_sweep_time = state.last_sweep_time
        
        # Thresholds
        big_sweep = state.sweep_magnitude >= 2.0
        moderate_sweep = state.sweep_magnitude >= 1.5
        quiet_sweep = state.sweep_magnitude <= 1.2
        expanding = state.current_h4_range > state.avg_range_5d * 1.2
        contracting = state.current_h4_range < state.avg_range_5d * 0.8
        choch_working = state.choch_success_rate >= 0.5
        choch_failing = state.choch_success_rate < 0.4
        
        if big_sweep and choch_working and expanding:
            new_state.phase = CyclePhase.DISTRIBUTION
            new_state.confidence = 0.85
            new_state.notes = "Expanding range + CHoCH follow-through = distribution"
        elif moderate_sweep and (choch_failing or not expanding):
            new_state.phase = CyclePhase.MANIPULATION
            new_state.confidence = 0.70
            new_state.notes = "Sweep present but follow-through weak = manipulation"
        elif quiet_sweep and contracting and choch_failing:
            new_state.phase = CyclePhase.ACCUMULATION
            new_state.confidence = 0.75
            new_state.notes = "Contracting range, CHoCH failing = accumulation"
        else:
            new_state.phase = CyclePhase.UNKNOWN
            new_state.confidence = 0.3
            new_state.notes = "Unclear phase — mixed signals"
        
        # Track day counter (resets after 5 days without big sweep)
        if new_state.phase != CyclePhase.UNKNOWN:
            if state.last_sweep_time:
                hours_since = (datetime.now(timezone.utc) - state.last_sweep_time).total_seconds() / 3600
                new_state.day_number = min(5, int(hours_since / 24) + 1)
            else:
                new_state.day_number = 0
        else:
            new_state.day_number = state.day_number
        
        return new_state
    
    def get_state(self) -> CycleState:
        return self.current_state
    
    def get_size_multiplier(self) -> float:
        """Return position size multiplier based on phase."""
        if self.current_state.phase == CyclePhase.DISTRIBUTION:
            return 1.0   # Full size
        elif self.current_state.phase == CyclePhase.MANIPULATION:
            return 0.75  # Reduced — wait for CHoCH
        elif self.current_state.phase == CyclePhase.ACCUMULATION:
            return 0.5   # Minimum — or skip
        return 0.0       # Unknown = no trade
    
    def get_aggression_level(self) -> str:
        """Human-readable aggression for logging."""
        return {
            CyclePhase.DISTRIBUTION: "AGGRESSIVE",
            CyclePhase.MANIPULATION: "CAUTIOUS",
            CyclePhase.ACCUMULATION: "DEFENSIVE",
            CyclePhase.UNKNOWN: "STANDBY",
        }[self.current_state.phase]
