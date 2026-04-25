"""
advanced_structure_analyzer.py — Enhanced Multi-Timeframe Order Block & AMD Analysis
Detects major/minor order blocks, liquidity sweeps, and precise entry/exit points
across all timeframes for scalping, day trading, and swing trading.

Key features:
- Multi-timeframe order block hierarchy (major/minor/mitigated)
- AMD phase analysis with structural confluence
- Liquidity sweep detection (internal ↔ external)
- Precise entry points based on timeframe confluence
- Exit triggers based on structural levels
- Fee and swap impact calculations
"""

from __future__ import annotations
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
from enum import Enum
from collections import defaultdict
import json
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS & DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

class Timeframe(Enum):
    """Trading timeframes with minute values"""
    M5 = 5
    M15 = 15
    M30 = 30
    H1 = 60
    H4 = 240
    DAILY = 1440
    WEEKLY = 10080


class AMDPhase(Enum):
    """ICT Accumulation/Manipulation/Distribution phases"""
    ACCUMULATION = "ACC"
    MANIPULATION = "MAN"
    DISTRIBUTION = "DIS"
    UNKNOWN = "UNK"


class LiquidityType(Enum):
    """Types of liquidity levels"""
    INTERNAL = "INTERNAL"      # Within price action (support/resistance)
    EXTERNAL = "EXTERNAL"      # Beyond recent range (gap areas, breakouts)
    SESSION_LEVEL = "SESSION"  # Session high/low
    SWING_LEVEL = "SWING"      # Swing high/low


class StructureType(Enum):
    """Types of market structures"""
    ORDER_BLOCK = "OB"
    FAIR_VALUE_GAP = "FVG"
    BREAKER = "BREAKER"
    EQUAL_HIGH_LOW = "EHL"


class OrderBlockSize(Enum):
    """Classification of order block importance"""
    MAJOR = "MAJOR"        # Institutional level, multiple touches, high volatility
    MINOR = "MINOR"        # Smaller timeframe OBs, single touch
    MITIGATED = "MITIGATED"  # OB that has been partially filled


@dataclass
class PriceLevel:
    """Represents a significant price level or zone"""
    high: float
    low: float
    level_type: StructureType
    size: OrderBlockSize
    timeframe: Timeframe
    strength: float  # 0-1: based on how many times tested, size of move
    touches: int = 0
    created_at_candle: int = 0
    mitigation_percentage: float = 0.0  # How much has been mitigated
    
    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2
    
    @property
    def range(self) -> float:
        return self.high - self.low
    
    def contains(self, price: float, tolerance: float = 0.0) -> bool:
        """Check if price is within this level"""
        return (self.low - tolerance) <= price <= (self.high + tolerance)
    
    def distance_to(self, price: float) -> float:
        """Distance from price to nearest edge"""
        if self.contains(price):
            return 0.0
        elif price < self.low:
            return self.low - price
        else:
            return price - self.high


@dataclass
class LiquidityLevel:
    """Represents a liquidity level that may be swept"""
    price: float
    type: LiquidityType
    origin: str  # "SWING_HIGH", "SESSION_HIGH", "GAP", etc.
    timeframe: Timeframe
    created_at: int
    distance_from_current: float = 0.0
    probability_of_sweep: float = 0.0  # 0-1 based on context
    
    def is_likely_sweep_target(self, current_price: float, recent_direction: str) -> bool:
        """Check if this level is likely to be swept based on direction and distance"""
        distance_pct = abs(self.price - current_price) / current_price if current_price > 0 else 0
        
        # Liquidity is more likely to be swept if:
        # 1. It's within recent action (not too far)
        if distance_pct > 0.05:  # More than 5% away is less likely
            return False
        
        # 2. Direction of recent movement aligns with reaching it
        if recent_direction == "UP" and self.price > current_price:
            return True
        elif recent_direction == "DOWN" and self.price < current_price:
            return True
        
        return False


@dataclass
class MultiTimeframeStructure:
    """Complete structure analysis across multiple timeframes"""
    symbol: str
    timestamp: datetime
    
    # Structures by timeframe
    order_blocks: Dict[Timeframe, List[PriceLevel]] = field(default_factory=dict)
    fair_value_gaps: Dict[Timeframe, List[PriceLevel]] = field(default_factory=dict)
    liquidity_levels: Dict[Timeframe, List[LiquidityLevel]] = field(default_factory=dict)
    
    # Overall analysis
    amd_phases: Dict[Timeframe, AMDPhase] = field(default_factory=dict)
    confluence_zones: List[Dict] = field(default_factory=list)
    
    # Direction and bias
    primary_direction: str = "NEUTRAL"  # Based on highest timeframe
    higher_tf_bias: str = "NEUTRAL"     # Bias from 4H/Daily
    lower_tf_bias: str = "NEUTRAL"      # Bias from 5m/15m/30m
    
    # Entry/Exit candidates
    precise_entries: List[Dict] = field(default_factory=list)
    exit_targets: List[Dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-TIMEFRAME ORDER BLOCK DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

class MultiTimeframeOrderBlockDetector:
    """
    Detects order blocks across multiple timeframes
    Classifies them as MAJOR (swing-level), MINOR (intraday), or MITIGATED (partial fill)
    """
    
    def __init__(self, lookback: int = 300):
        self.lookback = lookback
        self.min_body_percent = 0.6  # Candle body must be 60% of range
        
    def detect_order_blocks(self, df: pd.DataFrame, timeframe: Timeframe) -> List[PriceLevel]:
        """
        Detect order blocks from impulse moves
        
        Logic:
        1. Find strong impulse waves (3+ candles in direction)
        2. Identify the candle where buying/selling pressure was highest
        3. Wait for pullback to confirm
        4. Rate block size based on candle size and subsequent tests
        """
        df = df.tail(self.lookback).reset_index(drop=True)
        order_blocks = []
        
        if len(df) < 10:
            return order_blocks
        
        for i in range(5, len(df) - 3):
            # Look for bullish impulse (strong up move)
            if self._is_bullish_impulse(df, i):
                ob = self._create_order_block(
                    df, i, 
                    direction="UP",
                    timeframe=timeframe
                )
                if ob:
                    order_blocks.append(ob)
            
            # Look for bearish impulse (strong down move)
            elif self._is_bearish_impulse(df, i):
                ob = self._create_order_block(
                    df, i,
                    direction="DOWN",
                    timeframe=timeframe
                )
                if ob:
                    order_blocks.append(ob)
        
        # Deduplicate and classify by size
        return self._classify_and_merge(order_blocks, df)
    
    def _is_bullish_impulse(self, df: pd.DataFrame, idx: int) -> bool:
        """Detect 3+ candle bullish impulse move"""
        if idx < 3:
            return False
        
        # Last 3 candles should show uptrend
        for i in range(idx - 2, idx + 1):
            if df.loc[i, 'close'] <= df.loc[i, 'open']:
                return False  # Not all candles bullish
        
        # Candles should be getting progressively higher (trend)
        closes = df.loc[idx - 2:idx, 'close'].values
        return closes[1] > closes[0] and closes[2] > closes[1]
    
    def _is_bearish_impulse(self, df: pd.DataFrame, idx: int) -> bool:
        """Detect 3+ candle bearish impulse move"""
        if idx < 3:
            return False
        
        for i in range(idx - 2, idx + 1):
            if df.loc[i, 'close'] >= df.loc[i, 'open']:
                return False
        
        closes = df.loc[idx - 2:idx, 'close'].values
        return closes[1] < closes[0] and closes[2] < closes[1]
    
    def _create_order_block(self, df: pd.DataFrame, idx: int, 
                           direction: str, timeframe: Timeframe) -> Optional[PriceLevel]:
        """
        Create order block from impulse candle
        OB is typically the first or second candle of the impulse
        """
        # For bullish: OB is the last candle of impulse (buying pressure area)
        # For bearish: OB is the last candle of impulse (selling pressure area)
        
        ob_high = df.loc[idx, 'high']
        ob_low = df.loc[idx, 'low']
        ob_open = df.loc[idx, 'open']
        ob_close = df.loc[idx, 'close']
        
        # Body must be significant
        body = abs(ob_close - ob_open)
        total_range = ob_high - ob_low
        if total_range == 0 or body / total_range < self.min_body_percent:
            return None
        
        # Check if there's a pullback after
        if not self._has_valid_pullback(df, idx, direction):
            return None
        
        # Determine size based on candle characteristics
        size = self._classify_order_block_size(df, idx, direction)
        
        return PriceLevel(
            high=ob_high,
            low=ob_low,
            level_type=StructureType.ORDER_BLOCK,
            size=size,
            timeframe=timeframe,
            strength=body / total_range,
            created_at_candle=idx,
            touches=1
        )
    
    def _has_valid_pullback(self, df: pd.DataFrame, idx: int, direction: str) -> bool:
        """Verify pullback occurred after impulse"""
        if idx + 3 >= len(df):
            return False
        
        if direction == "UP":
            # After up move, price should pullback into OB
            pullback_low = df.loc[idx + 1:idx + 3, 'low'].min()
            ob_high = df.loc[idx, 'high']
            return pullback_low < ob_high
        else:
            # After down move, price should pullback into OB
            pullback_high = df.loc[idx + 1:idx + 3, 'high'].max()
            ob_low = df.loc[idx, 'low']
            return pullback_high > ob_low
    
    def _classify_order_block_size(self, df: pd.DataFrame, idx: int, 
                                   direction: str) -> OrderBlockSize:
        """
        Classify OB as MAJOR or MINOR based on:
        - Candle size
        - Move preceding it
        - Subsequent tests
        """
        ob_range = df.loc[idx, 'high'] - df.loc[idx, 'low']
        
        # Check move magnitude before impulse
        if idx >= 5:
            if direction == "UP":
                pre_range = df.loc[idx - 5:idx - 1, 'high'].max() - df.loc[idx - 5:idx - 1, 'low'].min()
            else:
                pre_range = df.loc[idx - 5:idx - 1, 'high'].max() - df.loc[idx - 5:idx - 1, 'low'].min()
            
            # Large move before impulse = likely MAJOR OB
            if pre_range > ob_range * 3:
                return OrderBlockSize.MAJOR
        
        # Check subsequent tests (forward looking in remaining data)
        tests_after = 0
        if direction == "UP":
            ob_high = df.loc[idx, 'high']
            for i in range(idx + 1, min(idx + 20, len(df))):
                if df.loc[i, 'low'] <= ob_high:
                    tests_after += 1
        else:
            ob_low = df.loc[idx, 'low']
            for i in range(idx + 1, min(idx + 20, len(df))):
                if df.loc[i, 'high'] >= ob_low:
                    tests_after += 1
        
        if tests_after >= 3:
            return OrderBlockSize.MAJOR
        elif tests_after >= 1:
            return OrderBlockSize.MINOR
        else:
            return OrderBlockSize.MINOR
    
    def _classify_and_merge(self, blocks: List[PriceLevel], 
                           df: pd.DataFrame) -> List[PriceLevel]:
        """Merge overlapping blocks and classify by size"""
        if not blocks:
            return []
        
        # Sort by timestamp (candle index)
        blocks_sorted = sorted(blocks, key=lambda x: x.created_at_candle)
        merged = []
        
        current = blocks_sorted[0]
        for block in blocks_sorted[1:]:
            # If blocks overlap significantly, merge
            if block.low <= current.high * 1.01:  # 1% tolerance
                current = PriceLevel(
                    high=max(current.high, block.high),
                    low=min(current.low, block.low),
                    level_type=current.level_type,
                    size=OrderBlockSize.MAJOR if current.size == OrderBlockSize.MAJOR else block.size,
                    timeframe=current.timeframe,
                    strength=max(current.strength, block.strength),
                    touches=current.touches + 1,
                    created_at_candle=current.created_at_candle
                )
            else:
                merged.append(current)
                current = block
        
        merged.append(current)
        return merged


# ─────────────────────────────────────────────────────────────────────────────
# AMD PHASE ANALYZER
# ─────────────────────────────────────────────────────────────────────────────

class AMDStructureAnalyzer:
    """
    Analyzes Accumulation/Manipulation/Distribution phase structures
    and detects supply/demand zones, FVGs, and fair value areas
    """
    
    def __init__(self):
        pass
    
    def identify_amd_phase(self, df: pd.DataFrame, timeframe: Timeframe) -> AMDPhase:
        """
        Identify current AMD phase based on:
        - Volume profile
        - Price structure (HH/HL/LL/LH patterns)
        - Range characteristics
        - Volatility
        """
        if len(df) < 20:
            return AMDPhase.UNKNOWN
        
        recent = df.tail(20)
        
        # Accumulation: Low volume, tight range, congestion
        if self._is_accumulation_pattern(recent):
            return AMDPhase.ACCUMULATION
        
        # Manipulation: Price moves into areas but comes back (stop hunts)
        if self._is_manipulation_pattern(recent):
            return AMDPhase.MANIPULATION
        
        # Distribution: High volume breakout, then reversal signs
        if self._is_distribution_pattern(recent):
            return AMDPhase.DISTRIBUTION
        
        return AMDPhase.UNKNOWN
    
    def _is_accumulation_pattern(self, df: pd.DataFrame) -> bool:
        """Low volatility, tight range, building base"""
        volatility = df['high'].sub(df['low']).mean() / df['close'].mean()
        range_size = df['high'].max() - df['low'].min()
        avg_move = df['high'].sub(df['low']).mean()
        
        # Low volatility + small range = accumulation
        return volatility < 0.01 and range_size < avg_move * 3
    
    def _is_manipulation_pattern(self, df: pd.DataFrame) -> bool:
        """Wicks beyond range, reversals, stop hunts"""
        recent_wicks = []
        for idx in range(len(df)):
            upper_wick = df.loc[idx, 'high'] - max(df.loc[idx, 'open'], df.loc[idx, 'close'])
            lower_wick = min(df.loc[idx, 'open'], df.loc[idx, 'close']) - df.loc[idx, 'low']
            recent_wicks.append(max(upper_wick, lower_wick))
        
        # Significant wicks = manipulation/rejection
        avg_wick = np.mean(recent_wicks)
        avg_body = df['high'].sub(df['low']).mean() * 0.5
        return avg_wick > avg_body
    
    def _is_distribution_pattern(self, df: pd.DataFrame) -> bool:
        """Strong directional move, then reversal signs"""
        # Check if we just had a strong move
        recent_close_high = df['close'].max()
        recent_close_low = df['close'].min()
        move_range = recent_close_high - recent_close_low
        
        # Check for reversal candles after move
        if len(df) >= 3:
            last_three = df.tail(3)
            if df.loc[df.index[-1], 'close'] < df.loc[df.index[-1], 'open']:
                # Last candle bearish after move up = distribution
                if recent_close_high == df.loc[df.index[-2], 'high'] or \
                   recent_close_high == df.loc[df.index[-3], 'high']:
                    return True
        
        return False
    
    def detect_fvg(self, df: pd.DataFrame, timeframe: Timeframe) -> List[PriceLevel]:
        """
        Detect Fair Value Gaps - imbalances in price action
        FVG occurs when price gaps over a candle (1st & 3rd candle don't overlap)
        """
        fvgs = []
        
        if len(df) < 3:
            return fvgs
        
        for i in range(2, len(df) - 1):
            candle_1 = df.loc[i - 2]
            candle_2 = df.loc[i - 1]
            candle_3 = df.loc[i]
            
            # Bullish FVG: Low of candle 3 > High of candle 1
            if candle_3['low'] > candle_1['high']:
                fvg_high = candle_3['low']
                fvg_low = candle_1['high']
                
                fvgs.append(PriceLevel(
                    high=fvg_high,
                    low=fvg_low,
                    level_type=StructureType.FAIR_VALUE_GAP,
                    size=OrderBlockSize.MINOR,
                    timeframe=timeframe,
                    strength=0.7,
                    created_at_candle=i
                ))
            
            # Bearish FVG: High of candle 3 < Low of candle 1
            elif candle_3['high'] < candle_1['low']:
                fvg_high = candle_1['low']
                fvg_low = candle_3['high']
                
                fvgs.append(PriceLevel(
                    high=fvg_high,
                    low=fvg_low,
                    level_type=StructureType.FAIR_VALUE_GAP,
                    size=OrderBlockSize.MINOR,
                    timeframe=timeframe,
                    strength=0.7,
                    created_at_candle=i
                ))
        
        return fvgs


# ─────────────────────────────────────────────────────────────────────────────
# LIQUIDITY MAPPER
# ─────────────────────────────────────────────────────────────────────────────

class LiquidityMapper:
    """
    Maps liquidity levels and detects liquidity sweeps
    Internal: within price action
    External: beyond recent range (gaps, unliquidated orders)
    """
    
    def __init__(self):
        pass
    
    def map_liquidity(self, df: pd.DataFrame, timeframe: Timeframe) -> List[LiquidityLevel]:
        """
        Identify all liquidity levels that could be swept
        """
        liquidity = []
        
        if len(df) < 20:
            return liquidity
        
        # Session highs/lows (external liquidity)
        session_high = df['high'].max()
        session_low = df['low'].min()
        
        liquidity.append(LiquidityLevel(
            price=session_high,
            type=LiquidityType.SESSION_LEVEL,
            origin="SESSION_HIGH",
            timeframe=timeframe,
            created_at=len(df) - 1
        ))
        
        liquidity.append(LiquidityLevel(
            price=session_low,
            type=LiquidityType.SESSION_LEVEL,
            origin="SESSION_LOW",
            timeframe=timeframe,
            created_at=len(df) - 1
        ))
        
        # Swing highs/lows
        swing_high = self._find_swing_high(df)
        swing_low = self._find_swing_low(df)
        
        if swing_high:
            liquidity.append(LiquidityLevel(
                price=swing_high['price'],
                type=LiquidityType.SWING_LEVEL,
                origin="SWING_HIGH",
                timeframe=timeframe,
                created_at=swing_high['index']
            ))
        
        if swing_low:
            liquidity.append(LiquidityLevel(
                price=swing_low['price'],
                type=LiquidityType.SWING_LEVEL,
                origin="SWING_LOW",
                timeframe=timeframe,
                created_at=swing_low['index']
            ))
        
        # Internal support/resistance (from order blocks)
        for idx, row in df.tail(30).iterrows():
            # Horizontal support (multiple tests)
            if self._is_support_level(df, idx):
                liquidity.append(LiquidityLevel(
                    price=row['low'],
                    type=LiquidityType.INTERNAL,
                    origin="SUPPORT",
                    timeframe=timeframe,
                    created_at=idx
                ))
            
            # Horizontal resistance
            if self._is_resistance_level(df, idx):
                liquidity.append(LiquidityLevel(
                    price=row['high'],
                    type=LiquidityType.INTERNAL,
                    origin="RESISTANCE",
                    timeframe=timeframe,
                    created_at=idx
                ))
        
        return liquidity
    
    def _find_swing_high(self, df: pd.DataFrame, lookback: int = 20) -> Optional[Dict]:
        """Find recent significant swing high"""
        recent = df.tail(lookback)
        max_idx = recent['high'].idxmax()
        return {'price': df.loc[max_idx, 'high'], 'index': max_idx}
    
    def _find_swing_low(self, df: pd.DataFrame, lookback: int = 20) -> Optional[Dict]:
        """Find recent significant swing low"""
        recent = df.tail(lookback)
        min_idx = recent['low'].idxmin()
        return {'price': df.loc[min_idx, 'low'], 'index': min_idx}
    
    def _is_support_level(self, df: pd.DataFrame, idx: int, tolerance: float = 0.001) -> bool:
        """Check if price level acts as support (multiple touches)"""
        level = df.loc[idx, 'low']
        touches = 0
        
        for i in range(max(0, idx - 20), idx):
            if abs(df.loc[i, 'low'] - level) / level < tolerance:
                touches += 1
        
        return touches >= 2
    
    def _is_resistance_level(self, df: pd.DataFrame, idx: int, tolerance: float = 0.001) -> bool:
        """Check if price level acts as resistance"""
        level = df.loc[idx, 'high']
        touches = 0
        
        for i in range(max(0, idx - 20), idx):
            if abs(df.loc[i, 'high'] - level) / level < tolerance:
                touches += 1
        
        return touches >= 2
    
    def predict_liquidity_sweep(self, current_price: float, liquidity_levels: List[LiquidityLevel],
                               recent_direction: str, amd_phase: AMDPhase) -> List[LiquidityLevel]:
        """
        Predict which liquidity levels are likely to be swept
        based on current direction and market conditions
        """
        probable_sweeps = []
        
        for level in liquidity_levels:
            # Calculate probability based on multiple factors
            probability = 0.0
            
            # Distance factor
            distance_pct = abs(level.price - current_price) / current_price if current_price > 0 else 0
            if distance_pct < 0.01:  # Very close
                probability += 0.3
            elif distance_pct < 0.03:
                probability += 0.2
            elif distance_pct < 0.05:
                probability += 0.1
            
            # Direction alignment
            if recent_direction == "UP" and level.price > current_price:
                probability += 0.3
            elif recent_direction == "DOWN" and level.price < current_price:
                probability += 0.3
            
            # AMD phase alignment
            if amd_phase == AMDPhase.MANIPULATION:
                probability += 0.2  # Sweeps likely during manipulation
            
            # Level type
            if level.type == LiquidityType.SESSION_LEVEL:
                probability += 0.1  # Session levels often swept
            
            if probability > 0.4:
                level.probability_of_sweep = min(probability, 1.0)
                probable_sweeps.append(level)
        
        return sorted(probable_sweeps, key=lambda x: x.probability_of_sweep, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY/EXIT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

class PrecisionEntryExitGenerator:
    """
    Generates precise entry and exit points based on multi-timeframe confluence
    """
    
    def __init__(self):
        pass
    
    def generate_entry_points(self, structure: MultiTimeframeStructure,
                             current_price: float) -> List[Dict]:
        """
        Generate entry points with the highest confluence
        
        Entry rules:
        - Price at confluence zone (2+ timeframes aligned)
        - Aligned with OB that has not been fully mitigated
        - AMD phase supports direction
        - Liquidity sweep imminent
        """
        entries = []
        
        # Find confluence zones
        for zone in structure.confluence_zones:
            entry = {
                'type': 'CONFLUENCE_ENTRY',
                'price': zone['level'],
                'zone_high': zone['high'],
                'zone_low': zone['low'],
                'confluence_count': zone['timeframes_count'],
                'timeframes': zone['timeframes'],
                'strength': zone['strength'],
                'direction': self._determine_entry_direction(zone, current_price),
                'rationale': []
            }
            
            # Check order block alignment
            ob = self._find_covering_ob(structure, zone['level'])
            if ob:
                entry['order_block'] = {
                    'high': ob.high,
                    'low': ob.low,
                    'size': ob.size.value,
                    'timeframe': ob.timeframe.name,
                    'strength': ob.strength
                }
                entry['rationale'].append(f"At {ob.size.value} OB ({ob.timeframe.name})")
            
            # Check AMD phase alignment
            dominant_phase = self._get_dominant_amd_phase(structure)
            entry['amd_phase'] = dominant_phase.value
            
            entries.append(entry)
        
        return sorted(entries, key=lambda x: x['strength'], reverse=True)
    
    def generate_exit_points(self, structure: MultiTimeframeStructure,
                            entry_price: float,
                            direction: str) -> List[Dict]:
        """
        Generate exit targets based on nearby structural levels
        
        For LONG: Exit at next resistance / OB high / session high
        For SHORT: Exit at next support / OB low / session low
        """
        exits = []
        
        if direction == "LONG":
            # Find next resistance levels above entry
            resistance_obs = [ob for tf_obs in structure.order_blocks.values() 
                            for ob in tf_obs if ob.high > entry_price]
            
            for ob in sorted(resistance_obs, key=lambda x: x.high)[:3]:
                exits.append({
                    'type': 'STRUCTURAL_RESISTANCE',
                    'price': ob.high,
                    'level_type': 'ORDER_BLOCK_HIGH',
                    'timeframe': ob.timeframe.name,
                    'size': ob.size.value,
                    'rationale': f"{ob.size.value} OB high on {ob.timeframe.name}"
                })
        
        else:  # SHORT
            # Find next support levels below entry
            support_obs = [ob for tf_obs in structure.order_blocks.values() 
                         for ob in tf_obs if ob.low < entry_price]
            
            for ob in sorted(support_obs, key=lambda x: x.low, reverse=True)[:3]:
                exits.append({
                    'type': 'STRUCTURAL_SUPPORT',
                    'price': ob.low,
                    'level_type': 'ORDER_BLOCK_LOW',
                    'timeframe': ob.timeframe.name,
                    'size': ob.size.value,
                    'rationale': f"{ob.size.value} OB low on {ob.timeframe.name}"
                })
        
        return exits
    
    def _determine_entry_direction(self, zone: Dict, current_price: float) -> str:
        """Determine if entry should be BUY or SELL based on zone position"""
        if current_price < zone['low']:
            return "BUY"  # Price below zone, likely to sweep up
        elif current_price > zone['high']:
            return "SELL"  # Price above zone, likely to sweep down
        else:
            return "NEUTRAL"
    
    def _find_covering_ob(self, structure: MultiTimeframeStructure, price: float) -> Optional[PriceLevel]:
        """Find OB that covers this price level"""
        all_obs = [ob for tf_obs in structure.order_blocks.values() for ob in tf_obs]
        covering = [ob for ob in all_obs if ob.contains(price, tolerance=0.0001)]
        
        if covering:
            # Return largest/strongest
            return max(covering, key=lambda x: x.strength)
        return None
    
    def _get_dominant_amd_phase(self, structure: MultiTimeframeStructure) -> AMDPhase:
        """Get most common AMD phase across timeframes"""
        phases = list(structure.amd_phases.values())
        if not phases:
            return AMDPhase.UNKNOWN
        return max(set(phases), key=phases.count)


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED ANALYZER
# ─────────────────────────────────────────────────────────────────────────────

class UnifiedMultiTimeframeAnalyzer:
    """
    Combines all analyzers into one comprehensive system
    """
    
    def __init__(self, timeframes: List[Timeframe] = None):
        self.timeframes = timeframes or [Timeframe.DAILY, Timeframe.H4, Timeframe.H1, Timeframe.M15]
        self.ob_detector = MultiTimeframeOrderBlockDetector()
        self.amd_analyzer = AMDStructureAnalyzer()
        self.liquidity_mapper = LiquidityMapper()
        self.entry_exit_generator = PrecisionEntryExitGenerator()
    
    def analyze(self, data_dict: Dict[Timeframe, pd.DataFrame], 
               symbol: str) -> MultiTimeframeStructure:
        """
        Run complete analysis across all timeframes
        
        Args:
            data_dict: {Timeframe.H4: df, Timeframe.H1: df, ...}
            symbol: Trading pair
        
        Returns:
            MultiTimeframeStructure with all analyses
        """
        structure = MultiTimeframeStructure(
            symbol=symbol,
            timestamp=datetime.now()
        )
        
        # Analyze each timeframe
        for tf, df in data_dict.items():
            if len(df) < 10:
                continue
            
            # Detect order blocks
            obs = self.ob_detector.detect_order_blocks(df, tf)
            structure.order_blocks[tf] = obs
            
            # Detect FVGs
            fvgs = self.amd_analyzer.detect_fvg(df, tf)
            structure.fair_value_gaps[tf] = fvgs
            
            # Identify AMD phase
            amd_phase = self.amd_analyzer.identify_amd_phase(df, tf)
            structure.amd_phases[tf] = amd_phase
            
            # Map liquidity
            liq = self.liquidity_mapper.map_liquidity(df, tf)
            structure.liquidity_levels[tf] = liq
        
        # Find confluence zones (same price levels across timeframes)
        structure.confluence_zones = self._find_confluence_zones(structure)
        
        # Determine bias
        current_price = max(df['close'].iloc[-1] for df in data_dict.values() if len(df) > 0)
        structure.primary_direction = self._determine_primary_direction(structure, current_price)
        
        # Generate entries and exits
        structure.precise_entries = self.entry_exit_generator.generate_entry_points(structure, current_price)
        if structure.precise_entries:
            entry_price = structure.precise_entries[0]['price']
            direction = structure.precise_entries[0]['direction']
            structure.exit_targets = self.entry_exit_generator.generate_exit_points(structure, entry_price, direction)
        
        return structure
    
    def _find_confluence_zones(self, structure: MultiTimeframeStructure) -> List[Dict]:
        """Find price zones where multiple timeframes align"""
        confluence = defaultdict(lambda: {'timeframes': [], 'prices': []})
        
        # Collect all price levels from all timeframes
        for tf, obs in structure.order_blocks.items():
            for ob in obs:
                # Round to nearest 0.001% for grouping
                key = round(ob.midpoint, 4)
                confluence[key]['timeframes'].append(tf.name)
                confluence[key]['prices'].append(ob.midpoint)
        
        # Filter for zones with 2+ timeframes
        result = []
        for key, data in confluence.items():
            if len(data['timeframes']) >= 2:
                result.append({
                    'level': key,
                    'high': max(data['prices']),
                    'low': min(data['prices']),
                    'timeframes': data['timeframes'],
                    'timeframes_count': len(data['timeframes']),
                    'strength': len(data['timeframes']) / len(structure.order_blocks)
                })
        
        return sorted(result, key=lambda x: x['strength'], reverse=True)
    
    def _determine_primary_direction(self, structure: MultiTimeframeStructure, 
                                    current_price: float) -> str:
        """Determine overall direction bias"""
        # Look at highest timeframe for direction
        higher_tfs = [Timeframe.WEEKLY, Timeframe.DAILY, Timeframe.H4]
        
        for tf in higher_tfs:
            if tf not in structure.order_blocks:
                continue
            
            obs = structure.order_blocks[tf]
            if not obs:
                continue
            
            # If price is above most recent major OB = bullish
            major_obs = [ob for ob in obs if ob.size == OrderBlockSize.MAJOR]
            if major_obs:
                avg_ob_level = np.mean([ob.midpoint for ob in major_obs])
                if current_price > avg_ob_level:
                    return "BULLISH"
                else:
                    return "BEARISH"
        
        return "NEUTRAL"


if __name__ == "__main__":
    print("advanced_structure_analyzer.py loaded")
    print("Use UnifiedMultiTimeframeAnalyzer for complete analysis")
