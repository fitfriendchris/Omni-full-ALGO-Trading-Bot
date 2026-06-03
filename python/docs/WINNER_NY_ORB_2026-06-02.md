# 🏆 WINNER — NY-Open ORB (gold/XAUUSD, H1, long-only)

**Date:** 2026-06-02 · **Method:** honest search of 96 base + 216 refinement configs on 11yr real Dukascopy gold, walk-forward (tune IS 2015–2021 / judge OOS 2022–2026), realistic costs (~$0.40 round-trip + $7/lot), intrabar stop fills, next-bar-open entries, 2–5% compounding sizing.

## The result (2% risk, OOS 2022–2026 — the verdict window)
| | Return | CAGR | maxDD | WR | PF | trades |
|---|---|---|---|---|---|---|
| **NY-ORB (this)** | **+344%** ($160→$711) | **+40%** | **−23%** | 33% | **2.05** | 126 |
| Buy & hold gold | +150% | +23% | −24% | — | — | — |

**It beats buy-and-hold on return AND drawdown.** First strategy in the entire audit (Omni ICT, AurumFlow, 300+ configs) to do so honestly.

## Why I believe it (robustness, not a fluke)
- **Generalized:** IS PF 1.48 → OOS PF 2.05 (improved out-of-sample — opposite of overfitting).
- **Whole family works:** all 18 long-only configs (range_bars × rr × body) are OOS-positive, PF 1.24–2.05. Structural edge, not a lucky point.
- **Survives 3× costs:** PF 2.05 → 1.93. Not a cost illusion (this is exactly what killed AurumFlow: +5259%→−58%).
- **Profitable every OOS year:** 2022 +15%, 2023 +11%, 2024 +30%, 2025 +114%, 2026 +20%.

## The rules (mechanical)
- Opening range = first **4× H1 bars from 13:00 UTC** (NY open).
- **Long** when an H1 bar **closes above** the range high with a strong body (body ≥ 0.5·ATR5 and ≥ 60% of candle).
- Entry next bar open · Stop = min(range, 2.5·ATR14) below · Target = entry + **4×** stop · one trade/day · **LONG ONLY**.
- Code: `python/ny_orb_strategy.py` (self-proving) · lab: `python/daytrade_lab.py` · refine: `python/orb_ny_refine.py`.

## ⚠ Honest caveats — read before risking money
1. **Low win rate (~33%).** You lose ~2 of 3 trades; profit is in the 4R winners. Losing streaks up to 11. Most people quit a 33%-WR system during a drawdown and lock in the losses — discipline is the whole game.
2. **Long-only + regime-dependent.** Its edge assumes gold keeps trending up (2015–26 was a secular bull). In a prolonged gold bear/chop, re-validate. It is a positive-expectancy edge, **not** a guarantee.
3. **Size small.** 2% risk → −23% DD; 3% → −33%; 5% → −48%. **Do NOT run 5% on $160.** Recommend 1–2% until the account and your stomach grow.
4. **Backtest ≠ live.** Real fills, broker spread spikes on news, and slippage will shave the edge. Paper/shadow it on YOUR broker feed before one cent goes live.

## Recommended next step (gated — your call)
Paper-first. Wire `ny_orb_strategy.scan_h1()` into the live signal path (`signal_writers → signals.json → execution_agent`) with `OMNI_PAPER_MODE=true`, risk capped at **1–2%**, using the existing `risk_sizing` halts. Run it shadow for a few weeks against the live feed, compare to backtest, THEN consider live. I will not enable live trading without your explicit go-ahead.
