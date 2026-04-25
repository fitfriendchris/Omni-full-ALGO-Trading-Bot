"""
community_bot.py — OMNI-ICT Central Community & Onboarding Bot
===============================================================
Runs as a background thread inside app.py on Railway.
ALL clients use this single bot — no need to create their own.

Flow:
  1. Client pays → receives license key via email
  2. Client messages this bot → guided credential collection
  3. Bot generates a one-line install command
  4. Client runs it on their VPS — trading starts in ~3 minutes
  5. All trade alerts and commands relay through this bot forever

Set environment variable: COMMUNITY_BOT_TOKEN=<your BotFather token>
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("community_bot")

TOKEN       = os.getenv("COMMUNITY_BOT_TOKEN", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
SERVER_URL  = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")  # set automatically by Railway
if not SERVER_URL:
    SERVER_URL = os.getenv("LICENSE_SERVER_URL",
                           "https://omni-full-algo-trading-bot-production.up.railway.app")

BASE_URL    = f"https://api.telegram.org/bot{TOKEN}"

BROKER_URL   = "https://www.midasfx.com/?ib=1128101"
WEBSITE_URL  = "https://fitfriendchris.github.io/Omni-full-ALGO-Trading-Bot/"
GITHUB_URL   = "https://github.com/fitfriendchris/Omni-full-ALGO-Trading-Bot"
GUIDE_URL    = f"{GITHUB_URL}/blob/main/STARTUP_GUIDE.md"
COMMUNITY_URL = "https://t.me/omni_ict_community"
CHANNEL_URL   = "https://t.me/OMNI_ICT_CHANNEL"
BUY_STARTER  = "https://buy.stripe.com/dRm7sK5U22048aZePc7Re05"
BUY_PRO      = "https://buy.stripe.com/00wdR8eqyeMQ2QF0Ym7Re06"
BUY_ELITE    = "https://buy.stripe.com/5kQ8wO6Y6eMQ62R0Ym7Re07"

COMMON_SERVERS = [
    "ICMarkets-Live",
    "ICMarkets-Demo",
    "XM.COM-Real 3",
    "XM.COM-Demo",
    "Exness-Real",
    "Exness-Demo",
    "FusionMarkets-Live",
    "Other (I'll type it)",
]

RISK_MODES = ["CONSERVATIVE", "MODERATE", "AGGRESSIVE"]

# In-memory state per chat_id (step tracking between messages)
# Persisted state is in the license server DB via /client/session
_state: dict[int, dict] = {}


# ── HTTP helpers ──────────────────────────────────────────────

def _tg(method: str, **params) -> dict:
    url  = f"{BASE_URL}/{method}"
    data = json.dumps(params).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("TG %s error: %s", method, e)
        return {}


def send(chat_id: int, text: str,
         reply_markup: dict | None = None,
         parse_mode: str = "HTML") -> None:
    params: dict[str, Any] = dict(chat_id=chat_id, text=text, parse_mode=parse_mode)
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    _tg("sendMessage", **params)


def answer_callback(callback_id: str) -> None:
    _tg("answerCallbackQuery", callback_query_id=callback_id)


def _ls_post(path: str, payload: dict) -> dict:
    """POST to license server."""
    url  = f"{SERVER_URL}{path}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json",
                                           "X-Admin-Token": ADMIN_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("LS POST %s error: %s", path, e)
        return {}


def _ls_get(path: str) -> dict:
    """GET from license server."""
    url = f"{SERVER_URL}{path}"
    req = urllib.request.Request(url, headers={"X-Admin-Token": ADMIN_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("LS GET %s error: %s", path, e)
        return {}


# ── Message senders ───────────────────────────────────────────

def send_sales(chat_id: int) -> None:
    text = (
        "👋 <b>Welcome to OMNI-ICT!</b>\n\n"
        "The fully automated MT5 trading bot using ICT / Smart Money Concepts.\n\n"
        "<b>✅ What you get:</b>\n"
        "• Detects order blocks, FVGs, BOS/CHoCH, sweeps\n"
        "• Smart compounding + pyramid sizing\n"
        "• Drawdown protection + smart trailing stops\n"
        "• AI market regime detection\n"
        "• Control everything from Telegram (this bot!)\n"
        "• Self-healing — auto-restarts on any crash\n\n"
        "<b>💰 Plans:</b>\n"
        "• Starter — $49/mo (1 account)\n"
        "• Pro — $99/mo (3 accounts) ⭐\n"
        "• Elite — $199/mo (unlimited)\n\n"
        "👇 Choose a plan to get started:"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "🥉 Starter — $49/mo", "url": BUY_STARTER}],
        [{"text": "🥈 Pro — $99/mo  ⭐ Most Popular", "url": BUY_PRO}],
        [{"text": "🥇 Elite — $199/mo", "url": BUY_ELITE}],
        [{"text": "📖 Setup Guide", "url": GUIDE_URL},
         {"text": "🌐 Website", "url": WEBSITE_URL}],
        [{"text": "💬 Community Group", "url": COMMUNITY_URL},
         {"text": "📢 Announcements", "url": CHANNEL_URL}],
        [{"text": "🏦 Open MT5 Account (MidasFX)", "url": BROKER_URL}],
        [{"text": "✅ I already have a license key", "callback_data": "have_key"}],
    ]}
    send(chat_id, text, reply_markup=keyboard)


def send_ask_key(chat_id: int) -> None:
    send(chat_id,
         "Great! 🎉\n\n"
         "Paste your <b>license key</b> — it was emailed to you after payment.\n"
         "It looks like: <code>OMNI-XXXX-XXXX-XXXX</code>")
    _state[chat_id] = {"step": "awaiting_key"}


def send_ask_login(chat_id: int, key: str) -> None:
    send(chat_id,
         "✅ <b>License verified!</b>\n\n"
         "Now let's connect your MT5 account.\n\n"
         "What is your <b>MT5 account number</b> (login ID)?\n"
         "<i>e.g. 123456</i>")
    _state[chat_id] = {"step": "awaiting_login", "key": key}


def send_ask_server(chat_id: int) -> None:
    keyboard = {"inline_keyboard": [
        [{"text": s, "callback_data": f"server:{s}"}]
        for s in COMMON_SERVERS
    ]}
    send(chat_id,
         "Which <b>MT5 server</b> is your account on?\n"
         "<i>Find it in MT5 → File → Login → Server field</i>",
         reply_markup=keyboard)
    _state[chat_id]["step"] = "awaiting_server"


def send_ask_password(chat_id: int) -> None:
    send(chat_id,
         "Almost there! Enter your <b>MT5 account password</b>.\n\n"
         "🔒 <i>This is stored securely on the server and only used to "
         "pre-fill your install script. Delete it anytime with /deletedata</i>")
    _state[chat_id]["step"] = "awaiting_password"


def send_ask_risk(chat_id: int) -> None:
    keyboard = {"inline_keyboard": [
        [
            {"text": "🐢 CONSERVATIVE", "callback_data": "risk:CONSERVATIVE"},
            {"text": "⚖️ MODERATE",     "callback_data": "risk:MODERATE"},
        ],
        [
            {"text": "🚀 AGGRESSIVE",   "callback_data": "risk:AGGRESSIVE"},
        ],
    ]}
    send(chat_id,
         "Choose your <b>risk mode</b>:\n\n"
         "🐢 <b>CONSERVATIVE</b> — 0.5% risk/trade, tight stops\n"
         "⚖️ <b>MODERATE</b> — 1% risk/trade (recommended)\n"
         "🚀 <b>AGGRESSIVE</b> — 2% risk/trade, wider targets\n\n"
         "<i>You can change this anytime with /setrisk</i>",
         reply_markup=keyboard)
    _state[chat_id]["step"] = "awaiting_risk"


def send_install_command(chat_id: int, key: str) -> None:
    install_url = f"{SERVER_URL}/install/{key}"
    text = (
        "🎉 <b>You're all set!</b>\n\n"
        "<b>Last step — run this on your VPS:</b>\n\n"
        f"<pre>bash &lt;(curl -fsSL {install_url})</pre>\n\n"
        "That's it. It will:\n"
        "• Install Python + clone the bot\n"
        "• Write your .env with all your credentials\n"
        "• Show you how to attach the MT5 EA\n\n"
        "🖥️ <b>Need a VPS?</b> A $5/mo Contabo or DigitalOcean works great.\n\n"
        "Once your bot is running, control it right here:\n"
        "/pnl /equity /trades /halt /resume /status\n\n"
        "Join the community while you set up 👇"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "💬 Join Community Group", "url": COMMUNITY_URL}],
        [{"text": "📢 Follow Announcements", "url": CHANNEL_URL}],
        [{"text": "📖 Full Setup Guide", "url": GUIDE_URL},
         {"text": "🏦 Open MT5 (MidasFX)", "url": BROKER_URL}],
    ]}
    send(chat_id, text, reply_markup=keyboard)


def send_dashboard(chat_id: int, key: str) -> None:
    """Send command prompt to an already-active user."""
    send(chat_id,
         "🤖 <b>OMNI-ICT Control Panel</b>\n\n"
         "Send commands to your trading bot:\n\n"
         "/pnl — today's P&amp;L\n"
         "/equity — live balance\n"
         "/trades — open positions\n"
         "/signals — latest ICT signals\n"
         "/performance — win rate &amp; stats\n"
         "/halt — stop new entries\n"
         "/resume — lift halt\n"
         "/setrisk &lt;pct&gt; — change risk %\n"
         "/paper on|off — toggle paper mode\n"
         "/status — all services\n\n"
         "Commands are relayed to your bot in real time.")


# ── Key validation ────────────────────────────────────────────

def _validate_key(key: str) -> dict:
    """Check key against license server."""
    url = f"{SERVER_URL}/validate?key={key}"
    req = urllib.request.Request(url, headers={"User-Agent": "community-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"valid": False, "message": f"HTTP {e.code}"}
    except Exception as e:
        return {"valid": False, "message": str(e)}


def _save_session(data: dict) -> None:
    _ls_post("/client/session", data)


def _relay_command(key: str, text: str) -> None:
    """Store a command for the client's bridge to pick up."""
    _ls_post("/client/session", {"key": key})   # ensure session exists
    # Write as to_client relay message directly via internal route
    # (We call the same DB that app.py uses — same process)
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(os.getenv("DB_PATH", "/app/licenses.db"))
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO client_relay (license_key, direction, content, ts) VALUES (?,?,?,?)",
            (key, "to_client", text, time.time())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("relay_command error: %s", e)


def _get_responses(key: str) -> list[str]:
    """Fetch undelivered responses from the client bridge."""
    result = _ls_get(f"/client/messages/{key}?token={ADMIN_TOKEN}")
    return [m["text"] for m in result.get("messages", [])]


# ── Update handlers ───────────────────────────────────────────

def handle_text(chat_id: int, text: str) -> None:
    st = _state.get(chat_id, {})
    step = st.get("step", "")

    # ── Awaiting license key
    if step == "awaiting_key" or (not step and text.upper().startswith("OMNI-")):
        key = text.strip().upper()
        result = _validate_key(key)
        if not result.get("valid"):
            send(chat_id,
                 f"❌ That key didn't validate: <i>{result.get('message','')}</i>\n\n"
                 "Double-check the key from your email or "
                 "<a href='" + BUY_PRO + "'>subscribe here</a>.")
            return
        plan = result.get("plan", "starter")
        send(chat_id, f"✅ License valid — <b>{plan.title()}</b> plan")
        send_ask_login(chat_id, key)
        return

    # ── Awaiting MT5 login
    if step == "awaiting_login":
        if not text.strip().isdigit():
            send(chat_id, "Please enter just your MT5 <b>account number</b> (digits only).\ne.g. <code>123456</code>")
            return
        _state[chat_id]["mt5_login"] = text.strip()
        send_ask_server(chat_id)
        return

    # ── Awaiting custom server (typed manually)
    if step == "awaiting_server_text":
        _state[chat_id]["mt5_server"] = text.strip()
        _state[chat_id]["step"] = "awaiting_password"
        send_ask_password(chat_id)
        return

    # ── Awaiting password
    if step == "awaiting_password":
        _state[chat_id]["mt5_password"] = text.strip()
        send_ask_risk(chat_id)
        return

    # ── Active user — relay command to their bridge
    key = st.get("key") or _find_key_for_chat(chat_id)
    if key:
        if text.startswith("/"):
            cmd = text.lower()
            if cmd in ("/start", "/help", "/menu"):
                send_dashboard(chat_id, key)
                return
            if cmd == "/deletedata":
                _save_session({"key": key, "mt5_password": ""})
                send(chat_id, "✅ MT5 password deleted from server.")
                return
            _relay_command(key, text)
            send(chat_id, "⏳ Sending to your bot…")
        else:
            send(chat_id,
                 "Use /commands to see available commands, or /help for the full list.\n"
                 "Commands start with /  e.g. /pnl")
        return

    # ── Unknown state → show sales
    send_sales(chat_id)


def handle_callback(chat_id: int, data: str, callback_id: str) -> None:
    answer_callback(callback_id)
    st = _state.get(chat_id, {})

    if data == "have_key":
        send_ask_key(chat_id)
        return

    if data.startswith("server:"):
        server = data[7:]
        if server == "Other (I'll type it)":
            send(chat_id, "Type your MT5 server name exactly as it appears in MT5:")
            _state[chat_id]["step"] = "awaiting_server_text"
        else:
            _state[chat_id]["mt5_server"] = server
            _state[chat_id]["step"] = "awaiting_password"
            send(chat_id, f"✅ Server: <b>{server}</b>")
            send_ask_password(chat_id)
        return

    if data.startswith("risk:"):
        risk = data[5:]
        _state[chat_id]["risk_mode"] = risk
        key = st.get("key", "")
        # Save everything to server
        _save_session({
            "key":             key,
            "telegram_chat_id": chat_id,
            "mt5_login":       st.get("mt5_login", ""),
            "mt5_server":      st.get("mt5_server", ""),
            "mt5_password":    st.get("mt5_password", ""),
            "risk_mode":       risk,
            "onboarding_step": "active",
        })
        _state[chat_id] = {"step": "active", "key": key}
        send(chat_id, f"✅ Risk mode: <b>{risk}</b>")
        send_install_command(chat_id, key)
        return


def _find_key_for_chat(chat_id: int) -> str | None:
    """Look up license key for a chat_id from the DB."""
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(os.getenv("DB_PATH", "/app/licenses.db"))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT license_key FROM client_sessions WHERE telegram_chat_id=?",
            (chat_id,)
        ).fetchone()
        conn.close()
        return row["license_key"] if row else None
    except Exception:
        return None


# ── Response poller — relays bridge results back to users ─────

def _poll_responses():
    """Runs periodically to relay bot responses back to Telegram users."""
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(os.getenv("DB_PATH", "/app/licenses.db"))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Find all active sessions
        sessions = conn.execute(
            "SELECT cs.license_key, cs.telegram_chat_id "
            "FROM client_sessions cs "
            "WHERE cs.telegram_chat_id IS NOT NULL AND cs.onboarding_step='active'"
        ).fetchall()

        for s in sessions:
            key     = s["license_key"]
            chat_id = s["telegram_chat_id"]
            msgs = conn.execute(
                "SELECT id, content FROM client_relay "
                "WHERE license_key=? AND direction='from_client' AND delivered=0 ORDER BY ts",
                (key,)
            ).fetchall()
            if msgs:
                ids = [m["id"] for m in msgs]
                for m in msgs:
                    try:
                        send(chat_id, m["content"])
                    except Exception:
                        pass
                conn.execute(
                    f"UPDATE client_relay SET delivered=1 WHERE id IN ({','.join('?'*len(ids))})",
                    ids
                )
                conn.commit()
        conn.close()
    except Exception as e:
        log.warning("poll_responses error: %s", e)


# ── Main polling loop ─────────────────────────────────────────

def run_bot() -> None:
    if not TOKEN:
        log.error("COMMUNITY_BOT_TOKEN not set — community bot disabled")
        return

    log.info("Community bot starting (token=%s...)", TOKEN[:10])
    offset = 0
    last_relay_check = 0.0

    while True:
        try:
            resp = _tg("getUpdates", offset=offset, timeout=20,
                       allowed_updates=["message", "callback_query"])
            updates = resp.get("result", [])
        except Exception as e:
            log.warning("getUpdates error: %s", e)
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    msg     = upd["message"]
                    chat_id = msg["chat"]["id"]
                    text    = (msg.get("text") or "").strip()
                    if text:
                        if text.lower().startswith("/start"):
                            key = _find_key_for_chat(chat_id)
                            if key:
                                _state[chat_id] = {"step": "active", "key": key}
                                send_dashboard(chat_id, key)
                            else:
                                send_sales(chat_id)
                        else:
                            handle_text(chat_id, text)

                elif "callback_query" in upd:
                    cb      = upd["callback_query"]
                    chat_id = cb["message"]["chat"]["id"]
                    handle_callback(chat_id, cb.get("data", ""), cb["id"])

            except Exception as e:
                log.exception("Error handling update %s: %s", upd.get("update_id"), e)

        # Relay bridge responses back to users every 3 seconds
        now = time.time()
        if now - last_relay_check >= 3:
            _poll_responses()
            last_relay_check = now

        if not updates:
            time.sleep(0.5)
