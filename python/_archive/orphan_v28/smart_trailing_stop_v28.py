#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smart_trailing_stop_v28.py — OMNI ICT Production Bot v28.0
Phase 2C: Order-Block-aware trailing stop with partials, Friday logic,
          and liquidity-pool avoidance.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timezone, timedelta
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class TradeStatus(Enum):
    OPEN = "open"
    PARTIAL_TP1 = "partial_tp1"  # 50% closed at TP1
    PARTIAL_TP2 = "partial_tp2"  # Additional 25% closed at TP2
    CLOSED = "closed"
    BE = "breakeven"  # SL moved to entry after TP1


@dataclass
class PositionConfig:
    # Entry
    entry_price: float = 0.0
    open_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    side: str = "BUY"   # BUY or SELL
    size_lots: float = 0.0
    
    # Static levels
    initial_sl: float = 0.0
    tp1: float = 0.0
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    ticket: int = 0  # MT5 ticket number
    
    # Dynamic trailing
    current_sl: float = 0.0
    current_tp: Optional[float] = None
    status: TradeStatus = TradeStatus.OPEN
    
    # Structural context
    order_block_low: Optional[float] = None   # For BUY: SL can move to this OB low
    order_block_high: Optional[float] = None  # For SELL: SL can move to this OB high
    nearest_session_high: Optional[float] = None
    nearest_session_low: Optional[float] = None
    pdh: Optional[float] = None
    pdl: Optional[float] = None


class SmartTrailingStopV28:
    """
    Chris's exact trailing methodology:
    
    §10d: No breakeven before TP1 (3R). Breakeven ONLY after TP1 hit + partial close.
    
    At 3R (TP1):
      - Close 50% of position at market
      - Move SL to entry price
    
    At 5R+:
      - Move SL to nearest unmitigated order block
      - If no OB, trail at 2R cushion behind price
    
    At 10R+:
      - Move SL to nearest 1H swing low/high
      - Lock minimum 8R
    
    Friday closure (17:00 UTC):
      - Positive position → move SL to 1R above entry (protect gains)
      - Negative position → close immediately
      - Hard close ALL positions by 19:00 UTC regardless
    
    Liquidity awareness:
      - Never trail into session high/low (10-pip buffer)
      - Approaching equal highs/lows → tighten to 8R lock
    """
    
    def __init__(self, symbol: str = "XAUUSD", point: float = 0.01, pip_value: float = 10.0):
        self.symbol = symbol
        self.point = point
        self.pip_value = pip_value
        self.min_sl_distance_pips: float = 25.0   # Min 25 pips from price
        self.liquidity_buffer_pips: float = 10.0  # Buffer from session levels
    
    def evaluate(self, pos: PositionConfig, current_price: float,
                 current_time: datetime, h4_bars: Optional[List[Dict]] = None) -> Dict:
        """
        Given current price and position state, return recommended action.
        
        Returns dict with keys:
          - action: "hold" | "close_partial_50" | "move_sl" | "close_full" | "emergency"
          - new_sl: float (if action includes SL move)
          - new_tp: float (if action includes TP adjust)
          - reason: str (human readable)
          - status: TradeStatus
        """
        side = pos.side.upper()
        is_buy = side == "BUY"
        
        # ── Friday hard rules ────────────────────────────────────────
        if self._is_friday_close_window(current_time):
            return self._friday_action(pos, current_price, current_time)
        
        # ── Calculate current R multiple ─────────────────────────────
        r = self._calculate_r(pos)
        if r is None:
            return {"action": "hold", "reason": "Cannot calculate R", "status": pos.status}
        
        current_r = self._price_to_r(pos, current_price, is_buy)
        if current_r is None:
            return {"action": "hold", "reason": "Price not available", "status": pos.status}
        
        # ── TP1 logic ──────────────────────────────────────────────
        if pos.status == TradeStatus.OPEN and current_r >= 3.0:
            # Hit TP1 — partial close + move SL to entry
            return {
                "action": "close_partial_50",
                "new_sl": pos.entry_price,
                "reason": f"TP1 reached ({current_r:.2f}R). Close 50%, move SL to entry.",
                "status": TradeStatus.PARTIAL_TP1,
            }
        
        # ── TP2 logic ────────────────────────────────────────────────
        if pos.status == TradeStatus.PARTIAL_TP1 and current_r >= 5.0:
            # Optional: close additional 25% at TP2
            if pos.tp2:
                # Check if we hit TP2
                if (is_buy and current_price >= pos.tp2) or (not is_buy and current_price <= pos.tp2):
                    return {
                        "action": "close_partial_25",
                        "new_sl": self._find_ob_sl(pos, current_price, is_buy),
                        "reason": f"TP2 reached ({current_r:.2f}R). Close 25% more, move SL to OB.",
                        "status": TradeStatus.PARTIAL_TP2,
                    }
            else:
                # No TP2 defined, just move SL to OB at 5R+
                new_sl = self._find_ob_sl(pos, current_price, is_buy)
                if new_sl and ((is_buy and new_sl > pos.current_sl) or (not is_buy and new_sl < pos.current_sl)):
                    return {
                        "action": "move_sl",
                        "new_sl": new_sl,
                        "reason": f"5R+ reached ({current_r:.2f}R). Move SL to nearest OB.",
                        "status": TradeStatus.PARTIAL_TP1,
                    }
        
        # ── 10R+ swing lock ──────────────────────────────────────────
        if current_r >= 10.0:
            swing_sl = self._find_swing_sl(pos, current_price, is_buy, h4_bars)
            if swing_sl:
                min_lock_r = 8.0
                min_lock_price = pos.entry_price + (r * min_lock_r) if is_buy else pos.entry_price - (r * min_lock_r)
                if is_buy:
                    new_sl = max(swing_sl, min_lock_price)
                    if new_sl > pos.current_sl:
                        return {"action": "move_sl", "new_sl": new_sl, "reason": f"10R+ swing lock ({current_r:.2f}R). Minimum 8R secured.", "status": pos.status}
                else:
                    new_sl = min(swing_sl, min_lock_price)
                    if new_sl < pos.current_sl:
                        return {"action": "move_sl", "new_sl": new_sl, "reason": f"10R+ swing lock ({current_r:.2f}R). Minimum 8R secured.", "status": pos.status}
        
        # ── Liquidity proximity tighten ─────────────────────────────
        if self._approaching_liquidity(pos, current_price, is_buy):
            tighten = self._tighten_for_liquidity(pos, current_price, is_buy, r)
            if tighten:
                return {"action": "move_sl", "new_sl": tighten["new_sl"], "reason": tighten["reason"], "status": pos.status}
        
        return {"action": "hold", "reason": f"No action at {current_r:.2f}R", "status": pos.status}
    
    # ── Internal helpers ──────────────────────────────────────────────
    def _calculate_r(self, pos: PositionConfig) -> Optional[float]:
        if pos.entry_price <= 0 or pos.initial_sl <= 0:
            return None
        return abs(pos.entry_price - pos.initial_sl)
    
    def _price_to_r(self, pos: PositionConfig, price: float, is_buy: bool) -> Optional[float]:
        r = self._calculate_r(pos)
        if not r or r <= 0:
            return None
        if is_buy:
            return (price - pos.entry_price) / r
        return (pos.entry_price - price) / r
    
    def _is_friday_close_window(self, t: datetime) -> bool:
        if t.weekday() != 4:  # 4 = Friday
            return False
        hour = t.hour
        return hour >= 17  # 17:00 UTC onward
    
    def _friday_action(self, pos: PositionConfig, price: float, t: datetime) -> Dict:
        is_buy = pos.side.upper() == "BUY"
        current_r = self._price_to_r(pos, price, is_buy) or 0
        
        if t.hour >= 19:
            # Hard close
            return {"action": "close_full", "reason": "Friday 19:00 UTC hard close", "status": TradeStatus.CLOSED}
        
        if current_r > 0:
            # Positive: move SL to 1R above entry
            new_sl = pos.entry_price + self._calculate_r(pos) if is_buy else pos.entry_price - self._calculate_r(pos)
            return {"action": "move_sl", "new_sl": new_sl, "reason": "Friday close — protect gains at 1R minimum", "status": pos.status}
        else:
            return {"action": "close_full", "reason": "Friday close — negative position", "status": TradeStatus.CLOSED}
    
    def _find_ob_sl(self, pos: PositionConfig, price: float, is_buy: bool) -> Optional[float]:
        """Find nearest order block for SL placement."""
        if is_buy and pos.order_block_low:
            return pos.order_block_low - (self.point * 5)  # 5 ticks below OB
        if not is_buy and pos.order_block_high:
            return pos.order_block_high + (self.point * 5)
        # Fallback: 2R cushion
        r = self._calculate_r(pos)
        if not r:
            return None
        if is_buy:
            return price - (r * 2)
        return price + (r * 2)
    
    def _find_swing_sl(self, pos: PositionConfig, price: float, is_buy: bool,
                       h4_bars: Optional[List[Dict]]) -> Optional[float]:
        """Find nearest H4 swing low/high for 10R+ lock."""
        if not h4_bars or len(h4_bars) < 3:
            return None
        # Fractal logic on H4 bars: look for lowest low with higher lows on each side
        if is_buy:
            swing_low = None
            for i in range(1, len(h4_bars) - 1):
                if h4_bars[i]["low"] < h4_bars[i-1]["low"] and h4_bars[i]["low"] < h4_bars[i+1]["low"]:
                    if swing_low is None or h4_bars[i]["low"] < swing_low:
                        swing_low = h4_bars[i]["low"]
            return swing_low
        else:
            swing_high = None
            for i in range(1, len(h4_bars) - 1):
                if h4_bars[i]["high"] > h4_bars[i-1]["high"] and h4_bars[i]["high"] > h4_bars[i+1]["high"]:
                    if swing_high is None or h4_bars[i]["high"] > swing_high:
                        swing_high = h4_bars[i]["high"]
            return swing_high
    
    def _approaching_liquidity(self, pos: PositionConfig, price: float, is_buy: bool) -> bool:
        buffer = self.liquidity_buffer_pips * self.point * 10  # Approx pip in points for XAUUSD
        if is_buy:
            targets = [pos.pdh, pos.nearest_session_high]
            for t in targets:
                if t and 0 < t - price < buffer:
                    return True
        else:
            targets = [pos.pdl, pos.nearest_session_low]
            for t in targets:
                if t and 0 < price - t < buffer:
                    return True
        return False
    
    def _tighten_for_liquidity(self, pos: PositionConfig, price: float, is_buy: bool, r: float) -> Optional[Dict]:
        """Tighten SL to 8R lock when approaching major liquidity."""
        if r <= 0:
            return None
        lock_price = pos.entry_price + (r * 8) if is_buy else pos.entry_price - (r * 8)
        if is_buy and lock_price > pos.current_sl:
            return {"new_sl": lock_price, "reason": "Approaching liquidity — tighten to 8R lock"}
        if not is_buy and lock_price < pos.current_sl:
            return {"new_sl": lock_price, "reason": "Approaching liquidity — tighten to 8R lock"}
        return None
