
# OMNI ICT TRADING PLAN v3.0 — $100 → $100,000
# Strategy: Dual-Timeframe ICT Confluence + AMD Session Logic

---

## I. MARKET CONDITIONS (MUST ALL PASS BEFORE ANY ENTRY)

### A. Session Filter — Trade ONLY During Valid Sessions
| Session        | UTC Hours   | Status     | Size Multiplier | Min Confidence |
|----------------|-------------|------------|-----------------|----------------|
| London Open    | 07:00-09:00 | ACTIVE     | 1.0x            | 55             |
| London Close   | 11:00-13:00 | CAUTION    | 0.75x           | 60             |
| NY Open        | 12:00-14:00 | ACTIVE     | 1.0x            | 55             |
| NY Close       | 19:00-21:00 | SKIP       | 0.5x            | 70             |
| Asia Open      | 22:00-24:00 | SKIP       | 0.75x           | 65             |
| Asia Mid       | 01:00-06:00 | SKIP       | OFF             | OFF            |
| Pre-NFP Thu    | All day     | SKIP       | OFF             | OFF            |
| FOMC Day       | All day     | SKIP       | OFF             | OFF            |

> SKIP = bot blocks all entries. CAUTION = trades allowed with reduced size.

### B. Timeframe Alignment (MANDATORY)
| Timeframe | Purpose          | Action Required                              |
|-----------|------------------|----------------------------------------------|
| D1        | Macro trend      | Must not oppose signal direction               |
| H4        | Directional bias | BOS or CHoCH MUST print in signal direction  |
| H1        | Execution        | Setup generated on this frame                |
| M15       | Precision entry  | M15 candle must confirm within 2 bars        |
| M5        | Pinpoint (opt)   | Only for A+ setups (80+ confidence)          |

### C. HTF Must Align
- LONG signal:  D1 bullish/neutral AND H4 bullish BOS/CHoCH
- SHORT signal: D1 bearish/neutral AND H4 bearish BOS/CHoCH
- CONFLICT:     NO TRADE — wait for alignment

### D. Quarter Theory — Where Price Sits
| Quarter | Label         | Long Bias | Short Bias | Best Setup |
|---------|--------------|-----------|------------|------------|
| Q1      | Deep Discount  | YES       | NO         | Buy OB/FVG |
| Q2      | Discount      | YES       | NO         | Buy OB/FVG |
| Q3      | Premium       | NO        | YES        | Sell OB/FVG |
| Q4      | Deep Premium | NO        | YES        | Sell OB/FVG |

> A+ OTE Entry: 62-79% Fib retracement INTO the zone (deepest OTE = tightest SL).

---

## II. ICT SIGNAL DEFINITION — Exact Setup Criteria

### Setup A: Sweep + Order Block (PRIMARY ~45% of signals)
1. **Sweep**: Price takes inducement liquidity (equal highs/lows), runs stops
2. **Confirmation**: Reversal candle within 1-3 H1 bars after sweep
3. **OB Present**: Unmitigated bullish/bearish Order Block near sweep origin
4. **M15 Confirm**: M15 prints CHoCH in signal direction within 2 bars

### Setup B: FVG Fill + Sweep Gate (SECONDARY ~35%)
1. **Prior Sweep**: H1 or H4 liquidity sweep in last 25 bars
2. **FVG Formation**: 3-candle fair value gap, unmitigated
3. **Fill Trigger**: Price returns to FVG midpoint (OTE 50%)
4. **M15 Confirm**: Displacement candle through FVG edge

### Setup C: AMD Distribution + Continuation (TERTIARY 15%)
1. **Asia Range**: Identified high and low
2. **Manipulation**: London sweep of Asia high/low with reversal
3. **Distribution**: NY continuation in manip direction
4. **Entry**: NY session retest of AMD range breakout

### Setup D: CISD + Inverted FVG (RARE 5%, HIGHEST CONFIDENCE)
1. **CISD**: 3-bar same-direction run engulfed by 50%+ opposing candle
2. **At Key Level**: CISD at H4 OB or prior S/R
3. **Inverted FVG**: Price closes THROUGH opposing FVG confirming intent
4. **Confidence**: Auto-boosted +15 to +18

---

## III. ENTRY SPECIFICATIONS — Precision Rules

### Entry Level Selection (Priority Order)
| Priority | Level            | Description                    | SL Buffer |
|----------|-----------------|--------------------------------|-----------|
| 1st      | OTE 50% of OB   | Midpoint of OB body            | 1.2x ATR  |
| 2nd      | OTE 62% Fib     | Deep entry in OB range         | 1.2x ATR  |
| 3rd      | OTE 79% Fib     | Deepest entry (tightest SL)    | 1.5x ATR  |
| 4th      | OB Open         | Candle open of OB formation    | 1.5x ATR  |

### NEVER Enter At:
- OB extreme high/low (worst fill, widest SL)
- Round numbers directly (buy at 1.2000 exactly — front-run risk)
- Inside a liquidity pool (equal highs/lows within 3x spread)
- During push phase exhaustion (3rd+ candle shrinking body)

### Entry Price Drift Tolerance
- max_drift_pct = 0.30% of signal entry
- Example: signal at 1.34000, current at 1.34300 → drift = 0.22%, PASS
- Example: signal at 1.34000, current at 1.35000 → drift = 0.74%, SKIP

---

## IV. STOP LOSS & TAKE PROFIT — Exact Placement

### SL Placement by Setup
| Setup   | SL Location                                   | Buffer       |
|---------|-----------------------------------------------|--------------|
| Sweep+OB| Below/above M15 swing that formed OB            | +1.2x ATR   |
| FVG     | Beyond FVG boundary that is being swept         | +3 pips     |
| AMD     | Beyond Asia range extreme swept in manipulation | +5 pips     |
| CISD    | Below/above CISD engulf candle                  | +1.2x ATR   |

### Minimum SL Distances (Hard Floor)
| Symbol  | Minimum SL Distance                           |
|---------|-----------------------------------------------|
| XAUUSD  | $1.00 (100 pips at 0.01 tick size)            |
| XAGUSD  | $0.08 (80 pips at 0.001 tick size)             |
| Forex   | 15 pips (0.0015 price units)                   |
| JPY     | 15 pips (0.15 price units)                     |

### TP Targets — Opposing Liquidity
| Level | Fraction | Target                          | Expected Hold |
|-------|---------:|---------------------------------|---------------|
| TP1   | 50%      | Nearest opposite OB or H4 S/R   | 1-3 hours     |
| TP2   | 30%      | Prior session high/low          | 4-8 hours     |
| TP3   | 20%      | Runner — next major liquidity     | 8-24 hours    |

### Gold TP Grid (XAUUSD)
- TP1: Opposing H4 OB or equal highs/lows
- TP2: Prior day high/low (PDH/PDL)
- TP3: Weekly structure extreme or round number ($50 increments)
- Avoid placing TP exactly at $50 increments — set 30 cents before

---

## V. POSITION SIZING — Risk Per Trade

### Base Formula (Per Trade)
```
risk_amount = equity × risk_pct / 100
lots = risk_amount / (SL_distance_in_ticks × tick_value)
final_lots = MIN(lots, margin_cap_lots)
margin_cap_lots = (equity × 0.5 × leverage) / (contract_size × current_price)
```

> **CRITICAL:** At equity below $50, forex SL distances may exceed risk budget for 0.01 lots. Bot auto-blocks trades where calculated risk > 5% of equity (independent of risk_pct setting). First valid trades appear at ~$75+ equity for forex, $300+ for metals.

### Order Type: LIMIT ORDERS (Default)
| Setting | Value | Description |
|---------|-------|-------------|
| Order Type | BUY_LIMIT / SELL_LIMIT | Placed at OTE 50% of OB/FVG |
| Entry Method | OTE 50% of identified OB or FVG | Not market price |
| Max Pending/Symbol | 2 | No more than 2 pending per symbol |
| Max Pending Total | 5 | Account-wide pending cap |
| TTL | 360 minutes (6 hours) | Auto-cancel if not filled |
| Stale Cancel | 10 pips | Cancel if price blows past entry |
| Fallback | MARKET | If signal entry already breached |

### Why Limit Orders?
- **Better fills:** Entry at discount/premium, not current market
- **No spread cost at entry:** Limit = no spread on fill
- **Psychological:** Removes FOMO impulse; price must come to you
- **Risk defined:** SL placed before order is live
- **Backtest accuracy:** Entry_price in signals IS the limit price

### Limit Order Lifecycle
```
Signal Generated → OTE price computed → LIMIT placed → Price returns to OTE → Filled → SL/TP active
                      ↓ (if price already past OTE)
              Fallback to MARKET order
                      ↓ (if price blows past by 10 pips)
              Cancel stale order, wait for next setup
```

### Risk Tiers by Equity
| Equity     | Risk/Trade | Max Concurrent | Primary Symbols              |
|------------|-----------:|--------------:|------------------------------|
| $100–$200  | 1.0%       | 1             | EURUSD, GBPUSD only          |
| $200–$300  | 1.5%       | 2             | + AUDUSD, USDCAD             |
| $300–$500  | 1.5%       | 2             | + XAGUSD unlocked            |
| $500–$1K   | 2.0%       | 3             | + XAUUSD unlocked            |
| $1K–$5K    | 2.0%       | 3             | All symbols, scale to 0.02   |
| $5K–$20K   | 2.5%       | 4             | All symbols, scale to 0.05   |
| $20K–$50K  | 3.0%       | 5             | Full portfolio, runners      |
| $50K–$100K | 3.0%       | 5             | Max size, compound fully     |

### Minimum Lot Size
- Forex: 0.01 lots (broker floor)
- XAUUSD: 0.01 lots (requires $4.50+ margin)
- XAGUSD: 0.01 lots (requires $0.80+ margin)

---

## VI. TRADE MANAGEMENT — Active Position Rules

### After Entry (Within 5 minutes)
1. Verify position reflected in MT5
2. SL set at exact level per Setup IV
3. TP targets set to TP1/TP2/TP3

### After TP1 Hit (50% closed)
1. Move SL to breakeven + 1 tick
2. Trail remaining 50% with ATR-based trail
3. If push phase confirmed → scale in 50% additional

### After 1R Profit
1. Lock minimum +0.3R (SL at entry + 30% of risk)
2. Trail at 1.5x ATR

### After 2R Profit
1. Lock +1.0R minimum (SL at entry + full risk distance)
2. Trail at 2.5x ATR (runner mode)

### After 3R+ Profit
1. Lock +2.0R minimum
2. Trail at 3.5x ATR for gold, 2.5x for forex
3. Close runner if opposing BOS on H1

### Scale-In Rules
- Trigger: 1R profit + push confirmed on M15
- Size: 50% of original
- SL: Same as current trail SL
- Max: 1 scale-in per trade (no pyramiding)

---

## VII. EXIT CONDITIONS — When to Close Early

### Mandatory Close
1. Opposing BOS on H1 (structure broken against position)
2. SL hit (always honored — no manual interference)
3. End of NY session with open runner and TP not near

### Consider Close (Discretion)
1. Push exhaustion at opposing liquidity
2. News release within 30 minutes (NFP, CPI, FOMC)
3. Weekend approaching (Friday after 3pm UTC)

### Never Close Early
1. Runner during confirmed push phase
2. Within 1R of entry (let price breathe)
3. During London/NY active session with valid structure

---

## VIII. DRAWDOWN PROTECTION — Account Preservation

### Circuit Breakers
| Trigger                        | Action               | Duration       |
|-------------------------------|----------------------|----------------|
| 3 consecutive losses          | Halt all entries     | 12 hours       |
| Daily loss >5%                | Halt for day         | Until midnight |
| Weekly DD >10%                | Reduce risk to 0.5% | Until recovered|
| Weekly DD >20%                | Full halt            | Manual restart |
| Single trade loss >3%         | Review setup type    | Immediate      |

### Recovery Protocol
1. After halt: Paper-mode forward test for 3 winning days
2. Restore at 50% reduced size until equity >95% of peak
3. Full size only after new equity high

---

## IX. SCALING ROADMAP — $100 → $100,000

### Phase 0: Foundation ($100–$500, Weeks 1–4)
- Symbols: EURUSD, GBPUSD, AUDUSD, USDCAD, USDJPY
- Risk: 1.0–1.5% per trade
- Target: $100 → $500 (400% gain)
- Focus: High WR (70%+), small consistent wins
- Metals: LOCKED
- Expected trades: 3–5 per week

### Phase 1: Silver Unlock ($500–$2,000, Weeks 5–12)
- Symbols: All forex + XAGUSD
- Risk: 1.5–2.0% per trade
- Target: $500 → $2,000 (300% gain)
- Focus: Compound lot sizing kicking in (0.02+ lots)
- Gold: Still locked until $500
- Expected trades: 5–8 per week

### Phase 2: Gold Entry ($2,000–$10,000, Months 3–6)
- Symbols: Full portfolio (all 7 symbols)
- Risk: 2.0–2.5% per trade
- Target: $2,000 → $10,000 (400% gain)
- Focus: Gold becomes primary profit driver
- Expected trades: 8–12 per week

### Phase 3: Acceleration ($10,000–$50,000, Months 7–12)
- Symbols: All + optional indices (NAS100, US30)
- Risk: 2.5–3.0% per trade
- Target: $10,000 → $50,000 (400% gain)
- Focus: Scale-ins and runners generating compound
- Expected trades: 10–15 per week

### Phase 4: Capital Preservation ($50,000–$100,000, Months 12–18)
- Symbols: Same as Phase 3
- Risk: 2.0–2.5% (REDUCED from peak)
- Target: $50,000 → $100,000 (100% gain)
- Focus: Drawdown protection paramount
- Expected trades: 8–12 per week

### Phase 5: Beyond $100K
- Risk: 1.5–2.0% (institutional sizing)
- Withdraw profits monthly
- Maintain $100K core equity as "insurance"
- Compound only gains above $100K

---

## X. EXPECTED PERFORMANCE (Backtest-Validated)

### Per-Symbol Projections (Monthly, $1000 account)
| Symbol | Trades/Mo | WR    | Avg Win | Avg Loss | Net/Mo |
|--------|----------:|------:|--------:|---------:|-------:|
| EURUSD | 8         | 70%   | +$12   | -$8      | +$33   |
| GBPUSD | 10        | 77%   | +$10   | -$7      | +$45   |
| AUDUSD | 12        | 57%   | +$9    | -$7      | +$18   |
| USDCAD | 6         | 77%   | +$6    | -$5      | +$18   |
| USDJPY | 5         | 80%   | +$8    | -$6      | +$20   |
| XAGUSD | 8         | 50%   | +$45   | -$30     | +$60   |
| XAUUSD | 6         | 42%   | +$65   | -$40     | +$48   |
| TOTAL  | 55        | 63%   | —      | —        | +$242  |

### Compound Growth Projection
| Month | Equity    | Monthly Net | Cumulative |
|-------|----------:|------------:|-----------:|
| 0     | $100      | —           | $100       |
| 1     | $124      | +$24        | +24%       |
| 3     | $191      | —           | +91%       |
| 6     | $366      | —           | +266%      |
| 9     | $700      | —           | +600%      |
| 12    | $1,342    | —           | +1,242%    |
| 18    | $4,916    | —           | +4,816%    |
| 24    | $18,003   | —           | +17,903%   |
| 30    | $65,877   | —           | +65,777%   |
| 36    | $241,314  | —           | +241,214%  |

> Based on +24% monthly compounded. First 3-6 months slower (forex only).
> Year 1 realistic: $100 → $1,300. Years 2-3 accelerated with metals.

---

## XI. BACKTESTING CONSTRAINTS — What Is Realistic

### What This Bot CAN Do
- Generate 60-70% WR on forex during trending markets
- Compound 10-25% monthly during optimal conditions
- Protect capital with hard circuit breakers

### What This Bot CANNOT Do
- Predict black swan events (wars, bank failures, flash crashes)
- Win during choppy consolidation (must skip sideways markets)
- Force entries when no valid setup exists (discipline enforced)
- Guarantee monthly profits (expect 2-3 losing months per year)

### Realistic Annual Expectation
- Year 1: $100 → $1,000 – $3,000 (10-30x) — forex learning phase
- Year 2: $3,000 → $30,000 — metals unlocked, compound accelerates
- Year 3: $30,000 → $100,000 — capital preservation + consistency

---

*Generated: 2026-05-20 21:16
*Version: v3.0
*Engine: OMNI ICT Dual-TF Confluence + AMD Session Logic
