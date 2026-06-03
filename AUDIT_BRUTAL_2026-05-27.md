# OMNI BOT — BRUTAL AUDIT REPORT
**Generated:** 2026-05-27T08:15 UTC  |  **Auditor:** Hermes Agent

---

## EXECUTIVE SUMMARY

**RATING: SYSTEM IS FUNCTIONALLY BROKEN. FIVE CRITICAL FAILURES.**

The bot generates signals. It does NOT trade correctly. It has NOT traded successfully in at least 2 days. The trailing stop is a death trap. Telegram is dead. The server is failing. Data timestamps are in the wrong format. You are flying blind.

| Tier | Count | Impact |
|------|-------|--------|
| P0 (Account Destroyer) | 3 | Immediate money risk |
| P1 (Blindness) | 2 | No visibility into system |
| P2 (False Confidence) | 3 | You think it's working, it's not |
| P3 (Systemic) | 3 | Structural rot underneath |

---

## FINDING 1 — TRAILING STOP: ACTIVE MONEY-TRAP (CRITICAL)

**Problem:** The "smart" trailing stop is NOT smart. It is a reactive position killer.

**Root Cause A:** `smart_trailing_stop.py` contains:
```
profit_lock_ladder = ((1.0, 0.0), (2.5, 0.30), ...)
```
`(1.0, 0.0)` means: at 1R peak, locked_frac = 0.0% give-back = SL jumps to THE EXACT PEAK PRICE. A $0.01 pullback from peak = STOPPED OUT.

The comment says "NO lock until 1R" — but the IMPLEMENTATION locks tighter than anything at 1R. Not a trail. A booby trap.

**Root Cause B:** Dual pip_size warfare:
- `smart_trail_adapter.py`: pip_size = 0.01 for XAUUSD
- `position_trailing_manager.py`: pip_size = sym_info.point × 10 = 0.10
- TrailConfig.min_modify_pips: 25.0

Adapter pushes noise-level $0.25 moves. Manager wants $2.50+. They fight. Result: erratic jumps.

**Root Cause C:** TWO competing trailing stop modules exist:
- `smart_trailing_stop.py` (v1, 16KB)
- `smart_trailing_stop_v28.py` (v28, 12KB — UNUSED)

`position_trailing_manager.py` imports `smart_trail_adapter` → calls `smart_trailing_stop.py` (v1). v28 sits dead. Classic code archaeology.

**Root Cause D:** No backtested trail behavior. Your 468% raw / 15% reality backtest uses simplified fill model. It does NOT simulate the actual trail adapter's stop logic. The backtest CANNOT catch this bug.

**Evidence:** Swarm.log shows Ticket 13898xx:
- 14:56:58: Trail SL 4484.84 → 4508.06 | ERROR: Invalid stops (4756)
- 14:57:28: Trail SL 4484.84 → 4508.38 | ERROR: Invalid stops
- 14:57:43: Trail SL 4484.84 → 4508.81 | ERROR: Invalid stops
- 14:57:58: Finally accepted SL → 4508.63
- Position closed shortly after — consistent with "stops getting stopped moving into profits"

**Why This Matters:** Every position you take is being murdered by its own protection. You pay spread + swap to enter. The trail stops you on a $0.50 pullback before any real profit. The bot literally cannot hold a winner open.

**Verdict:** This component is actively destroying edge. NOT a minor bug.

---

## FINDING 2 — EXECUTION PIPELINE: DOES NOT TRADE (CRITICAL)

**Problem:** The bot emits signals. They never become MT5 orders.

**Evidence:**
- `trade_memory.json`: **0 RECORDS. Empty.**
- Last recorded MT5 closes in `omni_data.json`: **2026-05-25**
- `swarm.log` shows 22,639 "execution events" — ALL agent START messages, not actual order placements.
- No "LIVE", "PAPER", "ticket", or "placed" entries in recent swarm logs.

**Root Cause A:** Equity tier gate:
- `execution_agent.py` checks `equity_gate.min_equity_usd`
- Your account: **$125.48**
- Gate was at **$500** (possibly still is despite attempted patch)
- Result: EVERY signal rejected silently

**Root Cause B:** No fallback for small accounts. Even with 0.01 lots and $1.25 risk, gate says NO. Built for $500+ accounts. Never adjusted for sub-$200 reality. Deployment mismatch.

**Root Cause C:** Signal expiry: signals expire after 360 minutes. Since no execution, signals age and die, then new ones regenerate. Loop: signal → reject → expire → resignal. Looks active in dashboard. Nothing happens.

**Root Cause D:** `trader_state.json`:
- `total_trades`: 0 since last reset (2026-05-25)
- `win_streak`: 0, `loss_streak`: 0
- State machine has never run.

**Why This Matters:** You think you have a live trading bot. You have a signal generator that writes JSON files and an MT5 EA that reads stale data. There is NO EXECUTION. You are NOT trading.

**Verdict:** Bot is in simulation-as-reality mode.

---

## FINDING 3 — DATA PIPELINE: TIMESTAMP FORMAT MISMATCH (CRITICAL)

**Problem:** MT5 writes timestamps as strings. Python expects integer epoch.

**Evidence:**
- `omni_data.json` (from Wine MT5): `"timestamp": "2026.05.27 11:08:20"` ← STRING
- `orchestrator.py`: checks `isinstance(ts, (int, float))`
- Result: Python detects the string → falls back to stale cached data
- System believes it has fresh data when it does not.

**Why This Matters:** All confluence scoring, AMD phase detection, manipulation leg detection depend on bar timestamps. If timestamps are strings, chronological ordering, kill zone detection, session filtering may silently produce WRONG results. The engine runs on fake time.

**Verdict:** Data pipeline compromised. Timestamps tell the system what time it is. If broken, entire decision chain is wrong-timed.

---

## FINDING 4 — TELEGRAM BOT: CRASH LOOP (HIGH)

**Problem:** `telegram_bot.py` crashes on startup. Every restart = another crash.

**Evidence:**
- `logs/telegram_bot.log`: 567 errors, 167 traceback blocks, same pattern
- Same error every ~5-30 minutes for days:
```
Traceback (most recent call last):
  File "telegram_bot.py", line 1781, in <module>
    main()
  File "telegram_bot.py", line 1672, in main
    monitor = AlertMonitor(chat_id) if chat_id else None
```
- Never reaches main loop. AlertMonitor constructor fails.

**Why This Matters:** You rely on Telegram for live trade alerts, fills, notifications. You have NOT been getting them for at least days. You might have trades you don't know about, or no trades at all — either way, you're blind.

**Verdict:** Communication layer down. Safety-critical system with no external monitoring.

---

## FINDING 5 — SERVER: PORT BIND FAILURE (MEDIUM)

**Problem:** `server.py` tries to bind `127.0.0.1:8787`. Already in use.

**Evidence:**
- 1,923 ERROR lines: "[Errno 48] error while attempting to bind..."
- Old process from previous restart still holds port.
- New process launches, fails to bind, then persists as zombie.

**Why This Matters:** Web dashboard depends on this server. External systems (webhook, health checks) cannot connect. Process consumes resources while appearing healthy to launchd.

**Verdict:** Resource leak + failed health endpoint. Bot looks alive. It is not.

---

## FINDING 6 — STRATEGY ENGINE: OVER-ENGINEERED, UNDER-VALIDATED (HIGH)

**Problem:** 10+ confluence criteria across 8+ files, AMD on 3 timescales, but NO unit tests for actual execution path.

**Evidence:**
- `test_confluence_engine.py` tests isolated functions
- `test_smart_trailing_stop.py` tests fixed scenarios
- No integration test: MT5 data → signal → trail → fill → close
- No test for timestamp parsing across MT5/Python boundary
- No test that trade actually reaches MT5 with correct lot size

**Current signal quality:**
```
SIGNAL XAUUSD BEAR | confluence=4/8 | conf=0.73
  [OK] C3_FVG_PRESENT
  [OK] C4_SWEEP_CONFIRMED
  [OK] C5_STRUCTURE_ALIGNED
  [OK] C6_KILLZONE_AMD
  [FAIL] C1_OTE_LEVEL        — price not at OTE
  [FAIL] C2_OB_PRESENT       — no unmitigated OB
  [FAIL] C7_Micro AMD        — in MANIPULATION phase
  [FAIL] C8_Redistribution   — cycle not complete
```
4/8 passes with 0.73 confidence — but ONLY because C6 gives +0.10 for London open during "MANIPULATION" phase ("watch for sweep confirmation"). This is marginal. You're trading noise.

**Verdict:** More code ≠ more edge. No verified path from MT5 tick to filled MT5 order. Every layer adds failure modes.

---

## FINDING 7 — BACKTEST: OPTIMISTIC AND INADEQUATE (HIGH)

**Problem:** The 468% raw / 15% reality backtest is useless for deployment decisions.

**Evidence:**
- 17 days of data → ~29 raw signals, 12 filled → tiny sample
- No walk-forward validation
- No regime shift detection (2022-2024 macro ≠ 2025-2026)
- Commission estimated at $10/lot (not verified from actual broker)
- Slippage estimated, not measured from execution
- Trail stop behavior NOT simulated — biggest cost driver is untested

**Why This Matters:** You are a profitable manual trader ($100→$27K). The bot does NOT replicate your decision process. Your manual edge is in reading structure in real-time and selectively skipping 70% of setups. The bot skips nothing — it scores every noise fluctuation. And when it does score, the trail kills it.

**Verdict:** Backtest is theatre. It tells you what you want to hear.

---

## RANKED CORRECTION PLAN

### P0 — FIX BEFORE ANY MONEY IS AT RISK

**P0.1: Trailing Stop Rewrite**
- **What:** Replace `(1.0, 0.0)` with `(1.0, 0.50)` in `smart_trailing_stop.py`
- **What else:** Align `smart_trail_adapter.py` XAUUSD pip_size to 0.10 (matches MT5)
- **What else:** Delete `smart_trailing_stop_v28.py` or make it canonical (pick ONE)
- **Why:** Every winning position currently dies on micro pullback
- **How:** Edit ladder → restart swarm → open test position in paper mode → verify trail holds

**P0.2: Execution Gate Fix**
- **What:** Lower `min_equity_usd` from $500 → $50 in `execution_agent.py`
- **What else:** Add explicit LOG entry every time a signal is rejected so rejection is visible
- **What else:** Fix rules.json to have gate per-symbol, not global $500
- **Why:** You have $125.48. The bot rejects every signal. You are not trading.
- **How:** Edit agent → restart swarm → watch for "EXECUTED" log entries

**P0.3: Timestamp Parser Fix**
- **What:** Add string timestamp parser to `orchestrator.py` MT5 data reader
- **Format:** `"2026.05.27 11:08:20"` → parse with `datetime.strptime` → compute age
- **What else:** If age > 5 minutes, reject data and log CRITICAL warning
- **Why:** Currently system runs on stale data silently.
- **How:** Patch fetcher, add parse logic, test with actual omni_data.json string

### P1 — RESTORE VISIBILITY

**P1.1: Telegram Bot Recovery**
- **What:** Fix AlertMonitor constructor error in `telegram_bot.py` line 1672
- **What else:** Add try/except around constructor with fallback to console-only mode
- **What else:** Verify `chat_id=5786598754` resolves correctly
- **Why:** You have zero trade alerts for days. On 1:1000 leverage this is dangerous.
- **How:** Read traceback → identify missing import/constructor arg → patch → restart

**P1.2: Server Port Recovery**
- **What:** Kill old process holding port 8787 before starting new server
- **Command:** `lsof -i :8787 | awk '{print $2}' | xargs kill -9` (or equivalent)
- **What else:** Add port-cleanup to launchd prep script
- **Why:** Dashboard is a dead UI showing stale data.
- **How:** One-shot kill + script modification

### P2 — VALIDATE BEFORE DEPLOYMENT

**P2.1: End-to-End Integration Test**
- **What:** Script that feeds known MT5 data → runs orchestrator → checks signal → runs execution agent → verifies "order would be sent" (paper mode)
- **What else:** Timestamp edge cases, weekend data, empty bars, stale data
- **Why:** No amount of unit tests prove the system works together.
- **How:** Create `test_end_to_end.py` → run in CI or manual

**P2.2: Honest Backtest (6+ months)**
- **What:** Run backtester with reality-adjusted fills + commission verification + slippage + trail simulation
- **What else:** Walk-forward by quarter (2022 Q1, Q2, Q3, Q4, 2023 Q1...)
- **Why:** Current 17-day test is a toy.
- **How:** Use MT5 tick data export, run `backtester.py` with trail module hooked in

**P2.3: Reconcile Python State with MT5**
- **What:** Script that queries MT5 open positions and compares to Python's expected open positions
- **Mismatch = CRITICAL alert**
- **Why:** Ghost positions on 1:1000 leverage are account-ending.
- **How:** Read MT5 `account` + `positions` from omni_data.json → compare to Python state

### P3 — STRUCTURAL

**P3.1: Delete or Deprecate `smart_trailing_stop_v28.py`**
- One canonical trailing stop module. Not two.

**P3.2: Write `/docs/ARCHITECTURE.md`**
- Signal flow, module dependencies, data formats, restart procedure

**P3.3: Add Reconciliation Loop**
- Every 60s: Python expected position count vs MT5 actual position count
- Discrepancy → instant Telegram + log CRITICAL

---

## WHAT WILL BE DIFFERENT AFTER FIXES

**BEFORE:** Signal → reject (equity gate) → trail would kill anyway → dashboard shows signal → Chris feels good
**AFTER:**  Signal → execute (adjusted gate) → trail holds winners → Telegram confirms fill → dashboard shows live P&L → account actually changes

**BEFORE:** You check dashboard → see BEAR signal at 4487 → feel good
**AFTER:**  You check dashboard → see position open, ticket #, SL at proper distance, TP target, running P&L, trail status

**BEFORE:** 0 trades in Python memory, blind to real account state
**AFTER:**  Every MT5 position reconciled with Python state. Discrepancy = instant alert.
