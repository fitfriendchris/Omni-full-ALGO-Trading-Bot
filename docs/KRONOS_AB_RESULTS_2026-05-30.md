# Kronos A/B — 60-day gold screen (2026-05-30)

Window: `GC=F` H1/M15 via yfinance, 2026-03-19 → 2026-05-29 (HTF=1132 bars, LTF=4526).
Engine: `ict_sequential` (strict gated). Overlay: `kronos_filter` G7, Kronos-small,
6 MC paths, `min_win_prob=0.45`. Run on `.venv-kronos` (py3.12, CPU torch 2.2.2).

| Metric            | Baseline (pure ICT) | + Kronos G7 |
|-------------------|---------------------|-------------|
| Trades            | 3                   | 2           |
| Wins / Losses     | 0 / 3               | 0 / 2       |
| Total R           | -3.04               | -2.03       |
| Avg R             | -1.01               | -1.02       |
| Return %          | -39.2               | -25.6       |
| Verdict           | NO EDGE             | NO EDGE     |

## Read

- **Mechanically validated.** Kronos loads, forecasts real gold candles, and G7
  vetoed 1 of 3 setups — that one was a loser, so DD shrank -39.2% → -25.6%.
- **Statistically meaningless.** N=3 is noise. Both configs went 0-for-everything;
  with no wins there is no win-rate for the overlay to lift. The *base ICT engine
  shows no edge on this recent regime* — an overlay can only filter, not create edge.
- **Do NOT deploy live.** Neither config earned it.

## Why N is so small

H1/M15 yfinance intraday is capped at ~60 days, and the strict sequence fires
rarely → only 3 actionable setups. Need multi-year broker data (`--source mt5`
after `OmniHistoryExport.mq5`) or a looser LTF (M5) to reach a meaningful sample.

## Environment note

The main bot venv is Python 3.14 (no torch wheels exist). The Kronos overlay runs
only under `.venv-kronos` (py3.12). Any live wiring must account for this.

See `WHY_LOSING_AND_PLAN_2026-05-30.md` for the loss diagnosis + improvement plan.
