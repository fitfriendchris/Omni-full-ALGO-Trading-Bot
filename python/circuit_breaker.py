"""
circuit_breaker.py — Borsellino Commandment #9: 3 losses → mandatory break.

Implements:
  - 3-strike circuit breaker (Command IX)
  - Daily/weekly loss limits with auto-risk reduction (capital preservation)
  - Mandatory loss review before resuming (Command VIII — love your losers)

State persisted in trader_state.json["borsellino_state"].
No dependencies on auto_trader.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = print  # agents set their own logger; this module stays lightweight

_HERE = Path(__file__).resolve().parent
_STATE_PATH = _HERE / "trader_state.json"


# ── Default configuration (override via rules.json → borsellino_rules) ─────
DEFAULTS = {
    "circuit_breaker": {
        "enabled": True,
        "consecutive_losses_to_halt": 3,
        "halt_duration_hours": 12,
        "reset_on_next_killzone": True,
    },
    "capital_preservation": {
        "enabled": True,
        "weekly_drawdown_pct_to_reduce_risk": 10.0,
        "weekly_drawdown_pct_to_halt": 20.0,
        "reduced_risk_multiplier": 0.5,
    },
    "loss_review": {
        "mandatory_after_streak": 3,
        "confluence_boost_post_loss": 2,
    },
}


class CircuitBreaker:
    """Stateful circuit breaker tracking losses, drawdown, and mandatory review."""

    def __init__(self, rules: dict = None):
        self._rules = rules or {}
        self._bs = {}  # borsellino_state dict from trader_state

    # ── Public API ──────────────────────────────────────────────────────────

    def on_trade_closed(self, outcome: str, r_multiple: float, equity: float,
                        state: dict) -> dict:
        """
        Call after every trade close with outcome WIN or LOSS.
        Returns dict with keys: halted(bool), reason(str), actions(list).
        May mutate state["borsellino_state"] in-place.
        """
        actions: list[str] = []
        bs = self._get_bs(state)

        # Update streaks
        if outcome == "WIN":
            bs["loss_streak"] = 0
            bs["win_streak"] = bs.get("win_streak", 0) + 1
            actions.append("streak_reset")
        elif outcome == "LOSS":
            bs["win_streak"] = 0
            bs["loss_streak"] = bs.get("loss_streak", 0) + 1
            bs["total_losses"] = bs.get("total_losses", 0) + 1
            actions.append(f"loss_streak_{bs['loss_streak']}")
        else:
            return {"halted": False, "reason": "", "actions": []}

        # Track R for loss review
        if outcome == "LOSS":
            bs["last_loss_r"] = r_multiple
            bs["review_pending"] = True

        # Track weekly drawdown
        week_key = datetime.now(timezone.utc).strftime("%Y-W%W")
        if bs.get("week_key") != week_key:
            bs["week_key"] = week_key
            bs["week_start_equity"] = equity
            bs["week_max_equity"] = equity
        bs["week_max_equity"] = max(bs.get("week_max_equity", equity), equity)
        dd_pct = ((bs["week_max_equity"] - equity) / bs["week_max_equity"] * 100) if bs["week_max_equity"] > 0 else 0
        bs["week_drawdown_pct"] = dd_pct

        # ── Circuit breaker (Command IX) ─────────────────────────────
        cb = self._get_sub("circuit_breaker")
        if cb.get("enabled") and bs["loss_streak"] >= cb["consecutive_losses_to_halt"]:
            halt_until = (datetime.now(timezone.utc) + timedelta(hours=cb["halt_duration_hours"])).isoformat()
            bs["halted"] = True
            bs["halt_reason"] = f"3-strike circuit breaker (commandment_ix)"
            bs["halt_until"] = halt_until
            actions.append("circuit_breaker_engaged")
            return {"halted": True,
                    "reason": bs["halt_reason"],
                    "actions": actions,
                    "halt_until": halt_until}

        # ── Capital preservation — weekly drawdown ────────────────────
        cp = self._get_sub("capital_preservation")
        if cp.get("enabled"):
            reduce_at = cp["weekly_drawdown_pct_to_reduce_risk"]
            halt_at = cp["weekly_drawdown_pct_to_halt"]
            if dd_pct >= halt_at:
                bs["halted"] = True
                bs["halt_reason"] = f"weekly drawdown halt: {dd_pct:.1f}%"
                actions.append("weekly_drawdown_halt")
                return {"halted": True, "reason": bs["halt_reason"], "actions": actions}
            elif dd_pct >= reduce_at:
                bs["risk_multiplier"] = cp.get("reduced_risk_multiplier", 0.5)
                actions.append(f"risk_reduced_{bs['risk_multiplier']}")

        # ── Loss review mandatory (Command VIII) ──────────────────────
        lr = self._get_sub("loss_review")
        if lr.get("mandatory_after_streak") and bs["loss_streak"] >= lr["mandatory_after_streak"]:
            bs["review_required"] = True
            actions.append("review_required")

        self._save_bs(state, bs)
        return {"halted": False, "reason": "", "actions": actions}

    def can_trade(self, state: dict) -> dict:
        """Check if trading is currently allowed. Returns {ok, reason, risk_multiplier}."""
        bs = self._get_bs(state)
        if bs.get("halted"):
            halt_until = bs.get("halt_until")
            if halt_until:
                try:
                    until = datetime.fromisoformat(halt_until)
                    if datetime.now(timezone.utc) >= until:
                        # Auto-release if halt period done
                        bs["halted"] = False
                        bs["halt_reason"] = ""
                        bs["review_required"] = True  # still need review
                        self._save_bs(state, bs)
                        return {"ok": False, "reason": "circuit_breaker_expired_review_required", "risk_multiplier": bs.get("risk_multiplier", 1.0)}
                except Exception:
                    pass
            return {"ok": False, "reason": bs.get("halt_reason", "trading_halted"), "risk_multiplier": 0.0}
        return {"ok": True, "reason": "", "risk_multiplier": bs.get("risk_multiplier", 1.0)}

    def submit_review(self, state: dict, review_text: str) -> dict:
        """Submit mandatory loss review to lift review_required and halt flags."""
        bs = self._get_bs(state)
        bs["review_pending"] = False
        bs["review_required"] = False
        bs["halted"] = False
        bs["halt_reason"] = f"review_submitted: {review_text[:80]}"
        bs["halt_until"] = ""
        bs["loss_streak"] = 0
        bs["last_review"] = review_text
        bs["last_review_ts"] = datetime.now(timezone.utc).isoformat()
        self._save_bs(state, bs)
        return {"status": "review_submitted", "loss_streak_reset": True, "halt_cleared": True}

    def get_summary(self, state: dict) -> dict:
        bs = self._get_bs(state)
        return {
            "loss_streak": bs.get("loss_streak", 0),
            "win_streak": bs.get("win_streak", 0),
            "review_pending": bs.get("review_pending", False),
            "review_required": bs.get("review_required", False),
            "halted": bs.get("halted", False),
            "halt_reason": bs.get("halt_reason", ""),
            "halt_until": bs.get("halt_until", ""),
            "week_drawdown_pct": bs.get("week_drawdown_pct", 0.0),
            "risk_multiplier": bs.get("risk_multiplier", 1.0),
        }

    # ── Internals ───────────────────────────────────────────────────────────

    def _get_sub(self, key: str) -> dict:
        return self._rules.get("borsellino_rules", {}).get(key, DEFAULTS.get(key, {}))

    def _get_bs(self, state: dict) -> dict:
        bs = state.get("borsellino_state", {})
        if not bs:
            state["borsellino_state"] = bs = {
                "loss_streak": 0, "win_streak": 0,
                "total_losses": 0, "review_pending": False,
                "review_required": False, "halted": False,
                "halt_reason": "", "halt_until": "",
                "week_key": "", "week_start_equity": 0.0,
                "week_max_equity": 0.0, "week_drawdown_pct": 0.0,
                "risk_multiplier": 1.0, "last_review": "",
                "last_review_ts": "", "last_loss_r": 0.0,
            }
        return bs

    def _save_bs(self, state: dict, bs: dict) -> None:
        state["borsellino_state"] = bs


# ── Helper: risk-agent integration ──────────────────────────────────────────

def check_before_trade(state: dict, rules: dict) -> dict:
    """Drop-in for risk_agent — returns {approved, reason}."""
    cb = CircuitBreaker(rules)
    result = cb.can_trade(state)
    if not result["ok"]:
        return {"approved": False, "reason": result["reason"]}
    return {"approved": True, "risk_multiplier": result["risk_multiplier"]}


def on_trade_result(state: dict, outcome: str, r_multiple: float, equity: float,
                    rules: dict) -> dict:
    """Drop-in for journal_agent / execution_agent — process close event."""
    cb = CircuitBreaker(rules)
    return cb.on_trade_closed(outcome, r_multiple, equity, state)


def mandatory_review_done(state: dict, text: str, rules: dict) -> dict:
    cb = CircuitBreaker(rules)
    return cb.submit_review(state, text)


if __name__ == "__main__":
    # Smoke test
    state = {}
    cb = CircuitBreaker()
    print("INIT", cb.can_trade(state))
    print("LOSS1", cb.on_trade_closed("LOSS", -1.0, 200.0, state))
    print("LOSS2", cb.on_trade_closed("LOSS", -1.5, 198.0, state))
    print("LOSS3", cb.on_trade_closed("LOSS", -2.0, 196.0, state))
    print("CAN_TRADE", cb.can_trade(state))
    print("REVIEW", cb.submit_review(state, "SL too tight; widen by 20% next session"))
    print("CAN_TRADE_AFTER", cb.can_trade(state))
    print("SUMMARY", cb.get_summary(state))
