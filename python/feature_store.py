"""
Feature Store — append-only ledger of every signal + outcome.

Schema (one row per signal):
    signal_id           TEXT PRIMARY KEY
    symbol              TEXT
    timeframe           TEXT
    ts_signal           INTEGER (unix)
    direction           TEXT (BUY/SELL)
    entry_price         REAL
    sl_price            REAL
    tp_price            REAL
    ict_features        JSON     (confluence layers)
    pivot_features      JSON     (nearest pivot, confluence, etc.)
    structure_features  JSON     (OB/FVG/BOS validation results)
    candle_features     JSON     (wick/body ratios, pattern type)
    regime              TEXT     (TRENDING/RANGING/...)
    session             TEXT
    decision            TEXT     (TRADED, SKIPPED, BLOCKED)
    skip_reason         TEXT
    confidence          INTEGER
    rr_ratio            REAL
    -- outcome (filled at close) --
    outcome             TEXT     (WIN, LOSS, BREAKEVEN, NULL=open)
    r_multiple          REAL
    profit_usd          REAL
    mae                 REAL     (max adverse excursion)
    mfe                 REAL     (max favorable excursion)
    time_in_trade_s     INTEGER
    exit_level          TEXT     (TP1/TP2/TP3/SL/MANUAL)
    ts_close            INTEGER

Phase 2f's online_learner reads this store to retrain the prediction model;
parameter_optimizer reads it for Bayesian tuning.

Public API:
- FeatureStore(db_path=None)
- record_signal(signal_id, **fields)
- record_outcome(signal_id, **fields)
- record_skip(signal_id, **fields, skip_reason)
- query(filters=..., limit=..., since_ts=..., outcome_only=False)
- export_csv(path)
- stats() → dict
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import logging

log = logging.getLogger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id           TEXT PRIMARY KEY,
    symbol              TEXT NOT NULL,
    timeframe           TEXT,
    ts_signal           INTEGER NOT NULL,
    direction           TEXT,
    entry_price         REAL,
    sl_price            REAL,
    tp_price            REAL,
    ict_features        TEXT,
    pivot_features      TEXT,
    structure_features  TEXT,
    candle_features     TEXT,
    regime              TEXT,
    session             TEXT,
    decision            TEXT,
    skip_reason         TEXT,
    confidence          INTEGER,
    rr_ratio            REAL,
    outcome             TEXT,
    r_multiple          REAL,
    profit_usd          REAL,
    mae                 REAL,
    mfe                 REAL,
    time_in_trade_s     INTEGER,
    exit_level          TEXT,
    ts_close            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_symbol      ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_outcome     ON signals(outcome);
CREATE INDEX IF NOT EXISTS idx_ts_signal   ON signals(ts_signal);
CREATE INDEX IF NOT EXISTS idx_regime      ON signals(regime);
CREATE INDEX IF NOT EXISTS idx_session     ON signals(session);
"""


def _to_json(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, default=str, separators=(",", ":"))
    except Exception:
        return str(val)


def _from_json(val: Optional[str]) -> Any:
    if not val:
        return None
    try:
        return json.loads(val)
    except Exception:
        return val


class FeatureStore:
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent / "feature_store.db"
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_schema()

    def _init_schema(self) -> None:
        conn = self._connect()
        with conn:
            for stmt in _SCHEMA_SQL.strip().split(";"):
                if stmt.strip():
                    conn.execute(stmt)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, isolation_level=None,
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL mode for concurrent reads while writing
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record_signal(
        self,
        signal_id: str,
        symbol: str,
        timeframe: str,
        direction: str,
        entry_price: float,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
        ict_features: Optional[dict] = None,
        pivot_features: Optional[dict] = None,
        structure_features: Optional[dict] = None,
        candle_features: Optional[dict] = None,
        regime: str = "",
        session: str = "",
        decision: str = "TRADED",
        skip_reason: str = "",
        confidence: int = 0,
        rr_ratio: float = 0.0,
        ts_signal: Optional[int] = None,
    ) -> None:
        if ts_signal is None:
            ts_signal = int(time.time())
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO signals (
                    signal_id, symbol, timeframe, ts_signal, direction,
                    entry_price, sl_price, tp_price,
                    ict_features, pivot_features, structure_features, candle_features,
                    regime, session, decision, skip_reason, confidence, rr_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_id, symbol, timeframe, ts_signal, direction,
                    float(entry_price), float(sl_price), float(tp_price),
                    _to_json(ict_features), _to_json(pivot_features),
                    _to_json(structure_features), _to_json(candle_features),
                    regime, session, decision, skip_reason,
                    int(confidence), float(rr_ratio),
                ),
            )
        except Exception as e:
            log.warning(f"feature_store record_signal error: {e}")

    def record_outcome(
        self,
        signal_id: str,
        outcome: str,
        r_multiple: float = 0.0,
        profit_usd: float = 0.0,
        mae: float = 0.0,
        mfe: float = 0.0,
        time_in_trade_s: int = 0,
        exit_level: str = "",
        ts_close: Optional[int] = None,
    ) -> None:
        if ts_close is None:
            ts_close = int(time.time())
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE signals SET
                       outcome=?, r_multiple=?, profit_usd=?,
                       mae=?, mfe=?, time_in_trade_s=?, exit_level=?, ts_close=?
                   WHERE signal_id=?""",
                (
                    outcome, float(r_multiple), float(profit_usd),
                    float(mae), float(mfe), int(time_in_trade_s),
                    exit_level, ts_close, signal_id,
                ),
            )
        except Exception as e:
            log.warning(f"feature_store record_outcome error: {e}")

    def record_skip(self, signal_id: str, skip_reason: str, **kwargs) -> None:
        kwargs["decision"] = "SKIPPED"
        kwargs["skip_reason"] = skip_reason
        self.record_signal(signal_id=signal_id, **kwargs)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def query(
        self,
        filters: Optional[Dict[str, Any]] = None,
        since_ts: Optional[int] = None,
        until_ts: Optional[int] = None,
        outcome_only: bool = False,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        Args:
            filters: {column: value} for exact-match WHERE clauses
            since_ts / until_ts: filter on ts_signal
            outcome_only: only rows where outcome IS NOT NULL
            limit: cap row count

        Returns: list of dicts (json-decoded feature columns).
        """
        conn = self._connect()
        clauses, params = [], []
        if filters:
            for col, val in filters.items():
                if val is None:
                    clauses.append(f"{col} IS NULL")
                else:
                    clauses.append(f"{col} = ?")
                    params.append(val)
        if since_ts is not None:
            clauses.append("ts_signal >= ?")
            params.append(int(since_ts))
        if until_ts is not None:
            clauses.append("ts_signal <= ?")
            params.append(int(until_ts))
        if outcome_only:
            clauses.append("outcome IS NOT NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM signals {where} ORDER BY ts_signal DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for col in ("ict_features", "pivot_features",
                        "structure_features", "candle_features"):
                d[col] = _from_json(d.get(col))
            out.append(d)
        return out

    def get_recent_outcomes(self, n: int = 200) -> List[Dict]:
        """Last N closed trades — primary feed for online_learner."""
        return self.query(outcome_only=True, limit=n)

    # ------------------------------------------------------------------
    # Stats / export
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        conn = self._connect()
        cur = conn.execute(
            """SELECT
                   COUNT(*)                                  AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)  AS wins,
                   SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open,
                   AVG(CASE WHEN outcome IS NOT NULL THEN r_multiple END) AS avg_r,
                   SUM(profit_usd)                           AS total_pnl
               FROM signals"""
        ).fetchone()
        return {
            "total_signals":    cur["total"] or 0,
            "wins":             cur["wins"] or 0,
            "losses":           cur["losses"] or 0,
            "open":             cur["open"] or 0,
            "win_rate":         (cur["wins"] / max(1, (cur["wins"] or 0) + (cur["losses"] or 0)))
                                if (cur["wins"] or cur["losses"]) else 0.0,
            "avg_r_multiple":   cur["avg_r"] or 0.0,
            "total_pnl_usd":    cur["total_pnl"] or 0.0,
        }

    def export_csv(self, path: Path) -> int:
        """Export all rows to CSV. Returns row count."""
        import csv
        rows = self.query()
        if not rows:
            return 0
        path = Path(path)
        cols = list(rows[0].keys())
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                # Re-serialize JSON columns
                for c in ("ict_features", "pivot_features",
                          "structure_features", "candle_features"):
                    if isinstance(r.get(c), (dict, list)):
                        r[c] = json.dumps(r[c])
                w.writerow(r)
        return len(rows)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# Module-level singleton for convenience
_default_store: Optional[FeatureStore] = None


def get_default_store() -> FeatureStore:
    global _default_store
    if _default_store is None:
        _default_store = FeatureStore()
    return _default_store
