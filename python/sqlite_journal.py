#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sqlite_journal.py — OMNI ICT Production Bot v28.0
Phase 5B: Structured SQLite trade journal + immutable daily JSONL append.
Replaces the legacy JSON journal with typed schema and relational queries.
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
class TradeRecord:
    ticket: int
    symbol: str = "XAUUSD"
    side: str = ""               # BUY | SELL
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    size_lots: float = 0.0
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pips: Optional[float] = None
    r_multiple: Optional[float] = None
    setup_type: str = ""         # redistribution_bullish | redistribution_bearish | bos_continuation
    confluence_count: int = 0
    session: str = ""            # asian | london | ny
    killzone: str = ""           # european | ny
    sweep_type: str = "none"     # asian_high | asian_low | pdh | pdl | equal_high | equal_low
    choch_type: str = "none"     # bullish | bearish | none
    fvg_size_pips: float = 0.0
    cycle_phase: str = "unknown"
    h4_bias: str = ""
    d1_bias: str = ""
    status: str = "open"         # open | partial_tp1 | partial_tp2 | closed
    sl_modifications: int = 0
    largest_dd_pct: float = 0.0  # Max drawdown during trade
    notes: str = ""


class SQLiteJournal:
    """
    SQLite journal with trades table + events table.
    Also appends to daily JSONL for immutable audit trail.
    """
    
    SCHEMA = r"""
    CREATE TABLE IF NOT EXISTS trades (
        ticket INTEGER PRIMARY KEY,
        symbol TEXT,
        side TEXT,
        entry_price REAL,
        stop_loss REAL,
        take_profit_1 REAL,
        take_profit_2 REAL,
        take_profit_3 REAL,
        size_lots REAL,
        open_time TEXT,
        close_time TEXT,
        pnl REAL,
        pnl_pips REAL,
        r_multiple REAL,
        setup_type TEXT,
        confluence_count INTEGER,
        session TEXT,
        killzone TEXT,
        sweep_type TEXT,
        choch_type TEXT,
        fvg_size_pips REAL,
        cycle_phase TEXT,
        h4_bias TEXT,
        d1_bias TEXT,
        status TEXT,
        sl_modifications INTEGER,
        largest_dd_pct REAL,
        notes TEXT
    );
    CREATE TABLE IF NOT EXISTS trade_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER,
        event_time TEXT,
        event_type TEXT,         -- OPEN | MODIFY_SL | TP1_HIT | TP2_HIT | CLOSE | PAUSE | RESUME
        price REAL,
        detail TEXT,
        FOREIGN KEY (ticket) REFERENCES trades(ticket)
    );
    CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
    CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
    CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(open_time);
    CREATE INDEX IF NOT EXISTS idx_events_ticket ON trade_events(ticket);
    """
    
    def __init__(self, db_path: str = None, jsonl_dir: str = None):
        if db_path is None:
            db_path = str(Path.home() / "Omni-full-ALGO-Trading-Bot" / "python" / "trade_journal_v28.db")
        if jsonl_dir is None:
            jsonl_dir = str(Path.home() / "Omni-full-ALGO-Trading-Bot" / "logs")
        self.db_path = db_path
        self.jsonl_dir = Path(jsonl_dir)
        self.jsonl_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.SCHEMA)
        conn.commit()
        conn.close()
    
    def record_open(self, record: TradeRecord) -> None:
        """Log a new trade at open."""
        data = asdict(record)
        # Remove None values for SQL
        cols = [k for k, v in data.items() if v is not None]
        vals = [v for v in data.values() if v is not None]
        placeholders = ",".join(["?"] * len(vals))
        
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"INSERT OR REPLACE INTO trades ({','.join(cols)}) VALUES ({placeholders})", vals)
        conn.execute("INSERT INTO trade_events (ticket, event_time, event_type, price, detail) VALUES (?, ?, ?, ?, ?)",
                     (record.ticket, record.open_time or datetime.now(timezone.utc).isoformat(), "OPEN",
                      record.entry_price, json.dumps(data)))
        conn.commit()
        conn.close()
        self._append_jsonl({"event": "OPEN", **data})
        logger.info(f"Journal OPEN ticket={record.ticket} side={record.side}")
    
    def record_event(self, ticket: int, event_type: str, price: float, detail: str = "") -> None:
        """Log a mid-trade event (SL modification, TP hit, etc.)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO trade_events (ticket, event_time, event_type, price, detail) VALUES (?, ?, ?, ?, ?)",
                     (ticket, now, event_type, price, detail))
        if event_type == "MODIFY_SL":
            conn.execute("UPDATE trades SET sl_modifications = COALESCE(sl_modifications,0)+1 WHERE ticket=?", (ticket,))
        if event_type in ("TP1_HIT", "TP2_HIT", "CLOSE"):
            conn.execute("UPDATE trades SET status=? WHERE ticket=?", (event_type.lower(), ticket))
        conn.commit()
        conn.close()
        self._append_jsonl({"event": event_type, "ticket": ticket, "time": now, "price": price, "detail": detail})
    
    def record_close(self, ticket: int, close_price: float, pnl: float, pnl_pips: float,
                     r_multiple: float, status: str = "closed", notes: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""UPDATE trades SET close_time=?, pnl=?, pnl_pips=?, r_multiple=?, status=?, notes=?
                        WHERE ticket=?""", (now, pnl, pnl_pips, r_multiple, status, notes, ticket))
        conn.execute("INSERT INTO trade_events (ticket, event_time, event_type, price, detail) VALUES (?, ?, ?, ?, ?)",
                     (ticket, now, "CLOSE", close_price, f"pnl={pnl}, r={r_multiple}"))
        conn.commit()
        conn.close()
        self._append_jsonl({"event": "CLOSE", "ticket": ticket, "time": now, "price": close_price,
                           "pnl": pnl, "r_multiple": r_multiple})
        logger.info(f"Journal CLOSE ticket={ticket} pnl=${pnl:.2f} r={r_multiple:.2f}")
    
    def get_open_trades(self) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades WHERE status IN ('open', 'partial_tp1', 'partial_tp2')").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    
    def get_trade_summary(self, days: int = 30) -> Dict:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("""
            SELECT COUNT(*) as total, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(pnl) as total_pnl, AVG(r_multiple) as avg_r,
                   SUM(CASE WHEN r_multiple >= 3 THEN 1 ELSE 0 END) as big_wins
            FROM trades
            WHERE close_time > datetime('now', '-{days} days') AND status = 'closed'
        """.format(days=days)).fetchone()
        conn.close()
        total, wins, total_pnl, avg_r, big_wins = rows
        return {
            "period_days": days,
            "total_trades": total or 0,
            "winning_trades": wins or 0,
            "win_rate": (wins / total * 100) if total else 0,
            "total_pnl": total_pnl or 0,
            "avg_r_multiple": avg_r or 0,
            "big_wins_3R_plus": big_wins or 0,
        }
    
    def _append_jsonl(self, record: Dict) -> None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.jsonl_dir / f"trades_{date_str}.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    j = SQLiteJournal()
    r = TradeRecord(ticket=999999, symbol="XAUUSD", side="BUY", entry_price=3300.0,
                    stop_loss=3295.0, take_profit_1=3315.0, size_lots=0.5,
                    open_time="2026-05-26T08:30:00Z", setup_type="redistribution_bullish",
                    confluence_count=7, sweep_type="asian_low", choch_type="bullish",
                    fvg_size_pips=3.5, cycle_phase="distribution")
    j.record_open(r)
    j.record_event(999999, "MODIFY_SL", 3297.0, "Breakeven after TP1")
    j.record_close(999999, 3315.5, 155.0, 15.5, 3.1)
    print("Journal test OK. Summary:", j.get_trade_summary())
