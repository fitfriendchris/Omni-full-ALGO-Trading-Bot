"""
Timezone-Aware Asset Rotation Manager

Decides which symbols are "in season" right now based on the active trading
session (London, NY, Asia, or after-hours). Returns priority-weighted lists
that the scanner uses to focus on assets where liquidity is currently active.

Key insight: scanning EURUSD at 03:00 UTC (Asian session) is mostly noise —
that pair's institutional flow is sleeping. But XAUUSD and USDJPY are very
active. Rotating attention by session improves signal quality and saves
scan budget.

Session times (UTC, FX convention — 22:00 UTC = "new day"):
    Sydney/Asia:   22:00 → 07:00
    London:        07:00 → 17:00
    NY:            13:00 → 21:00
    Overlap (HOT): 13:00 → 17:00 (London + NY)

Public API:
- AssetRotationManager(symbols=None, schedule=None)
- get_active_assets_now() → {symbol: priority}     (priority 1=low, 3=high)
- should_skip_asset(symbol, state=None) → bool
- get_session_now() → str   ("LONDON", "NY", "OVERLAP", "ASIA", "AFTER_HOURS")
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
import logging

log = logging.getLogger(__name__)

# Default symbol → asset-class mapping
ASSET_CLASS = {
    # Forex majors
    "EURUSD": "FOREX_MAJOR", "GBPUSD": "FOREX_MAJOR", "USDJPY": "FOREX_MAJOR",
    "USDCHF": "FOREX_MAJOR", "AUDUSD": "FOREX_MAJOR", "NZDUSD": "FOREX_MAJOR",
    "USDCAD": "FOREX_MAJOR",
    # Forex crosses
    "EURJPY": "FOREX_CROSS", "GBPJPY": "FOREX_CROSS", "EURGBP": "FOREX_CROSS",
    "AUDJPY": "FOREX_CROSS", "EURAUD": "FOREX_CROSS",
    # Metals
    "XAUUSD": "METAL", "XAGUSD": "METAL",
    # Indices
    "US30": "INDEX_US", "US500": "INDEX_US", "NAS100": "INDEX_US",
    "GER40": "INDEX_EU", "UK100": "INDEX_EU",
    "JPN225": "INDEX_ASIA",
    # Crypto
    "BTCUSD": "CRYPTO", "ETHUSD": "CRYPTO", "XRPUSD": "CRYPTO", "LTCUSD": "CRYPTO",
    "SOLUSD": "CRYPTO", "DOGEUSD": "CRYPTO", "BNBUSD": "CRYPTO", "ADAUSD": "CRYPTO",
    # Stocks
    "AAPL": "STOCK_US", "MSFT": "STOCK_US", "GOOGL": "STOCK_US", "AMZN": "STOCK_US",
    "TSLA": "STOCK_US", "NVDA": "STOCK_US", "META": "STOCK_US",
}

# Session priority for each asset class:
# Priority: 3 = scan eagerly, 2 = scan if budget allows, 1 = low priority,
#           0 = skip entirely (out of session)
PRIORITY_BY_SESSION_AND_CLASS = {
    "ASIA": {
        "FOREX_MAJOR": 1, "FOREX_CROSS": 2,           # USDJPY/AUDJPY active
        "METAL": 1, "INDEX_US": 0, "INDEX_EU": 0,
        "INDEX_ASIA": 3, "CRYPTO": 3, "STOCK_US": 0,
    },
    "LONDON": {
        "FOREX_MAJOR": 3, "FOREX_CROSS": 3,
        "METAL": 3, "INDEX_US": 1, "INDEX_EU": 3,
        "INDEX_ASIA": 0, "CRYPTO": 2, "STOCK_US": 0,
    },
    "OVERLAP": {                                      # London + NY = highest activity
        "FOREX_MAJOR": 3, "FOREX_CROSS": 3,
        "METAL": 3, "INDEX_US": 3, "INDEX_EU": 3,
        "INDEX_ASIA": 0, "CRYPTO": 3, "STOCK_US": 3,
    },
    "NY": {
        "FOREX_MAJOR": 3, "FOREX_CROSS": 2,
        "METAL": 3, "INDEX_US": 3, "INDEX_EU": 1,
        "INDEX_ASIA": 0, "CRYPTO": 3, "STOCK_US": 3,
    },
    "AFTER_HOURS": {                                  # 21:00-22:00 UTC
        "FOREX_MAJOR": 1, "FOREX_CROSS": 1,
        "METAL": 1, "INDEX_US": 0, "INDEX_EU": 0,
        "INDEX_ASIA": 1, "CRYPTO": 3, "STOCK_US": 0,
    },
}


class AssetRotationManager:
    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        asset_class_overrides: Optional[Dict[str, str]] = None,
    ):
        self.symbols = symbols or list(ASSET_CLASS.keys())
        self.asset_class = dict(ASSET_CLASS)
        if asset_class_overrides:
            self.asset_class.update(asset_class_overrides)

    @staticmethod
    def get_session_now(now: Optional[datetime] = None) -> str:
        """Return the active trading session at `now` (or current time if None)."""
        if now is None:
            now = datetime.now(timezone.utc)
        h = now.hour
        # Overlap: both London (07-17) AND NY (13-21) are open → 13-17 UTC
        if 13 <= h < 17:
            return "OVERLAP"
        if 7 <= h < 13:
            return "LONDON"
        if 17 <= h < 21:
            return "NY"
        if 21 <= h < 22:
            return "AFTER_HOURS"
        # 22:00 → 07:00 next day
        return "ASIA"

    def get_priority_for(self, symbol: str, session: Optional[str] = None) -> int:
        if session is None:
            session = self.get_session_now()
        cls = self.asset_class.get(symbol, "FOREX_MAJOR")  # safe default
        table = PRIORITY_BY_SESSION_AND_CLASS.get(session, {})
        return int(table.get(cls, 1))

    def get_active_assets_now(self, min_priority: int = 1) -> Dict[str, int]:
        """
        Returns {symbol: priority} for symbols whose priority ≥ min_priority.
        Sorted (caller can iterate in priority order).
        """
        session = self.get_session_now()
        out: Dict[str, int] = {}
        for sym in self.symbols:
            pri = self.get_priority_for(sym, session)
            if pri >= min_priority:
                out[sym] = pri
        # Sort dict by priority desc (highest first)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def should_skip_asset(
        self,
        symbol: str,
        recently_traded: Optional[Set[str]] = None,
        sym_loss_streak: Optional[Dict[str, int]] = None,
        max_loss_streak: int = 2,
    ) -> bool:
        """
        Skip rules:
        - Out of session (priority == 0)
        - In recently_traded set (avoid re-entry on same bar)
        - Per-symbol consecutive loss streak hit limit
        """
        if self.get_priority_for(symbol) == 0:
            return True
        if recently_traded and symbol in recently_traded:
            return True
        if sym_loss_streak and sym_loss_streak.get(symbol, 0) >= max_loss_streak:
            return True
        return False

    def diagnostic(self) -> Dict[str, any]:
        session = self.get_session_now()
        active = self.get_active_assets_now()
        return {
            "session":           session,
            "active_count":      len(active),
            "high_priority":     [s for s, p in active.items() if p == 3],
            "medium_priority":   [s for s, p in active.items() if p == 2],
            "low_priority":      [s for s, p in active.items() if p == 1],
            "skipped":           [s for s in self.symbols
                                  if self.get_priority_for(s, session) == 0],
        }
