# Why the sequential ICT engine loses — diagnosis + improvement plan (2026-05-30)

## Evidence (the only 3 trades on the 60-day GC=F H1/M15 window)

| # | Dir  | Entry   | SL      | TP      | Risk $ | Reward $ | Planned RR | Result      |
|---|------|---------|---------|---------|--------|----------|-----------|-------------|
| 1 | BEAR | 4424.15 | 4447.79 | 4322.0  | 23.6   | 102.2    | **4.3R**  | −1.01R (SL) |
| 2 | BULL | 4544.56 | 4526.73 | 4738.2  | 17.8   | 193.6    | **10.9R** | −1.01R (SL) |
| 3 | BEAR | 4740.03 | 4749.99 | 4720.6  | 10.0   | 19.4     | 1.95R     | −1.02R (SL) |

Regime over the window: gold's 90-day daily tape ranged **4376–5294** (a ~$900 / 20%
swing), net **−10%**, with only **27% of days above the 20-day MA** → **choppy /
whipsaw, no persistent trend.** The M15 window caught a counter-bounce inside it.

## Root causes (ranked by impact)

### 1. TP is targeted at the far HTF "draw" — demanding monster moves (PRIMARY)
`ict_sequential.evaluate()` sets `TP = draw` (nearest *opposing HTF liquidity*) and
only enforces a 2R **minimum**, with **no cap**. Result: planned RRs of 4.3R and
**10.9R** against $10–24 stops. To win, price must travel $100–194 while a $10–24
adverse wiggle ends it. Even a correct entry almost never runs 5–10R without first
retracing through a tight stop. **This single asymmetry explains 0/3 wins** and is
regime-independent. High "planned RR" is a vanity metric here.

### 2. No trade management — every loss is a full −1R (SECONDARY)
There is no break-even move, no partial profit, no time stop. All three lost the
full −1R. Trade 2 (a *with-direction* long) was stopped on a pullback before its
huge target; trade 3 was stopped almost instantly ($10 of room). With BE-at-+1R
and a partial at the first opposing pool, losers become scratches and small wins.

### 3. No regime filter — trades reversal setups into chop (TERTIARY)
In a 20%-range whipsaw, every swept high/low looks like a reversal and isn't. The
engine has no "stand aside when the tape is choppy / not trending" gate and no
momentum/HTF-trend alignment, so it fires sweep→CHoCH reversals that get whipsawed.
Two of three were shorts caught in the counter-bounce.

### 4. Statistically blind — N=3 (CROSS-CUTTING)
60 days of H1/M15 yfinance yields ~3 setups. Nothing — engine or Kronos — can be
validated on this. Need multi-year data and more setups per regime.

## The plan — drastically improve, in priority order

Each item: the lever, the file, and how we'll *prove* it (paper-first, A/B on the
same harness). **No live trading until a multi-regime backtest shows positive
expectancy.**

### P1 — Realistic TP + partial scaling (biggest expected lift)
- In `ict_sequential.py`: stop using the raw far draw as the only TP. Return a
  **TP ladder**: `TP1 = nearest opposing *minor* pool or a fixed 2R` (whichever is
  closer), `TP2 = next pool`, `runner → draw`. Cap planned RR sanity (e.g. ignore
  draws beyond ~6R for the primary target).
- In `ict_sequential_backtest.py`: model partials — take 50% at TP1, move SL→BE,
  trail the runner. Report realized R with management on.
- Prove: rerun the 60-day A/B; expect win-rate ↑ and avg-R > 0 even with the same
  entries, because TP1 is now reachable.

### P2 — Break-even + time stop + structure trailing
- Backtest + live manager (`agents/position_trailing_manager.py`,
  `smart_trailing_stop.py`): SL→BE once price hits +1R; cancel/stop if not +1R
  within N bars (time stop); trail behind LTF structure after TP1.
- Prove: A/B "management ON vs OFF" — expect max-DD ↓ and full-loss count ↓.

### P3 — Regime / momentum alignment gate (a new G0)
- New filter in `ict_sequential.py` (or a small `regime.py`): classify the tape as
  TREND-UP / TREND-DOWN / CHOP using HTF (D1/H4) structure + an EMA/ADX read. In
  CHOP, **require a higher bar** (only A+ sweeps at HTF extremes) or stand aside; in
  a trend, only take **with-trend** entries unless a counter-trend setup clears a
  strict premium/discount + displacement test.
- Prove: A/B with the gate on a window that contains both trend and chop.

### P4 — Stop sizing vs gold noise (size down, don't tighten)
- Keep structure-based SL but add an **ATR floor** so stops aren't $10 in a tape
  that wiggles $10 routinely; absorb the wider stop with **smaller lots** via
  `risk_sizing.py` (already supports this under the 23% aggregate cap), not by
  moving the stop closer.
- Prove: fewer instant stop-outs in the trade log.

### P5 — Kronos as the probabilistic continuation veto (now well-motivated)
- The A/B already showed G7 vetoes losers (it cut 1 of 3). Raise `min_win_prob` to
  ~0.55–0.60 and keep `min_dir_agreement` so Kronos specifically blocks
  reversal-into-momentum and chop setups. Feed `kronos_confidence` into `risk_sizing`
  to scale lots toward higher-prob setups.
- Prove: A/B `--kronos` on the P1–P3 engine; it must lift avg-R/PF to keep its veto.

### P6 — Real validation infrastructure (unblocks everything)
- Export multi-year broker history via `OmniHistoryExport.mq5` → run with
  `--source mt5`. Add an **M5 LTF** option for more setups. Build a **regime-split
  walk-forward** (trend vs chop buckets) so we never judge on N=3 again.
- Target a go/no-go bar: ≥100 trades, PF ≥ 1.3, avg-R > 0 across both buckets.

## Sequencing

1. **P6 first** (data) — otherwise nothing below is measurable.
2. **P1 + P2** (TP ladder + management) — the largest, most certain lift.
3. **P3** (regime gate) — removes the worst class of entries.
4. **P4** (stop/size) — reduces noise stop-outs.
5. **P5** (Kronos tuning) — the probabilistic cherry on top, only after the base
   engine has a real edge to protect.

Order matters: an overlay (Kronos) can only *filter* an edge, never create one.
Fix the TP/management/regime base first, then let Kronos shave the remainder.
