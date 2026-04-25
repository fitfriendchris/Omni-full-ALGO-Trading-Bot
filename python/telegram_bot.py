"""
telegram_bot.py — OMNI-ICT Telegram control interface.

Fully autonomous — responds to commands and sends proactive alerts.
Uses only Python stdlib (urllib) — zero extra dependencies.

Commands
--------
  📊 Dashboard
    /start               — register as admin (first-run only)
    /help                — full command list
    /dashboard           — full snapshot: equity + trades + signals + status
    /status              — all services alive/dead
    /equity              — live balance & equity from MT5
    /trades              — active open positions
    /pnl                 — today's P&L and streaks
    /signals             — latest ICT signals
    /performance         — win rate, avg R, expectancy
    /risk                — current risk settings & limits
    /log <service>       — last 20 lines of a service log

  🎛 Control
    /halt                — stop new trade entries immediately
    /resume              — lift the halt
    /restart <service>   — restart a service via watchdog
    /setrisk <pct>       — set base risk % (e.g. /setrisk 1.5)
    /paper on|off        — toggle paper mode

  🏦 Accounts
    /account             — show current active account
    /accounts            — list all configured accounts
    /switch <id>         — switch to a different account
    /addaccount          — add a new MT5 account (guided)
    /deleteaccount <id>  — remove an account from config
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
HERE         = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
LOG_DIR      = PROJECT_ROOT / "logs"
CONFIG_PATH  = PROJECT_ROOT / "config.json"

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("OMNI_TELEGRAM_TOKEN", "")
CHAT_ID_FILE    = HERE / "telegram_chat_id.txt"
ACTIVE_ACC_FILE = HERE / "active_account.txt"
WATCHDOG_STATE  = LOG_DIR / "watchdog_state.json"
ALERTS_PATH     = LOG_DIR / "alerts.json"
HALT_PATH       = HERE / "HALT"

POLL_TIMEOUT    = 30     # Telegram long-poll seconds
ALERT_POLL_S    = 10     # how often to check for new alerts/trades

log = logging.getLogger("telegram_bot")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# Conversation state for multi-step flows keyed by chat_id
_pending: dict[int, dict] = {}


# ── Telegram API ──────────────────────────────────────────────────────────────

def _api(method: str, **params) -> dict:
    url  = f"{BASE_URL}/{method}"
    data = json.dumps(params).encode("utf-8")
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("Telegram %s HTTP %s: %s", method, e.code,
                    e.read().decode("utf-8", errors="replace"))
        return {}
    except Exception as e:
        log.warning("Telegram %s error: %s", method, e)
        return {}


def send(chat_id, text: str, parse_mode: str = "HTML",
         reply_markup: dict | None = None) -> None:
    params: dict = dict(chat_id=chat_id, text=text, parse_mode=parse_mode)
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    _api("sendMessage", **params)


def _sales_page(chat_id: int) -> None:
    """Send full sales pitch with inline buy buttons to any stranger."""
    text = (
        "🤖 <b>OMNI-ICT — Automated MT5 Trading Bot</b>\n\n"
        "Trade ICT / Smart Money Concepts 24/5 — fully automated.\n\n"
        "<b>What you get:</b>\n"
        "📊 ICT pattern detection (OB, FVG, BOS, sweeps)\n"
        "📈 Smart compounding &amp; pyramid sizing\n"
        "🛡️ Drawdown protection &amp; smart trailing stops\n"
        "🤖 AI market regime detection (Claude)\n"
        "📱 Full Telegram control center\n"
        "🔁 Self-healing, auto-restarts, crash recovery\n\n"
        "<b>Choose your plan:</b>"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🥉 Starter — $49/mo", "url": "https://buy.stripe.com/dRm7sK5U22048aZePc7Re05"},
            ],
            [
                {"text": "🥈 Pro — $99/mo ⭐ Popular", "url": "https://buy.stripe.com/00wdR8eqyeMQ2QF0Ym7Re06"},
            ],
            [
                {"text": "🥇 Elite — $199/mo", "url": "https://buy.stripe.com/5kQ8wO6Y6eMQ62R0Ym7Re07"},
            ],
            [
                {"text": "📖 Setup Guide", "url": "https://github.com/fitfriendchris/Omni-full-ALGO-Trading-Bot/blob/main/STARTUP_GUIDE.md"},
                {"text": "🏦 Open MT5 Account", "url": "https://www.midasfx.com/?ib=1128101"},
            ],
            [
                {"text": "🌐 Visit Website", "url": "https://fitfriendchris.github.io/Omni-full-ALGO-Trading-Bot/"},
            ],
        ]
    }
    send(chat_id, text, reply_markup=keyboard)


def get_updates(offset: int, timeout: int = POLL_TIMEOUT) -> list[dict]:
    return _api("getUpdates", offset=offset, timeout=timeout,
                allowed_updates=["message"]).get("result", [])


# ── Chat ID / account persistence ─────────────────────────────────────────────

def _load_chat_id() -> int | None:
    try:
        return int(CHAT_ID_FILE.read_text().strip()) if CHAT_ID_FILE.exists() else None
    except Exception:
        return None


def _save_chat_id(cid: int) -> None:
    CHAT_ID_FILE.write_text(str(cid))


def _load_active_account() -> str:
    return ACTIVE_ACC_FILE.read_text().strip() if ACTIVE_ACC_FILE.exists() else ""


def _save_active_account(acc_id: str) -> None:
    ACTIVE_ACC_FILE.write_text(acc_id)


# ── File helpers ──────────────────────────────────────────────────────────────

def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _write_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def _state_path(acc_id: str = "") -> Path:
    """Return trader_state.json path for the given account."""
    cfg = _json(CONFIG_PATH)
    if cfg:
        accounts = cfg.get("accounts", [cfg])
        for a in accounts:
            if a.get("id") == acc_id and a.get("state_path"):
                return Path(a["state_path"])
        for a in accounts:
            if a.get("state_path"):
                return Path(a["state_path"])
    default = HERE / (f"trader_state_{acc_id}.json" if acc_id else "trader_state.json")
    return default if default.exists() else HERE / "trader_state.json"


def _mt5_data_path(acc_id: str = "") -> Path | None:
    """Return omni_data.json path for the given account."""
    cfg = _json(CONFIG_PATH)
    if cfg:
        accounts = cfg.get("accounts", [cfg])
        for a in accounts:
            if a.get("id") == acc_id and a.get("data_path"):
                return Path(a["data_path"])
        for a in accounts:
            if a.get("data_path"):
                return Path(a["data_path"])
    if cfg and cfg.get("data_path"):
        return Path(cfg["data_path"])
    return None


def _live_equity(acc_id: str = "") -> tuple[float, float, float, float, str]:
    """Returns (balance, equity, free_margin, profit, currency) from live MT5 data."""
    mt5_path = _mt5_data_path(acc_id)
    if mt5_path and mt5_path.exists():
        data = _json(mt5_path)
        if data:
            acc  = data.get("account", {})
            bal  = acc.get("balance")  or 0.0
            eq   = acc.get("equity")   or 0.0
            free = acc.get("free_margin", acc.get("margin_free", 0)) or 0.0
            prof = acc.get("profit")   or 0.0
            curr = acc.get("currency", "USD")
            return bal, eq, free, prof, curr
    return 0.0, 0.0, 0.0, 0.0, "USD"


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_dashboard(acc_id: str = "") -> str:
    """Consolidated full-snapshot view."""
    bal, eq, free, prof, curr = _live_equity(acc_id)
    state   = _json(_state_path(acc_id)) or {}
    peak    = state.get("peak_equity") or eq
    if peak < eq:
        peak = eq
    dd      = ((peak - eq) / peak * 100) if peak > 0 else 0.0
    day_pnl = state.get("day_profit") or 0.0
    day_tr  = state.get("day_trades") or 0
    wins    = state.get("win_streak") or 0
    losses  = state.get("loss_streak") or 0
    halted  = state.get("trading_halted", False)

    # Active trades
    trades  = state.get("active_trades", {})
    trades_lines = []
    for t in list(trades.values())[:4]:
        sym  = t.get("symbol", "?")
        dir_ = t.get("direction", "?")
        ep   = t.get("entry_price", t.get("entry", 0))
        icon = "🟢" if dir_ == "BUY" else "🔴"
        trades_lines.append(f"  {icon} {sym} {dir_} @{ep:.5f}")

    # Latest signals
    signals_path = PROJECT_ROOT / "shared" / "signals.json"
    payload = _json(signals_path) or {}
    sigs    = payload.get("signals", [])
    sig_lines = []
    for s in sigs[:3]:
        sym  = s.get("symbol", "?")
        dir_ = s.get("direction", "?")
        conf = s.get("confidence", 0)
        et   = s.get("entry_type", "")
        icon = "🟢" if dir_ == "BULL" else ("🔴" if dir_ == "BEAR" else "⚪")
        sig_lines.append(f"  {icon} {sym} {dir_} {et} {conf:.0%}")

    # Service health
    ws   = _json(WATCHDOG_STATE) or {}
    svcs = ws.get("services", {})
    svc_line = "  " + "  ".join(
        ("✅" if v.get("alive") else "❌") + f" {k}"
        for k, v in svcs.items()
    )

    ts  = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        f"<b>🤖 OMNI-ICT Dashboard</b>  <code>{ts}</code>",
        "",
        f"<b>💰 Account</b>",
        f"  Balance:  {curr} {bal:,.2f}",
        f"  Equity:   {curr} {eq:,.2f}",
        f"  Open P&L: {curr} {prof:+,.2f}",
        f"  Today:    {curr} {day_pnl:+,.2f}  ({day_tr} trades)",
        f"  Drawdown: {dd:.2f}%  |  Peak: {curr} {peak:,.2f}",
        f"  Streaks:  ✅{wins}W  ❌{losses}L",
    ]
    if halted:
        lines.append(f"  🛑 <b>HALTED</b>: {state.get('halt_reason','')}")

    if trades:
        lines += ["", f"<b>📊 Open Trades ({len(trades)})</b>"] + trades_lines
        if len(trades) > 4:
            lines.append(f"  … +{len(trades)-4} more  (/trades for all)")

    if sigs:
        lines += ["", f"<b>📡 Signals ({len(sigs)})</b>"] + sig_lines
        if len(sigs) > 3:
            lines.append(f"  … +{len(sigs)-3} more  (/signals for all)")

    lines += ["", "<b>⚙️ Services</b>", svc_line]

    if HALT_PATH.exists():
        lines.append("\n🛑 <b>HALT ACTIVE</b>")

    return "\n".join(lines)


def cmd_status() -> str:
    ws = _json(WATCHDOG_STATE)
    if not ws:
        return "⚠️ Watchdog not running."
    ts = ws.get("ts", "?")[:19]
    lines = [f"<b>🤖 OMNI-ICT Services</b>  <code>{ts} UTC</code>\n"]
    for name, info in ws.get("services", {}).items():
        icon = "✅" if info.get("alive") else "❌"
        pid  = info.get("pid", "—")
        rs   = info.get("restarts", 0)
        started = (info.get("started_at") or "")[:16]
        lines.append(f"{icon} <b>{name}</b>  pid={pid}  restarts={rs}  up since {started}")
    if HALT_PATH.exists():
        lines.append("\n🛑 <b>HALT ACTIVE</b> — no new entries")
    return "\n".join(lines)


def cmd_equity(acc_id: str = "") -> str:
    mt5_path = _mt5_data_path(acc_id)
    if mt5_path and mt5_path.exists():
        data = _json(mt5_path)
        if data:
            acc  = data.get("account", {})
            bal  = acc.get("balance", 0) or 0
            eq   = acc.get("equity",  0) or 0
            free = acc.get("free_margin", acc.get("margin_free", 0)) or 0
            prof = acc.get("profit", 0) or 0
            lev  = acc.get("leverage", 0)
            curr = acc.get("currency", "USD")
            name = acc.get("name", acc.get("login", "?"))
            ts   = data.get("timestamp", "")[:19]
            age_s = ""
            try:
                from datetime import datetime
                ts_dt = datetime.strptime(ts, "%Y.%m.%d %H:%M:%S")
                age   = (datetime.utcnow() - ts_dt).total_seconds()
                age_s = f"  (data age: {int(age)}s)"
            except Exception:
                pass
            lines = [
                f"<b>💰 Live Account</b>  <code>{ts}</code>{age_s}",
                f"Name:        {name}",
                f"Balance:     {curr} {bal:,.2f}",
                f"Equity:      {curr} {eq:,.2f}",
                f"Free margin: {curr} {free:,.2f}",
                f"Open P&L:    {curr} {prof:+,.2f}",
            ]
            if lev:
                lines.append(f"Leverage:    1:{lev}")
            return "\n".join(lines)
    state = _json(_state_path(acc_id))
    if not state:
        return "❌ No MT5 data or trader state found.\nMake sure MT5 is running and the EA is attached."
    eq  = state.get("equity")  or state.get("day_start_equity") or 0
    bal = state.get("balance") or eq
    return (f"<b>💰 Equity</b> (from state — MT5 offline)\n"
            f"Balance: ${bal:,.2f}\nEquity:  ${eq:,.2f}")


def cmd_trades(acc_id: str = "") -> str:
    state = _json(_state_path(acc_id))
    if not state:
        return "📊 No trader state found."
    trades = state.get("active_trades", {})
    if not trades:
        return "📊 No active trades right now."
    lines = [f"<b>📊 Open Trades ({len(trades)})</b>\n"]
    for tid, t in list(trades.items()):
        sym  = t.get("symbol", "?")
        dir_ = t.get("direction", "?")
        ep   = t.get("entry_price", t.get("entry", 0))
        sl   = t.get("current_sl", t.get("sl", 0))
        tp   = t.get("tp1", t.get("tp", 0))
        tp1  = "✓" if t.get("tp1_taken") else ""
        icon = "🟢" if dir_ == "BUY" else "🔴"
        lines.append(f"{icon} <b>{sym}</b> {dir_}  entry={ep:.5f}")
        lines.append(f"   SL={sl:.5f}  TP1={tp:.5f}{tp1}  ticket={tid}")
    return "\n".join(lines)


def cmd_pnl(acc_id: str = "") -> str:
    state = _json(_state_path(acc_id)) or {}
    day_pnl  = state.get("day_profit")     or 0.0
    day_tr   = state.get("day_trades")     or 0
    wins     = state.get("win_streak")     or 0
    losses   = state.get("loss_streak")    or 0
    risk_pct = state.get("current_risk_pct") or 1.0
    halted   = state.get("trading_halted", False)
    halt_why = state.get("halt_reason", "")
    peak     = state.get("peak_equity")    or 0.0

    bal, eq, free, prof, curr = _live_equity(acc_id)
    if eq == 0.0:
        eq = state.get("equity") or state.get("day_start_equity") or 0.0
    if bal == 0.0:
        bal = state.get("balance") or eq
    if peak == 0.0 or (eq > 0 and peak < eq):
        peak = eq

    dd = ((peak - eq) / peak * 100) if peak > 0 else 0.0

    lines = [
        "<b>💰 P&L Dashboard</b>",
        f"Balance:      {curr} {bal:,.2f}",
        f"Equity:       {curr} {eq:,.2f}",
        f"Peak equity:  {curr} {peak:,.2f}",
        f"Drawdown:     {dd:.2f}%",
        f"Open P&L:     {curr} {prof:+,.2f}",
        "",
        f"Today P&L:    {curr} {day_pnl:+.2f}  ({day_tr} trades)",
        f"Win streak:   {wins}  |  Loss streak: {losses}",
        f"Current risk: {risk_pct:.2f}%",
    ]
    if halted:
        lines.append(f"\n🛑 <b>HALTED</b>: {halt_why}")
    return "\n".join(lines)


def cmd_performance(acc_id: str = "") -> str:
    state = _json(_state_path(acc_id)) or {}
    total_trades = state.get("total_trades") or 0
    total_wins   = state.get("total_wins")   or 0
    total_losses = state.get("total_losses") or 0
    total_pnl    = state.get("total_profit") or 0.0
    avg_win      = state.get("avg_win")      or 0.0
    avg_loss     = state.get("avg_loss")     or 0.0
    best_trade   = state.get("best_trade")   or 0.0
    worst_trade  = state.get("worst_trade")  or 0.0
    max_dd       = state.get("max_drawdown") or 0.0
    _, eq, _, _, curr = _live_equity(acc_id)

    wr = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    rr = abs(avg_win / avg_loss) if avg_loss and avg_loss != 0 else 0.0
    expectancy = (wr/100 * avg_win) + ((1 - wr/100) * avg_loss)

    mem_path = HERE / (f"trade_memory_{acc_id}.json" if acc_id else "trade_memory.json")
    mem_trades = 0
    if mem_path.exists():
        mem = _json(mem_path) or {}
        mem_trades = len(mem.get("trades", []))

    lines = [
        "<b>📈 Performance Stats</b>",
        f"Total trades:  {total_trades}  (memory: {mem_trades})",
        f"Win rate:      {wr:.1f}%  ({total_wins}W / {total_losses}L)",
        f"Avg win:       {curr} {avg_win:+,.2f}",
        f"Avg loss:      {curr} {avg_loss:+,.2f}",
        f"R:R ratio:     {rr:.2f}",
        f"Expectancy:    {curr} {expectancy:+,.2f} per trade",
        f"Best trade:    {curr} {best_trade:+,.2f}",
        f"Worst trade:   {curr} {worst_trade:+,.2f}",
        f"Total P&L:     {curr} {total_pnl:+,.2f}",
        f"Max drawdown:  {max_dd:.2f}%",
    ]
    if total_trades == 0:
        lines.append("\n<i>No completed trades yet.</i>")
    return "\n".join(lines)


def cmd_risk(acc_id: str = "") -> str:
    state = _json(_state_path(acc_id)) or {}
    try:
        from config import cfg
        base_r  = cfg.BASE_RISK_PCT
        min_r   = cfg.MIN_RISK_PCT
        max_r   = cfg.MAX_RISK_PCT
        max_dd  = cfg.MAX_DD_FROM_PEAK
        max_dly = cfg.MAX_DAILY_LOSS_PCT
        paper   = cfg.PAPER_MODE
    except Exception:
        base_r = min_r = max_r = max_dd = max_dly = 0.0
        paper  = True
    cur_r   = state.get("current_risk_pct") or base_r
    wins    = state.get("win_streak")  or 0
    losses  = state.get("loss_streak") or 0
    halted  = state.get("trading_halted", False)
    halt_w  = state.get("halt_reason", "")

    lines = [
        "<b>⚖️ Risk Settings</b>",
        f"Paper mode:     {'🟡 YES' if paper else '🔴 LIVE'}",
        f"Current risk:   {cur_r:.2f}%",
        f"Base risk:      {base_r:.2f}%",
        f"Risk range:     {min_r:.2f}% – {max_r:.2f}%",
        f"Max drawdown:   {max_dd:.1f}%",
        f"Max daily loss: {max_dly:.1f}%",
        f"Win streak:     {wins}  |  Loss streak: {losses}",
    ]
    if halted:
        lines.append(f"\n🛑 <b>HALTED</b>: {halt_w}")
        lines.append("Use /resume to lift halt.")
    return "\n".join(lines)


def cmd_signals() -> str:
    signals_path = PROJECT_ROOT / "shared" / "signals.json"
    payload = _json(signals_path)
    if not payload:
        return "📡 No signals file yet. Orchestrator may not have run."
    sigs = payload.get("signals", [])
    ts   = payload.get("generated_at", "")[:19]
    if not sigs:
        return f"📡 No signals this cycle.\nLast run: <code>{ts}</code>"
    lines = [f"<b>📡 Signals ({len(sigs)})</b>  <code>{ts}</code>\n"]
    for s in sigs[:10]:
        sym  = s.get("symbol", "?")
        dir_ = s.get("direction", "?")
        et   = s.get("entry_type", "none")
        conf = s.get("confidence", 0)
        ep   = s.get("entry_price")
        sl   = s.get("sl")
        icon = "🟢" if dir_ == "BULL" else ("🔴" if dir_ == "BEAR" else "⚪")
        ep_s = f"  @{ep:.5f}" if ep else ""
        sl_s = f"  SL {sl:.5f}" if sl else ""
        lines.append(f"{icon} <b>{sym}</b> {dir_}  {et}  {conf:.0%}{ep_s}{sl_s}")
    return "\n".join(lines)


def cmd_log(service: str) -> str:
    allowed = {"orchestrator", "auto_trader", "server", "telegram_bot",
               "watchdog", "launchagent"}
    svc = service.lower().strip()
    if svc not in allowed:
        return (f"❌ Unknown service <code>{svc}</code>\n"
                f"Available: {', '.join(sorted(allowed))}")
    if svc == "launchagent":
        log_path = LOG_DIR / "launchagent.out.log"
    else:
        log_path = LOG_DIR / f"{svc}.log"
    if not log_path.exists():
        return f"❌ Log not found: <code>{log_path.name}</code>"
    try:
        lines = log_path.read_text(errors="replace").splitlines()
        tail  = lines[-20:] if len(lines) >= 20 else lines
        block = "\n".join(tail)
        return (f"<b>📋 {svc}.log</b> (last {len(tail)} lines)\n"
                f"<pre>{block}</pre>")
    except Exception as e:
        return f"❌ Could not read log: {e}"


def cmd_halt(chat_id) -> str:
    HALT_PATH.write_text(
        f"HALT by Telegram {chat_id} at {datetime.now(timezone.utc).isoformat()}")
    return "🛑 <b>HALT written.</b> No new entries until /resume."


def cmd_resume(chat_id) -> str:
    if HALT_PATH.exists():
        HALT_PATH.unlink()
        return "✅ <b>HALT removed.</b> Trading active."
    return "ℹ️ No halt was active."


def cmd_restart(service: str) -> str:
    allowed = {"orchestrator", "auto_trader", "server", "telegram_bot"}
    svc = service.lower().strip()
    if svc not in allowed:
        return (f"❌ Unknown service <code>{svc}</code>\n"
                f"Available: {', '.join(sorted(allowed))}")
    ws = _json(WATCHDOG_STATE)
    if not ws:
        return "❌ Watchdog state unavailable."
    pid = ws.get("services", {}).get(svc, {}).get("pid")
    if not pid:
        return f"❌ No PID for <code>{svc}</code>."
    try:
        import signal as _sig
        os.kill(int(pid), _sig.SIGTERM)
        return (f"✅ SIGTERM sent to <code>{svc}</code> (pid={pid}).\n"
                f"Watchdog will restart it in ~5 seconds.")
    except ProcessLookupError:
        return f"ℹ️ <code>{svc}</code> (pid={pid}) was already stopped."
    except Exception as e:
        return f"❌ Error: {e}"


# ── Settings management ───────────────────────────────────────────────────────

# Map of user-facing param names → (env_var, type, min, max, unit, description)
_SETTINGS: dict[str, tuple] = {
    # name          env_var              type    min   max    unit   description
    "risk":       ("OMNI_RISK_MODE",    "choice", None, None, "",    "LOW / MODERATE / HIGH"),
    "freq":       ("OMNI_FREQ_MODE",    "choice", None, None, "",    "CONSERVATIVE / NORMAL / AGGRESSIVE"),
    "base_risk":  ("OMNI_BASE_RISK",    "float",  0.1,  5.0,  "%",   "Base risk per trade"),
    "max_risk":   ("OMNI_MAX_RISK",     "float",  0.1,  10.0, "%",   "Max risk after win streak"),
    "min_risk":   ("OMNI_MIN_RISK",     "float",  0.05, 3.0,  "%",   "Min risk after loss streak"),
    "daily_loss": ("OMNI_DAILY_LOSS",   "float",  0.5,  20.0, "%",   "Max daily loss before halt"),
    "max_dd":     ("OMNI_MAX_DD",       "float",  1.0,  30.0, "%",   "Max drawdown from peak before halt"),
    "min_conf":   ("OMNI_MIN_CONF",     "int",    30,   95,   "",    "Min signal confidence (0-100)"),
    "max_trades": ("OMNI_MAX_TRADES",   "int",    1,    10,   "",    "Max open positions at once"),
    "scan":       ("OMNI_SCAN_INTERVAL","int",    5,    300,  "s",   "Seconds between MT5 scans"),
    "min_rr":     ("OMNI_MIN_RR",       "float",  0.5,  5.0,  "R",   "Min risk:reward ratio"),
    "paper":      ("OMNI_PAPER_MODE",   "bool",   None, None, "",    "Paper mode (true/false)"),
}

_RISK_CHOICES = {"LOW", "MODERATE", "HIGH"}
_FREQ_CHOICES = {"CONSERVATIVE", "NORMAL", "AGGRESSIVE"}

_RISK_DESCS = {
    "LOW":      "0.5% risk | 1% max | 2% daily limit | 5% max DD",
    "MODERATE": "1.0% risk | 2% max | 3% daily limit | 10% max DD",
    "HIGH":     "2.0% risk | 4% max | 5% daily limit | 15% max DD",
}
_FREQ_DESCS = {
    "CONSERVATIVE": "A+/A grades only | conf≥70 | max 2 open",
    "NORMAL":       "A+/A/B+ grades  | conf≥65 | max 3 open",
    "AGGRESSIVE":   "All grades      | conf≥55 | max 5 open",
}


def _env_read() -> dict[str, str]:
    """Parse .env into a key→value dict."""
    env_path = PROJECT_ROOT / ".env"
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _env_set(key: str, value: str) -> None:
    """Write or update one key in .env atomically."""
    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    new_lines = [l for l in lines if not l.startswith(f"{key}=")]
    new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n")


def cmd_settings() -> str:
    """Show all current settings with their active values."""
    env = _env_read()

    risk_mode = env.get("OMNI_RISK_MODE", "MODERATE")
    freq_mode = env.get("OMNI_FREQ_MODE", "AGGRESSIVE")
    paper     = env.get("OMNI_PAPER_MODE", "true")

    lines = [
        "<b>⚙️ OMNI-ICT Settings</b>",
        f"(Use /set &lt;name&gt; &lt;value&gt; to change)\n",
        "<b>Modes</b>",
        f"  risk   = <b>{risk_mode}</b>   — {_RISK_DESCS.get(risk_mode, '')}",
        f"  freq   = <b>{freq_mode}</b>",
        f"           {_FREQ_DESCS.get(freq_mode, '')}",
        f"  paper  = <b>{paper}</b>",
        "",
        "<b>Risk Fine-Tuning</b>",
        f"  base_risk  = {env.get('OMNI_BASE_RISK',  '1.0 (default)')}%",
        f"  max_risk   = {env.get('OMNI_MAX_RISK',   '2.0 (default)')}%",
        f"  min_risk   = {env.get('OMNI_MIN_RISK',   '0.5 (default)')}%",
        f"  daily_loss = {env.get('OMNI_DAILY_LOSS', '3.0 (default)')}%",
        f"  max_dd     = {env.get('OMNI_MAX_DD',     '10.0 (default)')}%",
        "",
        "<b>Trade Quality</b>",
        f"  min_conf   = {env.get('OMNI_MIN_CONF',     '65 (default)')}",
        f"  min_rr     = {env.get('OMNI_MIN_RR',       '2.0 (default)')}R",
        f"  max_trades = {env.get('OMNI_MAX_TRADES',   '3 (default)')}",
        f"  scan       = {env.get('OMNI_SCAN_INTERVAL','10 (default)')}s",
        "",
        "/restart auto_trader to apply changes.",
    ]
    return "\n".join(lines)


def cmd_set(key: str, value: str) -> str:
    """Set a named setting and persist it to .env."""
    key = key.lower().strip()
    value = value.strip()

    if key not in _SETTINGS:
        names = ", ".join(f"<code>{k}</code>" for k in sorted(_SETTINGS))
        return (f"❌ Unknown setting <code>{key}</code>\n\n"
                f"Available settings:\n{names}\n\n"
                f"Example: /set risk MODERATE")

    env_var, kind, lo, hi, unit, desc = _SETTINGS[key]

    if kind == "choice":
        val_up = value.upper()
        if key == "risk":
            if val_up not in _RISK_CHOICES:
                choices = " | ".join(sorted(_RISK_CHOICES))
                return (f"❌ <code>risk</code> must be: {choices}\n\n"
                        + "\n".join(f"  <b>{k}</b> — {v}" for k, v in _RISK_DESCS.items()))
            _env_set(env_var, val_up)
            return (f"✅ <b>risk = {val_up}</b>\n"
                    f"{_RISK_DESCS[val_up]}\n\n"
                    f"Use /restart auto_trader to apply.")
        elif key == "freq":
            if val_up not in _FREQ_CHOICES:
                choices = " | ".join(sorted(_FREQ_CHOICES))
                return (f"❌ <code>freq</code> must be: {choices}\n\n"
                        + "\n".join(f"  <b>{k}</b> — {v}" for k, v in _FREQ_DESCS.items()))
            _env_set(env_var, val_up)
            return (f"✅ <b>freq = {val_up}</b>\n"
                    f"{_FREQ_DESCS[val_up]}\n\n"
                    f"Use /restart auto_trader to apply.")

    elif kind == "bool":
        val_low = value.lower()
        if val_low in ("on", "true", "yes", "1"):
            _env_set(env_var, "true")
            return "✅ <b>paper mode ON</b>\nUse /restart auto_trader to apply."
        elif val_low in ("off", "false", "no", "0"):
            return ("⚠️ <b>Disabling paper mode trades REAL MONEY.</b>\n"
                    "Edit <code>.env</code> manually and set "
                    "<code>OMNI_PAPER_MODE=false</code>, then restart auto_trader.")
        return "❌ Use: /set paper on  or  /set paper off"

    elif kind == "float":
        try:
            num = float(value.rstrip("%Rr"))
        except ValueError:
            return f"❌ <code>{key}</code> needs a number. Example: /set {key} {lo}"
        if lo is not None and num < lo:
            return f"❌ <code>{key}</code> minimum is {lo}{unit}"
        if hi is not None and num > hi:
            return f"❌ <code>{key}</code> maximum is {hi}{unit}"
        _env_set(env_var, str(num))
        return (f"✅ <b>{key} = {num}{unit}</b>  ({desc})\n"
                f"Use /restart auto_trader to apply.")

    elif kind == "int":
        try:
            num = int(float(value))
        except ValueError:
            return f"❌ <code>{key}</code> needs a whole number. Example: /set {key} {lo}"
        if lo is not None and num < lo:
            return f"❌ <code>{key}</code> minimum is {lo}{unit}"
        if hi is not None and num > hi:
            return f"❌ <code>{key}</code> maximum is {hi}{unit}"
        _env_set(env_var, str(num))
        return (f"✅ <b>{key} = {num}{unit}</b>  ({desc})\n"
                f"Use /restart auto_trader to apply.")

    return f"❌ Internal error for setting <code>{key}</code>"


def cmd_setrisk(arg: str) -> str:
    return cmd_set("base_risk", arg)


# ── Account management ────────────────────────────────────────────────────────

def cmd_accounts() -> str:
    cfg = _json(CONFIG_PATH)
    if not cfg:
        return "❌ config.json not found."
    accounts = cfg.get("accounts", [cfg])
    current  = _load_active_account()
    lines    = ["<b>🏦 Configured Accounts</b>\n"]
    for a in accounts:
        aid  = a.get("id", "?")
        name = a.get("name", aid)
        typ  = a.get("type", "?")
        mark = " ◀ active" if aid == current else ""
        lines.append(f"• <code>{aid}</code>  {name}  [{typ}]{mark}")
    lines.append("\nUse /switch &lt;id&gt; to change account.")
    lines.append("Use /addaccount to add a new MT5 account.")
    return "\n".join(lines)


def cmd_account(acc_id: str = "") -> str:
    current = acc_id or _load_active_account()
    cfg     = _json(CONFIG_PATH)
    accounts = cfg.get("accounts", [cfg]) if cfg else []
    info     = next((a for a in accounts if a.get("id") == current), None)
    if not info and accounts:
        info    = accounts[0]
        current = info.get("id", "")
    if not info:
        return "❌ No account info found."
    lines = [
        f"<b>🏦 Active Account</b>",
        f"ID:      <code>{info.get('id','?')}</code>",
        f"Name:    {info.get('name', info.get('id','?'))}",
        f"Type:    {info.get('type','?')}",
    ]
    login = info.get("login") or info.get("mt5_login")
    if login:
        lines.append(f"Login:   <code>{login}</code>")
    server = info.get("server") or info.get("mt5_server")
    if server:
        lines.append(f"Server:  {server}")
    return "\n".join(lines)


def cmd_switch(new_id: str) -> str:
    cfg = _json(CONFIG_PATH)
    if not cfg:
        return "❌ config.json not found."
    accounts = cfg.get("accounts", [cfg])
    ids = [a.get("id", "") for a in accounts]
    if new_id not in ids:
        return (f"❌ Account <code>{new_id}</code> not found.\n"
                f"Available: {', '.join(ids)}\n"
                f"Use /addaccount to add a new one.")
    _save_active_account(new_id)
    return (f"✅ Switched to account <code>{new_id}</code>.\n"
            f"Use /restart auto_trader to apply the change.")


def cmd_deleteaccount(acc_id: str) -> str:
    cfg = _json(CONFIG_PATH)
    if not cfg:
        return "❌ config.json not found."
    accounts = cfg.get("accounts", [])
    ids = [a.get("id", "") for a in accounts]
    if acc_id not in ids:
        return f"❌ Account <code>{acc_id}</code> not found."
    if len(accounts) <= 1:
        return "❌ Cannot delete the only account."
    cfg["accounts"] = [a for a in accounts if a.get("id") != acc_id]
    _write_config(cfg)
    active = _load_active_account()
    if active == acc_id:
        new_active = cfg["accounts"][0]["id"]
        _save_active_account(new_active)
        return (f"✅ Account <code>{acc_id}</code> deleted.\n"
                f"Active account set to <code>{new_active}</code>.")
    return f"✅ Account <code>{acc_id}</code> deleted."


def cmd_addaccount_start(chat_id: int) -> str:
    """Begin guided MT5 account setup."""
    _pending[chat_id] = {"step": "id", "data": {}}
    return (
        "<b>➕ Add MT5 Account</b>\n\n"
        "Step 1/5 — Enter a short ID for this account\n"
        "(letters/numbers only, e.g. <code>live1</code>, <code>ftmo</code>, <code>demo2</code>)\n\n"
        "Type /cancel to abort."
    )


def _handle_setup(chat_id: int, text: str) -> str:
    """Process one step of the multi-step account setup."""
    pending = _pending.get(chat_id)
    if not pending:
        return ""

    if text.strip().lower() == "/cancel":
        del _pending[chat_id]
        return "❌ Account setup cancelled."

    step = pending["step"]
    data = pending["data"]

    if step == "id":
        acc_id = re.sub(r"[^a-zA-Z0-9_-]", "", text.strip())
        if not acc_id:
            return "❌ Invalid ID. Use letters/numbers only. Try again:"
        cfg = _json(CONFIG_PATH) or {}
        existing = [a.get("id") for a in cfg.get("accounts", [])]
        if acc_id in existing:
            return f"❌ Account <code>{acc_id}</code> already exists. Choose a different ID:"
        data["id"] = acc_id
        pending["step"] = "name"
        return (f"ID: <code>{acc_id}</code> ✅\n\n"
                f"Step 2/5 — Enter a display name\n"
                f"(e.g. <code>FTMO Challenge</code>, <code>Live USD</code>)")

    elif step == "name":
        data["name"] = text.strip()
        pending["step"] = "login"
        return (f"Name: <b>{data['name']}</b> ✅\n\n"
                f"Step 3/5 — Enter MT5 login number\n"
                f"(the numeric account number from your broker, e.g. <code>12345678</code>)")

    elif step == "login":
        login = text.strip()
        if not login.isdigit():
            return "❌ Login must be a number. Try again:"
        data["login"] = login
        pending["step"] = "password"
        return (f"Login: <code>{login}</code> ✅\n\n"
                f"Step 4/5 — Enter MT5 password\n"
                f"⚠️ Note: Telegram messages are stored on Telegram servers.\n"
                f"For security, consider changing this password after setup.")

    elif step == "password":
        data["password"] = text.strip()
        pending["step"] = "server"
        return (f"Password: <code>{'*' * len(data['password'])}</code> ✅\n\n"
                f"Step 5/5 — Enter broker server name\n"
                f"(visible in MT5 → File → Open Account, e.g. <code>MetaQuotes-Demo</code>, "
                f"<code>ICMarkets-Live01</code>)")

    elif step == "server":
        data["server"] = text.strip()
        pending["step"] = "type"
        return (f"Server: <b>{data['server']}</b> ✅\n\n"
                f"Account type? Reply with <code>demo</code> or <code>live</code>")

    elif step == "type":
        acc_type = text.strip().lower()
        if acc_type not in ("demo", "live"):
            return "❌ Reply with <code>demo</code> or <code>live</code>"
        data["type"] = acc_type
        pending["step"] = "confirm"

        # Build default paths based on shared MT5 terminal
        default_mt5 = _mt5_data_path()
        acc_id = data["id"]
        return (
            f"<b>📋 Review — new account</b>\n\n"
            f"ID:       <code>{data['id']}</code>\n"
            f"Name:     {data['name']}\n"
            f"Login:    <code>{data['login']}</code>\n"
            f"Password: <code>{'*' * len(data['password'])}</code>\n"
            f"Server:   {data['server']}\n"
            f"Type:     {acc_type}\n\n"
            f"MT5 data file: <code>{default_mt5 or '(not found)'}</code>\n\n"
            f"Reply <b>yes</b> to save, or <b>no</b> to cancel."
        )

    elif step == "confirm":
        if text.strip().lower() not in ("yes", "y"):
            del _pending[chat_id]
            return "❌ Cancelled. Account not saved."

        acc_id = data["id"]
        cfg    = _json(CONFIG_PATH) or {"accounts": []}
        if "accounts" not in cfg:
            cfg["accounts"] = []

        default_mt5  = _mt5_data_path()
        default_cmd  = None
        if cfg["accounts"]:
            first = cfg["accounts"][0]
            default_mt5 = Path(first.get("data_path", "")) if first.get("data_path") else default_mt5
            default_cmd = Path(first.get("cmd_path",  "")) if first.get("cmd_path")  else None

        new_acc = {
            "id":          acc_id,
            "name":        data["name"],
            "type":        data["type"],
            "mt5_login":   data["login"],
            "mt5_password": data["password"],
            "mt5_server":  data["server"],
            "data_path":   str(default_mt5) if default_mt5 else "",
            "cmd_path":    str(default_cmd)  if default_cmd  else "",
            "state_path":  str(HERE / f"trader_state_{acc_id}.json"),
            "log_path":    str(HERE / f"trader_{acc_id}.log"),
            "memory_path": str(HERE / f"trade_memory_{acc_id}.json"),
            "journal_path": str(HERE / f"trade_journal_{acc_id}.log"),
        }
        cfg["accounts"].append(new_acc)
        _write_config(cfg)
        del _pending[chat_id]

        return (
            f"✅ <b>Account saved!</b>\n\n"
            f"<code>{acc_id}</code> — {data['name']} [{data['type']}]\n\n"
            f"Next steps:\n"
            f"1. Open MT5 and log in to account <code>{data['login']}</code> on <b>{data['server']}</b>\n"
            f"2. Make sure the OMNI EA is attached to a chart\n"
            f"3. Use /switch {acc_id} to make it active\n"
            f"4. Use /restart auto_trader to apply"
        )

    del _pending[chat_id]
    return "❌ Unknown setup state. Setup cancelled."


def cmd_paper(arg: str) -> str:
    arg = arg.lower().strip()
    env_path = PROJECT_ROOT / ".env"
    if arg == "on":
        try:
            lines = env_path.read_text().splitlines() if env_path.exists() else []
            new_lines = [l for l in lines if not l.startswith("OMNI_PAPER_MODE=")]
            new_lines.append("OMNI_PAPER_MODE=true")
            env_path.write_text("\n".join(new_lines) + "\n")
        except Exception:
            pass
        return "✅ Paper mode ON saved to .env\nUse /restart auto_trader to apply."
    elif arg == "off":
        return (
            "⚠️ <b>Disabling paper mode trades REAL MONEY.</b>\n\n"
            "To enable live trading:\n"
            "1. Edit <code>.env</code> → set <code>OMNI_PAPER_MODE=false</code>\n"
            "2. Restart auto_trader: /restart auto_trader\n\n"
            "Not done automatically for safety."
        )
    return "Usage: /paper on  or  /paper off"


def cmd_help() -> str:
    return (
        "<b>🤖 OMNI-ICT Command Center</b>\n\n"
        "<b>📊 Dashboard</b>\n"
        "/dashboard — full snapshot (equity + trades + signals + status)\n"
        "/equity    — live balance &amp; equity from MT5\n"
        "/trades    — open positions with SL/TP\n"
        "/pnl       — today's P&amp;L, equity, streaks\n"
        "/signals   — latest ICT signals\n"
        "/performance — win rate, avg R, expectancy\n"
        "/risk      — current risk settings &amp; limits\n"
        "/status    — service health &amp; PIDs\n"
        "/log &lt;service&gt; — last 20 lines of log\n\n"
        "<b>⚙️ Settings</b>\n"
        "/settings                — view all settings &amp; current values\n"
        "/set risk LOW|MODERATE|HIGH\n"
        "/set freq CONSERVATIVE|NORMAL|AGGRESSIVE\n"
        "/set base_risk 1.5       — risk % per trade\n"
        "/set daily_loss 3.0      — max daily loss %\n"
        "/set max_dd 10.0         — max drawdown % before halt\n"
        "/set min_conf 65         — min signal confidence\n"
        "/set max_trades 3        — max open positions\n"
        "/set scan 10             — seconds between scans\n"
        "/set paper on|off        — toggle paper mode\n\n"
        "<b>🎛 Control</b>\n"
        "/halt         — stop new entries immediately\n"
        "/resume       — lift the halt\n"
        "/restart &lt;svc&gt; — restart a service\n\n"
        "<b>🏦 Accounts</b>\n"
        "/account    — show active account\n"
        "/accounts   — list all accounts\n"
        "/switch &lt;id&gt;  — change active account\n"
        "/addaccount — add new MT5 account (guided)\n"
        "/deleteaccount &lt;id&gt; — remove account\n\n"
        "/help — this message"
    )


# ── Alert monitor ─────────────────────────────────────────────────────────────

class AlertMonitor:
    def __init__(self, chat_id):
        self.chat_id     = chat_id
        self._seen       = set()
        self._halt_state = None
        self._trades     = set()
        alerts = _json(ALERTS_PATH)
        if isinstance(alerts, list):
            for a in alerts:
                self._seen.add((a.get("service"), a.get("code"), a.get("ts")))
        acc_id = _load_active_account()
        state  = _json(_state_path(acc_id))
        if isinstance(state, dict):
            self._trades = set(state.get("active_trades", {}).keys())

    def check(self) -> None:
        self._alerts()
        self._halt()
        self._trades_check()

    def _alerts(self) -> None:
        alerts = _json(ALERTS_PATH)
        if not isinstance(alerts, list):
            return
        for a in alerts:
            key = (a.get("service"), a.get("code"), a.get("ts"))
            if key in self._seen:
                continue
            self._seen.add(key)
            send(self.chat_id,
                 f"🚨 <b>ALERT</b>  <code>{a.get('service','?')}</code>\n"
                 f"Code: <code>{a.get('code','?')}</code>\n"
                 f"Watchdog left it stopped — use /log {a.get('service','?')} to investigate.")

    def _halt(self) -> None:
        cur = HALT_PATH.exists()
        if cur != self._halt_state:
            if self._halt_state is not None:
                send(self.chat_id,
                     "🛑 <b>Trading HALTED</b>" if cur
                     else "✅ <b>Trading RESUMED</b>")
            self._halt_state = cur

    def _trades_check(self) -> None:
        acc_id  = _load_active_account()
        state   = _json(_state_path(acc_id))
        if not isinstance(state, dict):
            return
        current = set(state.get("active_trades", {}).keys())
        for tid in current - self._trades:
            t    = state["active_trades"].get(tid, {})
            sym  = t.get("symbol", "?")
            dir_ = t.get("direction", "?")
            ep   = t.get("entry_price", t.get("entry", 0))
            sl   = t.get("current_sl", t.get("sl", 0))
            icon = "🟢" if dir_ == "BUY" else "🔴"
            send(self.chat_id,
                 f"{icon} <b>Trade Opened</b>\n"
                 f"{sym} {dir_}  entry={ep:.5f}  SL={sl:.5f}\n"
                 f"Ticket: <code>{tid}</code>")
        for tid in self._trades - current:
            send(self.chat_id,
                 f"⚪ <b>Trade Closed</b>  <code>{tid}</code>")
        self._trades = current


# ── Dispatcher ────────────────────────────────────────────────────────────────

def _dispatch(text: str, chat_id) -> str:
    parts = text.strip().split()
    cmd   = parts[0].lower().lstrip("/").split("@")[0]
    acc   = _load_active_account()

    if cmd == "help":        return cmd_help()
    if cmd == "dashboard":   return cmd_dashboard(acc)
    if cmd == "status":      return cmd_status()
    if cmd == "equity":      return cmd_equity(acc)
    if cmd == "trades":      return cmd_trades(acc)
    if cmd == "pnl":         return cmd_pnl(acc)
    if cmd == "signals":     return cmd_signals()
    if cmd == "performance": return cmd_performance(acc)
    if cmd == "risk":        return cmd_risk(acc)
    if cmd == "settings":    return cmd_settings()
    if cmd == "halt":        return cmd_halt(chat_id)
    if cmd == "resume":      return cmd_resume(chat_id)
    if cmd == "account":     return cmd_account(acc)
    if cmd == "accounts":    return cmd_accounts()
    if cmd == "addaccount":  return cmd_addaccount_start(chat_id)
    if cmd == "set":
        key = parts[1].lower() if len(parts) > 1 else ""
        val = " ".join(parts[2:]) if len(parts) > 2 else ""
        if not key:
            return ("Usage: /set &lt;name&gt; &lt;value&gt;\n"
                    "Send /settings to see all options.")
        if not val:
            return (f"Usage: /set {key} &lt;value&gt;\n"
                    f"Send /settings to see current values.")
        return cmd_set(key, val)
    if cmd == "log":
        svc = parts[1].lower() if len(parts) > 1 else ""
        if not svc:
            return ("Usage: /log &lt;service&gt;\n"
                    "Services: orchestrator, auto_trader, server, telegram_bot, watchdog, launchagent")
        return cmd_log(svc)
    if cmd == "restart":
        svc = parts[1].lower() if len(parts) > 1 else ""
        if not svc:
            return "Usage: /restart &lt;service&gt;\n(orchestrator, auto_trader, server, telegram_bot)"
        return cmd_restart(svc)
    if cmd == "switch":
        new_id = parts[1] if len(parts) > 1 else ""
        if not new_id:
            return "Usage: /switch &lt;account_id&gt;"
        return cmd_switch(new_id)
    if cmd == "deleteaccount":
        acc_id = parts[1] if len(parts) > 1 else ""
        if not acc_id:
            return "Usage: /deleteaccount &lt;account_id&gt;"
        return cmd_deleteaccount(acc_id)
    if cmd == "setrisk":
        arg = parts[1] if len(parts) > 1 else ""
        if not arg:
            return "Usage: /setrisk &lt;percent&gt;  (e.g. /setrisk 1.5)"
        return cmd_setrisk(arg)
    if cmd == "paper":
        arg = parts[1] if len(parts) > 1 else ""
        return cmd_paper(arg)

    return f"Unknown command: <code>/{cmd}</code>\nSend /help for the full list."


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not TOKEN:
        log.error("OMNI_TELEGRAM_TOKEN not set. Bot cannot start.")
        sys.exit(1)

    log.info("OMNI-ICT Telegram bot starting")

    chat_id = _load_chat_id()
    if chat_id:
        log.info("Admin chat ID: %s", chat_id)
        try:
            send(chat_id, "🤖 <b>OMNI-ICT bot online.</b>  /dashboard for full view.")
        except Exception:
            pass
    else:
        log.info("No admin yet — send /start to the bot.")

    monitor  = AlertMonitor(chat_id) if chat_id else None
    offset   = 0
    last_chk = time.time()

    while True:
        try:
            updates = get_updates(offset)
        except Exception as e:
            log.warning("getUpdates error: %s — retry in 5s", e)
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg    = upd.get("message", {})
            if not msg:
                continue
            from_id = msg.get("chat", {}).get("id")
            text    = (msg.get("text") or "").strip()
            if not text:
                continue

            # Multi-step setup: if pending conversation, handle non-command text too
            if from_id in _pending and not text.startswith("/"):
                try:
                    reply = _handle_setup(from_id, text)
                except Exception as e:
                    log.exception("setup error for %r", text)
                    reply = f"❌ Setup error: {e}"
                if reply:
                    send(from_id, reply)
                continue

            if not text.startswith("/"):
                continue

            cmd_lower = text.lower().lstrip("/").split("@")[0].split()[0]
            if cmd_lower == "start":
                if chat_id is None:
                    chat_id = from_id
                    _save_chat_id(chat_id)
                    monitor = AlertMonitor(chat_id)
                    send(chat_id,
                         "✅ <b>OMNI-ICT registered!</b>\n"
                         "You'll receive trade &amp; crash alerts here.\n\n"
                         + cmd_help())
                elif from_id == chat_id:
                    send(chat_id, "✅ Already registered.\n" + cmd_help())
                else:
                    # Not the owner — show the public sales page
                    _sales_page(from_id)
                continue

            if from_id != chat_id:
                # Any command from a non-owner gets the sales page
                _sales_page(from_id)
                continue

            # Route /cancel to setup handler if pending
            if cmd_lower == "cancel" and from_id in _pending:
                try:
                    reply = _handle_setup(from_id, "/cancel")
                except Exception:
                    reply = "❌ Cancelled."
                send(chat_id, reply)
                continue

            try:
                reply = _dispatch(text, chat_id)
            except Exception as e:
                log.exception("dispatch error for %r", text)
                reply = f"❌ Internal error: {e}"
            send(chat_id, reply)

        # Proactive alerts
        now = time.time()
        if monitor and (now - last_chk) >= ALERT_POLL_S:
            last_chk = now
            try:
                monitor.check()
            except Exception as e:
                log.warning("Monitor error: %s", e)


if __name__ == "__main__":
    main()
