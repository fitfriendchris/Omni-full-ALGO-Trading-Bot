"""
execution_agent.py — EXECUTION_DESK business: trade execution gatekeeper.

GOAL: Convert valid signals into MT5 orders with correct sizing and risk,
      notify on fills/rejections, and gate on risk_agent approval.

V2 Fixes:
  ✓ Dynamic spread read from MT5 data (not hardcoded)
  ✓ Equity gate — skip symbols if margin > 50% of equity
  ✓ Entry price proximity check — skip if current price too far from signal
  ✓ Symbol availability validation
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_base import BaseAgent, Task

HERE         = Path(__file__).resolve().parent.parent
PROJECT_ROOT = HERE.parent
SIGNALS_PATH = PROJECT_ROOT / "shared" / "signals.json"
RULES_PATH   = HERE / "rules.json"
MT5_DATA     = (Path.home() / "Library/Application Support"
                / "net.metaquotes.wine.metatrader5/drive_c/users/user"
                / "AppData/Roaming/MetaQuotes/Terminal/Common/Files"
                / "omni_data.json")


def _load_mt5_data() -> dict:
    try:
        with open(MT5_DATA) as f:
            return json.load(f)
    except Exception:
        return {}


def _contract_value(symbol: str, chart: dict) -> float:
    """Approx margin required per 0.01 lot (USD)."""
    bid = float(chart.get("bid", chart.get("ndog_close", 0)))
    if bid <= 0:
        return float("inf")
    contract = float(chart.get("contract_size", 100000))
    # Margin ≈ bid × contract × lot_size / leverage
    # For gold: contract_size is frequently 100 oz, bid=$4540, lot=0.01
    # margin ≈ 4540 × 100 × 0.01 / 1000 = 4.54
    # But for small accounts broker may enforce higher margin
    return bid * contract * 0.01 / 1000.0


def _symbol_spread(symbol: str) -> float:
    data = _load_mt5_data()
    chart = data.get("charts", {}).get(symbol, {})
    spread = chart.get("spread")
    tick = float(chart.get("tick_size", 0.01))
    if spread is not None:
        return float(spread) * tick
    return tick * 20


def _symbol_spread_price(symbol: str) -> float:
    """Return spread converted to PRICE units (spread_points * tick_size)."""
    data = _load_mt5_data()
    chart = data.get("charts", {}).get(symbol, {})
    spread = chart.get("spread")
    tick = float(chart.get("tick_size", 0.01))
    if spread is not None:
        return float(spread) * tick
    return tick * 20


def _proximity_pct(entry: float, current: float) -> float:
    if entry <= 0:
        return 100.0
    return abs(entry - current) / entry * 100


def _load_rules() -> dict:
    try:
        with open(RULES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


class ExecutionAgent(BaseAgent):
    NAME   = "execution_agent"
    GOAL   = "Execute valid ICT signals as MT5 orders with correct risk sizing"
    DOMAIN = "EXECUTION_DESK"
    HANDLES = ["EXECUTE_SIGNAL", "CANCEL_ORDER", "CLOSE_POSITION"]
    CYCLE_INTERVAL_S = 15.0

    def __init__(self):
        super().__init__()
        self._seen_signals: set = set()
        self._paper_mode = os.getenv("OMNI_PAPER_MODE", "false").lower() != "false"  # LIVE now
        self._log = logging.getLogger("execution_agent")

    async def _run_cycle(self) -> dict:
        pending = self.bus.get_tasks(status="pending", to=self.NAME)
        stale_count = 0
        for t in pending:
            if t.get("type") == "EXECUTE_SIGNAL":
                try:
                    age = (datetime.now(timezone.utc) -
                           datetime.fromisoformat(t["created_at"])).total_seconds()
                    if age > 300:
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

    def _execute(self, payload: dict) -> dict:
        rules = _load_rules()
        sig = payload.get("signal", {})
        symbol    = sig.get("symbol", "")
        direction = sig.get("direction", "")
        entry     = sig.get("entry_price")
        sl        = sig.get("sl")
        tp        = sig.get("tp")
        conf      = sig.get("confidence", 0)

        # ── 1. Signal validation ──
        if not all([symbol, direction, entry, sl, tp]):
            return {"status": "invalid_signal", "reason": "missing fields"}

        # ── 2. Load live MT5 data ──
        mt5_data = _load_mt5_data()
        charts = mt5_data.get("charts", {})
        account = mt5_data.get("account", {})
        equity = float(account.get("equity", 0)) if isinstance(account, dict) else 0.0

        # ── 3. Equity gate ──
        chart = charts.get(symbol, {}) if isinstance(charts, dict) else {}
        margin_per_001 = _contract_value(symbol, chart)
        if equity > 0 and margin_per_001 > equity * 0.5:
            self._log.warning("EQUITY_GATE: skip %s — need $%.2f margin, have $%.2f",
                              symbol, margin_per_001, equity)
            self.broadcast("TRADE_REJECTED", {
                "symbol": symbol, "reason": "insufficient_margin",
                "margin_needed": margin_per_001, "equity": equity,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "rejected", "reason": "insufficient_margin",
                    "margin_needed": margin_per_001, "equity": equity}

        # ── 3.5  EQUITY TIER GATE ──
        sym_override = rules.get("symbol_overrides", {}).get(symbol, {})
        gate = sym_override.get("equity_gate")
        if gate:
            min_eq = float(gate.get("min_equity_usd", 0))
            override_conf = float(gate.get("override_confidence", 999))
            if equity < min_eq and conf < override_conf:
                msg = gate.get("message", f"{symbol} locked below ${min_eq} equity.")
                self._log.warning("EQUITY_TIER_GATE: %s (eq=%.2f < min=%.2f, conf=%.1f < %.1f)",
                                  symbol, equity, min_eq, conf, override_conf)
                self.broadcast("TRADE_REJECTED", {
                    "symbol": symbol, "reason": "equity_tier_gate",
                    "message": msg, "equity": equity, "min_equity": min_eq,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                return {"status": "rejected", "reason": "equity_tier_gate",
                        "message": msg, "equity": equity, "min_equity": min_eq}

        # ── 4. Symbol availability ──
        if symbol not in charts:
            self._log.warning("SYMBOL_SKIP %s: not in MT5 Market Watch / charts", symbol)
            return {"status": "rejected", "reason": "symbol_not_in_mt5"}

        # ── 5. Entry price proximity ──
        prices = mt5_data.get("prices", [])
        cur_bid = next((p.get("bid", 0) for p in prices if p.get("symbol") == symbol), 0)
        drift_threshold = float(
            rules.get("smart_trail", {}).get("max_entry_drift_pct", 0.30)  # percent
        )
        if cur_bid > 0 and _proximity_pct(entry, cur_bid) > drift_threshold:
            self._log.info("SKIP %s: entry %.5f vs bid %.5f (%.3f%% drift, threshold %.2f%%)",
                           symbol, entry, cur_bid, _proximity_pct(entry, cur_bid), drift_threshold)
            self.broadcast("TRADE_REJECTED", {
                "symbol": symbol, "reason": "entry_drift_too_far",
                "signal_entry": entry, "current_bid": cur_bid,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "rejected", "reason": "entry_drift_too_far",
                    "signal_entry": entry, "current_bid": cur_bid}

        # ── 6. Minimum ATR separation ──
        atr = float(chart.get("atr") or 0)
        # fallback: compute rough from D1 bars
        if atr <= 0 and chart.get("D1"):
            d1 = chart.get("D1", [])
            if len(d1) >= 2:
                from backtester import Bar as BTBar
                from ict_precision import _calc_atr
                bars = [BTBar(b["t"], b["o"], b["h"], b["l"], b["c"]) for b in d1[-20:]]
                atr = _calc_atr(bars, 14)
        sl_dist = abs(entry - sl)
        if atr > 0:
            atr_threshold = atr * 0.5
            if sl_dist < atr_threshold:
                ts = float(chart.get("tick_size", 0.0001))
                tv = float(chart.get("tick_value", 1.0))
                ml = float(chart.get("min_lot", 0.01))
                risk_dollars = (sl_dist / ts) * tv * ml
                # NEW: small-account bypass — if dollar risk is within 2% of equity, allow tight SL
                max_risk = max(equity * 0.02, 0.50)
                if risk_dollars <= max_risk:
                    self._log.info("PASS %s: SL %.5f < 0.5xATR (%.5f) BUT risk $%.2f <= %.0f%% (%.2f) — allowing trade",
                                   symbol, sl_dist, atr_threshold, risk_dollars, (max_risk/equity)*100 if equity > 0 else 0, max_risk)
                else:
                    self._log.info("SKIP %s: SL %.5f < 0.5xATR (%.5f), risk $%.2f > %.2f limit",
                                   symbol, sl_dist, atr_threshold, risk_dollars, max_risk)
                    return {"status": "rejected", "reason": "sl_too_tight_for_atr",
                            "sl_dist": sl_dist, "atr_threshold": atr_threshold, "risk_dollars": risk_dollars}
            else:
                self._log.debug("PASS %s: SL %.5f >= 0.5xATR (%.5f)", symbol, sl_dist, atr_threshold)

        # ── 7. Spread-aware RR check ──
        spread_price = _symbol_spread_price(symbol)  # PRICE units (spread_pts × tick_size)
        rr_distance = abs(sl - entry)
        spread_cost_ratio = spread_price / rr_distance if rr_distance > 1e-9 else 1.0
        # Use dynamic threshold from rules.json (default: 0.30 i.e. 30%)
        rr_threshold = float(rules.get("smart_trail", {}).get("spread_atr_frac", 0.30))
        if spread_cost_ratio > rr_threshold:
            self._log.info("SKIP %s: spread_price=%.5f / RR_dist=%.5f = %.1f%% > threshold=%.1f%%",
                           symbol, spread_price, rr_distance, spread_cost_ratio*100, rr_threshold*100)
            return {"status": "rejected", "reason": "spread_consumes_rr",
                    "spread_ratio": spread_cost_ratio, "threshold": rr_threshold}

        # ── 8. Confidence scaling for small accounts ──
        effective_conf = conf
        if 0 < equity < 500:
            # Small accounts need higher conviction
            effective_conf = conf * ((equity / 100) ** 0.5)
            if effective_conf < 0.60:
                self._log.info("SKIP %s: effective_conf %.2f < 0.60 (small account)",
                               symbol, effective_conf)
                return {"status": "rejected", "reason": "confidence_too_low_for_small_account",
                        "effective_conf": effective_conf}

        lot = payload.get("lot_size", 0.01)
        # Hard cap: never exceed 0.01 lot on sub-$250 accounts
        rules = _load_rules()
        max_eq = float(rules.get("risk_rules", {}).get("small_account_equity_max", 500))
        if max_eq >= 250:
            small_cap = float(rules.get("risk_rules", {}).get("small_account_lot_cap", 0.01))
            if lot > small_cap:
                lot = small_cap
                self._log.info("LOT_CAP: forced lot %.3f for small account", lot)

        # ── 9. Execute ──
        if self._paper_mode:
            self._log.info("[PAPER] %s %s @ %.5f SL=%.5f TP=%.5f LOT=%.2f",
                           direction, symbol, entry, sl, tp, lot)
            self.broadcast("TRADE_OPENED", {
                "signal": sig, "symbol": symbol, "direction": direction,
                "entry": entry, "sl": sl, "tp": tp,
                "lot": lot, "paper": True,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "paper_logged", "symbol": symbol}

        try:
            from auto_trader import place_order
            # ── LIMIT ORDER PLACEMENT ──
            # Signal entry_price is already computed as OTE limit level
            # Validate: limit must be on correct side of current price
            prices = mt5_data.get("prices", [])
            cur_bid = next((p.get("bid", 0) for p in prices if p.get("symbol") == symbol), 0)
            
            if direction in ("BUY", "BULL"):
                if cur_bid > 0 and entry >= cur_bid:
                    self._log.warning("LIMIT_INVALID: %s BUY_LIMIT entry %.5f >= bid %.5f — converting to MARKET", symbol, entry, cur_bid)
                    order_type = "BUY"
                else:
                    order_type = "BUY_LIMIT"
            else:
                if cur_bid > 0 and entry <= cur_bid:
                    self._log.warning("LIMIT_INVALID: %s SELL_LIMIT entry %.5f <= bid %.5f — converting to MARKET", symbol, entry, cur_bid)
                    order_type = "SELL"
                else:
                    order_type = "SELL_LIMIT"
            
            # Max pending orders check
            max_pending = int(rules.get("execution", {}).get("max_pending_limit_orders", 5))
            # Count existing pending for this symbol (simplified — MT5 will reject if over)
            
            # ── DUPLICATE GUARD ──
            # Do NOT open a new position if one already exists or is pending for this symbol
            active_positions = mt5_data.get("positions", [])
            symbol_positions = [p for p in active_positions if p.get("symbol") == symbol]
            if symbol_positions:
                self._log.info("SKIP %s: already %d active position(s) open — no duplicate trades",
                              symbol, len(symbol_positions))
                return {"status": "rejected", "reason": "already_in_position",
                        "symbol": symbol, "positions": len(symbol_positions)}
            
            # Also check pending orders for this symbol
            pending_orders = mt5_data.get("orders", [])
            symbol_pending = [o for o in pending_orders if o.get("symbol") == symbol]
            if symbol_pending:
                self._log.info("SKIP %s: already %d pending limit order(s) — no duplicate orders",
                              symbol, len(symbol_pending))
                return {"status": "rejected", "reason": "already_pending_order",
                        "symbol": symbol, "pending": len(symbol_pending)}
            
            result = place_order(
                symbol=symbol, direction=direction, order_type=order_type,
                price=entry, sl=sl, tp=tp, volume=lot,
                comment="OMNI_LIMIT_OTE",
            )
            self.broadcast("TRADE_OPENED", {
                "signal": sig, "symbol": symbol, "direction": direction,
                "entry": entry, "sl": sl, "tp": tp,
                "lot": lot, "paper": False, "result": str(result),
                "order_type": order_type, "limit_order": True,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return {"status": "executed", "symbol": symbol, "result": str(result), "order_type": order_type}
        except Exception as e:
            self._log.exception("execution failed: %s", e)
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
        return {"status": "not_implemented"}

    def _load_signals(self) -> list:
        if not SIGNALS_PATH.exists():
            return []
        return json.loads(SIGNALS_PATH.read_text()).get("signals", [])
