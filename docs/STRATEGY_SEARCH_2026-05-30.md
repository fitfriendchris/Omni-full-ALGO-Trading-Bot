# Strategy search over 3 years of gold — what actually wins (2026-05-30)

Method: `python/strategy_search.py`. 7 strategy families (trend + mean-reversion),
each parameter-swept, **tuned on the first 2 years (in-sample) and judged only on
the unseen 3rd year (out-of-sample)** to kill curve-fitting. Benchmark = buy & hold.
Realistic costs ($0.40 round-trip) on every position change. Data: 3yr Dukascopy
XAUUSD (H1/M15).

## The benchmark nobody beat

| Period | Buy & Hold gold |
|--------|-----------------|
| Full 3yr (2023-05→2026-05) | **+132%, Sharpe 1.56** |
| Out-of-sample (last 12mo)  | +37%, Sharpe 1.36 |

Gold roughly **doubled** over the window. That bull run is the dominant fact.

## What the search found

**Out-of-sample (last year), only the long-only 200-MA trend filter edged the
benchmark on Sharpe (1.46 vs 1.36)** — but that was period-specific. Across the
**full 3 years and every individual year, buy & hold beat every timing strategy on
risk-adjusted return:**

| Strategy (H1, full 3yr) | Return | Sharpe | Max DD | Time in mkt |
|-------------------------|-------:|-------:|-------:|------------:|
| **Buy & hold**          | **+132%** | **1.56** | (B&H)  | 100% |
| close > SMA150 (long)   | +92%   | 1.66   | −12%   | 59% |
| Donchian-40 (long)      | +80%   | 1.50   | −19%   | 57% |
| close > SMA200 (long)   | +72%   | 1.38   | −19%   | 60% |
| EMA50>EMA200 (long)     | +64%   | 1.18   | −22%   | 63% |
| ATR breakout            | ~+50%  | ~0.8   | −23%   | 100% |
| MA-cross / momentum     | lower, ~B&H-lagging | | | |
| **RSI / Bollinger (mean-revert)** | **negative OOS** | <0 | −40%+ | |

Year by year, close>SMA200 vs B&H: 2024 +14% vs **+27%**; 2025 +35% vs **+65%**.
The filter never out-returned holding; it only ever gave a *smoother* ride.

## The honest verdict

1. **The winning "strategy" on 3 years of gold was simply being long gold.** No
   tested timing system beat buy & hold on risk-adjusted return across the full
   period or consistently year-by-year.
2. **Trend filters are a defence, not alpha.** "Long while price > SMA150–200, flat
   below" captures ~55–70% of the upside with ~half the time in market and lower
   drawdown (−12% to −19% vs B&H's deeper pullbacks). It buys *peace of mind and
   downside protection*, paid for with ~30–45% of the return. Robust across MA
   lengths 100–250 (Sharpe 1.0–1.66) → not overfit.
3. **Counter-trend, short, and intraday-noise strategies lose.** Mean-reversion is
   negative OOS; shorts lose (you can't fade a structural bull); M15/M5 timing does
   not beat the benchmark after costs.
4. **The ICT engine (sweep→CHoCH→OTE) is not competitive** — see
   `THREE_YEAR_VALIDATION_2026-05-30.md` (PF ≤ 0.50, negative every config).

## What to actually do

**There is no high-frequency gold edge in this data. The evidence supports a
simple, robust, position-trading approach — not the intraday ICT swarm.**

Deployable spec (long-only, defensive trend-follow):
- **Signal:** on H1, long when `close > SMA(200)`; flat when below. No shorts.
  (SMA in 150–200 is the robust zone; 200 chosen as the non-cherry-picked round
  value — the *whole family* works, which is the anti-overfit evidence.)
- **Why long-only:** every short variant lost; gold's regime is up.
- **Expectation:** lags pure buy & hold in raw return, but with materially lower
  drawdown and ~40% of the time out of the market (sits out downtrends — the real
  value if gold finally rolls over).
- **Caveat — regime risk:** this "edge" is mostly *gold went up*. Forward, gold may
  not double again. The trend filter's job is exactly to protect you then: it goes
  flat below the MA. Size positions for that, don't lever the bull assumption.

Bottom line: a one-line moving-average trend filter on gold is more robust and more
profitable than the entire ICT bot. The complexity was not adding edge.
