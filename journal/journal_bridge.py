"""
journal_bridge.py — Zella Trade Scribe ↔ OMNI Integration Bridge

Reads OMNI trading data (trade_memory.json, signals.json, MT5 CSV)
and formats it into Zella-compatible journal entries for:
  • Obsidian dashboard (logs/journal.json)
  • Telegram bot (/journal command)
  • GitHub repo sync (journal/entries.jsonl)

Author: JARVIS
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Paths ──────────────────────────────────────────────────────────
OMNI = Path.home() / "Omni-full-ALGO-Trading-Bot"
PYTHON = OMNI / "python"
SHARED = OMNI / "shared"
LOGS = Path.home() / "Desktop/Hermes-Command-Center/logs"
JOURNAL_DIR = OMNI / "journal"

# ── Data Models (Zella-compatible) ─────────────────────────────────

@dataclass
class JournalTrade:
    id: str
    accountId: str = "omni-midasfx-live"
    currencyPair: str = "XAUUSD"
    date: str = ""                 # YYYY-MM-DD
    timeIn: str = ""               # HH:MM
    timeOut: Optional[str] = None
    session: str = "european"      # asian | european | us | overlap
    timestamp: int = 0
    side: str = "long"             # long | short
    direction: str = "long"
    entryPrice: float = 0.0
    exitPrice: Optional[float] = None
    spread: Optional[float] = None
    lotSize: float = 0.01
    lotType: str = "micro"         # standard | mini | micro
    units: int = 1000
    stopLoss: Optional[float] = None
    takeProfit: Optional[float] = None
    riskAmount: Optional[float] = None
    rMultiple: Optional[float] = None
    leverage: int = 1000
    marginUsed: Optional[float] = None
    pips: Optional[float] = None
    pipValue: Optional[float] = None
    pnl: Optional[float] = None
    commission: float = 0.0
    swap: float = 0.0
    accountCurrency: str = "USD"
    strategy: str = "ict_dual_tf"
    marketConditions: str = ""
    timeframe: str = "M15"
    confidence: Optional[float] = None
    emotions: str = ""
    notes: str = ""
    screenshots: List[str] = field(default_factory=list)
    status: str = "open"           # open | closed
    tags: List[str] = field(default_factory=list)
    setup: Optional[Dict[str, Any]] = None
    patterns: List[Dict[str, Any]] = field(default_factory=list)
    partialCloses: List[Dict[str, Any]] = field(default_factory=list)

    def to_zella(self) -> dict:
        """Export as Zella Trade Scribe compatible dict."""
        d = asdict(self)
        d["timestamp"] = int(datetime.now(timezone.utc).timestamp() * 1000)
        return d


@dataclass
class JournalEntry:
    id: str
    date: str                      # YYYY-MM-DD
    title: str
    content: str
    tags: List[str] = field(default_factory=list)
    linkedTradeIds: List[str] = field(default_factory=list)
    emotions: str = ""
    lessons: str = ""
    marketOutlook: str = ""
    screenshots: List[str] = field(default_factory=list)
    completionPercentage: float = 0.0
    createdAt: str = ""
    updatedAt: str = ""


@dataclass
class DayMetrics:
    date: str
    pnl: float = 0.0
    tradeCount: int = 0
    winRate: float = 0.0
    hasJournalEntry: bool = False
    hasTradeNotes: bool = False
    completionPercentage: float = 0.0
    totalVolume: float = 0.0
    averageWin: float = 0.0
    averageLoss: float = 0.0
    maxDrawdown: float = 0.0
    sharpeRatio: Optional[float] = None
    hasScreenshots: bool = False
    emotionalState: str = "neutral"   # positive | neutral | negative
    riskLevel: str = "low"              # low | medium | high


# ── Readers ────────────────────────────────────────────────────────

def load_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_trade_memory() -> List[Dict[str, Any]]:
    """Read trades from OMNI trade_memory.json or trader_state.json."""
    mem = load_json(PYTHON / "trade_memory.json") or load_json(OMNI / "trade_memory.json")
    if isinstance(mem, dict):
        return mem.get("history") or mem.get("trades") or []
    return []


def read_signals() -> Dict[str, Any]:
    return load_json(SHARED / "signals.json") or {}


def read_trader_state() -> Dict[str, Any]:
    return load_json(PYTHON / "trader_state.json") or {}


def read_mt5_csv() -> List[Dict[str, Any]]:
    """Read MT5 OrderHistory if available."""
    csv_candidates = [
        OMNI / "mt5_orders.csv",
        OMNI / "python" / "mt5_orders.csv",
        Path.home() / "Downloads" / "OrderHistory.csv",
    ]
    for p in csv_candidates:
        if p.exists():
            try:
                import csv
                with open(p, "r", encoding="utf-8") as f:
                    return list(csv.DictReader(f))
            except Exception:
                pass
    return []


# ── Transformers ─────────────────────────────────────────────────────

def omni_trade_to_journal(raw: Dict[str, Any], idx: int) -> JournalTrade:
    """Convert an OMNI raw trade dict to a Zella-compatible JournalTrade."""
    now = datetime.now(timezone.utc)
    ts = raw.get("timestamp") or raw.get("time") or raw.get("date")
    dt = now
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(float(ts or 0), tz=timezone.utc)
    elif isinstance(ts, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ts.split(".")[0], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue

    side = (raw.get("direction") or raw.get("side") or "long").lower()
    if side not in ("long", "short"):
        side = "long"

    pair = raw.get("symbol") or raw.get("currencyPair") or "XAUUSD"
    if pair.upper() == "GOLD":
        pair = "XAUUSD"
    if pair.upper() == "SILVER":
        pair = "XAGUSD"

    entry = raw.get("entry_price") or raw.get("entryPrice") or raw.get("price") or 0.0
    exit_p = raw.get("exit_price") or raw.get("exitPrice") or raw.get("close_price")
    pnl = raw.get("pnl") or raw.get("profit") or raw.get("pl")

    # Determine session from hour
    hour = dt.hour
    session = "asian" if 0 <= hour < 7 else "european" if 7 <= hour < 12 else "us" if 12 <= hour < 17 else "overlap"

    # Build tags from strategy / setup
    tags = []
    for key in ("strategy", "setup", "pattern", "confluence"):
        val = raw.get(key)
        if val:
            tags.append(str(val).lower().replace(" ", "_"))

    return JournalTrade(
        id=raw.get("id") or raw.get("ticket") or f"omni-{dt.strftime('%Y%m%d')}-{idx:03d}",
        accountId=raw.get("accountId") or "omni-midasfx-live",
        currencyPair=pair,
        date=dt.strftime("%Y-%m-%d"),
        timeIn=dt.strftime("%H:%M"),
        timeOut=(datetime.fromtimestamp(float(raw.get("close_timestamp")), tz=timezone.utc).strftime("%H:%M")
                if raw.get("close_timestamp") is not None else None),
        session=session,
        timestamp=int(dt.timestamp() * 1000),
        side=side,
        direction=side,
        entryPrice=float(entry) if entry else 0.0,
        exitPrice=float(exit_p) if exit_p else None,
        spread=raw.get("spread"),
        lotSize=float(raw.get("lot_size") or raw.get("lotSize") or 0.01),
        lotType=raw.get("lotType") or "micro",
        units=int(raw.get("units") or 1000),
        stopLoss=raw.get("stop_loss") or raw.get("stopLoss"),
        takeProfit=raw.get("take_profit") or raw.get("takeProfit"),
        riskAmount=raw.get("risk_amount") or raw.get("riskAmount"),
        rMultiple=raw.get("r_multiple") or raw.get("rMultiple"),
        leverage=int(raw.get("leverage") or 1000),
        marginUsed=raw.get("margin_used") or raw.get("marginUsed"),
        pips=raw.get("pips"),
        pipValue=raw.get("pip_value") or raw.get("pipValue"),
        pnl=float(pnl) if pnl is not None else None,
        commission=float(raw.get("commission") or 0),
        swap=float(raw.get("swap") or 0),
        accountCurrency=raw.get("account_currency") or "USD",
        strategy=raw.get("strategy") or "ict_dual_tf",
        marketConditions=raw.get("market_conditions") or raw.get("regime") or "",
        timeframe=raw.get("timeframe") or "M15",
        confidence=raw.get("confidence"),
        emotions=raw.get("emotions") or "",
        notes=raw.get("notes") or raw.get("comment") or "",
        screenshots=raw.get("screenshots") or [],
        status="closed" if pnl is not None else "open",
        tags=tags,
        setup=raw.get("setup"),
        patterns=raw.get("patterns") or [],
        partialCloses=raw.get("partial_closes") or raw.get("partialCloses") or [],
    )


def build_journal_entries(trades: List[JournalTrade]) -> List[JournalEntry]:
    """Create daily journal entries from trade list."""
    entries: Dict[str, JournalEntry] = {}
    for t in trades:
        d = t.date
        if d not in entries:
            entries[d] = JournalEntry(
                id=f"journal-{d}",
                date=d,
                title=f"Trading Journal — {d}",
                content="",
                linkedTradeIds=[],
                createdAt=datetime.now(timezone.utc).isoformat(),
            )
        ent = entries[d]
        ent.linkedTradeIds.append(t.id)

    # Enrich with content
    for d, ent in entries.items():
        day_trades = [t for t in trades if t.date == d]
        wins = sum(1 for t in day_trades if (t.pnl or 0) > 0)
        losses = sum(1 for t in day_trades if (t.pnl or 0) < 0)
        total_pnl = sum((t.pnl or 0) for t in day_trades)
        ent.content = (
            f"**Trades:** {len(day_trades)}  \n"
            f"**Wins:** {wins} · **Losses:** {losses}  \n"
            f"**P&L:** ${total_pnl:+.2f}  \n"
            f"**Best:** {max((t.pnl for t in day_trades), default=0):+.2f}  \n"
            f"**Worst:** {min((t.pnl for t in day_trades), default=0):+.2f}"
        )
        ent.completionPercentage = min(100, len(day_trades) * 20)

    return list(entries.values())


def build_day_metrics(trades: List[JournalTrade]) -> List[DayMetrics]:
    """Build calendar day metrics for dashboard heatmap."""
    by_date: Dict[str, List[JournalTrade]] = {}
    for t in trades:
        by_date.setdefault(t.date, []).append(t)

    metrics = []
    for d, day_trades in sorted(by_date.items()):
        closed = [t for t in day_trades if t.status == "closed" and t.pnl is not None]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl < 0]
        pnl = sum(t.pnl for t in closed)
        wr = (len(wins) / len(closed) * 100) if closed else 0
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0
        max_dd = min((t.pnl for t in closed), default=0)
        total_vol = sum(t.lotSize for t in day_trades)

        # Risk level based on drawdown
        risk_level = "low"
        if max_dd < -20:
            risk_level = "high"
        elif max_dd < -10:
            risk_level = "medium"

        metrics.append(DayMetrics(
            date=d,
            pnl=round(pnl, 2),
            tradeCount=len(day_trades),
            winRate=round(wr, 1),
            totalVolume=round(total_vol, 2),
            averageWin=round(avg_win, 2),
            averageLoss=round(avg_loss, 2),
            maxDrawdown=round(abs(max_dd), 2),
            riskLevel=risk_level,
        ))

    return metrics


# ── Writers ──────────────────────────────────────────────────────────

def write_journal_json(trades: List[JournalTrade], entries: List[JournalEntry],
                       metrics: List[DayMetrics]) -> None:
    """Write unified journal.json for Obsidian + Telegram dashboard."""
    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "id": "omni-midasfx-live",
            "name": "Omni MidasFX Live",
            "type": "live",
            "broker": "MidasFX",
            "currency": "USD",
            "balance": 0.0,
            "initialBalance": 100.0,
            "platform": "mt5",
        },
        "trades": [asdict(t) for t in trades],
        "journal_entries": [asdict(e) for e in entries],
        "day_metrics": [asdict(m) for m in metrics],
        "summary": {
            "total_trades": len(trades),
            "closed_trades": len([t for t in trades if t.status == "closed"]),
            "open_trades": len([t for t in trades if t.status == "open"]),
            "total_pnl": round(sum((t.pnl or 0) for t in trades if t.pnl is not None), 2),
            "win_rate": round(
                len([t for t in trades if (t.pnl or 0) > 0]) /
                max(1, len([t for t in trades if t.pnl is not None])) * 100, 1
            ),
            "profit_factor": 0.0,  # calculated below
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "current_streak": 0,
        }
    }

    # Calculate profit factor
    wins = [t.pnl for t in trades if (t.pnl or 0) > 0]
    losses = [abs(t.pnl) for t in trades if (t.pnl or 0) < 0]
    payload["summary"]["profit_factor"] = round(sum(wins) / max(1, sum(losses)), 2)
    payload["summary"]["avg_win"] = round(sum(wins) / max(1, len(wins)), 2)
    payload["summary"]["avg_loss"] = round(-sum(losses) / max(1, len(losses)), 2)
    payload["summary"]["max_drawdown"] = round(
        abs(min((t.pnl for t in trades if t.pnl is not None), default=0)), 2
    )

    # Streak
    streak = 0
    for t in sorted(trades, key=lambda x: x.timestamp, reverse=True):
        if (t.pnl or 0) > 0:
            streak = streak + 1 if streak >= 0 else 1
        elif (t.pnl or 0) < 0:
            streak = streak - 1 if streak <= 0 else -1
        else:
            break
    payload["summary"]["current_streak"] = streak

    out = LOGS / "journal.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"✓ journal.json → {out} ({len(trades)} trades, {len(entries)} entries, {len(metrics)} days)")


def write_jsonl(trades: List[JournalTrade]) -> None:
    """Append trades to journal/entries.jsonl for GitHub repo sync."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    p = JOURNAL_DIR / "entries.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(asdict(t), default=str) + "\n")
    print(f"✓ entries.jsonl → {p}")


def write_zella_export(trades: List[JournalTrade]) -> None:
    """Export as Zella Trade Scribe JSON for potential web app import."""
    p = JOURNAL_DIR / "zella_export.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "source": "omni-ict-bot",
            "trades": [t.to_zella() for t in trades],
        }, f, indent=2, default=str)
    print(f"✓ zella_export.json → {p}")


# ── Main ────────────────────────────────────────────────────────────

def main():
    raw_trades = read_trade_memory()
    if not raw_trades:
        print("⚠ No trades found in trade_memory — checking MT5 CSV…")
        raw_trades = read_mt5_csv()

    # Also pull from trader_state if present
    ts = read_trader_state()
    if isinstance(ts, dict) and "trades" in ts:
        raw_trades.extend(ts["trades"])

    # Deduplicate by id
    seen = set()
    uniq = []
    for t in raw_trades:
        tid = t.get("id") or t.get("ticket") or json.dumps(t, sort_keys=True)
        if tid not in seen:
            seen.add(tid)
            uniq.append(t)

    trades = [omni_trade_to_journal(t, i) for i, t in enumerate(uniq)]
    entries = build_journal_entries(trades)
    metrics = build_day_metrics(trades)

    write_journal_json(trades, entries, metrics)
    write_jsonl(trades)
    write_zella_export(trades)

    return trades, entries, metrics


if __name__ == "__main__":
    main()
