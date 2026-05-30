# Kronos Confirmation Overlay

**What:** [Kronos](https://github.com/shiyu-coder/Kronos) is an open-source
decoder-only foundation model for OHLC "K-line" sequences (AAAI-2026), pre-trained
on 12B+ candles from 45 exchanges. It forecasts the next *N* candles.

**Best fit for this bot:** a post-gate **confirmation + confidence overlay** on the
strict sequential ICT engine — **never a signal generator.**

## Why a filter, not a generator

The 2026-05-29 deep audit named the failure: ML-as-generator (the checklist
`dual_tf_selector` noise-trader + the frozen `online_learner`) is what bled the
account. The edge is the strict ICT *sequence*. So Kronos is wired in the one way
that respects that: after the 6 ICT gates + TP produce an **actionable** Setup,
Kronos forecasts forward and answers a single question —

> Across the model's sampled forward paths, does price reach this trade's TP
> (the draw-on-liquidity) **before** its SL?

- `P(TP before SL) ≥ min_win_prob` **and** enough paths close in the trade
  direction → **confirm** (and the probability becomes a 0–1 sizing confidence).
- otherwise → **veto** (a failed `G7_KRONOS` gate; the trade is dropped).

It can only *remove* or *down-weight* a trade ICT already justified. It cannot
invent a direction, entry, or target.

## Files

| File | Role |
|------|------|
| `python/kronos_filter.py` | The overlay: lazy model loader, MC forecast, `assess()`, `evaluate_with_kronos()` |
| `python/ict_sequential.py` | Pure ICT engine (unchanged logic; gained an optional `Setup.kronos_confidence` data field — stays torch-free) |
| `python/ict_sequential_backtest.py` | `--kronos` flag to A/B the overlay on the same window |
| `requirements-kronos.txt` | Optional torch/HF deps |

## Setup (one-time)

```bash
cd ~/Omni-full-ALGO-Trading-Bot
.venv/bin/pip install -r requirements-kronos.txt
git clone https://github.com/shiyu-coder/Kronos ~/Kronos
export KRONOS_HOME=~/Kronos      # add to .env / plist to persist
```

Models auto-download from HuggingFace on first run:
`NeoQuasar/Kronos-small` (24.7M) + `NeoQuasar/Kronos-Tokenizer-base`. Apple-silicon
uses MPS automatically.

## Graceful degradation (important)

If `torch` / the Kronos repo / the model is missing or errors, the overlay
**fails open** — it returns the pure-ICT Setup unchanged so the proven path is
never blocked by an ML dependency (the bot has been bitten by dead ML before).
Set `KronosConfig(fail_open=False)` to instead veto-on-unavailable.

The overlay is also fully **lazy**: `ict_sequential.py` imports no torch, so the
live signal path keeps working with or without Kronos installed.

## Usage

### In code

```python
from kronos_filter import evaluate_with_kronos, KronosConfig

setup = evaluate_with_kronos(htf_bars, ltf_bars,
                             kronos_cfg=KronosConfig(min_win_prob=0.45))
if setup.actionable:
    # setup.kronos_confidence ∈ [0,1] (or None if overlay unavailable)
    # feed it to risk_sizing to bias size toward higher-prob setups,
    # WITHIN the existing 23% aggregate cap.
    ...
```

Drop-in: same signature/return as `ict_sequential.evaluate`.

### Prove the lift first (paper-first rule)

A/B the overlay on the same honest 60-day gold window:

```bash
cd python
../.venv/bin/python ict_sequential_backtest.py                 # pure ICT (baseline)
../.venv/bin/python ict_sequential_backtest.py --kronos        # + Kronos G7
../.venv/bin/python ict_sequential_backtest.py --kronos --kronos-min-prob 0.55
```

Expect **fewer trades, higher win-rate / avg-R**. If the overlay doesn't lift
avg-R and profit-factor on the window, it is not earning its veto — don't ship it
live. (Note: Kronos runs a transformer per actionable setup, so `--kronos`
backtests are slower; setups are rare so it's tolerable.)

## Config knobs (`KronosConfig`)

| Field | Default | Meaning |
|-------|---------|---------|
| `model_repo` | `NeoQuasar/Kronos-small` | swap to `-base` (102M) for more accuracy/cost |
| `lookback` | 256 | bars of history fed to the model (≤ `max_context` 512) |
| `pred_len` | 24 | candles forecast forward (must cover typical time-to-TP) |
| `mc_paths` | 12 | independent sampled paths → the empirical P(TP first) |
| `min_win_prob` | 0.45 | veto threshold |
| `min_dir_agreement` | 0.50 | frac of paths that must close in the trade direction |
| `fail_open` | True | model unavailable → allow (pure ICT) vs veto |

## Next steps (not yet wired to live)

1. **Sizing hook:** map `setup.kronos_confidence` to a lot band in `risk_sizing`
   (e.g. only take ≥0.55-confidence setups, or scale lot 0.5×–1× by confidence)
   — still under the 23% aggregate cap.
2. **Volatility-aware SL:** use the forecast high–low dispersion to set
   `sl_atr_buffer` dynamically instead of the fixed 0.40.
3. Only after the backtest A/B shows a real lift, wire `evaluate_with_kronos`
   into `orchestrator.py` in **paper mode** before any live use.
