# OMNI-ICT Upgrade Roadmap

Phased plan to deliver: smart trailing stop, dual-TF SMC scaling, Pine/MQL5 signal writers, and log-in-once autonomy. Sequencing minimises risk to live capital. Every phase gates through backtest → paper → demo → live.

---

## Guiding principles

1. **No "100% accurate."** That target produces overfit systems that fail live. The goal is *positive expectancy with controlled drawdown* across regimes.
2. **Every change is feature-flagged.** New logic ships disabled by default. A single flag in `rules.json` turns it on per account.
3. **Monotonic safety on stops.** Trailing logic can only ratchet SL toward profit, never away.
4. **Fail-safe.** Any error in new modules returns *no change* — the bot reverts to previous behaviour rather than crashing the loop.
5. **Backtest-comparable.** New modules are pure functions with explicit inputs so the backtester can replay both old and new side-by-side.

---

## Phase 1 — Smart trailing stop ✅ built

**File:** `python/smart_trailing_stop.py`

Replaces the static R-multiple ladder with five reactive layers:

| Layer | Input | Effect |
|---|---|---|
| Volatility | ATR(14) on M15 + H1 | SL distance = ATR × multiplier (1.2× normal, 1.8× runner, 0.8× exhaustion, 2.2× displacement) |
| Structure | Last opposing swing on M15 (guarded by H1) | SL placed behind HL (BUY) / LH (SELL) with configurable buffer |
| Momentum | Exhaustion flag (long wicks + flattening bodies) / Displacement flag (3+ bodies in trend) | Tighten on exhaustion, loosen on displacement |
| Liquidity | Equal highs/lows, session H/L, PDH/PDL | Never leave SL sitting inside a sweep zone |
| Profit lock | R-multiple ladder | Hard floor on locked R that can never retreat |

**Plus:** opposing-CHoCH-while-profitable = signal close (not just tighten).

**Self-test:** `python smart_trailing_stop.py` → 3/3 pass.

### Integration (next session)

1. Add `smart_trail: {enabled: true/false, ...}` to `rules.json`.
2. In `auto_trader.manage_open_trades`, when the flag is on, build a `MarketContext` from the existing scanner output and call `compute_trailing_sl(pos, ctx, cfg)` instead of the inline ladder.
3. Gate behind PAPER mode for one full week before enabling live.
4. Add `last_trail_proposal` to `trader_state.json` for dashboard visibility.

### What can still go wrong

- ATR ref goes to 0 on a data gap → volatility layer returns None → structure + lock layers carry the trail. ✅ handled.
- Stale swing data → SL could be placed further than intended → capped by monotonic ratchet and min-modify hysteresis.
- Flapping between exhaustion and displacement → hysteresis (`min_modify_atr_frac = 0.15`) suppresses chatter.

---

## Phase 2 — Dual-TF SMC scaling engine

**Goal:** Major-TF context (D1/H4) identifies the order block and bias. Micro-TF (M5/M1) finds precision entries on retests. Bot scales in on confirmed liquidity retests, scales out on signs of reversal.

### 2.1 Port SMC Pine logic into `ict_precision.py`

Mirror the public SMC indicator's core objects as Python classes:

```python
@dataclass
class OrderBlock:
    side: str          # "BULL" or "BEAR"
    high: float
    low: float
    origin_bar: int
    mitigated: bool = False
    strong: bool = False    # formed after sweep of liquidity
    timeframe: str = ""

@dataclass
class FairValueGap:
    side: str
    top: float
    bottom: float
    origin_bar: int
    filled_pct: float = 0.0

@dataclass
class LiquidityPool:
    kind: str          # "EQH", "EQL", "TRENDLINE"
    level: float
    touches: int
    swept: bool = False
```

Detection rules (Pine → Python):

- **OB:** last opposing candle before displacement that closes >1× ATR in opposite direction.
- **FVG:** 3-bar imbalance, `bar[i-1].high < bar[i+1].low` (bull) or inverse.
- **Liquidity sweep:** wick beyond equal H/L with close back inside.
- **CHoCH:** break of last *internal* swing. **BOS:** break of last *external* swing.

### 2.2 Dual-TF selector

New module `python/dual_tf_selector.py`:

1. Bias on H4 (bullish/bearish based on most recent BOS direction).
2. On D1: identify untouched OBs and FVGs in bias direction → "zones of interest."
3. When price enters a zone, spin up M5 analysis until:
   - Sweep of minor liquidity *inside* the zone, then
   - M5 CHoCH in bias direction, then
   - Entry at resulting M5 OB with SL beyond swept level.

### 2.3 Scaling engine

New module `python/scaling_engine.py`. Rules:

| Trigger | Action |
|---|---|
| Price retests a prior untested FVG *inside the zone* and respects it (M5 rejection) | Add 50% of original size, SL at zone boundary |
| External liquidity (session high/low, PDH/PDL) gets swept and price re-enters | Add 30% of original size |
| M5 displacement extending beyond last BOS | Add 25% of original size |
| Opposing M15 CHoCH while price is above entry by ≥1R | Scale out 50% |
| M15 body closes back inside zone from opposite side | Scale out 100% of remaining |

Hard caps: max 2 scale-ins per trade, max position = 2× original, max single-symbol exposure = account risk cap (from `advanced_risk_manager.py`).

### 2.4 AMD phase awareness

Extend the existing AMD detection in `ict_engine.py`:

- Asia session range captured live; classify Asia as accumulation if range ≤ 0.6×ATR, manipulation if sweep of Asia range occurs in first 30m of London, distribution if sustained move away from Asia mid.
- Use phase to *modulate entry confidence*, not to filter outright — the README already does this in principle; we make it dynamic per symbol.

### 2.5 Validation before live

- Backtest: replay 12 months of exported M5 bars for each watchlist symbol. Compare `winrate`, `avg_R`, `max_DD`, `trades_per_week` vs the current engine.
- Paper: run for 2 full trading weeks. Compare: frequency (shouldn't explode), win rate (shouldn't collapse), avg R (should improve).
- Demo: 2 weeks on broker demo at 0.01 lots per trade.
- Live: enable on one account, lowest risk profile (LOW), for 2 weeks before widening.

---

## Phase 3 — Pine / MQL5 signal writers

Three surfaces, single source of truth: the Python SMC engine from Phase 2 emits a canonical "signal event." Adapters serialise it for each surface.

### 3.1 Canonical signal event

```json
{
  "timestamp": "2026-04-16T14:32:10Z",
  "symbol": "XAUUSD",
  "timeframe": "M15",
  "event": "ORDER_BLOCK",   // or FVG, SWEEP, BOS, CHOCH, ENTRY
  "side": "BULL",
  "top": 2345.12, "bottom": 2344.06,
  "strength": "STRONG",
  "confluence": ["asia_low_swept", "h4_bullish_bias", "q1_discount"],
  "confidence": 78
}
```

### 3.2 Python → TradingView (Pine)

Module: `python/tv_signal_writer.py`

- Bot POSTs each signal to a TradingView webhook endpoint (you configure once in TV as an alert).
- A companion Pine script reads webhook payloads via `alert()` / chart marks — but Pine can't receive arbitrary data. So the practical pattern is the inverse: bot *renders* signals into a `signals.json` that a lightweight viewer page overlays on a TV chart iframe.
- If you want TV-native overlays, the realistic path is a Pine script that computes the same SMC objects locally on TV (the logic is deterministic), and we generate that Pine from a template so it stays in sync with the Python engine.

**Deliverable:** `python/pine_codegen.py` that emits `pine/omni_smc_generated.pine` from the current engine parameters. Rebuilt whenever thresholds in `rules.json` change.

### 3.3 Python → MT5 (MQL5)

Module: `python/mql5_signal_writer.py`

- Bot writes a `omni_signals.json` alongside `omni_data.json`.
- New EA `mql5/OmniSignalOverlay.mq5` reads the file every 1s and draws labelled rectangles (OBs), shaded zones (FVGs), and horizontal lines (liquidity pools) on the active chart.
- Objects are tagged with a magic prefix so the EA can clean up stale drawings.

This makes the bot's "view" visible on your MT5 chart live — useful for debugging and for manually confirming trades during the paper phase.

### 3.4 Python → internal dashboard

The existing webapp already has a market-scanner panel. Extend it with a TradingView Lightweight Charts embed that reads the same `omni_signals.json` so you have one truth across MT5 + dashboard + TV.

---

## Phase 4 — Log-in-once autonomy (macOS)

### 4.1 Credentials

- Use macOS Keychain (`security` CLI) for MT5 login + server URL.
- `python/credentials.py` wraps Keychain access. First run prompts once; subsequent runs read silently.
- Never log credentials; redact in exception handlers.

### 4.2 LaunchAgent

Plist at `~/Library/LaunchAgents/com.omni.trader.plist`:

- `RunAtLoad = true` → starts on boot.
- `KeepAlive = true` with `SuccessfulExit = false` → auto-restart on crash.
- `ThrottleInterval = 30` → prevents crash loops.
- Logs to `~/Library/Logs/omni-trader.log` and `~/Library/Logs/omni-trader.err.log`.

Install/uninstall scripts in `scripts/install_launchagent.sh` and `scripts/uninstall_launchagent.sh`.

### 4.3 Watchdog

`python/watchdog.py`:

- Monitors `omni_data.json` mtime. If stale > 60s, flags MT5 dead.
- Attempts: soft restart of `auto_trader.py` → AppleScript to re-open MT5 → alert via webhook (Slack/email) if still dead after 3 attempts.

### 4.4 Self-healing data

- If scanner detects stale bars, pauses trading (no new entries, existing trades managed normally) until data is fresh.
- Daily reset (00:00 UTC) auto-verifies broker time sync.

### 4.5 What still requires you

Broker password change → you re-enter once via a one-line CLI (`python -m credentials refresh`). That's it.

---

## Risks I'm still flagging

1. **The ICT methodology is contested.** The *patterns* (OB, FVG, liquidity) exist as observable features of auction markets; the *causal story* ("banks hunt retail stops") is unverifiable. Your system should earn its keep from *statistical edge in the patterns*, not from the narrative. If a pattern doesn't backtest positive across 3+ regimes, it comes out.
2. **Overfitting to 2024–2026 FX conditions.** Walk-forward test with 2018–2022 data as well. If the system collapses on pre-COVID FX, that's a signal.
3. **Execution slippage on small account sizes.** 1% base risk × 5 concurrent scale-ins on XAUUSD during London open can rack up spread cost that wasn't modelled. `advanced_risk_manager.py` already accounts for spread — verify its numbers against 30 days of actual MT5 history before enabling scaling.
4. **Broker-side restrictions on EA automation.** Some brokers throttle order frequency; a fast scaling engine can trip that. Add an emergency back-off in `mt5_connector.py` if more than N modifies/sec are attempted.

---

## Suggested order for next sessions

1. **This session (done):** `smart_trailing_stop.py` + self-test. ✅
2. **Next session:** wire smart trailing into `auto_trader.py` behind `rules.json` flag, add dashboard visibility for layers fired, run backtester comparison and ship a report.
3. **Then:** Phase 2 — port SMC objects into `ict_precision.py`, ship dual-TF selector.
4. **Then:** Phase 2 scaling engine + extended backtest.
5. **Then:** Phase 3 signal writers (MQL5 overlay first — biggest feedback value for you).
6. **Then:** Phase 4 autonomy.

Total realistic calendar time at a careful pace: 3–5 weeks of iterative work, not one shot.
