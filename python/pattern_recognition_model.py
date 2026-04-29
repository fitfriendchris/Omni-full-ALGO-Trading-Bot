"""
Pattern Recognition Model

Lightweight, dependency-free predictor that learns "given these structural +
candlestick + pivot features, what's the probability of reversal?"

Architecture: ensemble of two simple models
1. Logistic regression (from online_learner) — fast, interpretable
2. Decision-stump committee — captures non-linear feature interactions

The committee is a list of (feature, threshold, vote_yes_wr, vote_no_wr) tuples.
At inference time, each stump votes; final prediction is the weighted average
weighted by abs(lift = vote_yes_wr - vote_no_wr).

Designed to be retrained by online_learner on every cycle. Persists to
`pattern_recognition_model.json` (atomic swap).

Public API:
- PatternRecognitionModel(model_path)
- fit(trades) → metrics
- predict_proba(features_dict) → float in [0, 1]
- explain(features_dict) → list[(feature, contribution)]
"""

import json
import math
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import logging

log = logging.getLogger(__name__)


def _flatten_features(trade: Dict) -> Dict[str, float]:
    """Flatten nested feature dicts into a single flat dict of numerics."""
    out: Dict[str, float] = {
        "confidence": float(trade.get("confidence") or 0),
        "rr_ratio":   float(trade.get("rr_ratio") or 0),
    }
    for prefix in ("ict_features", "pivot_features", "structure_features", "candle_features"):
        d = trade.get(prefix)
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(v, bool):
                out[f"{prefix}.{k}"] = 1.0 if v else 0.0
            elif isinstance(v, (int, float)):
                out[f"{prefix}.{k}"] = float(v)
    return out


def _outcome_to_label(trade: Dict) -> Optional[int]:
    o = trade.get("outcome")
    if o == "WIN":
        return 1
    if o == "LOSS":
        return 0
    return None  # Skip


# ─────────────────────────────────────────────────────────────────────────
# Decision stumps
# ─────────────────────────────────────────────────────────────────────────

def _fit_stump(values: List[float], labels: List[int]) -> Optional[Dict]:
    """
    Find the best threshold split for one feature.
    Returns a stump dict with {threshold, wr_above, wr_below, lift, n_above, n_below}.
    """
    if len(values) < 10:
        return None
    sorted_pairs = sorted(zip(values, labels))
    sorted_vals = [v for v, _ in sorted_pairs]
    sorted_labs = [l for _, l in sorted_pairs]

    best = None
    best_lift = 0.0
    n = len(sorted_vals)
    cum_wins = 0
    total_wins = sum(sorted_labs)
    for i in range(2, n - 2):                          # require ≥ 2 samples per side
        cum_wins += sorted_labs[i - 1]
        n_below = i
        n_above = n - i
        wr_below = cum_wins / n_below if n_below else 0
        wr_above = (total_wins - cum_wins) / n_above if n_above else 0
        lift = abs(wr_above - wr_below)
        if lift > best_lift:
            best_lift = lift
            best = {
                "threshold": sorted_vals[i],
                "wr_above":  round(wr_above, 4),
                "wr_below":  round(wr_below, 4),
                "lift":      round(lift, 4),
                "n_above":   n_above,
                "n_below":   n_below,
            }
    return best


# ─────────────────────────────────────────────────────────────────────────
# Logistic regression (mini)
# ─────────────────────────────────────────────────────────────────────────

def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _fit_logistic(X: List[List[float]], y: List[int],
                  epochs: int = 150, lr: float = 0.05,
                  l2: float = 0.001) -> Tuple[List[float], float]:
    if not X:
        return [], 0.0
    n_features = len(X[0])
    w = [0.0] * n_features
    b = 0.0
    for epoch in range(epochs):
        # Mini-batch SGD
        for i in range(len(X)):
            xi, yi = X[i], y[i]
            z = b + sum(w[j] * xi[j] for j in range(n_features))
            p = _sigmoid(z)
            err = yi - p
            b += lr * err
            for j in range(n_features):
                w[j] += lr * (err * xi[j] - l2 * w[j])
    return w, b


# ─────────────────────────────────────────────────────────────────────────
# Pattern Recognition Model
# ─────────────────────────────────────────────────────────────────────────

class PatternRecognitionModel:
    def __init__(self, model_path: Optional[Path] = None):
        if model_path is None:
            model_path = Path(__file__).parent / "pattern_recognition_model.json"
        self.model_path = Path(model_path)
        self._cache: Optional[Dict] = None

    # -- Training ---------------------------------------------------------

    def fit(self, trades: List[Dict], top_k_features: int = 10) -> Dict[str, Any]:
        """
        Train both logistic + stump ensemble. Persists model to disk.
        Returns metrics dict.
        """
        # Build training set
        rows = []
        for t in trades:
            label = _outcome_to_label(t)
            if label is None:
                continue
            rows.append((label, _flatten_features(t)))

        if len(rows) < 30:
            return {"trained": False, "reason": f"too few labeled samples ({len(rows)})"}

        # Pick features by stump lift
        all_feature_names = set()
        for _, fmap in rows:
            all_feature_names.update(fmap.keys())

        stumps: Dict[str, Dict] = {}
        for fname in all_feature_names:
            values, labels = [], []
            for label, fmap in rows:
                if fname in fmap:
                    values.append(fmap[fname])
                    labels.append(label)
            stump = _fit_stump(values, labels)
            if stump:
                stumps[fname] = stump

        # Top-K features by stump lift
        ranked = sorted(stumps.items(), key=lambda kv: -kv[1]["lift"])
        top_features = [name for name, _ in ranked[:top_k_features]]
        if not top_features:
            return {"trained": False, "reason": "no informative features"}

        # Build logistic regression matrix using top features only
        X, y = [], []
        for label, fmap in rows:
            X.append([fmap.get(f, 0.0) for f in top_features])
            y.append(label)
        # Normalize: scale each column to ~unit variance via z-score
        means, stds = [], []
        for j in range(len(top_features)):
            col = [row[j] for row in X]
            m = sum(col) / len(col)
            v = sum((c - m) ** 2 for c in col) / max(1, len(col) - 1)
            s = math.sqrt(v) or 1.0
            means.append(m)
            stds.append(s)
        Xn = [[(row[j] - means[j]) / stds[j] for j in range(len(top_features))] for row in X]
        weights, bias = _fit_logistic(Xn, y)

        # Self-check: training accuracy
        correct = 0
        for i, row in enumerate(Xn):
            z = bias + sum(weights[j] * row[j] for j in range(len(top_features)))
            pred = 1 if _sigmoid(z) >= 0.5 else 0
            if pred == y[i]:
                correct += 1
        train_acc = correct / len(y)

        model = {
            "version":          "pattern_recognition_v1",
            "trained_at":       int(time.time()),
            "n_samples":        len(rows),
            "top_features":     top_features,
            "stumps":           {f: stumps[f] for f in top_features},
            "logistic": {
                "weights": weights,
                "bias":    bias,
                "means":   means,
                "stds":    stds,
                "feature_names": top_features,
            },
            "train_accuracy":   round(train_acc, 4),
        }
        self._save(model)
        self._cache = model
        return {
            "trained":         True,
            "n_samples":       len(rows),
            "train_accuracy":  round(train_acc, 4),
            "top_features":    top_features[:5],  # log only top 5
        }

    # -- Inference --------------------------------------------------------

    def predict_proba(self, features: Dict[str, float]) -> float:
        """
        Returns P(WIN) ∈ [0, 1].
        Ensemble: 60% logistic, 40% stump committee.
        """
        m = self._load()
        if not m:
            return 0.5

        flat = features
        # Logistic
        log_pred = 0.5
        try:
            lg = m.get("logistic") or {}
            names = lg.get("feature_names") or []
            w = lg.get("weights") or []
            b = lg.get("bias", 0.0)
            means = lg.get("means") or [0.0] * len(names)
            stds = lg.get("stds") or [1.0] * len(names)
            if names and w:
                z = b
                for j, name in enumerate(names):
                    raw = float(flat.get(name, 0.0))
                    norm = (raw - means[j]) / (stds[j] or 1.0)
                    z += w[j] * norm
                log_pred = _sigmoid(z)
        except Exception as e:
            log.debug(f"logistic predict error: {e}")

        # Stump committee
        stump_pred, total_weight = 0.0, 0.0
        for fname, stump in (m.get("stumps") or {}).items():
            v = flat.get(fname)
            if v is None:
                continue
            wr = stump["wr_above"] if v >= stump["threshold"] else stump["wr_below"]
            weight = stump["lift"]
            stump_pred += wr * weight
            total_weight += weight
        committee_pred = (stump_pred / total_weight) if total_weight > 0 else 0.5

        # Ensemble
        return 0.6 * log_pred + 0.4 * committee_pred

    def explain(self, features: Dict[str, float], top_n: int = 5) -> List[Tuple[str, float]]:
        """
        Returns top features driving the prediction (positive = pushes toward WIN).
        """
        m = self._load()
        if not m:
            return []
        out: List[Tuple[str, float]] = []
        for fname, stump in (m.get("stumps") or {}).items():
            v = features.get(fname)
            if v is None:
                continue
            contribution = (stump["wr_above"] - 0.5) if v >= stump["threshold"] else (stump["wr_below"] - 0.5)
            contribution *= stump["lift"]
            out.append((fname, round(contribution, 4)))
        out.sort(key=lambda kv: -abs(kv[1]))
        return out[:top_n]

    # -- Persistence ------------------------------------------------------

    def _load(self) -> Optional[Dict]:
        if self._cache is not None:
            return self._cache
        if not self.model_path.exists():
            return None
        try:
            self._cache = json.loads(self.model_path.read_text())
            return self._cache
        except Exception:
            return None

    def _save(self, model: Dict) -> None:
        try:
            tmp = self.model_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(model, indent=2))
            tmp.replace(self.model_path)  # Atomic swap
        except Exception as e:
            log.error(f"pattern model save error: {e}")

    def info(self) -> Dict[str, Any]:
        m = self._load()
        if not m:
            return {"loaded": False}
        return {
            "loaded":         True,
            "version":        m.get("version"),
            "trained_at":     m.get("trained_at"),
            "n_samples":      m.get("n_samples"),
            "train_accuracy": m.get("train_accuracy"),
            "top_features":   m.get("top_features", [])[:5],
        }
