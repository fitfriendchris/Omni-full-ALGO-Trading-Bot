"""
execution_agent.py — EXECUTION_DESK business: trade execution gatekeeper.

GOAL: Convert valid signals into MT5 orders with correct sizing and risk,
      notify on fills/rejections, and gate on risk_agent approval.

Tasks handled:
  EXECUTE_SIGNAL    — place an order from a signal (sent by risk_agent after approval)
  CANCEL_ORDER      — cancel a pending limit order by ticket
  CLOSE_POSITION    — close an open position (from risk_agent or trail_agent)

Self-tasks/broadcasts emitted:
  TRADE_OPENED      — broadcast after successful fill
  TRADE_REJECTED    — broadcast if risk or MT5 rejects
  REQUEST_RISK_CHECK — sent to risk_agent before executing
"""

from __future__ import annotations

import json
import logging
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
SIGNALS_PATH = PROJECT_ROOT / "shared" / "signals.json"
MT5_DATA     = (Path.home() / "Library/Application Support"
                / "net.metaquotes.wine.metatrader5/drive_c/users/user"
                / "AppData/Roaming/MetaQuotes/Terminal/Common/Files"
                / "omni_data.json")


class ExecutionAgent(BaseAgent):
    NAME   = "execution_agent"
    GOAL   = "Execute valid ICT signals as MT5 orders with correct risk sizing"
    DOMAIN = "EXECUTION_DESK"
    HANDLES = ["EXECUTE_SIGNAL", "CANCEL_ORDER", "CLOSE_POSITION"]
    CYCLE_INTERVAL_S = 15.0  # read signals every 15s

    def __init__(self):
        super().__init__()
        self._seen_signals: set = set()  # dedup by signal id
        self._paper_mode = os.getenv("OMNI_PAPER_MODE", "true").lower() != "false"

    async def _run_cycle(self) -> dict:
        """Noop — execution_agent is purely task-driven.
        Signal routing is handled by signal_agent → risk_agent → EXECUTE_SIGNAL task.
        This cycle just keeps the heartbeat alive and checks for stuck tasks."""
        # Clean up very old EXECUTE_SIGNAL tasks from our queue (safety valve)
        pending = self.bus.get_tasks(status="pending", to=self.NAME)
        stale_count = 0
        for t in pending:
            if t.get("type") == "EXECUTE_SIGNAL":
                from datetime import datetime, timezone
                try:
                    age = (datetime.now(timezone.utc) -
                           datetime.fromisoformat(t["created_at"])).total_seconds()
                    if age > 300:   # 5 min old = stale
                        self.bus.fail(t["id"], "stale_task")
                        stale_count += 1
                except Exception:
                    pass
        return {"status": "ready", "stale_cleaned": stale_count}

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "EXECUTE_SIGNAL":
            return self._execute(task.payload)
        if task.type == "CLOSE_POSITION":
            return self._close(task.payload)
        if task.type == "CANCEL_ORDER":
            return self._cancel(task.payload)
        return {"status": "unknown"}

    # ── Execution ─────────────────────────────────────────────────────────────

    def _execute(self, payload: dict) -> dict:
        sig = payload.get("signal", {})
        symbol    = sig.get("symbol", "")
        direction = sig.get("direction", "")
        entry     = sig.get("entry_price")
        sl        = sig.get("sl")
        tp        = sig.get("tp")

        if not all([symbol, direction, entry, sl, tp]):
            return {"status": "invalid_signal", "reason": "missing fields"}

        lot = payload.get("lot_size", 0.01)

        if self._paper_mode:
            self.log.info("[PAPER] would execute %s %s @ %.5f SL=%.5f TP=%.5f",
                          direction, symbol, entry, sl, tp)
            # Single broadcast — journal_agent handles TRADE_OPENED
            self.broadcast("TRADE_OPENED", {
                "signal": sig, "symbol": symbol, "direction": direction,
                "entry": entry, "sl": sl, "tp": tp,
                "lot": lot, "paper": True, "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "paper_logged", "symbol": symbol}

        # Live execution via auto_trader's send_command interface
        try:
            from auto_trader import send_command, place_order
            # EA accepts BUY/SELL for market orders (not "MARKET")
            order_type = "BUY" if direction in ("BUY", "BULL") else "SELL"
            result = place_order(
                symbol=symbol, direction=direction, order_type=order_type,
                price=entry, sl=sl, tp=tp, volume=lot,
                comment="OMNI_SWARM",
            )
            self.broadcast("TRADE_OPENED", {
                "signal": sig, "symbol": symbol, "direction": direction,
                "entry": entry, "sl": sl, "tp": tp,
                "lot": lot, "paper": False, "result": str(result),
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "executed", "symbol": symbol, "result": str(result)}
        except Exception as e:
            self.log.exception("execution failed: %s", e)
            self.broadcast("TRADE_REJECTED", {
                "symbol": symbol, "reason": str(e),
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "failed", "error": str(e)}

    def _close(self, payload: dict) -> dict:
        ticket = payload.get("ticket")
        if not ticket or self._paper_mode:
            return {"status": "paper_skip" if self._paper_mode else "no_ticket"}
        try:
            from auto_trader import close_position
            result = close_position(int(ticket), volume=payload.get("volume", 0))
            self.broadcast("TRADE_CLOSED", {
                "ticket": ticket, "result": str(result),
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "closed", "ticket": ticket}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _cancel(self, payload: dict) -> dict:
        # Placeholder: pending orders via MT5 delete command
        return {"status": "not_implemented"}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_signals(self) -> list:
        if not SIGNALS_PATH.exists():
            return []
        data = json.loads(SIGNALS_PATH.read_text())
        return data.get("signals", [])

    def _load_rules(self) -> dict:
        try:
            return json.loads((HERE / "rules.json").read_text())
        except Exception:
            return {}
