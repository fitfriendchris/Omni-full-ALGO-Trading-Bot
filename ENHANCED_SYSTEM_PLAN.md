# OMNI ICT — ENHANCED SYSTEM PLAN
## Complete Market Structure + Price Action + 3/5-Day Cycle Integration

**Objective:** Transform the current multi-agent bot from a confluence-based signal generator into a true ICT institutional-grade system that understands weekly→daily→H4→H1→M15→M5 market structure, tracks the 3-day and 5-day Market Maker cycles in real time, and executes only when manipulation→displacement confirms on all timeframes.

---

## 1. CURRENT STATE ASSESSMENT

**What Exists (Good Foundation):**
| Module | Status | Strength | Weakness |
|--------|--------|----------|----------|
| `smc_engine.py` | ✅ Operational | OB, FVG, liquidity sweep, BOS/CHoCH detection | Pure O(n) detection; no higher-timeframe context |
| `manipulation_leg_detector.py` | ✅ Operational | Detects Judas sweeps, EQH/EQL sweeps, rejection | No linkage to cycle phase or daily structure |
| `dual_tf_selector.py` | ✅ Patched (Gate 3) | HTF bias + LTF precision, 6 confluence checks | No weekly bias; no true multi-TF structure alignment |
| `cycle_tracker.py` | ⚠️ Isolated | Accumulation/Manipulation/Distribution state machine | NOT wired into selector or execution; no daily open tracking |
| `h4_daily_swing.py` | ⚠️ Standalone | H4/D1 OB swing trading simulation | Not wired to live pipe; no cycle awareness |
| `rules.json` | ✅ Rich | Quarter theory, session rules, AMD rules | Static config — not computed from live structure |

**Critical Gaps:**
1. **No weekly timeframe module** — ICT institutional order flow starts on weekly
2. **Cycle tracker is isolated** — `cycle_tracker.py` computes phase but `dual_tf_selector.py` never reads it; execution_agent doesn't know if we're in accumulation or distribution
3. **No true multi-TF structure engine** — the code detects BOS/CHoCH on one TF at a time; there's no `weekly_bias + daily_bias + h4_bias + h1_bias` alignment checker
4. **No daily open/close structure** — ICT's "3-day and 5-day cycle" maps to daily candles taking liquidity above/below prior day high/low; this isn't modeled
5. **Manipulation legs aren't tracked across sessions** — a London Judas sweep followed by NY distribution is two separate detections; not linked as a single cycle
6. **Execution agent doesn't see structure** — it only sees confidence/lot from risk_agent; doesn't know if the H4 just shifted bias

---

## 2. TARGET ARCHITECTURE

### 2.1 New Module Map

```
weekly_bias_engine.py          ← NEW: D1 candle roll-up, weekly OB, weekly BOS/CHoCH
daily_structure_engine.py      ← NEW: daily open/close, PDH/PDL, daily FVG, daily sweep log
mtf_structure_aligner.py       ← NEW: aligns W1/D1/H4/H1/M15 bias (all must agree or veto)
cycle_phase_integrator.py      ← NEW: wires cycle_tracker.py into live pipeline
session_memory.py              ← NEW: tracks London→NY session flows, Judas→Displacement linking
market_structure_context.py     ← NEW: single source of truth for ALL structural state
enhanced_dual_tf_selector.py   ← REPLACE: merges old logic + new MTF context + cycle phase
```

### 2.2 Data Flow (Re-architected)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         MT5 DATA PIPELINE                               │
│  omni_data.json → parse W1/D1/H4/H1/M15/M5 bars                        │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ weekly_bias  │→ │ daily_struct │→ │ h4_structure │→ │ session_mem  │
│   engine     │   │   engine     │   │   (exists)   │   │   (new)      │
└──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘
       │                  │                 │                 │
       └──────────────────┴─────────────────┴─────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────┐
                    │   mtf_structure_aligner     │
                    │   (WEEKLY + D1 + H4 alignment engine)  │
                    └─────────────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────┐
                    │   cycle_phase_integrator     │
                    │   (3-day / 5-day cycle state)│
                    └─────────────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────┐
                    │ enhanced_dual_tf_selector    │
                    │  (8-layer confluence + cycle)│
                    └─────────────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────┐
                    │   risk_agent → execution    │
                    │   (sees full context now)    │
                    └─────────────────────────────┘
```

---

## 3. MODULE-BY-MODULE SPECIFICATION

### 3.1 `weekly_bias_engine.py` — Weekly Institutional Order Flow

**Purpose:** Compute weekly bias from D1 candles rolling up into weekly structure.

**ICT Mapping:**
- Weekly BULL = last weekly candle closed bullish AND price above weekly OB
- Weekly BEAR = last weekly candle closed bearish AND price below weekly OB
- Weekly NEUTRAL = price inside weekly OB or doji/inside week

**Output:**
```python
@dataclass
class WeeklyBias:
    direction: str          # "BULL" | "BEAR" | "NEUTRAL"
    weekly_ob_high: float   # weekly bullish OB top
    weekly_ob_low: float    # weekly bullish OB bottom
    last_weekly_bos: str    # "BULL" | "BEAR" | None
    prior_week_high: float
    prior_week_low: float
    confidence: float       # 0.0-1.0
```

**Logic:**
1. Group D1 bars into weeks (Monday open → Friday close)
2. Detect BOS on weekly pivots: HH/HL = bull, LH/LL = bear
3. Find last weekly unmitigated OB: the last opposing candle before weekly displacement
4. If price is above weekly OB → bull structural support
5. If price is below weekly OB → bear structural resistance

**Gating Rule (hard veto):**
- If weekly direction == "BEAR" and H4 signal is "BUY" → veto trade (institutional selling)
- If weekly direction == "BULL" and H4 signal is "SELL" → veto trade
- Exception: daily sweep + close back inside can override weekly bias (reversal setup)

---

### 3.2 `daily_structure_engine.py` — Daily Open / Close & PDH/PDL Tracking

**Purpose:** Track daily candle structure — the heartbeat of the 3-day and 5-day cycle.

**ICT Mapping:**
- Daily Open = institutional reference price for the session
- PDH (Prior Day High) / PDL (Prior Day Low) = primary intraday liquidity
- Daily FVG = the most reliable FVGs for next-session entries
- Daily OB = unmitigated daily OBs are the strongest confluence

**Output:**
```python
@dataclass
class DailyStructure:
    daily_open: float
    prior_day_high: float
    prior_day_low: float
    prior_day_close: float
    daily_fvg_bull: list    # unmitigated bullish FVGs from today
    daily_fvg_bear: list   # unmitigated bearish FVGs from today
    daily_ob_bull: list    # unmitified bullish OBs from today
    daily_ob_bear: list   # unmitigated bearish OBs from today
    day_of_week: int       # 0=Monday ... 6=Sunday
    is_midweek: bool       # Tue-Thu = manipulation/distribution window
    sweep_log: list        # today's sweeps tagged with time and outcome
```

**3-Day Cycle Logic:**
```
Day 1 (Mon/Tue):  Accumulation — mark the range, build liquidity
Day 2 (Tue/Wed):  Manipulation — sweep prior day extreme, false breakout
Day 3 (Wed/Thu):  Distribution — true move, displacement confirmed
Reset on Day 4 if structure breaks.
```

**5-Day Cycle Logic (extended):**
```
Week 1-2: Accumulation — tight range, institutions positioning
Week 3:   Manipulation — major sweep of prior 2-week extreme
Week 4-5: Distribution — sustained move in sweep direction
```

**Implementation Note:**
- Maintain a rolling log of the last 5 trading days
- Track whether each day's close swept above/below prior day high/low
- Count sweep + close back inside (manipulation) vs sweep + displacement (distribution)
- Reset cycle counter when a NEW WEEKLY extreme is taken

---

### 3.3 `mtf_structure_aligner.py` — Multi-Timeframe Alignment Engine

**Purpose:** The TRUE confluence gate. All TFs must align or the trade is blocked.

**ICT Hierarchy (strict):**
```
WEEKLY bias → DAILY bias → H4 bias → H1 execution → M15 precision → M5 trigger
```

**Alignment Rules:**
```python
ALIGNMENT_OK = (
    weekly in ["BULL", "NEUTRAL"] and daily in ["BULL", "NEUTRAL"] and h4 == "BULL" and h1 == "BULL" and m15_confirms
) or (
    weekly in ["BEAR", "NEUTRAL"] and daily in ["BEAR", "NEUTRAL"] and h4 == "BEAR" and h1 == "BEAR" and m15_confirms
)

CONFLICT = any lower_TF opposes higher_TF direction
  → Example: weekly BULL, daily BEAR = CONFLICT (wait for alignment)
  → Example: h4 BULL, h1 BEAR = CONFLICT (H1 CHoCH may be starting)
```

**Signal Output:**
```python
@dataclass
class MTFAlignment:
    weekly: str
    daily: str
    h4: str
    h1: str
    m15: str
    alignment_score: int   # -40 to +50
    grade: str             # "A+" | "A" | "B+" | "B" | "CONFLICT"
    tradeable: bool        # A+ or A only → full size; B+ → 50% size
```

**Scoring:**
- All TFs same direction: +25
- W1/D1 aligned, H4 aligned, H1 aligned: +15 (normal A+ setup)
- One TF neutral (e.g., H1 choppy): -5
- One TF opposing: -20 → CONFLICT, no trade
- Weekly opposing: -40 → hard veto

---

### 3.4 `cycle_phase_integrator.py` — Wire cycle_tracker into Live Pipeline

**Purpose:** Give `dual_tf_selector.py` and `execution_agent.py` real-time cycle awareness.

**Current Problem:** `cycle_tracker.py` computes accumulation/manipulation/distribution but NOBODY READS IT.

**Solution:** Build a persistent `cycle_state.json` file that is:
1. Updated by `orchestrator.py` every 60 seconds (after H4 bars feed in)
2. Read by `dual_tf_selector.py` as Confluence Layer 9: **Cycle Phase Alignment**
3. Read by `execution_agent.py` to enforce phase-specific sizing rules

**Phase-Specific Rules (from rules.json, now enforced by code):**
```python
PHASE_RULES = {
    "ACCUMULATION": {
        "max_size_mult": 0.5,
        "min_confidence": 0.75,
        "allowed_entries": ["SWEEP_REVERSAL"],
        "blocked_entries": ["FVG_FILL", "CHOCH_ENTRY"],
        "note": "Asia/Accumulation: identify range, no FVG entries"
    },
    "MANIPULATION": {
        "max_size_mult": 0.75,
        "min_confidence": 0.55,
        "allowed_entries": ["SWEEP_OB_RETEST", "JUDAS_REVERSAL"],
        "wait_for_choch": True,
        "note": "London Judas: wait for CHoCH confirmation, don't front-run"
    },
    "DISTRIBUTION": {
        "max_size_mult": 1.0,
        "min_confidence": 0.55,
        "allowed_entries": ALL,
        "scale_into_winners": True,
        "note": "Confirmed move — full size, scale, runners"
    }
}
```

**3-Day Cycle Tracking:**
- Maintain `cycle_day_counter` (1-5)
- Day counter increments when a new daily extreme is swept and closed back inside
- When counter reaches 3 and distribution is confirmed → reset counter, mark "cycle complete"
- Log cycle completion in `feature_store` for ML training

**5-Day Cycle Tracking:**
- Track last 5 weekly extremes (weekly high/low)
- If weekly sweep + close back inside occurs 2-3 times in 5 weeks → mark "extended accumulation"
- 4th week sweep that DOESN'T close back inside → mark "5-day distribution begins"

---

### 3.5 `session_memory.py` — Cross-Session Manipulation Linkage

**Purpose:** The London→NY handoff is where most ICT setups live or die.

**Current Problem:** `manipulation_leg_detector.py` detects a Judas sweep in isolation. It doesn't know if NY later confirmed the reversal.

**Solution:** Maintain a session event log that links:
```
Asia session range → London Judas sweep → London CHoCH → NY continuation/displacement
```

**Data Model:**
```python
@dataclass
class SessionFlow:
    asian_high: float
    asian_low: float
    london_judas: dict      # sweep time, direction, magnitude
    london_chooch: dict     # CHoCH time, direction, success?
    ny_continuation: dict   # did NY continue the London direction?
    ny_sweep: dict          # NY sweep of London extreme (failure pattern)
    outcome: str            # "CONTINUATION" | "REVERSAL" | "RANGE" | "PENDING"
```

**Trading Rules from Session Memory:**
1. If London swept Asian high and NY continues above → bullish continuation (no reversal)
2. If London swept Asian high but NY sweeps BELOW London low → reversal confirmed (bearish)
3. If London had no sweep → "clean open" → wait for NY to set direction
4. If both London AND NY sweep same direction → "two-leg manipulation" → wait for close of NY

---

### 3.6 `market_structure_context.py` — Single Source of Truth

**Purpose:** Aggregate ALL structural data into one JSON file that every agent reads.

**Current Problem:** Each module parses `omni_data.json` independently — race conditions, stale data.

**Solution:** `orchestrator.py` calls `market_structure_context.py` once per tick to build:
```
shared/market_structure.json:
  {
    "timestamp": "2026-05-26T18:00:00Z",
    "XAUUSD": {
      "weekly": {"direction": "BULL", "ob_high": 3500, "ob_low": 3300},
      "daily": {"direction": "BULL", "pdh": 3385, "pdl": 3340, "open": 3360},
      "h4": {"direction": "BULL", "last_bos": "BULL", "swing_high": 3375},
      "h1": {"direction": "BULL", "last_chooch": "BULL", "fvg_zone": [3350, 3345]},
      "m15": {"trigger": "OB_RETEST", "entry": 3352, "sl": 3340, "tp": 3390},
      "cycle_phase": "DISTRIBUTION",
      "cycle_day": 3,
      "session_flow": {"london_judas": {...}, "ny_continuation": {...}},
      "mtf_alignment": "A+",
      "alignment_score": 35,
      "tradeable": true,
      "size_mult": 1.0
    }
  }
```

**All agents read from this one file.** No more independent parsing.

---

## 4. ENHANCED CONFLUENCE LAYERS (8 → 12 LAYERS)

**Current:** `ConfluenceScore` has 8 layers (htf_bias, amd_phase, killzone, liquidity, fvg, mss, smt, ema)

**Enhanced:** Add 4 new layers for true institutional structure:

| Layer | Name | Detection | Weight |
|-------|------|-----------|--------|
| L1 | `weekly_bias_aligned` | weekly direction == trade direction | mandatory |
| L2 | `daily_structure_aligned` | daily BOS/CHoCH + PDH/PDL context | +0.10 |
| L3 | `h4_bias_aligned` | H4 BOS/CHoCH == trade direction | +0.10 |
| L4 | `cycle_phase_aligned` | cycle phase permits this trade type | mandatory |
| L5 | `session_flow_aligned` | London→NY flow confirms direction | +0.08 |
| L6 | `liquidity_swept` | PDH/PDL/EQH/EQL swept + rejected | +0.10 |
| L7 | `fvg_in_ote` | FVG or OB inside OTE 62-79% | +0.10 |
| L8 | `mss_confirmed` | M15 CHoCH after sweep | +0.10 |
| L9 | `smt_divergence` | correlated pair divergence | +0.05 |
| L10 | `killzone_active` | London/NY/Silver Bullet | +0.05 |
| L11 | `push_exhaustion_aligned` | push/exhaustion phase supports entry | +0.05 |
| L12 | `ema_aligned` | price returning to EMA20 on entry TF | +0.05 |

**A+ Setup = L1(mandatory) + L4(mandatory) + 4+ additional layers**  
**Minimum Tradeable = L1 + L4 + L3 + 1 other**  
**If any mandatory layer fails → hard veto regardless of confidence**

---

## 5. EXECUTION ENHANCEMENTS

### 5.1 Execution Agent Sees Full Context

**Current:** execution_agent.py evaluates equity, killzone, and confidence. It doesn't know the H4 just had a BOS.

**Enhanced:** Before executing, verify ALL of:
```
1. MTF alignment == "A+" or "A" (weekly/daily/h4/h1 all agree)
2. Cycle phase != "ACCUMULATION" OR confidence >= 0.75 AND entry_type == "SWEEP_REVERSAL"
3. Session flow not in "PENDING" (don't execute until London→NY direction is clear)
4. No opposing structure break in last 3 M15 candles (don't enter into a fresh CHoCH against you)
5. Price is still within OTE zone (if price ran past 79% retracement, cancel limit order)
```

### 5.2 Dynamic Position Sizing by Structure

**Current:** risk_agent computes lot size from equity × risk % only.

**Enhanced:** Size is also gated by:
```python
base_lot = equity * risk_pct / (sl_distance / pip_value)

# Structure multipliers
if mtf_alignment == "A+": size_mult *= 1.0
if mtf_alignment == "A":   size_mult *= 0.75
if mtf_alignment == "B+": size_mult *= 0.50
if cycle_phase == "DISTRIBUTION": size_mult *= 1.0
if cycle_phase == "MANIPULATION": size_mult *= 0.50
if cycle_phase == "ACCUMULATION": size_mult *= 0.25
if session_flow == "CONTINUATION": size_mult *= 1.25

final_lot = base_lot * size_mult
```

---

## 6. IMPLEMENTATION PHASES

### PHASE 1: Foundation (Days 1-3)
Build modules that don't break existing pipeline.

| Task | File | Priority |
|------|------|----------|
| 1.1 Build `weekly_bias_engine.py` | weekly_bias_engine.py | P0 |
| 1.2 Build `daily_structure_engine.py` | daily_structure_engine.py | P0 |
| 1.3 Build `market_structure_context.py` aggregator | market_structure_context.py | P0 |
| 1.4 Build `mtf_structure_aligner.py` aligner | mtf_structure_aligner.py | P0 |
| 1.5 Wire `orchestrator.py` to write `market_structure.json` every tick | orchestrator.py | P0 |

### PHASE 2: Cycle Integration (Days 4-6)
Wire cycle_tracker into the live signal pipeline.

| Task | File | Priority |
|------|------|----------|
| 2.1 Extend `cycle_tracker.py` with daily open/close tracking | cycle_tracker.py | P0 |
| 2.2 Build `cycle_phase_integrator.py` | cycle_phase_integrator.py | P0 |
| 2.3 Enhance `dual_tf_selector.py` with L1-L12 confluence + cycle phase gate | dual_tf_selector.py | P0 |
| 2.4 Add `session_memory.py` cross-session tracking | session_memory.py | P1 |
| 2.5 Update `rules.json` with new cycle-phase gates | rules.json | P1 |

### PHASE 3: Execution Intelligence (Days 7-9)
Make execution_agent context-aware.

| Task | File | Priority |
|------|------|----------|
| 3.1 Update `execution_agent.py` to read `market_structure.json` | execution_agent.py | P0 |
| 3.2 Add MTF alignment veto to execution_agent | execution_agent.py | P0 |
| 3.3 Add cycle-phase sizing rules to risk_agent | risk_agent.py | P1 |
| 3.4 Update `position_trailing_manager.py` to respect H4 structure breaks | position_trailing_manager.py | P1 |
| 3.5 Update `scaling_engine.py` to require distribution phase for scale-ins | scaling_engine.py | P1 |

### PHASE 4: Validation (Days 10-12)
Prove the system works.

| Task | Method | Expected Output |
|------|--------|-----------------|
| 4.1 Backtest new engine on 6mo XAUUSD data | `python backtester.py` --enhanced | A+ setups >= 70% win rate, B+ >= 55% |
| 4.2 Compare old vs new confluence counts | run old code → run new code | New system finds fewer signals but higher quality (fewer = better for Chris) |
| 4.3 Validate cycle_tracker on known ICT cycles | manual spot-check | Cycle tracker correctly labels known accumulation/manipulation/distribution periods |
| 4.4 Paper trade for 3 days | live paper mode | 0-2 trades/day, all A+ or A, all in distribution phase |

### PHASE 5: Polish (Days 13-14)

| Task | Detail |
|------|--------|
| 5.1 Telegram alerts show MTF alignment + cycle phase | "XAUUSD A+ | W1 BULL | D1 BULL | H4 BULL | Day 3 Dist | London→NY CONTINUATION" |
| 5.2 Dashboard shows cycle tracker visualization | Daily cycle chart with phase labels |
| 5.3 Auto-tune MIN_CONFLUENCE per cycle phase | During accumulation, require 5 layers; during distribution, 3 layers |

---

## 7. CRITICAL DESIGN DECISIONS

### Decision 1: Weekly Module or Weekly from MT5?
**Answer:** Build weekly_bias_engine.py in Python. The MT5 EA sends D1/H4/M15 data but omni_data.json currently doesn't include W1. Add W1 to the MT5 export OR compute weekly in Python from D1 bars. Computing from D1 is cleaner — no MT5 code changes needed.

### Decision 2: How Often Does Structure Update?
**Answer:** W1 updates once per week (Monday UTC). D1 updates once per day (00:00 UTC). H4 updates every 4 hours. H1/M15/M5 update every tick from MT5. The `market_structure.json` file is rebuilt every 60 seconds by orchestrator.py.

### Decision 3: What If MT5 Data Is Delayed?
**Answer:** The structure engine uses the LAST available bar. If MT5 is stale by >5 minutes, `market_structure.json` gets a `"data_stale": true` flag. execution_agent treats stale data as neutral and requires one extra confluence layer.

### Decision 4: Do We Remove Old Modules?
**Answer:** No. Keep `dual_tf_selector.py` as fallback. Activate enhanced selectors only when `market_structure.json` is present and valid. This provides graceful degradation if MT5 disconnects.

### Decision 5: Killzone Rules
**Answer:** European 07:00-12:00 UTC remains PRIMARY. The system will still find setups outside killzone (Chris's config: threshold=0.5) but enhanced confluence requires session_flow confirmation as a substitute for killzone during Asia/NY close hours.

---

## 8. FILE MANIFEST

| File | Purpose | Status |
|------|---------|--------|
| `weekly_bias_engine.py` | D1→weekly rollup, OB, BOS/CHoCH | NEW |
| `daily_structure_engine.py` | Daily open, PDH/PDL, daily FVG/OB | NEW |
| `mtf_structure_aligner.py` | Multi-TF alignment scoring | NEW |
| `cycle_phase_integrator.py` | Wire cycle into signal pipeline | NEW |
| `session_memory.py` | London→NY flow tracking | NEW |
| `market_structure_context.py` | Single JSON aggregator | NEW |
| `enhanced_dual_tf_selector.py` | 12-layer confluence + cycle + MTF | ENHANCE existing |
| `execution_agent.py` | Add MTF veto + phase sizing | PATCH existing |
| `risk_agent.py` | Add structure-aware sizing | PATCH existing |
| `orchestrator.py` | Build market_structure.json every tick | PATCH existing |
| `rules.json` | Add cycle-phase + MTF rules | PATCH existing |
| `feature_store_v28.py` | Add cycle/MTF features for ML | PATCH existing |

---

## 9. SUCCESS METRICS

| Metric | Current Target | Enhanced Target |
|--------|---------------|-----------------|
| A+ setup frequency | ~1-2 per week | ~3-5 per week |
| A+ win rate | Not tracked separately | >= 75% |
| A setup win rate | ~60% | >= 65% |
| Average R:R | 2.5:1 | 3.5:1 (wider stops on structure, bigger TP at opposing liquidity) |
| Trade frequency | 21/year | ~60-80/year (more aggressive during distribution) |
| Cycle tracker accuracy | N/A (not used) | >= 80% phase correct on known ICT examples |
| Max consecutive losses | 3 → halt | 2 → reduce size, 3 → halt |
| Average holding time | 2-4 hours | 4-12 hours (swing, not scalp) |

---

## 10. RISK: DON'T OVERCOMPLICATE

**The biggest risk:** Building so many layers that the system becomes too restrictive and never trades.

**Mitigation:**
1. Each new layer is a BONUS, not a hard filter (except L1 weekly and L4 cycle phase)
2. The enhanced system MUST still emit trades during the European window when MT5 data is flowing
3. Paper-trade for 3 days before live — count signals generated, verify A+ ratio >= 50%
4. If the system is too restrictive after Phase 3, relax confluence requirements on DISTRIBUTION phase

---

**END OF PLAN**

This plan turns your bot from a "confluence pattern matcher" into a true market-structure-aware ICT execution system. The core insight: ICT trading is not about finding patterns — it's about understanding WHERE in the institutional cycle you are, and ONLY trading when manipulation→displacement confirms across multiple timeframes.

Ready to implement upon your go-ahead.
