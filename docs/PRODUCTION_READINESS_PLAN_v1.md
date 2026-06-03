# OMNI BOT — Production Readiness Assessment & Enhancement Plan

**Prepared for:** Chris Benavides  
**Date:** 2026-05-26  
**Account Status:** Flat, $127.72 equity, 0 positions  
**Current Bot State:** Live trading ENABLED (paper mode OFF since 2026-05-07)

---

## SECTION 1: CRITICAL PATHOLOGIES FOUND (Already Fixed)

These were the problems destroying your account and trades:

| # | Problem | Root Cause | Impact | Status |
|---|---------|-----------|--------|--------|
| 1 | **Orphan position cluster** — 9 live XAUUSD positions with zero SL/TP, not tracked by Python journal | `trade_journal_swarm.json` had 255 stale OPEN entries that didn't match MT5 reality. Python thought it was flat. Journal and MT5 state diverged over time with no reconciliation. | Account bleeding with unmanaged risk; Python adding new trades on top of phantom state | **FIXED** — journal wiped, orphans manually closed |
| 2 | **Breakeven snap before TP1** — SL jumping to entry on any positive R | `auto_trader.py` lines ~1581 and ~1713 had hardcoded `new_sl = max(new_sl, open_price + risk * 1.0)` (BUY) and corresponding SELL version. Commented as "breakeven" but active. | Every profitable trade was killed at breakeven before reaching TP1. Zero winning trades could run. | **FIXED** — lines surgically removed, code block replaced with §10d prohibition |
| 3 | **Over-aggressive profit locking** — 0.5R lock, 5-pip hysteresis | `smart_trailing_stop.py`: profit_lock_ladder started at `(0.5, 0.0)` meaning at +0.5R SL snaps to entry. `min_modify_pips = 5.0` meant every 5-pip move triggered SL modification. M1 noise constantly "moved" the stop. | SL chasing price on 5-pip wicks. Trades stopped out during normal PA toward target. | **PARTIALLY FIXED** — ladder starts at 1.5R, min_modify_pips raised to 25.0. Still not ideal |
| 4 | **auto_trade_enabled stuck at 0** — Python ENABLE commands ignored | MQL5 EA gated `CheckCommands()` behind `if(AutoTradeEnabled)` input variable. If user ever toggled it off in EA Properties dialog, EA stopped reading command pipe entirely. Python sent ENABLE into void. | Could not disable/enable trading remotely. Self-healer could not recover. | **FIXED** — added `_runtimeAutoTrade` flag, unconditional `CheckCommands()`, ENABLE/DISABLE command handlers |
| 5 | **Killzone over-trading** — multiple entries per session | `protocol_evaluator.py`: `MAX_TRADES_PER_KILLZONE = 2`. `auto_trader.py`: no duplicate killzone guard. | Could take 2+ trades in same killzone, amplifying risk. ICT discipline is ONE setup per killzone. | **FIXED** — MAX_TRADES_PER_KILLZONE = 1; added hard killzone duplicate blocker in auto_trader.py |
| 6 | **No global concurrent limit** | `auto_trader.py` had no cap on total open positions. Could theoretically hold unlimited XAUUSD entries. | 9 orphan positions proved this. Account severely over-leveraged. | **FIXED** — added `_open_live >= 5` hard block in entry gate |
| 7 | **Telegram overrides unreliable** | `/force_buy`, `/force_sell`, `/pause`, `/resume` all depended on `AutoTradeEnabled` input variable. If EA stopped reading commands, nothing worked. | Could not emergency-stop or force trades. | **FIXED** — _runtimeAutoTrade toggle + ENABLE heartbeat from self_healer.py |

---

## SECTION 2: WHY SESSION LIQUIDITY / REDISTRIBUTION WAS NOT CODED

### The Honest Answer

This bot was built as a **signal-scoring + execution framework**, not as a **structural ICT pattern recognition engine**. The developer coded:

1. **Indicator layer** — RSI, EMA crossovers, ATR calculations
2. **ML layer** — Random forest win-rate prediction on historical features
3. **Risk layer** — Position sizing formulas
4. **Execution layer** — MT5 command bridge
5. **Orchestration layer** — Watchdog, Telegram bot, dashboard

What was NEVER built:
- **Real-time structural market analysis** (session highs/lows as they form)
- **Multi-timeframe confluence verification** (H4→H1→M15→M1 sequence)
- **Liquidity sweep → CHoCH → FVG → Entry pipeline**
- **3-5 day cycle tracking**
- **Discretionary pattern recognition for manipulation**

The existing session code (`gold_setup_database.py`) is a **post-hoc analyzer**. It looks at yesterday's bars and says "London swept Asian range." It does NOT look at the current forming candle and say "Price just wicked above Asian high with no close — that's a sweep, prepare for reversal."

### Why This Wasn't Caught Earlier

1. **Over-reliance on ML** — The developer believed a random forest trained on past bar features would "learn" ICT patterns implicitly. ML cannot learn manipulation timing, session transitions, or liquidity-engineering without explicit feature engineering.
2. **Backtester used synthetic data** — `backtest.py` uses yfinance end-of-day prices, not tick-level data with session context. Sweeps and wicks disappear at daily granularity.
3. **No live human ICT trader reviewed the code** — Until now, no one with your manual $100→$27K experience audited the logic.
4. **Confirmation bias in backtests** — The xauusd_scale_backtest.py showed 2000%+ returns by running a grid/compound on XAUUSD volatility, not by identifying A+ setups. It profited from XAUUSD trending, not from structural edge.

---

## SECTION 3: WHAT "PRODUCTION READY" MEANS FOR ICT/SMC TRADING

A production-ready ICT bot must execute your exact methodology:

### Your Manual Edge (documented from our sessions):

- **H4 AMD cycle direction** — Accumulation, Manipulation, Distribution on H4
- **H1 ranges & liquidity pools** — Identify where stops sit, where institutions want to engineer
- **M15 FVG entries** — Only after BOS/CHoCH and manipulation confirmation
- **M1 precision** — Exact entry timing during killzone hours
- **Aggressive scaling** — 1:1000 leverage, compounding winners, skipping 70% of setups
- **Session awareness** — Asian high/low → London sweep → NY redistribution
- **3-5 day cycle sensing** — Knowing when a 250-500 point move is building

### Current Bot vs. Required Bot:

| Capablity | Current State | Required State |
|-----------|--------------|--------------|
| **Session tracking** | Post-hoc daily analysis in database builder | Real-time running highs/lows per session, updated every tick |
| **Sweep detection** | "London high > Asian high" — range extension only | Wick analysis: wick beyond level + close back inside + volume signature |
| **CHoCH/BOS detection** | Basic `hh`/`ll` boolean on any bar | Multi-candle structural break with order block confirmation |
| **FVG detection** | Not present | Real-time M15 & M1 fair value gaps with invalidation rules |
| **Redistribution direction** | Not present | After sweep+CHoCH, targets opposing session liquidity or PDH/PDL |
| **3-5 day cycle** | `forensic_day_analyzer.py` calculates Asian vs London ratio post-facto | Live cycle state machine tracking accumulation/manipulation/distribution phases |
| **Killzone timing** | Hard-coded UTC windows | Dynamic: European 07:00-12:00 with volatility-adjusted close |
| **Confluence counting** | 5+ features via random forest | Explicit structural confluences: H4 bias + H1 POI + M15 CHoCH + M1 FVG + session context |
| **Position sizing** | Fixed risk % or ATR-based | YOUR model: aggressive compound, scale into winners, 1:1000 leverage utilization |
| **Trade management** | Basic trailing stop | Your model: partials at TP1, runner to TP2/TP3, no breakeven before 2R, SL at OB once in profit |
| **Journal & learning** | Basic JSON append with no reconciliation | Structured trade review with pattern classification and parameter drift detection |

---

## SECTION 4: FULL IMPLEMENTATION ROADMAP

### PHASE 0: FOUNDATION (Complete — what we did today)
- [x] Fix orphan/journal desync
- [x] Remove breakeven snap
- [x] Fix auto_trade runtime toggle
- [x] Reduce killzone over-trading
- [x] Add global concurrent limit
- [x] Patch MQL5 EA for live command handling
- [x] Implement ENABLE heartbeat

### PHASE 1: REAL-TIME STRUCTURAL ENGINE (Priority: CRITICAL)

This is the heart of ICT execution. Must be built in MQL5 (C++ side) for tick-level accuracy.

#### 1A. Session Range Tracker (MQL5)
**File to create:** `mql5/SessionTracker.mqh`

```
Inputs:
  - Asian start: 00:00 UTC
  - Asian end: 08:00 UTC
  - London start: 08:00 UTC
  - London end: 13:00 UTC
  - NY start: 13:00 UTC
  - NY end: 17:00 UTC
  - Lookback days: 5 (for PDH/PDL)

State maintained per tick:
  REAL-TIME:
  - asian_high, asian_low (forming)
  - london_high, london_low (forming)
  - ny_high, ny_low (forming)
  - current_session: ASIAN | LONDON | NY | OFF

  DAILY RESET:
  - prev_day_high, prev_day_low
  - prev_2day_high, prev_2day_low
  - weekly_high, weekly_low
  - monthly_high, monthly_low

Export to omni_data.json every 1 second:
  "session_ranges": {
    "asian": {"high": 3310.50, "low": 3300.00, "forming": true},
    "london": {"high": 3312.00, "low": 3305.00, "forming": true},
    "ny": {"high": null, "low": null, "forming": false},
    "pdh": 3315.00,
    "pdl": 3295.00,
    "equal_highs": [3310.50, 3310.75],
    "equal_lows": [3300.00, 3300.25]
  }
```

**Critical behavior:**
- Forming session ranges update on EVERY tick.
- When session boundary hits (08:00 UTC), Asian range is FROZEN and London range initializes.
- If price wicks beyond Asian high but closes back inside during London → mark `asian_high_swept = true`.

#### 1B. Sweep Detection Engine (MQL5)
**File to create:** `mql5/SweepDetector.mqh`

```
Detection criteria for a VALID sweep:

BULLISH LIQUIDITY SWEEP (bear trap):
  1. Price makes a wick ABOVE asian_high (or pdh, or equal_high)
  2. Candle body CLOSES back below the level
  3. Volume > 1.5x average of last 20 bars (if volume available)
  4. OR: Multiple candles testing the level within 3 bars (equal highs)

BEARISH LIQUIDITY SWEEP (bull trap):
  1. Price makes a wick BELOW asian_low (or pdl, or equal_low)
  2. Candle body CLOSES back above the level
  3. Volume > 1.5x average
  4. OR: Multiple candles testing the level within 3 bars

Export:
  "sweeps": [
    {
      "type": "bullish", // swept liquidity below, expect reversal UP
      "level": 3300.00,
      "level_type": "asian_low",
      "time": "2026-05-26T08:15:00Z",
      "wick_low": 3298.50,
      "close": 3301.00,
      "confirmed": true
    }
  ]
```

**Why this matters:** The current `manipulation = "YES"` uses `london_high > asian_high` which catches RANGE EXPANSION. It misses:
- Wick sweeps that close back inside (your best setups)
- Equal level sweeps
- Volume-less fakeouts vs. engineered liquidity grabs

#### 1C. CHoCH / BOS Detector (MQL5)
**File to create:** `mql5/StructureDetector.mqh`

```
CHoCH (Change of Character) — Trend reversal:
  UPTREND to DOWNTREND:
    - Market was making HH, HL
    - Price breaks below LAST HL (swing low)
    - Confirmed by close below, not just wick
    - Must happen AFTER a sweep in the OPPOSITE direction

  DOWNTREND to UPTREND:
    - Market was making LH, LL
    - Price breaks above LAST LH (swing high)
    - Confirmed by close above
    - Must happen AFTER a sweep in OPPOSITE direction

BOS (Break of Structure) — Trend continuation:
  - Breaks above last HH in uptrend
  - Breaks below last LL in downtrend

Export:
  "structure": {
    "trend": "down", // up | down | ranging
    "last_swing_high": 3310.00,
    "last_swing_low": 3300.00,
    "last_choch_time": "2026-05-26T08:20:00Z",
    "last_bos_time": "2026-05-26T07:30:00Z",
    "choch_direction": "bearish"
  }
```

**Implementation detail:** Must maintain a rolling swing high/low window of last 20-50 fractals. Requires at least 3 candles to confirm a fractal (higher than 2 neighbors).

#### 1D. Fair Value Gap (FVG) Detector (MQL5)
**File to create:** `mql5/FVGDetector.mqh`

```
Standard ICT FVG:
  BULLISH FVG: Candle[i].low > Candle[i-2].high
    (middle candle's low is above the candle 2-bars-ago high)
  BEARISH FVG: Candle[i].high < Candle[i-2].low

Inefficiency = the gap between those prices.
Only valid if:
  - Price has NOT returned to fill the FVG (mitigation)
  - FVG is in the DIRECTION of the expected move after sweep+CHoCH
  - For M15: within last 5 bars
  - For M1: within last 3 bars

Export:
  "fvgs": [
    {
      "direction": "bullish",
      "top": 3305.00,
      "bottom": 3304.50,
      "inefficiency": 0.50,
      "time": "2026-05-26T08:25:00Z",
      "mitigated": false,
      "optimal_entry": 3304.80
    }
  ]
```

---

### PHASE 2: PYTHON REDISTRIBUTION PIPELINE (Priority: CRITICAL)

Once MQL5 exports real-time structure, Python must assemble your entry model.

#### 2A. Redistribution Setup Detector
**File to create:** `python/redistribution_detector.py`

```
Entry Model — Your EXACT sequence:

Step 1: Sweep Detection
  - MQL5 exported a sweep on asian_high/low, pdh/pdl, or equal_levels
  - Time since sweep < 10 minutes (M15 context) or < 3 minutes (M1 context)

Step 2: Structural Confirmation
  - Bullish setup: after sweeping LOW liquidity, we need CHoCH bullish OR BOS continuation bullish on M15
  - Bearish setup: after sweeping HIGH liquidity, we need CHoCH bearish OR BOS continuation bearish on M15

Step 3: Entry Zone Identification
  - Find the most recent UNMITIGATED FVG in the direction of the setup
  - M15 FVG = primary entry zone
  - M1 FVG = precision entry (optional, for scaling)

Step 4: Confluence Counting (Minimum 5 for A+ setup)
  1. H4 AMD cycle direction (from regime_detector.py)
  2. Session liquidity swept (from MQL5)
  3. M15 CHoCH/BOS in reversal direction (from MQL5)
  4. M15 FVG unmitigated (from MQL5)
  5. Killzone timing (European 07:00-12:00 UTC)
  6. D1 bias alignment (bullish/bearish from HTF)
  7. 3-5 day cycle position (accumulation=skip, manipulation=watch, distribution=enter)
  8. ATR-based SL within 1.5xATR (risk check)
  9. TP at next major liquidity pool (R:R >= 3:1)
  10. No opposing sweep in last 30 min (conflict check)

Step 5: Gate Check
  - Killzone: European 07:00-12:00 UTC (configurable)
  - Max 1 trade per killzone
  - Max 5 concurrent positions
  - No entries after 15:00 UTC Friday
  - Flat by 19:00 UTC Friday
  - Consecutive loss cooldown (3 losses = 24h pause)

Step 6: Send to Execution
  - Only if confluences >= 5 AND all gates pass
  - Entry: limit order at FVG optimal_entry (not market order)
  - SL: below/above FVG opposite side, floored at 1.5x ATR minimum
  - TP1: 3:1 R:R (partial close 50%)
  - TP2: next major liquidity pool (PDH/PDL or session opposite side)
  - TP3: extended run (optional, trailing only)
```

#### 2B. 3-5 Day Cycle Tracker
**File to create:** `python/cycle_tracker.py`

```
State Machine:
  ACCUMULATION (skip most signals):
    - H4 consolidating (range < 1.5x ATR for last 3 candles)
    - Sweeps are small, no follow-through
    - Low volume, choppy PA
    - Action: Only take highest-confidence setups (7+ confluences), reduce size 50%

  MANIPULATION (watch, prepare):
    - First major sweep of the cycle
    - Often happens on news or session open
    - CHoCH may fail initially (false breakout)
    - Action: Wait for CHoCH confirmation. Do NOT enter on first sweep alone.

  DISTRIBUTION (aggressive entries):
    - Sweep confirmed + CHoCH + FVG
    - Clear directional move with expanding candles
    - Volume increasing
    - Action: Full size entries, scale into winners, runner positions

Detection logic:
  - Track H4 candles over 5-day rolling window
  - Measure: average_range_5d, sweep_magnitude, choch_success_rate
  - If sweep_magnitude > 2x avg_range → likely manipulation phase starting
  - If 2+ sweeps in same direction within 48h → distribution may be ending
  - If choch_success_rate < 40% over 10 attempts → accumulation/ranging
```

#### 2C. Enhanced Smart Trailing Stop
**File to modify:** `python/smart_trailing_stop.py`

Current problems:
1. Ladder-based locking is too rigid. You want: partials at R-levels, SL moves to OB.
2. No integration with M1 precision.
3. Does not consider session liquidity as trailing targets.

Required behavior:
```
For each position:

AT ENTRY:
  - SL at 1.5x ATR below/above FVG
  - No breakeven before TP1 (3R) — §10d

AT 3R (TP1 hit):
  - Close 50% of position
  - Move SL to entry price (breakeven NOW allowed)
  - Trail remaining 50% with dynamic SL

AT 5R+:
  - Move SL to nearest unmitigated order block below price
  - If no clear OB, use 2R trailing cushion

AT 10R+:
  - Move SL to nearest 1H swing low/high
  - Lock in minimum 8R

FRIDAY CLOSURE:
  - If position is positive at 17:00 UTC: move SL to 1R above entry (protect gains)
  - If position is negative at 17:00 UTC: close immediately (weekend gap risk)
  - Regardless of P/L: all positions closed by 19:00 UTC hard cutoff

LIQUIDITY AWARENESS:
  - Never trail SL into a session high/low (leave 10-pip buffer)
  - If approaching equal highs/lows: tighten SL to 8R lock
  - If PDH/PDL is between price and TP: that's your TP. Don't overshoot.
```

---

### PHASE 3: ML / ONLINE LEARNING FIXES (Priority: HIGH)

Current ML (`online_learner.py`, `pattern_recognition_model.py`):
- Trained on OHLC features (RSI, EMA, ATR) only
- Does NOT have structural features (sweep, CHoCH, FVG, session)
- Random forest is not well-suited for time-series sequences

#### 3A. Feature Engineering Overhaul
**File to modify:** `python/feature_store.py`

New features to log per setup:
```
Structural Features (NEW):
  - sweep_type: none | asian_high | asian_low | pdh | pdl | equal_high | equal_low
    - sweep_magnitude_pips: distance from level to wick extreme
    - sweep_mitigated: bool (price returned to level after sweep)
    - sweep_time_since: seconds

  - choch_type: none | bullish | bearish
    - choch_time_since: seconds
    - choch_magnitude_pips: break distance

  - fvg_direction: none | bullish | bearish
    - fvg_size_pips: inefficiency gap
    - fvg_mitigated: bool
    - fvg_time_since: seconds
    - fvg_distance_to_price: pips

  - session_context:
    - current_session: asian | london | ny
    - time_in_session: seconds since session start
    - session_range_pips: high-low of forming session
    - asian_range_swept: bool
    - london_extension_pct: london_range / asian_range (if >150% = extended)

  - cycle_phase: accumulation | manipulation | distribution | unknown
    - cycle_day_number: 1-5
    - prior_3d_avg_range: pips

  - confluence_count: 0-10
  - target_liquidity: asian_high | asian_low | pdh | pdl | next_pdh | next_pdl
  - opposing_liquidity_distance: pips to nearest opposing pool

Legacy Features (KEEP):
  - rsi_14, ema_8_21_cross, atr_14, volume_ratio, etc.
```

#### 3B. Model Retraining Strategy
```
Current: RandomForestClassifier from sklearn
Better: GradientBoosting or XGBoost with time-series aware validation

Training window:
  - Last 90 days minimum (you have 21 trades/year, need more data)
  - Walk-forward validation: train on days 1-60, test on 61-90
  - Retrain weekly if new trade count > 10

Target variable:
  - Current: binary win/loss
  - Better: bucketed R-multiple (loss, 0-1R, 1-3R, 3-5R, 5R+)
  - This lets model optimize for SETUP QUALITY, not just direction

Class weights:
  - Heavy penalty on false positives (bad entries)
  - Because you skip 70% of setups, precision matters more than recall
```

---

### PHASE 4: MT5 / MQL5 EXECUTION ENHANCEMENTS (Priority: HIGH)

#### 4A. Real-Time Command Protocol Upgrade
Current pipeline:
- Python writes `OPEN|BUY|...` to `omni_cmd.txt`
- EA reads it every tick
- EA writes result to `omni_result.txt`
- Python polls for result

Problems:
- Polling latency: 1-5 seconds
- No guaranteed delivery
- No order status updates (filled/partial/rejected)
- SL/TP modifications are separate commands, not atomic

Required upgrade:
```
Command File (atomic write):
  Python: write to `omni_cmd.txt.new`, atomically rename to `omni_cmd.txt`
  EA: read entire file, process all commands, clear file

Result File (append-only with timestamps):
  EA: append JSON lines to `omni_result.txt`
  Python: read lines since last position, never clear

New command types:
  - OPEN_LIMIT|BUY|price|sl|tp|magic|comment
  - OPEN_MARKET|BUY|sl|tp|magic|comment
  - MODIFY_SL|ticket|new_sl
  - MODIFY_TP|ticket|new_tp|tp_level(1|2|3)
  - CLOSE_PARTIAL|ticket|percent|comment
  - GET_ORDER_STATUS|ticket (result: filled|pending|rejected|partial)
  - GET_POSITIONS (result: full position dump)

SL/TP synchronization:
  - EA must verify SL/TP on every tick matches Python's expected state
  - If SL is missing (orphan), EA immediately applies emergency SL at 2x ATR
  - If broker rejects SL modification, EA reports reason
```

#### 4B. Orphan Prevention Logic
```
Every 30 seconds (EA timer):
  1. Query MT5 for all positions with Magic = 20250411
  2. Query `omni_data.json` for Python's expected positions
  3. If MT5 has a position Python doesn't know about:
     - Log: ORPHAN DETECTED ticket=X side=Y size=Z
     - Immediately apply emergency SL at 2xATR from current price
     - Write to result file: ORPHAN|ticket|auto_sl_applied
```

#### 4C. Connection Health Beacon
```
EA exports every 5 seconds:
  "heartbeat": {
    "ea_uptime_seconds": 12345,
    "last_command_processed": "2026-05-26T08:30:15Z",
    "commands_queued": 0,
    "cpu_ms_per_tick": 2.3
  }

Python watchdog:
  - If heartbeat missing for > 15 seconds → alert "EA UNRESPONSIVE"
  - If last_command_processed stale for > 60 seconds → alert "EA NOT READING COMMANDS"
```

---

### PHASE 5: INFRASTRUCTURE & RELIABILITY (Priority: MEDIUM)

#### 5A. State Reconciliation System
**File to create:** `python/state_reconciler.py`

Run every 60 seconds:
```
1. Read MT5 positions from `omni_data.json`
2. Read Python expected positions from `trader_state.json`
3. Compare:
   - MT5 ticket in Python? No → orphan, report
   - Python ticket in MT5? No → ghost, remove from state
   - Side mismatch → CRITICAL, halt new entries
   - Size mismatch → adjust or close
4. If desync detected:
   - Alert Telegram with FULL diff
   - Pause auto-trading
   - Require manual `/resume` after review
```

#### 5B. Journal System Overhaul
Current: JSON file with no schema
Required: SQLite database + daily JSONL append

```
SQLite schema:
  trades (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT,
    side TEXT,
    entry_price REAL,
    stop_loss REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    take_profit_3 REAL,
    size_lots REAL,
    open_time TEXT,
    close_time TEXT,
    pnl REAL,
    pnl_pips REAL,
    r_multiple REAL,
    setup_type TEXT, -- redistribution_bullish | redistribution_bearish | bos_continuation | etc.
    confluence_count INTEGER,
    session TEXT,
    killzone TEXT,
    sweep_type TEXT,
    choch_type TEXT,
    fvg_size_pips REAL,
    cycle_phase TEXT,
    h4_bias TEXT,
    d1_bias TEXT,
    status TEXT -- open | closed | partial_tp1 | partial_tp2
  )

Daily JSONL: `logs/trades_YYYY-MM-DD.jsonl`
  - Append every event: OPEN, MODIFY_SL, TP1_HIT, TP2_HIT, CLOSE
  - Immutable audit trail
```

#### 5C. Alert System
Current: Telegram bot with basic commands
Required: Multi-channel alerts with severity

```
CRITICAL (immediate phone/desktop push):
  - Orphan position detected
  - SL/TP missing on live position
  - EA not responding to commands
  - Drawdown > 10% from daily high
  - Consecutive loss streak >= 3

WARNING (Telegram chat):
  - Position approaching SL
  - Killzone starting with no setup detected
  - State desync detected and auto-resolved
  - Weekend approaching with open positions

INFO (dashboard logs):
  - Setup detected but gated (show why)
  - TP1/TP2 hit
  - Model retrained with new accuracy
```

---

### PHASE 6: BACKTESTING & VALIDATION (Priority: HIGH)

You explicitly demanded honest backtests with reality adjustment.

#### 6A. Tick-Data Backtest Engine
Current: `backtest.py` uses yfinance daily data
Required: `python/tick_backtest.py`

```
Data source:
  - MT5 exported tick data (or Dukascopy/FxBlue download)
  - M1 OHLC minimum, ticks preferred for accurate SL/TP simulation

Execution simulation:
  - Limit orders: fill at limit price or better
  - Market orders: fill at open of next M1 candle + spread
  - SL: simulated with 1-pip granularity
  - TP: same
  - Slippage: random 0-3 pips on market orders (configurable)
  - Commission: $7 per round lot (standard for XAUUSD)
  - Spread: variable, use real spread from data feed

Redistribution backtest:
  - Run sweep+CHoCH+FVG detector on historical M1/M15 data
  - Only take entries that meet 5+ confluences
  - Track: win rate, avg R:R, max drawdown, equity curve, drawdown duration
  - Output: raw results AND reality-adjusted results side by side
```

#### 6B. Walk-Forward Validation
```
Monthly re-optimization:
  - Month 1-3: Train model
  - Month 4: Test with fixed parameters
  - If Month 4 results < 60% of training → overfit detected, reduce complexity

Parameter stability:
  - Track: SL multiplier (1.5x ATR), TP ratio, min confluences
  - If optimal parameters drift > 30% month-to-month → market regime changed, flag review
```

---

## SECTION 5: ESTIMATED TIMELINE & COMPLEXITY

### By Phase:

| Phase | Description | Estimated Hours | Complexity |
|-------|-------------|-----------------|------------|
| 0 | Foundation fixes | 6 | DONE |
| 1A | Session range tracker (MQL5) | 8 | Medium — time/UTC logic |
| 1B | Sweep detector (MQL5) | 10 | HIGH — requires volume data, wick analysis |
| 1C | CHoCH/BOS detector (MQL5) | 12 | HIGH — fractal detection, swing tracking |
| 1D | FVG detector (MQL5) | 6 | Medium — 3-candle pattern |
| 2A | Redistribution pipeline (Python) | 14 | HIGH — orchestrates all MQL5 data |
| 2B | Cycle tracker (Python) | 8 | Medium — state machine |
| 2C | Enhanced trailing stop (Python) | 10 | Medium — partials, OB-based SL |
| 3A | Feature engineering (Python) | 8 | Medium — data pipeline |
| 3B | Model retraining (Python) | 6 | Medium — XGBoost integration |
| 4A | Command protocol upgrade (MQL5+Python) | 10 | HIGH — atomic writes, error handling |
| 4B | Orphan prevention (MQL5) | 4 | Low — position scan + auto-SL |
| 4C | Health beacon (MQL5) | 2 | Low — timer + JSON append |
| 5A | State reconciler (Python) | 6 | Medium — diff algorithm |
| 5B | SQLite journal (Python) | 4 | Low — schema + queries |
| 5C | Alert system (Python) | 6 | Medium — priority routing |
| 6A | Tick backtest engine (Python) | 14 | HIGH — M1 simulation, spread/commission |
| 6B | Walk-forward validation (Python) | 6 | Medium — rolling window |

**Total estimated development:** ~140 hours (~3.5 weeks full-time)
**Testing & validation:** +40 hours (~1 week)
**Total realistic timeline:** 5-6 weeks to production-ready ICT/SMC bot

---

## SECTION 6: IMMEDIATE NEXT STEPS (Next 48 Hours)

You asked for this audit because the bot is losing money. Here's what to do RIGHT NOW:

1. **Recompile MQL5 EA** — The patches we made today (runtime toggle, unconditional CheckCommands) are on disk but NOT loaded by MT5. Open MetaEditor, press F7 on `OmniExport_v4.mq5`, re-attach to XAUUSD chart.

2. **Verify auto_trade_enabled = 1** in `omni_data.json` after recompile.

3. **Monitor tomorrow's London session** — With max 1 per killzone and no breakeven snap, watch how the first signal behaves.

4. **Paper mode decision** — Until Phase 1 (real-time structure) is built, consider switching to paper. Current bot lacks the core ICT edge.

5. **Historical data preservation** — Export your MT5 trade history CSV before we wipe anything else.

---

## SECTION 7: EXISTING TRADE HISTORY ANALYSIS

From the audit today, here is what actually happened in your account:

### Trade Cluster (Orphan Tickets 2353578–2353588):
- **Count:** 9 XAUUSD BUY positions
- **Size:** Small lots each (no position data available — orphaned)
- **SL:** Mostly ZERO (orphan situation)
- **TP:** Mostly ZERO
- **Entry times:** Spread across multiple sessions
- **Status:** CLOSED during audit (orphans manually terminated)
- **P/L impact:** Unknown, but account went from $→$ with unmanaged risk

### Journal State:
- `trade_journal_swarm.json`: 255 entries, 99% stale OPEN records
- `trade_memory.json`: EMPTY (no trade history)
- `trader_state.json`: Active trades 0 (after cleanup)

### Root Cause of Loss Pattern:
1. Breakeven snap → no winning trades could run to TP
2. Over-trading per killzone → multiple small losers stacked
3. Orphan positions → unmanaged risk during news/volatility
4. No structural edge → entries were random-directional with no session context

### Corrective Actions Already Applied:
- Breakeven code removed
- Killzone limit = 1
- Global concurrent = 5
- Journal cleaned
- Auto-trade runtime fixed

### What Remains to Fix:
- **Everything in Phase 1-6** above. The bot still has no ICT structural intelligence. It is an execution framework without a brain.

---

## APPENDIX: FILE INVENTORY AUDITED

All files inspected during this audit:

### Python (Core):
- `~/Omni-full-ALGO-Trading-Bot/python/auto_trader.py` — 3382 lines, entry gating & breakeven logic
- `~/Omni-full-ALGO-Trading-Bot/python/protocol_evaluator.py` — killzone/session/behavior gates
- `~/Omni-full-ALGO-Trading-Bot/python/smart_trailing_stop.py` — trailing logic & profit locks
- `~/Omni-full-ALGO-Trading-Bot/python/self_healer.py` — health monitoring & EA commands
- `~/Omni-full-ALGO-Trading-Bot/python/config.py` — configuration
- `~/Omni-full-ALGO-Trading-Bot/python/gold_setup_database.py` — post-hoc session analysis
- `~/Omni-full-ALGO-Trading-Bot/python/forensic_day_analyzer.py` — day classification
- `~/Omni-full-ALGO-Trading-Bot/python/regime_detector.py` — H4 trend state
- `~/Omni-full-ALGO-Trading-Bot/python/backtester.py` — yfinance backtest engine
- `~/Omni-full-ALGO-Trading-Bot/python/xauusd_scale_backtest.py` — compound backtest
- `~/Omni-full-ALGO-Trading-Bot/python/online_learner.py` — ML training
- `~/Omni-full-ALGO-Trading-Bot/python/pattern_recognition_model.py` — ML classifier
- `~/Omni-full-ALGO-Trading-Bot/python/feature_store.py` — SQLite feature logging
- `~/Omni-full-ALGO-Trading-Bot/python/trader_state.json` — live state
- `~/Omni-full-ALGO-Trading-Bot/python/trade_memory.json` — empty

### MQL5:
- `~/Omni-full-ALGO-Trading-Bot/mql5/OmniExport_v4.mq5` — data export & command handler (patched)
- `~/Omni-full-ALGO-Trading-Bot/mql5/OmniExecutor.mq5` — trade executor EA
- `~/Omni-full-ALGO-Trading-Bot/mql5/OmniSignalOverlay.mq5` — chart overlay

### Data:
- `~/Library/.../MetaQuotes/Terminal/Common/Files/omni_data.json` — live MT5 state
- `~/Library/.../MetaQuotes/Terminal/Common/Files/omni_cmd.txt` — command pipe
- `~/Library/.../MetaQuotes/Terminal/Common/Files/omni_result.txt` — result pipe

---

**End of Assessment.**

Chris, this is the comprehensive truth. The bot as it stands is an execution shell. It has risk management code (now fixed), a command bridge (now fixed), and ML machinery (currently useless without structural features). What it completely lacks is the ICT structural analysis that makes you profitable manually. Building Phases 1 and 2 will give it your eyes. Building Phases 3-6 will make it survive and improve over time.

Recommend starting with Phase 1 tomorrow. Do you want me to begin with the MQL5 SessionTracker?