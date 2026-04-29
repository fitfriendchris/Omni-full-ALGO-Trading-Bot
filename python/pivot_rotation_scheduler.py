"""
Pivot Rotation Scheduler

Layered timeframe rotation that fits multi-asset multi-TF pivot scanning into
the SCAN_INTERVAL budget. Three rotating layers:

    Layer 1 (every cycle):     M15, H1, D1   for ALL active symbols (entry-grade)
    Layer 2 (every 3 cycles):  W1, MN1, 3M   for high-priority symbols only (macro)
    Layer 3 (every 2 cycles):  M1, M5, M30   for ACTIVE symbols only (intraday refinement)

Why layered: scanning all 10 TFs × 40 symbols every cycle would blow the budget.
By spreading higher and lower TFs across cycles, we cover everything within ~3
cycles (≈ 9 seconds at 3s SCAN_INTERVAL) while keeping each cycle under budget.

Public API:
- PivotRotationScheduler(active_symbols_priority, hot_symbols)
- get_next_scan_batch() → {symbol: [tfs]}    (call once per scan cycle)
- mark_active(symbol)                          (mark a symbol as recently-traded)
- diagnostic() → dict
"""

from typing import Dict, List, Optional, Set
from collections import deque
import logging

log = logging.getLogger(__name__)

LAYER1_TFS = ["M15", "H1", "D1"]                 # Entry timeframes — every cycle
LAYER2_TFS = ["W1", "MN1", "3M"]                 # Macro — every 3 cycles
LAYER3_TFS = ["M1", "M5", "M30"]                 # Intraday — every 2 cycles

LAYER2_INTERVAL = 3   # cycles between Layer 2 firings
LAYER3_INTERVAL = 2   # cycles between Layer 3 firings


class PivotRotationScheduler:
    def __init__(
        self,
        active_symbols_priority: Optional[Dict[str, int]] = None,
        hot_symbols: Optional[Set[str]] = None,
        max_layer2_symbols: int = 12,
        max_layer3_symbols: int = 8,
    ):
        """
        Args:
            active_symbols_priority: from AssetRotationManager.get_active_assets_now()
            hot_symbols: symbols with currently-open trades or recent activity
            max_layer2_symbols: cap on symbols processed in Layer 2 per firing
            max_layer3_symbols: cap on symbols processed in Layer 3 per firing
        """
        self.active = dict(active_symbols_priority or {})
        self.hot = set(hot_symbols or [])
        self.max_layer2 = max_layer2_symbols
        self.max_layer3 = max_layer3_symbols
        self.cycle = 0
        # Round-robin queue for Layer 2/3 to ensure every symbol gets covered eventually
        self._l2_queue: deque = deque()
        self._l3_queue: deque = deque()

    # -- State updates ---------------------------------------------------------

    def update_active(self, active_symbols_priority: Dict[str, int]) -> None:
        """Called when AssetRotationManager output changes (session crossover)."""
        self.active = dict(active_symbols_priority)
        # Reset queues so new actives are picked up
        self._l2_queue.clear()
        self._l3_queue.clear()

    def mark_active(self, symbol: str) -> None:
        """Mark a symbol as 'hot' (has open trade or just scanned recently)."""
        self.hot.add(symbol)

    def unmark_active(self, symbol: str) -> None:
        self.hot.discard(symbol)

    # -- Core scheduler --------------------------------------------------------

    def _refill_l2_queue(self) -> None:
        # Layer 2 = high-priority (priority 3) symbols
        candidates = [s for s, p in self.active.items() if p >= 3]
        if not candidates:
            candidates = list(self.active.keys())[: self.max_layer2]
        self._l2_queue = deque(candidates)

    def _refill_l3_queue(self) -> None:
        # Layer 3 = "hot" symbols + top priority (recent trades / open positions)
        candidates = list(self.hot)
        for s, p in self.active.items():
            if s not in candidates and p >= 3:
                candidates.append(s)
            if len(candidates) >= self.max_layer3:
                break
        self._l3_queue = deque(candidates[: self.max_layer3])

    def get_next_scan_batch(self) -> Dict[str, List[str]]:
        """
        Returns a dict {symbol: [timeframes_to_scan_this_cycle]}.

        Always includes Layer 1 (M15/H1/D1) for every active symbol.
        Optionally adds Layer 2 (W1/MN1/3M) on every Nth cycle for high-pri symbols.
        Optionally adds Layer 3 (M1/M5/M30) on every Mth cycle for hot symbols.
        """
        self.cycle += 1
        batch: Dict[str, List[str]] = {sym: list(LAYER1_TFS) for sym in self.active}

        # Layer 2 — macro TFs every N cycles
        if self.cycle % LAYER2_INTERVAL == 0:
            if not self._l2_queue:
                self._refill_l2_queue()
            l2_targets: List[str] = []
            while self._l2_queue and len(l2_targets) < self.max_layer2:
                l2_targets.append(self._l2_queue.popleft())
            for sym in l2_targets:
                if sym in batch:
                    batch[sym].extend(LAYER2_TFS)

        # Layer 3 — intraday TFs every M cycles
        if self.cycle % LAYER3_INTERVAL == 0:
            if not self._l3_queue:
                self._refill_l3_queue()
            l3_targets: List[str] = []
            while self._l3_queue and len(l3_targets) < self.max_layer3:
                l3_targets.append(self._l3_queue.popleft())
            for sym in l3_targets:
                if sym in batch:
                    batch[sym].extend(LAYER3_TFS)

        # De-duplicate timeframes per symbol (just in case)
        return {sym: list(dict.fromkeys(tfs)) for sym, tfs in batch.items()}

    # -- Diagnostics -----------------------------------------------------------

    def diagnostic(self) -> Dict[str, any]:
        return {
            "cycle":              self.cycle,
            "active_count":       len(self.active),
            "hot_symbols":        sorted(self.hot),
            "layer2_queue_size":  len(self._l2_queue),
            "layer3_queue_size":  len(self._l3_queue),
            "next_layer2_cycle":  LAYER2_INTERVAL - (self.cycle % LAYER2_INTERVAL),
            "next_layer3_cycle":  LAYER3_INTERVAL - (self.cycle % LAYER3_INTERVAL),
        }
