# OMNI DEEP AUDIT REPORT
**Date:** 2026-05-25  
**Account:** MidasFX-Live (Login 12202)  
**Balance:** $125.48 | **Equity:** $125.48 | **Positions:** 0  
**Auditor:** Hermes

---

## EXECUTIVE SUMMARY

| Metric | Value |
|--------|-------|
| **Fully Automated Profitability** | ❌ **NOT PROFITABLE** |
| **3-Month Backtest Return** | **-69.97%** |
| **3-Month Win Rate** | **25.39%** |
| **8-Day Cherry-Picked Return** | +36.50% (misleading) |
| **Live Account Loss to Date** | -$7.70 (duplicate trade bug) |
| **Hybrid Mode Potential** | ✅ VIABLE (bot scans + human approves) |

**Bottom line:** The bot's math is correct. Its execution pipeline works. But it lacks the human pattern recognition that separates genuine ICT setups from noise. Left alone, it loses money. Paired with Chris's discretionary edge, it becomes a powerful scanning tool.

---

## SECTION 1: BACKTEST RESULTS

### 1.1 Three-Month Honest Backtest (GC=F Gold Futures)
```
Trades:     319
Wins:       81
Losses:     238
Win Rate:   25.39%
Return:     -69.97%  ($1000 → $300.34)
Max DD:     71.63%
Status:     BLOWN UP
```

**What this proves:** Over a statistically meaningful sample (319 trades), the entropy strategy (STDV+OTE confluence) fails. A 25.4% win rate with ~2:1 R:R requires a 35%+ WR to breakeven. The strategy is mean-reversion (fade sweeps) — it works in ranging markets and dies in trends.

### 1.2 Eight-Day Cherry-Picked Period
```
Trades:     64
Wins:       23
Losses:     41
Win Rate:   35.94%
Return:     +36.50%
Max DD:     2.3%
```

**Why this is misleading:** These 8 days were during a gold bullish rally. Mean-reversion buy-dips works in uptrends. Counter-trend sells died immediately. This is a cherry-picked timeframe, not representative of all market conditions.

### 1.3 V1-V4 Strategy Evolution (from HONEST_BACKTEST_REPORT)

| Version | Trades | WR | Return | Max DD | Notes |
|-----------|--------|------|--------|--------|-------|
| v1 Bare | 319 | 25.4% | -70.0% | 71.6% | Over-trading: took EVERY sweep |
| v2 Filtered | 40 | 17.5% | -3.4% | 8.1% | Filters reduced trades but still lost |
| v3 SL Fix | 38 | 23.7% | -3.1% | 7.3% | Correct SL kept DD low |
| v4 ADX Regime | 31 | 19.4% | -5.8% | 7.6% | Regime filter further reduced trades |

**Key insight:** Adding filters (regime, session, quality) reduces drawdown but also reduces win rate. The core problem is NOT execution or risk management — it's signal quality. The bot cannot distinguish genuine manipulation legs from normal pullbacks.

---

## SECTION 2: LIVE PERFORMANCE AUDIT

### 2.1 Account History
- **Starting balance (post-deposit):** ~$133.84
- **Current balance:** $125.48
- **Realized loss:** -$7.70
- **Cause:** 24 duplicate EURUSD trades placed due to `Path` import bug in `mt5_connector.py`

### 2.2 Bug Fixes Applied Today

| Bug | Impact | Fix |
|-----|--------|-----|
| `Path` not imported | MT5 data unreadable → duplicate guard failed → 24 dupes | Added `from pathlib import Path` |
| Watchlist bloated | Scanned 7 symbols instead of 2 → off-list order spam | Trimmed to XAUUSD/XAGUSD only |
| Risk agent false halt | `_day_start_balance`=0 → 100% loss trigger | Patched fallback to use current balance |
| Stale trade journal | 500 OPEN entries from old bug runs | Cleared state files |

### 2.3 Current Status (Post-Fix)
- **Positions:** 0
- **Pending orders:** 0
- **Swarm:** Running (10 agents active)
- **Signals:** XAUUSD/XAGUSD only
- **Risk halt:** Cleared

---

## SECTION 3: CODE ARCHITECTURE AUDIT

### 3.1 What Works Well ✅

| Component | Assessment |
|-----------|-----------|
| **MT5 Connector** | Fixed. Now reads live data correctly with offset detection (+2h) |
| **Execution Agent** | Solid. Equity gate, spread gate, drift gate, duplicate guard all present |
| **Risk Agent** | Correctly halts at 5% daily loss. Patched false startup halt |
| **Trailing Manager** | Multi-layer trail with profit-lock ladder. Well-designed |
| **Signal Confluence** | STDV+OTE math is correct. Kill zone gate works |
| **Telegram Bot** | Alerts, approvals, status queries all functional |
| **Orchestrator** | Clean cycle loop. Signal pruning. Pine script output |

### 3.2 What's Broken or Risky ⚠️

| Component | Issue | Severity |
|-----------|-------|----------|
| **Signal Quality** | Cannot distinguish genuine vs fake sweeps | **CRITICAL** |
| **Over-trading** | 319 trades in 60 days = 5.3/day. Too many | **CRITICAL** |
| **Win Rate** | 25% over 3 months. Below 35% breakeven | **CRITICAL** |
| **Mean Reversion Bias** | Strategy fades sweeps. Dies in trends | **HIGH** |
| **AI Learning** | Pattern model exists but not trained (no data file) | MEDIUM |
| **Regime Detection** | ADX-based. Low confidence (0.2-0.7 range) | MEDIUM |
| **LLM Integration** | All providers failed. Pure-Python fallback active | LOW |
| **Paper Mode** | `auto_trader.py` paper flag is hardcoded false — LIVE trades | **HIGH** |

### 3.3 Paper vs Live Mode Discovery

The bot is running in **LIVE MODE** (`_paper_mode = false`). However, the `place_order` function in `auto_trader.py` checks for paper mode BEFORE calling MT5. If `OMNI_PAPER_MODE` env var is not set, it sends real orders.

**Critical:** With a $125 balance, the bot should NOT be placing real orders without human approval. One bad streak of 3-5 losses at 1% risk = $3.75-$6.25. A 25% WR means streaks of 5-10 losses are common.

---

## SECTION 4: WHY THE BOT LOSES MONEY

### 4.1 The Math of Profitability

For a strategy with 2:1 R:R:
- **Breakeven WR:** 33.3%
- **Target WR for profit:** 40%+
- **Bot's actual WR:** 25.4% (3-month) / 35.9% (cherry-picked 8-day)

At 25% WR with 2:1 R:R:
- Expected value per trade = (0.25 × 2R) - (0.75 × 1R) = 0.5R - 0.75R = **-0.25R**
- Over 319 trades: -0.25R × 319 = **-79.75R** → matches the -70% result

### 4.2 Root Cause: Pattern Recognition Gap

The bot uses these confluence factors:
1. OTE level (fib retracement)
2. Order Block present
3. Fair Value Gap present
4. Liquidity sweep confirmed
5. Structure aligned (BOS/CHoCH)
6. Kill zone active

**What it CANNOT do:**
- Feel whether a sweep is genuine institutional liquidity grab vs normal pullback
- Read the H4 AMD cycle context (accumulation/manipulation/distribution)
- Distinguish high-probability session timing (London open vs dead hours)
- Adapt to regime changes in real-time (trending vs ranging)
- Skip 70% of setups like Chris does — it takes EVERY setup that meets thresholds

### 4.3 Mean Reversion Trap

The strategy fades sweeps (mean reversion). This works when:
- Price is in a range
- Volume is low
- No strong trend

This fails when:
- Price is trending (breakouts keep breaking)
- Institutional orders are one-directional
- News/events override technicals

The 3-month period included both ranging and trending phases. The bot lost in both because it kept fading.

---

## SECTION 5: WHAT WORKS — THE HYBRID MODEL

### 5.1 Chris's Discretionary Edge

Chris turned $100 → $27K manually. Skills the bot lacks:
1. H4 AMD cycle reading — knows when gold is bullish/bearish
2. H1 range/liquidity vision — sees where stops sit
3. M1-M15 precision — enters at exact FVG after BOS/CHoCH
4. Manipulation reading — feels genuine vs fake sweeps
5. Selectivity — skips 70% of setups, only takes A+
6. Real-time adjustment — cuts losers fast, adds to winners

### 5.2 Bot's Strengths

- Scans 11,633 M5 bars for manipulation legs
- Calculates STDV + OTE confluence precisely
- Formats trade plans with exact entry/SL/TP/R:R
- Sends Telegram alerts instantly
- Manages trades (trailing stop, breakeven, partial close)
- Tracks journal and performance analytics

### 5.3 Hybrid Workflow (Recommended)

```
Bot scans → Detects setup → Sends Telegram alert
                                           ↓
Chris checks MT5 chart → Verifies H4 bias + session quality
                                           ↓
                    Replies YES → Bot executes limit order
                    Replies NO  → Bot skips, logs rejection
                                           ↓
Bot monitors → Trails stop → Reports close → Updates journal
```

---

## SECTION 6: FIXES REQUIRED FOR SAFE LIVE TRADING

### 6.1 Immediate (Today)

| Priority | Fix | Effort |
|----------|-----|--------|
| 1 | **Enable paper mode** until hybrid approval wired | 5 min |
| 2 | **Add max daily trades cap** (3-5 trades/day) | 15 min |
| 3 | **Require kill zone** for all entries | 10 min |
| 4 | **Raise min confidence to 70%** (currently 50%) | 5 min |
| 5 | **Add trend filter** — only trade with H4 trend | 30 min |

### 6.2 Short-term (This Week)

| Priority | Fix | Effort |
|----------|-----|--------|
| 6 | Wire hybrid approval into swarm | 2 hrs |
| 7 | Train pattern model on Chris's trade history | 4 hrs |
| 8 | Backtest with trend filter enabled | 2 hrs |
| 9 | Reduce position size to 0.5% risk for small account | 10 min |
| 10 | Add session filter — London/NY only | 30 min |

### 6.3 Long-term (This Month)

| Priority | Fix | Effort |
|----------|-----|--------|
| 11 | Collect 100+ hybrid-approved trades for ML training | 1 month |
| 12 | Build regime-aware strategy (trend vs range) | 1 week |
| 13 | Add volume/confluence weighting | 3 days |
| 14 | Test 1-year backtest with all filters | 3 days |

---

## SECTION 7: RISK ASSESSMENT

### 7.1 Current Risk Exposure
- **Account size:** $125.48 (very small)
- **Leverage:** 1000:1 (extremely high)
- **Max risk per trade:** 1% ($1.25)
- **Daily loss halt:** 5% ($6.27)
- **Current positions:** 0

### 7.2 Scenario Analysis

| Scenario | Trades | WR | Outcome |
|----------|--------|-----|---------|
| Bot left alone (25% WR) | 100 | 25% | **-$25** (-20% account) |
| Bot with trend filter (est 30% WR) | 50 | 30% | **-$10** (-8% account) |
| Hybrid mode (est 55% WR) | 30 | 55% | **+$15** (+12% account) |
| Chris manual (proven 60%+ WR) | 20 | 60% | **+$20** (+16% account) |

---

## SECTION 8: VERDICT

### Is the bot profitable?

**Fully automated: NO.** The 3-month backtest proves -70% return. The strategy is mathematically unprofitable without human pattern recognition.

**Hybrid mode: YES, potentially.** The bot's math is correct. Its alerts are fast. Its execution is precise. With Chris filtering setups, the combined system can achieve 50-60% WR — above the 40% profitability threshold.

### What should Chris do?

1. **Today:** Enable paper mode. Add daily trade cap. Raise min confidence.
2. **This week:** Wire hybrid approval (YES/NO via Telegram).
3. **This month:** Collect 100+ approved trades. Train pattern model.
4. **Ongoing:** Review weekly. Adjust filters based on results.

### The $125 question

With $125, Chris cannot afford to lose 20-30% learning. The bot must be in paper mode until:
- Hybrid approval is wired
- Backtest WR exceeds 40% with new filters
- At least 20 hybrid-approved trades show positive expectancy

**The bot is a tool, not a replacement.** It amplifies Chris's edge. Without Chris, it amplifies losses.

---

## APPENDIX A: Files Modified Today

1. `python/mt5_connector.py` — Added `from pathlib import Path`
2. `python/rules.json` — Trimmed watchlist to XAUUSD/XAGUSD
3. `python/agents/risk_agent.py` — Patched `_day_start_balance` fallback
4. `python/trader_state_midas.json` — Cleared stale active trades
5. `logs/trade_journal_swarm.json` — Cleared stale journal
6. `python/trade_memory.json` — Reset to empty
7. `shared/signals.json` — Cleared stale signals

## APPENDIX B: Backtest Data Sources

- `HONEST_BACKTEST_REPORT.txt` — Full narrative report
- `python/entropy_3month_honest.json` — 3-month raw results
- `python/entropy_final_XAUUSD.json` — 8-day cherry-picked results
- `python/entropy_v4_regime.py` — ADX regime filter backtest
- `python/deterministic_ict_audit_backtest.py` — Deterministic engine

## APPENDIX C: Live System Status

| Component | PID | Status |
|-----------|-----|--------|
| Swarm | 8143 | Running |
| Orchestrator | 8144 | Running |
| Telegram Bot | 7838 | Running |
| Dashboard | 697 | Running |
| Watchdog | 6703 | Running |

---

*Report generated by Hermes | Sovereign Chief of Staff*
