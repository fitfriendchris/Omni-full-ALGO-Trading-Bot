"""
journal_telegram.py — Telegram /journal command integration for OMNI bot.

Adds to telegram_bot.py:
  /journal           — today's trading journal
  /journal <date>    — journal for specific date (YYYY-MM-DD)
  /journal week      — this week's summary
  /journal month     — this month's summary
  /journal streak    — current win/loss streak
  /journal setups    — setup performance breakdown
  /journal calendar  — last 7 days heatmap
  /journal export    — send zella_export.json as document

Author: JARVIS
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

LOGS = Path.home() / "Desktop/Hermes-Command-Center/logs"
JOURNAL_JSON = LOGS / "journal.json"


def load_journal() -> Dict[str, Any]:
    try:
        with open(JOURNAL_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def journal_today() -> str:
    d = load_journal()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    metrics = [m for m in d.get("day_metrics", []) if m.get("date") == today]
    trades = [t for t in d.get("trades", []) if t.get("date") == today]
    ent = [e for e in d.get("journal_entries", []) if e.get("date") == today]

    if not trades:
        return f"📓 *Journal — {today}*\nNo trades recorded today.\n\nUse `/journal calendar` for recent activity."

    m = metrics[0] if metrics else {}
    wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    losses = sum(1 for t in trades if (t.get("pnl") or 0) < 0)
    pnl = sum(t.get("pnl", 0) for t in trades)

    msg = (
        f"📓 *Journal — {today}*\n"
        f"{'━'*18}\n"
        f"*Trades:* {len(trades)} (W{wins}/L{losses})\n"
        f"*P&L:* `${pnl:+.2f}`\n"
        f"*Win Rate:* {m.get('winRate', 0):.1f}%\n"
        f"*Volume:* {m.get('totalVolume', 0):.2f} lots\n"
        f"*Max DD:* ${m.get('maxDrawdown', 0):.2f}\n"
        f"*Risk:* {m.get('riskLevel', 'low')}\n"
    )

    if ent:
        e = ent[0]
        msg += f"\n📝 *Notes:* {e.get('content', 'No notes')[:200]}"

    msg += "\n\n📊 `/journal calendar` · 📈 `/journal week`"
    return msg


def journal_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "❌ Invalid date. Use format: `YYYY-MM-DD`\nExample: `/journal 2026-06-01`"

    d = load_journal()
    trades = [t for t in d.get("trades", []) if t.get("date") == date_str]
    metrics = [m for m in d.get("day_metrics", []) if m.get("date") == date_str]

    if not trades:
        return f"📓 No trades on {date_str}."

    pnl = sum(t.get("pnl", 0) for t in trades)
    m = metrics[0] if metrics else {}
    msg = (
        f"📓 *{date_str}*\n"
        f"Trades: {len(trades)} · P&L: `${pnl:+.2f}`\n"
        f"Win Rate: {m.get('winRate', 0):.1f}% · Volume: {m.get('totalVolume', 0):.2f} lots"
    )
    return msg


def journal_week() -> str:
    d = load_journal()
    today = datetime.now(timezone.utc)
    monday = today - timedelta(days=today.weekday())
    week_trades = [
        t for t in d.get("trades", [])
        if datetime.strptime(t.get("date", "1970-01-01"), "%Y-%m-%d") >= monday
    ]
    if not week_trades:
        return "📓 No trades this week."

    pnl = sum(t.get("pnl", 0) for t in week_trades)
    wins = sum(1 for t in week_trades if (t.get("pnl") or 0) > 0)
    losses = len(week_trades) - wins

    msg = (
        f"📓 *Week of {monday.strftime('%b %d')}*\n"
        f"{'━'*18}\n"
        f"*Trades:* {len(week_trades)} (W{wins}/L{losses})\n"
        f"*P&L:* `${pnl:+.2f}`\n"
        f"*Best day:* +${max((t.get('pnl',0) for t in week_trades), default=0):.2f}\n"
        f"*Worst day:* ${min((t.get('pnl',0) for t in week_trades), default=0):.2f}"
    )
    return msg


def journal_month() -> str:
    d = load_journal()
    today = datetime.now(timezone.utc)
    month_trades = [
        t for t in d.get("trades", [])
        if t.get("date", "").startswith(today.strftime("%Y-%m"))
    ]
    if not month_trades:
        return "📓 No trades this month."

    pnl = sum(t.get("pnl", 0) for t in month_trades)
    wins = sum(1 for t in month_trades if (t.get("pnl") or 0) > 0)

    msg = (
        f"📓 *{today.strftime('%B %Y')}*\n"
        f"Trades: {len(month_trades)} · W{wins}/L{len(month_trades)-wins}\n"
        f"P&L: `${pnl:+.2f}`\n"
        f"Win Rate: {wins/max(1,len(month_trades))*100:.1f}%"
    )
    return msg


def journal_streak() -> str:
    d = load_journal()
    streak = d.get("summary", {}).get("current_streak", 0)
    if streak > 0:
        return f"🔥 *WIN STREAK:* x{streak}\nMomentum is with you."
    elif streak < 0:
        return f"❄️ *LOSS STREAK:* x{abs(streak)}\nConsider reducing size. Review rules."
    return "⚖️ No active streak."


def journal_setups() -> str:
    d = load_journal()
    trades = [t for t in d.get("trades", []) if t.get("status") == "closed" and t.get("pnl") is not None]
    if not trades:
        return "📓 No closed trades with setup data."

    setups: Dict[str, List[float]] = {}
    for t in trades:
        s = t.get("strategy") or t.get("setup", {}).get("name") or "unknown"
        setups.setdefault(s, []).append(t.get("pnl", 0))

    msg = "📓 *Setup Performance*\n"
    for name, pnls in sorted(setups.items(), key=lambda x: sum(x[1]), reverse=True):
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        msg += f"\n*{name}:* {len(pnls)} trades · WR {wr:.0f}% · P&L ${sum(pnls):+.2f}"

    return msg


def journal_calendar() -> str:
    d = load_journal()
    today = datetime.now(timezone.utc)
    days = []
    for i in range(6, -1, -1):
        d_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        m = next((m for m in d.get("day_metrics", []) if m.get("date") == d_str), None)
        if m:
            emoji = "🟢" if m.get("pnl", 0) > 0 else "🔴" if m.get("pnl", 0) < 0 else "⚪"
            days.append(f"{emoji} {d_str[-5:]}: ${m.get('pnl',0):+.0f} ({m.get('tradeCount',0)}t)")
        else:
            days.append(f"⚪ {d_str[-5:]}: —")

    return "📅 *Last 7 Days*\n" + "\n".join(days)


def journal_export_cmd() -> tuple:
    """Return (caption, file_path) for document send."""
    p = Path.home() / "Omni-full-ALGO-Trading-Bot/journal/zella_export.json"
    if p.exists():
        return "📤 Zella Trade Scribe export", str(p)
    return "❌ No export available. Run journal bridge first.", None


# ── Router ─────────────────────────────────────────────────────────

def handle_journal_command(args: List[str]) -> str:
    """Route /journal subcommands."""
    if not args or args[0] in ("today", ""):
        return journal_today()
    if args[0] == "week":
        return journal_week()
    if args[0] == "month":
        return journal_month()
    if args[0] == "streak":
        return journal_streak()
    if args[0] == "setups":
        return journal_setups()
    if args[0] == "calendar":
        return journal_calendar()
    if args[0] == "export":
        return journal_export_cmd()[0]

    # Try as date
    return journal_date(args[0])


# ── Integration hook for telegram_bot.py ─────────────────────────────

def register_journal_handlers(bot) -> None:
    """Call this in telegram_bot.py setup to add /journal handlers."""
    import telegram_bot as tb

    @tb.bot.message_handler(commands=["journal"])
    def on_journal(msg):
        parts = msg.text.split()[1:] if msg.text else []
        reply = handle_journal_command(parts)
        tb.send_message(msg.chat.id, reply, parse_mode="Markdown")

    @tb.bot.message_handler(commands=["journalcalendar", "jcal"])
    def on_jcal(msg):
        tb.send_message(msg.chat.id, journal_calendar(), parse_mode="Markdown")

    @tb.bot.message_handler(commands=["journalweek", "jweek"])
    def on_jweek(msg):
        tb.send_message(msg.chat.id, journal_week(), parse_mode="Markdown")

    @tb.bot.message_handler(commands=["journalstreak", "jstreak"])
    def on_jstreak(msg):
        tb.send_message(msg.chat.id, journal_streak(), parse_mode="Markdown")

    # Add to help text
    tb.HELP_TEXT += (
        "\n📓 *Journal* (Zella Trade Scribe)\n"
        "  /journal — today's journal\n"
        "  /journal YYYY-MM-DD — specific date\n"
        "  /journal week — this week\n"
        "  /journal month — this month\n"
        "  /journal streak — current streak\n"
        "  /journal setups — setup performance\n"
        "  /journal calendar — 7-day heatmap\n"
        "  /journal export — Zella JSON export\n"
    )
