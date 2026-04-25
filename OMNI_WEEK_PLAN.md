# OMNI-ICT — Week Execution Plan

Dates: Thu Apr 16 — Wed Apr 22, 2026
Deliverable: all code written, unit-tested, wired, and backtested by EOW. Paper/demo validation starts the following week. **No live deployment this week.**

---

## Contents

1. [System Design — Phase 2 dual-TF SMC + scaling engine](#1-system-design)
2. [Architecture Decisions — ADRs for key choices](#2-architecture-decisions)
3. [Testing Strategy — what to test and how](#3-testing-strategy)
4. [Risk Register — what can go wrong and how we prevent it](#4-risk-register)
5. [Day-by-day execution plan](#5-day-by-day-execution-plan)
6. [Friday deliverable (end-of-week summary I'll produce)](#6-friday-deliverable)

---

## 1. System Design

> Applying `engineering:system-design` framework: boundaries → data → control flow → failure modes.

### 1.1 Component diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         MetaTrader 5 (host)                         │
│  OmniExport.mq5 ────▶ omni_data.json    (3s write cycle)            │
│  OmniExecutor.mq5 ◀── omni_cmd.txt      (reads, executes)           │
│  OmniSignalOverlay.mq5 ◀── omni_signals.json (new, Phase 3)         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ file-based IPC
┌──────────────────────────────▼──────────────────────────────────────┐
│                        OMNI Python core                             │
│                                                                     │
│  ┌─────────────┐   ┌────────────────┐   ┌─────────────────────┐     │
│  │ mt5_        │──▶│  smc_engine    │──▶│  dual_tf_selector    │    │
│  │ connector   │   │  (Phase 2)     │   │  (Phase 2)          │     │
│  └─────────────┘   │  OBs, FVGs,    │   │  D1 bias → H4 zone  │     │
│                    │  sweeps, CHoCH │   │  → M5 precision     │     │
│                    └────────┬───────┘   └──────────┬──────────┘     │
│                             │                      │                │
│                             ▼                      ▼                │
│                    ┌────────────────┐   ┌──────────────────────┐    │
│                    │ ict_precision  │   │  scaling_engine      │    │
│                    │ (existing)     │   │  (Phase 2)           │    │
│                    └────────┬───────┘   └──────────┬───────────┘    │
│                             │                      │                │
│                             └──────────┬───────────┘                │
│                                        ▼                            │
│                         ┌───────────────────────────┐               │
│                         │ auto_trader (orchestrator)│               │
│                         └──────┬──────────┬─────────┘               │
│                                │          │                         │
│                    ┌───────────▼────┐  ┌──▼────────────────┐        │
│                    │ smart_trailing │  │ advanced_risk_mgr │        │
│                    │  _stop (new)   │  │ (existing)        │        │
│                    └────────────────┘  └───────────────────┘        │
│                                                                     │
│  ┌──────────────────┐   ┌──────────────────┐   ┌─────────────────┐  │
│  │ trade_memory     │   │ signal_writers   │   │ watchdog        │  │
│  │ (existing)       │   │ pine / mql5 / tv │   │ (Phase 4)       │  │
│  │                  │   │ (Phase 3)        │   │                 │  │
│  └──────────────────┘   └──────────────────┘   └─────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 Data contracts

**Canonical `Signal` event** (Phase 2 → Phase 3 source of truth):

```python
@dataclass
class Signal:
    ts:        datetime
    symbol:    str
    tf:        Literal["D1","H4","H1","M15","M5","M1"]
    kind:      Literal["OB","FVG","SWEEP","BOS","CHOCH","ENTRY","SCALE_IN","EXIT"]
    side:      Literal["BULL","BEAR"]
    price_top: float
    price_bot: float
    strength:  Literal["WEAK","MEDIUM","STRONG"]
    confluence: list[str]         # ["asia_low_swept","h4_bull_bias","q1"]
    confidence: int               # 0-100
    mitigated: bool = False
    meta:      dict = field(default_factory=dict)
```

Every downstream (MQL5 overlay, Pine generator, dashboard, memory) reads from this single shape.

**`MarketContext`** is already defined in `smart_trailing_stop.py`; the Phase 2 SMC engine fills it on every scan.

### 1.3 Control flow (one scan cycle, Phase 2 complete)

```
t=0      read omni_data.json
t=+5ms   smc_engine.analyze(symbol, tf=D1) → OBs, FVGs for D1 bias
t=+15ms  smc_engine.analyze(symbol, tf=H4) → H4 structure + zones
t=+20ms  dual_tf_selector(symbol) → {zone, bias, in_zone?}
t=+30ms  IF in_zone:
            smc_engine.analyze(symbol, tf=M5) → entry trigger search
t=+35ms  ict_precision.score(symbol, context) → setup with confidence
t=+40ms  auto_trader.manage_open_trades:
            for each open pos: compute_trailing_sl() → modify if needed
            scaling_engine.evaluate(pos, context) → add/trim if triggered
t=+50ms  auto_trader.execute_setup() for any new setups passing threshold
t=+55ms  signal_writers.flush(signals_this_tick) → omni_signals.json
t=+60ms  save_state, sleep until next scan
```

Budget: full cycle target < 150ms per symbol. 9 symbols × 150ms = ~1.4s worst case. Scan interval stays at 10s with wide margin.

### 1.4 Failure modes

| Failure | Detection | Response |
|---|---|---|
| `omni_data.json` stale > 60s | watchdog mtime check | pause new entries; existing positions managed on cached last-good context |
| SMC engine raises | try/except in scanner | skip symbol this tick, log with context, continue |
| Smart trail returns `should_close` | caller sees flag | close runner via `close_position`, log to memory |
| Broker reject on modify | MT5 response parsing | retry once; if still rejected, log and skip (SL unchanged) |
| Two modifies within 1s | rate-limit counter in `mt5_connector` | back off 500ms, coalesce to latest value |
| Config change mid-run | file-watcher on `rules.json` | reload config on next scan; never mid-trade |

---

## 2. Architecture Decisions

> Applying `engineering:architecture` framework: each decision as a one-page ADR with context → options → decision → consequences.

### ADR-001 — Signal event schema

- **Context:** Three surfaces need the same SMC events (MT5 overlay, Pine, dashboard). Without a canonical schema, each surface drifts.
- **Options:** (A) Per-surface schemas (rejected, drift inevitable). (B) Canonical `Signal` dataclass serialised to JSON. (C) ProtoBuf (overkill for file-based IPC).
- **Decision:** **Option B** — `Signal` dataclass, JSON-serialised.
- **Consequences:** Good: single source of truth; schema versioned via `_schema_version` field. Bad: schema changes require coordinated redeploy of MT5 EA; mitigated by backward-compatible field additions only.

### ADR-002 — SMC engine placement

- **Context:** Where does the new SMC engine live relative to existing `ict_precision.py`?
- **Options:** (A) Rewrite `ict_precision.py`. (B) New `smc_engine.py` sibling, `ict_precision.py` becomes consumer. (C) Keep both, duplicate logic.
- **Decision:** **Option B**. `smc_engine.py` produces raw SMC objects; `ict_precision.py` scores and composes them into setups. Clean separation: detection vs decision.
- **Consequences:** Good: testable in isolation, supports multiple scoring strategies. Bad: one extra module boundary; mitigated by keeping interfaces narrow (one `analyze(symbol, tf)` → `SMCSnapshot`).

### ADR-003 — Scaling engine as separate module

- **Context:** Scaling logic could live inside `auto_trader.check_scale_in` (current location) or as its own module.
- **Options:** (A) Keep inside auto_trader. (B) New `scaling_engine.py` with pure evaluator. (C) Part of smart_trailing_stop.
- **Decision:** **Option B**. Scaling is strategic (trade construction); trailing is protective (capital preservation). Different concerns, different modules.
- **Consequences:** Good: composable, individually testable, feature-flaggable. Bad: two modules the orchestrator must coordinate; mitigated by both consuming the same `MarketContext`.

### ADR-004 — Feature flags in `rules.json`

- **Context:** How do we ship new logic safely?
- **Options:** (A) Replace old logic in place. (B) Branch by feature flag in rules.json. (C) Separate runtime profiles.
- **Decision:** **Option B**. Every new module reads its own sub-block in rules.json with an `enabled: bool`. Default `false` until validated.
- **Consequences:** Good: instant rollback by editing one flag; can A/B per symbol. Bad: more branching in auto_trader; mitigated by adapter layer that keeps core paths unchanged when flags are off.

### ADR-005 — MT5 overlay via file IPC (not socket)

- **Context:** How does the Python bot tell MT5 what to draw?
- **Options:** (A) Same file-based pattern as existing exporters. (B) TCP socket to a local daemon. (C) Named pipes.
- **Decision:** **Option A**. Consistent with existing `omni_data.json` / `omni_cmd.txt` pattern; simple, robust, already proven on your setup.
- **Consequences:** Good: zero new moving parts. Bad: 1-second polling latency on overlay (acceptable for visualisation, not for execution).

### ADR-006 — Pine generation strategy

- **Context:** How do we keep Pine overlay in sync with Python engine?
- **Options:** (A) Hand-write Pine. (B) Generate Pine from template + current rules. (C) Runtime sync via TV webhook.
- **Decision:** **Option B**. `pine_codegen.py` regenerates Pine whenever `rules.json` thresholds change. Pine remains deterministic and self-contained on TV side.
- **Consequences:** Good: no runtime coupling; TV renders locally. Bad: generated Pine lags live changes by one regen; mitigated by regen-on-save hook.

---

## 3. Testing Strategy

> Applying `engineering:testing-strategy`: pyramid — unit → integration → backtest → paper → demo.

### 3.1 Unit tests

**Target coverage:** 80%+ on all new modules. Pure functions only.

| Module | Test areas |
|---|---|
| `smart_trailing_stop` | Each layer in isolation; monotonic ratchet; hysteresis; CHoCH close signal; error fallback |
| `smc_engine` | OB detection on synthetic bars; FVG detection; sweep detection; CHoCH vs BOS disambiguation |
| `dual_tf_selector` | Bias agreement/disagreement; zone entry/exit; micro-TF entry trigger |
| `scaling_engine` | Each trigger rule; max-scale-in cap; scale-out on opposing CHoCH |
| `signal_writers` | JSON serialisation round-trip; schema version compatibility |

**Framework:** `pytest`. Target file: `python/tests/test_<module>.py`. CI run target: < 10s total.

Synthetic market fixtures live in `python/tests/fixtures/`. Each fixture is a hand-crafted bar series with known ICT events — tests assert the engine detects exactly those events.

### 3.2 Integration tests

- `auto_trader.manage_open_trades` with smart trailing enabled → simulated position + simulated context → expected modify command.
- Scale-in end-to-end: simulated winning position → triggered → `place_order` called with correct size.
- Kill switch: create HALT file → next scan sees no new entries, existing positions still managed.

### 3.3 Backtest harness

**Extend `backtester.py`:**

- Accept `--engine old|new` flag.
- Replay 12 months of exported M5 bars per watchlist symbol.
- Produce `backtest_report.md`:
  - Trades: count, win%, avg R, median R, 95th-percentile R.
  - Drawdown: max DD, DD recovery time, longest losing streak.
  - Per-session: same metrics sliced by Asia/London/NY/NY-close.
  - Per-symbol: same metrics.
  - Old vs new: delta on each metric, pass/fail flags.

**Pass gates** (new engine must match or beat old on all):

- Win rate: no worse than old.
- Avg R: +0.05 or better.
- Max DD: no worse than old.
- No symbol with negative expectancy.

If new engine fails any gate, it does NOT ship to paper mode.

### 3.4 Paper validation (starts next week)

- Run in paper mode for 10 trading days with live MT5 data.
- Success criteria:
  - Frequency: 0.5× to 2.0× of backtest frequency (anything outside = model drift).
  - Scan loop time: p99 < 1s per cycle.
  - Zero unhandled exceptions in `trader.log`.
  - Agreement: paper decisions match what backtest would decide on the same bar (±2% tolerance for timing noise).

### 3.5 Demo validation

- Broker demo account, LOW risk profile, 10 trading days.
- Full trade lifecycle including partial TP fills, scale-ins, trailing, close-on-CHoCH.
- Success criteria: broker accepts all orders, no reject cascades, actual fill prices within 1 pip of modelled (sanity check on slippage assumptions).

### 3.6 Live gating (weeks later, not this week)

- Start with 0.25% risk (quarter of base) on one account.
- Double weekly if all metrics hold.
- Full base risk only after 30 live trading days with no red flags.

---

## 4. Risk Register

> Applying `operations:risk-assessment` framework: likelihood × impact → mitigation → residual.

| # | Risk | L | I | Score | Mitigation | Residual |
|---|---|---|---|---|---|---|
| R-01 | Logic bug in smart trailing loosens SL in live trade | Low | Critical | High | Monotonic ratchet enforced in code; unit test asserts `new_sl >= current_sl` for BUY, `<=` for SELL; paper/demo validation before live | Low |
| R-02 | Scale-in triggers on false push → adds at bad price | Med | High | High | Require H1 structure intact + M15 confirmation + original confluence valid; max 1 scale-in; cap total position at 2× original | Low-Med |
| R-03 | SMC engine false positive on OB detection inflates signal count | Med | Med | Med | Strength gating (only STRONG OBs trigger entries); backtest pass-gate on win rate; AI memory demotes poor setups | Low |
| R-04 | MT5 disconnect during open position | Med | High | High | Watchdog detects stale data in 60s; OmniExecutor has client-side SL/TP already set at order time; broker honours SL server-side | Low |
| R-05 | Broker rejects modify due to "too close to price" | High | Low | Med | Minimum distance check in `modify_position`; retry-once-then-skip; unchanged SL is safe | Low |
| R-06 | `rules.json` reload during active trade causes config mismatch | Low | Med | Med | Config snapshot taken at trade open; stored in `active_trades[ticket]`; reload only affects new trades | Low |
| R-07 | Backtest overfits to sample window | Med | High | High | Walk-forward: train on 2024, test on 2025–26; require positive expectancy on both halves | Med |
| R-08 | File-based IPC races on Windows (omni_cmd.txt) | Low | Med | Med | Atomic write via temp-file + rename pattern already used; verify with stress test | Low |
| R-09 | Credentials leak via logs (Phase 4) | Low | Critical | High | Keychain access only; custom log filter strips `password=.*`; exception handlers redact | Low |
| R-10 | LaunchAgent keeps restarting crashed bot in tight loop | Low | Med | Low | `ThrottleInterval=30` in plist; alert after 3 restarts in 5 minutes | Low |
| R-11 | AI memory locks out profitable setup after noise streak | Med | Med | Med | Min 8 samples before adjustment; floor at -30; manual override via `rules.json` disable list | Low |
| R-12 | User expectation of "100% accuracy" leads to oversizing | Med | Critical | Critical | Explicit statement in every report that no system is 100%; base risk stays 1%; live deployment gated by sustained expectancy | Med |

**Stop conditions (auto-halt triggers, already in code or to be added):**

- Daily loss ≥ 3% of starting-of-day equity
- Drawdown from equity peak ≥ 10%
- Three consecutive unhandled exceptions in a single scan cycle
- Scan loop p99 > 5s (performance regression)
- Three consecutive broker rejects on any single symbol
- HALT file present

---

## 5. Day-by-day execution plan

**Thursday Apr 16 (today) — Phase 1 complete, Phase 2 started**

- ✅ `smart_trailing_stop.py` written + self-tested
- ✅ `rules.json` gains `smart_trail` block (disabled by default)
- ✅ `smart_trail_adapter.py` — glue so `auto_trader.manage_open_trades` can call the new engine with a one-line replacement
- 🔜 `python/tests/test_smart_trailing_stop.py` — pytest unit tests (tomorrow AM)

**Friday Apr 17 — Phase 1 wired; Phase 2 kickoff**

- Wire adapter call into `auto_trader.manage_open_trades` behind the `smart_trail.enabled` flag (default still false)
- Add `last_trail_proposal` to state for dashboard visibility
- Write pytest suite for smart_trailing_stop (8+ tests)
- Start `smc_engine.py`: OrderBlock, FairValueGap, LiquidityPool dataclasses + detection functions
- First unit tests for smc_engine on synthetic fixtures

**Saturday Apr 18 — Phase 2 engine core**

- Finish smc_engine: sweep detection, CHoCH/BOS disambiguation
- 15+ unit tests covering each detector
- `dual_tf_selector.py`: bias resolution, zone identification, in-zone detection

**Sunday Apr 19 — Phase 2 scaling**

- `scaling_engine.py`: evaluate() returns ScaleAction
- Integration tests covering each trigger rule
- Wire smc_engine into ict_precision.py as source for OB/FVG/sweep

**Monday Apr 20 — Phase 3 signal writers**

- `signal_writers.py` with Signal dataclass + JSON emitters
- `pine_codegen.py` with template + regeneration CLI
- `OmniSignalOverlay.mq5` EA skeleton that reads `omni_signals.json` and draws

**Tuesday Apr 21 — Phase 4 autonomy scaffolding**

- `credentials.py` with Keychain wrapper (no code paths use it yet)
- LaunchAgent plist + install/uninstall scripts
- `watchdog.py` basic implementation (mtime check + alert hook)

**Wednesday Apr 22 — Integration + backtest comparison**

- Run backtester with `--engine old` and `--engine new` on 12 months of data
- Produce `backtest_report.md` with all pass-gate metrics
- Produce end-of-week summary doc

---

## 6. Friday deliverable

At end-of-week I'll produce `WEEK_SUMMARY.md` with:

- What shipped (code manifest with line counts and test counts)
- What didn't and why
- Backtest old vs new results (tables + verdict on pass gates)
- Outstanding risks from the register
- Recommended next-week plan (paper mode config + monitoring setup)
- Explicit go/no-go on paper mode activation

If any pass-gate fails, the module stays disabled and we iterate.
