# OMNI BOT — DEEP AUDIT & GAP CHECK
**Date:** 2026-05-29 | **Scope:** full repo (`~/Omni-full-ALGO-Trading-Bot`, ~77k LOC Python)
**Verdict:** It now *executes* trades (the May-27 pipeline was fixed), but it is **trading live with an unproven edge, broken bookkeeping, no real learning loop, and reckless position sizing.** This is the most dangerous state a bot can be in: it *looks* alive and is quietly bleeding.

---

## 0. GROUND TRUTH (what is actually happening right now)

| Signal | Evidence | Reading |
|---|---|---|
| Account | MT5 `omni_data.json`: balance=equity=**$133.42**, 0 open positions, ready=True | Currently flat |
| P&L | root `trader_state.json`: total_profit **−$31.05**, peak $131.37 → equity $122.53 | Down ~20% from peak |
| Ghost state | `trader_state_midas.json`: **31** entries in `active_trades`; MT5 shows **0** open | State is NOT reconciled — 31 closed tickets never cleaned out |
| Counters | same file: `total_trades:0, winning:0, losing:0, day_initialized:false` | Trade accounting is dead while 31 "active" trades sit in memory |
| Learning | `swarm.log` every 2 min: *"200 trades, win_rate=43.0%, avg_r=+1.20, optimizer_accepted=False"* — identical every run | Learner runs on a frozen seed set; `trade_memory.json` = **0 records**. Not learning from real fills |
| LLM | `swarm.log`: *"llm: all providers failed — pure-Python fallback active"* repeatedly | Kimi/Ollama/OpenRouter all down — AI analyst is non-functional |
| Processes | Omni stack (swarm, orchestrator, telegram_bot, watchdog, dashboard) **+ Hermes** (`api_server.py`×2, `HERMES_TELEGRAM_DASHBOARD.py`) | Two trading/telegram systems coexisting — conflict risk |

**Net:** the May-27 P0 fixes worked (trail ladder `1.0,0.50`; equity gate 100/75; string-timestamp parsing all confirmed in code). But fixing execution just exposed the deeper problem — **there is no validated, profitable edge underneath it.**

---

## 1. CRITICAL (P0 — money at risk today)

### P0.1 — Position sizing is account-suicidal
`agents/execution_agent.py`:
- L353: `max_risk_pct = 0.30` → **30% of equity risked** on sub-$200 accounts
- L443: `kelly_risk_pct = 0.12` → **12% per trade**

This silently **overrides `config.json` (`max_risk_per_trade_pct: 5.0`)**. On $130 that's ~$15–40 at risk per position. 3–4 losers in a row = account gone. This is the mechanical cause of the −$31 bleed. **No bot on a $130 live account should risk >1–2% per trade.**

### P0.2 — `max_positions: 2` is not enforced on the live path
`config.json` says max 2; state shows **31** active. The cap lives in `auto_trader.py`, but **live execution runs `swarm.py` → `execution_agent.py`**, which has no concurrent-position ceiling I can find. Combined with P0.1, the bot can stack many oversized correlated XAU positions at once = uncontrolled aggregate risk.

### P0.3 — No state reconciliation on the live path = ghost positions
`state_reconciler.py` exists but is only imported by `auto_trader.py` (not the live swarm). Result: 31 closed trades remain "active" in `trader_state_midas.json`. The bot's view of its own exposure is **fiction**. On 1:1000 leverage, acting on a wrong position count is how accounts die. This is the *exact* P3.3 item from the May-27 audit — still not done.

### P0.4 — `smart_trail_adapter.py:36` still defaults to the death-trap ladder
The ladder was fixed in `smart_trailing_stop.py` (`1.0, 0.50`), but the adapter's **fallback** is still `[[1.0, 0.0], ...]` (lock SL at exact peak). If config ever fails to supply the ladder, every winner dies on a $0.01 pullback again. Latent landmine.

---

## 2. THE EDGE PROBLEM (the thing that actually decides profitability)

Your own `HONEST_BACKTEST_REPORT.txt` already proved it, honestly:

| Version | Trades | WR | Return | Status |
|---|---|---|---|---|
| v1 bare | 319 | 25.4% | **−70%** | blown up |
| v2–v4 filtered | 31–40 | 17–24% | −3 to −6% | "safe" (i.e. slow bleed) |

- **17–25% WR is below break-even.** You need >40% WR at 2:1 to profit. None of the automated variants clear it.
- The strategy is **mean-reversion (fade sweeps)** — it works in ranges, dies in trends. Across all regimes it's negative.
- The `online_learner`'s rosy "43% WR / +1.20R" is **a frozen seed dataset**, not live results — `trade_memory` is empty, so the optimizer is hallucinating an edge that the real fills don't show.

**Hard truth:** the fully-automated bot does not have a demonstrated edge. The May-21 report's own recommendation — **hybrid (bot scans, you approve)** — is the only configuration with evidence behind it (your $100→$27K is discretionary). That conclusion still stands and has not been implemented.

---

## 3. STRUCTURAL ROT — code sprawl

- **25 backtest scripts, 12 "engine" files, 20 strategy files.** Most are abandoned experiments (`entropy_v1..v4`, `deterministic_ict_*`, `ultra_proven_ict_engine` + `_v3`, `real_amd_fvg`, `final_amd_fvg`, …).
- `ict_precision.py` alone is **207 KB**. `auto_trader.py` 161 KB, `dashboard.py` 139 KB.
- The **live path actually uses only 4 modules**: `smc_engine` → `dual_tf_selector` → `scaling_engine` → `amd_engine` (per `orchestrator.py` imports). Everything else is noise that makes the system unmaintainable and hides bugs (you cannot reason about 77k LOC).
- Two parallel trailing-stop modules, two trader entrypoints (`auto_trader.py` vs `swarm.py`), two telegram/dashboards (Omni + Hermes). **Pick one of each. Delete the rest.**

---

## 4. GAP CHECK — what's missing to be profitable

| Gap | Why it blocks profit |
|---|---|
| **Validated edge** | No walk-forward, multi-regime, out-of-sample test that clears costs. Everything is in-sample or toy-length (17–60 days). |
| **Realistic backtest** | No simulation of the actual trail logic, real broker commission/swap, or slippage measured from fills. The "+2045%" scale backtest is fantasy. |
| **Live↔MT5 reconciliation loop** | Built but not wired to the live path. |
| **Real learning loop** | `trade_memory` empty; learner runs on seed data. No closed-trade → feature → model feedback. |
| **Position & exposure caps on live path** | No concurrent cap, no per-symbol aggregate risk, no correlation guard (all eggs in XAU). |
| **Kill switch / daily loss halt that actually fires** | `trading_halted:false`, daily counters never initialize. |
| **Hybrid approval mode** | Your only evidenced edge (you in the loop) isn't implemented. |
| **Working alerting** | LLM down; need to confirm Telegram fills/halts actually deliver. |

---

## 5. ACTION PLAN

### CHANGE NOW (before next London session — these are bleeding money)
1. **Cap risk: 1% per trade, hard.** Replace the 0.30 / 0.12 risk constants in `execution_agent.py` with `min(equity*0.01, config cap)`. No exceptions for "small account" — small account = *smaller* risk, not bigger.
2. **Enforce max concurrent positions = 1** on the live path (you have $133; one position at a time). Reject new signals while any position/pending is open.
3. **Wire `state_reconciler` into `swarm.py`**, run every loop: compare Python `active_trades` to MT5 `positions`; purge ghosts; alert on mismatch. Clean the 31 stale entries now.
4. **Fix `smart_trail_adapter.py:36`** fallback default to `[1.0, 0.50]`.
5. **Add a real daily kill switch:** initialize `day_start_equity`, halt all new entries at −5% day or −10% from peak. Verify it fires.

### APPROVE (decisions only you can make)
- **Hybrid vs fully-auto.** Evidence says go hybrid (bot alerts → you reply YES/NO → bot executes & manages). Recommend enabling it. Fully-auto stays OFF until an edge is proven.
- **One stack.** Kill either Omni or Hermes for live trading + one Telegram bot. Which is canonical?
- **Account floor.** Do not scale risk up until balance is consistently green over 30+ real trades.

### ADD (to build toward real profitability)
1. **Honest walk-forward harness** (one script, replaces the 25): 2+ yrs XAU H1/M5, real commission+swap+slippage, *simulates the live trail logic*, reports by quarter. Strategy ships only if it's green out-of-sample after costs.
2. **Real trade-memory loop:** every closed MT5 trade → `trade_memory` → features → learner. Kill the seed dataset.
3. **Regime filter** (ADX/trend) so the mean-reversion logic stands down in strong trends (where it loses).
4. **Correlation/exposure guard** (XAU and XAG move together — treat as one risk bucket).
5. **`docs/ARCHITECTURE.md`** documenting the *one* live path; then archive `python/_archive` candidates aggressively.

---

## 6. ONE-LINE BOTTOM LINE
The plumbing is finally connected — which means the bot is now live with **30% risk per trade, no position cap, ghost bookkeeping, a frozen fake learner, and a strategy your own honest backtest shows loses money.** Stop the bleeding (Section 5 "Change Now") today, switch to hybrid, and do not trust any "% return" number until a costed walk-forward says so.
