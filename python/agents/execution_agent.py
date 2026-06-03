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


def _sync_trader_state(mt5_data: dict) -> None:
    """Sync trader_state.json with live MT5 account data every execution cycle."""
    try:
        account = mt5_data.get("account", {})
        if not account:
            return
        mt5_balance = float(account.get("balance", 0))
        mt5_equity = float(account.get("equity", 0))
        # Compute total_profit from MT5 history if available
        total_p = 0.0
        for h in mt5_data.get("history", []):
            total_p += float(h.get("profit", 0)) + float(h.get("commission", 0)) + float(h.get("swap", 0))

        tstate_path = PROJECT_ROOT / "trader_state.json"
        if tstate_path.exists():
            with open(tstate_path, "r", encoding="utf-8") as f:
                tstate = json.load(f)
        else:
            tstate = {}

        tstate["total_profit"]    = round(total_p, 2)
        tstate["equity"]          = round(mt5_equity, 2)
        tstate["account_balance"] = round(mt5_balance, 2)
        # Update peak equity if new high
        prev_peak = tstate.get("peak_equity", 0)
        if mt5_equity > prev_peak:
            tstate["peak_equity"] = round(mt5_equity, 2)

        with open(tstate_path, "w", encoding="utf-8") as f:
            json.dump(tstate, f, indent=2)
    except Exception:
        pass  # Non-critical — state will sync next cycle


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

        # ── Cancel stale pending limit orders ──
        #  Check MT5 for pending orders older than 60 min — cancel them
        try:
            mt5_data = _load_mt5_data()
            orders = mt5_data.get("orders", [])
            for o in orders:
                sym = o.get("symbol", "")
                if not sym:
                    continue
                order_type = o.get("order_type", "")
                if "LIMIT" not in order_type:
                    continue
                placed = o.get("time_setup")
                if placed:
                    try:
                        order_time = datetime.fromisoformat(placed.replace("Z", "+00:00"))
                        age_min = (datetime.now(timezone.utc) - order_time).total_seconds() / 60
                        if age_min > 60:
                            self._log.info("CANCEL stale limit %s %s @ %.5f (age %.0f min)",
                                           sym, order_type, o.get("price", 0), age_min)
                            from auto_trader import send_command
                            cancel_result = send_command(f"DELETE|{o.get('ticket', 0)}||||||")
                            self._log.info("Cancel result: %s", cancel_result)
                    except Exception:
                        pass
        except Exception:
            pass  # Non-critical cleanup

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

        # ── 3. Load live MT5 data ──
        mt5_data = _load_mt5_data()
        charts = mt5_data.get("charts", {})
        account = mt5_data.get("account", {})
        equity = float(account.get("equity", 0)) if isinstance(account, dict) else 0.0

        #── 2.5 Sync trader_state.json with live MT5 account data ──
        _sync_trader_state(mt5_data)

        # ── 2.6 Equity fallback ──
        if equity <= 0:
            try:
                tstate_path = PROJECT_ROOT / "trader_state.json"
                ts = json.loads(tstate_path.read_text()) if tstate_path.exists() else {}
                equity = float(ts.get("equity", 0) or ts.get("account", {}).get("equity", 0))
            except Exception:
                equity = 0.0
            if equity > 0:
                self._log.info("EQUITY_FALLBACK: MT5 equity read failed; using trader_state equity=%.2f", equity)

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

        # ── 4.5 Duplicate guard ──
        active_positions = mt5_data.get("positions", [])
        symbol_positions = [p for p in active_positions if p.get("symbol") == symbol]
        if symbol_positions:
            self._log.info("SKIP %s: already %d active position(s) open — no duplicate trades",
                          symbol, len(symbol_positions))
            return {"status": "rejected", "reason": "already_in_position",
                    "symbol": symbol, "positions": len(symbol_positions)}
        pending_orders = mt5_data.get("orders", [])
        symbol_pending = [o for o in pending_orders if o.get("symbol") == symbol]
        if symbol_pending:
            self._log.info("SKIP %s: already %d pending limit order(s) — no duplicate orders",
                          symbol, len(symbol_pending))
            return {"status": "rejected", "reason": "already_pending_order",
                    "symbol": symbol, "pending": len(symbol_pending)}

        # ── 4.6 Kill-zone session guard (FULL DAY: 07:00-17:00 UTC) ──
        now = datetime.now(timezone.utc)
        hour = now.hour
        # Full day coverage: London (7-10) + European (7-12) + NY (12-15) + Silver Bullet (13-17)
        in_killzone = (7 <= hour < 17)
        # Asia killzone only if confidence >=85 (sweep-reversal only)
        in_asia = (0 <= hour < 7) or (21 <= hour < 24)
        # Asia session threshold from rules.json (default 0.75 for micro accounts)
        asia_threshold = float(
            rules.get("symbol_overrides", {}).get(symbol, {})
            .get("asia_min_confidence", 0.75)
        ) / 100.0
        if in_asia and conf < asia_threshold:
            self._log.info("SKIP %s: Asia session (UTC %02d:00) with confidence %.2f < %.2f",
                           symbol, hour, conf, asia_threshold)
            return {"status": "rejected", "reason": "outside_killzone",
                    "symbol": symbol, "utc_hour": hour, "confidence": conf}
        if not in_killzone and not in_asia:
            self._log.info("SKIP %s: outside full-day killzone (UTC %02d:00, window 07-17)", symbol, hour)
            return {"status": "rejected", "reason": "outside_killzone",
                    "symbol": symbol, "utc_hour": hour}

        # ───────────────── Multi-timeframe execution guard ───────────────────
        # SELECTOR-SINGLE-SOURCE-OF-TRUTH: dual_tf_selector.py already applied
        # weekly/MTF/accumulation/cycle penalties to the confidence score.
        # Execution_agent focuses on EXECUTION-LEVEL gates only
        # (spread, drift, margin, duplicate, drawdown). Do NOT re-penalize
        # structural confidence here — doing so creates duplicate veto.

        # ── 5. Entry price proximity ──
        prices = mt5_data.get("prices", [])
        cur_bid = next((p.get("bid", 0) for p in prices if p.get("symbol") == symbol), 0)
        drift_threshold = float(
            rules.get("smart_trail", {}).get("max_entry_drift_pct", 0.50)  # percent — 0.50 for micro accounts
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
            # TIERED SL minimum: micro accounts can use tighter stops
            if equity < 200:
                atr_mult = 0.15  # sub-$200: 15% of ATR (~14 pips on XAUUSD)
            elif equity < 500:
                atr_mult = 0.25  # $200-500: 25% of ATR (~24 pips)
            else:
                atr_mult = 0.50  # $500+: 50% of ATR (~48 pips)
            atr_threshold = atr * atr_mult
            if sl_dist < atr_threshold:
                ts = float(chart.get("tick_size", 0.0001))
                tv = float(chart.get("tick_value", 1.0))
                ml = float(chart.get("min_lot", 0.01))
                risk_dollars = (sl_dist / ts) * tv * ml
                # Tiered max risk: micro accounts need realistic limits for 0.01-lot minimum
                if equity < 200:
                    max_risk_pct = 0.30  # 30% for sub-$200 (0.01 lot floor)
                elif equity < 500:
                    max_risk_pct = 0.08
                else:
                    max_risk_pct = 0.02
                max_risk = max(equity * max_risk_pct, 0.30)
                if risk_dollars <= max_risk:
                    self._log.info("PASS %s: SL %.5f < %.2fxATR (%.5f) BUT risk \$%.2f <= %.0f%% (\$%.2f) — allowing",
                                   symbol, sl_dist, atr_mult, atr_threshold, risk_dollars, max_risk_pct*100, max_risk)
                elif equity >= 200:
                    self._log.info("SKIP %s: SL %.5f < %.2fxATR (%.5f), risk \$%.2f > %.2f limit",
                                   symbol, sl_dist, atr_mult, atr_threshold, risk_dollars, max_risk)
                    return {"status": "rejected", "reason": "sl_too_tight_for_atr",
                            "sl_dist": sl_dist, "atr_threshold": atr_threshold, "risk_dollars": risk_dollars}
                else:
                    self._log.warning("WARN %s: risk \$%.2f > %.1f%% (\$%.2f) but sub-\$200 account — 0.01 lot is minimum. Allowing trade.",
                                     symbol, risk_dollars, max_risk_pct*100, max_risk)
            else:
                self._log.debug("PASS %s: SL %.5f >= %.2fxATR (%.5f)", symbol, sl_dist, atr_mult, atr_threshold)


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

        # ── 8. Kelly position sizing + compounding ──
        #  Read live performance stats from trader_state for dynamic lots
        tstate_path = PROJECT_ROOT / "trader_state.json"
        win_count = 0
        loss_count = 0
        total_win_pips = 0.0
        total_loss_pips = 0.0
        peak_equity = equity
        try:
            if tstate_path.exists():
                with open(tstate_path, "r", encoding="utf-8") as f:
                    ts = json.load(f)
                win_count = int(ts.get("winning_trades", 0))
                loss_count = int(ts.get("losing_trades", 0))
                # Approximate pip tracking from trade_memory if available
                tm_path = PROJECT_ROOT / "trade_memory.json"
                if tm_path.exists():
                    with open(tm_path, "r", encoding="utf-8") as f:
                        tm = json.load(f)
                    for t in tm.get("trades", []):
                        profit = float(t.get("profit_usd", 0))
                        lots = float(t.get("lot_size", 0.01))
                        pip_val = 1.0  # $1/pip at 1.0 lot XAUUSD approx
                        pips = profit / (lots * pip_val) if lots > 0 else 0
                        if profit > 0:
                            total_win_pips += pips
                        else:
                            total_loss_pips += abs(pips)
                peak_equity = float(ts.get("peak_equity", equity))
        except Exception:
            pass

        # 20% drawdown circuit breaker — pause trading if DD > 20%
        if peak_equity > 0:
            dd_pct = (peak_equity - equity) / peak_equity
            if dd_pct > 0.20:
                self._log.warning("HALT %s: drawdown %.1f%% > 20%% — trading paused", symbol, dd_pct * 100)
                return {"status": "rejected", "reason": "max_drawdown_circuit_breaker",
                        "drawdown_pct": round(dd_pct * 100, 1), "peak_equity": peak_equity, "equity": equity}

        # Quarter-Kelly lot sizing
        total_trades = win_count + loss_count
        if total_trades >= 5:
            win_rate = win_count / total_trades
            avg_win = total_win_pips / win_count if win_count > 0 else 50
            avg_loss = total_loss_pips / loss_count if loss_count > 0 else 50
            # Kelly fraction
            if avg_loss > 0 and avg_win > 0:
                kelly = win_rate - ((1 - win_rate) / (avg_win / avg_loss))
                kelly = max(0, min(kelly * 0.25, 0.05))  # quarter-Kelly, cap 5% risk
            else:
                kelly = 0.01
            # Convert risk % to lots: risk_dollars / (SL_pips * pip_value_per_lot)
            sl_pips = sl_dist / float(chart.get("tick_size", 0.01)) if sl_dist > 0 else 50
            pip_value_per_lot = 1.0  # $1/pip at 1.0 lot for XAUUSD
            # Micro account risk scaling: sub-$500 needs higher risk % to make 0.01 lots viable
            if equity < 200:
                kelly_risk_pct = 0.12  # 12% risk for sub-$200 (~$15 on $127)
            elif equity < 500:
                kelly_risk_pct = 0.08   # 8% risk for $200-500
            else:
                kelly_risk_pct = kelly  # Normal Kelly for $500+
            risk_dollars = equity * kelly_risk_pct
            kelly_lots = risk_dollars / (sl_pips * pip_value_per_lot)
            lot = max(0.01, min(kelly_lots, 1.0))
            self._log.info("KELLY %s: WR=%.1f%% avg_win=%.1f avg_loss=%.1f kelly=%.3f risk%%=%.1f lots=%.3f",
                           symbol, win_rate * 100, avg_win, avg_loss, kelly, kelly_risk_pct*100, lot)
        else:
            # Insufficient data — use conservative default
            lot = payload.get("lot_size", 0.01)
            self._log.info("KELLY %s: insufficient history (%d trades), using default lot %.3f",
                           symbol, total_trades, lot)

        # Hard floor: never below 0.01 lot
        # Hard cap: max 0.50 lot for accounts under $1000
        if equity < 1000:
            max_lot = 0.50
        elif equity < 5000:
            max_lot = 1.0
        else:
            max_lot = 2.0
        lot = max(0.01, min(lot, max_lot))

        # Legacy small-account override (sub-$250 → 0.01 cap)
        if equity < 250 and lot > 0.01:
            lot = 0.01
            self._log.info("LOT_CAP: sub-$250 account, forced lot 0.01")

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
            # ── ICT LIMIT ORDER PROTOCOL ────────────────────────────
            #  Chris Rule: All entries are LIMIT orders at FVG/OB level
            #  NO market executions — price must come to us after reversal
            #
            #  VALIDATION SEQUENCE:
            #    1. Sweep confirmed in signal metadata (required)
            #    2. Reversal direction matches signal direction (required)
            #    3. Pullback to FVG/OB level = limit order price (required)
            #    4. If price already past FVG = signal expired, reject
            #
            # ── 1. Extract FVG/OB level from signal ──
            entry_type = sig.get("entry_type", "")
            prices = mt5_data.get("prices", [])
            cur_bid = next((p.get("bid", 0) for p in prices if p.get("symbol") == symbol), 0)

            # ── 2. Sweep + Reversal validation ──
            # Metadata structure: metadata.confluence.manipulation_leg  OR  metadata.confluence (flat)
            meta = sig.get("metadata", {})
            confluence = meta.get("confluence", {})

            # Try nested structure first (newer signals)
            manip_leg = confluence.get("manipulation_leg", {})
            sweep_type = manip_leg.get("type", "")
            sweep_dir  = manip_leg.get("direction", "")

            # Fall back to flat structure (legacy/current signals: JUDAS_LOW, JUDAS_HIGH)
            if not sweep_type:
                sweep_type = confluence.get("type", "")
                sweep_dir  = confluence.get("direction", "")

            # Accept both formal sweep types and Judas swing labels
            valid_sweep_types = ("LIQUIDITY_SWEEP", "JUDAS_LOW", "JUDAS_HIGH",
                                 "STOP_RUN", "LIQUIDITY_GRAB", "MANIPULATION_SWEEP")
            if not sweep_type or sweep_type not in valid_sweep_types:
                self._log.info("SKIP %s: no liquidity sweep in signal — not a reversal setup (type=%s)", symbol, sweep_type)
                return {"status": "rejected", "reason": "no_sweep_confirmed", "signal": sig, "sweep_type": sweep_type}
            if sweep_dir != direction:
                self._log.info("SKIP %s: sweep direction %s != trade direction %s — mismatch", symbol, sweep_dir, direction)
                return {"status": "rejected", "reason": "sweep_direction_mismatch", "sweep_dir": sweep_dir, "trade_dir": direction}

            # ── 3. FVG/OB level extraction ──
            #  Try signal entry_price first (already computed as OTE/FVG level)
            limit_price = entry
            #  FVG expiry is handled by signal expires_at; price pulling into the
            #  FVG zone is the desired state for a limit order. Skip stale-mitigation
            #  check here — the signal agent already validated structural recency.

            # ── 4. Determine order type ──
            if direction in ("BUY", "BULL"):
                order_type = "BUY_LIMIT"
            else:
                order_type = "SELL_LIMIT"

            # ── 4.5 Limit order validity check ──
            cur_ask = next((p.get("ask", 0) for p in prices if p.get("symbol") == symbol), 0)
            if order_type == "BUY_LIMIT" and cur_ask > 0 and cur_ask <= limit_price:
                self._log.info("LIMIT_INVALID %s: BUY_LIMIT %.5f must be < ask %.5f — price already inside FVG", symbol, limit_price, cur_ask)
                return {"status": "rejected", "reason": "limit_price_invalid", "limit": limit_price, "ask": cur_ask}
            if order_type == "SELL_LIMIT" and cur_bid > 0 and cur_bid >= limit_price:
                self._log.info("LIMIT_INVALID %s: SELL_LIMIT %.5f must be > bid %.5f — price already inside FVG", symbol, limit_price, cur_bid)
                return {"status": "rejected", "reason": "limit_price_invalid", "limit": limit_price, "bid": cur_bid}

            # ── 5. Limit order expiration ──
            #  Limit orders expire when the signal expires (default 60 min)
            expires = sig.get("expires_at")
            # EA supports expiration via additional parameter — we'll pass it as comment metadata
            # If no expiration, default 60 minutes
            if not expires:
                from datetime import timedelta
                expires_dt = datetime.now(timezone.utc) + timedelta(minutes=60)
                expires = expires_dt.isoformat()

            # ── 6. Place limit order ──
            comment = f"OMNI_LIM_{entry_type}_{expires[11:16]}"
            result = place_order(
                symbol=symbol, direction=direction, order_type=order_type,
                price=limit_price, sl=sl, tp=tp, volume=lot,
                comment=comment,
            )
            self.broadcast("TRADE_OPENED", {
                "signal": sig, "symbol": symbol, "direction": direction,
                "entry": limit_price, "sl": sl, "tp": tp,
                "lot": lot, "paper": False, "result": str(result),
                "order_type": order_type, "limit_order": True,
                "entry_type": entry_type,
                "expires": expires,
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
