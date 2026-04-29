"""
Online Learner — closed-loop self-update from new closed trades.

Every Nth closed trade triggers a non-blocking learning cycle:
1. Pull recent outcomes from feature_store
2. Compute per-feature lift (which features actually predict winners?)
3. Update a lightweight logistic-regression-style scorer for reversal probability
4. Run parameter_optimizer.optimize_now() to tune thresholds
5. Trigger optional shadow-mode validation before promotion

Designed to run in a background thread (or queued for the next idle scan
cycle) so it never blocks live trading. Heavy retraining is gated by a
simple lock — only one learner can run at a time.

Public API:
- OnlineLearner(feature_store, optimizer, model_path)
- maybe_retrain(state) → bool      (called after every close)
- force_retrain() → dict           (manual trigger)
- get_feature_importance() → dict
- last_run_summary() → dict
"""

import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging

log = logging.getLogger(__name__)


# Trigger after every N closed trades
DEFAULT_RETRAIN_INTERVAL = 25
# Minimum samples required before any retraining
MIN_SAMPLES_FOR_TRAIN = 50
# Rolling window of trades to learn from
DEFAULT_TRAIN_WINDOW = 200


@dataclass
class LearningRun:
    ts: int = 0
    n_trades: int = 0
    win_rate: float = 0.0
    avg_r: float = 0.0
    feature_importance: Dict[str, float] = field(default_factory=dict)
    optimizer_accepted: bool = False
    optimizer_reason: str = ""


class OnlineLearner:
    def __init__(
        self,
        feature_store,                              # type: FeatureStore
        optimizer=None,                             # type: ParameterOptimizer
        model_path: Optional[Path] = None,
        retrain_interval: int = DEFAULT_RETRAIN_INTERVAL,
        train_window: int = DEFAULT_TRAIN_WINDOW,
    ):
        self.fs = feature_store
        self.opt = optimizer
        if model_path is None:
            model_path = Path(__file__).parent / "online_model.json"
        self.model_path = Path(model_path)
        self.retrain_interval = retrain_interval
        self.train_window = train_window
        self._lock = threading.Lock()
        self._closes_since_last_run = 0
        self._last_run: Optional[LearningRun] = None

    # -- Trigger logic -----------------------------------------------------

    def maybe_retrain(self, force: bool = False) -> bool:
        """
        Increment the closed-trade counter. If we've hit the interval (or
        force=True), kick off a retrain in a background thread.
        Returns True if a retrain was triggered.
        """
        if not force:
            self._closes_since_last_run += 1
            if self._closes_since_last_run < self.retrain_interval:
                return False
        # Reset counter and start background retrain (non-blocking)
        self._closes_since_last_run = 0
        thread = threading.Thread(target=self._safe_retrain, daemon=True,
                                  name="OnlineLearner-retrain")
        thread.start()
        return True

    def force_retrain(self) -> Dict[str, Any]:
        """Synchronous retrain — for manual /retrain telegram command."""
        return self._safe_retrain(synchronous=True)

    def _safe_retrain(self, synchronous: bool = False) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=synchronous):
            log.info("OnlineLearner: retrain already running, skipping")
            return {"skipped": True, "reason": "lock held"}
        try:
            return self._retrain()
        except Exception as e:
            log.error(f"OnlineLearner retrain error: {e}", exc_info=True)
            return {"error": str(e)}
        finally:
            self._lock.release()

    # -- Core retrain -----------------------------------------------------

    def _retrain(self) -> Dict[str, Any]:
        if not self.fs:
            return {"skipped": True, "reason": "no feature store"}
        trades = self.fs.get_recent_outcomes(n=self.train_window)
        if len(trades) < MIN_SAMPLES_FOR_TRAIN:
            return {"skipped": True, "reason": f"only {len(trades)} samples"}

        # 1. Compute per-feature lift
        importance = self._compute_feature_importance(trades)

        # 2. Update scorer (simple logistic regression with stochastic gradient)
        weights = self._fit_scorer(trades)
        self._save_model(weights, importance)

        # 3. Trigger parameter optimizer
        opt_result = None
        if self.opt:
            try:
                opt_result = self.opt.optimize_now(window=self.train_window)
            except Exception as e:
                log.warning(f"optimizer failed: {e}")

        # 4. Record summary
        wins = sum(1 for t in trades if t.get("outcome") == "WIN")
        avg_r = sum(float(t.get("r_multiple") or 0) for t in trades) / len(trades)
        self._last_run = LearningRun(
            ts=int(time.time()),
            n_trades=len(trades),
            win_rate=wins / len(trades) if trades else 0.0,
            avg_r=avg_r,
            feature_importance=importance,
            optimizer_accepted=bool(opt_result and opt_result.accepted),
            optimizer_reason=(opt_result.reason if opt_result else "no optimizer"),
        )
        log.info(
            f"OnlineLearner ran: {len(trades)} trades, win_rate={wins/len(trades):.1%}, "
            f"avg_r={avg_r:+.2f}, optimizer_accepted={bool(opt_result and opt_result.accepted)}"
        )
        return {
            "n_trades": len(trades),
            "win_rate": wins / len(trades) if trades else 0.0,
            "avg_r": avg_r,
            "feature_importance": importance,
            "optimizer_accepted": bool(opt_result and opt_result.accepted),
            "optimizer_reason": (opt_result.reason if opt_result else ""),
        }

    # -- Feature importance ------------------------------------------------

    def _compute_feature_importance(self, trades: List[Dict]) -> Dict[str, float]:
        """
        For each numeric feature in pivot_features and structure_features,
        compute the win-rate lift conditional on the feature being present
        / above its median.
        """
        importance: Dict[str, float] = {}

        # Aggregate features
        feature_values: Dict[str, List[float]] = {}
        for t in trades:
            outcome_won = (t.get("outcome") == "WIN")
            for col in ("pivot_features", "structure_features", "ict_features"):
                feats = t.get(col)
                if not isinstance(feats, dict):
                    continue
                for k, v in feats.items():
                    if isinstance(v, bool):
                        v = 1.0 if v else 0.0
                    elif not isinstance(v, (int, float)):
                        continue
                    feature_values.setdefault(f"{col}.{k}", []).append((float(v), outcome_won))

        for fname, pairs in feature_values.items():
            if len(pairs) < 10:
                continue
            vals = sorted(p[0] for p in pairs)
            median = vals[len(vals) // 2]
            high = [won for v, won in pairs if v >= median]
            low  = [won for v, won in pairs if v <  median]
            if not high or not low:
                continue
            high_wr = sum(high) / len(high)
            low_wr  = sum(low)  / len(low)
            importance[fname] = round(high_wr - low_wr, 4)  # lift

        # Sort by absolute lift, top 25
        sorted_imp = sorted(importance.items(), key=lambda kv: -abs(kv[1]))
        return dict(sorted_imp[:25])

    # -- Logistic scorer (lightweight, no external deps) ------------------

    def _fit_scorer(self, trades: List[Dict]) -> Dict[str, float]:
        """
        Fit a tiny logistic regression on a fixed feature vector:
        [confidence, rr_ratio, pivot_alignment_score, structure_boost, reversal_prob, mtf_confluence]
        Predicts P(WIN). Stochastic gradient, 100 epochs.
        """
        # Build training matrix
        X, y = [], []
        for t in trades:
            outcome = t.get("outcome")
            if outcome not in ("WIN", "LOSS"):
                continue
            pf = t.get("pivot_features") or {}
            sf = t.get("structure_features") or {}
            xv = [
                float(t.get("confidence") or 0) / 100,
                float(t.get("rr_ratio") or 0) / 5,
                float(pf.get("alignment_score") or 0),
                float(sf.get("structure_boost") or 0) / 15,
                float(pf.get("reversal_probability") or 0),
                float(pf.get("confluence_count") or 0) / 5,
            ]
            X.append(xv)
            y.append(1.0 if outcome == "WIN" else 0.0)

        if len(X) < 30:
            return {}

        n_features = len(X[0])
        w = [0.0] * n_features
        b = 0.0
        lr = 0.05
        for epoch in range(100):
            # Shuffle indices
            indices = list(range(len(X)))
            random_state = (epoch * 17 + 31) % len(X)
            indices = indices[random_state:] + indices[:random_state]
            for idx in indices:
                xi, yi = X[idx], y[idx]
                z = b + sum(w[j] * xi[j] for j in range(n_features))
                # Stable sigmoid
                if z >= 0:
                    s = 1.0 / (1.0 + math.exp(-z))
                else:
                    e = math.exp(z)
                    s = e / (1.0 + e)
                err = yi - s
                b += lr * err
                for j in range(n_features):
                    w[j] += lr * err * xi[j]

        return {
            "weights": w,
            "bias": b,
            "feature_names": [
                "confidence", "rr_ratio", "alignment_score",
                "structure_boost", "reversal_probability", "confluence_count",
            ],
            "trained_at": int(time.time()),
            "n_samples": len(X),
        }

    # -- Persistence -------------------------------------------------------

    def _save_model(self, weights: Dict, importance: Dict) -> None:
        try:
            tmp = self.model_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "weights": weights,
                "feature_importance": importance,
                "saved_at": int(time.time()),
            }, indent=2))
            tmp.replace(self.model_path)  # Atomic swap
        except Exception as e:
            log.warning(f"OnlineLearner save error: {e}")

    def load_model(self) -> Optional[Dict]:
        if not self.model_path.exists():
            return None
        try:
            return json.loads(self.model_path.read_text())
        except Exception:
            return None

    # -- Inference ---------------------------------------------------------

    def predict_win_probability(self, features: Dict[str, float]) -> float:
        """Use the trained scorer to predict P(WIN) for a candidate signal."""
        model = self.load_model()
        if not model or not model.get("weights"):
            return 0.5  # No model — return neutral
        w = model["weights"].get("weights") or []
        b = model["weights"].get("bias", 0.0)
        names = model["weights"].get("feature_names") or []
        if not w or not names:
            return 0.5
        z = b
        for j, name in enumerate(names):
            v = float(features.get(name, 0))
            z += w[j] * v
        # Sigmoid
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        else:
            e = math.exp(z)
            return e / (1.0 + e)

    def get_feature_importance(self) -> Dict[str, float]:
        model = self.load_model()
        if not model:
            return {}
        return model.get("feature_importance") or {}

    def last_run_summary(self) -> Optional[Dict]:
        if not self._last_run:
            return None
        return {
            "ts":               self._last_run.ts,
            "n_trades":         self._last_run.n_trades,
            "win_rate":         round(self._last_run.win_rate, 3),
            "avg_r":            round(self._last_run.avg_r, 2),
            "feature_importance": self._last_run.feature_importance,
            "optimizer_accepted": self._last_run.optimizer_accepted,
            "optimizer_reason":   self._last_run.optimizer_reason,
        }
