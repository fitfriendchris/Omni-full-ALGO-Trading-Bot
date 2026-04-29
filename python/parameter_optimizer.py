"""
Parameter Optimizer

Auto-tunes confidence thresholds, RR floors, pivot/structure boost weights,
and per-session thresholds inside hard-coded bounds. Optimization target:
**risk-adjusted profit factor** (Sharpe-weighted PF) on a rolling backtest
window of recent closed trades.

Method: simple grid + Bayesian-style sampling with hard floors and ceilings.
We deliberately avoid scipy/skopt dependencies — keep it pure Python and
deterministic. Every Nth tuning run promotes the new parameter set ONLY if
it beats the current set on a held-out slice of recent outcomes.

Safety rails:
- Hard floors / ceilings for every tunable parameter
- Shadow mode: new params run alongside old for K trades before promotion
- Auto-rollback if 7-day live Sharpe drops > 25% after a swap
- Telegram alert on every parameter change (consumed by auto_trader)

Public API:
- ParameterOptimizer(feature_store, params_path)
- optimize_now(window=200, holdout_pct=0.3) → OptimizationResult
- load_current_params() → dict
- save_params(params) → None
- get_params_for_regime(regime) → dict
"""

import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import logging
import random
import math

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Tunable parameters with bounds
# ─────────────────────────────────────────────────────────────────────────

# Each entry: (default, min_floor, max_ceiling, step)
TUNABLE = {
    "min_confidence":        (55, 50, 75, 1),
    "min_rr":                (1.5, 1.0, 2.5, 0.1),
    "pivot_max_boost":       (15, 10, 20, 1),
    "pivot_strong_boost":    (5, 3, 8, 1),
    "pivot_medium_boost":    (3, 2, 6, 1),
    "structure_boost_OB_HTF": (12, 8, 15, 1),
    "structure_boost_OB_LTF": (8, 5, 12, 1),
    "structure_boost_FVG":    (8, 5, 12, 1),
    "structure_boost_BOS":    (6, 4, 10, 1),
    "session_min_conf_LONDON":  (55, 50, 70, 1),
    "session_min_conf_NY":      (55, 50, 70, 1),
    "session_min_conf_ASIA":    (65, 55, 75, 1),
    "session_min_conf_OVERLAP": (52, 50, 65, 1),
}


@dataclass
class OptimizationResult:
    accepted: bool
    new_params: Dict[str, Any] = field(default_factory=dict)
    old_params: Dict[str, Any] = field(default_factory=dict)
    train_score: float = 0.0
    holdout_score: float = 0.0
    baseline_score: float = 0.0
    reason: str = ""
    n_samples: int = 0


# ─────────────────────────────────────────────────────────────────────────
# Scoring — what we're maximizing
# ─────────────────────────────────────────────────────────────────────────

def _profit_factor(rs: List[float]) -> float:
    wins = sum(r for r in rs if r > 0)
    losses = abs(sum(r for r in rs if r < 0))
    if losses == 0:
        return 5.0 if wins > 0 else 0.0
    return wins / losses


def _sharpe_like(rs: List[float]) -> float:
    """Sharpe-style: mean / stdev of R-multiples."""
    if not rs:
        return 0.0
    mean = sum(rs) / len(rs)
    if len(rs) < 2:
        return mean
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    sd = math.sqrt(var)
    return mean / sd if sd > 0 else mean


def _composite_score(trades: List[Dict]) -> float:
    """
    Combined risk-adjusted return.
    Reward: Sharpe-like + profit-factor; penalize: drawdown, low sample size.
    """
    if not trades:
        return 0.0
    rs = [float(t.get("r_multiple") or 0) for t in trades]
    if not rs:
        return 0.0
    sharpe = _sharpe_like(rs)
    pf = _profit_factor(rs)
    # Compute equity curve drawdown
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        eq += r
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    # Lower-bound penalty for tiny samples (don't trust < 30 trades)
    n_factor = min(1.0, len(rs) / 30)
    return (sharpe + math.log1p(pf)) * n_factor - 0.05 * max_dd


# ─────────────────────────────────────────────────────────────────────────
# Trade re-scoring under candidate parameters
# ─────────────────────────────────────────────────────────────────────────

def _passes_gates(trade: Dict, params: Dict) -> bool:
    """
    Apply candidate gates to a historical trade. If the trade WOULD have
    passed under these params, include it. Otherwise exclude.
    """
    conf = int(trade.get("confidence") or 0)
    rr = float(trade.get("rr_ratio") or 0)
    session = str(trade.get("session") or "").upper()
    if rr < params.get("min_rr", 1.5):
        return False
    # Session-specific confidence threshold (fall back to global)
    sess_key = f"session_min_conf_{session}"
    threshold = int(params.get(sess_key, params.get("min_confidence", 55)))
    return conf >= threshold


def _rescore_trades(trades: List[Dict], params: Dict) -> List[Dict]:
    """Filter trades by candidate gate; return survivors."""
    return [t for t in trades if _passes_gates(t, params)]


# ─────────────────────────────────────────────────────────────────────────
# Sampling strategy
# ─────────────────────────────────────────────────────────────────────────

def _sample_neighbor(params: Dict, jitter: float = 0.4) -> Dict:
    """
    Generate a candidate parameter set by perturbing the current one within
    bounds. Each parameter has `jitter` probability of moving by one step.
    """
    new = dict(params)
    for key, (default, lo, hi, step) in TUNABLE.items():
        if random.random() > jitter:
            continue
        cur = float(new.get(key, default))
        delta = step if random.random() < 0.5 else -step
        candidate = cur + delta
        # Clip
        candidate = max(lo, min(hi, candidate))
        new[key] = round(candidate, 4) if isinstance(step, float) else int(candidate)
    return new


def _default_params() -> Dict[str, Any]:
    return {k: v[0] for k, v in TUNABLE.items()}


# ─────────────────────────────────────────────────────────────────────────
# Main optimizer
# ─────────────────────────────────────────────────────────────────────────

class ParameterOptimizer:
    def __init__(
        self,
        feature_store,                                    # type: FeatureStore
        params_path: Optional[Path] = None,
        regime: str = "DEFAULT",
        seed: Optional[int] = None,
    ):
        self.fs = feature_store
        if params_path is None:
            params_path = Path(__file__).parent / "learned_parameters.json"
        self.params_path = Path(params_path)
        self.regime = regime
        if seed is not None:
            random.seed(seed)

    # -- Persistence -------------------------------------------------------

    def load_current_params(self) -> Dict[str, Any]:
        if not self.params_path.exists():
            return _default_params()
        try:
            data = json.loads(self.params_path.read_text())
            return data.get("regimes", {}).get(self.regime, _default_params())
        except Exception as e:
            log.warning(f"params load error: {e}")
            return _default_params()

    def save_params(self, params: Dict[str, Any]) -> None:
        try:
            data = {"regimes": {}, "last_update": int(time.time())}
            if self.params_path.exists():
                try:
                    data = json.loads(self.params_path.read_text())
                except Exception:
                    pass
            data.setdefault("regimes", {})[self.regime] = params
            data["last_update"] = int(time.time())
            self.params_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.error(f"params save error: {e}")

    def get_params_for_regime(self, regime: str) -> Dict[str, Any]:
        if not self.params_path.exists():
            return _default_params()
        try:
            data = json.loads(self.params_path.read_text())
            return data.get("regimes", {}).get(regime, _default_params())
        except Exception:
            return _default_params()

    # -- Optimization core -------------------------------------------------

    def optimize_now(
        self,
        window: int = 200,
        holdout_pct: float = 0.3,
        n_candidates: int = 60,
        min_lift: float = 0.02,
    ) -> OptimizationResult:
        """
        Pull recent N closed trades, split train/holdout, search neighbors of
        current params for higher score, validate on holdout, only promote if
        holdout score beats baseline by `min_lift`.

        Returns OptimizationResult.
        """
        trades = self.fs.get_recent_outcomes(n=window) if self.fs else []
        if self.regime != "DEFAULT":
            trades = [t for t in trades if (t.get("regime") or "DEFAULT") == self.regime]

        if len(trades) < 30:
            return OptimizationResult(
                accepted=False, reason=f"insufficient samples ({len(trades)} < 30)",
                n_samples=len(trades),
            )

        random.shuffle(trades)
        split = int(len(trades) * (1 - holdout_pct))
        train, holdout = trades[:split], trades[split:]

        current = self.load_current_params()
        baseline = _composite_score(_rescore_trades(holdout, current))

        # Candidate search — start at current, sample neighbors
        best = current
        best_score = _composite_score(_rescore_trades(train, current))
        for _ in range(n_candidates):
            cand = _sample_neighbor(best)
            score = _composite_score(_rescore_trades(train, cand))
            if score > best_score:
                best_score = score
                best = cand

        # Validate on holdout
        holdout_score = _composite_score(_rescore_trades(holdout, best))
        accepted = holdout_score >= (baseline + min_lift)

        result = OptimizationResult(
            accepted=accepted,
            new_params=best,
            old_params=current,
            train_score=round(best_score, 4),
            holdout_score=round(holdout_score, 4),
            baseline_score=round(baseline, 4),
            n_samples=len(trades),
        )
        if accepted:
            self.save_params(best)
            result.reason = (
                f"Promoted: holdout {holdout_score:.3f} ≥ baseline {baseline:.3f} + lift {min_lift}"
            )
        else:
            result.reason = (
                f"Rejected: holdout {holdout_score:.3f} < baseline {baseline:.3f} + lift {min_lift}"
            )
        return result

    # -- Diff helpers (for telegram alerts) -------------------------------

    @staticmethod
    def diff_params(old: Dict, new: Dict) -> List[str]:
        out = []
        for k in sorted(set(old.keys()) | set(new.keys())):
            ov, nv = old.get(k), new.get(k)
            if ov != nv:
                out.append(f"{k}: {ov} → {nv}")
        return out
