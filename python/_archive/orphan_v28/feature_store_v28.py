#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feature_store_v28.py — OMNI ICT Production Bot v28.0
Phase 3: Structural feature logging for ML training.
Replaces legacy feature_store.py. Logs every setup attempt with full
structural context so the model learns WHAT worked (sweep type, CHoCH timing,
FVG size, session context) not just RSI/EMA noise.
"""
from __future__ import annotations
import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class StructuralFeatures:
    # Identity
    setup_id: str
    symbol: str
    timestamp: str
    
    # Sweep features
    sweep_type: str           # "none" | "asian_high" | "asian_low" | "pdh" | "pdl" | "equal_high" | "equal_low"
    sweep_magnitude_pips: float
    sweep_mitigated: bool
    sweep_time_since_sec: int
    sweep_multi_touch: bool
    sweep_volume_ratio: float
    
    # Structure features
    choch_type: str            # "none" | "bullish" | "bearish"
    choch_time_since_sec: int
    choch_magnitude_pips: float
    bos_type: str              # "none" | "bullish" | "bearish"
    bos_time_since_sec: int
    trend_before: str          # "up" | "down" | "ranging"
    trend_after: str
    
    # FVG features
    fvg_direction: str         # "none" | "bullish" | "bearish"
    fvg_size_pips: float
    fvg_mitigated: bool
    fvg_time_since_sec: int
    fvg_distance_to_price_pips: float
    
    # Session context
    current_session: str       # "asian" | "london" | "ny" | "off"
    time_in_session_min: int
    session_range_pips: float
    asian_range_swept: bool
    london_extension_pct: float
    
    # Higher timeframe
    h4_bias: str             # "bullish" | "bearish" | "neutral"
    d1_bias: str
    cycle_phase: str         # "accumulation" | "manipulation" | "distribution" | "unknown"
    cycle_day_number: int
    prior_3d_avg_range_pips: float
    
    # Confluence
    confluence_count: int
    target_liquidity: str    # "asian_high" | "asian_low" | "pdh" | "pdl" | "none"
    opposing_liquidity_distance_pips: float
    
    # Execution / Legacy
    atr_14: float
    rsi_14: float
    ema_8_21_cross: float
    volume_ratio: float


@dataclass
class SetupOutcome:
    setup_id: str
    entered: bool              # Did we actually take the trade?
    entry_slippage_pips: float
    exit_price: Optional[float]
    exit_time: Optional[str]
    pnl_pips: float
    r_multiple: float
    status: str                # "loss" | "0-1R" | "1-3R" | "3-5R" | "5R+"
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    stopped_out: bool
    manual_close: bool
    notes: str = ""


class FeatureStoreV28:
    """SQLite-backed feature store with structural columns."""
    
    SCHEMA_FEATURES = """
    CREATE TABLE IF NOT EXISTS structural_features (
        setup_id TEXT PRIMARY KEY,
        symbol TEXT,
        timestamp TEXT,
        sweep_type TEXT,
        sweep_magnitude_pips REAL,
        sweep_mitigated INTEGER,
        sweep_time_since_sec INTEGER,
        sweep_multi_touch INTEGER,
        sweep_volume_ratio REAL,
        choch_type TEXT,
        choch_time_since_sec INTEGER,
        choch_magnitude_pips REAL,
        bos_type TEXT,
        bos_time_since_sec INTEGER,
        trend_before TEXT,
        trend_after TEXT,
        fvg_direction TEXT,
        fvg_size_pips REAL,
        fvg_mitigated INTEGER,
        fvg_time_since_sec INTEGER,
        fvg_distance_to_price_pips REAL,
        current_session TEXT,
        time_in_session_min INTEGER,
        session_range_pips REAL,
        asian_range_swept INTEGER,
        london_extension_pct REAL,
        h4_bias TEXT,
        d1_bias TEXT,
        cycle_phase TEXT,
        cycle_day_number INTEGER,
        prior_3d_avg_range_pips REAL,
        confluence_count INTEGER,
        target_liquidity TEXT,
        opposing_liquidity_distance_pips REAL,
        atr_14 REAL,
        rsi_14 REAL,
        ema_8_21_cross REAL,
        volume_ratio REAL,
        raw_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_feat_symbol ON structural_features(symbol);
    CREATE INDEX IF NOT EXISTS idx_feat_time ON structural_features(timestamp);
    CREATE INDEX IF NOT EXISTS idx_feat_sweep ON structural_features(sweep_type);
    CREATE INDEX IF NOT EXISTS idx_feat_chooch ON structural_features(choch_type);
    CREATE INDEX IF NOT EXISTS idx_feat_phase ON structural_features(cycle_phase);
    """
    
    SCHEMA_OUTCOMES = """
    CREATE TABLE IF NOT EXISTS setup_outcomes (
        setup_id TEXT PRIMARY KEY,
        entered INTEGER,
        entry_slippage_pips REAL,
        exit_price REAL,
        exit_time TEXT,
        pnl_pips REAL,
        r_multiple REAL,
        status TEXT,
        tp1_hit INTEGER,
        tp2_hit INTEGER,
        tp3_hit INTEGER,
        stopped_out INTEGER,
        manual_close INTEGER,
        notes TEXT,
        FOREIGN KEY (setup_id) REFERENCES structural_features(setup_id)
    );
    """
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = Path.home() / "Omni-full-ALGO-Trading-Bot" / "python" / "feature_store_v28.db"
        self.db_path = str(db_path)
        self._init_db()
    
    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.SCHEMA_FEATURES)
        conn.executescript(self.SCHEMA_OUTCOMES)
        conn.commit()
        conn.close()
    
    def log_setup(self, features: StructuralFeatures) -> None:
        """Log a potential setup BEFORE the trade executes."""
        data = asdict(features)
        raw = json.dumps(data)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO structural_features VALUES (
                :setup_id, :symbol, :timestamp, :sweep_type, :sweep_magnitude_pips,
                :sweep_mitigated, :sweep_time_since_sec, :sweep_multi_touch, :sweep_volume_ratio,
                :choch_type, :choch_time_since_sec, :choch_magnitude_pips, :bos_type,
                :bos_time_since_sec, :trend_before, :trend_after, :fvg_direction,
                :fvg_size_pips, :fvg_mitigated, :fvg_time_since_sec, :fvg_distance_to_price_pips,
                :current_session, :time_in_session_min, :session_range_pips, :asian_range_swept,
                :london_extension_pct, :h4_bias, :d1_bias, :cycle_phase, :cycle_day_number,
                :prior_3d_avg_range_pips, :confluence_count, :target_liquidity,
                :opposing_liquidity_distance_pips, :atr_14, :rsi_14, :ema_8_21_cross,
                :volume_ratio, :raw_json
            )
        """, {**data, "raw_json": raw})
        conn.commit()
        conn.close()
        logger.debug(f"FeatureStore logged setup {features.setup_id}")
    
    def log_outcome(self, outcome: SetupOutcome) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO setup_outcomes VALUES (
                :setup_id, :entered, :entry_slippage_pips, :exit_price, :exit_time,
                :pnl_pips, :r_multiple, :status, :tp1_hit, :tp2_hit, :tp3_hit,
                :stopped_out, :manual_close, :notes
            )
        """, asdict(outcome))
        conn.commit()
        conn.close()
    
    def get_training_data(self, symbol: str = None, min_confluences: int = 5,
                         lookahead_days: int = 90) -> List[Dict]:
        """Return feature+outcome joined rows for model training."""
        conn = sqlite3.connect(self.db_path)
        query = """
            SELECT f.*, o.status as outcome_status, o.r_multiple, o.pnl_pips
            FROM structural_features f
            LEFT JOIN setup_outcomes o ON f.setup_id = o.setup_id
            WHERE f.timestamp > datetime('now', '-{days} days')
        """.format(days=lookahead_days)
        params = []
        if symbol:
            query += " AND f.symbol = ?"
            params.append(symbol)
        if min_confluences:
            query += " AND f.confluence_count >= ?"
            params.append(min_confluences)
        query += " ORDER BY f.timestamp DESC"
        rows = conn.execute(query, params).fetchall()
        cols = [d[0] for d in conn.execute(query, params).description]
        conn.close()
        
        result = []
        for row in rows:
            result.append(dict(zip(cols, row)))
        return result
    
    def get_feature_drift(self, symbol: str, window_days: int = 7) -> Dict[str, float]:
        """Detect parameter drift by comparing recent vs historical feature distributions."""
        conn = sqlite3.connect(self.db_path)
        recent = conn.execute("""
            SELECT AVG(sweep_magnitude_pips), AVG(fvg_size_pips), AVG(confluence_count)
            FROM structural_features
            WHERE symbol = ? AND timestamp > datetime('now', '-{} days')
        """.format(window_days), (symbol,)).fetchone()
        
        historical = conn.execute("""
            SELECT AVG(sweep_magnitude_pips), AVG(fvg_size_pips), AVG(confluence_count)
            FROM structural_features
            WHERE symbol = ? AND timestamp BETWEEN datetime('now', '-{} days') AND datetime('now', '-{} days')
        """.format(window_days * 3, window_days), (symbol,)).fetchone()
        conn.close()
        
        if not recent or not historical:
            return {}
        return {
            "sweep_mag_drift_pct": ((recent[0] or 1) - (historical[0] or 1)) / (historical[0] or 1) * 100,
            "fvg_size_drift_pct": ((recent[1] or 1) - (historical[1] or 1)) / (historical[1] or 1) * 100,
            "confluence_drift": ((recent[2] or 1) - (historical[2] or 1)),
        }


if __name__ == "__main__":
    # Quick test
    store = FeatureStoreV28()
    f = StructuralFeatures(
        setup_id="test_001", symbol="XAUUSD", timestamp="2026-05-26T08:30:00Z",
        sweep_type="asian_low", sweep_magnitude_pips=12.5, sweep_mitigated=False,
        sweep_time_since_sec=120, sweep_multi_touch=True, sweep_volume_ratio=1.8,
        choch_type="bullish", choch_time_since_sec=60, choch_magnitude_pips=8.2,
        bos_type="none", bos_time_since_sec=0, trend_before="down", trend_after="up",
        fvg_direction="bullish", fvg_size_pips=3.5, fvg_mitigated=False,
        fvg_time_since_sec=45, fvg_distance_to_price_pips=1.2,
        current_session="london", time_in_session_min=30, session_range_pips=25.0,
        asian_range_swept=True, london_extension_pct=150.0,
        h4_bias="bullish", d1_bias="bullish", cycle_phase="distribution",
        cycle_day_number=3, prior_3d_avg_range_pips=18.0, confluence_count=7,
        target_liquidity="pdh", opposing_liquidity_distance_pips=40.0,
        atr_14=5.0, rsi_14=55.0, ema_8_21_cross=0.3, volume_ratio=1.2,
    )
    store.log_setup(f)
    print("Feature store test OK")
