"""
regime_agent.py — SIGNAL_INTELLIGENCE business: market regime detection.

GOAL: Maintain a live, accurate read of the market regime for each symbol
      and broadcast changes so other agents can adapt strategy parameters.

Tasks handled:
  FORCE_REGIME_CHECK  — immediate recheck (from signal_agent or monitor)

Self-tasks/broadcasts emitted:
  REGIME_CHANGED   — when regime transitions (broadcast to all)
  PARAM_ADJUST     — regime-specific parameter recommendation (to optimizer)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_base import BaseAgent, Task

HERE         = Path(__file__).resolve().parent.parent
PROJECT_ROOT = HERE.parent
AI_STATE_PATH = HERE / "ai_state.json"
REGIME_STATE  = PROJECT_ROOT / "shared" / "regime_state.json"


class RegimeAgent(BaseAgent):
    NAME   = "regime_agent"
    GOAL   = "Detect market regime changes and broadcast adaptive parameters to the swarm"
    DOMAIN = "SIGNAL_INTELLIGENCE"
    HANDLES = ["FORCE_REGIME_CHECK"]
    CYCLE_INTERVAL_S = 300.0  # every 5 min, matches ai_engine

    def __init__(self):
        super().__init__()
        self._last_regimes: dict = {}  # symbol → last known regime

    async def _run_cycle(self) -> dict:
        return await self._check_regimes()

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "FORCE_REGIME_CHECK":
            return await self._check_regimes()
        return {"status": "unknown"}

    async def _check_regimes(self) -> dict:
        try:
            from orchestrator import MT5BarFetcher, FixtureBarFetcher
            from regime_detector import detect_regime
        except ImportError as e:
            self.log.warning("regime imports unavailable: %s", e)
            return {"status": "imports_failed"}

        try:
            fetcher = MT5BarFetcher()
            fetcher.fetch("XAUUSD", "H1", 5)
        except Exception:
            fetcher = FixtureBarFetcher()

        rules = self._load_rules()
        watchlist = rules.get("watchlist", ["XAUUSD", "XAGUSD"])[:6]

        changes = []
        regimes = {}
        for symbol in watchlist:
            try:
                bars_raw = fetcher.fetch(symbol, "H1", 100)
                bars_dict = [{"high": b.high, "low": b.low, "close": b.close}
                             for b in bars_raw]
                rr = detect_regime(bars_dict)
                regimes[symbol] = {
                    "regime":     rr.regime,
                    "confidence": round(rr.confidence, 3),
                    "reason":     rr.reason,
                    "ts":         datetime.now(timezone.utc).isoformat(),
                }
                prev = self._last_regimes.get(symbol, {}).get("regime")
                if prev and prev != rr.regime:
                    changes.append({
                        "symbol":  symbol,
                        "from":    prev,
                        "to":      rr.regime,
                        "conf":    rr.confidence,
                    })
            except Exception as e:
                self.log.debug("regime check failed for %s: %s", symbol, e)

        self._last_regimes = regimes
        self._save_regime_state(regimes)

        if changes:
            for ch in changes:
                self.log.info("REGIME CHANGE %s: %s → %s (conf=%.2f)",
                              ch["symbol"], ch["from"], ch["to"], ch["conf"])
            self.broadcast("REGIME_CHANGED", {"changes": changes}, priority=2)
            # Ask learning agent to re-examine recent trades in context of new regime
            self.send("learning_agent", "REGIME_SHIFT", {
                "changes": changes
            }, priority=2)
            # Ask signal agent to run immediately with new regime context
            self.send("signal_agent", "FORCE_SIGNAL_CYCLE", {
                "trigger": "regime_change", "changes": changes
            }, priority=1)

        # Also update ai_engine state with our regime data
        self._sync_ai_state(regimes)

        return {"checked": len(regimes), "changes": len(changes)}

    def _save_regime_state(self, regimes: dict) -> None:
        try:
            tmp = REGIME_STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(regimes, indent=2))
            os.replace(tmp, REGIME_STATE)
        except Exception as e:
            self.log.debug("save regime state failed: %s", e)

    def _sync_ai_state(self, regimes: dict) -> None:
        try:
            state = {}
            if AI_STATE_PATH.exists():
                state = json.loads(AI_STATE_PATH.read_text())
            state["regimes"] = regimes
            state["regime_updated_at"] = datetime.now(timezone.utc).isoformat()
            tmp = AI_STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, AI_STATE_PATH)
        except Exception:
            pass

    def _load_rules(self) -> dict:
        rules_path = HERE / "rules.json"
        try:
            return json.loads(rules_path.read_text())
        except Exception:
            return {}
