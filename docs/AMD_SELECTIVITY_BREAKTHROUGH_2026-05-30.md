# AMD engine + the selectivity finding (2026-05-30)

`python/ict_amd.py` encodes Chris's exact process as a strict gated state machine:
**BIAS → Accumulation (range at a major HTF OB) → Manipulation (judas sweep of the
range, against bias, then reclaim) → Distribution (LTF structure shift confirms the
reversal; enter at the reversal OB/FVG; target the opposing liquidity).** TP ladder
(tp1 = opposite range extreme, tp2 = major opposing HTF OB) + partials + breakeven.

## The finding: selectivity IS the edge

Loose mechanical AMD (take every match) loses. As each rule Chris actually stated is
enforced, profit factor climbs monotonically and the trade count collapses to the
handful of A+ setups he takes. 3yr gold, walk-forward, partials, IS = first 2/3,
OOS = last 1/3:

| Variant (each adds a stated rule) | IS T | IS WR | IS PF | OOS T | OOS WR | OOS PF |
|-----------------------------------|-----:|------:|------:|------:|-------:|-------:|
| V0 loose (all matches)            | 129  | 24.8  | 0.54  | 85    | 37.6   | 1.00   |
| V1 + major-OB anchor              |  44  | 27.3  | 0.65  | 11    | 36.4   | 0.94   |
| V2 + tight accumulation (≤3 ATR)  |  24  | 41.7  | 1.09  |  6    | 50.0   | 1.37   |
| V3 + real sweep (≥0.25 ATR)       |  20  | 50.0  | 1.41  |  1    | —      | —      |
| V4 + killzone only                |  19  | 52.6  | 1.57  |  0    | —      | —      |

This reverses the earlier "no edge" verdict for the *loose* engines: the prior
sequential/AMD failures took 129+ junk trades. With Chris's selectivity, PF goes
0.54 → 1.57 and win-rate → ~50%. **The edge was never in the trigger; it was in what
NOT to trade.**

## The honest caveat — underpowered

The strict config fires only ~7–10×/year, so 3 years = 20–30 trades. PF 1.37 on 6
OOS trades is encouraging, not proven; V3/V4 had ~0–1 OOS trades. Frequency also
looked regime-sensitive (fewer clean AMD ranges in the last-year strong trend).
**Verdict: promising, not yet deployable. Needs statistical power.**

## Next (in progress)

1. **More data** — pulling ~11yr gold so the strict config yields 100+ trades →
   real walk-forward power across many regimes.
2. **Cross-instrument** — apply the same rules to XAGUSD (+ FX majors) to test
   robustness (an edge that only works on one symbol/period is suspect).
3. **Lock the setup criteria** = V3 (major-OB anchor + tight accumulation + real
   sweep), then build the full 8-process production system (prep → setup → exec →
   manage → risk → session → journal → discipline) around this validated core.

Only after the edge holds OOS on 100+ trades does it earn paper-mode, then live.

---

## UPDATE — 11-year validation: the 3yr result did NOT hold

Pulled 11yr gold (H1 65k / M15 260k bars, 2015→2026) and re-ran the strict configs
walk-forward (IS 2015–22, OOS 2022–26), partials, stride 4:

| Config | Set | T | WR% | PF | avgR | ret% |
|--------|-----|--:|----:|---:|-----:|-----:|
| V2 OB+tightAccum | IS  | 43 | 37.2 | 0.68 | −0.22 | −32.8 |
| V2 OB+tightAccum | OOS | 27 | 33.3 | 0.81 | −0.14 |  −2.5 |
| V3 +realSweep    | IS  | 37 | 40.5 | 0.83 | −0.11 | −23.9 |
| V3 +realSweep    | OOS | 20 | 35.0 | 1.03 | +0.02 |  +0.1 |

**Verdict: break-even at best, not a deployable autonomous edge.** The 3yr PF 1.41 /
1.37 was small-sample + the recent gold-bull regime, not a durable edge. Selectivity
genuinely lifted PF (0.54 → ~1.0) — the structure has *signal* — but mechanical
rules alone top out at break-even across 11 years. Win-rate ~35% means the reversal
entries reach the opposite-extreme target only a third of the time.

### What this means (honest)

The live discretionary edge is real but is **not fully reducible to these mechanical
rules** — it lives in judgment (which ~1 of 3 mechanical setups is actually A+),
entry execution/RR, and dynamic management. That is the norm for skilled discretionary
traders, not a flaw. Chasing a green 11yr backtest by tweaking further would be
overfitting.

### The realistic, valuable build

1. **Setup scanner + decision-support** (not full autonomy): the bot runs the whole
   process — bias, levels, OBs, news, session — detects AMD setups live, grades/ranks
   them, shows context + RR + target, and ALERTS Chris. He applies the final
   discretionary filter and manages. Automates the tedious 90%; keeps the edge in the
   loop.
2. **Automated risk + discipline engine** (fully automatable, high value): sizing
   under the 23% cap, daily-loss halt, max-trades, profit lock.
3. **Journal every setup + outcome** → measure which setups/contexts actually pay for
   Chris, and tighten the scanner's grading from his real results over time.

Capturable mechanical refinements worth ONE validated test each (not guaranteed):
closer reachable tp1 (raise WR), runner/trail to capture big distributions, a
displacement-strength filter on the reversal, news/session gating.
