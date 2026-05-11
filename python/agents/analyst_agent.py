"""
analyst_agent.py — LEARNING_LAB: LLM-powered trading analyst.

Uses Kimi K2 (via OpenRouter) to review performance and suggest
parameter changes that improve profitability. Applies safe changes
to rules.json and conf_overrides. Falls back to a no-op if LLM
is unavailable — the swarm runs fine without it.

Tasks handled:
  ANALYST_REVIEW    — on-demand analysis request
  APPLY_SUGGESTION  — apply a specific parameter patch

Self-tasks/events emitted:
  ANALYST_INSIGHT   — event with Kimi's analysis (human-readable)
  PARAM_UPDATED     — after any rule change is applied
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
RULES_PATH   = HERE / "rules.json"
CONF_PATH    = PROJECT_ROOT / "shared" / "conf_overrides.json"
PERF_PATH    = PROJECT_ROOT / "shared" / "performance.json"
JOURNAL_PATH = PROJECT_ROOT / "logs" / "trade_journal_swarm.json"
ANALYST_LOG  = PROJECT_ROOT / "logs" / "analyst.log"
SIGNALS_PATH = PROJECT_ROOT / "shared" / "signals.json"

# Parameter change limits — prevents Kimi from making wild adjustments
PARAM_LIMITS = {
    "min_confidence":      (0.40, 0.90),
    "min_rr":              (1.2,  3.0),
    "max_risk_pct":        (0.5,  3.0),
    "sl_buffer_frac":      (0.05, 0.40),
    "conf_override":       (-0.25, 0.20),  # per-symbol bias
}

SYSTEM_PROMPT = """You are a quantitative trading analyst for OMNI-ICT, an ICT/SMC strategy bot.

Your job: review live trading performance and suggest SPECIFIC, CONSERVATIVE parameter changes
that will increase profitability. Output ONLY valid JSON — no markdown, no explanation outside JSON.

Rules for suggestions:
- Only change parameters you have strong evidence for
- Keep changes small (≤0.05 per adjustment for confidence thresholds)
- If performance is acceptable (win_rate > 0.50, profit_factor > 1.2), suggest minor tweaks only
- If performance is poor (win_rate < 0.40 or all OPEN), suggest filtering tighter (raise min_confidence)
- Never suggest going below min_confidence=0.40 or min_rr=1.2
- Symbols with 0 trades = insufficient data, skip them

Output format:
{
  "analysis": "2-3 sentence summary of what you see",
  "confidence": "high|medium|low",
  "param_changes": {
    "rules": {
      "dual_tf.min_confidence": 0.65,
      "risk_rules.min_rr": 1.8
    },
    "conf_overrides": {
      "XAUUSD": 0.05,
      "GBPUSD": -0.10
    }
  },
  "skip_reason": "optional — if no changes needed, explain why"
}"""


class AnalystAgent(BaseAgent):
    NAME   = "analyst_agent"
    GOAL   = "Use Kimi K2 to review performance and tune parameters for profitability"
    DOMAIN = "LEARNING_LAB"
    HANDLES = ["ANALYST_REVIEW", "APPLY_SUGGESTION"]
    CYCLE_INTERVAL_S = 600.0   # run every 10 minutes

    def __init__(self):
        super().__init__()
        self._last_analysis_ts: float = 0.0
        self._llm_available: Optional[bool] = None  # None=untested

    async def _run_cycle(self) -> dict:
        # Don't run more than once per 10 min even if restarted
        if time.time() - self._last_analysis_ts < 540:
            return {"status": "cooldown"}

        context = self._build_context()
        if not context.get("has_data"):
            return {"status": "insufficient_data"}

        result = await self._run_analysis(context)
        self._last_analysis_ts = time.time()
        return result

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "ANALYST_REVIEW":
            context = self._build_context()
            return await self._run_analysis(context)
        if task.type == "APPLY_SUGGESTION":
            return self._apply_patch(task.payload.get("patch", {}))
        return {"status": "unknown"}

    # ── Analysis pipeline ─────────────────────────────────────────────────────

    async def _run_analysis(self, context: dict) -> dict:
        user_msg = json.dumps(context, indent=2)

        try:
            sys.path.insert(0, str(HERE))
            from llm_client import ask, parse_json
        except ImportError:
            return {"status": "llm_client_missing", "mode": "pure_python"}

        raw = await ask(SYSTEM_PROMPT, user_msg, max_tokens=600)

        if raw is None:
            self._llm_available = False
            self.log.info("analyst: LLM unavailable — pure-Python fallback")
            return {"status": "llm_unavailable", "mode": "pure_python"}

        self._llm_available = True
        parsed = parse_json(raw)
        if not parsed:
            self.log.warning("analyst: LLM returned unparseable JSON: %s", raw[:200])
            return {"status": "parse_error", "raw": raw[:200]}

        analysis_text = parsed.get("analysis", "")
        self.log.info("analyst: %s", analysis_text)

        # Emit human-readable event
        self.event("ANALYST_INSIGHT", {
            "analysis":   analysis_text,
            "confidence": parsed.get("confidence", "?"),
            "ts":         datetime.now(timezone.utc).isoformat(),
        })

        # Apply parameter changes (validated)
        applied = {}
        patch = parsed.get("param_changes", {})
        if patch:
            applied = self._apply_patch(patch)

        # Write to analyst log
        self._write_log(parsed, applied)

        return {
            "status":   "ok",
            "analysis": analysis_text,
            "applied":  applied,
            "mode":     "kimi",
        }

    # ── Parameter application ─────────────────────────────────────────────────

    def _apply_patch(self, patch: dict) -> dict:
        applied = {}

        # ── rules.json changes ────────────────────────────────────────────────
        rules_patch = patch.get("rules", {})
        if rules_patch:
            try:
                rules = json.loads(RULES_PATH.read_text())
                changed = False
                for key_path, val in rules_patch.items():
                    parts = key_path.split(".")
                    limit_key = parts[-1]
                    lo, hi = PARAM_LIMITS.get(limit_key, (None, None))
                    if lo is not None:
                        val = max(lo, min(hi, float(val)))
                    # Navigate and set nested key
                    node = rules
                    for p in parts[:-1]:
                        node = node.setdefault(p, {})
                    old = node.get(parts[-1])
                    node[parts[-1]] = val
                    applied[key_path] = {"old": old, "new": val}
                    changed = True
                    self.log.info("analyst: rules.%s %s → %s", key_path, old, val)
                if changed:
                    tmp = RULES_PATH.with_suffix(".tmp")
                    tmp.write_text(json.dumps(rules, indent=2))
                    os.replace(tmp, RULES_PATH)
                    self.event("PARAM_UPDATED", {"source": "analyst", "rules": applied})
            except Exception as e:
                self.log.warning("analyst: rules patch failed: %s", e)

        # ── conf_overrides changes ────────────────────────────────────────────
        conf_patch = patch.get("conf_overrides", {})
        if conf_patch:
            try:
                overrides = {}
                if CONF_PATH.exists():
                    overrides = json.loads(CONF_PATH.read_text())
                lo, hi = PARAM_LIMITS["conf_override"]
                for sym, delta in conf_patch.items():
                    sym = sym.upper()
                    delta = max(lo, min(hi, float(delta)))
                    old = overrides.get(sym, 0.0)
                    overrides[sym] = max(lo, min(hi, old + delta))
                    applied[f"conf/{sym}"] = {"old": old, "new": overrides[sym]}
                    self.log.info("analyst: conf_override %s %+.3f → %.3f", sym, delta, overrides[sym])
                tmp = CONF_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(overrides, indent=2))
                os.replace(tmp, CONF_PATH)
                # Notify signal_agent to reload overrides
                self.send("signal_agent", "ADJUST_SYMBOL_CONF",
                          {"reload": True}, priority=3)
            except Exception as e:
                self.log.warning("analyst: conf patch failed: %s", e)

        return applied

    # ── Context builder ───────────────────────────────────────────────────────

    def _build_context(self) -> dict:
        ctx: dict = {"has_data": False}

        # Performance
        try:
            if PERF_PATH.exists():
                perf = json.loads(PERF_PATH.read_text())
                ctx["performance"] = perf
                ctx["has_data"] = True
        except Exception:
            pass

        # Recent trades (last 20 closed)
        try:
            if JOURNAL_PATH.exists():
                trades = json.loads(JOURNAL_PATH.read_text())
                closed = [t for t in trades if t.get("status") in ("WIN","LOSS","BREAKEVEN")]
                ctx["recent_closed_trades"] = [
                    {
                        "symbol":    t.get("symbol"),
                        "direction": t.get("direction"),
                        "status":    t.get("status"),
                        "r":         t.get("r_multiple"),
                        "reason":    t.get("close_reason"),
                        "entry_type": t.get("entry_type"),
                    }
                    for t in closed[-20:]
                ]
                open_t = [t for t in trades if t.get("status") == "OPEN"]
                ctx["open_trade_count"] = len(open_t)
                if closed:
                    ctx["has_data"] = True
        except Exception:
            pass

        # Current rules
        try:
            rules = json.loads(RULES_PATH.read_text())
            ctx["current_rules"] = {
                "min_confidence": rules.get("dual_tf", {}).get("min_confidence"),
                "min_rr":         rules.get("risk_rules", {}).get("min_rr"),
                "max_risk_pct":   rules.get("risk_rules", {}).get("max_risk_pct_per_trade"),
                "sl_buffer_frac": rules.get("dual_tf", {}).get("sl_buffer_frac"),
                "watchlist":      rules.get("watchlist", []),
            }
        except Exception:
            pass

        # Current conf overrides
        try:
            if CONF_PATH.exists():
                ctx["conf_overrides"] = json.loads(CONF_PATH.read_text())
        except Exception:
            pass

        # Recent signals (symbol/direction/confidence)
        try:
            if SIGNALS_PATH.exists():
                sigs = json.loads(SIGNALS_PATH.read_text())
                ctx["recent_signals"] = [
                    {"symbol": s.get("symbol"), "direction": s.get("direction"),
                     "confidence": s.get("confidence"), "entry_type": s.get("entry_type")}
                    for s in sigs.get("signals", [])
                    if s.get("entry_type", "none") != "none"
                ]
        except Exception:
            pass

        ctx["utc_hour"] = datetime.now(timezone.utc).hour
        return ctx

    # ── Logging ───────────────────────────────────────────────────────────────

    def _write_log(self, parsed: dict, applied: dict) -> None:
        try:
            ANALYST_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts":         datetime.now(timezone.utc).isoformat(),
                "analysis":   parsed.get("analysis", ""),
                "confidence": parsed.get("confidence", "?"),
                "skip":       parsed.get("skip_reason"),
                "applied":    applied,
            }
            with open(ANALYST_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
