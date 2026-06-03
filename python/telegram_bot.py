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
    /trading on|off      — enable/disable trading completely
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

import atexit
import errno
import fcntl
import json
import logging
import os
import re
import signal as _signal
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
JOURNAL_PATH    = LOG_DIR / "journal.json"  # Zella Trade Scribe integration

WEBAPP_URL      = os.getenv("OMNI_WEBAPP_URL", "")   # public HTTPS URL for Mini App

POLL_TIMEOUT    = 30     # Telegram long-poll seconds
ALERT_POLL_S    = 5      # how often to check for new alerts/trades (was 10)
TG_CLOSE_PATH   = HERE / "tg_close_cmd.txt"   # bot writes ticket; auto_trader reads+deletes
TRADING_DISABLED_PATH = HERE / "TRADING_DISABLED"  # presence = trading is off
SCALP_ENABLED_PATH    = HERE / "SCALP_ENABLED"     # presence = accumulation scalps allowed
PYRAMID_ENABLED_PATH  = HERE / "PYRAMID_ENABLED"   # presence = winning-trade pyramid scaling
HF_SCALP_PATH         = HERE / "HF_SCALP"          # presence = high-frequency scalp mode

log = logging.getLogger("telegram_bot")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# Conversation state for multi-step flows keyed by chat_id
_pending: dict[int, dict] = {}


# ── Telegram API ──────────────────────────────────────────────────────────────

_TG_AUTH_FAILED = False  # latched on 401/403 so we stop spamming retries

def _api(method: str, **params) -> dict:
    global _TG_AUTH_FAILED
    if _TG_AUTH_FAILED:
        return {}
    url  = f"{BASE_URL}/{method}"
    data = json.dumps(params).encode("utf-8")
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code in (401, 403):
            _TG_AUTH_FAILED = True
            log.error("Telegram auth failed (HTTP %s) — check OMNI_TELEGRAM_TOKEN: %s", e.code, body)
        else:
            log.warning("Telegram %s HTTP %s: %s", method, e.code, body)
        return {}
    except Exception as e:
        log.debug("Telegram %s error: %s", method, e)
        return {}


def send(chat_id, text: str, parse_mode: str = "HTML",
         reply_markup: dict | None = None) -> int:
    """Send a message; returns the message_id (0 on failure)."""
    params: dict = dict(chat_id=chat_id, text=text, parse_mode=parse_mode)
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    result = _api("sendMessage", **params)
    return (result.get("result") or {}).get("message_id", 0)


def edit_message(chat_id, msg_id: int, text: str,
                 parse_mode: str = "HTML",
                 reply_markup: dict | None = None) -> None:
    """Edit an existing message in place."""
    params: dict = dict(chat_id=chat_id, message_id=msg_id,
                        text=text, parse_mode=parse_mode)
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    _api("editMessageText", **params)


def answer_callback(cb_id: str, text: str = "", alert: bool = False) -> None:
    _api("answerCallbackQuery", callback_query_id=cb_id,
         text=text, show_alert=alert)


def get_updates(offset: int, timeout: int = POLL_TIMEOUT) -> list[dict]:
    return _api("getUpdates", offset=offset, timeout=timeout,
                allowed_updates=["message", "callback_query"]).get("result", [])


# ── Inline keyboard builders ──────────────────────────────────────────────────

def _main_kb() -> dict:
    rows = [
        [
            {"text": "📊 Refresh",   "callback_data": "cb:dashboard"},
            {"text": "📈 Trades",    "callback_data": "cb:trades"},
            {"text": "💰 P&L",       "callback_data": "cb:pnl"},
        ],
        [
            {"text": "🎯 Signals",   "callback_data": "cb:signals"},
            {"text": "📓 Journal",   "callback_data": "cb:journal_today"},
            {"text": "⚙️ Settings",  "callback_data": "cb:settings"},
        ],
        [
            {"text": "🛑 Halt",      "callback_data": "cb:halt"},
            {"text": "▶️ Resume",    "callback_data": "cb:resume"},
            {"text": "❓ Help",      "callback_data": "cb:help"},
        ],
    ]
    if WEBAPP_URL:
        rows.append([{"text": "📱 Open Dashboard", "web_app": {"url": WEBAPP_URL}}])
    return {"inline_keyboard": rows}


def _journal_kb() -> dict:
    return {"inline_keyboard": [
        [
            {"text": "📓 Today", "callback_data": "cb:journal_today"},
            {"text": "📅 Week", "callback_data": "cb:journal_week"},
            {"text": "📊 Streak", "callback_data": "cb:journal_streak"},
        ],
        [
            {"text": "🎯 Setups", "callback_data": "cb:journal_setups"},
            {"text": "📅 Calendar", "callback_data": "cb:journal_calendar"},
            {"text": "📤 Export", "callback_data": "cb:journal_export"},
        ],
        [
            {"text": "⬅️ Main Menu", "callback_data": "cb:dashboard"},
        ],
    ]}


def _back_kb() -> dict:
    return {"inline_keyboard": [[
        {"text": "⬅️ Main Menu", "callback_data": "cb:dashboard"},
        {"text": "🔄 Refresh",   "callback_data": "cb:current"},
    ]]}


def _trades_kb(acc_id: str = "") -> dict:
    """Build trades keyboard with one Close button per open trade."""
    state  = _json(_state_path(acc_id)) or {}
    at = state.get("active_trades", {})
    if isinstance(at, dict):
        trades = at
        rows   = []
        for tid, t in list(trades.items())[:6]:
            sym  = t.get("symbol", "?")
            dir_ = t.get("direction", "?")
            icon = "🟢" if dir_ == "BUY" else "🔴"
            rows.append([{
                "text":          f"❌ Close {icon}{sym} #{tid[:6]}",
                "callback_data": f"cb:close:{tid}",
            }])
    elif isinstance(at, list):
        rows = []
        for t in at[:6]:
            if not isinstance(t, dict):
                continue
            sym  = t.get("symbol", "?")
            dir_ = t.get("direction", "?")
            icon = "🟢" if dir_ == "BUY" else "🔴"
            tid  = str(t.get("ticket", "?"))
            rows.append([{
                "text":          f"❌ Close {icon}{sym} #{tid[:6]}",
                "callback_data": f"cb:close:{tid}",
            }])
    else:
        rows = []
    rows.append([
        {"text": "⬅️ Main Menu", "callback_data": "cb:dashboard"},
        {"text": "🔄 Refresh",   "callback_data": "cb:trades"},
    ])
    return {"inline_keyboard": rows}


def _settings_kb() -> dict:
    env  = _env_read()
    risk = env.get("OMNI_RISK_MODE", "MODERATE")
    freq = env.get("OMNI_FREQ_MODE", "NORMAL")

    def rb(m): return {"text": f"✅{m}" if m == risk else m, "callback_data": f"cb:setrisk:{m}"}
    def fb(m): return {"text": f"✅{m}" if m == freq else m, "callback_data": f"cb:setfreq:{m}"}

    return {"inline_keyboard": [
        [{"text": "── Risk Mode ──", "callback_data": "cb:noop"}],
        [rb("LOW"), rb("MODERATE"), rb("HIGH")],
        [{"text": "── Freq Mode ──", "callback_data": "cb:noop"}],
        [fb("CONSERVATIVE"), fb("NORMAL"), fb("AGGRESSIVE")],
        [{"text": "⬅️ Main Menu", "callback_data": "cb:dashboard"}],
    ]}


# ── Callback dispatcher ───────────────────────────────────────────────────────

def _dispatch_callback(cb_id: str, data: str, chat_id: int, msg_id: int) -> None:
    """Handle all inline keyboard button presses."""
    answer_callback(cb_id)   # acknowledge immediately (removes loading spinner)
    acc = _load_active_account()

    if data == "cb:noop":
        return

    elif data in ("cb:dashboard", "cb:current"):
        edit_message(chat_id, msg_id, cmd_dashboard(acc), reply_markup=_main_kb())

    elif data == "cb:trades":
        edit_message(chat_id, msg_id, cmd_trades(acc), reply_markup=_trades_kb(acc))

    elif data == "cb:pnl":
        edit_message(chat_id, msg_id, cmd_pnl(acc), reply_markup=_back_kb())

    elif data == "cb:signals":
        edit_message(chat_id, msg_id, cmd_signals(), reply_markup=_back_kb())

    elif data == "cb:settings":
        edit_message(chat_id, msg_id, cmd_settings(), reply_markup=_settings_kb())

    elif data == "cb:log":
        edit_message(chat_id, msg_id,
                     cmd_log("auto_trader"), reply_markup=_back_kb())

    elif data == "cb:help":
        edit_message(chat_id, msg_id, cmd_help(), reply_markup=_back_kb())

    # ── Journal callbacks (Zella Trade Scribe integration) ─────────
    elif data == "cb:journal_today":
        edit_message(chat_id, msg_id, cmd_journal_today(), reply_markup=_journal_kb())
    elif data == "cb:journal_week":
        edit_message(chat_id, msg_id, cmd_journal_week(), reply_markup=_journal_kb())
    elif data == "cb:journal_streak":
        edit_message(chat_id, msg_id, cmd_journal_streak(), reply_markup=_journal_kb())
    elif data == "cb:journal_setups":
        edit_message(chat_id, msg_id, cmd_journal_setups(), reply_markup=_journal_kb())
    elif data == "cb:journal_calendar":
        edit_message(chat_id, msg_id, cmd_journal_calendar(), reply_markup=_journal_kb())
    elif data == "cb:journal_export":
        edit_message(chat_id, msg_id, cmd_journal_export(chat_id), reply_markup=_journal_kb())

    elif data == "cb:halt":
        reply = cmd_halt(chat_id)
        answer_callback(cb_id, "🛑 Halted", alert=True)
        edit_message(chat_id, msg_id,
                     f"{reply}\n\n{cmd_dashboard(acc)}",
                     reply_markup=_main_kb())

    elif data == "cb:resume":
        reply = cmd_resume(chat_id)
        answer_callback(cb_id, "▶️ Resumed")
        edit_message(chat_id, msg_id,
                     f"{reply}\n\n{cmd_dashboard(acc)}",
                     reply_markup=_main_kb())

    elif data.startswith("cb:setrisk:"):
        mode  = data.split(":")[2]
        reply = cmd_set("risk", mode)
        answer_callback(cb_id, f"Risk → {mode}")
        edit_message(chat_id, msg_id,
                     f"{reply}\n\n{cmd_settings()}",
                     reply_markup=_settings_kb())

    elif data.startswith("cb:setfreq:"):
        mode  = data.split(":")[2]
        reply = cmd_set("freq", mode)
        answer_callback(cb_id, f"Freq → {mode}")
        edit_message(chat_id, msg_id,
                     f"{reply}\n\n{cmd_settings()}",
                     reply_markup=_settings_kb())

    elif data.startswith("cb:close:"):
        ticket = data.split(":", 2)[2]
        try:
            TG_CLOSE_PATH.write_text(ticket)
            answer_callback(cb_id, f"Close #{ticket} sent", alert=True)
            edit_message(chat_id, msg_id,
                         f"❌ <b>Close #{ticket} sent to trader.</b>\n"
                         f"Executes on next scan cycle.\n\n{cmd_trades(acc)}",
                         reply_markup=_trades_kb(acc))
        except Exception as e:
            answer_callback(cb_id, f"Error: {e}", alert=True)


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

def _parse_json_safe(raw: bytes) -> dict | None:
    """Parse JSON with Wine 4MB truncation recovery (same logic as mt5_connector)."""
    import re as _re
    text = raw.decode("utf-8", errors="replace").rstrip('\x00')
    text = _re.sub(r',\s*([\]}])', r'\1', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    last = text.rfind('},')
    if last == -1:
        last = text.rfind('}')
    if last != -1:
        trunk = text[:last + 1]
        od = ad = 0
        ins = esc = False
        for ch in trunk:
            if esc: esc = False; continue
            if ch == '\\' and ins: esc = True; continue
            if ch == '"': ins = not ins; continue
            if ins: continue
            if ch == '{': od += 1
            elif ch == '}': od -= 1
            elif ch == '[': ad += 1
            elif ch == ']': ad -= 1
        try:
            return json.loads(trunk + ']' * max(ad, 0) + '}' * max(od, 0))
        except Exception:
            pass
    return None


def _json(path: Path):
    if not path or not path.exists():
        return None
    try:
        raw = path.read_bytes()
        result = _parse_json_safe(raw)
        return result
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
        if cfg.get("data_path"):
            return Path(cfg["data_path"])
    # Fall back to OMNI_JSON_PATH env var (set in .env / launchd plist)
    env_path = os.getenv("OMNI_JSON_PATH", "")
    if env_path:
        return Path(env_path)
    # Last resort: auto-detect macOS Wine path
    _wine = (Path.home() / "Library/Application Support"
             / "net.metaquotes.wine.metatrader5/drive_c/users/user"
             / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/omni_data.json")
    return _wine if _wine.exists() else None


def _live_equity(acc_id: str = "") -> tuple[float, float, float, float, str]:
    """Returns (balance, equity, free_margin, profit, currency).

    Priority: live MT5 JSON → trader_state cached equity → zeros.
    Falls back gracefully when MT5 data is stale or EA not running.
    Returns currency="SYNC" when MT5 reports account data not yet ready
    (broker handshake still in progress) — callers should show a syncing message.
    """
    bal = eq = free = prof = 0.0
    curr = "USD"

    mt5_path = _mt5_data_path(acc_id)
    if mt5_path and mt5_path.exists():
        data = _json(mt5_path)
        if data:
            acc  = data.get("account", {})
            # EA sets ready=false for ~10-30s after broker connect while
            # AccountInfo* functions still return zeros
            acc_ready = acc.get("ready", True)
            if acc_ready is False:
                return 0.0, 0.0, 0.0, 0.0, "SYNC"
            bal  = float(acc.get("balance") or 0.0)
            eq   = float(acc.get("equity")  or 0.0)
            free = float(acc.get("free_margin", acc.get("margin_free", 0)) or 0.0)
            prof = float(acc.get("profit")  or 0.0)
            curr = acc.get("currency") or "USD"

    # If MT5 returned zeros (EA offline / stale) fall back to trader_state
    if bal == 0.0 and eq == 0.0:
        state = _json(_state_path(acc_id)) or {}
        peak  = float(state.get("peak_equity") or 0.0)
        # Use peak equity as best estimate of account value when MT5 is offline
        if peak > 0:
            bal = eq = peak
            prof = float(state.get("day_profit") or 0.0)

    return bal, eq, free, prof, curr


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
    trading_disabled = TRADING_DISABLED_PATH.exists()

    # Active trades
    at = state.get("active_trades", {})
    trades_lines = []
    trades = list(at.values()) if isinstance(at, dict) else (at if isinstance(at, list) else [])
    if isinstance(at, dict):
        for t in list(at.values())[:4]:
            sym  = t.get("symbol", "?")
            dir_ = t.get("direction", "?")
            ep   = t.get("entry_price", t.get("entry", 0))
            icon = "🟢" if dir_ == "BUY" else "🔴"
            trades_lines.append(f"  {icon} {sym} {dir_} @{ep:.5f}")
    elif isinstance(at, list):
        for t in at[:4]:
            if not isinstance(t, dict):
                continue
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
    ]
    if curr == "SYNC":
        lines.append("  ⏳ <i>Syncing with broker — balance available in ~30s</i>")
    else:
        lines += [
            f"  Balance:  {curr} {bal:,.2f}",
            f"  Equity:   {curr} {eq:,.2f}",
            f"  Open P&L: {curr} {prof:+,.2f}",
            f"  Today:    {curr} {day_pnl:+,.2f}  ({day_tr} trades)",
            f"  Drawdown: {dd:.2f}%  |  Peak: {curr} {peak:,.2f}",
            f"  Streaks:  ✅{wins}W  ❌{losses}L",
        ]
    if halted:
        lines.append(f"  🛑 <b>HALTED</b>: {state.get('halt_reason','')}")
    if trading_disabled:
        lines.append(f"  🛑 <b>TRADING DISABLED</b> — use /trading on to enable")

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
            # Check if broker handshake is still in progress
            if acc.get("ready") is False:
                ts = data.get("timestamp", "")[:19]
                return (f"<b>💰 Live Account</b>  <code>{ts}</code>\n"
                        "⏳ <i>MT5 account syncing with broker…\n"
                        "Balance will appear in ~30 seconds.</i>")
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
    if isinstance(trades, dict):
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
    elif isinstance(trades, list):
        lines = [f"<b>📊 Open Trades ({len(trades)})</b>\n"]
        for t in trades:
            if not isinstance(t, dict):
                continue
            tid  = str(t.get("ticket", "?"))
            sym  = t.get("symbol", "?")
            dir_ = t.get("direction", "?")
            ep   = t.get("entry_price", t.get("entry", 0))
            sl   = t.get("current_sl", t.get("sl", 0))
            tp   = t.get("tp1", t.get("tp", 0))
            tp1  = "✓" if t.get("tp1_taken") else ""
            icon = "🟢" if dir_ == "BUY" else "🔴"
            lines.append(f"{icon} <b>{sym}</b> {dir_}  entry={ep:.5f}")
            lines.append(f"   SL={sl:.5f}  TP1={tp:.5f}{tp1}  ticket={tid}")
    else:
        return "📊 Bad active_trades format."
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


# ── Zella Trade Scribe Journal Commands ───────────────────────────────────────

def _journal_data() -> dict:
    """Load the formatted journal.json from LOG_DIR."""
    if not JOURNAL_PATH.exists():
        return {}
    try:
        return _json(JOURNAL_PATH) or {}
    except Exception:
        return {}


def cmd_journal_today() -> str:
    d = _journal_data()
    if not d:
        return "📓 Journal not initialized.\nRun: <code>cd ~/Omni-full-ALGO-Trading-Bot/journal &\u0026 python3 journal_sync.py</code>"
    s = d.get("summary", {})
    lines = [
        "<b>📓 Today's Journal</b>",
        f"Trades: {s.get('total_trades', 0)}  |  Win Rate: {s.get('win_rate', 0):.1f}%",
        f"P\u0026L: ${s.get('total_pnl', 0):+.2f}  |  Profit Factor: {s.get('profit_factor', 0):.2f}",
        f"Streak: {s.get('current_streak', 0)}  |  Max Drawdown: ${s.get('max_drawdown', 0):.2f}",
        "",
        "Tap 📅 Calendar or 🎯 Setups for details.",
    ]
    return "\n".join(lines)


def cmd_journal_week() -> str:
    d = _journal_data()
    if not d:
        return "📓 Journal not initialized."
    metrics = d.get("day_metrics", [])
    lines = ["<b>📅 7-Day Heatmap</b>\n"]
    for m in metrics[-7:]:
        pnl = m.get("pnl", 0)
        emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        lines.append(f"{emoji} {m.get('date')}  {m.get('trades', 0)}tr  ${pnl:+.2f}  WR {m.get('win_rate', 0):.0f}%")
    if not lines[1:]:
        lines.append("No trades this week.")
    return "\n".join(lines)


def cmd_journal_streak() -> str:
    d = _journal_data()
    if not d:
        return "📓 Journal not initialized."
    s = d.get("summary", {})
    streak = s.get("current_streak", 0)
    best   = s.get("best_streak", 0)
    worst  = s.get("worst_streak", 0)
    lines = [
        "<b>🔥 Streak Analysis</b>",
        f"Current: {streak}  ({'winning' if streak > 0 else 'losing' if streak < 0 else 'neutral'})",
        f"Best:    {best}W",
        f"Worst:   {abs(worst)}L",
    ]
    return "\n".join(lines)


def cmd_journal_setups() -> str:
    d = _journal_data()
    if not d:
        return "📓 Journal not initialized."
    s = d.get("summary", {})
    setups = s.get("best_setups", {})
    if not setups:
        return "<b>🎯 Best Setups</b>\n\nNo enough data yet."
    lines = ["<b>🎯 Best Setups</b>"]
    for name, stats in sorted(setups.items(), key=lambda x: x[1].get("win_rate", 0), reverse=True):
        wr = stats.get("win_rate", 0)
        pf = stats.get("profit_factor", 0)
        emoji = "🟢" if wr >= 60 else "🟡" if wr >= 40 else "🔴"
        lines.append(f"{emoji} <b>{name}</b>  WR {wr:.0f}%  PF {pf:.2f}")
    return "\n".join(lines)


def cmd_journal_calendar() -> str:
    return cmd_journal_week()  # alias


def cmd_journal_export(chat_id) -> str:
    """Send the journal.json file as a document."""
    if not JOURNAL_PATH.exists():
        return "📓 No journal file to export."
    # Send via multipart form — simplified inline
    import urllib.request
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    boundary = "----WebKitFormBoundary"
    content = []
    content.append(f"--{boundary}")
    content.append(f'Content-Disposition: form-data; name="chat_id"')
    content.append("")
    content.append(str(chat_id))
    content.append(f"--{boundary}")
    content.append(f'Content-Disposition: form-data; name="document"; filename="journal.json"')
    content.append("Content-Type: application/json")
    content.append("")
    content.append(JOURNAL_PATH.read_text())
    content.append(f"--{boundary}--")
    body = "\r\n".join(content).encode()
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=30)
        return "📤 Journal exported!"
    except Exception as e:
        return f"❌ Export failed: {e}"


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
        # Read last ~8 KB from the end — avoids OOM on large log files
        chunk = 8 * 1024
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - chunk))
            raw = fh.read(chunk)
        text  = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
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


def cmd_trading_on(chat_id) -> str:
    if TRADING_DISABLED_PATH.exists():
        TRADING_DISABLED_PATH.unlink()
    return "✅ <b>TRADING ENABLED.</b> Bot will now accept new trades."


def cmd_trading_off(chat_id) -> str:
    TRADING_DISABLED_PATH.write_text(
        f"TRADING DISABLED by Telegram {chat_id} at {datetime.now(timezone.utc).isoformat()}")
    return "🛑 <b>TRADING DISABLED.</b> No new trades until /trading on."


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
        f"  scan       = {env.get('OMNI_SCAN_INTERVAL','3 (default)')}s",
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
        "<b>📓 Journal</b>\n"
        "/journal        — today's trading journal\n"
        "/journal week   — 7-day heatmap\n"
        "/journal streak — win/loss streaks\n"
        "/journal setups — best setup performance\n"
        "/journal export — send journal.json\n\n"
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
        "/trading on|off — enable/disable trading completely\n"
        "/restart &lt;svc&gt; — restart a service\n\n"
        "<b>🏦 Accounts</b>\n"
        "/account    — show active account\n"
        "/accounts   — list all accounts\n"
        "/switch &lt;id&gt;  — change active account\n"
        "/addaccount — add new MT5 account (guided)\n"
        "/deleteaccount &lt;id&gt; — remove account\n\n"
        "/pause  — stop new entries\n"
        "/resume — resume entries\n"
        "/status — protocol override state\n"
        "/help — this message"
    )


# ── Alert monitor ─────────────────────────────────────────────────────────────

class AlertMonitor:
    def __init__(self, chat_id):
        self.chat_id        = chat_id
        self._seen          = set()
        self._halt_state    = None
        self._trades        = set()
        self._last_equity   = 0.0
        self._last_dash_push = 0.0   # epoch — throttle auto-dashboard pushes
        alerts = _json(ALERTS_PATH)
        if isinstance(alerts, list):
            for a in alerts:
                self._seen.add((a.get("service"), a.get("code"), a.get("ts")))
        self._limit_orders = set()  # track pending order tickets
        acc_id = _load_active_account()
        state  = _json(_state_path(acc_id))
        if isinstance(state, dict):
            at = state.get("active_trades", {})
            if isinstance(at, dict):
                self._trades = set(at.keys())
                for tid, t in at.items():
                    if t.get("order_type", "") in ("BUY_LIMIT", "SELL_LIMIT"):
                        self._limit_orders.add(tid)
            elif isinstance(at, list):
                self._trades = {t.get("ticket") for t in at if isinstance(t, dict)}
                for t in at:
                    if isinstance(t, dict) and t.get("order_type", "") in ("BUY_LIMIT", "SELL_LIMIT"):
                        self._limit_orders.add(t.get("ticket"))
        self._last_equity = 0.0
        _, self._last_equity, _, _, _ = _live_equity(acc_id)

    def check(self) -> None:
        self._alerts()
        self._halt()
        self._trades_check()
        self._equity_update()
        self._limit_check()

    def _limit_check(self) -> None:
        """Check for new limit orders, fills, and stale cancellations."""
        acc_id = _load_active_account()
        state = _json(_state_path(acc_id))
        if not isinstance(state, dict):
            return
        at = state.get("active_trades", {})
        if isinstance(at, dict):
            trades = at
            current_limit = {tid for tid, t in trades.items()
                             if t.get("order_type", "").upper() in ("BUY_LIMIT", "SELL_LIMIT")}
        elif isinstance(at, list):
            trades = {str(t.get("ticket")): t for t in at if isinstance(t, dict)}
            current_limit = set()
            for t in at:
                if isinstance(t, dict) and t.get("order_type", "").upper() in ("BUY_LIMIT", "SELL_LIMIT"):
                    current_limit.add(str(t.get("ticket")))
        else:
            return
        
        # New limit order placed
        for tid in current_limit - self._limit_orders:
            t = trades.get(tid, {})
            sym = t.get("symbol", "?")
            dir_ = t.get("direction", "?")
            entry = float(t.get("entry", 0) or t.get("entry_price", 0))
            sl = float(t.get("sl", 0))
            tp = float(t.get("tp", 0))
            conf = t.get("confidence", 0)
            conf_pct = int(conf * 100) if isinstance(conf, float) else int(conf or 0)
            icon = "🟢" if "BUY" in dir_.upper() else "🔴"
            send(self.chat_id,
                 f"📥 <b>PENDING LIMIT ORDER</b>\n"
                 f"{icon} <b>{sym}</b> {dir_}\n"
                 f"Limit: <code>{entry:.5f}</code>  |  SL: <code>{sl:.5f}</code>\n"
                 f"TP: <code>{tp:.5f}</code>  |  Conf: {conf_pct}%\n"
                 f"<i>Waiting for price to return to OTE level…</i>")
            self._limit_orders.add(tid)
        
        # Limit converted to market (fallback when price breached)
        for tid in self._limit_orders:
            if tid in trades:
                t = trades.get(tid, {})
                ot = t.get("order_type", "").upper()
                if ot in ("BUY", "SELL", "MARKET"):
                    # Was limit, now market → filled
                    sym = t.get("symbol", "?")
                    dir_ = t.get("direction", "?")
                    entry = float(t.get("entry", 0) or t.get("entry_price", 0))
                    icon = "✅" if "BUY" in dir_.upper() else "🟢"
                    send(self.chat_id,
                         f"🎯 <b>LIMIT FILLED → MARKET</b>\n"
                         f"{icon} <b>{sym}</b> {dir_} @ <code>{entry:.5f}</code>\n"
                         f"<i>Price never returned to limit — executed at market</i>")
                    self._limit_orders.discard(tid)
        
        # Limit order stale/cancelled (removed from active_trades without becoming market)
        for tid in self._limit_orders - current_limit:
            if tid not in trades:
                send(self.chat_id,
                     f"❌ <b>LIMIT ORDER CANCELLED</b>  <code>{tid}</code>\n"
                     f"<i>Price blew past entry by 10+ pips or TTL expired</i>")
                self._limit_orders.discard(tid)
        
        # Update tracked set to match current
        self._limit_orders = current_limit

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
        at = state.get("active_trades", [])
        if isinstance(at, dict):
            current = set(at.keys())
            tr_lookup = at
        elif isinstance(at, list):
            current = {str(t.get("ticket")) for t in at if isinstance(t, dict)}
            tr_lookup = {str(t.get("ticket")): t for t in at if isinstance(t, dict)}
        else:
            return
        # Build lookup for recent closes by ticket
        recent_closes = state.get("recent_closes", []) or []
        closes_by_ticket = {str(c.get("ticket")): c for c in recent_closes if isinstance(c, dict)}

        for tid in current - self._trades:
            t    = tr_lookup.get(tid, {})
            sym  = t.get("symbol") or "?"
            dir_ = t.get("direction") or "?"
            if sym == "?" or dir_ == "?":
                continue  # Skip ghost trades with missing data
            ep   = float(t.get("entry_price") or t.get("entry") or 0)
            sl   = float(t.get("current_sl") or t.get("sl") or 0)
            icon = "🟢" if dir_ == "BUY" else "🔴"
            send(self.chat_id,
                 f"{icon} <b>Trade Opened</b>\n"
                 f"{sym} {dir_}  entry={ep:.5f}  SL={sl:.5f}\n"
                 f"Ticket: <code>{tid}</code>")
        for tid in self._trades - current:
            close = closes_by_ticket.get(str(tid))
            if close:
                outcome = close.get("outcome", "CLOSED")
                icon    = "✅" if outcome == "WIN" else "❌"
                profit  = close.get("profit", 0)
                r_mult  = close.get("r_multiple", 0)
                sign    = "+" if profit >= 0 else ""
                rsign   = "+" if r_mult >= 0 else ""
                send(self.chat_id,
                     f"{icon} <b>TRADE CLOSED — {outcome}</b>\n"
                     f"<b>{close.get('symbol','?')}</b> {close.get('direction','')} | "
                     f"Exit: <b>{close.get('exit_level','?')}</b>\n"
                     f"Entry: {close.get('entry',0):.5g} → Close: {close.get('close',0):.5g}\n"
                     f"P&amp;L: <b>{sign}${profit:.2f}</b> | R: <b>{rsign}{r_mult:.2f}R</b>\n"
                     f"Ticket: <code>{tid}</code>")
            else:
                # Fallback: closure record not yet written, emit minimal alert
                send(self.chat_id,
                     f"⚪ <b>Trade Closed</b>  <code>{tid}</code>\n"
                     f"<i>(details pending — use /pnl for daily P&amp;L)</i>")
        self._trades = current

    def _equity_update(self) -> None:
        """Push a refreshed mini-dashboard when equity moves ≥0.10 or every 5 min."""
        acc_id = _load_active_account()
        bal, eq, _, prof, curr = _live_equity(acc_id)
        now = time.time()
        equity_moved = eq > 0 and abs(eq - self._last_equity) >= 0.10
        periodic     = (now - self._last_dash_push) >= 300  # every 5 min
        if not (equity_moved or periodic):
            return
        self._last_equity    = eq
        self._last_dash_push = now
        state  = _json(_state_path(acc_id)) or {}
        peak   = float(state.get("peak_equity") or eq or 0)
        dd     = ((peak - eq) / peak * 100) if peak > 0 else 0.0
        day_pnl = float(state.get("day_profit") or 0.0)
        day_tr  = int(state.get("day_trades") or 0)
        wins    = int(state.get("win_streak") or 0)
        losses  = int(state.get("loss_streak") or 0)
        trades  = state.get("active_trades", {})
        ts      = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).strftime("%H:%M UTC")
        # Data source tag
        mt5_path = _mt5_data_path(acc_id)
        mt5_live = (mt5_path and mt5_path.exists() and
                    (time.time() - mt5_path.stat().st_mtime) < 60) if mt5_path else False
        src_tag = "🟢 live" if mt5_live else "🟡 cached"
        open_tr = f"{len(trades)} open" if trades else "no open trades"
        msg = (
            f"<b>📊 OMNI-ICT</b>  <code>{ts}</code>  {src_tag}\n"
            f"Balance: <b>{curr} {bal:,.2f}</b>  Equity: <b>{curr} {eq:,.2f}</b>\n"
            f"Open P&amp;L: <b>{curr} {prof:+,.2f}</b>  |  {open_tr}\n"
            f"Today: <b>{curr} {day_pnl:+,.2f}</b> ({day_tr} trades)  "
            f"DD: {dd:.2f}%  Peak: {curr} {peak:,.2f}\n"
            f"Streaks: ✅{wins}W  ❌{losses}L"
        )
        send(self.chat_id, msg)


# ── Dispatcher ────────────────────────────────────────────────────────────────

def _dispatch(text: str, chat_id) -> str:
    parts = text.strip().split()
    cmd   = parts[0].lower().lstrip("/").split("@")[0]
    acc   = _load_active_account()

    if cmd == "help":        return cmd_help()
    if cmd == "menu":        return None   # handled specially below (sends with keyboard)
    if cmd == "dashboard":   return None   # handled specially below (sends with keyboard)
    if cmd == "status":      return cmd_status()
    if cmd == "equity":      return cmd_equity(acc)
    if cmd == "trades":      return None   # handled specially below (sends with keyboard)
    if cmd == "pnl":         return cmd_pnl(acc)
    if cmd == "signals":     return cmd_signals()
    if cmd == "performance": return cmd_performance(acc)
    if cmd == "risk":        return cmd_risk(acc)
    if cmd == "settings":    return cmd_settings()
    if cmd == "halt":        return cmd_halt(chat_id)
    if cmd == "resume":      return cmd_resume(chat_id)
    if cmd == "trading":
        arg = parts[1].lower() if len(parts) > 1 else ""
        if arg == "on":
            return cmd_trading_on(chat_id)
        elif arg == "off":
            return cmd_trading_off(chat_id)
        return "Usage: /trading on  or  /trading off"
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

    # ── Zella Trade Scribe Journal ──
    if cmd == "journal":
        arg = parts[1] if len(parts) > 1 else "today"
        if arg == "today":    return None  # handled with keyboard below
        if arg == "week":     return cmd_journal_week()
        if arg == "streak":   return cmd_journal_streak()
        if arg == "setups":   return cmd_journal_setups()
        if arg == "export":   return cmd_journal_export(chat_id)
        return cmd_journal_today()

    # ── Protocol v27.0 Manual Override Commands ──
    if cmd == "pause":
        _override("STOP_NEW_ENTRIES", "ALL")
        return "⏸ <b>PAUSED</b> — No new entries until /resume"
    if cmd == "resume":
        _override("", "")
        return "▶️ <b>RESUMED</b> — Normal trading"
    if cmd == "force_buy":
        return _override("FORCE_BULLISH", parts[1] if len(parts)>1 else "ALL") or "🟢 FORCE_BULLISH active"
    if cmd == "force_sell":
        return _override("FORCE_BEARISH", parts[1] if len(parts)>1 else "ALL") or "🔴 FORCE_BEARISH active"
    if cmd == "status":
        return _override_status()

    return f"Unknown command: <code>/{cmd}</code>\nSend /help for the full list."

def _override(cmd: str, scope: str):
    import json
    p = Path.home() / "Omni-full-ALGO-Trading-Bot" / "shared" / "manual_override.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        json.dump({"active": bool(cmd), "command": cmd, "scope": scope}, f)

def _override_status() -> str:
    import json
    p = Path.home() / "Omni-full-ALGO-Trading-Bot" / "shared" / "manual_override.json"
    if not p.exists():
        return "✅ No active override"
    with open(p) as f:
        o = json.load(f)
    if not o.get('active'):
        return "✅ No active override"
    return f"🔒 OVERRIDE ACTIVE: {o.get('command')} ({o.get('scope')})"


# ── Single-instance lock ──────────────────────────────────────────────────────
# Telegram's getUpdates is exclusive: if two processes long-poll the same token,
# the older one gets HTTP 409 "Conflict: terminated by other getUpdates request".
# A flock-based PID file guarantees only one telegram_bot process runs at a time
# on this machine. The watchdog will simply restart any duplicate that exits.
_LOCK_PATH = LOG_DIR / "telegram_bot.pid"
_lock_fd: int | None = None


def _acquire_singleton_lock() -> None:
    """Acquire an exclusive flock on the PID file or exit cleanly."""
    global _lock_fd
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            try:
                existing_pid = open(_LOCK_PATH).read().strip()
            except Exception:
                existing_pid = "?"
            log.error("another telegram_bot instance is running (pid=%s) — exiting "
                      "to avoid Telegram getUpdates 409 conflict.", existing_pid)
            os.close(fd)
            sys.exit(0)
        raise
    # Got the lock — write our pid into the file and keep fd open for lifetime
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd

    def _release():
        try:
            if _lock_fd is not None:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                os.close(_lock_fd)
            try:
                _LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
        except Exception:
            pass

    atexit.register(_release)
    # Also release on SIGTERM (watchdog uses this for graceful shutdown)
    _signal.signal(_signal.SIGTERM, lambda *_: (_release(), sys.exit(0)))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not TOKEN:
        log.error("OMNI_TELEGRAM_TOKEN not set. Bot cannot start.")
        sys.exit(1)

    _acquire_singleton_lock()

    log.info("OMNI-ICT Telegram bot starting")

    chat_id = _load_chat_id()
    if chat_id:
        log.info("Admin chat ID: %s", chat_id)
        try:
            send(chat_id,
                 "🤖 <b>OMNI-ICT bot online.</b>  Tap a button or type /help.",
                 reply_markup=_main_kb())
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

            # ── Inline keyboard button press ───────────────────────────────
            cb = upd.get("callback_query")
            if cb:
                cb_chat = cb.get("message", {}).get("chat", {}).get("id")
                cb_mid  = cb.get("message", {}).get("message_id", 0)
                if cb_chat == chat_id:
                    try:
                        _dispatch_callback(cb["id"], cb.get("data",""),
                                           chat_id, cb_mid)
                    except Exception as e:
                        log.warning("callback dispatch error: %s", e)
                        answer_callback(cb["id"], "Error — try again")
                continue

            # ── Regular text message ───────────────────────────────────────
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
                         "You'll receive trade &amp; crash alerts here.",
                         reply_markup=_main_kb())
                elif from_id == chat_id:
                    send(chat_id, "✅ Already registered.",
                         reply_markup=_main_kb())
                else:
                    _sales_page(from_id)
                continue

            if from_id != chat_id:
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

            # Commands that send with inline keyboard
            acc = _load_active_account()
            if cmd_lower in ("menu", "dashboard"):
                send(chat_id, cmd_dashboard(acc), reply_markup=_main_kb())
                continue
            if cmd_lower == "trades":
                send(chat_id, cmd_trades(acc), reply_markup=_trades_kb(acc))
                continue
            if cmd_lower == "journal":
                send(chat_id, cmd_journal_today(), reply_markup=_journal_kb())
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
