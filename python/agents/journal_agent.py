"""
journal_agent.py — OPERATIONS business: trade journal and performance tracking.

GOAL: Record every trade open/close with full context and compute performance
      metrics so the team can see what's working and what isn't.

Tasks handled:
  LOG_TRADE_OPEN    — record new trade entry
  LOG_TRADE_CLOSE   — record trade exit with outcome
  PERFORMANCE_REPORT — generate performance summary

Self-tasks/broadcasts emitted:
  TRADE_CLOSED      — forwarded to learning_agent after every close
  PERFORMANCE_READY — broadcast when report is generated
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
JOURNAL_PATH = PROJECT_ROOT / "logs" / "trade_journal_swarm.json"
PERF_PATH    = PROJECT_ROOT / "shared" / "performance.json"


class JournalAgent(BaseAgent):
    NAME   = "journal_agent"
    GOAL   = "Record every trade with full context and surface performance insights"
    DOMAIN = "OPERATIONS"
    HANDLES = ["LOG_TRADE_OPEN", "LOG_TRADE_CLOSE", "PERFORMANCE_REPORT", "TRADE_OPENED", "TRADE_CLOSED"]
    CYCLE_INTERVAL_S = 60.0   # check paper exits every 60s, performance every cycle

    def __init__(self):
        super().__init__()
        self._trades: list = self._load_journal()

    async def _run_cycle(self) -> dict:
        closed = self._check_paper_exits()
        perf = self._compute_performance()
        self._save_performance(perf)
        return {**perf, "paper_closed": closed}

    async def _handle_task(self, task: Task) -> dict:
        if task.type in ("LOG_TRADE_OPEN", "TRADE_OPENED"):
            return self._log_open(task.payload)
        if task.type in ("LOG_TRADE_CLOSE", "TRADE_CLOSED"):
            return self._log_close(task.payload)
        if task.type == "PERFORMANCE_REPORT":
            perf = self._compute_performance()
            self.broadcast("PERFORMANCE_READY", perf, priority=5)
            return perf
        return {"status": "unknown"}

    # ── Paper position monitor ────────────────────────────────────────────────

    def _check_paper_exits(self) -> int:
        """Close paper trades whose TP or SL has been hit by current MT5 prices."""
        open_trades = [t for t in self._trades if t.get("status") == "OPEN" and t.get("paper", True)]
        if not open_trades:
            return 0

        try:
            import sys
            sys.path.insert(0, str(HERE))
            from mt5_connector import get_symbol_prices
            symbols = list({t["symbol"] for t in open_trades if t.get("symbol")})
            price_list = get_symbol_prices(symbols)
            prices = {p["symbol"]: float(p.get("bid", p.get("price", 0))) for p in price_list}
        except Exception as e:
            self.log.debug("price fetch failed: %s", e)
            return 0

        closed = 0
        changed = False
        for t in self._trades:
            if t.get("status") != "OPEN" or not t.get("paper", True):
                continue
            sym = t.get("symbol", "")
            price = prices.get(sym)
            if not price:
                continue
            entry = t.get("entry") or 0
            sl    = t.get("sl")    or 0
            tp    = t.get("tp")    or 0
            if not (entry and sl and tp):
                continue

            direction = t.get("direction", "BULL")
            risk = abs(entry - sl) or 1e-9
            hit = None

            if direction == "BULL":
                if price <= sl:
                    hit = "LOSS"
                elif price >= tp:
                    hit = "WIN"
            else:  # BEAR
                if price >= sl:
                    hit = "LOSS"
                elif price <= tp:
                    hit = "WIN"

            if hit:
                pnl = (tp - entry if hit == "WIN" else sl - entry) if direction == "BULL" \
                      else (entry - tp if hit == "WIN" else entry - sl)
                lot = float(t.get("lot") or 0.01)
                r_mult = round(pnl / risk, 2) if hit == "WIN" else -1.0
                t["status"]       = hit
                t["close_ts"]     = datetime.now(timezone.utc).isoformat()
                t["close_price"]  = price
                t["profit_usd"]   = round(pnl * lot * 100, 2)
                t["r_multiple"]   = r_mult
                t["close_reason"] = "tp_hit" if hit == "WIN" else "sl_hit"
                self.log.info("paper exit: %s %s %s @ %.5f  R=%.2f",
                              hit, direction, sym, price, r_mult)
                self.send("learning_agent", "TRADE_CLOSED", {"trade": t}, priority=3)
                closed += 1
                changed = True

        if changed:
            self._save_journal()
        return closed

    # ── Journal ops ───────────────────────────────────────────────────────────

    def _log_open(self, payload: dict) -> dict:
        sig = payload.get("signal", payload)
        record = {
            "id":         sig.get("id", f"swarm_{int(time.time()*1000)}"),
            "symbol":     sig.get("symbol", ""),
            "direction":  sig.get("direction", ""),
            "entry_type": sig.get("entry_type", ""),
            "entry":      sig.get("entry_price"),
            "sl":         sig.get("sl"),
            "tp":         sig.get("tp"),
            "confidence": sig.get("confidence"),
            "reasons":    sig.get("reasons", []),
            "lot":        payload.get("lot_size", payload.get("lot")),
            "paper":      payload.get("paper", True),
            "status":     "OPEN",
            "open_ts":    datetime.now(timezone.utc).isoformat(),
        }
        self._trades.append(record)
        self._save_journal()
        self.log.info("journal: opened %s %s @ %s", record["direction"], record["symbol"], record["entry"])
        return {"id": record["id"]}

    def _log_close(self, payload: dict) -> dict:
        ticket = payload.get("ticket") or payload.get("id", "")
        profit = float(payload.get("profit_usd", payload.get("profit", 0)) or 0)
        r_mult = float(payload.get("r_multiple", 0) or 0)
        reason = payload.get("close_reason", payload.get("reason", "unknown"))

        # Find open trade
        for t in reversed(self._trades):
            if t["status"] == "OPEN" and (
                t["id"] == ticket or t["symbol"] == payload.get("symbol", "")
            ):
                t["status"] = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BREAKEVEN")
                t["close_ts"]    = datetime.now(timezone.utc).isoformat()
                t["profit_usd"]  = profit
                t["r_multiple"]  = r_mult
                t["close_reason"] = reason
                self._save_journal()

                # Forward to learning agent
                self.send("learning_agent", "TRADE_CLOSED", {"trade": t}, priority=3)
                self.log.info("journal: closed %s %s profit=%.2f R=%.2f",
                              t["direction"], t["symbol"], profit, r_mult)
                return {"id": t["id"], "status": t["status"]}

        self.log.debug("no open trade found for close payload: %s", payload)
        return {"status": "no_match"}

    # ── Performance ───────────────────────────────────────────────────────────

    def _compute_performance(self) -> dict:
        closed = [t for t in self._trades if t["status"] in ("WIN", "LOSS", "BREAKEVEN")]
        if not closed:
            return {"total": 0, "note": "no closed trades"}

        wins   = [t for t in closed if t["status"] == "WIN"]
        losses = [t for t in closed if t["status"] == "LOSS"]
        total_pnl = sum(t.get("profit_usd", 0) or 0 for t in closed)
        avg_r     = (sum(t.get("r_multiple", 0) or 0 for t in closed) / len(closed)) if closed else 0
        win_rate  = len(wins) / len(closed) if closed else 0

        gross_win  = sum(t.get("profit_usd", 0) or 0 for t in wins)
        gross_loss = abs(sum(t.get("profit_usd", 0) or 0 for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

        by_symbol: dict = {}
        for t in closed:
            sym = t.get("symbol", "?")
            if sym not in by_symbol:
                by_symbol[sym] = {"wins": 0, "losses": 0, "pnl": 0.0}
            by_symbol[sym]["pnl"] += t.get("profit_usd", 0) or 0
            if t["status"] == "WIN":
                by_symbol[sym]["wins"] += 1
            else:
                by_symbol[sym]["losses"] += 1

        return {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "total":       len(closed),
            "wins":        len(wins),
            "losses":      len(losses),
            "win_rate":    round(win_rate, 3),
            "avg_r":       round(avg_r, 3),
            "profit_factor": round(pf, 2) if pf != float("inf") else 99.9,
            "total_pnl":   round(total_pnl, 2),
            "by_symbol":   by_symbol,
            "open_count":  len([t for t in self._trades if t["status"] == "OPEN"]),
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_journal(self) -> list:
        try:
            if JOURNAL_PATH.exists():
                return json.loads(JOURNAL_PATH.read_text())
        except Exception:
            pass
        return []

    def _save_journal(self) -> None:
        try:
            JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = JOURNAL_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._trades[-500:], indent=2))
            os.replace(tmp, JOURNAL_PATH)
        except Exception as e:
            self.log.warning("journal save failed: %s", e)

    def _save_performance(self, perf: dict) -> None:
        try:
            tmp = PERF_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(perf, indent=2))
            os.replace(tmp, PERF_PATH)
        except Exception:
            pass
