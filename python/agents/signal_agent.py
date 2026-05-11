"""
signal_agent.py — SIGNAL_INTELLIGENCE business: signal analysis and routing.

GOAL: Route high-confidence signals from signals.json to execution_agent,
      request regime rechecks on low confidence, feed learning pipeline.

NOTE: Signal generation is handled by the existing orchestrator service.
      This agent reads signals.json (written by orchestrator) and routes
      them — it does NOT run the orchestrator itself, avoiding duplication.

Tasks handled:
  FORCE_SIGNAL_CHECK  — re-read signals.json immediately
  ADJUST_SYMBOL_CONF  — apply per-symbol confidence override from learning

Self-tasks/events emitted:
  SIGNALS_READY       — when tradeable signals found
  signals routed to execution_agent via REQUEST_RISK_CHECK (via risk_agent)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_base import BaseAgent, Task

HERE         = Path(__file__).resolve().parent.parent
PROJECT_ROOT = HERE.parent
RULES_PATH   = HERE / "rules.json"
SIGNALS_PATH = PROJECT_ROOT / "shared" / "signals.json"
CONF_OVERRIDES_PATH = PROJECT_ROOT / "shared" / "conf_overrides.json"


class SignalAgent(BaseAgent):
    NAME   = "signal_agent"
    GOAL   = "Route high-confidence ICT signals to execution, feed learning pipeline"
    DOMAIN = "SIGNAL_INTELLIGENCE"
    HANDLES = ["FORCE_SIGNAL_CHECK", "ADJUST_SYMBOL_CONF"]
    CYCLE_INTERVAL_S = 20.0   # check signals.json every 20s

    def __init__(self):
        super().__init__()
        self._routed_ids: set = set()   # signal ids already routed this session
        self._conf_overrides: dict = {}
        self._no_tradeable_since: float = time.time()
        # Pre-seed with current batch so we don't re-route stale signals on startup
        existing = self._read_signals()
        self._last_signal_ts: str = existing.get("generated_at", "")
        if self._last_signal_ts:
            for s in existing.get("signals", []):
                if s.get("id"):
                    self._routed_ids.add(s["id"])
        self._load_conf_overrides()

    def _load_conf_overrides(self) -> None:
        try:
            if CONF_OVERRIDES_PATH.exists():
                self._conf_overrides = json.loads(CONF_OVERRIDES_PATH.read_text())
        except Exception:
            self._conf_overrides = {}

    async def _run_cycle(self) -> dict:
        rules    = self._load_rules()
        min_conf = float(rules.get("dual_tf", {}).get("min_confidence", 0.50))
        signals  = self._read_signals()

        if not signals:
            return {"status": "no_signals"}

        # Check if this is a new batch (different generated_at)
        new_batch_ts = signals.get("generated_at", "")
        if new_batch_ts == self._last_signal_ts:
            return {"status": "already_processed", "ts": new_batch_ts}
        self._last_signal_ts = new_batch_ts

        sig_list = signals.get("signals", [])
        tradeable = []
        low_conf  = []

        for sig in sig_list:
            sid = sig.get("id", "")
            if not sid or sid in self._routed_ids:
                continue
            if sig.get("entry_type", "none") == "none":
                self._routed_ids.add(sid)
                continue

            # Apply symbol-level confidence override from learning agent
            symbol = sig.get("symbol", "")
            override = self._conf_overrides.get(symbol, 0.0)
            effective_conf = float(sig.get("confidence", 0)) + override

            # Check symbol-specific min_conf from rules
            sym_override = rules.get("symbol_overrides", {}).get(symbol, {})
            sym_min = float(sym_override.get("min_confidence", min_conf * 100)) / 100.0 \
                      if sym_override.get("min_confidence", 0) > 1 else \
                      float(sym_override.get("min_confidence", min_conf))

            if effective_conf < sym_min:
                self.log.debug("skip %s: conf=%.2f < min=%.2f", symbol, effective_conf, sym_min)
                low_conf.append(symbol)
                self._routed_ids.add(sid)
                continue

            tradeable.append(sig)
            self._routed_ids.add(sid)

        # Route tradeable signals through risk_agent
        routed = 0
        for sig in tradeable:
            self.send("risk_agent", "REQUEST_RISK_CHECK", {
                "signal": sig,
                "paper_mode": True,   # risk_agent reads OMNI_PAPER_MODE env
            }, priority=1)
            routed += 1

        if tradeable:
            self._no_tradeable_since = time.time()
            self.event("SIGNALS_READY", {
                "count":   len(tradeable),
                "symbols": [s["symbol"] for s in tradeable],
                "ts":      new_batch_ts,
            })
            # Feed to learning pipeline
            self.send("learning_agent", "SIGNAL_EMITTED", {
                "signals": [
                    {"symbol": s["symbol"], "direction": s["direction"],
                     "entry_type": s["entry_type"], "confidence": s["confidence"],
                     "reasons": s.get("reasons", [])}
                    for s in tradeable
                ]
            }, priority=5)

        # If no tradeable signal for >30 min during active hours, request regime check
        gap = time.time() - self._no_tradeable_since
        if gap > 1800 and self._is_active_session():
            self.send("regime_agent", "FORCE_REGIME_CHECK", {
                "reason": f"no_tradeable_signal_{int(gap)}s"
            }, priority=3)
            self._no_tradeable_since = time.time()

        # Prune seen set (keep last 200)
        if len(self._routed_ids) > 400:
            self._routed_ids = set(list(self._routed_ids)[-200:])

        return {
            "ts":       new_batch_ts,
            "routed":   routed,
            "low_conf": len(low_conf),
            "total":    len(sig_list),
        }

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "FORCE_SIGNAL_CHECK":
            self._last_signal_ts = ""   # force re-process
            return await self._run_cycle()

        if task.type == "ADJUST_SYMBOL_CONF":
            symbol = task.payload.get("symbol")
            delta  = task.payload.get("delta", 0.0)
            if symbol and delta:
                self._conf_overrides[symbol] = self._conf_overrides.get(symbol, 0.0) + delta
                # Clamp between -0.30 and +0.20
                self._conf_overrides[symbol] = max(-0.30, min(0.20, self._conf_overrides[symbol]))
                self._save_conf_overrides()
                self.log.info("conf override %s: %+.2f → %.2f",
                              symbol, delta, self._conf_overrides[symbol])
            return {"symbol": symbol, "override": self._conf_overrides.get(symbol)}

        return {"status": "unknown_task"}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_signals(self) -> dict:
        try:
            if SIGNALS_PATH.exists():
                return json.loads(SIGNALS_PATH.read_text())
        except Exception as e:
            self.log.debug("signals read failed: %s", e)
        return {}

    def _load_rules(self) -> dict:
        try:
            return json.loads(RULES_PATH.read_text())
        except Exception:
            return {}

    def _is_active_session(self) -> bool:
        h = datetime.now(timezone.utc).hour
        return 7 <= h < 21  # London through NY close

    def _save_conf_overrides(self) -> None:
        try:
            import os
            tmp = CONF_OVERRIDES_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._conf_overrides, indent=2))
            os.replace(tmp, CONF_OVERRIDES_PATH)
        except Exception as e:
            self.log.debug("save conf overrides failed: %s", e)
