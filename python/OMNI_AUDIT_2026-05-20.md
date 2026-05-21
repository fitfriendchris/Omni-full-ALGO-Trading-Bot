# OMNI ICT Bot Strategy Audit
## Date: 2026-05-20 | Data Period: ~13 Days (H1 bars: May 8 - May 21)

---

## Executive Summary

The OMNI ICT bot is **LIVE** (OMNI_PAPER_MODE=false confirmed). MT5 is now running.

However, the strategy has **critical systemic failures** that explain why a 60-94% backtest
win rate translates to **90% real-money losses** (30 trades, $2.51 equity remaining).

The #1 root cause is not the strategy logic — it's the **account balance.

---

## 1. LIVE PERFORMANCE (Reality Check)

| Metric | Value |
|--------|-------|
| Total Live Trades | 30 |
| Net PnL | **-$18.32** |
| Win Rate | **13.3%** (4 wins / 30) |
| Remaining Equity | **$2.51** |
| Leverage | 1:1000 |

### Per-Symbol Breakdown (real money trades since May 7):
| Symbol | Trades | WR | Net PnL | Avg Loss |
|--------|--------|-----|---------|----------|
| AUDUSD | 8 | 12.5% | -$5.70 | -$0.81 |
| GBPUSD | 10 | 10.0% | -$5.58 | -$0.62 |
| EURUSD | 8 | 25.0% | +$0.55 | ~$0 |
| USDCAD | 2 | 0.0% | -$2.09 | -$1.04 |
| USDJPY | 2 | 0.0% | -$5.50 | -$2.75 |

**Key Finding:** ALL XAUUSD/XAGUSD trades are MISSING from live history.
The bot fires signals for gold/silver but CANNOT execute them.

---

## 2. WHY THE BACKTEST SAYS 60-94% WR BUT REALITY SAYS 13%

### Backtest Results (13 days, H1 bars, current logic):
| Symbol | Trades | Backtest WR | Live WR | Discrepancy |
|--------|--------|-------------|---------|-------------|
| XAUUSD | 23 | 60.9% | **0%*** | Full miss |
| XAGUSD | 36 | 63.9% | **0%*** | Full miss |
| GBPUSD | 16 | 93.8% | 10.0% | Massive |
| USDCAD | 22 | 77.3% | 0.0% | Massive |

*No XAUUSD/XAGUSD trades in live history at all.

### Why Backtest is Misleading:

1. **H1-bar-only execution**
   - Replays at bar CLOSE, not at setup detection time
   - Misses intrabar wicks that hit SL before TP1 on lower TFs
   - Real entries happen inside bars, often far from close

2. **Spread modeling is optimistic**
   - `spread_points=2` (only 2 ticks) for XAUUSD
   - MidasFX spread = 58 points during active hours
   - 29x undermodeled — destroys expectancy on tight trades

3. **Partial exit logic is generous**
   - TP1 = 1.5R with 50% position closure
   - Guaranteed to close "profitable" on H1 close even when intrabar SL hit first
   - Real trading: SL often hits within minutes on M5 noise

4. **Small sample sizes (8-36 trades)**
   - 13 days is not statistically meaningful
   - Live: same 13-day period yielded catastrophic results

5. **1yr scale backtest (properly modeled) tells the truth:**
   - XAUUSD win rate = 17.8% with Avg Win = 6.2R, Avg Loss = -0.91R
   - Max drawdown = 60-88% depending on risk profile
   - Profit factor peaks at 1.25 — barely profitable

---

## 3. ROOT CAUSE: BALANCE = $2.51

| Asset | Min Lot | Margin Req (1:1000) | Min Equity |
|-------|---------|----------------------|------------|
| XAUUSD | 0.01 | ~$45.40 | $50+ |
| XAGUSD | 0.01 | ~$7.55 | $10+ |
| EURUSD | 0.01 | ~$1.17 | $2+ |
| GBPUSD | 0.01 | ~$1.36 | $2+ |

**Current equity ($2.51) is below the margin threshold for XAUUSD and XAGUSD.**
The bot signals them (they're primary symbols) but MT5 rejects the orders silently.
All capital went into forex pairs (EURUSD, GBPUSD, etc.), which have tighter spreads
but less edge in current model.

**The strategy is correct. The account is too small to trade it.**

---

## 4. CRITICAL STRATEGY LEAKS (Ranked by Impact)

### A. SPREAD SENSITIVITY (Impact: HIGH)
- XAUUSD spread = 58 points ($5.80/lot at 0.01, $58 at 0.1)
- Current SL placement puts entry often within 1 ATR of invalidation
- Spread alone can consume 30-50% of RR on small moves
- **Fix:** Require minimum ATR × 3 between entry and SL for XAUUSD/XAGUSD

### B. NO PROFIT-LOCKING (Impact: HIGH)
- Live trades exit at SL or close manually at near-breakeven
- No "once in profit, lock +10%" stop mechanism exists in live execution
- The profit-lock pattern (crypto bot standard) is MISSING here
- **Fix:** Implement tiered profit-lock: +10% unrealized → lock +5%, +50% → trail at 50%

### C. SIGNAL-TO-EXECUTION MAPPING GAP (Impact: HIGH)
- Signals say "entry_type=fvg_fill" at specific price
- Live execution likely uses market orders (slippage) instead of limit orders
- No "entry triggered" confirmation loop — just fires once and hopes
- **Fix:** Add limit order placement with retry logic + price proximity gate

### D. MACRO BIAS CONFLICT (Impact: MEDIUM)
- Signals show "macro TF conflict -0.30" repeatedly
- H4 bias says BULL but macro (D1/W1) says BEAR
- 30% confidence penalty is too small — should be a VETO below certain thresholds
- **Fix:** When macro disagrees with HTF AND account < $500, block all entries on that symbol

### E. NO BROKER OFFERED SYMBOL VALIDATION (Impact: MEDIUM)
- Config allows scanning all 7 symbols even if broker doesn't offer them
- GBPJPY skipped because "stale HTF bars" — actually NOT in Market Watch
- **Fix:** Filter watchlist to only broker-offered symbols

### F. CONFIDENCE THRESHOLD TOO LOW (Impact: MEDIUM)
- `min_confidence=55` allows marginal setups
- In a $2 account, only CONF >= 0.85 should fire
- **Fix:** Scale confidence threshold with equity: lower = higher bar

### G. CHOPPINESS FILTER IS REACTIVE, NOT PREVENTIVE (Impact: LOW)
- `_h1_choppy` detects AFTER chop happens, then applies 4-bar cooldown
- Should use ADX-based pre-filter to avoid low-vol sessions entirely
- **Fix:** Skip entries when ADX < 20 on H1

---

## 5. RECOMMENDED FIXES (Priority Ordered)

### IMMEDIATE (Do Now)
1. **Inject capital** — $2.51 cannot trade XAUUSD. Minimum $50-100 to open 0.01 lot.
2. **Add balance gate** — If `equity < margin_required * 2`, skip symbol entirely.
3. **Map spread dynamically** — Read `spread` from MT5 data file (it's exported per symbol) instead of hardcoding 2 points.

### SHORT-TERM (This Week)
4. **Implement profit-lock ladder**:
   - +1R unrealized → SL to breakeven
   - +2R unrealized → trail at 50% of peak
   - +5R unrealized → hard lock at +3R minimum
5. **Add entry price validation**:
   - Only execute if current bid/ask within 0.3 ATR of signal entry
   - Cancel stale signals after 2 candles
6. **Macro veto threshold**:
   - If macro_bias disagree AND equity < $500 → full block

### MEDIUM-TERM (Backtest-Validate First)
7. **Switch from H1-close to M5 intrabar simulation**
   - Replays M5 bars within H1 window for realistic SL/TP ordering
8. **Add ATR multiplier for SL minimum separation**
   - XAUUSD: SL must be >= 3 ATR from entry
   - Smaller forex pairs: >= 2 ATR
9. **Implement dynamic confidence scaling**
   - `effective_conf = raw_conf * (equity / 100)^0.5`
   - Small accounts need higher conviction

---

## 6. ACTION ITEMS

| # | Action | Owner | Priority |
|---|--------|-------|----------|
| 1 | Add `equity < 2x_margin` gate per symbol | Code | P0 |
| 2 | Read live spread from omni_data.json | Code | P0 |
| 3 | Implement profit-lock trailing stop | Code | P1 |
| 4 | Add entry price proximity check | Code | P1 |
| 5 | Raise macro disagree to VETO when low equity | Rules | P1 |
| 6 | Validate broker symbol availability | Code | P2 |
| 7 | Switch backtest to M5 intrabar | Backtest | P2 |
| 8 | Add ADX pre-filter (<20 skip) | Code | P2 |

---

*End of Audit*
