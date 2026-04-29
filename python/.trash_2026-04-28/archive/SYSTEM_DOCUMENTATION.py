"""
═══════════════════════════════════════════════════════════════════════════════
  ADVANCED MULTI-TIMEFRAME AUTO-TRADING SYSTEM
  Complete Documentation & Implementation Roadmap
═══════════════════════════════════════════════════════════════════════════════

Your upgraded auto trader now includes:

✅ Multi-Timeframe Order Block Detection
   - Major vs Minor classification
   - Mitigation tracking
   - Timeframe hierarchy analysis

✅ AMD (Accumulation/Manipulation/Distribution) Phase Analysis
   - Detect market phases across all timeframes
   - Confidence weighting by phase
   - Structural alignment detection

✅ Liquidity Mapping & Sweep Detection
   - Internal vs External liquidity identification
   - Session level tracking
   - Sweep probability calculation
   - Support/Resistance identification

✅ Precision Entry/Exit Generation
   - Confluence-based entries
   - Structural target placement
   - Multi-level take profit system

✅ Advanced Risk Management
   - Leverage calculation based on setup quality
   - Fee and swap cost impact
   - Account balance dynamics
   - Win rate-based position sizing
   - Streak-based risk adjustment

✅ Integrated Signal Generation
   - Complete TradingSignal objects
   - Trade suitability classification (scalp/day/swing)
   - Expected value calculation
   - Risk factor identification


═══════════════════════════════════════════════════════════════════════════════
SYSTEM ARCHITECTURE
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│                         MARKET DATA (Multi-Timeframe)                       │
│                    H4 | H1 | M15 | M5 OHLCV Data                           │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────────────┐
│          ADVANCED_STRUCTURE_ANALYZER                                        │
│                                                                             │
│  ┌─ MultiTimeframeOrderBlockDetector                                       │
│  │  • Detects impulse moves                                                │
│  │  • Creates order blocks (major/minor)                                   │
│  │  • Tracks mitigation %                                                  │
│  │  • Classifies by timeframe importance                                   │
│  │                                                                         │
│  ├─ AMDStructureAnalyzer                                                   │
│  │  • Identifies ACC/MAN/DIS phases                                        │
│  │  • Detects Fair Value Gaps                                              │
│  │  • Measures volatility profile                                          │
│  │                                                                         │
│  └─ LiquidityMapper                                                        │
│     • Maps session highs/lows                                              │
│     • Identifies swing levels                                              │
│     • Predicts sweep targets                                               │
│                                                                             │
│  Returns: MultiTimeframeStructure                                          │
│  ├─ order_blocks[Timeframe] → List[PriceLevel]                            │
│  ├─ fair_value_gaps[Timeframe]                                             │
│  ├─ liquidity_levels[Timeframe]                                            │
│  ├─ confluence_zones → List[Dict]                                          │
│  ├─ amd_phases[Timeframe] → AMDPhase                                       │
│  └─ precise_entries/exits → List[Dict]                                     │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────────────┐
│              ADVANCED_RISK_MANAGER                                          │
│                                                                             │
│  ┌─ FeeCalculator                                                          │
│  │  • Spread costs (pips)                                                  │
│  │  • Commission calculations                                              │
│  │  • Slippage assumptions                                                 │
│  │  • Swap point calculations (daily, 3x Wed)                              │
│  │  • Broker fee presets (METATRADER, IC_MARKETS, OANDA, etc.)            │
│  │                                                                         │
│  └─ AdvancedPositionSizer                                                  │
│     • Account balance-aware sizing                                         │
│     • Leverage calculation (setup quality dependent)                       │
│     • Risk % adjustment (streak, confidence)                               │
│     • Margin checking                                                      │
│     • Fee impact on net risk/reward                                        │
│     • Expected value calculation                                           │
│                                                                             │
│  Returns: PositionMetrics                                                  │
│  ├─ quantity (units)                                                       │
│  ├─ leverage, margin_required                                              │
│  ├─ entry_fee, exit_fee_short, exit_fee_long                              │
│  ├─ net_risk_amount, net_reward_amount                                     │
│  ├─ expected_value                                                         │
│  └─ estimated_swap_cost                                                    │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────────────┐
│          INTEGRATED_TRADING_ENGINE                                          │
│                                                                             │
│  IntegratedSignalGenerator                                                 │
│  ├─ Combines structure + risk analysis                                     │
│  ├─ Calculates confluence zones                                            │
│  ├─ Determines confidence levels                                           │
│  ├─ Generates precise entries/exits                                        │
│  ├─ Classifies trade suitability                                           │
│  └─ Identifies risk factors                                                │
│                                                                             │
│  Returns: TradingSignal[]                                                  │
│  ├─ direction, entry_price, entry_type                                     │
│  ├─ stop_loss, take_profit_1, take_profit_2                               │
│  ├─ position (PositionMetrics)                                             │
│  ├─ confidence_level (VERY_HIGH/HIGH/MEDIUM/LOW)                          │
│  ├─ confluence_score (0-1)                                                 │
│  ├─ risk_reward_ratio                                                      │
│  ├─ timeframes_aligned, amd_phases                                         │
│  ├─ scalp/day/swing_suitable (bool)                                       │
│  └─ key_reasons, risk_factors                                              │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────────────┐
│                    EXECUTION LAYER                                          │
│                                                                             │
│  SignalManager tracks active signals                                       │
│  IntegratedAutoTrader executes in paper/live mode                          │
│  Monitors position P&L, manages exits                                      │
└──────────────────────────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════════════
KEY FEATURES EXPLAINED
═══════════════════════════════════════════════════════════════════════════════

1. MULTI-TIMEFRAME ORDER BLOCK DETECTION
   ─────────────────────────────────────
   
   What it does:
   • Detects impulse moves (3+ candles in same direction)
   • Identifies order blocks (where institutional buying/selling occurred)
   • Classifies as MAJOR (swing-level) or MINOR (intraday)
   • Tracks how much has been "mitigated" (filled)
   
   Why it matters:
   • Major OBs on Daily/4H are more reliable
   • Minor OBs on 15m useful for scalping precision entries
   • Mitigated OBs are less reliable (already tested, partially filled)
   
   Example:
   ────────
   EURUSD on H4: Strong up move from 1.0750 → 1.0850
   └─ Order Block created at 1.0820-1.0850
      └─ Size: MAJOR (multiple touches after, strong move before)
      └─ Strength: 0.85 (large body, high conviction)
   
   Subsequent pullback to 1.0810 → Price uses OB as support → BUY signal


2. AMD PHASE ANALYSIS
   ───────────────────
   
   Accumulation Phase (Building):
   • Low volatility, tight range
   • Volume concentrated in small area
   • Usually following previous distribution
   • Action: Wait for breakout OR enter on FVGs within range
   
   Manipulation Phase (Stop Hunting):
   • Price spikes beyond range to catch stops
   • Followed by reversal
   • High wicks on both sides
   • Action: Use wider stops, expect volatility
   
   Distribution Phase (Selling):
   • High volume breakout
   • Reversals at previous resistance
   • Weak closes after moves up
   • Action: Be cautious with longs, fade rallies
   
   How it's used:
   ──────────────
   If Daily is in MANIPULATION and 4H shows ACCUMULATION:
   → Higher probability that NYSE open will trigger ACC break
   → Focus on breakout entries, wider stops
   
   If Daily is DISTRIBUTION and we're at major resistance:
   → Don't hold through resistance (higher chance of reversal)
   → Use tight stops, reduce position size


3. LIQUIDITY MAPPING & SWEEP DETECTION
   ────────────────────────────────────
   
   Internal Liquidity (within recent price action):
   • Support and resistance levels
   • Previous swing highs/lows
   • Tight range consolidations
   • Less likely to be swept unless already tested
   
   External Liquidity (beyond recent range):
   • Session highs/lows
   • Gap areas
   • Unliquidated orders from London close
   • VERY likely to be swept during AM sessions
   
   Sweep Probability Calculation:
   ─────────────────────────────
   Probability = (Direction alignment × 0.3) + (Distance proximity × 0.3) +
                 (AMD phase × 0.2) + (Level type × 0.2)
   
   Example:
   ────────
   Current price: 1.0850
   Session high: 1.0900 (external, not yet swept)
   Recent direction: UP
   AMD phase: ACCUMULATION
   
   Sweep probability = 0.3 + 0.3 + 0 + 0.1 = 0.7 (70% likely)
   → Plan for sweep to 1.0900 during NY open


4. PRECISION ENTRY/EXIT POINTS
   ────────────────────────────
   
   Entries are generated at:
   • Confluence zones (2+ timeframes align)
   • Order block boundaries
   • FVG lows/highs
   • Pullback into support/resistance
   • Liquidity sweep completion
   
   Stop losses placed at:
   • Support/resistance level (1% beyond)
   • ATR × 1.5 if no structural level
   • Below minor order block if available
   • Avoids tight stops in high volatility (AMD.MANIPULATION)
   
   Take profits placed at:
   • Fibonacci extensions (1.618, 2.618)
   • Next order block
   • Session high/low
   • ATR × 3.0
   
   Two-level exit system:
   ───────────────────────
   TP1 (50% exit at) = Entry + 0.5 × (Final TP - Entry)
   └─ Locks in profit early, reduces risk
   └─ Especially useful for scalps/day trades
   
   TP2 (remaining 50%) = Entry + full swing target
   └─ Allows profit running on strong moves
   └─ Uses trailing stops after TP1 hit


5. ADVANCED RISK MANAGEMENT
   ─────────────────────────
   
   Fee Impact Calculation:
   ──────────────────────
   
   Entry Cost = Spread + Commission + Slippage
   └─ EURUSD Spread: 1 pip = $10 per 1 lot
   └─ Commission: $10 per standard lot
   └─ Slippage: Assume 0.5 pip = $5 per lot
   └─ Total entry for 0.5 lot: ~$12.50
   
   Exit Cost (short-term same day):
   └─ Same as entry: ~$12.50
   
   Exit Cost (long-term holding):
   └─ Exit cost + Swap costs
   └─ EURUSD Long: -0.1 pip per day × 0.5 lot = -$0.50/day
   └─ 5-day hold = -$2.50 swap cost
   └─ Total exit: ~$15 for 5-day trade
   
   Impact on risk/reward:
   ──────────────────────
   Position risk = (Entry SL distance) × position size
   Net risk = Position risk + Entry cost + Exit cost
   
   Example:
   ────────
   Entry: 1.0850, SL: 1.0800, TP: 1.0950
   Risk distance: 50 pips
   Position: 1.0 lot
   
   Gross risk: 50 pips × $10 = $500
   Entry fee: $12.50
   Short exit fee: $12.50
   Net risk: $525
   
   Gross reward: 100 pips × $10 = $1,000
   Exit fee: $12.50
   Net reward: $987.50
   
   Net RR ratio: 987.50 / 525 = 1.88 (18% fee impact!)


6. POSITION SIZING WITH LEVERAGE
   ──────────────────────────────
   
   Risk % based on:
   • Base: 2% for good setups (70%+ confidence)
   • Reduced to 1% for low confidence (<60%)
   • Reduced to 0.5% after 2+ consecutive losses
   • Increased to 3% after 3+ consecutive wins (max)
   • Further reduced if daily loss limit approaching
   
   Leverage based on:
   • Setup quality (major OBs + high confluence = more leverage)
   • Risk/reward ratio (better RR = more leverage)
   • Current margin utilization (high = reduce leverage)
   • Strategy type (scalp = 50x, swing = 20x)
   
   Example:
   ────────
   Setup: 75% confidence, 1.8 RR ratio, 30% margin used
   Base leverage: 20x
   Quality bonus: 20 × 1.3 = 26x
   RR adjustment: 26 × 1.2 = 31.2x
   Margin adjustment: 31.2 × 1.0 = 31.2x (no reduction)
   Final: 31x leverage


7. ACCOUNT BALANCE DYNAMICS
   ──────────────────────────
   
   Daily loss limit:
   └─ Once day equity down X%, stop trading (avoid drawdown spiral)
   └─ Prevents "revenge trading"
   
   Streak management:
   └─ After wins: increase position size (compound profits)
   └─ After losses: reduce position size (recover capital)
   └─ 3 consecutive wins: increase to 3% risk
   └─ 3 consecutive losses: reduce to 0.5% risk
   
   Margin level tracking:
   └─ Calculate free margin after each trade
   └─ Prevent overleveraging
   └─ Reduce size if margin level > 80%
   
   Equity curve optimization:
   └─ Track peak equity
   └─ If down >10% from peak, reduce leverage/size


═══════════════════════════════════════════════════════════════════════════════
USAGE EXAMPLES
═══════════════════════════════════════════════════════════════════════════════

EXAMPLE 1: Scalping Setup (5-15 minute timeframe)
──────────────────────────────────────────────────

Scenario:
├─ EURUSD 1.0850 (current)
├─ Daily: MANIPULATION phase (high volatility expected)
├─ 4H: ORDER BLOCK at 1.0820-1.0850 (MAJOR)
├─ H1: ORDER BLOCK at 1.0830-1.0850 (MINOR) - just broken
├─ 15m: Pullback into H1 order block, showing base
└─ Confluence: 3 timeframes (Daily structure, 4H OB, H1 OB)

Signal Generation:
├─ Entry: 1.0835 (inside H1 OB, pullback)
├─ Stop: 1.0810 (below 1H OB low)
├─ TP1: 1.0852 (50% of move to next level)
├─ TP2: 1.0875 (4H resistance)
├─ Risk: 25 pips, Reward: 40 pips → RR 1.6
├─ Confidence: HIGH (3 TF aligned)
└─ Trade duration: SCALP (< 5 min)

Position Sizing:
├─ Account: $10,000
├─ Risk: 2% = $200
├─ Risk per pip: 50 pips = $200 / 50 = $4/pip
├─ For standard lot ($10/pip): Size = 0.4 lot
├─ Leverage: 30x (good RR, high confidence, low margin)
├─ Margin required: (1.0850 × 100k × 0.4) / 30 = $1,447
└─ Free margin after: $10,000 - $1,447 = $8,553

Why this works:
└─ Tight stop in volatile phase = acceptable
└─ Multiple timeframe confluence = high probability
└─ Liquidity nearby (4H OB) = easy exit management
└─ High leverage OK because: small size, tight stop, good risk/reward


EXAMPLE 2: Swing Trade Setup (4H - Daily)
───────────────────────────────────────────

Scenario:
├─ GBPUSD 1.2700 (current)
├─ Daily: ACCUMULATION phase (building after distribution)
├─ 4H: Fair Value Gap at 1.2680-1.2700 (bullish, not yet filled)
├─ H1: Multiple minor OBs holding support
├─ AMD alignment: All higher TFs bullish
└─ Confluence: 2 timeframes (Daily + 4H structure)

Signal Generation:
├─ Entry: 1.2690 (into Daily + 4H order block/FVG)
├─ Stop: 1.2640 (below Daily support, 50 pip buffer)
├─ TP1: 1.2735 (Daily resistance via FVG fill)
├─ TP2: 1.2800 (Major Daily resistance)
├─ Risk: 50 pips, Reward: 110 pips → RR 2.2
├─ Confidence: MEDIUM-HIGH (2 TF aligned, good AMD phase)
└─ Trade duration: SWING (2-5 days)

Position Sizing:
├─ Account: $10,000
├─ Risk: 1.5% (fewer daily trades, longer hold) = $150
├─ Risk per pip: 50 pips = $150 / 50 = $3/pip
├─ Standard lot = $10/pip → Size = 0.3 lot
├─ Leverage: 15x (swing trade, wider stop acceptable)
├─ Margin required: (1.2700 × 100k × 0.3) / 15 = $2,540
├─ Free margin after: $10,000 - $2,540 = $7,460
├─ Swap cost (5 days): +2.0 pips × 0.3 lot = $6 cost
└─ Fee impact: ~$15 entry + $15 exit + $6 swap = $36 cost

Why this works:
└─ Wider stops necessary for swing trades
└─ Good RR (2.2) justifies longer hold
└─ AMD phase supports direction (accumulation = preparing upside)
└─ Lower leverage (15x) = reduce liquidation risk
└─ Plan for swap cost in total P&L


EXAMPLE 3: High Leverage Momentum Trade (Risk: NOT RECOMMENDED)
───────────────────────────────────────────────────────────────

What NOT to do:
├─ Entry: 1.0850 (just touched level, no confirmation)
├─ Stop: 1.0840 (10 pips - too tight for volatility)
├─ Position: 2 lots with 50x leverage = $21,400 margin needed!
├─ Account size: $10,000 → MARGIN CALL AT +10%!
├─ Risk: 3% on tight stop in MANIPULATION phase = recipe for disaster
└─ One adverse 1H candle = forced liquidation, 100% loss

Why this fails:
└─ Leverage too high for account size
└─ Stop too tight for volatility phase
└─ No confluence (single timeframe entry)
└─ Margin utilization > 100%
└─ No edge, pure guess


═══════════════════════════════════════════════════════════════════════════════
IMPLEMENTATION CHECKLIST
═══════════════════════════════════════════════════════════════════════════════

PHASE 1: Setup & Testing (Week 1)
──────────────────────────────────
□ Copy all 4 new modules to your trading directory:
  - advanced_structure_analyzer.py
  - advanced_risk_manager.py
  - integrated_trading_engine.py
  - INTEGRATION_GUIDE.py

□ Install any missing dependencies:
  pip install pandas numpy scipy

□ Verify each module loads without errors:
  python -c "import advanced_structure_analyzer"
  python -c "import advanced_risk_manager"
  python -c "import integrated_trading_engine"

□ Run INTEGRATION_GUIDE.py example:
  python INTEGRATION_GUIDE.py

□ Review the generated signals (check quality, confidence levels)

□ Backtest signals on historical data (use your backtester.py)


PHASE 2: Integration with auto_trader.py (Week 1-2)
────────────────────────────────────────────────────
□ In auto_trader.py, add imports (after existing imports):
  from integrated_trading_engine import IntegratedSignalGenerator
  from advanced_risk_manager import AccountMetrics
  from advanced_structure_analyzer import Timeframe

□ Create signal generator (in main startup):
  signal_gen = IntegratedSignalGenerator(broker="METATRADER")

□ Replace signal detection logic:
  OLD: setups, smt = ict_engine.get_ict_scanner()
  NEW: 
    tf_data = {...fetch multi-timeframe data...}
    account = AccountMetrics(...current account state...)
    signals = signal_gen.generate_signals(symbol, tf_data, account)

□ Update trade execution:
  OLD: place_trade(symbol, direction, qty, sl, tp, confidence)
  NEW: place_trade(symbol, signal.direction, signal.position.quantity,
                    signal.stop_loss, signal.take_profit_2, signal.confidence)

□ Test on paper mode for 2+ weeks:
  PAPER_MODE = True  # Keep this for testing!
  
□ Verify:
  - Signals have correct direction (match visual analysis)
  - Stop losses and take profits make sense
  - Position sizes are appropriate for account
  - Expected values are positive on average


PHASE 3: Live Paper Trading (Week 2-3)
───────────────────────────────────────
□ Continue paper trading for minimum 2 more weeks
□ Track all signals in spreadsheet:
  - Entry time, price, direction
  - Exit time, reason, P&L
  - Slippage vs calculated fees
  - Win/loss confirmation
  
□ Analyze results:
  - Win rate (target: >50%)
  - Risk/reward actual vs predicted
  - Average hold time
  - Largest wins and losses
  - Confluence accuracy (2TF better than 1TF?)

□ Identify improvements:
  - Which entry types work best for you?
  - Which timeframe combinations give best results?
  - Do AMD phases actually matter?
  - What confidence level should be minimum?

□ Adjust parameters:
  - MIN_CONFLUENCE_COUNT
  - MIN_CONFIDENCE_LEVEL
  - POSITION_SIZE_MULTIPLIER
  - MAX_LEVERAGE


PHASE 4: Live Trading (Only if paper results are profitable!)
──────────────────────────────────────────────────────────────
□ Reduce initial position sizes to 50% of calculated:
  - Instead of 0.5 lot, trade 0.25 lot
  - Instead of 50x leverage, use 25x leverage
  - This cushion accounts for slippage not modeled

□ Start with 1 symbol only:
  - Not EURUSD if highly correlated
  - Pick one that showed best results in paper trading

□ Add symbols one at a time:
  - After 2 weeks profitable with Symbol1
  - Add Symbol2 if Symbol1 still profitable
  - Never have >3 symbols trading simultaneously

□ Monitor daily:
  - Daily P&L vs target
  - Win/loss ratio
  - Largest drawdown vs prediction
  - Are fees/slippage matching calculations?

□ Emergency kill switches:
  DAILY_LOSS_LIMIT_PCT = 5.0    # Stop if down 5% in one day
  WEEKLY_LOSS_LIMIT = 10.0       # Stop if down 10% in one week
  CONSECUTIVE_LOSS_LIMIT = 5     # Stop after 5 losses in a row
  
  If any triggered → HALT trading, review what went wrong


═══════════════════════════════════════════════════════════════════════════════
PERFORMANCE METRICS TO TRACK
═══════════════════════════════════════════════════════════════════════════════

Minimum viable trading results (paper/live):
────────────────────────────────────────────
□ Win Rate: ≥50% (break-even at best with poor RR)
□ Average RR: ≥1.5 (at least 1.5:1 risk to reward)
□ Expected Value: Positive (WR% × Reward) - (1-WR% × Risk) > 0
□ Sharpe Ratio: ≥1.0 (risk-adjusted returns)
□ Max Drawdown: <20% from peak equity
□ Profit Factor: ≥1.5 (Gross Profit / Gross Loss)

Quality of signals generated:
──────────────────────────────
□ Confluence accuracy:
  - 4TF aligned signals: 65%+ win rate expected
  - 3TF aligned signals: 55%+ win rate expected
  - 2TF aligned signals: 50%+ win rate expected
  - 1TF signals: <50% win rate (skip these)

□ Confidence level accuracy:
  - VERY_HIGH signals: 70%+ win rate
  - HIGH signals: 60%+ win rate
  - MEDIUM signals: 50%+ win rate

□ AMD phase impact:
  - Do DISTRIBUTION phase trades lose more?
  - Do ACCUMULATION phase trades win more?
  - Should you skip MANIPULATION phase?

□ Fee/swap impact:
  - Are actual costs matching calculations?
  - Do swap costs materially impact swings?
  - Is slippage higher/lower than assumed?


═══════════════════════════════════════════════════════════════════════════════
TROUBLESHOOTING
═══════════════════════════════════════════════════════════════════════════════

Problem: "Position size is zero / too small"
───────────────────────────────────────────
Cause: Stop loss too close, not enough margin, or low confidence
Solution:
  - Check if SL is tighter than 20 pips (use wider stops)
  - If account balance low, add funds
  - Only trade HIGH+ confidence signals
  - Reduce MAX_LEVERAGE if margin constrained

Problem: "Signals are missing (no confluences found)"
────────────────────────────────────────────────────
Cause: Data issues or wrong timeframe pairs
Solution:
  - Verify data has 200+ candles per timeframe
  - Check timeframes are [DAILY, H4, H1, M15]
  - Ensure no data gaps or missing OHLCV columns
  - Look at structure analyzer output directly

Problem: "Win rate is <50% on good signals"
────────────────────────────────────────────
Cause: Slippage worse than expected or AMD phase matters more
Solution:
  - Increase slippage_pips in FeeCalculator (0.5 → 1.0)
  - Skip trades if MANIPULATION phase active
  - Tighter stops on high volatility
  - Only trade during active sessions (London/NY open)

Problem: "Getting liquidated or hitting margin call"
──────────────────────────────────────────────────────
Cause: Over-leveraging or position too large
Solution:
  - Cut max_leverage in half
  - Multiply all position sizes by 0.5
  - Increase daily loss limit stop (5% → 7%)
  - Check margin calculations match broker

Problem: "Fees are destroying profitability"
──────────────────────────────────────────────
Cause: Wrong fee structure or too many trades
Solution:
  - Verify broker fees in FeeCalculator match your actual broker
  - Increase SCAN_INTERVAL (fewer trades)
  - Only trade HIGH/VERY_HIGH confidence
  - Use wider TP1 targets (make more pips per trade)


═══════════════════════════════════════════════════════════════════════════════
MAINTENANCE & OPTIMIZATION
═══════════════════════════════════════════════════════════════════════════════

Monthly Review:
───────────────
□ Export all signals and trades to CSV
□ Analyze which setups performed best:
  - Confluence level (2TF vs 3TF vs 4TF)?
  - Trade duration (scalp vs swing)?
  - AMD phase impact?
  - Time of day?
  - Which symbols?

□ Identify weak spots:
  - Do MAJOR OBs perform better than MINOR?
  - Are FVG fills profitable?
  - Do liquidity sweeps actually happen?
  - What % of confluence zones are accurate?

□ Adjust parameters:
  MIN_CONFIDENCE_LEVEL = 2  # Currently trading MEDIUM+, try HIGH+
  MAX_LEVERAGE = 50  # Reduce if getting stopped out more
  DAILY_LOSS_LIMIT = 5  # Reduce if getting revenge-traded
  
Quarterly Review:
──────────────────
□ Backtest ALL changes before live deployment
□ Check that improvements generalize to new data
□ Validate against 1-year historical chart
□ Update fee structures if broker changes rates

Annual Review:
───────────────
□ Recalibrate order block detection (maybe more sensitive?)
□ Update liquidity level calculations (market structure changes)
□ Verify AMD phase logic still works (especially post-volatility)
□ Full system rewrite if win rate < 48%


═══════════════════════════════════════════════════════════════════════════════
FINAL NOTES
═══════════════════════════════════════════════════════════════════════════════

This system is a FRAMEWORK, not a guaranteed money machine.

Key principles:
┌────────────────────────────────────────────────────────────────┐
│ 1. Confluence beats single indicators                          │
│ 2. Structural bias matters more than timeframe                │
│ 3. Fees are real - always calculate net risk/reward           │
│ 4. Leverage is a double-edged sword - use conservatively      │
│ 5. Position sizing is 90% of success, direction is 10%        │
│ 6. AMD phases filter noise but aren't foolproof               │
│ 7. Order blocks + liquidity mapping find best entries         │
│ 8. Never risk more than 2% per trade, ever                   │
│ 9. Backtesting doesn't equal forward performance             │
│ 10. Discipline beats intuition, always                        │
└────────────────────────────────────────────────────────────────┘

Good luck, and trade with discipline! 🎯

═══════════════════════════════════════════════════════════════════════════════
"""

if __name__ == "__main__":
    print(__doc__)
