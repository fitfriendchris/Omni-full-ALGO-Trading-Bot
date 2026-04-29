"""
QUICK REFERENCE GUIDE
Multi-Timeframe Auto Trading System
"""

# ═════════════════════════════════════════════════════════════════════════════
# 1-MINUTE SETUP
# ═════════════════════════════════════════════════════════════════════════════

"""
1. Download all 5 files into your trading folder
2. Install dependencies: pip install pandas numpy
3. Test with: python INTEGRATION_GUIDE.py
4. See example output confirming system works
"""

# ═════════════════════════════════════════════════════════════════════════════
# KEY MODULES & CLASSES
# ═════════════════════════════════════════════════════════════════════════════

# Module 1: ADVANCED_STRUCTURE_ANALYZER
# ──────────────────────────────────────────────────────────────────────────
from advanced_structure_analyzer import (
    UnifiedMultiTimeframeAnalyzer,      # Main analyzer
    Timeframe,                           # H4, H1, M15, etc.
    AMDPhase,                            # ACC, MAN, DIS phases
    OrderBlockSize,                      # MAJOR, MINOR, MITIGATED
    MultiTimeframeStructure              # Complete analysis result
)

# Module 2: ADVANCED_RISK_MANAGER
# ──────────────────────────────────────────────────────────────────────────
from advanced_risk_manager import (
    FeeCalculator,                       # Calculate fees/swaps
    AdvancedPositionSizer,               # Calculate position size
    AccountMetrics,                      # Account state
    PositionMetrics,                     # Trade parameters
    AssetClass,                          # FOREX, METAL, etc.
    TradeDuration                        # SCALP, INTRADAY, SWING
)

# Module 3: INTEGRATED_TRADING_ENGINE
# ──────────────────────────────────────────────────────────────────────────
from integrated_trading_engine import (
    IntegratedSignalGenerator,           # Generate signals
    TradingSignal,                       # Actionable signal
    SignalConfidence,                    # VERY_HIGH, HIGH, etc.
    SignalManager                        # Manage active signals
)


# ═════════════════════════════════════════════════════════════════════════════
# COMMON WORKFLOWS
# ═════════════════════════════════════════════════════════════════════════════

# WORKFLOW 1: Analyze a single symbol
# ────────────────────────────────────────────────────────────────────────────

def analyze_symbol():
    import pandas as pd
    
    # Step 1: Prepare data (get from MT5 or CSV)
    data_dict = {
        Timeframe.DAILY: pd.read_csv('eurusd_daily.csv'),
        Timeframe.H4: pd.read_csv('eurusd_h4.csv'),
        Timeframe.H1: pd.read_csv('eurusd_h1.csv'),
    }
    
    # Step 2: Analyze structures
    analyzer = UnifiedMultiTimeframeAnalyzer()
    structure = analyzer.analyze(data_dict, 'EURUSD')
    
    # Step 3: See results
    print(f"Order blocks found: {len(structure.order_blocks)}")
    print(f"Confluence zones: {len(structure.confluence_zones)}")
    print(f"Primary direction: {structure.primary_direction}")
    print(f"AMD phases: {structure.amd_phases}")


# WORKFLOW 2: Generate trading signals
# ────────────────────────────────────────────────────────────────────────────

def generate_signals():
    # Step 1: Prepare
    signal_gen = IntegratedSignalGenerator(broker="METATRADER")
    
    account = AccountMetrics(
        balance=10000,
        equity=10500,
        used_margin=2000,
        free_margin=8500,
        win_rate=0.55,
    )
    
    # Step 2: Generate
    signals = signal_gen.generate_signals(
        symbol='EURUSD',
        data_dict={
            Timeframe.DAILY: daily_df,
            Timeframe.H4: h4_df,
            Timeframe.H1: h1_df,
        },
        account=account
    )
    
    # Step 3: Use best signal
    if signals and signals[0].is_actionable:
        signal = signals[0]
        print(f"Direction: {signal.direction}")
        print(f"Entry: {signal.entry_price}")
        print(f"Stop: {signal.stop_loss}")
        print(f"TP1: {signal.take_profit_1}")
        print(f"TP2: {signal.take_profit_2}")
        print(f"Confidence: {signal.confidence_level.name}")
        print(f"Position size: {signal.position.quantity}")
        print(f"Risk: ${signal.risk_amount:.2f}")
        print(f"Reward: ${signal.reward_amount:.2f}")


# WORKFLOW 3: Calculate position size with risk management
# ────────────────────────────────────────────────────────────────────────────

def calculate_position():
    fee_calc = FeeCalculator(broker="METATRADER", asset_class=AssetClass.FOREX)
    sizer = AdvancedPositionSizer(fee_calc)
    
    account = AccountMetrics(
        balance=10000,
        equity=10500,
        used_margin=2000,
        free_margin=8500,
        win_rate=0.55,
    )
    
    position = sizer.calculate_position(
        symbol='EURUSD',
        entry_price=1.0850,
        stop_loss=1.0800,
        take_profit=1.0950,
        account=account,
        setup_quality=0.75,  # 75% confidence
        direction='BUY',
        expected_duration=TradeDuration.INTRADAY,
        max_leverage=50.0
    )
    
    if position:
        print(f"Size: {position.quantity:.2f} units")
        print(f"Leverage: {position.leverage:.1f}x")
        print(f"Entry fee: ${position.entry_fee:.2f}")
        print(f"Gross risk: ${position.risk_amount:.2f}")
        print(f"Net risk (with fees): ${position.net_risk_amount:.2f}")
        print(f"Gross reward: ${position.reward_amount:.2f}")
        print(f"Expected value: ${position.expected_value:.2f}")


# WORKFLOW 4: Track and manage active trades
# ────────────────────────────────────────────────────────────────────────────

def manage_trades():
    manager = SignalManager()
    
    # Add signal
    signal = TradingSignal(...)  # from generate_signals()
    manager.add_signal(signal)
    
    # Check if should exit
    current_price = 1.0875
    should_exit, reason = manager.should_exit_signal(signal, current_price)
    
    if should_exit:
        print(f"Exit signal: {reason}")
        # Close position
    
    # Export for record keeping
    manager.export_signals('signals_history.json')


# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION CHEAT SHEET
# ═════════════════════════════════════════════════════════════════════════════

"""
BROKER FEES (in FeeCalculator):
─────────────────────────────────
METATRADER:
  spread: 1.0 pip
  commission: $10/lot
  max_leverage: 100x

IC_MARKETS:
  spread: 0.0 pip
  commission: 0.06% (better than MT5)
  max_leverage: 500x

OANDA:
  spread: 0.3 pip
  commission: 0% (built into spread)
  max_leverage: 50x

INTERACTIVE_BROKERS:
  spread: 0.2 pip
  commission: 0.02%
  max_leverage: 50x


TIMEFRAME HIERARCHY:
──────────────────
For best results, use:
  Timeframe.DAILY      # Overall structure
  Timeframe.H4         # Swing entry confirmation
  Timeframe.H1         # Intraday tactical
  Timeframe.M15        # Scalp precision

Skip:
  Timeframe.M5         # Too much noise, not enough data
  Timeframe.WEEKLY     # Too slow, limited data


RISK MANAGEMENT DEFAULTS:
─────────────────────────
Base risk: 2.0% per trade
Min risk: 0.5% (after losses)
Max risk: 3.0% (after wins)
Daily loss limit: 5% of balance
Max drawdown: 10% from peak
Max concurrent trades: 3
Min RR ratio: 1.5
Min confluence: 2 timeframes


SIGNAL CONFIDENCE THRESHOLDS:
──────────────────────────────
VERY_HIGH (4):  4+ TF aligned, major confluence, >80% quality
              → Can use 50x leverage
              
HIGH (3):       3 TF aligned, good confluence, 70-80% quality
              → Use 30x leverage
              
MEDIUM (2):     2 TF aligned, adequate confluence, 60-70% quality
              → Use 20x leverage
              
LOW (1):        1 TF aligned, weak setup, <60% quality
              → Use 10x leverage or skip
              
INVALID (0):    <50% confluence, poor structure
              → Do not trade


TRADE SUITABILITY:
──────────────────
SCALP:    2+ lower TF aligned (M5, M15, M30)
          Risk: $10-50 per trade
          Duration: <5 minutes
          Leverage: 50-100x
          
INTRADAY: 2+ TF aligned (H1, H4)
          Risk: $50-200 per trade
          Duration: 1-4 hours
          Leverage: 20-50x
          
SWING:    Daily + H4 aligned
          Risk: $100-500 per trade
          Duration: 1-5 days
          Leverage: 10-20x
          Account: $10k+ recommended


AMD PHASE TRADING RULES:
──────────────────────
ACCUMULATION (Building):
  ✅ Trade breakouts (tight stop)
  ✅ Trade FVG fills
  ❌ Avoid counter-trades (likely to fail)
  └─ Action: Only long positions
  
MANIPULATION (Stop Hunts):
  ⚠️  Use wider stops (2x normal)
  ⚠️  Reduce position size
  ✅ Trade bounces OFF wicks
  ❌ Don't chase the wick
  └─ Action: Defensive, scaled positions
  
DISTRIBUTION (Selling):
  ❌ Avoid long entries at resistance
  ✅ Trade short at resistance
  ✅ Fade rallies
  └─ Action: Bias shorts, tighter stops
"""


# ═════════════════════════════════════════════════════════════════════════════
# SIGNAL INTERPRETATION
# ═════════════════════════════════════════════════════════════════════════════

"""
High-Quality Signal Example:
────────────────────────────

symbol:              'EURUSD'
direction:           'BUY'
confidence_level:    'VERY_HIGH' (✅ High probability)
confluence_score:    0.85 (✅ 4 timeframes aligned)
timeframes_aligned:  ['DAILY', 'H4', 'H1', 'M15']
entry_price:         1.0850
stop_loss:           1.0800
take_profit_1:       1.0885 (50% exit here)
take_profit_2:       1.0920
risk_reward_ratio:   2.0 (✅ Good ratio)
position.quantity:   0.5 lot
position.leverage:   30.0x
risk_amount:         $250
reward_amount:       $500
expected_value:      +$175 (positive!)
scalp_suitable:      True  (Can scalp at TP1)
day_trade_suitable:  True
swing_trade_suitable: False (too intraday)
key_reasons:         [
  'Daily MAJOR OB confirmed',
  '4H+ confluence (3 levels)',
  'Liquidity sweep to 1.0900 likely',
  'Strong AMD accumulation phase'
]
risk_factors:        []  (✅ No warnings)

INTERPRETATION: This is a STRONG buy signal!
└─ 4 timeframes perfectly aligned
└─ Confidence very high (85%)
└─ Risk/reward favorable (2.0)
└─ Expected value positive (+$175)
└─ No red flags
└─ Suitable for immediate entry


Weak Signal Example:
────────────────────

symbol:              'GBPUSD'
direction:           'SELL'
confidence_level:    'MEDIUM' (⚠️  Medium probability)
confluence_score:    0.50 (⚠️  Only 2 timeframes aligned)
timeframes_aligned:  ['H1', 'M15']  (Missing Daily/H4)
entry_price:         1.2700
stop_loss:           1.2750
take_profit_1:       1.2680
take_profit_2:       1.2650
risk_reward_ratio:   1.2 (⚠️  Too small)
position.quantity:   0.25 lot
position.leverage:   15.0x
risk_amount:         $125
reward_amount:       $125
expected_value:      +$0 (break-even at best!)
scalp_suitable:      True
day_trade_suitable:  False
swing_trade_suitable: False
key_reasons:         [
  'H1 minor OB only',
  'Missing higher TF confluence'
]
risk_factors:        [
  'Low RR ratio (1.2:1)',
  'Fees will reduce net profit',
  'Only intraday suitable'
]

INTERPRETATION: This is a WEAK signal - SKIP IT!
└─ Only 2 timeframes (missing Daily structure)
└─ Medium confidence only
└─ Poor RR ratio (1.2 too small)
└─ Expected value ~$0 (fees will kill it)
└─ Too risky for position size
└─ Only scalp suitable, not ideal
└─ Recommendation: Wait for better setup
"""


# ═════════════════════════════════════════════════════════════════════════════
# TROUBLESHOOTING QUICK FIXES
# ═════════════════════════════════════════════════════════════════════════════

"""
Issue: No signals generated
───────────────────────────
Fix 1: Check data quality
  └─ Ensure each dataframe has 200+ rows
  └─ Verify OHLCV columns exist: open, high, low, close, volume
  └─ Check for data gaps (missing time periods)

Fix 2: Check timeframe overlap
  └─ Minimum 3 timeframes required for confluence
  └─ Use [DAILY, H4, H1] at minimum
  └─ Can add M15 for intraday

Fix 3: Relax confluence requirement
  └─ Change min_confluence from 3 to 2
  └─ Reduce confidence_threshold from HIGH to MEDIUM
  └─ Lower setup_quality threshold


Issue: Signals fail in execution
────────────────────────────────
Fix 1: Slippage worse than expected?
  └─ Increase slippage_pips in FeeCalculator (0.5 → 1.0)
  └─ This will reduce position size automatically
  └─ Accounts for market impact

Fix 2: Stop losses too tight?
  └─ Use ATR-based stops (1.5x ATR for SL)
  └─ Skip high volatility trades (AMD.MANIPULATION)
  └─ Increase SL buffer by 1-2 pips

Fix 3: Position size too large?
  └─ Reduce max_leverage from 50 to 30
  └─ Multiply all positions by 0.7
  └─ Lower setup_quality threshold


Issue: Win rate is below 50%
───────────────────────────
Fix 1: Reduce noise
  └─ Only trade VERY_HIGH/HIGH confidence
  └─ Require 3+ TF confluence minimum
  └─ Skip MANIPULATION phase trades

Fix 2: Improve entries
  └─ Wait for pullback into OB (don't chase)
  └─ Enter at 2x confluence zones only
  └─ Use limit orders, not market orders

Fix 3: Tighter exits
  └─ Lower TP1 (exit 50% earlier)
  └─ Use trailing stops on winners
  └─ Close if volatility explodes (SL hit risk)
"""


# ═════════════════════════════════════════════════════════════════════════════
# COPY-PASTE CODE EXAMPLES
# ═════════════════════════════════════════════════════════════════════════════

# Quick signal generator (minimal setup)
# ────────────────────────────────────────────────────────────────────────────
def quick_signal(symbol, h4_df, h1_df, h15_df, balance):
    from integrated_trading_engine import IntegratedSignalGenerator
    from advanced_risk_manager import AccountMetrics
    from advanced_structure_analyzer import Timeframe
    
    gen = IntegratedSignalGenerator()
    acc = AccountMetrics(balance=balance, equity=balance, free_margin=balance)
    
    signals = gen.generate_signals(
        symbol,
        {Timeframe.H4: h4_df, Timeframe.H1: h1_df, Timeframe.M15: h15_df},
        acc
    )
    
    return signals[0] if signals else None


# Quick fee calculator (copy specific broker settings)
# ────────────────────────────────────────────────────────────────────────────
from advanced_risk_manager import FeeCalculator, AssetClass

# For IC Markets (tight spreads, low fees)
fee_calc = FeeCalculator(broker="IC_MARKETS", asset_class=AssetClass.FOREX)

# For OANDA (high spreads but simple)
fee_calc = FeeCalculator(broker="OANDA", asset_class=AssetClass.FOREX)

# For MetaTrader (standard)
fee_calc = FeeCalculator(broker="METATRADER", asset_class=AssetClass.FOREX)


# Quick position sizing (no parameters)
# ────────────────────────────────────────────────────────────────────────────
def quick_size(symbol, entry, sl, tp, account_balance):
    from advanced_risk_manager import AdvancedPositionSizer, FeeCalculator, AccountMetrics, AssetClass
    
    sizer = AdvancedPositionSizer(FeeCalculator())
    acc = AccountMetrics(balance=account_balance, equity=account_balance, free_margin=account_balance*0.9)
    
    pos = sizer.calculate_position(symbol, entry, sl, tp, acc, setup_quality=0.7)
    return pos if pos else None


# ═════════════════════════════════════════════════════════════════════════════
# PERFORMANCE BENCHMARKS
# ═════════════════════════════════════════════════════════════════════════════

"""
Expected performance (realistic targets):
─────────────────────────────────────────

Month 1: Learning phase
  └─ Win rate: 40-45% (learning to filter signals)
  └─ Avg RR: 1.8 (some losses hurt)
  └─ Monthly return: -5% to +5% (focus on learning)
  
Month 2-3: Refinement phase
  └─ Win rate: 50-55% (filters improving)
  └─ Avg RR: 1.8-2.2 (better entries)
  └─ Monthly return: 5-15% (compounding begins)
  
Month 4+: Mature phase
  └─ Win rate: 55-60% (solid system)
  └─ Avg RR: 2.0+ (good risk management)
  └─ Monthly return: 10-25% (with proper sizing)
  
Note: These assume:
  ✅ Paper trading first
  ✅ Proper position sizing
  ✅ Strict discipline
  ✅ Adapting to market changes
  ❌ NOT guaranteed (past performance ≠ future results)
"""


if __name__ == "__main__":
    print("Quick Reference Guide loaded")
    print("See code examples above for common tasks")
