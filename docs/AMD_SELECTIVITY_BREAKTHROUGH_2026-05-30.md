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
