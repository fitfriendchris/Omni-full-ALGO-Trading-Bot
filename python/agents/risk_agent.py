"""
risk_agent.py — EXECUTION_DESK business: risk gatekeeper and drawdown guard.

GOAL: Protect capital by enforcing drawdown limits, position concentration,
      and per-trade risk sizing before any order reaches MT5.

Tasks handled:
  REQUEST_RISK_CHECK  — from execution_agent: approve/reject a signal
  HALT_TRADING        — emergency halt from monitor or user
  RESUME_TRADING      — lift halt
  CHECK_DRAWDOWN      — on-demand drawdown report

Self-tasks/broadcasts emitted:
  EXECUTE_SIGNAL      — forwarded to execution_agent after approval
  RISK_ALERT          — broadcast when drawdown/loss limits approached
  TRADING_HALTED      — broadcast on emergency halt
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_base import BaseAgent, Task

HERE         = Path(__file__).resolve().parent.parent
PROJECT_ROOT = HERE.parent
MT5_DATA_PATH = (Path.home() / "Library/Application Support"
                 / "net.metaquotes.wine.metatrader5/drive_c/users/user"
                 / "AppData/Roaming/MetaQuotes/Terminal/Common/Files"
                 / "omni_data.json")
STATE_PATH = HERE / "trader_state.json"


class RiskAgent(BaseAgent):
    NAME   = "risk_agent"
    GOAL   = "Protect capital: enforce drawdown limits and size every trade correctly"
    DOMAIN = "EXECUTION_DESK"
    HANDLES = ["REQUEST_RISK_CHECK", "HALT_TRADING", "RESUME_TRADING", "CHECK_DRAWDOWN"]
    CYCLE_INTERVAL_S = 30.0  # periodic drawdown scan

    def __init__(self):
        super().__init__()
        self._halted = False
        self._halt_reason = ""
        self._daily_loss_usd = 0.0
        self._open_count = 0
        # Seed day-start balance from live account so daily PnL tracks realized moves,
        # not unrealized swap/float on pre-existing positions.
        acct = self._get_account()
        self._day_start_balance = float(acct.get("balance", 0)) if acct else 0.0

    async def _run_cycle(self) -> dict:
        """Periodic: scan account state, alert if near limits."""
        acct = self._get_account()
        if not acct:
            return {"status": "no_account_data"}

        equity   = float(acct.get("equity", 0))
        balance  = float(acct.get("balance", equity))
        rules    = self._load_rules().get("risk_rules", {})
        max_dd   = float(rules.get("max_daily_loss_pct", 5.0)) / 100.0

        # Daily PnL measured against the balance at agent startup, not open-position
        # equity — this prevents overnight swap charges on pre-existing positions from
        # falsely triggering a halt on startup.
        ref = self._day_start_balance if self._day_start_balance > 0 else balance
        daily_pnl = equity - ref

        result = {"equity": equity, "balance": balance, "daily_pnl": daily_pnl, "ref": ref}

        # Auto-halt on daily loss breach
        if ref > 0 and (-daily_pnl / ref) > max_dd:
            if not self._halted:
                reason = f"daily_loss_pct={(-daily_pnl/ref)*100:.1f}% > max={max_dd*100:.1f}%"
                self._trigger_halt(reason)
                result["halt_triggered"] = True

        # Alert if approaching limit (80%)
        elif ref > 0 and (-daily_pnl / ref) > (max_dd * 0.8):
            self.broadcast("RISK_ALERT", {
                "type": "approaching_daily_limit",
                "daily_pnl": daily_pnl,
                "limit_pct": max_dd * 100,
                "current_pct": (-daily_pnl / ref) * 100,
            }, priority=2)
            result["alert_sent"] = True

        return result

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "REQUEST_RISK_CHECK":
            return self._check_signal(task.payload)
        if task.type == "HALT_TRADING":
            return self._trigger_halt(task.payload.get("reason", "manual_halt"))
        if task.type == "RESUME_TRADING":
            return self._resume()
        if task.type == "CHECK_DRAWDOWN":
            return await self._run_cycle()
        return {"status": "unknown"}

    # ── Core risk check ───────────────────────────────────────────────────────

    def _check_signal(self, payload: dict) -> dict:
        sig = payload.get("signal", {})
        symbol    = sig.get("symbol", "")
        direction = sig.get("direction", "")
        entry     = float(sig.get("entry_price", 0) or 0)
        sl        = float(sig.get("sl", 0) or 0)
        conf      = float(sig.get("confidence", 0))
        paper     = payload.get("paper_mode", True)

        if self._halted:
            return {"approved": False, "reason": f"trading_halted: {self._halt_reason}"}

        rules = self._load_rules()

        # ── Borsellino circuit breaker & capital preservation ─────────────
        try:
            from circuit_breaker import check_before_trade
            ts = {}
            if STATE_PATH.exists():
                ts = json.loads(STATE_PATH.read_text())
            cb_ok = check_before_trade(ts, rules)
            if not cb_ok["approved"]:
                return {"approved": False, "reason": f"borsellino_halt: {cb_ok['reason']}"}
        except Exception:
            pass

        risk_rules = rules.get("risk_rules", {})
        overrides  = rules.get("symbol_overrides", {}).get(symbol, {})

        # Min confidence
        min_conf = float(overrides.get("min_confidence",
                         risk_rules.get("min_confidence_to_trade", 0.50)))
        if isinstance(min_conf, (int, float)) and min_conf > 1:
            min_conf /= 100.0
        if conf < min_conf:
            return {"approved": False, "reason": f"conf={conf:.2f} < min={min_conf:.2f}"}

        # SL must be valid
        if entry <= 0 or sl <= 0 or abs(entry - sl) < 1e-8:
            return {"approved": False, "reason": "invalid_entry_or_sl"}

        # Max open positions
        max_open = int(risk_rules.get("max_open_positions", 3))
        if self._open_count >= max_open and not paper:
            return {"approved": False, "reason": f"max_open={max_open} reached"}

        # Calculate lot size
        acct = self._get_account()
        equity = float(acct.get("equity", 10000)) if acct else 10000
        lot = self._calc_lot(equity, entry, sl, risk_rules, symbol)

        if lot <= 0:
            return {"approved": False, "reason": f"lot_zero_or_negative: equity=${equity:.2f}, lot={lot}"}

        # ── Confi
        tp = float(sig.get("tp", 0) or 0)
        if tp > 0 and entry > 0 and sl > 0:
            risk   = abs(entry - sl)
            reward = abs(tp - entry)
            rr     = reward / risk if risk > 0 else 0
            min_rr = float(risk_rules.get("min_rr_ratio", 2.0))
            if rr < min_rr:
                return {"approved": False, "reason": f"rr={rr:.2f} < min={min_rr}"}

        # Approved — forward to execution agent
        self.send("execution_agent", "EXECUTE_SIGNAL", {
            "signal":   sig,
            "lot_size": lot,
            "paper_mode": paper,
        }, priority=1)

        return {"approved": True, "symbol": symbol, "lot": lot, "conf": conf}

    def _calc_lot(self, equity: float, entry: float, sl: float,
                  risk_rules: dict, symbol: str) -> float:
        if equity < 10:
            self.log.critical("EQUITY %.2f — UNDERCAPITALIZED. Rejecting trade.", equity)
            return 0.0
        if equity < 100:
            self.log.warning("EQUITY %.2f — very low. Capping risk at 0.5%%.", equity)
        base_risk = float(risk_rules.get("base_risk_pct", 1.0)) / 100.0
        risk_usd  = equity * base_risk
        # Cap risk if equity < $100
        risk_usd  = min(risk_usd, equity * 0.005)
        sl_dist   = abs(entry - sl)
        if sl_dist < 1e-8:
            return 0.01

        if symbol == "XAUUSD":
            units = 100.0
        elif symbol == "XAGUSD":
            units = 5000.0
        elif "JPY" in symbol:
            units = 100000.0 / entry
        else:
            units = 100000.0

        lot = risk_usd / (sl_dist * units)
        return round(max(min(lot, 0.10), 0.01), 2)

    def _trigger_halt(self, reason: str) -> dict:
        self._halted = True
        self._halt_reason = reason
        self.log.warning("TRADING HALTED: %s", reason)
        self.broadcast("TRADING_HALTED", {"reason": reason, "ts": datetime.now(timezone.utc).isoformat()}, priority=1)
        self.send("monitor_agent", "SERVICE_ERROR", {
            "source": "risk_agent", "errors": [f"HALT: {reason}"]
        }, priority=1)
        return {"halted": True, "reason": reason}

    def _resume(self) -> dict:
        self._halted = False
        self._halt_reason = ""
        self.log.info("trading resumed")
        self.broadcast("TRADING_RESUMED", {"ts": datetime.now(timezone.utc).isoformat()}, priority=2)
        return {"halted": False}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_account(self) -> dict:
        """Read live account info from omni_data.json."""
        try:
            if MT5_DATA_PATH.exists():
                raw = MT5_DATA_PATH.read_bytes()
                # Strip trailing commas (MT5 quirk)
                import re
                raw_str = re.sub(rb",\s*([}\]])", rb"\1", raw).decode("utf-8", errors="replace")
                data = json.loads(raw_str)
                # Return the account block, not the root dict
                acct = data.get("account", {})
                if acct and acct.get("login"):
                    return acct
        except Exception:
            pass
        try:
            if STATE_PATH.exists():
                s = json.loads(STATE_PATH.read_text())
                return {"equity": s.get("equity", 0), "balance": s.get("balance", 0),
                        "currency": s.get("currency", "USD")}
        except Exception:
            pass
        return {}
    def _load_rules(self) -> dict:
        try:
            return json.loads((HERE / "rules.json").read_text())
        except Exception:
            return {}
