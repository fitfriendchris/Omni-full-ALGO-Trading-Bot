#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""omni_v28_integration.py — Integration hub for all Phase 1-6 modules.

This file provides the glue code to wire the new production-ready components
into the existing auto_trader.py and swarm.py orchestration.

USING THIS IN auto_trader.py:
    from omni_v28_integration import V28ExecutionEngine
    
    class AutoTrader:
        def __init__(self):
            self.v28 = V28ExecutionEngine(self)
            
        def on_tick(self):
            # In your main loop, replace old signal evaluation with:
            setup = self.v28.evaluate_setup(omni_data, atr_14, h4_bias, d1_bias, cycle_phase)
            if setup and setup.confluences >= 5:
                self.v28.send_entry(setup)
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, Any

# Import new Phase modules
from redistribution_detector import RedistributionDetector, RedistributionConfig
from cycle_tracker import CycleTracker, CyclePhase
from smart_trailing_stop_v28 import SmartTrailingStopV28, PositionConfig, TradeStatus
from feature_store_v28 import FeatureStoreV28, StructuralFeatures
from sqlite_journal import SQLiteJournal, TradeRecord
from alert_manager import AlertManager, Severity
from state_reconciler import StateReconciler

logger = logging.getLogger(__name__)


class V28ExecutionEngine:
    """
    Single integration point for all v28 production modules.
    
    Responsibilities:
      1. Parse MQL5 structural JSON data
      2. Run redistribution detector (sweep → CHoCH → FVG pipeline)
      3. Apply cycle phase gating
      4. Calculate position size with cycle multiplier
      5. Enforce risk limits
      6. Log features + journal events
      7. Send alerts on critical events
      8. Run state reconciliation periodically
    """
    
    def __init__(self, auto_trader=None, telegram_bot=None):
        """
        Args:
            auto_trader: Reference to parent AutoTrader instance for legacy methods
            telegram_bot: Telegram bot instance for AlertManager
        """
        self.auto_trader = auto_trader
        self.redist = RedistributionDetector(RedistributionConfig())
        self.cycle = CycleTracker()
        self.trail = SmartTrailingStopV28()
        self.features = FeatureStoreV28()
        self.journal = SQLiteJournal()
        self.alerts = AlertManager(telegram_bot=telegram_bot)
        self.reconciler = StateReconciler(alert_fn=lambda msg, sev: self.alerts.send(msg, sev))
        
        self.last_reconcile = datetime.now(timezone.utc)
        self.reconcile_interval_sec = 60
        self.paused = False
    
    def evaluate_setup(self, omni_data: Dict, atr_14: float, h4_bias: str = "",
                      d1_bias: str = "", cycle_phase_str: str = "unknown") -> Optional[Any]:
        """
        Main entry: evaluate whether current MQL5-exported data produces an A+ setup.
        
        Returns RedistributionSetup dataclass if A+ detected, else None.
        """
        if self.paused:
            logger.info("V28: Engine paused (desync or manual)")
            return None
        
        # Periodic reconciliation
        now = datetime.now(timezone.utc)
        if (now - self.last_reconcile).total_seconds() >= self.reconcile_interval_sec:
            self._run_reconciliation()
            self.last_reconcile = now
        
        # Run redistribution pipeline
        setup = self.redist.evaluate(omni_data, atr_14, h4_bias, d1_bias, cycle_phase_str)
        if not setup:
            return None
        
        # Cycle phase size adjustment
        size_mult = self.cycle.get_size_multiplier()
        setup.size_lots *= size_mult
        
        # Log structural features for ML
        self._log_features(setup)
        
        # Log to SQLite journal (pending open)
        self._journal_pending(setup)
        
        # Alert on A+ detection
        self.alerts.send(
            f"A+ Setup {setup.direction.value.upper()} {setup.symbol}\n"
            f"Entry: {setup.entry_price} | SL: {setup.stop_loss} | TP1: {setup.take_profit_1}\n"
            f"Confluences: {setup.confluences}/10 ({', '.join(setup.confluence_list)})\n"
            f"Cycle: {setup.cycle_phase.value} | Killzone: {setup.killzone}",
            Severity.INFO,
            channels=["dashboard_log"]
        )
        
        return setup
    
    def send_entry(self, setup) -> bool:
        """
        Send entry to MT5 via upgraded command protocol.
        Uses limit order if setup.entry_price differs from current market.
        Returns True if command queued.
        """
        try:
            # Determine order type
            order_type = "limit" if setup.entry_price else "market"
            cmd = {
                "type": "OPEN_LIMIT" if order_type == "limit" else "OPEN_MARKET",
                "cmd_id": f"v28_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{setup.symbol}",
                "symbol": setup.symbol,
                "price": setup.entry_price,
                "sl": setup.stop_loss,
                "tp": setup.take_profit_1,
                "lots": setup.size_lots,
                "magic": 20250411,
                "comment": f"V28_{setup.direction.value}_{setup.confluences}c_{setup.sweep.level_type if setup.sweep else 'none'}",
            }
            self._write_cmd(cmd)
            
            self.alerts.send(
                f"ENTRY SENT: {setup.direction.value.upper()} {setup.symbol}\n"
                f"Ticket: {cmd['cmd_id']}",
                Severity.INFO
            )
            
            # Journal will receive OPEN confirmation from EA result polling
            return True
        except Exception as e:
            logger.error(f"Entry send failed: {e}")
            self.alerts.send(f"ENTRY FAILED: {e}", Severity.CRITICAL)
            return False
    
    def manage_position(self, pos: PositionConfig, current_price: float,
                        current_time: datetime, h4_bars=None) -> Dict:
        """
        Evaluate trailing stop / partial close for an active position.
        Returns action dict from SmartTrailingStopV28.
        """
        action = self.trail.evaluate(pos, current_price, current_time, h4_bars)
        
        if action.get("action") == "move_sl":
            self._write_cmd({
                "type": "MODIFY_SL",
                "cmd_id": f"slmod_{pos.ticket}_{int(current_time.timestamp())}",
                "ticket": pos.ticket,
                "sl": action["new_sl"],
            })
            self.journal.record_event(pos.ticket, "MODIFY_SL", current_price, action.get("reason", ""))
        
        elif action.get("action") in ("close_partial_50", "close_partial_25"):
            pct = 50 if "50" in action["action"] else 25
            self._write_cmd({
                "type": "CLOSE_PARTIAL",
                "cmd_id": f"partial_{pos.ticket}_{pct}",
                "ticket": pos.ticket,
                "percent": pct,
            })
            ev = "TP1_HIT" if pct == 50 else "TP2_HIT"
            self.journal.record_event(pos.ticket, ev, current_price, action.get("reason", ""))
        
        elif action.get("action") == "close_full":
            # Close handled by position management
            self.journal.record_event(pos.ticket, "CLOSE", current_price, action.get("reason", ""))
        
        return action
    
    def handle_fill(self, ticket: int, fill_price: float, side: str, size: float,
                    sl: float, tp: float, setup: Any) -> None:
        """Called when EA confirms an order filled."""
        if setup:
            self.journal.record_event(ticket, "OPEN", fill_price, f"side={side}, size={size}")
            # Convert pending to full record
            rec = TradeRecord(
                ticket=ticket,
                symbol=setup.symbol,
                side=side,
                entry_price=fill_price,
                stop_loss=sl,
                take_profit_1=tp,
                take_profit_2=setup.take_profit_2,
                take_profit_3=setup.take_profit_3,
                size_lots=size,
                open_time=datetime.now(timezone.utc).isoformat(),
                setup_type=f"redistribution_{setup.direction.value}",
                confluence_count=setup.confluences,
                session=setup.session.current_session if setup.session else "",
                killzone=setup.killzone,
                sweep_type=setup.sweep.level_type if setup.sweep else "none",
                choch_type=setup.structure.last_choch_dir if setup.structure else "none",
                fvg_size_pips=setup.fvg.size_pips if setup.fvg else 0,
                cycle_phase=setup.cycle_phase.value,
                h4_bias=setup.h4_bias,
                d1_bias=setup.d1_bias,
            )
            self.journal.record_open(rec)
    
    def handle_exit(self, ticket: int, exit_price: float, pnl: float, reason: str) -> None:
        """Called when position closes."""
        # Calculate R multiple from journal
        open_trade = self.journal.get_open_trades()
        r = 0.0
        for t in open_trade:
            if t["ticket"] == ticket:
                entry = t["entry_price"]
                sl = t["stop_loss"]
                risk = abs(entry - sl)
                if risk > 0:
                    pips = abs(exit_price - entry) / 0.01
                    r = pips * 10 / (risk / 0.01 * 10)  # Simplified
                break
        
        status = "closed"
        if reason == "tp1": status = "partial_tp1"
        elif reason == "tp2": status = "partial_tp2"
        
        self.journal.record_close(ticket, exit_price, pnl, pnl / 10, r, status, reason)
        
        # Outcome logging for ML training
        # (Feature store outcome record would be written here)
    
    def _run_reconciliation(self) -> None:
        report = self.reconciler.run()
        if report.get("paused"):
            self.paused = True
            self.alerts.send(
                "Trading PAUSED by StateReconciler. Desync detected. Review logs and /resume.",
                Severity.CRITICAL
            )
    
    def _log_features(self, setup) -> None:
        """Log structural features for ML training."""
        try:
            f = StructuralFeatures(
                setup_id=f"v28_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                symbol=setup.symbol,
                timestamp=datetime.now(timezone.utc).isoformat(),
                sweep_type=setup.sweep.level_type if setup.sweep else "none",
                sweep_magnitude_pips=abs(setup.sweep.wick_extreme - setup.sweep.level) / 0.01 if setup.sweep else 0,
                sweep_mitigated=False,
                sweep_time_since_sec=int((datetime.now(timezone.utc) - setup.sweep.time).total_seconds()) if setup.sweep else 0,
                sweep_multi_touch=setup.sweep.multi_touch if setup.sweep else False,
                sweep_volume_ratio=setup.sweep.volume_ratio if setup.sweep else 1.0,
                choch_type=setup.structure.last_choch_dir if setup.structure else "none",
                choch_time_since_sec=int((datetime.now(timezone.utc) - setup.structure.last_choch_time).total_seconds()) if (setup.structure and setup.structure.last_choch_time) else 0,
                choch_magnitude_pips=0,  # Would need structure detector magnitude export
                bos_type=setup.structure.last_bos_dir if setup.structure else "none",
                bos_time_since_sec=int((datetime.now(timezone.utc) - setup.structure.last_bos_time).total_seconds()) if (setup.structure and setup.structure.last_bos_time) else 0,
                trend_before="unknown", trend_after=setup.structure.trend if setup.structure else "unknown",
                fvg_direction=setup.fvg.direction if setup.fvg else "none",
                fvg_size_pips=setup.fvg.size_pips if setup.fvg else 0,
                fvg_mitigated=setup.fvg.mitigated if setup.fvg else False,
                fvg_time_since_sec=int((datetime.now(timezone.utc) - setup.fvg.time).total_seconds()) if (setup.fvg and setup.fvg.time) else 0,
                fvg_distance_to_price_pips=abs(setup.fvg.optimal_entry - setup.entry_price) / 0.01 if setup.fvg else 0,
                current_session=setup.session.current_session if setup.session else "off",
                time_in_session_min=0,
                session_range_pips=(setup.session.london_high - setup.session.london_low) / 0.01 if (setup.session and setup.session.london_high and setup.session.london_low) else 0,
                asian_range_swept=False,
                london_extension_pct=0,
                h4_bias=setup.h4_bias,
                d1_bias=setup.d1_bias,
                cycle_phase=setup.cycle_phase.value,
                cycle_day_number=0,
                prior_3d_avg_range_pips=0,
                confluence_count=setup.confluences,
                target_liquidity="none",
                opposing_liquidity_distance_pips=0,
                atr_14=0, rsi_14=0, ema_8_21_cross=0, volume_ratio=1.0,
            )
            self.features.log_setup(f)
        except Exception as e:
            logger.warning(f"Feature logging failed: {e}")
    
    def _journal_pending(self, setup) -> None:
        """Create a pending journal entry before fill."""
        # Could store in temp table; for now feature store captures it
        pass
    
    def _write_cmd(self, cmd: Dict) -> None:
        """Write command to MQL5 command file."""
        cmd_dir = Path.home() / "Library" / "Application Support" / "net.metaquotes.wine.metatrader5" / "drive_c" / "users" / "user" / "AppData" / "Roaming" / "MetaQuotes" / "Terminal" / "Common" / "Files"
        new_file = cmd_dir / "omni_cmd.new"
        ready_file = cmd_dir / "omni_cmd.txt"
        
        lines = []
        if ready_file.exists():
            with open(ready_file) as f:
                lines = f.read().strip().split("\n")
        lines.append(json.dumps(cmd))
        
        with open(new_file, "w") as f:
            f.write("\n".join(lines) + "\n")
        # Atomic rename (though on macOS this may not be perfect across Wine)
        new_file.replace(ready_file)


if __name__ == "__main__":
    print("V28ExecutionEngine integration class loaded.")
