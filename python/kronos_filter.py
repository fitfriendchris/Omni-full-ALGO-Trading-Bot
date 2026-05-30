"""
kronos_filter.py — Kronos probabilistic CONFIRMATION overlay for ict_sequential.

WHY THIS IS A FILTER, NEVER A GENERATOR
=======================================
The 2026-05-29 audit named the failure mode plainly: ML-as-GENERATOR is what
bled the account. The legacy `dual_tf_selector` was a checklist noise-trader,
and `online_learner` ran forever on a frozen 200-trade seed. The edge is the
strict ICT *sequence* (`ict_sequential.py`), not a model's opinion.

So Kronos (https://github.com/shiyu-coder/Kronos — a decoder-only foundation
model for OHLC "K-line" sequences, AAAI-2026) is wired in the ONLY way that
respects that lesson: as a post-gate CONFIRMATION + CONFIDENCE overlay.

ROLE
----
After the 6 ICT gates + TP produce an *actionable* Setup, Kronos forecasts the
next N candles (Monte-Carlo sampled paths) and answers ONE question:

    "Across the model's forward distribution, does price reach THIS trade's TP
     (the draw-on-liquidity) before its SL?"

  * prob_tp_first  >= min_win_prob   -> allow (and pass confidence to sizing)
  * otherwise                        -> VETO (append a failed G7 gate)

It never proposes a direction, an entry, or a target. It can only *remove* or
*down-weight* a trade the ICT sequence already justified.

GRACEFUL DEGRADATION (critical — the bot has been bitten by dead ML before)
---------------------------------------------------------------------------
`torch` and the Kronos model may be absent, downloading, or slow. EVERYTHING is
lazy and wrapped: if the model can't load, the overlay FAILS OPEN — it returns
the original Setup unchanged so the proven pure-ICT path is never blocked by an
ML dependency. Set `fail_open=False` to instead veto-on-unavailable (paranoid).

SETUP (one-time)
----------------
    pip install -r requirements-kronos.txt          # torch, einops, huggingface_hub, tqdm
    git clone https://github.com/shiyu-coder/Kronos ~/Kronos   # the `model` package
    export KRONOS_HOME=~/Kronos                      # or set kronos_home in KronosConfig
Models auto-download from HuggingFace on first use (NeoQuasar/Kronos-small +
NeoQuasar/Kronos-Tokenizer-base, ~25M params — fine on Apple-silicon MPS/CPU).

Run `python kronos_filter.py` for a dependency-free self-test (uses a stub
forecaster so it works even without torch installed).
"""

from __future__ import annotations

import os
import sys
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Callable

# ict_sequential is pure (no torch); importing it here is cheap and safe.
from ict_sequential import (
    Bar, Setup, Gate, SequentialConfig, evaluate as _ict_evaluate,
)


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class KronosConfig:
    enabled: bool = True

    # HuggingFace repos (open-source). small = 24.7M / 512 ctx — best CPU/MPS fit.
    model_repo: str = "NeoQuasar/Kronos-small"
    tokenizer_repo: str = "NeoQuasar/Kronos-Tokenizer-base"
    # Path to a clone of https://github.com/shiyu-coder/Kronos (provides `model`).
    kronos_home: Optional[str] = None          # falls back to $KRONOS_HOME
    device: str = "auto"                        # auto -> mps > cuda > cpu

    # Forecast window.
    max_context: int = 512                      # model hard cap (small/base = 512)
    lookback: int = 256                         # bars of history fed to the model
    pred_len: int = 24                          # candles to forecast forward
    bar_seconds: Optional[int] = None           # None -> infer from bar spacing

    # Sampling. We run `mc_paths` independent draws (sample_count=1 each) to build
    # an empirical TP-before-SL probability, not a single smoothed path.
    mc_paths: int = 12
    temperature: float = 1.0                    # Kronos `T`
    top_p: float = 0.9

    # Decision thresholds.
    min_win_prob: float = 0.45                  # veto below this modeled P(TP first)
    min_dir_agreement: float = 0.50             # frac of paths closing in trade dir
    fail_open: bool = True                      # model unavailable -> allow (pure ICT)

    def resolved_home(self) -> Optional[str]:
        return self.kronos_home or os.environ.get("KRONOS_HOME")


# ──────────────────────────────────────────────────────────────────────────────
# Verdict
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class KronosVerdict:
    available: bool                 # did the model actually run?
    allow: bool                     # keep the trade?
    prob_tp_first: float = 0.0      # empirical P(TP reached before SL)
    dir_agreement: float = 0.0      # frac of paths whose final close is in dir
    confidence: float = 0.0         # 0..1 size-scaling signal (== prob_tp_first)
    n_paths: int = 0
    reason: str = ""

    def gate(self) -> Gate:
        return Gate("G7_KRONOS", self.allow, self.reason)


# ──────────────────────────────────────────────────────────────────────────────
# Lazy model loader (the only place torch is touched)
# ──────────────────────────────────────────────────────────────────────────────

_PREDICTOR = None            # cached KronosPredictor
_LOAD_ERROR: Optional[str] = None


def _pick_device(pref: str) -> str:
    try:
        import torch
        if pref != "auto":
            return pref
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _load_predictor(cfg: KronosConfig):
    """Import + build a KronosPredictor, cached. Returns None on any failure
    (and records why in _LOAD_ERROR). Never raises."""
    global _PREDICTOR, _LOAD_ERROR
    if _PREDICTOR is not None:
        return _PREDICTOR
    if _LOAD_ERROR is not None:
        return None                                  # don't retry a known-bad load
    try:
        home = cfg.resolved_home()
        if home and home not in sys.path:
            sys.path.insert(0, home)
        # The Kronos repo exposes these at top level (`model/__init__.py`).
        from model import Kronos, KronosTokenizer, KronosPredictor  # type: ignore

        device = _pick_device(cfg.device)
        tok = KronosTokenizer.from_pretrained(cfg.tokenizer_repo)
        mdl = Kronos.from_pretrained(cfg.model_repo)
        _PREDICTOR = KronosPredictor(mdl, tok, device=device, max_context=cfg.max_context)
        return _PREDICTOR
    except Exception as e:                           # ImportError, network, OOM, …
        _LOAD_ERROR = f"{type(e).__name__}: {e}"
        return None


# A swappable forecaster hook so the backtest / tests can inject a stub without
# torch. Signature: (bars, cfg, n_paths) -> List[List[Bar]]  (forward OHLC paths).
ForecasterFn = Callable[[List[Bar], KronosConfig, int], List[List[Bar]]]
_FORECASTER_OVERRIDE: Optional[ForecasterFn] = None


def set_forecaster(fn: Optional[ForecasterFn]) -> None:
    """Inject a custom forecaster (e.g. a deterministic stub for backtests/tests).
    Pass None to restore the real Kronos path."""
    global _FORECASTER_OVERRIDE
    _FORECASTER_OVERRIDE = fn


def _infer_step_seconds(bars: List[Bar], cfg: KronosConfig) -> int:
    if cfg.bar_seconds:
        return cfg.bar_seconds
    deltas = [int(bars[i].time - bars[i - 1].time)
              for i in range(1, len(bars)) if bars[i].time > bars[i - 1].time]
    if not deltas:
        return 900                                   # default M15
    return max(1, int(statistics.median(deltas)))


def _kronos_forecast(bars: List[Bar], cfg: KronosConfig, n_paths: int) -> List[List[Bar]]:
    """Real Kronos forecast -> list of forward OHLC paths (each `pred_len` bars).
    Returns [] if the model is unavailable."""
    pred = _load_predictor(cfg)
    if pred is None:
        return []
    try:
        import pandas as pd

        hist = bars[-cfg.lookback:]
        df = pd.DataFrame({
            "open":  [b.open for b in hist],
            "high":  [b.high for b in hist],
            "low":   [b.low for b in hist],
            "close": [b.close for b in hist],
        })
        step = _infer_step_seconds(hist, cfg)
        x_ts = pd.to_datetime([int(b.time) for b in hist], unit="s")
        last = int(hist[-1].time)
        y_ts = pd.to_datetime([last + step * (k + 1) for k in range(cfg.pred_len)], unit="s")
        x_ts = pd.Series(x_ts)
        y_ts = pd.Series(y_ts)

        paths: List[List[Bar]] = []
        for _ in range(max(1, n_paths)):
            out = pred.predict(
                df=df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=cfg.pred_len,
                T=cfg.temperature, top_p=cfg.top_p, sample_count=1,
            )
            path = [Bar(time=float((last + step * (k + 1))),
                        open=float(out["open"].iloc[k]), high=float(out["high"].iloc[k]),
                        low=float(out["low"].iloc[k]), close=float(out["close"].iloc[k]))
                    for k in range(len(out))]
            paths.append(path)
        return paths
    except Exception as e:
        global _LOAD_ERROR
        _LOAD_ERROR = f"predict failed: {type(e).__name__}: {e}"
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Path analysis — TP-before-SL along each forecast path
# ──────────────────────────────────────────────────────────────────────────────

def _path_outcome(path: List[Bar], direction: str, entry: float,
                  sl: float, tp: float) -> str:
    """Walk one forecast path bar-by-bar. Return 'WIN' if TP is touched before SL,
    'LOSS' if SL first, 'NONE' if neither within the horizon. Pessimistic tie
    (a bar spanning both) counts as LOSS — matches the backtest convention."""
    for b in path:
        if direction == "BULL":
            hit_sl, hit_tp = b.low <= sl, b.high >= tp
        else:
            hit_sl, hit_tp = b.high >= sl, b.low <= tp
        if hit_sl:
            return "LOSS"            # tie -> SL first (pessimistic)
        if hit_tp:
            return "WIN"
    return "NONE"


def assess(ltf_bars: List[Bar], setup: Setup, cfg: Optional[KronosConfig] = None
           ) -> KronosVerdict:
    """Score an actionable Setup against Kronos's forward distribution."""
    cfg = cfg or KronosConfig()

    if not cfg.enabled:
        return KronosVerdict(available=False, allow=True, reason="kronos disabled (pass-through)")
    if not setup.actionable or setup.entry is None or setup.sl is None or setup.tp is None:
        return KronosVerdict(available=False, allow=setup.actionable,
                             reason="no actionable setup to confirm")

    forecaster = _FORECASTER_OVERRIDE or _kronos_forecast
    paths = forecaster(ltf_bars, cfg, cfg.mc_paths)

    if not paths:
        reason = _LOAD_ERROR or "model unavailable"
        if cfg.fail_open:
            return KronosVerdict(available=False, allow=True,
                                 reason=f"kronos unavailable -> FAIL-OPEN (pure ICT): {reason}")
        return KronosVerdict(available=False, allow=False,
                             reason=f"kronos unavailable -> VETO (fail_open=False): {reason}")

    wins = nones = 0
    dir_ok = 0
    for p in paths:
        if not p:
            continue
        outcome = _path_outcome(p, setup.direction, setup.entry, setup.sl, setup.tp)
        if outcome == "WIN":
            wins += 1
        elif outcome == "NONE":
            nones += 1
        final = p[-1].close
        if (setup.direction == "BULL" and final > setup.entry) or \
           (setup.direction == "BEAR" and final < setup.entry):
            dir_ok += 1

    n = len(paths)
    prob = wins / n if n else 0.0
    agree = dir_ok / n if n else 0.0
    allow = prob >= cfg.min_win_prob and agree >= cfg.min_dir_agreement
    verdict = (f"P(TP<SL)={prob:.2f} dir_agree={agree:.2f} over {n} paths "
               f"(thr p>={cfg.min_win_prob:.2f}, dir>={cfg.min_dir_agreement:.2f}) "
               f"-> {'CONFIRM' if allow else 'VETO'}"
               + (f"; {nones} path(s) reached neither" if nones else ""))
    return KronosVerdict(available=True, allow=allow, prob_tp_first=round(prob, 3),
                         dir_agreement=round(agree, 3), confidence=round(prob, 3),
                         n_paths=n, reason=verdict)


# ──────────────────────────────────────────────────────────────────────────────
# Public API — drop-in wrapper around ict_sequential.evaluate
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_with_kronos(htf_bars: List[Bar], ltf_bars: List[Bar],
                         cfg: Optional[SequentialConfig] = None,
                         kronos_cfg: Optional[KronosConfig] = None,
                         now_ts: Optional[float] = None) -> Setup:
    """Run the strict ICT sequence, then apply the Kronos confirmation gate.

    Identical contract to `ict_sequential.evaluate`: returns a Setup. If the ICT
    sequence is not actionable, returns it untouched (Kronos never *creates* a
    trade). If actionable, appends a G7_KRONOS gate; on veto sets actionable=False
    and records why. On confirm, stamps `setup.kronos_confidence` for sizing.
    """
    setup = _ict_evaluate(htf_bars, ltf_bars, cfg=cfg, now_ts=now_ts)
    if not setup.actionable:
        return setup

    verdict = assess(ltf_bars, setup, kronos_cfg)
    setup.gates.append(verdict.gate())
    setup.kronos_confidence = verdict.confidence if verdict.available else None
    if not verdict.allow:
        setup.actionable = False
    return setup


# ──────────────────────────────────────────────────────────────────────────────
# Self-test (dependency-free — uses a deterministic stub forecaster)
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    # A clean BULL setup: entry 4500, SL 4496, TP 4512 (3R).
    setup = Setup(direction="BULL", actionable=True, entry=4500.0, sl=4496.0,
                  tp=4512.0, rr=3.0, entry_type="fvg")

    def stub_up(bars, cfg, n):
        # All paths drift up and tag TP without touching SL -> should CONFIRM.
        out = []
        for _ in range(n):
            out.append([Bar(time=0, open=4500, high=4500 + 2 * (k + 1),
                             low=4499, close=4500 + 1.5 * (k + 1)) for k in range(6)])
        return out

    def stub_down(bars, cfg, n):
        # All paths sink and tag SL first -> should VETO.
        out = []
        for _ in range(n):
            out.append([Bar(time=0, open=4500, high=4501,
                             low=4495 - (k + 1), close=4498 - (k + 1)) for k in range(6)])
        return out

    set_forecaster(stub_up)
    v = assess([], setup, KronosConfig(mc_paths=10))
    print("UP   ->", v.reason)
    assert v.available and v.allow and v.prob_tp_first == 1.0

    set_forecaster(stub_down)
    v = assess([], setup, KronosConfig(mc_paths=10))
    print("DOWN ->", v.reason)
    assert v.available and not v.allow and v.prob_tp_first == 0.0

    # Fail-open: no forecaster output, model unavailable.
    set_forecaster(lambda b, c, n: [])
    v = assess([], setup, KronosConfig(fail_open=True))
    print("FAILOPEN ->", v.reason)
    assert (not v.available) and v.allow

    v = assess([], setup, KronosConfig(fail_open=False))
    print("FAILCLOSED ->", v.reason)
    assert (not v.available) and (not v.allow)

    # Disabled -> pure pass-through.
    set_forecaster(None)
    v = assess([], setup, KronosConfig(enabled=False))
    assert v.allow and not v.available

    print("\n[OK] kronos_filter self-test passed (stub forecaster; no torch needed)")


if __name__ == "__main__":
    _self_test()
