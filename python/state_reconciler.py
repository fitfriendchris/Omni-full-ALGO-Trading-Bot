#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_reconciler.py — OMNI ICT Production Bot v28.0
Phase 5A: Compare MT5 positions (from omni_data.json or GET_POSITIONS
command result) against Python's expected state (trader_state.json).
Auto-pause on desync. Requires manual /resume after review.
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionKey:
    ticket: int
    symbol: str


class StateReconciler:
    """
    MT5 vs Python state reconciliation.
    
    Expected to run every 60 seconds from watchdog or monitor agent.
    
    Desync types:
      ORPHAN: MT5 position exists, Python doesn't know about it
      GHOST: Python thinks position exists, MT5 does not
      SIDE_MISMATCH: Same ticket, different BUY/SELL
      SIZE_MISMATCH: Same ticket, different volume
    """
    
    DESYNC_LOG_FILE = "desync_log.jsonl"
    
    def __init__(self, omni_dir: str = None, state_file: str = None, alert_fn=None):
        """
        Args:
            omni_dir: Directory containing omni_data.json and omni_result.txt
            state_file: Path to trader_state.json
            alert_fn: Callable(str, severity) for critical alerts
        """
        if omni_dir is None:
            omni_dir = os.path.expanduser("~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files")
        if state_file is None:
            state_file = os.path.expanduser("~/Omni-full-ALGO-Trading-Bot/python/trader_state.json")
        self.omni_dir = omni_dir
        self.state_file = state_file
        self.alert_fn = alert_fn
        self.magic = 20250411
        self.last_run: Optional[datetime] = None
        self.desync_count_24h = 0
    
    def run(self) -> Dict:
        """Perform full reconciliation. Returns summary dict."""
        now = datetime.now(timezone.utc)
        self.last_run = now
        
        # Read MT5 positions
        mt5_positions = self._read_mt5_positions()
        
        # Read Python expected state
        py_positions = self._read_python_state()
        
        # Compare
        orphans, ghosts, side_mismatches, size_mismatches = self._diff(mt5_positions, py_positions)
        
        report = {
            "time": now.isoformat(),
            "mt5_count": len(mt5_positions),
            "py_count": len(py_positions),
            "orphans": [self._pos_to_dict(p) for p in orphans],
            "ghosts": [self._pos_to_dict(p) for p in ghosts],
            "side_mismatches": [self._pos_to_dict(p) for p in side_mismatches],
            "size_mismatches": [self._pos_to_dict(p) for p in size_mismatches],
            "desync_detected": bool(orphans or ghosts or side_mismatches or size_mismatches),
            "paused": False,
        }
        
        # Action on desync
        if report["desync_detected"]:
            self.desync_count_24h += 1
            self._log_desync(report)
            
            severity = "WARNING"
            if side_mismatches:
                severity = "CRITICAL"
                self._alert("STATE DESYNC: Side mismatch detected! HALTING NEW ENTRIES. Review immediately.", severity)
                report["paused"] = True
                self._write_pause_flag()
            elif self.desync_count_24h >= 3:
                severity = "CRITICAL"
                self._alert(f"STATE DESYNC: {self.desync_count_24h} desyncs in 24h. Auto-paused.", severity)
                report["paused"] = True
                self._write_pause_flag()
            else:
                self._alert(f"STATE DESYNC: {len(orphans)} orphans, {len(ghosts)} ghosts. Auto-resolving.", severity)
                # Auto-resolve ghosts by removing from Python state
                for g in ghosts:
                    self._remove_ghost_from_state(g)
        else:
            self.desync_count_24h = max(0, self.desync_count_24h - 1)
        
        return report
    
    # ── Readers ────────────────────────────────────────────────────
    def _read_mt5_positions(self) -> Dict[int, Dict]:
        """Read omni_data.json for live positions."""
        positions = {}
        try:
            data_path = Path(self.omni_dir) / "omni_data.json"
            if not data_path.exists():
                return positions
            with open(data_path) as f:
                data = json.load(f)
            for p in data.get("positions", []):
                tid = p.get("ticket")
                if tid:
                    positions[tid] = {
                        "ticket": tid,
                        "symbol": p.get("symbol", ""),
                        "type": p.get("type", ""),
                        "volume": p.get("volume", 0.0),
                        "open_price": p.get("open_price", 0.0),
                        "sl": p.get("sl", 0.0),
                        "tp": p.get("tp", 0.0),
                        "magic": p.get("magic", 0),
                    }
        except Exception as e:
            logger.error(f"Read MT5 positions failed: {e}")
        return positions
    
    def _read_python_state(self) -> Dict[int, Dict]:
        """Read trader_state.json for expected active_trades."""
        positions = {}
        try:
            if not Path(self.state_file).exists():
                return positions
            with open(self.state_file) as f:
                state = json.load(f)
            for t in state.get("active_trades", []):
                tid = t.get("ticket")
                if tid:
                    positions[tid] = {
                        "ticket": tid,
                        "symbol": t.get("symbol", ""),
                        "type": t.get("type", ""),
                        "volume": t.get("volume", 0.0),
                        "open_price": t.get("entry_price", 0.0),
                        "sl": t.get("sl", 0.0),
                        "tp": t.get("tp", 0.0),
                        "magic": t.get("magic", self.magic),
                    }
        except Exception as e:
            logger.error(f"Read Python state failed: {e}")
        return positions
    
    # ── Diff engine ────────────────────────────────────────────────
    def _diff(self, mt5: Dict[int, Dict], py: Dict[int, Dict]) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
        orphans = []      # MT5 has, Python does not
        ghosts = []       # Python has, MT5 does not
        side_mm = []      # Both have but side differs
        size_mm = []      # Both have but volume differs
        
        mt5_tickets = set(mt5.keys())
        py_tickets = set(py.keys())
        
        # Orphans
        for tid in mt5_tickets - py_tickets:
            orphans.append(mt5[tid])
        
        # Ghosts
        for tid in py_tickets - mt5_tickets:
            ghosts.append(py[tid])
        
        # Shared: check side and size
        for tid in mt5_tickets & py_tickets:
            m = mt5[tid]
            p = py[tid]
            if m.get("type", "").upper() != p.get("type", "").upper():
                side_mm.append({"ticket": tid, "mt5_type": m["type"], "py_type": p["type"]})
            elif abs(m.get("volume", 0) - p.get("volume", 0)) > 0.001:
                size_mm.append({"ticket": tid, "mt5_vol": m["volume"], "py_vol": p["volume"]})
        
        return orphans, ghosts, side_mm, size_mm
    
    # ── Actions ─────────────────────────────────────────────────────
    def _log_desync(self, report: Dict) -> None:
        log_path = Path(self.state_file).parent / self.DESYNC_LOG_FILE
        with open(log_path, "a") as f:
            f.write(json.dumps(report) + "\n")
    
    def _alert(self, msg: str, severity: str) -> None:
        logger.warning(f"[{severity}] {msg}")
        if self.alert_fn:
            self.alert_fn(msg, severity)
    
    def _write_pause_flag(self) -> None:
        flag = Path(self.state_file).parent / "OMNI_PAUSED_DESYNC"
        flag.write_text(datetime.now(timezone.utc).isoformat())
    
    def _remove_ghost_from_state(self, ghost: Dict) -> None:
        """Remove a ghost ticket from trader_state.json."""
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            state["active_trades"] = [t for t in state.get("active_trades", []) if t.get("ticket") != ghost.get("ticket")]
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
            logger.info(f"Removed ghost ticket {ghost.get('ticket')} from state")
        except Exception as e:
            logger.error(f"Ghost removal failed: {e}")
    
    @staticmethod
    def _pos_to_dict(p: Dict) -> Dict:
        return dict(p)


if __name__ == "__main__":
    # Test
    reconciler = StateReconciler(alert_fn=lambda msg, sev: print(f"ALERT [{sev}]: {msg}"))
    report = reconciler.run()
    print(json.dumps(report, indent=2))
