"""
position_trailing_manager.py — Standalone trailing stop + open position manager.
Runs as an OMNI swarm agent so it stays alive and manages SLs/TPs
independently of auto_trader's main loop (which is often not running).

WHAT IT DOES
------------
Every SCAN_INTERVAL seconds:
  1. Reads omni_data.json for live open positions
  2. Loads trader_state.json for TP ladder / tp1_taken / etc.
  3. Applies the same trailing stop logic auto_trader.py has, minus
     the heavy ICT scanning / new-trade decision code.
  4. Calls modify_position() when SL should tighten, or close_position()
     on take-profit / early-exit.
  5. Persents updated state back to trader_state.json and logs

INTEGRATION
-----------
Add it to the swarm registry in swarm.py:

    ("trail_agent", "agents.position_trailing_manager", "TrailingManager"),
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Bring in OMNI modules
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from agent_base import BaseAgent, Task

# Auto_trader helpers — import in a safe way (may fail if dotenv/config is borked)
try:
    from auto_trader import (
        modify_position, close_position, load_rules, load_state, save_state,
        TraderState, get_open_positions, get_account,
    )
    _AT_OK = True
except Exception as _e:
    _AT_OK = False
    _AT_ERR = str(_e)

# Smart trail integration
try:
    from smart_trail_adapter import maybe_smart_trail, is_enabled as _smart_trail_enabled
    _ST_OK = True
except Exception as _e:
    _ST_OK = False

# MT5 data
# NOTE: mt5_connector no longer exports load_mt5_data — auto_trader does.
# Keep mt5_connector fallback but auto_trader is the canonical source.
try:
    from mt5_connector import load_with_retry as _load_mt5_data
    _MT5_OK = True
except Exception:
    _MT5_OK = False
    from config import cfg as _cfg
    JSON_PATH = getattr(_cfg, "JSON_PATH", "")

log = logging.getLogger("trail_manager")

SCAN_INTERVAL = 15  # seconds between trail checks


class TrailingManager(BaseAgent):
    """Swarm agent that continuously tightens trailing stops and manages exits."""

    NAME = "trail_manager"
    GOAL = "Protect open positions with trailing stops and manage exits"
    DOMAIN = "EXECUTION_DESK"
    HANDLES = ["TRAIL_CHECK", "TRAIL_EMERGENCY_CLOSE"]
    CYCLE_INTERVAL_S = SCAN_INTERVAL

    def __init__(self):
        super().__init__()
        self._state: Optional[TraderState] = None
        self._rules: dict = {}
        self._scan_count = 0
        if not _AT_OK:
            log.error("auto_trader import failed: %s", _AT_ERR)

    async def _run_cycle(self) -> dict:
        if not _AT_OK:
            return {"status": "init_error", "error": _AT_ERR}

        self._scan_count += 1
        sc = self._scan_count

        # ── Load fresh data ───────────────────────────────────────
        try:
            self._state = load_state()
        except Exception as e:
            log.warning("load_state error: %s", e)
            self._state = None

        try:
            self._rules = load_rules()
        except Exception as e:
            log.warning("load_rules error: %s", e)
            self._rules = {}

        data = None
        try:
            data = _load_mt5_data()
        except Exception as e:
            log.debug("load_mt5_data error: %s", e)

        if not data:
            return {"status": "no_data", "scan": sc}

        positions = get_open_positions(data)
        account = get_account(data)
        equity = account.get("equity", 0)
        charts = data.get("charts", {})

        if not positions:
            return {"status": "no_positions", "scan": sc}

        # ── Manage each position ──────────────────────────────────
        modified = 0
        closed = 0
        errors = 0

        for pos in positions:
            try:
                res = self._manage_one(pos, charts, equity)
                if res == "modified":
                    modified += 1
                elif res == "closed":
                    closed += 1
            except Exception as e:
                log.warning("trail error %s: %s", pos.get("ticket", "?"), e)
                errors += 1

        # Persist state
        try:
            if self._state:
                save_state(self._state)
        except Exception as e:
            log.warning("save_state error: %s", e)

        # Telemetry
        trail_proposals = getattr(self._state, "last_trail_proposals", {}) if self._state else {}
        return {
            "status": "ok",
            "scan": sc,
            "positions": len(positions),
            "modified": modified,
            "closed": closed,
            "errors": errors,
            "trail_proposals": len(trail_proposals),
        }

    def _manage_one(self, pos: dict, charts: dict, equity: float) -> str:
        """Apply trailing stop logic to a single position. Returns action name."""
        if not self._state:
            return "skipped"

        ticket = pos.get("ticket", 0)
        ticket_str = str(ticket)
        symbol = pos.get("symbol", "")
        pos_type = pos.get("type", "")
        open_price = float(pos.get("open_price", 0))
        current_price = float(pos.get("current_price", 0))
        current_sl = float(pos.get("sl", 0))
        current_tp = float(pos.get("tp", 0))
        volume = float(pos.get("volume", 0))

        if not ticket or not symbol or open_price == 0 or current_price == 0:
            return "skipped"

        state = self._state

        # Ensure ticket is tracked in state
        if ticket_str not in state.active_trades:
            state.active_trades[ticket_str] = {}
        state.active_trades[ticket_str]["last_profit"] = pos.get("profit", 0)

        # Per-symbol info
        sym_info = charts.get(symbol, {})
        pip_size = float(sym_info.get("point", 0.0001)) * 10
        min_lot = float(sym_info.get("min_lot", 0.01))

        setup = state.active_trades.get(ticket_str, {})
        tp1 = setup.get("tp1", current_tp)
        tp2 = setup.get("tp2", current_tp)
        tp3 = setup.get("tp3", current_tp)
        tp1_taken = setup.get("tp1_taken", False)
        tp2_taken = setup.get("tp2_taken", False)

        risk = abs(open_price - current_sl)
        if risk == 0:
            return "skipped"

        new_sl = current_sl
        new_tp = current_tp
        profit_in_r = 0.0

        if pos_type == "BUY":
            profit_in_r = (current_price - open_price) / risk

            # TP1 partial close: bank 50%
            if not tp1_taken and tp1 > 0 and current_price >= tp1:
                close_vol = round(volume * 0.50, 2)
                if close_vol >= min_lot:
                    result = close_position(ticket, close_vol)
                    if str(result).startswith("OK") or str(result).startswith("PAPER"):
                        state.active_trades[ticket_str]["tp1_taken"] = True
                        new_sl = max(new_sl, open_price + pip_size)
                        if tp2 > 0 and tp2 > current_tp:
                            new_tp = tp2
                        log.info("%s TP1 hit — closed 50%% %.2f lots | %s", symbol, close_vol, result)

            # TP2 partial close: bank 25%, release runner
            elif tp1_taken and not tp2_taken and tp2 > 0 and current_price >= tp2:
                close_vol = round(volume * 0.25, 2)
                if close_vol >= min_lot:
                    result = close_position(ticket, close_vol)
                    if str(result).startswith("OK") or str(result).startswith("PAPER"):
                        state.active_trades[ticket_str]["tp2_taken"] = True
                        new_tp = 0
                        log.info("%s TP2 hit — runner released %.2f lots | %s", symbol, close_vol, result)

            # Early exit — 0.3R adverse after 20min
            if not tp1_taken and profit_in_r < -0.3:
                try:
                    entry_ts = float(setup.get("entry_ts", 0)) or time.time()
                    mins_open = (time.time() - entry_ts) / 60
                    if mins_open > 20:
                        log.info("%s BUY early exit %.2fR after %.0fmin", symbol, profit_in_r, mins_open)
                        result = close_position(ticket, volume)
                        return "closed"
                except Exception:
                    pass

            # ── Legacy profit-lock trails ───────────────────────────
            if profit_in_r >= 0.4 and current_sl < open_price:
                candidate = open_price + pip_size
                if candidate > new_sl:
                    new_sl = candidate
                    log.info("%s BUY 0.4R: SL → breakeven %.5f", symbol, new_sl)

            if profit_in_r >= 1.0 and current_sl < open_price + risk * 0.5:
                candidate = open_price + risk * 0.5
                if candidate > new_sl:
                    new_sl = candidate
                    log.info("%s BUY 1R: lock 0.5R profit SL %.5f", symbol, new_sl)

            if profit_in_r >= 2.0 and current_sl < open_price + risk * 1.2:
                candidate = open_price + risk * 1.2
                if candidate > new_sl:
                    new_sl = candidate
                    log.info("%s BUY 2R: lock 1.2R profit SL %.5f", symbol, new_sl)

            if profit_in_r >= 3.0:
                # Tight trail — regime-aware
                _regime = "DEFAULT"
                try:
                    _regime = self._rules.get("current_regime", "DEFAULT")
                except Exception:
                    pass
                _width = 0.20 if _regime == "VOLATILE" else (0.4 if _regime == "TRENDING" else 0.35)
                candidate = current_price - risk * _width
                if candidate > new_sl:
                    new_sl = candidate
                    log.info("%s BUY 3R: tight trail %.5f [regime=%s]", symbol, new_sl, _regime)

                # Progressive trail beyond 3R
                prog = current_price - risk * 0.3
                if prog > new_sl:
                    new_sl = prog

        elif pos_type == "SELL":
            profit_in_r = (open_price - current_price) / risk

            if not tp1_taken and tp1 > 0 and current_price <= tp1:
                close_vol = round(volume * 0.50, 2)
                if close_vol >= min_lot:
                    result = close_position(ticket, close_vol)
                    if str(result).startswith("OK") or str(result).startswith("PAPER"):
                        state.active_trades[ticket_str]["tp1_taken"] = True
                        new_sl = min(new_sl, open_price - pip_size)
                        if tp2 > 0 and tp2 < current_tp:
                            new_tp = tp2
                        log.info("%s SELL TP1 hit — closed 50%% %.2f lots | %s", symbol, close_vol, result)

            elif tp1_taken and not tp2_taken and tp2 > 0 and current_price <= tp2:
                close_vol = round(volume * 0.25, 2)
                if close_vol >= min_lot:
                    result = close_position(ticket, close_vol)
                    if str(result).startswith("OK") or str(result).startswith("PAPER"):
                        state.active_trades[ticket_str]["tp2_taken"] = True
                        new_tp = 0
                        log.info("%s SELL TP2 hit — runner released %.2f lots | %s", symbol, close_vol, result)

            if not tp1_taken and profit_in_r < -0.3:
                try:
                    entry_ts = float(setup.get("entry_ts", 0)) or time.time()
                    mins_open = (time.time() - entry_ts) / 60
                    if mins_open > 20:
                        log.info("%s SELL early exit %.2fR after %.0fmin", symbol, profit_in_r, mins_open)
                        result = close_position(ticket, volume)
                        return "closed"
                except Exception:
                    pass

            if profit_in_r >= 0.4 and current_sl > open_price:
                candidate = open_price - pip_size
                if candidate < new_sl:
                    new_sl = candidate
                    log.info("%s SELL 0.4R: SL → breakeven %.5f", symbol, new_sl)

            if profit_in_r >= 1.0 and current_sl > open_price - risk * 0.5:
                candidate = open_price - risk * 0.5
                if candidate < new_sl:
                    new_sl = candidate
                    log.info("%s SELL 1R: lock 0.5R profit SL %.5f", symbol, new_sl)

            if profit_in_r >= 2.0 and current_sl > open_price - risk * 1.2:
                candidate = open_price - risk * 1.2
                if candidate < new_sl:
                    new_sl = candidate
                    log.info("%s SELL 2R: lock 1.2R profit SL %.5f", symbol, new_sl)

            if profit_in_r >= 3.0:
                _regime = "DEFAULT"
                try:
                    _regime = self._rules.get("current_regime", "DEFAULT")
                except Exception:
                    pass
                _width = 0.20 if _regime == "VOLATILE" else (0.4 if _regime == "TRENDING" else 0.35)
                candidate = current_price + risk * _width
                if candidate < new_sl:
                    new_sl = candidate
                    log.info("%s SELL 3R: tight trail %.5f [regime=%s]", symbol, new_sl, _regime)

                prog = current_price + risk * 0.3
                if prog < new_sl:
                    new_sl = prog

        # ── Smart trail overlay ───────────────────────────────────
        # This is where ATR / structure / momentum layers tighten SL further
        if _ST_OK and _smart_trail_enabled(self._rules):
            try:
                proposal = maybe_smart_trail(pos, charts, self._rules, None)
                if proposal is not None:
                    # Record for dashboard
                    state.last_trail_proposals[ticket_str] = {
                        "symbol": symbol,
                        "direction": pos_type,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "current_sl": current_sl,
                        "proposed_sl": proposal.new_sl,
                        "should_close": bool(proposal.should_close),
                        "reason": proposal.reason,
                        "layers": list(proposal.layers_fired or []),
                    }
                    if proposal.should_close and ticket_str.isdigit():
                        close_position(ticket)
                        log.info("%s smart_trail CLOSE: %s", symbol, proposal.reason)
                        return "closed"
                    # Take whichever SL is MORE protective
                    if pos_type == "BUY" and proposal.new_sl > new_sl:
                        log.info("%s smart_trail SL %.5f → %.5f (%s)", symbol, new_sl, proposal.new_sl, proposal.reason)
                        new_sl = proposal.new_sl
                    elif pos_type == "SELL" and proposal.new_sl < new_sl:
                        log.info("%s smart_trail SL %.5f → %.5f (%s)", symbol, new_sl, proposal.new_sl, proposal.reason)
                        new_sl = proposal.new_sl
            except Exception as e:
                log.warning("smart_trail error %s: %s", symbol, e)

        # ── Apply modification if SL moved meaningfully ─────────
        min_move = max(risk * 0.05, pip_size * 3)
        if abs(new_sl - current_sl) > min_move and ticket_str.isdigit():
            result = modify_position(ticket, new_sl, new_tp)
            log.info("Modified %s SL %.5f → %.5f | %s", ticket_str, current_sl, new_sl, result)
            return "modified"

        return "checked"

    async def _handle_task(self, task: Task) -> dict:
        if task.type == "TRAIL_CHECK":
            return await self._run_cycle()
        if task.type == "TRAIL_EMERGENCY_CLOSE":
            ticket = task.payload.get("ticket", 0)
            if ticket:
                result = close_position(ticket)
                return {"status": "emergency_closed", "ticket": ticket, "result": str(result)}
            return {"status": "no_ticket"}
        return {"status": "unknown_task"}


if __name__ == "__main__":
    # Standalone test
    logging.basicConfig(level=logging.DEBUG)
    tm = TrailingManager()
    import asyncio
    print(asyncio.run(tm._run_cycle()))
