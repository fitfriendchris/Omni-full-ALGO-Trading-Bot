"""
integrated_trading_engine.py — Complete Multi-Timeframe Trading System
Combines advanced structure analysis with sophisticated risk management
Produces actionable, high-confidence trade signals with precise execution parameters
"""

from __future__ import annotations
import json
import pandas as pd
import numpy as np
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from enum import Enum

from advanced_structure_analyzer import (
    UnifiedMultiTimeframeAnalyzer, MultiTimeframeStructure, Timeframe,
    AMDPhase, OrderBlockSize
)
from advanced_risk_manager import (
    AdvancedPositionSizer, FeeCalculator, AccountMetrics,
    PositionMetrics, AssetClass, TradeDuration
)


class SignalConfidence(Enum):
    """Signal confidence levels"""
    VERY_HIGH = 4   # 4+ timeframes aligned, major confluence
    HIGH = 3        # 3 timeframes aligned, good confluence
    MEDIUM = 2      # 2 timeframes aligned, adequate setup
    LOW = 1         # 1 timeframe, weak signal
    INVALID = 0     # Does not meet minimum requirements


@dataclass
class TradingSignal:
    """Complete actionable trading signal"""
    symbol: str
    timestamp: datetime
    
    # Direction and confidence
    direction: str              # "BUY" or "SELL"
    confidence_level: SignalConfidence
    confluence_score: float     # 0-1
    
    # Entry
    entry_price: float
    entry_zone_high: float
    entry_zone_low: float
    entry_type: str             # "ORDER_BLOCK", "CONFLUENCE", "FVG", "LIQUIDITY_SWEEP"
    
    # Stops and targets
    stop_loss: float
    stop_loss_rationale: str
    take_profit_1: float       # First target (50% of position)
    take_profit_2: float       # Full exit
    atr_multiple_used: float
    
    # Risk management
    position: Optional[PositionMetrics] = None
    leverage_used: float = 1.0
    risk_amount: float = 0.0
    reward_amount: float = 0.0
    risk_reward_ratio: float = 0.0
    expected_value: float = 0.0
    
    # Analysis details
    timeframes_aligned: List[str] = None
    amd_phases: Dict[str, str] = None
    major_levels: List[Dict] = None  # Nearby order blocks, support/resistance
    liquidity_sweeps_probable: List[Dict] = None
    
    # Quality factors
    scalp_suitable: bool = False
    day_trade_suitable: bool = False
    swing_trade_suitable: bool = False
    
    # Rationale
    key_reasons: List[str] = None
    market_condition: str = ""  # "Trending", "Range", "Volatile", etc.
    risk_factors: List[str] = None
    
    def __post_init__(self):
        if self.timeframes_aligned is None:
            self.timeframes_aligned = []
        if self.amd_phases is None:
            self.amd_phases = {}
        if self.major_levels is None:
            self.major_levels = []
        if self.liquidity_sweeps_probable is None:
            self.liquidity_sweeps_probable = []
        if self.key_reasons is None:
            self.key_reasons = []
        if self.risk_factors is None:
            self.risk_factors = []
    
    @property
    def is_actionable(self) -> bool:
        """Check if signal meets minimum criteria to trade"""
        if self.confidence_level == SignalConfidence.INVALID:
            return False
        if self.confluence_score < 0.5:
            return False
        if self.risk_reward_ratio < 1.5:
            return False
        return True
    
    def to_dict(self) -> Dict:
        """Convert to JSON-serializable dict"""
        signal_dict = asdict(self)
        signal_dict['timestamp'] = self.timestamp.isoformat()
        signal_dict['confidence_level'] = self.confidence_level.name
        signal_dict['position'] = asdict(self.position) if self.position else None
        return signal_dict


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

class IntegratedSignalGenerator:
    """
    Generates complete trading signals by combining:
    1. Multi-timeframe structure analysis
    2. Sophisticated risk management
    3. Entry/exit optimization
    4. Trade suitability classification
    """
    
    def __init__(self, 
                 broker: str = "METATRADER",
                 asset_class: AssetClass = AssetClass.FOREX,
                 min_confluence: int = 2):
        
        self.analyzer = UnifiedMultiTimeframeAnalyzer()
        self.fee_calc = FeeCalculator(broker=broker, asset_class=asset_class)
        self.position_sizer = AdvancedPositionSizer(self.fee_calc)
        self.min_confluence = min_confluence
        self.asset_class = asset_class
    
    def generate_signals(self,
                        symbol: str,
                        data_dict: Dict[Timeframe, pd.DataFrame],
                        account: AccountMetrics,
                        atr_data: Optional[Dict[Timeframe, float]] = None) -> List[TradingSignal]:
        """
        Generate all trading signals for a symbol
        
        Args:
            symbol: Trading pair (e.g., "EURUSD")
            data_dict: {Timeframe.H4: df, Timeframe.H1: df, ...}
            account: Current account metrics
            atr_data: {Timeframe.H1: 0.0050, ...} for volatility-based stops
        
        Returns:
            List of TradingSignal objects, sorted by quality
        """
        
        signals = []
        
        # Run complete multi-timeframe analysis
        structure = self.analyzer.analyze(data_dict, symbol)
        
        if not structure.precise_entries:
            return signals  # No valid entries
        
        current_price = self._get_current_price(data_dict)
        
        # Generate signal for each entry point
        for entry_spec in structure.precise_entries:
            signal = self._create_signal(
                symbol=symbol,
                entry_spec=entry_spec,
                structure=structure,
                current_price=current_price,
                account=account,
                atr_data=atr_data
            )
            
            if signal and signal.is_actionable:
                signals.append(signal)
        
        # Sort by quality (confluence, RR, confidence)
        signals.sort(key=lambda s: (s.confluence_score, s.risk_reward_ratio), reverse=True)
        
        return signals
    
    def _create_signal(self,
                      symbol: str,
                      entry_spec: Dict,
                      structure: MultiTimeframeStructure,
                      current_price: float,
                      account: AccountMetrics,
                      atr_data: Optional[Dict] = None) -> Optional[TradingSignal]:
        """
        Create complete trading signal from entry specification
        """
        
        entry_price = entry_spec['price']
        direction = entry_spec['direction']
        
        if direction == "NEUTRAL":
            return None
        
        # Determine stop loss and take profit
        sl_rationale = ""
        tp_rationale = ""
        
        if direction == "BUY":
            # Find nearest support below entry for stop loss
            support_candidates = [level for level in structure.liquidity_levels.get(Timeframe.H1, [])
                                 if level.price < entry_price]
            if support_candidates:
                support = min(support_candidates, key=lambda x: entry_price - x.price)
                stop_loss = support.price * 0.99  # 1% below support
                sl_rationale = f"Below {support.origin} on {support.timeframe.name}"
            else:
                # Use ATR-based stop if available
                if atr_data and Timeframe.H1 in atr_data:
                    atr = atr_data[Timeframe.H1]
                    stop_loss = entry_price - (atr * 1.5)
                    sl_rationale = f"ATR-based (1.5x ATR)"
                else:
                    stop_loss = entry_price * 0.98
                    sl_rationale = "Default 2% below entry"
            
            # Find nearest resistance above entry for take profit
            resistance_candidates = [level for level in structure.liquidity_levels.get(Timeframe.H1, [])
                                    if level.price > entry_price]
            if resistance_candidates:
                resistance = min(resistance_candidates, key=lambda x: x.price - entry_price)
                take_profit = resistance.price * 1.01
                tp_rationale = f"At {resistance.origin} on {resistance.timeframe.name}"
            else:
                if atr_data and Timeframe.H1 in atr_data:
                    atr = atr_data[Timeframe.H1]
                    take_profit = entry_price + (atr * 3)
                    tp_rationale = f"ATR-based (3x ATR)"
                else:
                    take_profit = entry_price * 1.02
                    tp_rationale = "Default 2% above entry"
        
        else:  # SELL
            # Find nearest resistance above entry for stop loss
            resistance_candidates = [level for level in structure.liquidity_levels.get(Timeframe.H1, [])
                                    if level.price > entry_price]
            if resistance_candidates:
                resistance = min(resistance_candidates, key=lambda x: x.price - entry_price)
                stop_loss = resistance.price * 1.01
                sl_rationale = f"Above {resistance.origin} on {resistance.timeframe.name}"
            else:
                if atr_data and Timeframe.H1 in atr_data:
                    atr = atr_data[Timeframe.H1]
                    stop_loss = entry_price + (atr * 1.5)
                    sl_rationale = f"ATR-based (1.5x ATR)"
                else:
                    stop_loss = entry_price * 1.02
                    sl_rationale = "Default 2% above entry"
            
            # Find nearest support below entry for take profit
            support_candidates = [level for level in structure.liquidity_levels.get(Timeframe.H1, [])
                                 if level.price < entry_price]
            if support_candidates:
                support = min(support_candidates, key=lambda x: entry_price - x.price)
                take_profit = support.price * 0.99
                tp_rationale = f"At {support.origin} on {support.timeframe.name}"
            else:
                if atr_data and Timeframe.H1 in atr_data:
                    atr = atr_data[Timeframe.H1]
                    take_profit = entry_price - (atr * 3)
                    tp_rationale = f"ATR-based (3x ATR)"
                else:
                    take_profit = entry_price * 0.98
                    tp_rationale = "Default 2% below entry"
        
        # Calculate position size and risk management
        expected_duration = self._determine_trade_duration(entry_spec)
        
        position = self.position_sizer.calculate_position(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            account=account,
            setup_quality=entry_spec['strength'],
            direction=direction,
            expected_duration=expected_duration,
            max_leverage=self._get_max_leverage_for_symbol(symbol)
        )
        
        if not position:
            return None  # Position sizing failed
        
        # Calculate ATR multiple used
        atr_mult = 1.0
        if atr_data and Timeframe.H1 in atr_data:
            atr = atr_data[Timeframe.H1]
            if direction == "BUY":
                atr_mult = (entry_price - stop_loss) / atr
            else:
                atr_mult = (stop_loss - entry_price) / atr
        
        # Determine confidence level
        confluence_count = len(entry_spec['timeframes'])
        confluence_score = entry_spec['strength']
        confidence = self._calculate_confidence(confluence_count, confluence_score)
        
        # Classify for different trade styles
        scalp_suitable, day_suitable, swing_suitable = self._classify_trade_suitability(
            entry_spec, structure, expected_duration
        )
        
        # Identify probable liquidity sweeps
        probable_sweeps = self._identify_probable_sweeps(
            structure, entry_price, direction
        )
        
        # Get market condition
        market_condition = self._assess_market_condition(structure)
        
        # Risk factors
        risk_factors = self._identify_risk_factors(
            position, account, structure, symbol
        )
        
        # Create signal
        signal = TradingSignal(
            symbol=symbol,
            timestamp=datetime.now(),
            direction=direction,
            confidence_level=confidence,
            confluence_score=confluence_score,
            entry_price=entry_price,
            entry_zone_high=entry_spec.get('zone_high', entry_price * 1.001),
            entry_zone_low=entry_spec.get('zone_low', entry_price * 0.999),
            entry_type=entry_spec['type'],
            stop_loss=stop_loss,
            stop_loss_rationale=sl_rationale,
            take_profit_1=entry_price + (take_profit - entry_price) * 0.5 if direction == "BUY" 
                         else entry_price - (entry_price - take_profit) * 0.5,
            take_profit_2=take_profit,
            atr_multiple_used=atr_mult,
            position=position,
            leverage_used=position.leverage,
            risk_amount=position.net_risk_amount,
            reward_amount=position.net_reward_amount,
            risk_reward_ratio=position.risk_reward_ratio,
            expected_value=position.expected_value,
            timeframes_aligned=entry_spec['timeframes'],
            amd_phases={tf.name: phase.value for tf, phase in structure.amd_phases.items()},
            scalp_suitable=scalp_suitable,
            day_trade_suitable=day_suitable,
            swing_trade_suitable=swing_suitable,
            liquidity_sweeps_probable=probable_sweeps,
            market_condition=market_condition,
            risk_factors=risk_factors,
            key_reasons=entry_spec['rationale']
        )
        
        return signal
    
    def _get_current_price(self, data_dict: Dict[Timeframe, pd.DataFrame]) -> float:
        """Get most recent price from any timeframe"""
        for df in data_dict.values():
            if len(df) > 0:
                return df['close'].iloc[-1]
        return 0.0
    
    def _determine_trade_duration(self, entry_spec: Dict) -> TradeDuration:
        """Determine expected trade duration based on entry type"""
        timeframes = entry_spec['timeframes']
        
        if any(tf in timeframes for tf in ['M5', 'M15']):
            return TradeDuration.SCALP
        elif any(tf in timeframes for tf in ['M30', 'H1']):
            return TradeDuration.INTRADAY
        elif 'H4' in timeframes:
            return TradeDuration.SWING
        else:
            return TradeDuration.POSITION
    
    def _get_max_leverage_for_symbol(self, symbol: str) -> float:
        """Max leverage varies by symbol volatility"""
        # Metals are lower
        if "XAU" in symbol or "XAG" in symbol:
            return 30.0
        # JPY pairs can go higher
        elif "JPY" in symbol:
            return 75.0
        # Standard forex
        else:
            return 50.0
    
    def _calculate_confidence(self, confluence_count: int, confluence_score: float) -> SignalConfidence:
        """Determine confidence level from confluence"""
        combined = confluence_count * 0.5 + confluence_score * 2
        
        if combined >= 3.5:
            return SignalConfidence.VERY_HIGH
        elif combined >= 2.5:
            return SignalConfidence.HIGH
        elif combined >= 1.5:
            return SignalConfidence.MEDIUM
        elif combined >= 0.5:
            return SignalConfidence.LOW
        else:
            return SignalConfidence.INVALID
    
    def _classify_trade_suitability(self, entry_spec: Dict, structure: MultiTimeframeStructure,
                                   duration: TradeDuration) -> Tuple[bool, bool, bool]:
        """Classify if setup is suitable for scalping, day trading, or swing trading"""
        timeframes = entry_spec['timeframes']
        
        scalp = any(tf in ['M5', 'M15'] for tf in timeframes)
        day = any(tf in ['M30', 'H1', 'H4'] for tf in timeframes) and not scalp
        swing = 'DAILY' in timeframes or duration == TradeDuration.SWING
        
        return scalp, day, swing
    
    def _identify_probable_sweeps(self, structure: MultiTimeframeStructure,
                                 entry_price: float, direction: str) -> List[Dict]:
        """Identify probable liquidity sweeps"""
        sweeps = []
        
        for tf, liquidity_levels in structure.liquidity_levels.items():
            for level in liquidity_levels:
                is_sweep_target = level.is_likely_sweep_target(entry_price, direction)
                if is_sweep_target:
                    sweeps.append({
                        'price': level.price,
                        'origin': level.origin,
                        'timeframe': tf.name,
                        'type': level.type.value,
                        'probability': level.probability_of_sweep
                    })
        
        return sorted(sweeps, key=lambda x: x['probability'], reverse=True)[:3]
    
    def _assess_market_condition(self, structure: MultiTimeframeStructure) -> str:
        """Assess overall market condition"""
        amd_phases = list(structure.amd_phases.values())
        
        if any(phase == AMDPhase.ACCUMULATION for phase in amd_phases):
            return "Building Position"
        elif any(phase == AMDPhase.DISTRIBUTION for phase in amd_phases):
            return "Distributing"
        elif any(phase == AMDPhase.MANIPULATION for phase in amd_phases):
            return "Stop Hunting"
        else:
            return "Neutral"
    
    def _identify_risk_factors(self, position: PositionMetrics, account: AccountMetrics,
                              structure: MultiTimeframeStructure, symbol: str) -> List[str]:
        """Identify risk factors for this trade"""
        risks = []
        
        # Margin risk
        if position.margin_required / account.free_margin > 0.5:
            risks.append("High margin utilization (>50%)")
        
        # Fee impact
        if (position.entry_fee + position.exit_fee_short_term) > position.net_risk_amount * 0.1:
            risks.append("High fees relative to risk")
        
        # Swap risk for longer positions
        if position.hours_to_hold > 24 and position.estimated_swap_cost > position.net_risk_amount * 0.15:
            risks.append("High swap cost for duration")
        
        # Account risk
        if position.net_risk_amount > account.balance * 0.03:
            risks.append("Risk exceeds 3% of balance")
        
        # Volatility
        daily_phase = structure.amd_phases.get(Timeframe.DAILY)
        if daily_phase == AMDPhase.MANIPULATION:
            risks.append("Manipulation phase - higher volatility")
        
        return risks


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MANAGER (tracking active signals)
# ─────────────────────────────────────────────────────────────────────────────

class SignalManager:
    """
    Manages active trading signals
    Tracks entry, monitor conditions, exit rules
    """
    
    def __init__(self):
        self.active_signals: Dict[str, TradingSignal] = {}
        self.signal_history: List[TradingSignal] = []
    
    def add_signal(self, signal: TradingSignal):
        """Register a new signal"""
        self.active_signals[signal.symbol] = signal
    
    def should_exit_signal(self, signal: TradingSignal, current_price: float) -> Tuple[bool, str]:
        """Check if signal should be exited"""
        
        if signal.direction == "BUY":
            # Exit on stop loss
            if current_price <= signal.stop_loss:
                return True, "Stop loss hit"
            # Exit on take profit
            elif current_price >= signal.take_profit_2:
                return True, "Take profit hit"
        else:
            # Exit on stop loss
            if current_price >= signal.stop_loss:
                return True, "Stop loss hit"
            # Exit on take profit
            elif current_price <= signal.take_profit_2:
                return True, "Take profit hit"
        
        return False, ""
    
    def export_signals(self, filepath: str):
        """Export signals to JSON for record keeping"""
        signals_list = [s.to_dict() for s in self.signal_history]
        
        with open(filepath, 'w') as f:
            json.dump(signals_list, f, indent=2)


if __name__ == "__main__":
    print("integrated_trading_engine.py loaded")
    print("Ready for production signal generation")
