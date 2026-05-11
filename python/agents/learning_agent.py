"""
learning_agent.py — LEARNING_LAB business: continuous self-improvement loop.

GOAL: Close the feedback loop between trade outcomes and signal parameters.
      Every N closed trades triggers retraining; improvements are published
      as per-symbol confidence adjustments back to signal_agent.

Tasks handled:
  SIGNAL_EMITTED    — log signal context for future outcome matching
  TRADE_CLOSED      — record outcome, trigger maybe_retrain
  REGIME_SHIFT      — re-examine recent trades in new regime context
  FORCE_RETRAIN     — immediate retrain (manual trigger)

Self-tasks/broadcasts emitted:
  ADJUST_SYMBOL_CONF  — sent to signal_agent with learned confidence delta
  PARAM_UPDATED       — broadcast when optimizer promotes new params
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_base import BaseAgent, Task

HERE         = Path(__file__).resolve().parent.parent
PROJECT_ROOT = HERE.parent
LEARNING_LOG = PROJECT_ROOT / "logs" / "learning.log"
MEMORY_PATH  = HERE / "trade_memory.json"


class LearningAgent(BaseAgent):
    NAME   = "learning_agent"
    GOAL   = "Learn from every trade outcome and improve signal quality continuously"
    DOMAIN = "LEARNING_LAB"
    HANDLES = ["SIGNAL_EMITTED", "TRADE_CLOSED", "REGIME_SHIFT", "FORCE_RETRAIN"]
    CYCLE_INTERVAL_S = 600.0  # review every 10 min

    def __init__(self):
        super().__init__()
        self._pending_signals: dict = {}  # signal_id → signal context
        self._learner = None
        self._memory  = None
        self._optimizer = None
        self._closed_since_retrain = 0
        self._retrain_interval = 25
        self._init_components()

    def _init_components(self):
        try:
            from feature_store import FeatureStore
            fs = FeatureStore()
            from online_learner import OnlineLearner
            self._learner = OnlineLearner(fs)
            from parameter_optimizer import ParameterOptimizer
            self._optimizer = ParameterOptimizer(fs, HERE / "learned_params.json")
        except Exception as e:
            self.log.warning("learning components unavailable: %s", e)
        try:
            from trade_memory import TradeMemory
            self._memory = TradeMemory()
        except Exception as e:
            self.log.warning("trade_memory unavailable: %s", e)

    async def _run_cycle(self) -> dict:
        """Periodic: generate learned rules and push confidence adjustments."""
        result = {}

        if self._memory:
            try:
                rules = self._memory.generate_learned_rules()
                if rules:
                    self._log_learning(f"Learned rules ({len(rules)}): " + "; ".join(rules[:3]))
                    result["learned_rules"] = len(rules)

                # Per-symbol win rate → push confidence adjustments
                adjustments = self._compute_adjustments()
                for symbol, delta in adjustments.items():
                    if abs(delta) >= 0.02:  # only meaningful deltas
                        self.send("signal_agent", "ADJUST_SYMBOL_CONF", {
                            "symbol": symbol, "delta": delta,
                            "source": "learning_cycle"
                        }, priority=4)
                result["adjustments"] = len(adjustments)
            except Exception as e:
                self.log.warning("memory review failed: %s", e)

        if self._learner and self._closed_since_retrain >= self._retrain_interval:
            try:
                summary = self._learner.force_retrain()
                self._closed_since_retrain = 0
                self._log_learning(f"Retrain: win_rate={summary.get('win_rate', 0):.2f} n={summary.get('n_trades', 0)}")
                result["retrained"] = True
                result["train_summary"] = summary
            except Exception as e:
                self.log.warning("retrain failed: %s", e)

        return result

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "SIGNAL_EMITTED":
            self._store_pending_signals(task.payload.get("signals", []))
            return {"stored": len(task.payload.get("signals", []))}

        if task.type == "TRADE_CLOSED":
            return self._record_close(task.payload)

        if task.type == "REGIME_SHIFT":
            changes = task.payload.get("changes", [])
            self._log_learning(f"Regime shift: {changes}")
            # Trigger immediate retrain with regime context
            if self._learner and self._memory:
                try:
                    summary = self._learner.force_retrain()
                    return {"retrained": True, "regime_context": changes, "summary": summary}
                except Exception as e:
                    return {"retrained": False, "error": str(e)}
            return {"status": "no_learner"}

        if task.type == "FORCE_RETRAIN":
            if self._learner:
                try:
                    summary = self._learner.force_retrain()
                    self._closed_since_retrain = 0
                    return {"retrained": True, "summary": summary}
                except Exception as e:
                    return {"retrained": False, "error": str(e)}
            return {"status": "no_learner"}

        return {"status": "unknown"}

    # ── Internals ─────────────────────────────────────────────────────────────

    def _store_pending_signals(self, signals: list) -> None:
        for s in signals:
            sid = f"{s.get('symbol')}_{s.get('direction')}_{int(time.time())}"
            self._pending_signals[sid] = {**s, "stored_at": datetime.now(timezone.utc).isoformat()}
        # Prune old pending signals (>2h)
        cutoff = time.time() - 7200
        self._pending_signals = {
            k: v for k, v in self._pending_signals.items()
            if time.time() - cutoff < 7200  # keep last 2h
        }

    def _record_close(self, payload: dict) -> dict:
        trade = payload.get("trade", {})
        if not trade:
            return {"status": "no_trade_data"}

        self._closed_since_retrain += 1

        # Notify learner
        if self._learner:
            try:
                self._learner.maybe_retrain()
            except Exception:
                pass

        # Log to memory
        if self._memory:
            try:
                tid = trade.get("id", "")
                r_mult = float(trade.get("r_multiple", 0))
                profit = float(trade.get("profit_usd", 0))
                reason = trade.get("close_reason", "unknown")
                self._memory.record_close(tid, profit, r_mult, reason)
            except Exception as e:
                self.log.debug("memory record_close failed: %s", e)

        # Broadcast so journal agent can also log
        self.broadcast("TRADE_OUTCOME_RECORDED", {
            "symbol":   trade.get("symbol"),
            "r_mult":   trade.get("r_multiple"),
            "win":      float(trade.get("r_multiple", 0)) > 0,
        }, priority=4)

        # If optimizer available, maybe update params
        if self._optimizer and self._closed_since_retrain % 50 == 0:
            try:
                opt_result = self._optimizer.optimize_now()
                if opt_result.accepted:
                    self.broadcast("PARAM_UPDATED", {
                        "new_params": opt_result.new_params,
                        "train_score": opt_result.train_score,
                    }, priority=3)
            except Exception:
                pass

        return {"closed_since_retrain": self._closed_since_retrain}

    def _compute_adjustments(self) -> dict[str, float]:
        """Return per-symbol confidence delta based on recent win rate."""
        if not self._memory:
            return {}
        try:
            data = json.loads(MEMORY_PATH.read_text()) if MEMORY_PATH.exists() else {}
            trades = data.get("trades", [])
            if len(trades) < 15:
                return {}
            recent = [t for t in trades if t.get("status") in ("WIN", "LOSS")][-50:]
            by_symbol: dict[str, list] = {}
            for t in recent:
                s = t.get("symbol", "")
                if s:
                    by_symbol.setdefault(s, []).append(1 if t["status"] == "WIN" else 0)
            adjustments = {}
            for sym, outcomes in by_symbol.items():
                if len(outcomes) < 8:
                    continue
                wr = sum(outcomes) / len(outcomes)
                if wr > 0.65:
                    adjustments[sym] = +0.05
                elif wr > 0.55:
                    adjustments[sym] = +0.02
                elif wr < 0.35:
                    adjustments[sym] = -0.10
                elif wr < 0.45:
                    adjustments[sym] = -0.05
            return adjustments
        except Exception:
            return {}

    def _log_learning(self, msg: str) -> None:
        try:
            LEARNING_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(LEARNING_LOG, "a") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
        except Exception:
            pass
