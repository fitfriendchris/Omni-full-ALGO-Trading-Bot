# 3-year validation — the honest verdict (2026-05-30)

## Data

Real spot gold from **Dukascopy**, written to the backtest's native CSV format:
- H1 17,729 bars · M15 70,877 bars · M5 212,610 bars
- Span **2023-05-31 → 2026-05-29** (3 full years, multiple regimes)
- Fetched by `python/fetch_dukascopy_history.py` (broker MidasFX spread differences
  are absorbed by the backtest's spread/slippage params, so spot is a sound proxy).

This replaces the 60-day yfinance screen that yielded only N=3 trades.

## Experiment matrix (walk-forward, stride=3, H1/M15, $0.30 spread + $0.10 slip + comm)

| Config | Trades | Win % | avg R | PF | Max consec L | Return |
|--------|-------:|------:|------:|----:|-------------:|-------:|
| Baseline — far-draw TP (legacy)      | 26 | 11.5 | −0.53 | 0.43 | 17 | −35.1% |
| TP ladder + partials, tp1=1.5R       | 27 | 25.9 | −0.43 | 0.45 |  6 | −42.2% |
| …+ regime filter (HTF trend align)   |  9 | 11.1 | −0.69 | 0.24 |  4 | −37.1% |
| TP ladder + partials, tp1=1.0R       | 29 | 37.9 | −0.33 | 0.50 |  6 | −32.8% |
| Baseline + Kronos veto (min_prob .45)|  2 | 50.0 | +0.42 | 1.80 |  1 |  −0.3% |

**Kronos note:** On the 3yr run the overlay vetoed **24 of 26** baseline setups,
keeping only 2 (1W/1L, ~flat). PF 1.80 on N=2 is statistical noise, not edge — what
actually happened is Kronos found almost nothing it would confirm and so mostly
*abstained*, which "avoids" the −35% baseline loss by not trading. Abstention is not
a money-maker. This is the overlay behaving exactly as designed: it can only *remove*
trades from a negative-edge set, never add edge. (Consistent with the 60-day A/B,
where it vetoed a loser and cut DD −39.2% → −25.6%.) Kronos earns its place only
*after* an entry shows positive raw edge — then its veto sharpens a winner instead of
emptying a loser.

## What the matrix proves

1. **The far-draw TP was the single worst design choice.** Targeting HTF
   liquidity (planned 4–11R) against ~1R stops → 11.5% hit rate. Fixing it is the
   biggest single improvement.
2. **Exit shaping massively improves robustness.** TP ladder + partials + move-to-
   break-even lifted win-rate 11.5% → 37.9% and cut max-consecutive-losses 17 → 6.
   Closer first targets (tp1 1.5R → 1.0R) raise win-rate monotonically.
3. **But expectancy stays negative in every configuration.** As the target moves
   closer, win-rate rises and per-win payoff falls in lockstep — PF crawls 0.43 →
   0.50 but never crosses 1.0. **Exit management cannot create edge; only entries
   can.** The smooth, monotonic win-rate-vs-target relationship also rules out an
   inversion bug — the mechanics are correct.
4. **Trend/regime filtering makes it WORSE** (PF 0.24, WR 11%). The trades it
   removed were the better ones → the sweep→CHoCH→OTE engine is *reversal-natured*;
   forcing trend-alignment is counterproductive. (My initial "fades the trend"
   hypothesis was wrong.)

## The verdict

**Across 3 years of real gold, the sequential ICT entry (sweep → CHoCH → OTE) has
no positive directional edge net of costs.** Even at ~1:1 reward:risk with partials
it is profit-factor 0.50. This is the money-saving finding the multi-year test was
for. **Do NOT deploy any current configuration live.**

Keep the wins that are real: the TP-ladder + partials + break-even machinery is a
genuine robustness upgrade and should stay. The problem is upstream — the entry.

## Recommended next direction — fix the ENTRY, not the exits/overlays

1. **Isolate the raw signal.** Build an entry-only study: at each confirmed
   sweep→CHoCH, take a market entry with a fixed 1R/1R bracket (no OTE limit, no
   draw). Measure P(+1R before −1R). If it's ~50%, the ICT reversal premise does
   not hold on gold intraday and no exit logic will save it.
2. **Interrogate the OTE limit-fill.** The limit entry fills on a pullback *into*
   the zone, which in a failing setup is adverse selection (fills right before
   continuation to SL). Compare limit-OTE vs market-at-CHoCH.
3. **Test higher timeframes.** M15 may be too noisy; retest H4/H1 entries where ICT
   structure is cleaner. (We now have the H1 + M5 data too.)
4. **Reconsider Kronos's role.** If the ICT entry isn't predictive, Kronos as a
   *filter* has little to improve. Its forecast could instead be evaluated as a
   *primary* signal — but that's a different strategy needing its own validation.
5. **Only after an entry shows a positive raw edge** do partials/Kronos/sizing
   compound it. Order is non-negotiable: edge first, then leverage it.

## Reusable infrastructure delivered (all committed)

- `fetch_dukascopy_history.py` — multi-year data on demand.
- Backtest: `--ltf-window/--htf-window` (tractable + leak-free), `--stride`,
  `--tp-mode ladder`/`--tp1-rr`, `--partials`/`--partial-frac`, `--regime`, `--kronos`.
- Engine: TP ladder (`Setup.tp1/tp2`), regime gate (G1b) — both optional, torch-free.
