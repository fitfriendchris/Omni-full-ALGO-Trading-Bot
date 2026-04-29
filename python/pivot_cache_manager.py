"""
Pivot Point Cache Manager

Handles persistent storage and in-memory caching of pivot levels.
- Versioned by (symbol, timeframe, bar_time) for integrity
- TTL-based expiry (recalculate if bar closed or >5 min elapsed for M1 TFs)
- Load/save from pivot_levels_cache.json
"""

import json
import time
import logging
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from pivot_engine import PivotLevel, PivotType

log = logging.getLogger(__name__)

CACHE_VERSION = "1.0"
CACHE_FILE = "pivot_levels_cache.json"
DEFAULT_TTL_SECONDS = {
    "M1": 300,   # 5 minutes for M1
    "M5": 600,   # 10 minutes for M5
    "M15": 900,  # 15 minutes for M15
    "M30": 1800,  # 30 minutes
    "H1": 3600,   # 1 hour
    "H4": 14400,  # 4 hours
    "D1": 86400,  # 1 day
    "W1": 604800,  # 1 week
    "3M": 2592000,  # 30 days (approx)
    "6M": 5184000,  # 60 days (approx)
}


@dataclass
class CacheEntry:
    """In-memory cache entry with TTL metadata."""
    symbol: str
    timeframe: str
    bar_time: float
    pivots: List[PivotLevel]
    cached_at: float
    ttl_seconds: float


class PivotCacheManager:
    """
    Manages pivot level caching with disk persistence and TTL.
    """

    def __init__(self, cache_dir: Path = None):
        """
        Initialize cache manager.

        Args:
            cache_dir: Directory for cache file (defaults to current dir)
        """
        self.cache_dir = cache_dir or Path.cwd()
        self.cache_file = self.cache_dir / CACHE_FILE
        self.memory_cache: Dict[str, CacheEntry] = {}
        self.disk_cache: Dict = {}
        self.load_disk_cache()

    def load_disk_cache(self) -> bool:
        """
        Load pivot levels from disk if cache file exists.

        Returns:
            True if loaded successfully, False if file doesn't exist or corrupt
        """
        try:
            if not self.cache_file.exists():
                log.debug(f"Cache file not found: {self.cache_file}")
                return False

            with open(self.cache_file, 'r') as f:
                data = json.load(f)

            # Validate version
            if data.get("cache_version") != CACHE_VERSION:
                log.warning(
                    f"Cache version mismatch: {data.get('cache_version')} != {CACHE_VERSION}. "
                    "Clearing cache."
                )
                return False

            self.disk_cache = data.get("symbols", {})
            log.info(f"Loaded cache with {len(self.disk_cache)} symbols")
            return True

        except Exception as e:
            log.error(f"Error loading disk cache: {e}")
            self.disk_cache = {}
            return False

    def save_disk_cache(self) -> bool:
        """
        Save current in-memory cache to disk.

        Returns:
            True if saved successfully
        """
        try:
            cache_data = {
                "cache_version": CACHE_VERSION,
                "last_update": datetime.now(timezone.utc).isoformat(),
                "symbols": self._serialize_cache(),
            }

            with open(self.cache_file, 'w') as f:
                json.dump(cache_data, f, indent=2)

            log.info(f"Saved cache to {self.cache_file}")
            return True

        except Exception as e:
            log.error(f"Error saving disk cache: {e}")
            return False

    def _serialize_cache(self) -> Dict:
        """Convert in-memory cache to serializable dict for disk storage."""
        serialized = {}

        for key, entry in self.memory_cache.items():
            symbol = entry.symbol
            tf = entry.timeframe

            if symbol not in serialized:
                serialized[symbol] = {}

            if tf not in serialized[symbol]:
                serialized[symbol][tf] = {
                    "bar_time": entry.bar_time,
                    "cached_at": entry.cached_at,
                    "pivots": {},
                }

            # Group pivots by type
            for pivot in entry.pivots:
                ptype = pivot.pivot_type
                if ptype not in serialized[symbol][tf]["pivots"]:
                    serialized[symbol][tf]["pivots"][ptype] = []

                # Convert PivotLevel to dict, excluding dataclass fields that can't serialize
                pivot_dict = {
                    "level": pivot.level,
                    "level_type": pivot.level_type,
                    "strength": pivot.strength,
                    "touches": pivot.touches,
                    "last_touch_idx": pivot.last_touch_idx,
                    "distance_pips": pivot.distance_pips,
                    "reversal_probability": pivot.reversal_probability,
                    "confluence_count": pivot.confluence_count,
                }
                serialized[symbol][tf]["pivots"][ptype].append(pivot_dict)

        return serialized

    def should_recalculate(
        self,
        symbol: str,
        timeframe: str,
        current_bar_time: float
    ) -> bool:
        """
        Check if we should recalculate pivots (expired or new bar).

        Args:
            symbol: Trading symbol
            timeframe: Timeframe identifier
            current_bar_time: Current bar's Unix timestamp

        Returns:
            True if should recalculate (cache expired or new bar)
        """
        key = f"{symbol}_{timeframe}"

        # No entry in memory cache
        if key not in self.memory_cache:
            return True

        entry = self.memory_cache[key]

        # New bar closed (different bar_time)
        if current_bar_time > entry.bar_time:
            return True

        # Check TTL expiry
        ttl = entry.ttl_seconds
        age = time.time() - entry.cached_at
        if age > ttl:
            return True

        return False

    def cache_pivots(
        self,
        symbol: str,
        timeframe: str,
        pivots: List[PivotLevel]
    ) -> None:
        """
        Store calculated pivots in memory cache.

        Args:
            symbol: Trading symbol
            timeframe: Timeframe identifier
            pivots: List of PivotLevel objects to cache
        """
        if not pivots:
            return

        bar_time = pivots[0].bar_time  # All pivots have same bar_time
        ttl = DEFAULT_TTL_SECONDS.get(timeframe, 3600)  # Default 1 hour

        key = f"{symbol}_{timeframe}"
        self.memory_cache[key] = CacheEntry(
            symbol=symbol,
            timeframe=timeframe,
            bar_time=bar_time,
            pivots=pivots,
            cached_at=time.time(),
            ttl_seconds=ttl,
        )

    def get_cached_pivots(
        self,
        symbol: str,
        timeframe: str,
        current_bar_time: float
    ) -> Optional[List[PivotLevel]]:
        """
        Retrieve cached pivots if still valid.

        Args:
            symbol: Trading symbol
            timeframe: Timeframe identifier
            current_bar_time: Current bar's Unix timestamp for freshness check

        Returns:
            List of PivotLevel if cache hit and valid, None otherwise
        """
        key = f"{symbol}_{timeframe}"

        if key not in self.memory_cache:
            return None

        entry = self.memory_cache[key]

        # Check if pivots are from current bar (not stale)
        if entry.bar_time != current_bar_time:
            return None  # New bar, cache is stale

        # Check TTL
        age = time.time() - entry.cached_at
        if age > entry.ttl_seconds:
            del self.memory_cache[key]  # Expire old entry
            return None

        return entry.pivots

    def get_all_cached_symbols(self) -> List[str]:
        """Get list of symbols currently in cache."""
        return list(set(entry.symbol for entry in self.memory_cache.values()))

    def get_cached_timeframes(self, symbol: str) -> List[str]:
        """Get timeframes cached for a specific symbol."""
        return list(set(
            entry.timeframe
            for entry in self.memory_cache.values()
            if entry.symbol == symbol
        ))

    def clear_cache(self) -> None:
        """Clear all in-memory cache entries."""
        self.memory_cache.clear()
        log.info("Cleared in-memory pivot cache")

    def clear_symbol(self, symbol: str) -> None:
        """Clear cache entries for a specific symbol."""
        keys_to_remove = [
            key for key, entry in self.memory_cache.items()
            if entry.symbol == symbol
        ]
        for key in keys_to_remove:
            del self.memory_cache[key]
        log.info(f"Cleared cache for symbol {symbol}")

    def get_cache_stats(self) -> Dict:
        """Return cache statistics for diagnostics."""
        if not self.memory_cache:
            return {
                "total_entries": 0,
                "symbols": [],
                "size_bytes": 0,
            }

        symbols = self.get_all_cached_symbols()
        total_size = sum(
            len(json.dumps(asdict(entry)).encode())
            for entry in self.memory_cache.values()
        )

        return {
            "total_entries": len(self.memory_cache),
            "symbols": symbols,
            "timeframes_by_symbol": {
                sym: self.get_cached_timeframes(sym)
                for sym in symbols
            },
            "size_bytes": total_size,
            "last_update": datetime.now(timezone.utc).isoformat(),
        }

    def __repr__(self) -> str:
        stats = self.get_cache_stats()
        return f"PivotCacheManager(entries={stats['total_entries']}, symbols={len(stats['symbols'])})"
