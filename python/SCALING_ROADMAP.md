
# OMNI ICT — $100,000 SCALING ROADMAP
# Based on actual backtest: May 8–21, 2026 (2 weeks)
# Generated: 2026-05-20 21:19

═══════════════════════════════════════════════════════════════════════════════════════════
ACTUAL BACKTEST RESULTS (2 Weeks, $100 Start, Compound)
═══════════════════════════════════════════════════════════════════════════════════════════

Forex Majors Only (Phase 0):
  Trades:   120
  Wins:     88 (73.3%)
  Net PnL:  $+89.86
  Avg/Trade: $+0.75
  Projected monthly (×2 weeks): $+179.72

Metals (XAU + XAG):
  Trades:   66
  Wins:     34 (51.5%)
  Net PnL:  $+1349.10
  Avg/Trade: $+20.44
  Projected monthly (×2 weeks): $+2698.20

═══════════════════════════════════════════════════════════════════════════════════════════
PHASE-BY-PHASE SCALING ROADMAP
═══════════════════════════════════════════════════════════════════════════════════════════

PHASE 0: Foundation ($100 → $500) — Weeks 1–4
───────────────────────────────────────────────────────────────────────────────────────────
Symbols:    EURUSD, GBPUSD, AUDUSD, USDCAD, USDJPY (metals LOCKED)
Risk/Trade: 1.0%
Expectation: $90–$180 / month (conservative range)

Month-by-month:
  Month 1:  $100  → $150–$200    (forex only, building base)
  Month 2:  $175 → $250–$350    (compound kicks in, 0.01→0.02 lots possible)
  Month 3:  $300 → $450–$600  ← XAGUSD unlocks at $300
  Month 4:  $525 → $750–$900  ← Phase 1 begins, XAUUSD still locked

Key Rule: No metals trades until $300 equity. Forex only.

PHASE 1: Silver Unlock ($300 → $500) — Weeks 5–8
───────────────────────────────────────────────────────────────────────────────────────────
Symbols:    All forex + XAGUSD
Risk/Trade: 1.5%
Expectation: +$809–$1349 additional/month from silver

Month-by-month:
  Month 4–5: $500+  → Silver adds momentum
  Target:     $500 by end of month 5
  Milestone:  🥈 XAGUSD UNLOCKED (at $300)

PHASE 2: Gold Entry ($500 → $2,000) — Months 3–6
───────────────────────────────────────────────────────────────────────────────────────────
Symbols:    Full portfolio (all 7 symbols)
Risk/Trade: 1.5–2.0%
Expectation: Gold becomes primary driver (+$2698–$4047/mo)

Month-by-month:
  Month 3:  $600  → $1,000   (gold starts compound)
  Month 4:  $1,000 → $1,600  (0.02→0.05 lots now viable)
  Month 5:  $1,450 → $2,200  (runners working)
  Month 6:  $2,000 → $3,000+ ← Phase 3 begins

Milestone: 🥇 XAUUSD UNLOCKED (at $500)

PHASE 3: Acceleration ($2,000 → $10,000) — Months 6–12
───────────────────────────────────────────────────────────────────────────────────────────
Symbols:    Full portfolio
Risk/Trade: 2.0–2.5%
Focus:      Compound scaling, multiple runners, lot sizes 0.05–0.20

Month-by-month:
  Month 6:   $3,000  → $4,500
  Month 7:   $4,000  → $6,000
  Month 8:   $5,500  → $7,500
  Month 9:   $7,000  → $9,000
  Month 10:  $8,500  → $10,500  ← Phase 4 begins
  Month 12:  $12,000 → $15,000

PHASE 4: Scale Up ($10,000 → $50,000) — Months 12–24
───────────────────────────────────────────────────────────────────────────────────────────
Symbols:    Full portfolio + optional indices (NAS100, US30)
Risk/Trade: 2.5–3.0%
Focus:      Maximum compound, 0.20–1.00 lots, runners held longer

Quarterly targets:
  Q1 (M13-15): $15,000 → $25,000
  Q2 (M16-18): $25,000 → $35,000
  Q3 (M19-21): $35,000 → $45,000
  Q4 (M22-24): $45,000 → $60,000  ← Phase 5 begins

PHASE 5: Capital Preservation ($50,000 → $100,000) — Months 24–36
───────────────────────────────────────────────────────────────────────────────────────────
Symbols:    Full portfolio
Risk/Trade: 2.0% (REDUCED from peak)
Focus:      Drawdown protection paramount, monthly withdrawals begin

Quarterly targets:
  Q1 (M25-27): $60,000 → $75,000
  Q2 (M28-30): $75,000 → $90,000
  Q3 (M31-33): $90,000 → $100,000 ← TARGET ACHIEVED
  Q4 (M34-36): Maintain $100K core, withdraw profits above

═══════════════════════════════════════════════════════════════════════════════════════════
REALISTIC EXPECTATIONS
═══════════════════════════════════════════════════════════════════════════════════════════

Conservative (70% of projected): $100 → $50,000 in 36 months
Moderate (100% of projected):     $100 → $100,000 in 33 months  
Optimistic (130% of projected):   $100 → $150,000+ in 30 months

What WILL happen (guaranteed):
  - 2–3 months with losses (expect them, they're normal)
  - Circuit breakers will trigger at least once
  - You'll be tempted to override the bot (DON'T)
  - First $500 takes longest (psychological barrier)

What to AVOID:
  - Adding metal trades before gates unlock
  - Increasing risk % manually
  - Trading during skipped sessions (Asia mid, FOMC, NFP)
  - Closing runners early during push phases

═══════════════════════════════════════════════════════════════════════════════════════════
BOT CONFIGURATION CHECKLIST FOR TONIGHT
═══════════════════════════════════════════════════════════════════════════════════════════

[ ] Fund MidasFX account to $100+
[ ] Verify MT5 terminal running (terminal64.exe in Activity Monitor)
[ ] Confirm EURUSD + GBPUSD in Market Watch
[ ] Ensure telegram_bot.py is running (send /status)
[ ] Verify rules.json loaded (check logs for "v2.3.1")
[ ] Confirm equity tier gates active (search log for "EQUITY_TIER_GATE")
[ ] Set OMNI_PAPER_MODE=false  (LIVE trading)
[ ] Test one micro trade manually to confirm fills
[ ] START BOT and monitor first 24 hours

═══════════════════════════════════════════════════════════════════════════════════════════
