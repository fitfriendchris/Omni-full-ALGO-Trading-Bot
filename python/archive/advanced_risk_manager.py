"""
advanced_risk_manager.py — Sophisticated Position Sizing & Risk Management
Handles leverage, fees, swaps, slippage, and account balance dynamics
"""

from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum
import math


class AssetClass(Enum):
    """Different asset classes with different characteristics"""
    FOREX = "FOREX"
    METAL = "METAL"
    CRYPTO = "CRYPTO"
    INDEX = "INDEX"
    COMMODITY = "COMMODITY"


class TradeDuration(Enum):
    """Expected trade duration"""
    SCALP = "SCALP"        # < 5 minutes
    INTRADAY = "INTRADAY"  # < 4 hours
    SWING = "SWING"        # 1-5 days
    POSITION = "POSITION"  # 5+ days


@dataclass
class BrokerFees:
    """Broker fee structure"""
    spread_pips: float = 0.0        # Standard spread (in pips)
    commission_per_lot: float = 0.0 # Fixed commission per lot
    commission_pct: float = 0.0     # Percentage commission on notional
    
    # Swap points (per day, per lot)
    swap_long_pips: float = 0.0
    swap_short_pips: float = 0.0
    
    # Leverage available
    max_leverage: float = 100.0
    
    # Slippage assumptions
    slippage_pips: float = 0.5


@dataclass
class AccountMetrics:
    """Current account state"""
    balance: float              # Account balance in base currency
    equity: float              # Current equity (balance + open P&L)
    used_margin: float         # Used margin from open positions
    free_margin: float         # Available margin for new trades
    margin_level: float        # Used margin / Free margin %
    
    daily_profit: float = 0.0  # P&L for current day
    daily_trades: int = 0      # Number of trades today
    win_rate: float = 0.5      # Historical win rate
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    
    # Risk metrics
    max_daily_loss_pct: float = 5.0   # Maximum allowed daily loss %
    max_daily_loss: float = 0.0       # Amount (calculated from max_daily_loss_pct)
    
    def __post_init__(self):
        self.max_daily_loss = self.balance * (self.max_daily_loss_pct / 100)
        self.free_margin = self.balance - self.used_margin


@dataclass
class PositionMetrics:
    """Metrics for a single position"""
    symbol: str
    direction: str              # "BUY" or "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    
    quantity: float            # Position size in units
    risk_amount: float         # Amount at risk (in base currency)
    reward_amount: float       # Potential profit (in base currency)
    
    # Leverage and margin
    leverage: float
    margin_required: float     # Margin needed for this position
    
    # Costs
    entry_fee: float          # Cost to enter (spread, commission, slippage)
    exit_fee_long_term: float  # Cost if held long-term (swaps)
    exit_fee_short_term: float # Cost if closed today (spread, commission)
    
    # Risk/Reward
    risk_reward_ratio: float
    expected_value: float     # Risk * Win% - Loss * (1-Win%)
    
    # Duration
    expected_duration: TradeDuration
    hours_to_hold: float
    estimated_swap_cost: float
    
    @property
    def total_cost_worst_case(self) -> float:
        """All fees if position goes max against us"""
        return self.entry_fee + self.exit_fee_long_term
    
    @property
    def net_risk_amount(self) -> float:
        """Actual risk considering fees"""
        return self.risk_amount + self.entry_fee + self.exit_fee_short_term
    
    @property
    def net_reward_amount(self) -> float:
        """Actual reward considering fees"""
        return self.reward_amount - self.entry_fee - self.exit_fee_short_term


# ─────────────────────────────────────────────────────────────────────────────
# FEE AND COST CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────

class FeeCalculator:
    """Calculate all fees, slippage, and swap costs"""
    
    # Broker fee presets
    BROKER_FEES = {
        "IC_MARKETS": {
            "FOREX": BrokerFees(spread_pips=0.0, commission_pct=0.0006, max_leverage=500, slippage_pips=0.3),
            "METAL": BrokerFees(spread_pips=0.1, commission_pct=0.0006, max_leverage=100, slippage_pips=0.5),
        },
        "OANDA": {
            "FOREX": BrokerFees(spread_pips=0.3, commission_pct=0.0, max_leverage=50, slippage_pips=0.5),
            "METAL": BrokerFees(spread_pips=2.0, commission_pct=0.0, max_leverage=20, slippage_pips=1.0),
        },
        "INTERACTIVE_BROKERS": {
            "FOREX": BrokerFees(spread_pips=0.2, commission_pct=0.0002, max_leverage=50, slippage_pips=0.3),
            "METAL": BrokerFees(spread_pips=0.5, commission_pct=0.0002, max_leverage=30, slippage_pips=0.5),
        },
        "METATRADER": {
            "FOREX": BrokerFees(spread_pips=1.0, commission_per_lot=10.0, max_leverage=100, slippage_pips=1.0),
            "METAL": BrokerFees(spread_pips=2.0, commission_per_lot=20.0, max_leverage=50, slippage_pips=1.5),
        }
    }
    
    # Swap points (pips per day) - these vary by broker/symbol
    SWAP_RATES = {
        ("EURUSD", "LONG"): -0.1,   # Paying interest on long EUR
        ("EURUSD", "SHORT"): -0.2,  # Earning interest on short USD
        ("GBPUSD", "LONG"): 0.1,
        ("GBPUSD", "SHORT"): -0.3,
        ("USDJPY", "LONG"): -1.0,   # High swap due to rate differential
        ("USDJPY", "SHORT"): 2.0,
        ("AUDUSD", "LONG"): 0.5,
        ("AUDUSD", "SHORT"): -1.5,
        ("XAUUSD", "LONG"): -0.3,   # Storage costs on long gold
        ("XAUUSD", "SHORT"): 0.1,
        ("XAGUSD", "LONG"): -0.2,   # Storage costs on long silver
        ("XAGUSD", "SHORT"): 0.1,
    }
    
    def __init__(self, broker: str = "METATRADER", asset_class: AssetClass = AssetClass.FOREX):
        self.broker = broker
        self.asset_class = asset_class
        self.fees = self._get_fee_structure()
    
    def _get_fee_structure(self) -> BrokerFees:
        """Get fee structure for current broker and asset"""
        asset_name = "FOREX"
        if self.asset_class == AssetClass.METAL:
            asset_name = "METAL"
        elif self.asset_class == AssetClass.CRYPTO:
            asset_name = "CRYPTO"
        
        if self.broker not in self.BROKER_FEES:
            # Default to MetaTrader if broker not in presets
            self.broker = "METATRADER"
        
        if asset_name not in self.BROKER_FEES[self.broker]:
            asset_name = "FOREX"  # Fallback
        
        return self.BROKER_FEES[self.broker][asset_name]
    
    def calculate_entry_cost(self, symbol: str, price: float, quantity: float) -> float:
        """
        Calculate total cost to enter position
        Includes: spread, commission, slippage
        """
        # Spread cost
        spread_cost = quantity * price * (self.fees.spread_pips / 10000)
        
        # Commission cost
        if self.fees.commission_per_lot > 0:
            # commission_per_lot is typically per standard lot (100k units)
            standard_lot = 100000
            num_lots = quantity / standard_lot
            commission = num_lots * self.fees.commission_per_lot
        else:
            commission = quantity * price * self.fees.commission_pct
        
        # Slippage cost (on top of spread)
        slippage = quantity * price * (self.fees.slippage_pips / 10000)
        
        total = spread_cost + commission + slippage
        return abs(total)
    
    def calculate_exit_cost(self, symbol: str, price: float, quantity: float,
                           duration_hours: float) -> Tuple[float, float]:
        """
        Calculate exit costs
        Returns: (short_term_cost, long_term_cost)
        
        Short-term: spread + commission + slippage (if closed today)
        Long-term: includes swap costs if held overnight
        """
        # Immediate exit cost (spread, commission, slippage)
        spread_cost = quantity * price * (self.fees.spread_pips / 10000)
        
        if self.fees.commission_per_lot > 0:
            standard_lot = 100000
            num_lots = quantity / standard_lot
            commission = num_lots * self.fees.commission_per_lot
        else:
            commission = quantity * price * self.fees.commission_pct
        
        slippage = quantity * price * (self.fees.slippage_pips / 10000)
        short_term = spread_cost + commission + slippage
        
        # Long-term exit cost (includes swaps)
        # Swap is charged daily at 3x on Wednesday
        days_held = duration_hours / 24
        wednesday_multiplier = 3 if self._is_wednesday() else 1
        
        # Get swap rate for symbol
        swap_key = (symbol, "LONG")  # Will be dynamic based on direction
        daily_swap = self.SWAP_RATES.get(swap_key, 0.0)  # In pips
        
        # Swap cost in currency
        swap_cost = quantity * price * (daily_swap / 10000) * days_held * wednesday_multiplier
        
        long_term = short_term + abs(swap_cost)
        
        return abs(short_term), abs(long_term)
    
    def calculate_swap_cost(self, symbol: str, quantity: float, price: float,
                           direction: str, days: int = 1) -> float:
        """
        Calculate swap cost for holding position
        Swaps are charged daily, 3x on Wednesdays
        """
        swap_key = (symbol, direction)
        daily_swap_pips = self.SWAP_RATES.get(swap_key, 0.0)
        
        # Wednesday multiplier
        wednesday_mult = 3 if self._is_wednesday() else 1
        
        swap_cost = quantity * price * (daily_swap_pips / 10000) * days * wednesday_mult
        return abs(swap_cost)
    
    def _is_wednesday(self) -> bool:
        """Check if today is Wednesday (when swaps are 3x)"""
        return datetime.now().weekday() == 2


# ─────────────────────────────────────────────────────────────────────────────
# POSITION SIZER
# ─────────────────────────────────────────────────────────────────────────────

class AdvancedPositionSizer:
    """
    Calculates position size based on:
    - Account balance and equity
    - Risk tolerance and win rate
    - Setup quality and confluence
    - Leverage availability
    - Fee and swap impact
    - Streak adjustment (reduce after losses, increase after wins)
    """
    
    def __init__(self, fee_calc: FeeCalculator):
        self.fee_calc = fee_calc
    
    def calculate_position(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        account: AccountMetrics,
        setup_quality: float = 0.7,  # 0-1 confidence/confluence
        direction: str = "BUY",
        expected_duration: TradeDuration = TradeDuration.INTRADAY,
        max_leverage: float = 50.0,
    ) -> Optional[PositionMetrics]:
        """
        Calculate optimal position size with all constraints
        
        Args:
            symbol: Trading pair
            entry_price: Entry price
            stop_loss: Stop loss price
            take_profit: Take profit price
            account: Current account metrics
            setup_quality: Confluence/confidence score (0-1)
            direction: "BUY" or "SELL"
            expected_duration: How long we expect to hold
            max_leverage: Maximum leverage to use (varies by setup quality)
        
        Returns:
            PositionMetrics or None if setup doesn't meet criteria
        """
        
        # Risk distance in pips
        if direction == "BUY":
            risk_pips = (entry_price - stop_loss) * 10000
            reward_pips = (take_profit - entry_price) * 10000
        else:
            risk_pips = (stop_loss - entry_price) * 10000
            reward_pips = (entry_price - take_profit) * 10000
        
        # Sanity checks
        if risk_pips <= 0 or reward_pips <= 0:
            return None
        
        # Calculate risk/reward ratio
        rr_ratio = reward_pips / risk_pips
        
        # Risk amount (% of account that we're willing to lose)
        base_risk_pct = self._calculate_risk_percentage(account, setup_quality)
        risk_amount = account.balance * (base_risk_pct / 100)
        
        # Determine leverage based on setup quality and account conditions
        recommended_leverage = self._calculate_leverage(
            account, setup_quality, rr_ratio, max_leverage
        )
        
        # Calculate position size
        # Position size = Risk amount / (Risk pips * Point value)
        # For forex 1 lot = 100k units, point value = 10 for most pairs
        
        point_value = 10.0 if "JPY" not in symbol else 100.0  # JPY pairs
        pip_value_per_unit = point_value * 0.0001
        
        position_size = risk_amount / (risk_pips * pip_value_per_unit)
        
        # Apply leverage to position size
        max_position_size = (account.free_margin * recommended_leverage) / (entry_price)
        position_size = min(position_size, max_position_size)
        
        # Apply streak reduction/increase
        if account.consecutive_losses >= 3:
            position_size *= 0.7  # Reduce after 3 losses
        elif account.consecutive_wins >= 3:
            position_size *= 1.2  # Increase after 3 wins
        
        # Calculate actual risk/reward amounts
        actual_risk = position_size * risk_pips * pip_value_per_unit
        actual_reward = position_size * reward_pips * pip_value_per_unit
        
        # Calculate fees
        entry_fee = self.fee_calc.calculate_entry_cost(symbol, entry_price, position_size)
        
        # Duration-based fees
        hours_to_hold = self._estimate_hours(expected_duration)
        exit_fee_short, exit_fee_long = self.fee_calc.calculate_exit_cost(
            symbol, entry_price, position_size, hours_to_hold
        )
        
        # Margin required
        notional_value = position_size * entry_price
        margin_required = notional_value / recommended_leverage
        
        # Check if we have enough margin
        if margin_required > account.free_margin:
            # Reduce position size to fit margin
            position_size = (account.free_margin * 0.95) * recommended_leverage / entry_price
            actual_risk = position_size * risk_pips * pip_value_per_unit
            actual_reward = position_size * reward_pips * pip_value_per_unit
            margin_required = position_size * entry_price / recommended_leverage
        
        # Final sanity checks
        if position_size < 0.01:  # Too small
            return None
        
        # Create position metrics
        position = PositionMetrics(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quantity=position_size,
            risk_amount=actual_risk,
            reward_amount=actual_reward,
            leverage=recommended_leverage,
            margin_required=margin_required,
            entry_fee=entry_fee,
            exit_fee_long_term=exit_fee_long,
            exit_fee_short_term=exit_fee_short,
            risk_reward_ratio=rr_ratio,
            expected_value=self._calculate_expected_value(actual_risk, actual_reward, account.win_rate),
            expected_duration=expected_duration,
            hours_to_hold=hours_to_hold,
            estimated_swap_cost=exit_fee_long - exit_fee_short,
        )
        
        return position
    
    def _calculate_risk_percentage(self, account: AccountMetrics, 
                                  setup_quality: float) -> float:
        """
        Determine risk % based on account state and setup quality
        
        Rules:
        - Base: 2% for good setups (70%+ confidence)
        - Reduce to 1% for lower quality setups
        - Reduce to 0.5% after consecutive losses
        - Increase to 3% after consecutive wins (max)
        - Consider daily loss limit
        """
        base_risk = 2.0
        
        # Quality adjustment
        if setup_quality < 0.6:
            base_risk = 1.0
        elif setup_quality > 0.8:
            base_risk = 2.5
        
        # Streak adjustment
        if account.consecutive_losses >= 2:
            base_risk *= 0.5
        elif account.consecutive_wins >= 3:
            base_risk = min(base_risk * 1.5, 3.0)
        
        # Daily loss limit check
        remaining_daily_loss = account.max_daily_loss - abs(account.daily_profit)
        if remaining_daily_loss < 0:
            return 0.0  # Stop trading
        
        max_risk_allowed = (remaining_daily_loss / account.balance) * 100
        base_risk = min(base_risk, max_risk_allowed)
        
        # Floor: minimum 0.5%
        return max(base_risk, 0.5)
    
    def _calculate_leverage(self, account: AccountMetrics, setup_quality: float,
                           rr_ratio: float, max_available: float) -> float:
        """
        Determine leverage based on setup quality and risk/reward
        
        Rules:
        - High quality + good RR + high margin = higher leverage
        - Low quality or poor RR = lower leverage
        - High margin level (>80%) = reduce leverage
        """
        
        base_leverage = 20.0
        
        # Setup quality
        if setup_quality > 0.8:
            base_leverage = 30.0
        elif setup_quality < 0.6:
            base_leverage = 10.0
        
        # RR ratio (better RR can handle more leverage)
        if rr_ratio > 2.0:
            base_leverage *= 1.3
        elif rr_ratio < 1.5:
            base_leverage *= 0.7
        
        # Margin utilization (reduce if already high)
        margin_level = account.used_margin / account.balance * 100
        if margin_level > 80:
            base_leverage *= 0.5
        elif margin_level > 50:
            base_leverage *= 0.7
        
        # Apply maximum
        return min(base_leverage, max_available)
    
    def _estimate_hours(self, duration: TradeDuration) -> float:
        """Estimate how many hours position will be held"""
        estimates = {
            TradeDuration.SCALP: 0.1,      # 6 minutes
            TradeDuration.INTRADAY: 4.0,   # 4 hours
            TradeDuration.SWING: 24.0,     # 1 day
            TradeDuration.POSITION: 120.0, # 5 days
        }
        return estimates.get(duration, 4.0)
    
    def _calculate_expected_value(self, risk_amount: float, reward_amount: float,
                                 win_rate: float) -> float:
        """
        Calculate expected value of trade
        EV = (Win% * Reward) - ((1 - Win%) * Risk)
        """
        win_prob = win_rate
        loss_prob = 1 - win_rate
        
        ev = (win_prob * reward_amount) - (loss_prob * risk_amount)
        return ev


# ─────────────────────────────────────────────────────────────────────────────
# POSITION MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class DynamicPositionManager:
    """
    Manages position lifecycle with dynamic adjustments
    - Scales positions based on performance
    - Adjusts stops/targets based on volatility
    - Closes losers after volatility spikes
    """
    
    def __init__(self, position_sizer: AdvancedPositionSizer):
        self.position_sizer = position_sizer
    
    def should_reduce_position(self, position: PositionMetrics, 
                              current_price: float, current_volatility: float,
                              account: AccountMetrics) -> Tuple[bool, str]:
        """
        Determine if position should be reduced or closed
        
        Returns: (should_reduce, reason)
        """
        
        # If in profit by 50% of potential, close half
        distance_to_tp = abs(position.take_profit - current_price)
        total_distance = abs(position.take_profit - position.entry_price)
        
        in_profit = (position.direction == 'BUY' and current_price > position.entry_price) or \
                (position.direction == 'SELL' and current_price < position.entry_price)
        if distance_to_tp < total_distance * 0.5 and in_profit:
            return True, "In profit 50% - close half"
        
        # If volatility spikes above 2x normal, reduce size
        normal_volatility = total_distance / position.entry_price
        if current_volatility > normal_volatility * 2:
            return True, f"Volatility spike: {current_volatility:.3%}"
        
        # If we hit max daily loss limit, stop all new trades
        if account.daily_profit < -account.max_daily_loss:
            return True, "Daily loss limit hit"
        
        return False, ""
    
    def calculate_atr_adjusted_stops(self, entry: float, atr: float, 
                                    direction: str, atr_multiple: float = 1.5) -> Dict:
        """
        Adjust stop loss based on ATR (volatility)
        """
        if direction == "BUY":
            sl = entry - (atr * atr_multiple)
            tp_tight = entry + (atr * atr_multiple)
            tp_normal = entry + (atr * atr_multiple * 2)
        else:
            sl = entry + (atr * atr_multiple)
            tp_tight = entry - (atr * atr_multiple)
            tp_normal = entry - (atr * atr_multiple * 2)
        
        return {
            'stop_loss': sl,
            'take_profit_tight': tp_tight,
            'take_profit_normal': tp_normal,
            'atr_multiple_used': atr_multiple
        }


if __name__ == "__main__":
    print("advanced_risk_manager.py loaded")
    
    # Example usage
    fee_calc = FeeCalculator(broker="METATRADER", asset_class=AssetClass.FOREX)
    sizer = AdvancedPositionSizer(fee_calc)
    
    # Example account
    account = AccountMetrics(
        balance=10000,
        equity=10000,
        used_margin=2000,
        free_margin=8000,
        win_rate=0.55,
        consecutive_wins=1
    )
    
    # Example position
    pos = sizer.calculate_position(
        symbol="EURUSD",
        entry_price=1.0850,
        stop_loss=1.0800,
        take_profit=1.0950,
        account=account,
        setup_quality=0.75,
        direction="BUY",
        expected_duration=TradeDuration.INTRADAY
    )
    
    if pos:
        print(f"\nPosition: {pos.symbol}")
        print(f"  Size: {pos.quantity:.2f} units")
        print(f"  Leverage: {pos.leverage:.1f}x")
        print(f"  Risk: ${pos.net_risk_amount:.2f}")
        print(f"  Reward: ${pos.net_reward_amount:.2f}")
        print(f"  RR Ratio: {pos.risk_reward_ratio:.2f}")
        print(f"  Entry Fee: ${pos.entry_fee:.2f}")
        print(f"  Expected Value: ${pos.expected_value:.2f}")
