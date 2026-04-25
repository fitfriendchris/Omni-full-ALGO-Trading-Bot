---
name: omni-ict-trading
description: >
  Master skill for developing, enhancing, and operating the OMNI ICT Terminal — a Python/MQL5 algorithmic 
  trading system built on ICT (Inner Circle Trader) / SMC (Smart Money Concepts) methodology integrated 
  with MetaTrader 5. Use this skill whenever working on ANY component of the OMNI system: OmniExport.mq5 
  (MT5 EA), ict_precision.py (signal engine), auto_trader.py (execution engine), dashboard.py (Dash UI), 
  heatmap_engine.py (institutional flow visualization), or any enhancement to the ICT/SMC analysis 
  pipeline. Also triggers for: ICT concept implementations (FVG, OB, BOS, CHoCH, SMT, IOFED, AMD), 
  dual-timeframe analysis, institutional heatmaps, liquidity zone mapping, trade checklists, phase matrix 
  analysis, kill zone detection, Silver Bullet model, or prop firm rule compliance. If the user mentions 
  OMNI, ICT, SMC, order blocks, fair value gaps, AMD phases, kill zones, or MT5 data export — USE THIS SKILL.
---

# OMNI ICT Terminal — Master Development Skill

## System Overview

OMNI ICT Terminal is a 4→7 module Python/MQL5 trading system:

```
OmniExport.mq5 → omni_data.json → ict_precision.py → signals.json
                                 → heatmap_engine.py → heatmap_data.json  
                                 → auto_trader.py    → trader_state.json
                                 → ai_engine.py      → ai_state.json
                                 → dashboard.py      (Dash web UI, port 8050)
```

**Active blocker:** OmniExport.mq5 may not be writing to omni_data.json. Always verify this is resolved before any signal/dashboard work.

## File Locations (macOS/Wine)
```
MT5 Common Files: ~/Library/Application Support/net.metaquotes.wine.metatrader5/
                  drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/
Python scripts:   ~/[project_dir]/  (check project files in context)
Data files:       omni_data.json, ai_state.json, trader_state.json, heatmap_data.json
Database:         omni_ict.db (SQLite, trade history)
```

---

## ICT/SMC Core Concepts (Quick Reference)

### Structural Concepts
| Term | Definition | Detection Logic |
|------|-----------|----------------|
| FVG | 3-candle imbalance (Bull: C1.high < C3.low) | Check C[i-1].h vs C[i+1].l |
| OB | Last opposing candle before impulse | Find bearish candle before bullish impulse |
| BOS | New swing high/low in trend direction | Compare to prior swing |
| CHoCH | Opposing BOS (early reversal signal) | BOS against current trend |
| MSS | Displacement candle breaking structure | Large body + FVG left |
| IOFED | FVG within OTE zone after displacement | OTE = Fib 61.8–79% of leg |
| Breaker | Swept OB that flips role | Old support→resistance |
| EQH/EQL | Equal highs/lows (resting stops) | Within 3 pip tolerance |
| HVI | High Volume Imbalance (institutional bar) | Body > 60%, leaves FVG |

### AMD Phase Framework
```
Session   GMT Time    Phase         Behavior
ASIA      22–07       ACCUMULATION  Range definition, trap retail
LONDON    07–12       MANIPULATION  Stop hunts, fake reversals  
NY OPEN   12–17       DISTRIBUTION  True directional delivery
NY CLOSE  17–22       REACCUM       Setup for next session

Sub-phases exist WITHIN each session (see ULTRAPLAN Section 11).
Phases at different timeframes can CONFLICT — this is normal and actionable.
```

### Kill Zones
```
Asia Open:    22:00–01:00 GMT
London Open:  07:00–09:00 GMT  ← Primary
London Close: 11:00–13:00 GMT
NY Open:      12:00–14:00 GMT  ← Primary  
Silver Bullet 1: 10:00–11:00 EST (15:00–16:00 GMT)
Silver Bullet 2: 14:00–15:00 EST (19:00–20:00 GMT)
```

---

## 7-Layer Confluence Scoring System

Every trade setup MUST be scored on 7 layers. **Minimum 4/7 required. 6+/7 = A+ setup.**

```python
LAYERS = {
    "L1_HTF_BIAS":      {"weight": 4, "desc": "D1/H4 structure confirms direction"},
    "L2_AMD_PHASE":     {"weight": 4, "desc": "Current AMD phase matches trade"},
    "L3_KILLZONE":      {"weight": 3, "desc": "Kill zone or Silver Bullet active"},
    "L4_LIQ_SWEEP":     {"weight": 4, "desc": "Liquidity sweep confirmed"},
    "L5_FVG_OB_OTE":    {"weight": 4, "desc": "FVG or OB within OTE (61.8–79%)"},
    "L6_MSS_CHOCH":     {"weight": 4, "desc": "MSS or CHoCH on entry timeframe"},
    "L7_SMT_DIV":       {"weight": 3, "desc": "SMT divergence on correlated pair"},
}
# Score = sum of passed layer weights
# Max = 26 points. Tradeable ≥ 15 points. A+ ≥ 22 points.
```

---

## IOFED Entry Model (Highest Probability Setup)

```
Step 1: Identify displacement (large candle, FVG left behind)
Step 2: Draw Fibonacci 0→100% on displacement leg  
Step 3: OTE zone = 61.8% to 79% retracement
Step 4: Find FVG, OB, or Breaker Block WITHIN OTE zone
Step 5: MSS/CHoCH confirms on entry TF
Step 6: Entry = limit at top of FVG (bull) or bottom of FVG (bear)
Step 7: SL = below FVG low (bull) or above FVG high (bear)
Step 8: TP1 = 1:1 (25% close), TP2 = prior swing, TP3 = HTF target
```

---

## Phase Matrix — Phases Within Phases

```
                    HTF PHASE (D1/H4)
LTF (M15/H1)    ACC        MAN        DIST
ACC           [WAIT]     [WAIT]     [ALERT: setup forming]
MAN           [WAIT]     [EXECUTE]  [EXECUTE: high prob]
DIST          [SKIP]     [EXECUTE]  [EXECUTE: max size]

RULES:
- HTF DIST + LTF MAN  = High-probability sell. Stop hunt happening NOW.
- HTF ACC  + LTF MAN  = Sweep of HTF lows before bullish reversal. BUY dip.
- Conflicting phases   = 50% size or skip.
- Same phase all TFs   = Maximum conviction. Full size.
```

---

## 72-Point Trade Checklist (Summary)

**Section A: HTF Narrative (20pts)** — W1+D1+H4 structure, premium/discount, IPDA zone  
**Section B: AMD Phase (15pts)** — Session phase, HTF+LTF phase alignment, manipulation complete  
**Section C: Session/Kill Zone (10pts)** — Kill zone active, Silver Bullet window, news clear  
**Section D: Liquidity (12pts)** — Pool identified, sweep confirmed, engineered run  
**Section E: Entry Model (20pts)** — OB/FVG/OTE, IOFED confluence, MSS/CHoCH  
**Section F: SMT/Correlation (8pts)** — Divergence, DXY inverse, correlated pairs  
**Section G: Risk (binary pass/fail)** — SL defined, RR≥1:3, risk≤1%, no revenge trade

**Thresholds:** A+(80+/85) = execute full size | A(70+) = execute | B(55+) = half size | Skip(<55)

Full 72-point detail: See ULTRAPLAN Section 9.

---

## Dashboard v5 Architecture

```
Tab 1: OMNI COMMAND   — Live signals, equity, AMD status, news timer
Tab 2: HEATMAP GRID   — VPVR, liquidity zones, FVG map, SMT radar
Tab 3: PHASE MATRIX   — All symbols × all TFs AMD phase grid
Tab 4: CHECKLIST      — Interactive 72-point live scorecard
Tab 5: PERFORMANCE    — Stats by session/phase/model, Sharpe, PF
Tab 6: JOURNAL        — Annotated trades with ICT concept tags
```

---

## Heatmap Engine (heatmap_engine.py)

Three heatmap types + SMT detector:

1. **Volume Profile (VPVR):** Price-volume histogram, identifies POC/VAH/VAL
   - Color: `#1a1200` → `#FFD700` (dark to gold by volume intensity)

2. **Liquidity Zone Map:** EQH/EQL + PDH/PDL/PWH/PWL + session H/L density
   - BSL (buy-side) = green glow above price
   - SSL (sell-side) = red glow below price

3. **Order Flow Imbalance:** FVG rectangles overlaid on chart
   - Bullish FVG: `rgba(0,255,200,0.15)`
   - Bearish FVG: `rgba(255,60,100,0.15)`  
   - HVI (institutional): gold border `rgba(255,215,0,0.4)`

4. **SMT Divergence Radar:** Spider chart for 4 correlated pairs

---

## MT5 Data Blocker — Fix Protocol

If `omni_data.json` is not being written:
1. In MT5 Experts log, add: `Print(TerminalInfoString(TERMINAL_COMMONDATA_PATH));`
2. In Python, use multi-path resolver (see ULTRAPLAN Section 3)
3. Run `python dashboard.py --debug-path` to see all candidate paths
4. Install `watchdog` for file-change trigger (replace 3s poll)
5. Check Wine path mapping: `~/Library/Application Support/net.metaquotes.wine.metatrader5/...`

---

## OmniExport v4 — New Data Fields

Enhanced exports to add to the EA:
- 6 timeframes per symbol (M5/M15/H1/H4/D1/W1), 200 bars each
- `tick_volume`, `spread`, `body_pct`, `wick_ratio` per bar
- `key_levels`: PDH/PDL/PWH/PWL/PMH/PML/EQH/EQL
- `ipda`: 20/40/60-day range, equilibrium price
- `kill_zones`: structured object with Silver Bullet status
- `smt_pairs`: correlated instrument + divergence flag
- `session_context`: sub-session phase within main session

---

## Code Standards for OMNI System

```python
# Signal dataclass pattern
@dataclass
class ICTSetup:
    symbol: str
    direction: str          # BUY | SELL
    entry_type: str         # IOFED | OB_RETEST | SILVER_BULLET | SWEEP_REVERSAL
    confluence: ConfluenceScore  # 7-layer scoring object
    checklist_score: int    # 0–85 (72-point checklist)
    checklist_grade: str    # A+ | A | B | SKIP
    htf_phase: str          # HTF AMD phase
    ltf_phase: str          # LTF AMD phase
    phase_alignment: str    # STRONG | MODERATE | CONFLICT
    entry_price: float
    sl_price: float
    tp1_price: float        # 1:1 (25% close)
    tp2_price: float        # 1:2 (30% close)  
    tp3_price: float        # Runner (45% close)
    invalidation: float     # Breaks HTF narrative
    rr_ratio: float
    iofed_active: bool
    silver_bullet: bool
    session: str
    kill_zone: str
    smt_divergence: bool
```

---

## Development Priorities (from ULTRAPLAN)

**SPRINT 1 (Blocker):** Fix MT5 data pipeline → verify live data in dashboard  
**SPRINT 2 (Intelligence):** 7-layer scorer, IOFED, EQH/EQL, Silver Bullet, top-down  
**SPRINT 3 (Heatmaps):** heatmap_engine.py, FVG map, SMT detector, heatmap_data.json  
**SPRINT 4 (Dashboard):** Phase Matrix tab, Heatmap tab, Checklist tab, enhanced analytics  
**SPRINT 5 (Execution):** Checklist gate in auto_trader, phase sizing, adaptive stops  

**For full spec of any sprint, reference: OMNI_ULTRAPLAN.md**

---

## Research Baseline (for AI Engine prompts)

Proven profitable ICT/SMC patterns from prop firm data:
- **Minimum RR:** 1:3 for IOFED setups (often 1:5–1:6 achievable)
- **Best sessions:** London Open KZ + NY Open KZ + Silver Bullet windows
- **Top models:** IOFED (highest precision), Silver Bullet (time-reliable), OB Retest
- **Phase alignment stat:** HTF+LTF aligned → ~65% win rate; conflicting → ~40%
- **Kill zone filter:** 78% of winning trades occur within kill zones
- **SMT divergence boost:** Adds ~12% to win rate when present
- **News avoidance:** 30-min pre / 15-min post high-impact news eliminates slippage blowups
- **IPDA lookback:** Price delivers to 20/40-day highs/lows with statistical regularity

---

*OMNI ICT Terminal Skill v1.0 — Covers all development, analysis, and enhancement tasks for the full system.*
